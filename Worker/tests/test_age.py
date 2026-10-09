"""A study's age from PatientAge or the birth date (ADR 0024 decision 5)."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from bcoa_worker.index.age import StudyAge, parse_date, study_age


@pytest.mark.parametrize(
    ("patient_age", "years"),
    [
        ("045Y", 45.0),
        ("006M", 0.5),
        ("003W", 3 / 52.1775),
        ("010D", 10 / 365.2425),
        ("000D", 0.0),
        ("045y", 45.0),
    ],
)
def test_patient_age_in_days_weeks_months_and_years(patient_age: str, years: float) -> None:
    age = study_age(patient_age, None, "20240101")
    assert age == StudyAge(pytest.approx(years), "age", False)  # type: ignore[arg-type]


@pytest.mark.parametrize("patient_age", ["45", "45Y", "0450Y", "045 Y", "045X", "", "   "])
def test_a_malformed_patient_age_falls_back_to_the_birth_date(patient_age: str) -> None:
    assert study_age(patient_age, "19780301", "20240101") == StudyAge(45.0, "birth_date", False)


def test_completed_years_turn_on_the_birthday() -> None:
    assert study_age(None, "19600615", "20240614").years == 63
    assert study_age(None, "19600615", "20240615").years == 64


@pytest.mark.parametrize(
    ("study", "years"),
    [
        ("20010228", 0),
        ("20010301", 1),
        ("20040228", 3),
        ("20040229", 4),
        ("20050228", 4),
        ("20050301", 5),
    ],
)
def test_born_on_29_february(study: str, years: int) -> None:
    # A year older on 1 March of a common year, on 29 February of a leap year.
    assert study_age(None, "20000229", study) == StudyAge(float(years), "birth_date", False)


def test_a_birth_date_after_the_study_gives_no_age() -> None:
    assert study_age(None, "20250101", "20240101") == StudyAge(None, None, False)


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("20240229", date(2024, 2, 29)),
        (" 20240101 ", date(2024, 1, 1)),
        ("2024.01.15", date(2024, 1, 15)),
        ("20230229", None),
        ("20231301", None),
        ("2024-01-15", None),
        ("202401", None),
        ("２０２４０１０１", None),
        ("", None),
        (None, None),
    ],
)
def test_dicom_dates(value: str | None, parsed: date | None) -> None:
    assert parse_date(value) == parsed


@pytest.mark.parametrize(
    ("birth", "conflict"),
    [
        ("19780101", False),  # 46 completed years against 045Y: one year apart
        ("19790101", False),  # 45
        ("19800101", False),  # 44: one year apart
        ("19800102", True),  # 43
        ("19770101", True),  # 47
    ],
)
def test_a_conflict_is_more_than_a_year_apart(birth: str, conflict: bool) -> None:
    # PatientAge wins either way; the conflict is a check, not a correction.
    assert study_age("045Y", birth, "20240101") == StudyAge(45.0, "age", conflict)


@pytest.mark.parametrize(("patient_age", "conflict"), [("012M", False), ("013M", True)])
def test_the_conflict_threshold_in_months(patient_age: str, conflict: bool) -> None:
    # Born seven months before the study: 0 completed years.
    assert study_age(patient_age, "20230601", "20240101").conflict is conflict


def test_without_a_second_age_there_is_no_conflict() -> None:
    assert study_age("045Y", "19500101", None) == StudyAge(45.0, "age", False)
    assert study_age("045Y", None, "20240101") == StudyAge(45.0, "age", False)
    assert study_age(None, None, "20240101") == StudyAge(None, None, False)
    assert study_age(None, "19600101", None) == StudyAge(None, None, False)


def test_the_birth_date_is_not_kept() -> None:
    # Plan §6: the age is derived and the birth date dropped. The result has
    # no field that could hold it.
    assert [f.name for f in dataclasses.fields(StudyAge)] == ["years", "source", "conflict"]
    assert "1960" not in repr(study_age(None, "19600615", "20240615"))
