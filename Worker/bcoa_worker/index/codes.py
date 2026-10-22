"""Check rows as the index writes them into `cat_checks` (ADR 0023, ADR 0024).

A check is a code, a level and parameters, never prose: the app renders the
text through its String Catalog, and Protocol/index_codes.json is the one list
of codes, levels and parameter names. `test_index_codes.py` holds the table
below to that list, so a level changed in one place and not the other fails a
test instead of showing a warning as information.
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

Level = Literal["info", "warning"]


class Check(NamedTuple):
    code: str
    level: Level
    params: dict[str, Any]


# Every check the worker itself computes, with its level. The merge-only
# checks (sex_conflict, new_series_since_manual, primary_gone,
# new_studies_not_added, age_inconsistent) are written by the app's SQL and
# are not here, so the worker cannot emit one by mistake.
CHECK_LEVELS: dict[str, Level] = {
    "check.too_few_slices": "warning",
    "check.gap": "warning",
    "check.uneven_spacing": "warning",
    "check.duplicate_positions": "warning",
    "check.gantry_tilt": "warning",
    "check.tilt_tag_only": "info",
    "check.oblique": "info",
    "check.no_geometry": "warning",
    "check.split": "info",
    "check.enhanced_mixed_frames": "warning",
    "check.unsupported_transfer_syntax": "warning",
    "check.truncated": "warning",
    "check.missing_pixel_data": "warning",
    "check.no_rescale": "warning",
    "check.values_vary": "info",
    "check.non_square_pixels": "info",
    "check.duplicates": "info",
    "check.uid_conflict": "warning",
    "check.uid_conflict_lost": "warning",
    "check.dicomdir_incomplete": "warning",
    "check.burned_in_annotation": "info",
    "check.nifti_unnamed": "info",
    "check.pet_units": "info",
    "check.pet_no_dose": "info",
    "check.pet_no_weight": "info",
    "check.pet_not_decay_corrected": "info",
    "check.pet_not_attenuation_corrected": "info",
    "check.no_eligible_series": "info",
    "check.study_patient_conflict": "warning",
    "check.issuer_conflict": "info",
    "check.age_conflict": "info",
    "check.unreadable_files": "info",
    "check.not_dicom": "info",
    "check.archives_skipped": "info",
    "check.symlinks_skipped": "info",
    "check.bad_dirs": "warning",
    "check.dicomdir_series_missing": "warning",
    "check.files_changing": "info",
}


def check(code: str, **params: Any) -> Check:
    """A check row with the level the registry gives its code.

    An unknown code raises KeyError: a code that is not in the table is not
    in the registry either, and the app would show it as unknown.
    """
    return Check(code, CHECK_LEVELS[code], params)


def rounded(value: float) -> float:
    """A length in millimeters or an angle in degrees, as a parameter: to a
    thousandth.

    Enough for GE's 0.625 mm slices, which two decimals would show as 0.62,
    and short enough that `400.00000000000006` never reaches the database.
    The app formats the number for display.
    """
    return round(value, 3)
