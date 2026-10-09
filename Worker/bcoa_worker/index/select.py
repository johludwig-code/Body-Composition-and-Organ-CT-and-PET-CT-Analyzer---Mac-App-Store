"""The automatic selection: which parts of a study qualify, how they rank,
and why (ADR 0023).

The worker decides and the app applies: eligibility, rank and reason are
computed at every regroup and stored per part in `cat_series` (`auto_rank`,
`auto_selected`, `reason_json`), and the app never ranks. A reason is codes
and numbers, `{"v": 1, "outcome": …, "codes": […], "params": {…}}`, never
text, so that the app renders it through its String Catalog. The `held`
outcome (a patient whose ID is not confirmed) is the merge's, not the
worker's: the merge writes it over the worker's reason and keeps that one
under `if_confirmed` for the confirmation that releases it (ADR 0029).

The ranking is a cascade, not a score as in BOCARTA-MOOSE: the plan
prescribes an order, and a cascade can name the one step that decided, where
a weighted sum can only list its terms.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from bcoa_worker.index import image_type, kernels
from bcoa_worker.index.codes import rounded
from bcoa_worker.index.geometry import AXIS_MIN_ABS, Vector, orientation_class
from bcoa_worker.index.kernels import KernelClass, KernelTable

Reason = tuple[str, dict[str, Any]]
Outcome = Literal["chosen", "eligible", "excluded"]

# Secondary Capture and its multi-frame variants are saved pictures, excluded
# whatever their ImageType says (ADR 0023 decision 3).
SECONDARY_CAPTURE_SOP_CLASSES = frozenset(
    {
        "1.2.840.10008.5.1.4.1.1.7",
        "1.2.840.10008.5.1.4.1.1.7.1",
        "1.2.840.10008.5.1.4.1.1.7.2",
        "1.2.840.10008.5.1.4.1.1.7.3",
        "1.2.840.10008.5.1.4.1.1.7.4",
    }
)

# The syntaxes the pinned dcm2niix 1.0.20260724 reads (ADR 0023 decision 5).
# Each was converted on Linux from synthetic CT slices, and the NIfTI held the
# input's values exactly (lossless syntaxes) or within 1 of the values
# imagecodecs 2026.8.16 decodes from the same stream (lossy ones). The same
# probes refused Deflated (1.2.840.10008.1.2.1.99) and JPEG Extended 12-bit
# (1.2.840.10008.1.2.4.51). Syntaxes nobody tried, HTJ2K in RPCL order
# (…4.202) and JPEG XL among them, count as unreadable: a series the converter
# cannot read must not be chosen automatically, and the user can still choose
# it by hand. test_dcm2niix_syntaxes.py is to hold this set to the wheel.
CONVERTIBLE_TRANSFER_SYNTAXES = frozenset(
    {
        "1.2.840.10008.1.2",  # Implicit VR Little Endian
        "1.2.840.10008.1.2.1",  # Explicit VR Little Endian
        "1.2.840.10008.1.2.2",  # Explicit VR Big Endian (retired)
        "1.2.840.10008.1.2.4.50",  # JPEG Baseline (8-bit)
        "1.2.840.10008.1.2.4.57",  # JPEG Lossless
        "1.2.840.10008.1.2.4.70",  # JPEG Lossless, first-order prediction
        "1.2.840.10008.1.2.4.80",  # JPEG-LS Lossless
        "1.2.840.10008.1.2.4.81",  # JPEG-LS Near-Lossless
        "1.2.840.10008.1.2.4.90",  # JPEG 2000 Lossless
        "1.2.840.10008.1.2.4.91",  # JPEG 2000
        "1.2.840.10008.1.2.4.201",  # HTJ2K Lossless
        "1.2.840.10008.1.2.4.203",  # HTJ2K
        "1.2.840.10008.1.2.5",  # RLE Lossless
    }
)

# `cor` and `sag` are not here (ADR 0023): coronal and sagittal stacks already
# fail the geometry rule, so the terms could only ever exclude axial series,
# and in German "Cor" names the heart.
DEFAULT_EXCLUDED_DESCRIPTION_TERMS: tuple[str, ...] = (
    "topogram",
    "scout",
    "localizer",
    "surview",
    "scanogram",
    "dose report",
    "patient protocol",
    "screen save",
    "mip",
    "minip",
    "mpr",
    "vrt",
    "3d",
)


def is_convertible(transfer_syntax_uid: str) -> bool:
    return transfer_syntax_uid in CONVERTIBLE_TRANSFER_SYNTAXES


# ------------------------------------------------------------------ settings


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    """`project_meta.selection_config`, version 1 (ADR 0023 decision 8).

    The defaults are the plan's behavior. Four of them wait for the owner:
    `accept_derived_primary` (OPEN_QUESTIONS #24), `kernel_before_thickness`
    (#28), `thickness_floor_mm` (#29) and `coverage_tolerance_mm` (#30), which
    is 1.0 mm for rounding only, because a series 10 mm short can miss the
    lung apex or the liver dome.
    """

    version: int = 1
    min_slices: int = 50
    coverage_tolerance_mm: float = 1.0
    thickness_tie_mm: float = 0.02
    thickness_floor_mm: float | None = None
    kernel_before_thickness: bool = False
    accept_derived_primary: bool = False
    axial_min_abs_nz: float = AXIS_MIN_ABS
    excluded_image_type_values: tuple[str, ...] = image_type.DEFAULT_EXCLUDED_VALUES
    excluded_description_terms: tuple[str, ...] = DEFAULT_EXCLUDED_DESCRIPTION_TERMS
    kernels: Mapping[str, KernelTable] = field(
        default_factory=lambda: dict(kernels.DEFAULT_KERNELS)
    )
    bulk_thin_ct_max_mm: float = 3.0

    @classmethod
    def from_json(cls, value: object) -> SelectionConfig:
        """Decode leniently: a missing key takes its default, and so does a
        value of the wrong type or outside the range the payload schema
        allows. A configuration saved by an older version therefore still
        decodes, which a strict decoder would refuse the moment a key is
        added."""
        raw: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
        default = cls()
        return cls(
            version=_integer(raw.get("version"), default.version, minimum=1),
            min_slices=_integer(raw.get("min_slices"), default.min_slices, minimum=1),
            coverage_tolerance_mm=_number(
                raw.get("coverage_tolerance_mm"), default.coverage_tolerance_mm
            ),
            thickness_tie_mm=_number(raw.get("thickness_tie_mm"), default.thickness_tie_mm),
            thickness_floor_mm=_finite_number(raw.get("thickness_floor_mm")),
            kernel_before_thickness=_boolean(
                raw.get("kernel_before_thickness"), default.kernel_before_thickness
            ),
            accept_derived_primary=_boolean(
                raw.get("accept_derived_primary"), default.accept_derived_primary
            ),
            axial_min_abs_nz=_number(
                raw.get("axial_min_abs_nz"), default.axial_min_abs_nz, maximum=1.0
            ),
            excluded_image_type_values=_strings(
                raw.get("excluded_image_type_values"), default.excluded_image_type_values
            ),
            excluded_description_terms=_strings(
                raw.get("excluded_description_terms"), default.excluded_description_terms
            ),
            kernels=kernels.tables_from_json(raw.get("kernels")),
            bulk_thin_ct_max_mm=_number(
                raw.get("bulk_thin_ct_max_mm"), default.bulk_thin_ct_max_mm, positive=True
            ),
        )

    def to_json(self) -> dict[str, Any]:
        """The settings as the payload carries them, every key written."""
        return {
            "version": self.version,
            "min_slices": self.min_slices,
            "coverage_tolerance_mm": self.coverage_tolerance_mm,
            "thickness_tie_mm": self.thickness_tie_mm,
            "thickness_floor_mm": self.thickness_floor_mm,
            "kernel_before_thickness": self.kernel_before_thickness,
            "accept_derived_primary": self.accept_derived_primary,
            "axial_min_abs_nz": self.axial_min_abs_nz,
            "excluded_image_type_values": list(self.excluded_image_type_values),
            "excluded_description_terms": list(self.excluded_description_terms),
            "kernels": kernels.tables_to_json(self.kernels),
            "bulk_thin_ct_max_mm": self.bulk_thin_ct_max_mm,
        }

    def sha256(self) -> str:
        """`catalog_meta.selection_config_sha256`: the hash of the decoded
        settings, so that a key left out and the same key written with its
        default are the same settings and queue no regroup."""
        text = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _integer(value: object, default: int, *, minimum: int) -> int:
    # bool is an int in Python and never a count here.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    if isinstance(value, float) and not value.is_integer():
        return default
    return int(value) if value >= minimum else default


def _number(
    value: object, default: float, *, maximum: float = math.inf, positive: bool = False
) -> float:
    number = _finite_number(value, maximum=maximum, positive=positive)
    return default if number is None else number


def _finite_number(
    value: object, *, maximum: float = math.inf, positive: bool = False
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > maximum:
        return None
    if positive and number == 0:
        return None
    return number


def _boolean(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _strings(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        return default
    return tuple(item for item in value if isinstance(item, str))


# ------------------------------------------------------------------ descriptions

_TOKEN = re.compile(r"[^\W_]+")
_SHORT_TERM = 3


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def matching_term(description: str | None, terms: Iterable[str]) -> str | None:
    """The first term, as configured, that the description matches, or None.

    Both sides are compared after NFC and case-folding. A term longer than
    three characters matches as a substring; a shorter one only as a whole
    token, splitting on anything that is not a letter or digit (underscores
    too), so that "Thorax MPR 3.0" matches `mpr` and "Compressed" does not.
    """
    if not description:
        return None
    text = _fold(description)
    tokens: set[str] | None = None
    for term in terms:
        folded = _fold(term).strip()
        if not folded:
            continue
        if len(folded) > _SHORT_TERM:
            if folded in text:
                return term
            continue
        if tokens is None:
            tokens = set(_TOKEN.findall(text))
        if folded in tokens:
            return term
    return None


# ------------------------------------------------------------------ eligibility


@dataclass(frozen=True, slots=True)
class PartFacts:
    """What the selection needs to know about one part of a study.

    The regroup fills it from the catalog: geometry from
    `geometry.stack_geometry` (`normal` and `z_extent_mm` are None for a part
    without geometry), `kernel_class` from `kernels.classify` with the same
    settings, and `warnings` as the number of warning-level checks of the
    part.
    """

    series_uid: str
    part: int
    modality: str
    is_image: bool = True
    sop_class_uid: str | None = None
    # As `image_type.image_type_values` returns them.
    image_type: tuple[str, ...] = ()
    description: str | None = None
    normal: Vector | None = None
    z_extent_mm: float | None = None
    # Distinct positions; None without geometry.
    slice_count: int | None = None
    mixed_frames: bool = False
    truncated_files: int = 0
    missing_pixel_files: int = 0
    # Every transfer syntax among the part's files: the converter reads them
    # all, and one file it cannot read leaves a hole in the volume.
    transfer_syntaxes: frozenset[str] = frozenset()
    nifti: bool = False
    nifti_3d: bool = True
    nifti_named: bool = True
    slice_thickness_mm: float | None = None
    slice_spacing_mm: float | None = None
    kernel: str | None = None
    kernel_class: KernelClass = "unknown"
    warnings: int = 0
    series_number: int | None = None


def eligibility(part: PartFacts, config: SelectionConfig) -> list[Reason]:
    """Every condition of ADR 0023's eligibility table that the part fails,
    in table order, with its parameters. An empty list means it qualifies.

    Every failing condition is recorded, not just the first, so that the user
    who changes one setting sees what else still stands in the way.
    """
    reasons: list[Reason] = []
    if not part.is_image:
        reasons.append(("select.excluded.not_image", {}))
    if part.sop_class_uid in SECONDARY_CAPTURE_SOP_CLASSES:
        reasons.append(("select.excluded.secondary_capture", {}))
    if part.modality.strip().upper() != "CT":
        reasons.append(("select.excluded.not_ct", {"modality": part.modality}))
    if not part.nifti:
        # A NIfTI file has no ImageType; its name stands in for it below.
        reasons.extend(
            image_type.image_type_reasons(
                part.image_type,
                sop_class_uid=part.sop_class_uid,
                excluded_values=config.excluded_image_type_values,
                accept_derived_primary=config.accept_derived_primary,
            )
        )
    if part.normal is None or part.z_extent_mm is None:
        reasons.append(("select.excluded.no_geometry", {}))
    elif abs(part.normal[2]) < config.axial_min_abs_nz:
        reasons.append(("select.excluded.not_axial", {"orientation": _orientation_code(part)}))
    if part.mixed_frames:
        reasons.append(("select.excluded.mixed_frames", {}))
    term = matching_term(part.description, config.excluded_description_terms)
    if term is not None:
        reasons.append(("select.excluded.description", {"term": term}))
    if part.slice_count is not None and part.slice_count < config.min_slices:
        reasons.append(
            (
                "select.excluded.too_few_slices",
                {"count": part.slice_count, "minimum": config.min_slices},
            )
        )
    # The parameters of these codes have names of their own (truncated_files,
    # missing_files, first_value rather than count and value), because a
    # reason holds one params object for all its codes, and a part that is
    # truncated and too short would otherwise carry one count for both.
    if part.truncated_files:
        reasons.append(("select.excluded.truncated", {"truncated_files": part.truncated_files}))
    if part.missing_pixel_files:
        reasons.append(
            ("select.excluded.missing_pixels", {"missing_files": part.missing_pixel_files})
        )
    unreadable = sorted(s for s in part.transfer_syntaxes if not is_convertible(s))
    if unreadable:
        reasons.append(("select.excluded.transfer_syntax", {"syntax": unreadable[0]}))
    if part.nifti and not part.nifti_3d:
        reasons.append(("select.excluded.nifti_not_3d", {}))
    if part.nifti and not part.nifti_named:
        reasons.append(("select.excluded.nifti_unnamed", {}))
    return reasons


def _orientation_code(part: PartFacts) -> str:
    assert part.normal is not None
    plane = orientation_class(part.normal)
    # A setting stricter than 0.95 can refuse a part that the orientation
    # class still calls axial; to the user that part is tilted, not axial.
    return "orientation.oblique" if plane == "axial" else f"orientation.{plane}"


# ------------------------------------------------------------------ ranking

Step = Literal[
    "coverage", "thickness", "kernel", "original", "warnings", "series_number", "uid", "part"
]

_STEPS: tuple[Step, ...] = (
    "coverage",
    "thickness",
    "kernel",
    "original",
    "warnings",
    "series_number",
    "uid",
    "part",
)
_KERNEL_STEPS_FIRST: tuple[Step, ...] = ("coverage", "kernel", "thickness", *_STEPS[3:])
_KERNEL_ORDER: dict[KernelClass, int] = {"soft": 0, "unknown": 1, "sharp": 2}


def steps(config: SelectionConfig) -> tuple[Step, ...]:
    """The cascade's order; `kernel_before_thickness` swaps steps 2 and 3."""
    return _KERNEL_STEPS_FIRST if config.kernel_before_thickness else _STEPS


def _positive(value: float | None) -> float | None:
    # A thickness of 0 or less is no thickness: taken at its word it would be
    # the thinnest there is and win step 2.
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def thickness_mm(part: PartFacts) -> float | None:
    """The slice thickness, or the spacing when the thickness is missing."""
    return _positive(part.slice_thickness_mm) or _positive(part.slice_spacing_mm)


def effective_thickness_mm(part: PartFacts, config: SelectionConfig) -> float:
    """The thickness step 2 compares: max(thickness, floor) when a floor is
    set; infinite when neither thickness nor spacing is known, so that such a
    part ranks last on thickness instead of first."""
    value = thickness_mm(part)
    if value is None:
        return math.inf
    if config.thickness_floor_mm is not None:
        return max(value, config.thickness_floor_mm)
    return value


def _survivors(
    step: Step, parts: Sequence[PartFacts], alive: list[int], config: SelectionConfig
) -> list[int]:
    if step == "coverage":
        best = max(_z(parts[i]) for i in alive)
        return [i for i in alive if _z(parts[i]) >= best - config.coverage_tolerance_mm]
    if step == "thickness":
        thickness = {i: effective_thickness_mm(parts[i], config) for i in alive}
        thinnest = min(thickness.values())
        return [i for i in alive if thickness[i] <= thinnest + config.thickness_tie_mm]
    key: Callable[[PartFacts], Any]
    if step == "kernel":
        key = lambda p: _KERNEL_ORDER[p.kernel_class]  # noqa: E731
    elif step == "original":
        key = lambda p: image_type.is_derived(p.image_type)  # noqa: E731
    elif step == "warnings":
        key = lambda p: p.warnings  # noqa: E731
    elif step == "series_number":
        key = lambda p: (p.series_number is None, p.series_number or 0)  # noqa: E731
    elif step == "uid":
        key = lambda p: p.series_uid  # noqa: E731
    else:
        key = lambda p: p.part  # noqa: E731
    best_key = min(key(parts[i]) for i in alive)
    return [i for i in alive if key(parts[i]) == best_key]


def _z(part: PartFacts) -> float:
    assert part.z_extent_mm is not None
    return part.z_extent_mm


def _cascade(
    parts: Sequence[PartFacts], alive: list[int], config: SelectionConfig
) -> tuple[int, dict[int, Step]]:
    """The survivor of the cascade over the parts at `alive`, and for every
    other one of them the step at which it dropped out."""
    dropped: dict[int, Step] = {}
    for step in steps(config):
        kept = _survivors(step, parts, alive, config)
        dropped.update((i, step) for i in alive if i not in kept)
        alive = kept
        if len(alive) == 1:
            break
    # Steps 7 and 8 leave one part, since (series UID, part) is unique; the
    # first of the rest stands in should two parts ever be identical.
    dropped.update((i, "part") for i in alive[1:])
    return alive[0], dropped


@dataclass(frozen=True, slots=True)
class Selection:
    """The automatic choice for one part: its rank (None when it does not
    qualify; only rank 1 is selected automatically) and its reason."""

    auto_rank: int | None
    reason: dict[str, Any]

    @property
    def auto_selected(self) -> bool:
        return self.auto_rank == 1

    @property
    def outcome(self) -> Outcome:
        return self.reason["outcome"]

    @property
    def reason_json(self) -> str:
        """`cat_series.reason_json`, compact and in a fixed key order, so
        that two regroups of the same files write the same bytes."""
        return json.dumps(self.reason, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def reason_payload(outcome: Outcome, reasons: Sequence[Reason]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for _, values in reasons:
        params.update(values)
    return {"v": 1, "outcome": outcome, "codes": [code for code, _ in reasons], "params": params}


def select_study(parts: Sequence[PartFacts], config: SelectionConfig) -> list[Selection]:
    """Eligibility, rank and reason of every part of one study, in the order
    given.

    Rank 1 survives the cascade over all eligible parts; it is removed and the
    cascade runs again for rank 2, and so on. The reason of rank 1 names the
    step at which rank 2 dropped out of the first cascade, and every other
    eligible part's reason names the step at which it dropped out of that
    same cascade, which is where it lost to rank 1.
    """
    results: dict[int, Selection] = {}
    eligible: list[int] = []
    for index, part in enumerate(parts):
        reasons = eligibility(part, config)
        if reasons:
            results[index] = Selection(None, reason_payload("excluded", reasons))
        else:
            eligible.append(index)
    if eligible:
        winner, lost_at = _cascade(parts, eligible, config)
        ranking = [winner]
        rest = [i for i in eligible if i != winner]
        while rest:
            following, _ = _cascade(parts, rest, config)
            ranking.append(following)
            rest.remove(following)
        if len(ranking) == 1:
            chosen: Reason = ("select.chosen.only_candidate", {})
        else:
            chosen = _chosen(lost_at[ranking[1]], parts[winner])
        results[winner] = Selection(1, reason_payload("chosen", [chosen]))
        for rank, index in enumerate(ranking[1:], start=2):
            reason = _eligible(lost_at[index], parts[index], parts[winner])
            results[index] = Selection(rank, reason_payload("eligible", [reason]))
    return [results[index] for index in range(len(parts))]


def _chosen(step: Step, winner: PartFacts) -> Reason:
    if step == "coverage":
        return ("select.chosen.coverage", {"coverage": rounded(_z(winner))})
    if step == "thickness":
        return ("select.chosen.thinnest", {"thickness": _shown_thickness(winner)})
    if step == "kernel":
        if winner.kernel_class == "soft":
            return ("select.chosen.kernel", {"kernel": kernels.first_value(winner.kernel)})
        # An unknown kernel wins only against sharp ones, and calling it a
        # soft-tissue kernel would claim what nobody knows.
        return ("select.chosen.kernel_not_sharp", {})
    if step == "original":
        return ("select.chosen.original", {})
    if step == "warnings":
        return ("select.chosen.fewer_warnings", {})
    if step == "series_number":
        return ("select.chosen.series_number", {"number": winner.series_number})
    if step == "uid":
        return ("select.chosen.tie", {})
    # Two parts of one series, equal in everything else: "taken by its UID"
    # would be untrue, since they share it.
    return ("select.chosen.part", {})


def _eligible(step: Step, part: PartFacts, winner: PartFacts) -> Reason:
    if step == "coverage":
        return (
            "select.eligible.coverage",
            {"coverage": rounded(_z(part)), "chosen_coverage": rounded(_z(winner))},
        )
    if step == "thickness":
        return (
            "select.eligible.thicker",
            {"thickness": _shown_thickness(part), "chosen_thickness": _shown_thickness(winner)},
        )
    if step == "kernel":
        kernel = kernels.first_value(part.kernel)
        if part.kernel_class == "sharp":
            return ("select.eligible.kernel_sharp", {"kernel": kernel})
        if kernel is None:
            return ("select.eligible.kernel_missing", {})
        return ("select.eligible.kernel_unknown", {"kernel": kernel})
    if step == "original":
        return ("select.eligible.derived", {})
    if step == "warnings":
        return (
            "select.eligible.warnings",
            {"count": part.warnings, "chosen_count": winner.warnings},
        )
    if step == "series_number":
        return ("select.eligible.series_number", {})
    if step == "uid":
        return ("select.eligible.tie", {})
    return ("select.eligible.part", {})


def _shown_thickness(part: PartFacts) -> float | None:
    # The thickness as recorded, not the floored one: a 0.6 mm series that
    # tied with 1 mm under a 1 mm floor still has 0.6 mm slices.
    value = thickness_mm(part)
    return None if value is None else rounded(value)
