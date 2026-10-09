"""The checks the regroup computes from header values (ADR 0022, ADR 0023,
ADR 0024), each rule on plain values. The geometry checks are held by
test_geometry.py, and the corpus test holds all of them on real files."""

from __future__ import annotations

import json
from typing import Any

import pytest

from bcoa_worker.index import checks
from bcoa_worker.index.codes import Check


def _codes(found: list[Check]) -> dict[str, dict[str, Any]]:
    return {c.code: c.params for c in found}


def _pet(**values: Any) -> str:
    facts = {
        "units": "BQML",
        "decay": "START",
        "attenuation_corrected": 1,
        "has_dose": True,
        "has_start_time": True,
        "has_weight": True,
    }
    facts.update(values)
    return json.dumps(facts)


def test_the_mode_is_the_most_frequent_value_and_ties_go_to_the_smallest() -> None:
    assert checks.mode(["b", "a", "b", None, None, None]) == "b"
    assert checks.mode(["b", "a"]) == "a"
    assert checks.mode([None, None]) is None


def test_too_few_slices_needs_a_known_count() -> None:
    assert _codes(checks.too_few_slices(49, 50)) == {
        "check.too_few_slices": {"count": 49, "minimum": 50}
    }
    assert checks.too_few_slices(50, 50) == []
    assert checks.too_few_slices(None, 50) == []


def test_pixel_data_counts_truncated_and_missing_files() -> None:
    found = checks.pixel_data(["ok", "truncated", "missing", "truncated", None])
    assert _codes(found) == {
        "check.truncated": {"count": 2},
        "check.missing_pixel_data": {"count": 1},
    }
    assert checks.pixel_data(["ok", None]) == []


def test_the_transfer_syntax_check_names_the_lowest_one_the_converter_refuses() -> None:
    syntaxes = ["1.2.840.10008.1.2.4.51", "1.2.840.10008.1.2.1", "1.2.840.10008.1.2.1.99", None]
    assert _codes(checks.transfer_syntax(syntaxes)) == {
        "check.unsupported_transfer_syntax": {"syntax": "1.2.840.10008.1.2.1.99"}
    }
    assert checks.transfer_syntax(["1.2.840.10008.1.2", None]) == []


def test_rescale() -> None:
    # A CT file without an intercept; values that vary between files.
    assert set(_codes(checks.rescale("CT", [(1.0, -1024.0), (1.0, None)]))) == {
        "check.no_rescale",
        "check.values_vary",
    }
    assert set(_codes(checks.rescale(" ct ", [(1.0, -1024.0), (1.0, -1000.0)]))) == {
        "check.values_vary"
    }
    # PET has no Hounsfield units to lose; neither has a file without either.
    assert checks.rescale("PT", [(None, None), (None, None)]) == []
    assert checks.rescale("CT", [(1.0, -1024.0)] * 3) == []


def test_burned_in_text() -> None:
    assert _codes(checks.burned_in([None, "yes "])) == {"check.burned_in_annotation": {}}
    assert checks.burned_in(["NO", None]) == []


def test_pet_checks_from_the_files_pet_facts() -> None:
    assert checks.pet([_pet(), _pet()]) == []
    found = checks.pet(
        [
            _pet(units="CNTS", decay="NONE", attenuation_corrected=0),
            _pet(units="CNTS", decay="NONE", attenuation_corrected=0, has_weight=False),
            _pet(units="CNTS", decay="NONE", attenuation_corrected=0, has_start_time=False),
        ]
    )
    assert _codes(found) == {
        "check.pet_units": {"units": "CNTS"},
        "check.pet_no_dose": {},
        "check.pet_no_weight": {},
        "check.pet_not_decay_corrected": {"value": "NONE"},
        "check.pet_not_attenuation_corrected": {},
    }


def test_an_absent_tag_says_nothing() -> None:
    # Without CorrectedImage, Units or DecayCorrection nothing is known
    # (C13 of the corpus).
    assert checks.pet([_pet(units=None, decay=None, attenuation_corrected=None)]) == []
    assert checks.pet_attenuation([_pet(attenuation_corrected=None)]) is None
    assert checks.pet_attenuation([_pet(), _pet(), _pet(attenuation_corrected=0)]) == 1
    assert checks.pet([None, "not json"]) == []


def test_duplicates_and_uid_conflicts() -> None:
    assert _codes(checks.duplicates(51, 1)) == {
        "check.duplicates": {"count": 51},
        "check.uid_conflict": {"count": 1},
    }
    assert checks.duplicates(0, 0) == []


@pytest.mark.parametrize(
    ("values", "codes"),
    [
        ({}, set()),
        ({"eligible": False}, {"check.no_eligible_series"}),
        ({"links": ["pid:a", "pid:b", None, "pid:a"]}, {"check.study_patient_conflict"}),
        ({"issuers": ["issuer:1", "issuer:2"]}, {"check.issuer_conflict"}),
        ({"issuers": ["issuer:1", None, "issuer:1"]}, set()),
        ({"age_conflict": True}, {"check.age_conflict"}),
    ],
)
def test_study_checks(values: dict[str, Any], codes: set[str]) -> None:
    arguments: dict[str, Any] = {
        "eligible": True,
        "links": ["pid:a"],
        "issuers": [],
        "age_conflict": False,
    }
    arguments.update(values)
    assert set(_codes(checks.study(**arguments))) == codes


def test_the_patient_conflict_counts_distinct_links() -> None:
    found = checks.study(
        eligible=True, links=["pid:a", "pid:b", "pid:c", "pid:a"], issuers=[], age_conflict=False
    )
    assert _codes(found) == {"check.study_patient_conflict": {"count": 3}}


def test_source_checks_are_counts_and_codes() -> None:
    found = checks.source(
        {"image": 10, "unreadable": 3, "not_dicom": 2, "archive": 1, "symlink": 4, "changing": 5},
        {"read.permission_denied": 1, "read.invalid_dicom": 2},
        bad_dirs=6,
    )
    assert _codes(found) == {
        "check.unreadable_files": {
            "count": 3,
            "by_code": {"read.invalid_dicom": 2, "read.permission_denied": 1},
        },
        "check.not_dicom": {"count": 2},
        "check.archives_skipped": {"count": 1},
        "check.symlinks_skipped": {"count": 4},
        "check.files_changing": {"count": 5},
        "check.bad_dirs": {"count": 6},
    }
    assert checks.source({"image": 10}, {}, 0) == []


def test_parameters_are_written_the_same_way_every_time() -> None:
    found = checks.source({"unreadable": 2}, {"read.io_error": 1, "read.invalid_dicom": 1}, 0)
    assert checks.params_json(found[0]) == (
        '{"by_code":{"read.invalid_dicom":1,"read.io_error":1},"count":2}'
    )
