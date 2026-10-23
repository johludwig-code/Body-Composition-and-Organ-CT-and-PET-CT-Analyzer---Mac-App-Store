"""The checks the regroup computes from header values rather than from
geometry (ADR 0022 decisions 7 to 13, ADR 0023, ADR 0024): pixel data,
transfer syntaxes, rescale, PET, duplicates, studies and sources.
`geometry.stack_checks` and `geometry.pixel_checks` hold the rest.

Each function takes plain values, the ones of a part's winning files or of a
study's or source's rows, and returns `Check` rows, so that every rule can be
held by a test without a catalog. None of them decides what a part or study
is; the regroup does that and hands over its members.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from bcoa_worker.index.codes import Check, check
from bcoa_worker.index.select import is_convertible


def too_few_slices(slice_count: int | None, minimum: int) -> list[Check]:
    """A part whose distinct positions are known and fewer than the
    minimum. Unknown counts say nothing (C1 of corpus_expected.json): a part
    without geometry already has check.no_geometry."""
    if slice_count is None or slice_count >= minimum:
        return []
    return [check("check.too_few_slices", count=slice_count, minimum=minimum)]


def pixel_data(states: Iterable[str | None]) -> list[Check]:
    """check.truncated and check.missing_pixel_data, counted over the files
    the converter will read; a losing duplicate is not among them."""
    counts = Counter(states)
    found: list[Check] = []
    if counts["truncated"]:
        found.append(check("check.truncated", count=counts["truncated"]))
    if counts["missing"]:
        found.append(check("check.missing_pixel_data", count=counts["missing"]))
    return found


def unreadable_syntax(syntaxes: Iterable[str | None]) -> str | None:
    """The lowest transfer syntax the converter cannot read, as the
    selection names it in select.excluded.transfer_syntax, so that the check
    and the reason name the same one."""
    refused = sorted({s for s in syntaxes if s is not None and not is_convertible(s)})
    return refused[0] if refused else None


def transfer_syntax(syntaxes: Iterable[str | None]) -> list[Check]:
    syntax = unreadable_syntax(syntaxes)
    return [] if syntax is None else [check("check.unsupported_transfer_syntax", syntax=syntax)]


def rescale(
    modality: str | None, pairs: Iterable[tuple[float | None, float | None]]
) -> list[Check]:
    """check.no_rescale for CT with any file missing a slope or an intercept,
    and check.values_vary for any image part whose files do not all share
    one pair.

    A CT without rescale may be stored in raw detector units, and a
    threshold in Hounsfield units then measures nothing; a pair that varies
    is normal for PET, whose slope is scaled per slice, and worth knowing
    for everything else.
    """
    pairs = list(pairs)
    found: list[Check] = []
    if (modality or "").strip().upper() == "CT" and any(
        slope is None or intercept is None for slope, intercept in pairs
    ):
        found.append(check("check.no_rescale"))
    recorded = {pair for pair in pairs if pair != (None, None)}
    if len(recorded) > 1:
        found.append(check("check.values_vary"))
    return found


def burned_in(values: Iterable[str | None]) -> list[Check]:
    if any((value or "").strip().upper() == "YES" for value in values):
        return [check("check.burned_in_annotation")]
    return []


def _pet_facts(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def mode(values: Iterable[Any]) -> Any:
    """The most frequent value that is not None, ties to the smallest, or
    None. Ties go to the smallest so that a regroup of the same files
    decides the same way whatever order SQLite returns them in."""
    counts = Counter(value for value in values if value is not None)
    if not counts:
        return None
    return min(counts.items(), key=lambda item: (-item[1], item[0]))[0]


def pet_attenuation(pet_json: Iterable[str | None]) -> int | None:
    """1 when CorrectedImage holds ATTN, 0 when it is present without it,
    None when no file has the tag (ADR 0022 decision 12): the most frequent
    value the files record."""
    return mode(_pet_facts(raw).get("attenuation_corrected") for raw in pet_json if raw)


def pet(pet_json: Iterable[str | None]) -> list[Check]:
    """The PET checks of a PT part's files, from `files.pet_json`.

    Dose, start time and weight are needed for every slice's SUV, so one
    file without them is enough to say so. Units, decay correction and
    attenuation correction describe the series and are taken as most of its
    files record them; an absent tag raises nothing, because nothing is
    known then (C13).
    """
    # A file without PET facts (none recorded, or not readable as such) is
    # left out rather than taken for a file without dose or weight.
    facts = [found for raw in pet_json if raw and (found := _pet_facts(raw))]
    if not facts:
        return []
    found: list[Check] = []
    units = mode(f.get("units") for f in facts)
    if isinstance(units, str) and units.strip().upper() != "BQML":
        found.append(check("check.pet_units", units=units))
    if any(not (f.get("has_dose") and f.get("has_start_time")) for f in facts):
        found.append(check("check.pet_no_dose"))
    if any(not f.get("has_weight") for f in facts):
        found.append(check("check.pet_no_weight"))
    decay = mode(f.get("decay") for f in facts)
    if isinstance(decay, str) and decay.strip().upper() == "NONE":
        found.append(check("check.pet_not_decay_corrected", value=decay))
    if mode(f.get("attenuation_corrected") for f in facts) == 0:
        found.append(check("check.pet_not_attenuation_corrected"))
    return found


def duplicates(losers: int, conflicts: int, lost: int = 0) -> list[Check]:
    """check.duplicates counts every file that lost all its instances to
    this part, a UID conflict's loser included; check.uid_conflict counts
    the losers that came from another study or series (C7); and
    check.uid_conflict_lost the files of this part's own series that lost
    their instance to a file of another one (ADR 0029)."""
    found: list[Check] = []
    if losers:
        found.append(check("check.duplicates", count=losers))
    if conflicts:
        found.append(check("check.uid_conflict", count=conflicts))
    if lost:
        found.append(check("check.uid_conflict_lost", count=lost))
    return found


def study(
    *,
    eligible: bool,
    links: Iterable[str | None],
    issuers: Iterable[str | None],
    age_conflict: bool,
) -> list[Check]:
    """The checks of one study. `links` are the pid_link of its files and
    `issuers` the issuer_link of the files that carry the study's own link,
    because two issuers matter only for one ID."""
    found: list[Check] = []
    if not eligible:
        found.append(check("check.no_eligible_series"))
    distinct = {link for link in links if link is not None}
    if len(distinct) > 1:
        found.append(check("check.study_patient_conflict", count=len(distinct)))
    distinct_issuers = {issuer for issuer in issuers if issuer is not None}
    if len(distinct_issuers) > 1:
        found.append(check("check.issuer_conflict", count=len(distinct_issuers)))
    if age_conflict:
        found.append(check("check.age_conflict"))
    return found


def source(
    kinds: Mapping[str, int],
    unreadable: Mapping[str, int],
    bad_dirs: int,
    lost_series: Sequence[int] = (),
) -> list[Check]:
    """The checks of one source from its counts of files by kind, its
    unreadable files by read code, its folders that could not be listed and
    the images missing of each series a DICOMDIR lists and no part holds.
    Counts and codes only: "Show Files…" reads which files they are from the
    catalog when the user asks (ADR 0024 decision 11)."""
    found: list[Check] = []
    count = kinds.get("unreadable", 0)
    if count:
        found.append(
            check("check.unreadable_files", count=count, by_code=dict(sorted(unreadable.items())))
        )
    for kind, code in (
        ("not_dicom", "check.not_dicom"),
        ("archive", "check.archives_skipped"),
        ("symlink", "check.symlinks_skipped"),
        ("changing", "check.files_changing"),
    ):
        if kinds.get(kind, 0):
            found.append(check(code, count=kinds[kind]))
    if bad_dirs:
        found.append(check("check.bad_dirs", count=bad_dirs))
    if lost_series:
        found.append(
            check(
                "check.dicomdir_series_missing", series=len(lost_series), missing=sum(lost_series)
            )
        )
    return found


def params_json(found: Check) -> str:
    """`cat_checks.params_json`: compact and with sorted keys, so that two
    regroups of the same files write the same bytes; NaN never reaches it,
    since every number here is a count or a rounded length."""
    return json.dumps(found.params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
