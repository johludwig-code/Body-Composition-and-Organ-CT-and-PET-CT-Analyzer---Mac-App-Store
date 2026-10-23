"""A study's age from its headers (ADR 0024 decision 5).

PatientAge is taken where it is written in the standard's form; otherwise the
age is the completed years from PatientBirthDate to StudyDate. The birth date
is read for this and nothing else: it is never returned, stored or logged, so
nothing here keeps it beyond the call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from bcoa_worker.pseudonym import age_in_years, parse_dicom_age

AgeSource = Literal["age", "birth_date"]

# PatientAge and the birth date disagree when they are more than a year apart
# (ADR 0024). Up to a year is rounding: not every system writes PatientAge as
# completed years on the study date.
AGE_CONFLICT_YEARS = 1.0


@dataclass(frozen=True, slots=True)
class StudyAge:
    years: float | None
    source: AgeSource | None
    # Both ages exist and differ by more than AGE_CONFLICT_YEARS
    # (check.age_conflict).
    conflict: bool


def parse_date(value: str | None) -> date | None:
    """A DICOM DA value (YYYYMMDD), or None for anything that is not a real
    date. The ACR-NEMA form YYYY.MM.DD is still found in old archives and is
    read too."""
    if not value:
        return None
    text = value.strip().replace(".", "")
    if len(text) != 8 or not text.isascii() or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def study_age(patient_age: str | None, birth_date: str | None, study_date: str | None) -> StudyAge:
    """The age at the study: PatientAge (nnnD, nnnW, nnnM or nnnY) if valid,
    otherwise completed years from the birth date to the study date.

    Completed years make someone born on 29 February a year older on
    1 March of a common year, not on 28 February. A birth date after the
    study date gives no age rather than a negative one.
    """
    from_age = parse_dicom_age(patient_age) if patient_age else None
    born, on = parse_date(birth_date), parse_date(study_date)
    from_birth = (
        age_in_years(born, on) if born is not None and on is not None and born <= on else None
    )
    conflict = (
        from_age is not None
        and from_birth is not None
        and abs(from_age - from_birth) > AGE_CONFLICT_YEARS
    )
    if from_age is not None:
        return StudyAge(from_age, "age", conflict)
    if from_birth is not None:
        return StudyAge(float(from_birth), "birth_date", False)
    return StudyAge(None, None, False)
