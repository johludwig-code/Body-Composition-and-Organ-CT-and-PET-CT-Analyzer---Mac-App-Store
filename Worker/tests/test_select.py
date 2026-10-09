"""The automatic selection (ADR 0023): the settings and their lenient decoder,
description terms, every eligibility code in table order, and each step of
the ranking deciding once, with the reason codes and parameters the app
renders."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import jsonschema
import pytest

from bcoa_worker.index import select
from bcoa_worker.index.select import (
    PartFacts,
    Selection,
    SelectionConfig,
    eligibility,
    matching_term,
    select_study,
)
from conftest import PROTOCOL_ROOT

DEFAULTS = SelectionConfig()
CT_IMAGE_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
EXPLICIT_LE = "1.2.840.10008.1.2.1"
SIEMENS_PET_CT = ("DERIVED", "CT_SOM5 SPI", "PRIMARY", "AXIAL")
REGISTRY = {
    entry["code"]: entry
    for entry in json.loads((PROTOCOL_ROOT / "index_codes.json").read_text())["groups"]["select"][
        "codes"
    ]
}
EXCLUDED_ORDER = [code for code in REGISTRY if code.startswith("select.excluded.")]


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((PROTOCOL_ROOT / "fixtures" / "jobs" / name).read_text())["payload"]


def part(
    uid: str = "1.2.826.0.1.3680043.8.498.1", number: int | None = 2, **changes: Any
) -> PartFacts:
    """A 1 mm soft-tissue axial CT of 400 mm that qualifies."""
    values: dict[str, Any] = {
        "series_uid": uid,
        "part": 0,
        "modality": "CT",
        "sop_class_uid": CT_IMAGE_STORAGE,
        "image_type": ("ORIGINAL", "PRIMARY", "AXIAL"),
        "description": "Abdomen 1.0 B30f",
        "normal": (0.0, 0.0, 1.0),
        "z_extent_mm": 400.0,
        "slice_count": 401,
        "transfer_syntaxes": frozenset({EXPLICIT_LE}),
        "slice_thickness_mm": 1.0,
        "slice_spacing_mm": 1.0,
        "kernel": "B30f",
        "kernel_class": "soft",
        "series_number": number,
    }
    values.update(changes)
    return PartFacts(**values)


def _reasons(
    *parts: PartFacts, config: SelectionConfig = DEFAULTS
) -> list[tuple[int | None, str, dict[str, Any]]]:
    """(rank, the one code, params) of each part."""
    result = []
    for selection in select_study(parts, config):
        assert len(selection.reason["codes"]) == 1
        result.append(
            (selection.auto_rank, selection.reason["codes"][0], selection.reason["params"])
        )
    return result


def _codes(facts: PartFacts, config: SelectionConfig = DEFAULTS) -> list[str]:
    return [code for code, _ in eligibility(facts, config)]


# ------------------------------------------------------------------ settings


def test_the_defaults_are_adr_0023s() -> None:
    assert DEFAULTS.min_slices == 50
    assert DEFAULTS.coverage_tolerance_mm == 1.0
    assert DEFAULTS.thickness_tie_mm == 0.02
    assert DEFAULTS.thickness_floor_mm is None
    assert DEFAULTS.kernel_before_thickness is False
    assert DEFAULTS.accept_derived_primary is False
    assert DEFAULTS.axial_min_abs_nz == 0.95
    assert DEFAULTS.bulk_thin_ct_max_mm == 3.0
    assert DEFAULTS.excluded_image_type_values == (
        "LOCALIZER", "SCOUT", "PROJECTION IMAGE", "SCREEN SAVE", "REFORMATTED", "MPR", "MIP",
        "MINIP", "VRT", "CPR", "CURVED", "SECONDARY",
    )  # fmt: skip
    assert DEFAULTS.excluded_description_terms == (
        "topogram", "scout", "localizer", "surview", "scanogram", "dose report",
        "patient protocol", "screen save", "mip", "minip", "mpr", "vrt", "3d",
    )  # fmt: skip


def test_the_scan_fixture_carries_the_defaults() -> None:
    selection = _fixture("index_scan.json")["selection"]
    assert selection == DEFAULTS.to_json()
    assert SelectionConfig.from_json(selection) == DEFAULTS


def test_the_regroup_fixture_carries_the_owners_alternatives() -> None:
    config = SelectionConfig.from_json(_fixture("index_regroup.json")["selection"])
    assert (
        dataclasses.replace(
            config,
            thickness_floor_mm=None,
            kernel_before_thickness=False,
            accept_derived_primary=False,
        )
        == DEFAULTS
    )
    assert (config.thickness_floor_mm, config.kernel_before_thickness) == (1.0, True)
    assert config.accept_derived_primary


def test_the_settings_encode_to_the_payload_schema() -> None:
    schema = json.loads((PROTOCOL_ROOT / "schemas" / "index_payload.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/selection_config", "$defs": schema["$defs"]}
    )
    validator.validate(DEFAULTS.to_json())
    validator.validate(dataclasses.replace(DEFAULTS, thickness_floor_mm=0.8).to_json())


@pytest.mark.parametrize("raw", [None, {}, "settings", [1, 2]])
def test_nothing_usable_decodes_to_the_defaults(raw: object) -> None:
    assert SelectionConfig.from_json(raw) == DEFAULTS


def test_a_missing_key_takes_its_default() -> None:
    config = SelectionConfig.from_json({"min_slices": 30, "kernel_before_thickness": True})
    assert config == dataclasses.replace(DEFAULTS, min_slices=30, kernel_before_thickness=True)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("min_slices", 0),
        ("min_slices", True),
        ("min_slices", "50"),
        ("min_slices", 50.5),
        ("coverage_tolerance_mm", -1.0),
        ("thickness_tie_mm", "0.02"),
        ("axial_min_abs_nz", 1.5),
        ("bulk_thin_ct_max_mm", 0),
        ("accept_derived_primary", 1),
        ("excluded_description_terms", "mip"),
        ("version", 0),
    ],
)
def test_a_malformed_value_takes_its_default(key: str, value: object) -> None:
    assert SelectionConfig.from_json({key: value}) == DEFAULTS


def test_the_thickness_floor_is_a_number_or_null() -> None:
    assert SelectionConfig.from_json({"thickness_floor_mm": 1.0}).thickness_floor_mm == 1.0
    assert SelectionConfig.from_json({"thickness_floor_mm": None}).thickness_floor_mm is None
    assert SelectionConfig.from_json({"thickness_floor_mm": -1}).thickness_floor_mm is None
    assert SelectionConfig.from_json({"min_slices": 50.0}).min_slices == 50


def test_lists_that_are_present_are_taken_as_they_are() -> None:
    config = SelectionConfig.from_json(
        {"excluded_description_terms": [], "excluded_image_type_values": ["MIP", 3]}
    )
    assert config.excluded_description_terms == ()
    assert config.excluded_image_type_values == ("MIP",)


def test_the_hash_is_of_the_decoded_settings() -> None:
    assert SelectionConfig.from_json({}).sha256() == DEFAULTS.sha256()
    assert SelectionConfig.from_json(DEFAULTS.to_json()).sha256() == DEFAULTS.sha256()
    assert SelectionConfig.from_json({"min_slices": 49}).sha256() != DEFAULTS.sha256()
    assert len(DEFAULTS.sha256()) == 64


# ------------------------------------------------------------------ description terms


@pytest.mark.parametrize(
    ("description", "term"),
    [
        ("Thorax MPR 3.0", "mpr"),
        ("Compressed", None),
        ("MIP_axial", "mip"),
        # The first term in the settings' order: vrt comes before 3d.
        ("3D-VRT", "vrt"),
        ("Thorax 3D", "3d"),
        ("Abdomen 3dimensional", None),
        ("Topogramm", "topogram"),
        ("DOSE REPORT", "dose report"),
        ("Patient Protocol", "patient protocol"),
        ("Minip 10 mm", "minip"),
        # Not a default term any more: an axial series named for the heart.
        ("Thorax_Cor 1.0", None),
        ("", None),
        (None, None),
    ],
)
def test_default_terms(description: str | None, term: str | None) -> None:
    assert matching_term(description, DEFAULTS.excluded_description_terms) == term


def test_short_terms_match_whole_tokens_only() -> None:
    # Why cor and sag left the defaults: as tokens they still hit axial series.
    assert matching_term("Thorax_Cor 1.0", ["cor"]) == "cor"
    assert matching_term("Coronary CTA", ["cor"]) is None
    assert matching_term("SAG", ["sag"]) == "sag"


def test_terms_are_compared_after_nfc_and_case_folding() -> None:
    nfd = "Ganzkörper MIP"
    assert matching_term(nfd, ["körper"]) == "körper"
    assert matching_term("STRASSE", ["Straße"]) == "Straße"
    # The term is returned as configured, for the reason's parameter.
    assert matching_term("topogram 0.6", ["Topogram"]) == "Topogram"
    assert matching_term("anything", ["", "  "]) is None


# ------------------------------------------------------------------ eligibility


def test_a_plain_axial_ct_qualifies() -> None:
    assert eligibility(part(), DEFAULTS) == []


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"is_image": False}, ("select.excluded.not_image", {})),
        ({"sop_class_uid": "1.2.840.10008.5.1.4.1.1.7"}, ("select.excluded.secondary_capture", {})),
        (
            {"sop_class_uid": "1.2.840.10008.5.1.4.1.1.7.4"},
            ("select.excluded.secondary_capture", {}),
        ),
        ({"modality": "PT"}, ("select.excluded.not_ct", {"modality": "PT"})),
        ({"image_type": ()}, ("select.excluded.image_type_missing", {})),
        (
            {"image_type": ("ORIGINAL", "PRIMARY", "AXIAL", "MIP")},
            ("select.excluded.image_type_value", {"value": "MIP"}),
        ),
        ({"image_type": SIEMENS_PET_CT}, ("select.excluded.derived", {})),
        (
            {"image_type": ("XYZ", "PRIMARY", "AXIAL")},
            ("select.excluded.not_original", {"first_value": "XYZ"}),
        ),
        (
            {"image_type": ("ORIGINAL", "PRIMARY", "OTHER")},
            ("select.excluded.not_axial_image_type", {}),
        ),
        (
            {"normal": None, "z_extent_mm": None, "slice_count": None},
            ("select.excluded.no_geometry", {}),
        ),
        (
            {"normal": (0.0, 1.0, 0.0)},
            ("select.excluded.not_axial", {"orientation": "orientation.coronal"}),
        ),
        (
            {"normal": (-1.0, 0.0, 0.0)},
            ("select.excluded.not_axial", {"orientation": "orientation.sagittal"}),
        ),
        (
            {"normal": (0.0, 0.34, 0.94)},
            ("select.excluded.not_axial", {"orientation": "orientation.oblique"}),
        ),
        ({"mixed_frames": True}, ("select.excluded.mixed_frames", {})),
        (
            {"description": "Topogram 0.6 T20f"},
            ("select.excluded.description", {"term": "topogram"}),
        ),
        ({"slice_count": 49}, ("select.excluded.too_few_slices", {"count": 49, "minimum": 50})),
        ({"truncated_files": 2}, ("select.excluded.truncated", {"truncated_files": 2})),
        ({"missing_pixel_files": 1}, ("select.excluded.missing_pixels", {"missing_files": 1})),
        (
            {"transfer_syntaxes": frozenset({EXPLICIT_LE, "1.2.840.10008.1.2.1.99"})},
            ("select.excluded.transfer_syntax", {"syntax": "1.2.840.10008.1.2.1.99"}),
        ),
        (
            {"transfer_syntaxes": frozenset({"1.2.840.10008.1.2.4.51"})},
            ("select.excluded.transfer_syntax", {"syntax": "1.2.840.10008.1.2.4.51"}),
        ),
        (
            {"transfer_syntaxes": frozenset({"1.2.826.0.1.3680043.8.498.99"})},
            ("select.excluded.transfer_syntax", {"syntax": "1.2.826.0.1.3680043.8.498.99"}),
        ),
    ],
)
def test_each_condition_alone(changes: dict[str, Any], reason: tuple[str, dict[str, Any]]) -> None:
    assert eligibility(part(**changes), DEFAULTS) == [reason]


def test_the_siemens_pet_ct_passes_with_accept_derived_primary() -> None:
    config = dataclasses.replace(DEFAULTS, accept_derived_primary=True)
    assert eligibility(part(image_type=SIEMENS_PET_CT), config) == []


def test_thresholds_of_slices_and_axial() -> None:
    assert _codes(part(slice_count=50)) == []
    assert _codes(part(slice_count=12), dataclasses.replace(DEFAULTS, min_slices=12)) == []
    assert _codes(part(normal=(0.0, 0.3122, 0.95))) == []
    assert _codes(part(normal=(0.0, 0.3153, 0.949))) == ["select.excluded.not_axial"]


def test_a_stricter_axial_setting_calls_the_part_oblique() -> None:
    config = dataclasses.replace(DEFAULTS, axial_min_abs_nz=0.99)
    assert eligibility(part(normal=(0.0, 0.2431, 0.97)), config) == [
        ("select.excluded.not_axial", {"orientation": "orientation.oblique"})
    ]


def test_every_convertible_syntax_passes() -> None:
    for syntax in select.CONVERTIBLE_TRANSFER_SYNTAXES:
        assert _codes(part(transfer_syntaxes=frozenset({syntax}))) == [], syntax
    assert "1.2.840.10008.1.2.1.99" not in select.CONVERTIBLE_TRANSFER_SYNTAXES
    assert "1.2.840.10008.1.2.4.51" not in select.CONVERTIBLE_TRANSFER_SYNTAXES


def test_nifti_parts_have_no_image_type_and_are_judged_by_their_name() -> None:
    named = part(
        nifti=True,
        image_type=(),
        sop_class_uid=None,
        transfer_syntaxes=frozenset(),
        description=None,
    )
    assert eligibility(named, DEFAULTS) == []
    assert _codes(dataclasses.replace(named, nifti_3d=False)) == ["select.excluded.nifti_not_3d"]
    # A file named outside CT_<id> is OT: a label mask beside a CT is not a CT.
    assert _codes(dataclasses.replace(named, modality="OT", nifti_named=False)) == [
        "select.excluded.not_ct",
        "select.excluded.nifti_unnamed",
    ]


def test_every_failing_condition_is_recorded_in_table_order() -> None:
    worst = part(
        is_image=False,
        sop_class_uid="1.2.840.10008.5.1.4.1.1.7",
        modality="SR",
        image_type=("DERIVED", "SECONDARY", "MPR"),
        normal=None,
        z_extent_mm=None,
        slice_count=3,
        mixed_frames=True,
        description="Dose Report",
        truncated_files=1,
        missing_pixel_files=2,
        transfer_syntaxes=frozenset({"1.2.840.10008.1.2.1.99"}),
    )
    tilted = part(image_type=("XYZ", "PRIMARY"), normal=(0.0, 1.0, 0.0))
    nifti = part(nifti=True, image_type=(), modality="OT", nifti_3d=False, nifti_named=False)
    seen: set[str] = set()
    for facts in (worst, tilted, nifti, part(image_type=())):
        codes = _codes(facts)
        assert codes == sorted(codes, key=EXCLUDED_ORDER.index)
        seen.update(codes)
    assert seen == set(EXCLUDED_ORDER)
    selection = select_study([worst], DEFAULTS)[0]
    assert selection.reason["params"] == {
        "modality": "SR",
        "value": "SECONDARY",
        "term": "dose report",
        "count": 3,
        "minimum": 50,
        "truncated_files": 1,
        "missing_files": 2,
        "syntax": "1.2.840.10008.1.2.1.99",
    }


# ------------------------------------------------------------------ ranking: one step each


def test_only_candidate() -> None:
    localizer = part("1.2.9", image_type=("ORIGINAL", "PRIMARY", "LOCALIZER"))
    chosen, excluded = select_study([part(), localizer], DEFAULTS)
    assert (chosen.auto_rank, chosen.reason["codes"]) == (1, ["select.chosen.only_candidate"])
    assert (excluded.auto_rank, excluded.outcome) == (None, "excluded")


def test_step_1_coverage() -> None:
    long, short = part("1.2.1", slice_thickness_mm=5.0), part("1.2.2", z_extent_mm=398.99)
    assert _reasons(long, short) == [
        (1, "select.chosen.coverage", {"coverage": 400.0}),
        (2, "select.eligible.coverage", {"coverage": 398.99, "chosen_coverage": 400.0}),
    ]


def test_coverage_within_the_tolerance_ties() -> None:
    # 0.99 mm short ties on coverage, and the thinner series wins at step 2.
    thick, thin = part("1.2.1", slice_thickness_mm=5.0), part("1.2.2", z_extent_mm=399.01)
    assert _reasons(thick, thin)[1] == (1, "select.chosen.thinnest", {"thickness": 1.0})


def test_step_2_thinnest() -> None:
    thin, thick = part("1.2.1"), part("1.2.2", slice_thickness_mm=1.021)
    assert _reasons(thin, thick) == [
        (1, "select.chosen.thinnest", {"thickness": 1.0}),
        (2, "select.eligible.thicker", {"thickness": 1.021, "chosen_thickness": 1.0}),
    ]


def test_thickness_within_the_tie_goes_on_to_the_next_step() -> None:
    later, earlier = part("1.2.1", number=5), part("1.2.2", number=3, slice_thickness_mm=1.019)
    assert _reasons(later, earlier) == [
        (2, "select.eligible.series_number", {}),
        (1, "select.chosen.series_number", {"number": 3}),
    ]


def test_step_3_kernel() -> None:
    soft = part("1.2.1")
    assert _reasons(soft, part("1.2.2", kernel="B70f", kernel_class="sharp")) == [
        (1, "select.chosen.kernel", {"kernel": "B30f"}),
        (2, "select.eligible.kernel_sharp", {"kernel": "B70f"}),
    ]
    assert _reasons(soft, part("1.2.2", kernel="XR77\\2", kernel_class="unknown"))[1] == (
        2,
        "select.eligible.kernel_unknown",
        {"kernel": "XR77"},
    )
    assert _reasons(soft, part("1.2.2", kernel=None, kernel_class="unknown"))[1] == (
        2,
        "select.eligible.kernel_missing",
        {},
    )


def test_an_unknown_kernel_that_beats_a_sharp_one_is_not_called_soft() -> None:
    unknown = part("1.2.1", kernel=None, kernel_class="unknown")
    sharp = part("1.2.2", kernel="B70f", kernel_class="sharp")
    assert _reasons(unknown, sharp) == [
        (1, "select.chosen.kernel_not_sharp", {}),
        (2, "select.eligible.kernel_sharp", {"kernel": "B70f"}),
    ]


def test_step_4_original_before_derived() -> None:
    config = dataclasses.replace(DEFAULTS, accept_derived_primary=True)
    derived, original = part("1.2.1", image_type=SIEMENS_PET_CT), part("1.2.2", number=9)
    assert _reasons(derived, original, config=config) == [
        (2, "select.eligible.derived", {}),
        (1, "select.chosen.original", {}),
    ]


def test_step_5_fewer_warnings() -> None:
    quiet, noisy = part("1.2.1", number=9), part("1.2.2", number=1, warnings=2)
    assert _reasons(quiet, noisy) == [
        (1, "select.chosen.fewer_warnings", {}),
        (2, "select.eligible.warnings", {"count": 2, "chosen_count": 0}),
    ]


def test_step_6_lowest_series_number_and_missing_last() -> None:
    assert _reasons(part("1.2.1", number=3), part("1.2.2", number=5)) == [
        (1, "select.chosen.series_number", {"number": 3}),
        (2, "select.eligible.series_number", {}),
    ]
    assert _reasons(part("1.2.1", number=None), part("1.2.2", number=700))[1] == (
        1,
        "select.chosen.series_number",
        {"number": 700},
    )


def test_step_7_series_uid_in_string_order() -> None:
    # String order, as SQLite compares text: "1.2.10" sorts before "1.2.9".
    assert _reasons(part("1.2.9"), part("1.2.10")) == [
        (2, "select.eligible.tie", {}),
        (1, "select.chosen.tie", {}),
    ]
    assert (
        _reasons(part("1.2.9", number=None), part("1.2.10", number=None))[1][1]
        == "select.chosen.tie"
    )


def test_step_8_part() -> None:
    assert _reasons(part("1.2.1", part=1), part("1.2.1", part=0)) == [
        (2, "select.eligible.part", {}),
        (1, "select.chosen.part", {}),
    ]


# ------------------------------------------------------------------ ranking: the cascade


def test_ranks_follow_the_cascade_and_only_rank_1_is_selected() -> None:
    parts = [
        part("1.2.1", slice_thickness_mm=5.0),
        part("1.2.2"),
        part("1.2.3", slice_thickness_mm=2.0),
        part("1.2.4", description="Topogram"),
    ]
    selections = select_study(parts, DEFAULTS)
    assert [s.auto_rank for s in selections] == [3, 1, 2, None]
    assert [s.auto_selected for s in selections] == [False, True, False, False]
    assert [s.outcome for s in selections] == ["eligible", "chosen", "eligible", "excluded"]


def test_rank_1_names_the_step_at_which_rank_2_dropped_out() -> None:
    # B lasts longer in the first cascade (it drops at the kernel), but once A
    # is gone C beats B, so C is rank 2, and C dropped out at the coverage.
    a = part("1.2.1")
    b = part("1.2.2", z_extent_mm=399.2, kernel="B70f", kernel_class="sharp")
    c = part("1.2.3", z_extent_mm=398.5)
    assert _reasons(a, b, c) == [
        (1, "select.chosen.coverage", {"coverage": 400.0}),
        (3, "select.eligible.kernel_sharp", {"kernel": "B70f"}),
        (2, "select.eligible.coverage", {"coverage": 398.5, "chosen_coverage": 400.0}),
    ]


def test_kernel_before_thickness_swaps_steps_2_and_3() -> None:
    thin_sharp = part("1.2.1", kernel="B70f", kernel_class="sharp")
    thick_soft = part("1.2.2", slice_thickness_mm=5.0)
    assert _reasons(thin_sharp, thick_soft) == [
        (1, "select.chosen.thinnest", {"thickness": 1.0}),
        (2, "select.eligible.thicker", {"thickness": 5.0, "chosen_thickness": 1.0}),
    ]
    config = dataclasses.replace(DEFAULTS, kernel_before_thickness=True)
    assert _reasons(thin_sharp, thick_soft, config=config) == [
        (2, "select.eligible.kernel_sharp", {"kernel": "B70f"}),
        (1, "select.chosen.kernel", {"kernel": "B30f"}),
    ]


def test_a_thickness_floor_ties_everything_thinner_than_it() -> None:
    sub_mm, one_mm = part("1.2.1", number=5, slice_thickness_mm=0.6), part("1.2.2", number=2)
    assert _reasons(sub_mm, one_mm)[0] == (1, "select.chosen.thinnest", {"thickness": 0.6})
    config = dataclasses.replace(DEFAULTS, thickness_floor_mm=1.0)
    assert _reasons(sub_mm, one_mm, config=config) == [
        (2, "select.eligible.series_number", {}),
        (1, "select.chosen.series_number", {"number": 2}),
    ]
    # The reason shows the thickness as recorded, not the floor.
    two_mm = part("1.2.3", number=1, slice_thickness_mm=2.0)
    assert _reasons(sub_mm, two_mm, config=config) == [
        (1, "select.chosen.thinnest", {"thickness": 0.6}),
        (2, "select.eligible.thicker", {"thickness": 2.0, "chosen_thickness": 0.6}),
    ]


def test_the_spacing_stands_in_for_a_missing_or_zero_thickness() -> None:
    for missing in (None, 0.0):
        spaced = part("1.2.1", number=5, slice_thickness_mm=missing, slice_spacing_mm=0.8)
        assert _reasons(spaced, part("1.2.2"))[0] == (
            1,
            "select.chosen.thinnest",
            {"thickness": 0.8},
        )


def test_a_part_without_thickness_or_spacing_ranks_last_on_thickness() -> None:
    config = dataclasses.replace(DEFAULTS, min_slices=1)
    unknown = part("1.2.1", number=1, slice_count=1, slice_thickness_mm=None, slice_spacing_mm=None)
    assert _reasons(unknown, part("1.2.2", slice_thickness_mm=5.0), config=config) == [
        (2, "select.eligible.thicker", {"thickness": None, "chosen_thickness": 5.0}),
        (1, "select.chosen.thinnest", {"thickness": 5.0}),
    ]


def test_input_order_does_not_change_the_choice() -> None:
    parts = [
        part(f"1.2.{n}", number=n % 3, slice_thickness_mm=1.0 + (n % 2) * 0.01) for n in range(1, 8)
    ]
    forward = {p.series_uid: s for p, s in zip(parts, select_study(parts, DEFAULTS), strict=True)}
    backward = {
        p.series_uid: s
        for p, s in zip(parts[::-1], select_study(parts[::-1], DEFAULTS), strict=True)
    }
    assert forward == backward


# ------------------------------------------------------------------ the stored reason


def test_the_reason_json_is_compact_and_in_a_fixed_order() -> None:
    selection = select_study([part(), part("1.2.2", z_extent_mm=300.0)], DEFAULTS)[1]
    assert selection.reason_json == (
        '{"v":1,"outcome":"eligible","codes":["select.eligible.coverage"],'
        '"params":{"coverage":300.0,"chosen_coverage":400.0}}'
    )
    assert (
        Selection(None, {"v": 1, "outcome": "excluded", "codes": [], "params": {}}).auto_selected
        is False
    )


def test_parameters_are_rounded_to_a_thousandth() -> None:
    a, b = part("1.2.1", slice_thickness_mm=0.625), part("1.2.2", slice_thickness_mm=1.25 + 1e-12)
    assert _reasons(a, b)[1][2] == {"thickness": 1.25, "chosen_thickness": 0.625}


def test_every_code_select_writes_is_registered_with_its_parameters() -> None:
    derived = dataclasses.replace(DEFAULTS, accept_derived_primary=True)
    studies: list[tuple[list[PartFacts], SelectionConfig]] = [
        ([part()], DEFAULTS),
        ([part("1.2.1"), part("1.2.2", z_extent_mm=300.0)], DEFAULTS),
        ([part("1.2.1"), part("1.2.2", slice_thickness_mm=5.0)], DEFAULTS),
        ([part("1.2.1"), part("1.2.2", kernel="B70f", kernel_class="sharp")], DEFAULTS),
        ([part("1.2.1"), part("1.2.2", kernel="XR77", kernel_class="unknown")], DEFAULTS),
        ([part("1.2.1"), part("1.2.2", kernel=None, kernel_class="unknown")], DEFAULTS),
        (
            [
                part("1.2.1", kernel=None, kernel_class="unknown"),
                part("1.2.2", kernel="B70f", kernel_class="sharp"),
            ],
            DEFAULTS,
        ),
        ([part("1.2.1"), part("1.2.2", image_type=SIEMENS_PET_CT)], derived),
        ([part("1.2.1"), part("1.2.2", warnings=1)], DEFAULTS),
        ([part("1.2.1"), part("1.2.2", number=3)], DEFAULTS),
        ([part("1.2.1"), part("1.2.2")], DEFAULTS),
        ([part("1.2.1"), part("1.2.1", part=1)], DEFAULTS),
        (
            [
                part(
                    is_image=False,
                    sop_class_uid="1.2.840.10008.5.1.4.1.1.7",
                    modality="SR",
                    image_type=("DERIVED", "SECONDARY"),
                    normal=None,
                    z_extent_mm=None,
                    slice_count=3,
                    mixed_frames=True,
                    description="Dose Report",
                    truncated_files=1,
                    missing_pixel_files=1,
                    transfer_syntaxes=frozenset({"1.2.840.10008.1.2.4.51"}),
                ),
                part(image_type=(), normal=(0.0, 1.0, 0.0)),
                part(image_type=("XYZ", "PRIMARY", "AXIAL")),
                part(nifti=True, image_type=(), nifti_3d=False, nifti_named=False),
            ],
            DEFAULTS,
        ),
    ]
    emitted: set[str] = set()
    for parts, config in studies:
        for selection in select_study(parts, config):
            reason = selection.reason
            assert list(reason) == ["v", "outcome", "codes", "params"]
            assert reason["v"] == 1
            registered: set[str] = set()
            for code in reason["codes"]:
                assert code.startswith(f"select.{reason['outcome']}."), code
                registered |= set(REGISTRY[code]["params"])
                emitted.add(code)
            assert set(reason["params"]) == registered, reason
    # Every select code but the merge's own (held) comes from the worker.
    assert emitted == {code for code in REGISTRY if not code.startswith("select.held.")}
