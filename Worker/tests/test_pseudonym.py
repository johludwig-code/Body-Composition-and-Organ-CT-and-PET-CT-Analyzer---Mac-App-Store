from __future__ import annotations

from datetime import date

import pytest

from bcoa_worker import pseudonym


def test_pseudonyms() -> None:
    assert pseudonym.pseudonym(1) == "P0001"
    assert pseudonym.pseudonym(12345) == "P12345"


def test_uid_hash_depends_on_project_key() -> None:
    uid = "1.2.826.0.1.3680043.8.498.1"
    a, b = pseudonym.new_project_key(), pseudonym.new_project_key()
    assert pseudonym.hashed_uid(a, uid) == pseudonym.hashed_uid(a, uid)
    assert pseudonym.hashed_uid(a, uid) != pseudonym.hashed_uid(b, uid)
    assert uid not in pseudonym.hashed_uid(a, uid)
    with pytest.raises(ValueError):
        pseudonym.hashed_uid(b"short", uid)


def test_age_on_and_before_birthday() -> None:
    assert pseudonym.age_in_years(date(1960, 6, 15), date(2024, 6, 14)) == 63
    assert pseudonym.age_in_years(date(1960, 6, 15), date(2024, 6, 15)) == 64


def test_dicom_age_strings() -> None:
    assert pseudonym.parse_dicom_age("045Y") == 45
    assert pseudonym.parse_dicom_age("006M") == 0.5
    assert pseudonym.parse_dicom_age("45") is None
