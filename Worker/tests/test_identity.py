"""Patient links (ADR 0024) against Protocol/fixtures/link_vectors.json, the
file the app's LinkHasher is held to as well."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

import pytest

from bcoa_worker.index import identity
from bcoa_worker.index.identity import (
    FolderCandidate,
    IdentityConfig,
    LinkKeyMismatch,
    PatientLink,
    folder_candidates,
    folder_link,
    issuer_link,
    patient_link,
)
from conftest import PROTOCOL_ROOT

VECTORS = json.loads((PROTOCOL_ROOT / "fixtures" / "link_vectors.json").read_text())
KEYS = VECTORS["keys"]
FIRST = identity.decode_link_key(KEYS[0]["link_key"])


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((PROTOCOL_ROOT / "fixtures" / "jobs" / name).read_text())["payload"]


@pytest.mark.parametrize("entry", KEYS, ids=lambda e: e["link_key_id"])
def test_link_key_and_its_id(entry: dict[str, Any]) -> None:
    key = identity.decode_link_key(entry["link_key"])
    assert len(key) == 32
    assert identity.link_key_id(key) == entry["link_key_id"]
    assert identity.verify_link_key(entry["link_key"], entry["link_key_id"]) == key


@pytest.mark.parametrize("entry", KEYS, ids=lambda e: e["link_key_id"])
def test_pid_vectors(entry: dict[str, Any]) -> None:
    key = identity.decode_link_key(entry["link_key"])
    for case in entry["pid"]:
        link = patient_link(key, case["patient_id"], VECTORS["placeholder_ids"])
        assert link == PatientLink(case["normalized"], case["state"], case["link"]), case


@pytest.mark.parametrize("entry", KEYS, ids=lambda e: e["link_key_id"])
def test_folder_vectors(entry: dict[str, Any]) -> None:
    key = identity.decode_link_key(entry["link_key"])
    for case in entry["folder"]:
        link = folder_link(key, case["source_id"], case["components"], case["level"])
        assert link == case["link"], case


def test_a_mismatched_or_malformed_key_is_refused() -> None:
    first, second = KEYS
    with pytest.raises(LinkKeyMismatch):
        identity.verify_link_key(first["link_key"], second["link_key_id"])
    short = "base64:" + base64.b64encode(bytes(31)).decode()
    for bad in (first["link_key"].removeprefix("base64:"), "base64:not base64!", short):
        with pytest.raises(LinkKeyMismatch) as caught:
            identity.verify_link_key(bad, first["link_key_id"])
        # A refusal is logged as its code; it must not carry the key with it.
        assert first["link_key"].removeprefix("base64:") not in str(caught.value)


def test_the_default_placeholders_are_the_vectors_and_the_payloads() -> None:
    assert list(identity.DEFAULT_PLACEHOLDER_IDS) == VECTORS["placeholder_ids"]
    assert (
        list(identity.DEFAULT_PLACEHOLDER_IDS)
        == _fixture("index_scan.json")["identity"]["placeholder_ids"]
    )


@pytest.mark.parametrize("patient_id", ["Anonymous", "n/a", "None ", "\x00null\x00", "na", "-"])
def test_placeholders_are_compared_case_folded(patient_id: str) -> None:
    assert patient_link(FIRST, patient_id).state == "placeholder"
    assert patient_link(FIRST, patient_id).link is None


@pytest.mark.parametrize("patient_id", ["ANONYMOUS1", "00", "N-A", "0 0"])
def test_ids_that_only_resemble_a_placeholder_are_present(patient_id: str) -> None:
    assert patient_link(FIRST, patient_id).state == "present"


def test_the_placeholder_list_comes_from_the_settings() -> None:
    assert patient_link(FIRST, "ANONYMOUS", []).state == "present"
    assert patient_link(FIRST, "unbekannt", ["UNBEKANNT"]).state == "placeholder"


def test_only_spaces_and_nul_are_stripped() -> None:
    assert identity.normalize_id(" \x00 12345 \x00") == "12345"
    assert identity.normalize_id("\t12345") == "\t12345"
    assert identity.normalize_id(None) == ""
    assert patient_link(FIRST, None) == PatientLink("", "missing", None)


def test_issuer_links() -> None:
    assert issuer_link(FIRST, None) is None
    assert issuer_link(FIRST, "  ") is None
    link = issuer_link(FIRST, " HOSPITAL_A ")
    assert link is not None and link.startswith("issuer:")
    assert link == issuer_link(FIRST, "HOSPITAL_A")
    assert link != issuer_link(FIRST, "HOSPITAL_B")
    # The same text as an ID and as an issuer gives two different links.
    assert link.removeprefix("issuer:") != identity.pid_link(FIRST, "HOSPITAL_A").removeprefix(
        "pid:"
    )
    expected = hmac.new(FIRST, b"issuer\x1fHOSPITAL_A", hashlib.sha256).hexdigest()
    assert link == "issuer:" + expected


def test_folder_candidates_up_to_the_maximum_level() -> None:
    components = ["Cohort A", "CASE_017", "CT", "2019"]
    vectors = {
        c["level"]: c["link"]
        for c in KEYS[0]["folder"]
        if c["source_id"] == 1 and c["components"] == components
    }
    candidates = folder_candidates(FIRST, 1, "Share", components)
    assert candidates == [
        FolderCandidate(0, "Share", vectors[0]),
        FolderCandidate(1, "Cohort A", vectors[1]),
        FolderCandidate(2, "CASE_017", vectors[2]),
        FolderCandidate(3, "CT", vectors[3]),
    ]
    assert [c.level for c in folder_candidates(FIRST, 1, "Share", components, max_level=1)] == [
        0,
        1,
    ]
    assert [c.level for c in folder_candidates(FIRST, 1, "Share", components, max_level=0)] == [0]
    # A study directly below the root has the source folder only.
    assert [c.level for c in folder_candidates(FIRST, 1, "Share", [])] == [0]
    assert [c.level for c in folder_candidates(FIRST, 1, "Share", ["CASE_018"])] == [0, 1]


def test_folder_labels_are_nfc_and_links_ignore_the_normal_form() -> None:
    nfd, nfc = (
        folder_candidates(FIRST, 1, "Share", ["Grün"]),
        folder_candidates(FIRST, 1, "Share", ["Grün"]),
    )
    assert nfd == nfc
    assert nfd[1].label == "Grün"


def test_a_folder_name_that_is_not_utf8_keeps_its_bytes_in_the_link() -> None:
    name = os.fsdecode(b"Gr\xfcn")
    candidate = folder_candidates(FIRST, 1, "Share", [name])[1]
    assert candidate.label == "Gr�n"
    expected = hmac.new(FIRST, b"folder\x1f1\x1fGr\xfcn", hashlib.sha256).hexdigest()
    assert candidate.link == "folder:" + expected


def test_a_folder_level_outside_the_path_is_refused() -> None:
    with pytest.raises(ValueError):
        folder_link(FIRST, 1, ["a"], 2)
    with pytest.raises(ValueError):
        folder_link(FIRST, 1, ["a"], -1)


def test_identity_settings_decode_leniently() -> None:
    payload = _fixture("index_scan.json")["identity"]
    assert IdentityConfig.from_json(payload) == IdentityConfig()
    assert IdentityConfig().to_json() == payload
    assert IdentityConfig.from_json(None) == IdentityConfig()
    assert (
        IdentityConfig.from_json({"folder_ids": "yes", "folder_max_level": 4}) == IdentityConfig()
    )
    assert IdentityConfig.from_json({"folder_max_level": True}) == IdentityConfig()
    assert IdentityConfig.from_json({"folder_max_level": 0, "folder_ids": False}) == IdentityConfig(
        folder_ids=False, folder_max_level=0
    )
    assert IdentityConfig.from_json({"placeholder_ids": ["X", 1]}).placeholder_ids == ("X",)
