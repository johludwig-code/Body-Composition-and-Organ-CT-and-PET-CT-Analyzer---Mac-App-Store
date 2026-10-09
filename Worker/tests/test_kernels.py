"""Kernel classes (ADR 0023 decision 7): BOCARTA-MOOSE's KernelTableTests,
the suffix rule, Philips' exact codes, the order of the lists, and the three
places where the rule deliberately differs from BOCARTA-MOOSE."""

from __future__ import annotations

import json

import pytest

from bcoa_worker.index import kernels
from bcoa_worker.index.kernels import DEFAULT_KERNELS, KernelTable, classify, vendor
from conftest import PROTOCOL_ROOT


@pytest.mark.parametrize(
    ("kernel", "manufacturer", "expected"),
    [
        # testSiemensKernels
        ("Br40", "SIEMENS", "soft"),
        ("B30f", "SIEMENS", "soft"),
        ("Bl64", "SIEMENS", "sharp"),
        ("B70f", "SIEMENS", "sharp"),
        # testGEAndCanonKernels
        ("STANDARD", "GE MEDICAL SYSTEMS", "soft"),
        ("BONE", "GE MEDICAL SYSTEMS", "sharp"),
        ("FC18", "TOSHIBA", "soft"),
        ("FC51", "Canon Medical Systems", "sharp"),
        # testSharpBeatsSoftOnPrefixCollision
        ("b60s", "SIEMENS", "sharp"),
        # testSingleLetterCodesDoNotSwallowLongerOnes
        ("BONE", None, "sharp"),
        ("BONEPLUS", None, "sharp"),
        ("LUNG", None, "sharp"),
        ("DETAIL", None, "sharp"),
        ("B", "Philips", "soft"),
        ("YB", "Philips", "sharp"),
        # testUnrecognisedKernelIsUnclearNotSoft
        ("ZZ99", "ACME", "unknown"),
        # testBodyPartWordsAreNotKernels
        ("BODY", "GE MEDICAL SYSTEMS", "unknown"),
        ("ABDOMEN", "GE MEDICAL SYSTEMS", "unknown"),
    ],
)
def test_bocarta_moose_examples(kernel: str, manufacturer: str | None, expected: str) -> None:
    assert classify(kernel, manufacturer) == expected


@pytest.mark.parametrize("code", ["B30f", "B31s", "I30f", "Br40d"])
def test_siemens_suffix_letters_on_soft_codes(code: str) -> None:
    assert classify(code, "SIEMENS") == "soft"


@pytest.mark.parametrize("code", ["B70f", "Bl64d", "Br64f"])
def test_siemens_suffix_letters_on_sharp_codes(code: str) -> None:
    assert classify(code, "SIEMENS") == "sharp"


@pytest.mark.parametrize("letter", sorted(kernels.SUFFIX_LETTERS))
def test_each_suffix_letter_is_accepted(letter: str) -> None:
    assert classify(f"b30{letter}", "SIEMENS") == "soft"


@pytest.mark.parametrize("code", ["b30x", "b30ff", "b3", "b300", "fc130"])
def test_anything_else_after_a_code_is_unknown(code: str) -> None:
    assert classify(code, "SIEMENS") == "unknown"


def test_codes_shorter_than_three_characters_take_no_suffix() -> None:
    tables = {"siemens": KernelTable(soft=("ab",), sharp=())}
    assert classify("ab", "SIEMENS", tables) == "soft"
    assert classify("abf", "SIEMENS", tables) == "unknown"


def test_philips_codes_match_exactly() -> None:
    # BOCARTA-MOOSE's code let these take a Siemens suffix letter and called
    # them soft; Philips appends none.
    assert classify("SMOOTHD", "Philips") == "unknown"
    assert classify("SHARPF", "Philips") == "unknown"
    # A suffixed code still matches another vendor's inexact list, sharp first.
    assert classify("LUNGD", "Philips") == "sharp"


def test_a_missing_kernel_is_unknown_whatever_the_description() -> None:
    # BOCARTA-MOOSE guessed from words of the description ("weich",
    # "Lunge"); here a part without a kernel is unknown, which ranks between
    # soft and sharp. There is no description parameter to guess from.
    for kernel in (None, "", "   ", "\\B30f"):
        assert classify(kernel, "SIEMENS") == "unknown"


def test_the_first_value_is_the_filter() -> None:
    assert kernels.first_value("I30f\\3") == "I30f"
    assert kernels.first_value(" B30f ") == "B30f"
    assert kernels.first_value("") is None
    assert classify("I30f\\3", "SIEMENS") == "soft"
    assert classify("Br64d\\2", "SIEMENS") == "sharp"


@pytest.mark.parametrize(
    ("manufacturer", "expected"),
    [
        ("SIEMENS", "siemens"),
        ("Siemens Healthineers", "siemens"),
        ("GE MEDICAL SYSTEMS", "ge"),
        ("GE Healthcare", "ge"),
        ("GE_MEDICAL_SYSTEMS", "ge"),
        ("General Electric Company", "ge"),
        ("Philips Medical Systems", "philips"),
        ("Philips", "philips"),
        ("TOSHIBA", "canon"),
        ("Canon Medical Systems", "canon"),
        # Names that only begin with the letters G and E, which
        # BOCARTA-MOOSE took for GE.
        ("GEMS Imaging", None),
        ("Gemini Scanners", None),
        ("AGFA-GEVAERT", None),
        ("ACME", None),
        ("", None),
        (None, None),
    ],
)
def test_vendor_from_manufacturer(manufacturer: str | None, expected: str | None) -> None:
    assert vendor(manufacturer) == expected


# A code that two vendors classify differently, which the default lists do
# not contain, so the order of the lists is held with lists of its own.
CROSSED = {
    "siemens": KernelTable(soft=("x10",), sharp=()),
    "ge": KernelTable(soft=("y20",), sharp=()),
    "canon": KernelTable(soft=(), sharp=("x10", "y20")),
}


def test_a_vendors_own_table_decides_its_own_codes() -> None:
    assert classify("x10", "SIEMENS", CROSSED) == "soft"
    assert classify("x10", "Canon Medical Systems", CROSSED) == "sharp"


def test_for_any_other_code_the_sharp_lists_come_first() -> None:
    assert classify("x10", None, CROSSED) == "sharp"
    assert classify("x10", "ACME", CROSSED) == "sharp"
    # GE's own table decides only when the manufacturer is GE.
    assert classify("y20", "GE MEDICAL SYSTEMS", CROSSED) == "soft"
    assert classify("y20", "General Electric", CROSSED) == "soft"
    assert classify("y20", "GEMS Imaging", CROSSED) == "sharp"


def test_the_vendors_own_sharp_list_comes_before_its_soft_list() -> None:
    both = {"siemens": KernelTable(soft=("z30",), sharp=("z30",))}
    assert classify("z30", "SIEMENS", both) == "sharp"


def test_the_defaults_are_the_protocol_fixtures_lists() -> None:
    fixture = json.loads((PROTOCOL_ROOT / "fixtures" / "jobs" / "index_scan.json").read_text())
    assert kernels.tables_to_json(DEFAULT_KERNELS) == fixture["payload"]["selection"]["kernels"]
    assert kernels.tables_from_json(fixture["payload"]["selection"]["kernels"]) == DEFAULT_KERNELS


def test_the_tables_decode_leniently() -> None:
    assert kernels.tables_from_json(None) == DEFAULT_KERNELS
    assert kernels.tables_from_json({}) == DEFAULT_KERNELS
    assert kernels.tables_from_json({"siemens": "b30"}) == DEFAULT_KERNELS
    decoded = kernels.tables_from_json(
        {"philips": {"soft": ["b"]}, "ge": {"soft": [], "sharp": ["bone", 7]}}
    )
    # A key left out takes the vendor's default, Philips' exactness included;
    # a list that is there is taken as it is, empty or not.
    assert decoded["philips"] == KernelTable(
        soft=("b",), sharp=DEFAULT_KERNELS["philips"].sharp, exact=True
    )
    assert decoded["ge"] == KernelTable(soft=(), sharp=("bone",))
    assert decoded["siemens"] == DEFAULT_KERNELS["siemens"]


def test_a_vendor_the_defaults_do_not_know_takes_part_in_the_shared_lists() -> None:
    tables = kernels.tables_from_json({"united": {"sharp": ["ub80"]}})
    assert tables["united"] == KernelTable(soft=(), sharp=("ub80",))
    assert classify("ub80", "United Imaging", tables) == "sharp"
    assert classify("ub80", "United Imaging") == "unknown"


def test_philips_made_inexact_stays_inexact() -> None:
    tables = kernels.tables_from_json({"philips": {"exact": False}})
    assert not tables["philips"].exact
    assert kernels.tables_from_json(kernels.tables_to_json(tables)) == tables
    assert classify("SMOOTHD", "Philips", tables) == "soft"
