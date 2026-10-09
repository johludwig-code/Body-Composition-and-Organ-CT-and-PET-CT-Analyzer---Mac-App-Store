"""The ImageType rule (ADR 0023 decision 3), held to BOCARTA-MOOSE's table
(its Tests/python/test_image_type.py), the Enhanced VOLUME case and the
places the standard gives each term."""

from __future__ import annotations

import json

import pytest

from bcoa_worker.index.image_type import (
    ENHANCED_CT_STORAGE,
    LEGACY_CONVERTED_ENHANCED_CT_STORAGE,
    image_type_reasons,
    image_type_values,
    is_derived,
)
from conftest import PROTOCOL_ROOT

CT_IMAGE_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
SIEMENS_PET_CT = "DERIVED\\CT_SOM5 SPI\\PRIMARY\\AXIAL"


def _codes(raw: str, *, accept: bool = False, sop_class: str = CT_IMAGE_STORAGE) -> list[str]:
    return [
        code
        for code, _ in image_type_reasons(
            image_type_values(raw), sop_class_uid=sop_class, accept_derived_primary=accept
        )
    ]


# ADR 0023's table: BOCARTA-MOOSE's cases with accept_derived_primary on and
# off; "either" rows appear twice.
TABLE = [
    (SIEMENS_PET_CT, True, True),
    (SIEMENS_PET_CT, False, False),
    ("ORIGINAL\\PRIMARY\\AXIAL", True, True),
    ("ORIGINAL\\PRIMARY\\AXIAL", False, True),
    ("DERIVED\\SECONDARY\\AXIAL", True, False),
    ("DERIVED\\SECONDARY\\AXIAL", False, False),
    ("DERIVED\\PRIMARY\\REFORMATTED", True, False),
    ("DERIVED\\PRIMARY\\REFORMATTED", False, False),
    ("ORIGINAL\\PRIMARY\\LOCALIZER", True, False),
    ("ORIGINAL\\PRIMARY\\LOCALIZER", False, False),
]


@pytest.mark.parametrize(("raw", "accept", "passes"), TABLE)
def test_bocarta_moose_table(raw: str, accept: bool, passes: bool) -> None:
    assert (_codes(raw, accept=accept) == []) is passes


@pytest.mark.parametrize(
    ("raw", "accept", "expected"),
    [
        (SIEMENS_PET_CT, False, ["select.excluded.derived"]),
        (
            "DERIVED\\SECONDARY\\AXIAL",
            True,
            ["select.excluded.image_type_value", "select.excluded.derived"],
        ),
        (
            "DERIVED\\PRIMARY\\REFORMATTED",
            True,
            ["select.excluded.image_type_value", "select.excluded.not_axial_image_type"],
        ),
        (
            "DERIVED\\PRIMARY\\REFORMATTED",
            False,
            [
                "select.excluded.image_type_value",
                "select.excluded.derived",
                "select.excluded.not_axial_image_type",
            ],
        ),
        (
            "ORIGINAL\\PRIMARY\\LOCALIZER",
            False,
            ["select.excluded.image_type_value", "select.excluded.not_axial_image_type"],
        ),
    ],
)
def test_every_failing_condition_is_recorded_in_table_order(
    raw: str, accept: bool, expected: list[str]
) -> None:
    assert _codes(raw, accept=accept) == expected


def test_the_rule_departs_from_the_plans_wording_where_adr_0023_says() -> None:
    # Accepted by the plan's "contains ORIGINAL and AXIAL", refused here: a
    # MIP is a picture, not the acquisition.
    assert image_type_reasons(image_type_values("ORIGINAL\\PRIMARY\\AXIAL\\MIP")) == [
        ("select.excluded.image_type_value", {"value": "MIP"})
    ]
    # A private term after the plane does not matter.
    assert _codes("ORIGINAL\\PRIMARY\\AXIAL\\CT_SOM5 SPI") == []


@pytest.mark.parametrize("sop_class", [ENHANCED_CT_STORAGE, LEGACY_CONVERTED_ENHANCED_CT_STORAGE])
def test_enhanced_ct_may_say_volume(sop_class: str) -> None:
    assert _codes("ORIGINAL\\PRIMARY\\VOLUME\\NONE", sop_class=sop_class) == []
    assert _codes("ORIGINAL\\PRIMARY\\AXIAL\\NONE", sop_class=sop_class) == []


def test_classic_ct_may_not_say_volume() -> None:
    assert _codes("ORIGINAL\\PRIMARY\\VOLUME\\NONE") == ["select.excluded.not_axial_image_type"]
    assert _codes("ORIGINAL\\PRIMARY\\VOLUME", sop_class=None) == [  # type: ignore[arg-type]
        "select.excluded.not_axial_image_type"
    ]


def test_each_term_counts_only_in_its_place() -> None:
    # ORIGINAL must be value 0, and AXIAL must be among values 2…
    assert image_type_reasons(image_type_values("PRIMARY\\ORIGINAL\\AXIAL")) == [
        ("select.excluded.not_original", {"first_value": "PRIMARY"})
    ]
    assert _codes("ORIGINAL\\AXIAL") == ["select.excluded.not_axial_image_type"]
    # PRIMARY must follow DERIVED for accept_derived_primary to help.
    assert _codes("DERIVED\\CT_SOM5 SPI\\AXIAL", accept=True) == ["select.excluded.derived"]
    assert _codes("PRIMARY\\DERIVED\\AXIAL", accept=True) == ["select.excluded.not_original"]


@pytest.mark.parametrize("raw", [None, "", "\\", " \\ \\ ", ()])
def test_a_missing_image_type_is_reported_alone(raw: str | tuple[str, ...] | None) -> None:
    assert image_type_values(raw) == ()
    assert image_type_reasons(()) == [("select.excluded.image_type_missing", {})]


def test_values_are_read_from_text_or_from_pydicom() -> None:
    assert image_type_values(" original\\primary \\axial") == ("ORIGINAL", "PRIMARY", "AXIAL")
    assert image_type_values(["ORIGINAL", "PRIMARY", "AXIAL"]) == ("ORIGINAL", "PRIMARY", "AXIAL")
    # An empty value keeps its place, so that AXIAL stays value 3 here.
    assert image_type_values("ORIGINAL\\\\AXIAL") == ("ORIGINAL", "", "AXIAL")


def test_the_excluded_value_named_is_the_first_in_image_type_order() -> None:
    # LOCALIZER comes first in the settings, MIP first in the header.
    reasons = image_type_reasons(image_type_values("ORIGINAL\\PRIMARY\\MIP\\LOCALIZER"))
    assert reasons[0] == ("select.excluded.image_type_value", {"value": "MIP"})


def test_the_excluded_values_come_from_the_settings() -> None:
    values = image_type_values("ORIGINAL\\PRIMARY\\AXIAL\\MIP")
    assert image_type_reasons(values, excluded_values=["LOCALIZER"]) == []
    assert image_type_reasons(values, excluded_values=[" mip "]) == [
        ("select.excluded.image_type_value", {"value": "MIP"})
    ]


def test_derived_for_the_ranking() -> None:
    assert is_derived(image_type_values(SIEMENS_PET_CT))
    assert not is_derived(image_type_values("ORIGINAL\\PRIMARY\\AXIAL"))
    assert not is_derived(())


def test_the_codes_and_their_parameters_are_registered() -> None:
    registry = {
        entry["code"]: entry
        for entry in json.loads((PROTOCOL_ROOT / "index_codes.json").read_text())["groups"][
            "select"
        ]["codes"]
    }
    emitted = [
        *image_type_reasons(()),
        *image_type_reasons(image_type_values("XYZ\\PRIMARY\\LOCALIZER")),
        *image_type_reasons(image_type_values("DERIVED\\PRIMARY\\AXIAL")),
    ]
    assert {code for code, _ in emitted} == {
        "select.excluded.image_type_missing",
        "select.excluded.image_type_value",
        "select.excluded.not_original",
        "select.excluded.not_axial_image_type",
        "select.excluded.derived",
    }
    for code, params in emitted:
        assert set(params) == set(registry[code]["params"]), code
