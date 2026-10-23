"""The ImageType rule of the automatic selection (ADR 0023 decisions 2 and 3).

The standard puts the terms of ImageType in fixed places: value 0 is ORIGINAL
or DERIVED, value 1 PRIMARY or SECONDARY, and the plane (AXIAL, LOCALIZER, …)
follows from value 2 on. The rule therefore looks for each term where it
belongs, so that a term in another place cannot pass it.

Both rules that the M2 designs first proposed refused the CT of BOCARTA-MOOSE's
own case: Siemens writes the low-dose CT of a PET/CT as
`DERIVED\\CT_SOM5 SPI\\PRIMARY\\AXIAL`, with PRIMARY third, not second.
`accept_derived_primary` exists for that series; it is off until the owner
answers OPEN_QUESTIONS #24, which leaves the plan's rule ("ORIGINAL and AXIAL,
without LOCALIZER and DERIVED/SECONDARY") in force.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

ENHANCED_CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2.1"
LEGACY_CONVERTED_ENHANCED_CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2.2"

# Enhanced and Legacy Converted CT may write VOLUME where a classic CT writes
# AXIAL, and the plan's wording would refuse every one of them. The geometry
# rule still requires axial slices, so VOLUME lets nothing coronal through.
_VOLUME_SOP_CLASSES = frozenset({ENHANCED_CT_STORAGE, LEGACY_CONVERTED_ENHANCED_CT_STORAGE})

# Reconstructions and pictures rather than the acquisition the pipeline
# needs, even where value 0 says ORIGINAL (ADR 0023 decision 8).
DEFAULT_EXCLUDED_VALUES: tuple[str, ...] = (
    "LOCALIZER",
    "SCOUT",
    "PROJECTION IMAGE",
    "SCREEN SAVE",
    "REFORMATTED",
    "MPR",
    "MIP",
    "MINIP",
    "VRT",
    "CPR",
    "CURVED",
    "SECONDARY",
)

Reason = tuple[str, dict[str, Any]]


def image_type_values(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    """ImageType as its values, stripped and upper-cased.

    Takes the values pydicom returns or the backslash-joined text of the
    catalog. Values keep their places, empty ones included, because the rule
    is about places; an ImageType whose every value is empty is missing.
    """
    if raw is None:
        return ()
    items: Iterable[str] = raw.split("\\") if isinstance(raw, str) else raw
    values = tuple(str(item).strip().upper() for item in items)
    return values if any(values) else ()


def image_type_reasons(
    values: Sequence[str],
    *,
    sop_class_uid: str | None = None,
    excluded_values: Iterable[str] = DEFAULT_EXCLUDED_VALUES,
    accept_derived_primary: bool = False,
) -> list[Reason]:
    """The ImageType codes of the eligibility table that fail, in table order,
    with their parameters; an empty list passes.

    `values` are those of `image_type_values`. A missing ImageType gives
    image_type_missing alone: the conditions on value 0 and on the plane say
    nothing more about an ImageType that is not there.
    """
    if not values:
        return [("select.excluded.image_type_missing", {})]
    reasons: list[Reason] = []
    excluded = {value.strip().upper() for value in excluded_values}
    # The registry's text names one value ("Image Type contains {value}."),
    # and the first in ImageType order is the one a reader of the header
    # sees first.
    hit = next((value for value in values if value in excluded), None)
    if hit is not None:
        reasons.append(("select.excluded.image_type_value", {"value": hit}))
    first = values[0]
    if first == "DERIVED":
        if not (accept_derived_primary and "PRIMARY" in values[1:]):
            reasons.append(("select.excluded.derived", {}))
    elif first != "ORIGINAL":
        reasons.append(("select.excluded.not_original", {"first_value": first}))
    planes = {"AXIAL", "VOLUME"} if sop_class_uid in _VOLUME_SOP_CLASSES else {"AXIAL"}
    if planes.isdisjoint(values[2:]):
        reasons.append(("select.excluded.not_axial_image_type", {}))
    return reasons


def is_derived(values: Sequence[str]) -> bool:
    """Value 0 is DERIVED: ranks after ORIGINAL (ADR 0023 decision 6, step 4)."""
    return bool(values) and values[0] == "DERIVED"
