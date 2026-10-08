"""The index job's part of the protocol (ADR 0020, ADR 0024): its payload and
result schemas, their fixtures, and the shared link vectors that the app's
LinkHasher reproduces in swift test."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from bcoa_worker.protocol import parse_event, parse_job
from conftest import PROTOCOL_ROOT

FIXTURES = PROTOCOL_ROOT / "fixtures"
INDEX_JOBS = sorted((FIXTURES / "jobs").glob("index_*.json"))
INDEX_RESULTS = sorted((FIXTURES / "events").glob("result_index*.json"))
SEPARATOR = "\x1f"


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


def _schema(name: str) -> dict[str, Any]:
    return _load(PROTOCOL_ROOT / "schemas" / name)


def _valid(instance: Any, schema: str) -> bool:
    return jsonschema.Draft202012Validator(_schema(schema)).is_valid(instance)


@pytest.mark.parametrize("path", INDEX_JOBS, ids=lambda p: p.stem)
def test_index_job_fixture_matches_both_schemas(path: Path) -> None:
    raw = _load(path)
    jsonschema.validate(raw, _schema("job.schema.json"))
    jsonschema.Draft202012Validator(_schema("index_payload.schema.json")).validate(raw["payload"])
    job = parse_job(raw)
    assert job.kind == "index"
    assert raw["payload"]["mode"] == path.stem.removeprefix("index_")


def test_every_mode_has_a_job_fixture() -> None:
    modes = set(_schema("index_payload.schema.json")["properties"]["mode"]["enum"])
    assert {p.stem.removeprefix("index_") for p in INDEX_JOBS} == modes


@pytest.mark.parametrize("path", INDEX_JOBS, ids=lambda p: p.stem)
def test_a_fixture_link_key_matches_its_id(path: Path) -> None:
    payload = _load(path)["payload"]
    key = payload.get("link_key")
    if key is None:
        assert payload.get("link_key_id") is None
        return
    assert payload["link_key_id"] == _key_id(_key_bytes(key))


def _scan() -> dict[str, Any]:
    return copy.deepcopy(_load(FIXTURES / "jobs" / "index_scan.json")["payload"])


def _previews() -> dict[str, Any]:
    return copy.deepcopy(_load(FIXTURES / "jobs" / "index_previews.json")["payload"])


def _set(payload: dict[str, Any], **changes: Any) -> dict[str, Any]:
    payload.update(changes)
    return payload


def _without(payload: dict[str, Any], key: str) -> dict[str, Any]:
    del payload[key]
    return payload


def _source(payload: dict[str, Any], index: int, **changes: Any) -> dict[str, Any]:
    payload["sources"][index].update(changes)
    return payload


BAD_PAYLOADS: dict[str, Callable[[], dict[str, Any]]] = {
    "unknown mode": lambda: _set(_scan(), mode="rescan"),
    "absolute catalog path": lambda: _set(_scan(), catalog="/tmp/catalog.sqlite"),
    "scan of no source": lambda: _set(_scan(), scan_sources=[]),
    "regroup that scans": lambda: _set(_scan(), mode="regroup"),
    "scan without selection": lambda: _without(_scan(), "selection"),
    "scan without a link key decision": lambda: _without(_scan(), "link_key"),
    "key id without a key": lambda: _set(_scan(), link_key=None),
    "key without its id": lambda: _set(_scan(), link_key_id=None),
    "key that is not base64 of 32 bytes": lambda: _set(_scan(), link_key="base64:AAEC"),
    "key id in capitals": lambda: _set(_scan(), link_key_id="3683C115DE125163"),
    "active source without a root": lambda: _source(_scan(), 0, root=None),
    "active source with a relative root": lambda: _source(_scan(), 0, root="Share/Cohort A"),
    "removed source that keeps its root": lambda: _source(_scan(), 2, root="/Volumes/Old"),
    "unknown volume kind": lambda: _source(_scan(), 0, volume_kind="cloud"),
    "previews of nothing": lambda: _set(_previews(), parts=[]),
    "previews without a size": lambda: _without(_previews(), "preview"),
    "more than 64 previews": lambda: _set(
        _previews(), parts=[{"catalog_id": "0" * 32, "part_ref": n} for n in range(1, 66)]
    ),
    "catalog id that is not uuid4 hex": lambda: _set(
        _previews(), parts=[{"catalog_id": "3F2A" * 8, "part_ref": 1}]
    ),
    # A previews job reads no identifier; its job file has no use for the key.
    "previews that carry the link key": lambda: _set(
        _previews(), link_key=_scan()["link_key"], link_key_id=_scan()["link_key_id"]
    ),
    "previews that carry the key id": lambda: _set(_previews(), link_key_id="3683c115de125163"),
}


@pytest.mark.parametrize("make", BAD_PAYLOADS.values(), ids=BAD_PAYLOADS.keys())
def test_malformed_payloads_are_refused(make: Callable[[], dict[str, Any]]) -> None:
    assert not _valid(make(), "index_payload.schema.json")


def test_a_payload_after_remove_identifiers_has_null_key_and_id() -> None:
    assert _valid(_set(_scan(), link_key=None, link_key_id=None), "index_payload.schema.json")


def _branch(payload: dict[str, Any]) -> str:
    schema = _schema("index_result.schema.json")
    matches = [
        name
        for name in ("changed", "unchanged", "previews")
        if jsonschema.Draft202012Validator(
            {"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}
        ).is_valid(payload)
    ]
    assert len(matches) == 1, matches
    return matches[0]


@pytest.mark.parametrize("path", INDEX_RESULTS, ids=lambda p: p.stem)
def test_index_result_fixture_matches_both_schemas(path: Path) -> None:
    raw = _load(path)
    jsonschema.validate(raw, _schema("event.schema.json"))
    jsonschema.Draft202012Validator(_schema("index_result.schema.json")).validate(raw["payload"])
    assert parse_event(raw).payload == raw["payload"]  # type: ignore[union-attr]


def test_every_result_shape_has_a_fixture() -> None:
    assert {_branch(_load(p)["payload"]) for p in INDEX_RESULTS} == {
        "changed",
        "unchanged",
        "previews",
    }


def test_the_unchanged_fixture_reports_a_dropped_share() -> None:
    # The case the per-source states of an unchanged result exist for: an
    # empty walk keeps the source's rows, so nothing changed and no merge runs.
    sources = _load(FIXTURES / "events" / "result_index_unchanged.json")["payload"]["sources"]
    assert {"state": "unreachable", "code": "source.empty_walk"}.items() <= sources["4"].items()


def _result(name: str) -> dict[str, Any]:
    return copy.deepcopy(_load(FIXTURES / "events" / f"{name}.json")["payload"])


def _part(payload: dict[str, Any], **changes: Any) -> dict[str, Any]:
    payload["parts"][0] = {"part_ref": 12} | changes
    return payload


def _files_without_seen() -> dict[str, Any]:
    payload = _result("result_index")
    del payload["files"]["seen"]
    return payload


def _files_with(key: str, count: int) -> dict[str, Any]:
    payload = _result("result_index")
    payload["files"][key] = count
    return payload


BAD_RESULTS: dict[str, Callable[[], dict[str, Any]]] = {
    "changed without generation": lambda: _without(_result("result_index"), "generation"),
    "generation 0": lambda: _set(_result("result_index"), generation=0),
    "source keyed by name": lambda: _set(
        _result("result_index"),
        sources={"Cohort A": {"state": "complete", "files": 1, "bad_dirs": 0}},
    ),
    "source still walking": lambda: _set(
        _result("result_index"),
        sources={"1": {"state": "walking", "files": 1, "bad_dirs": 0}},
    ),
    "files without seen": _files_without_seen,
    "negative count": lambda: _set(_result("result_index"), studies=-1),
    "unchanged that is not false": lambda: _set(_result("result_index_unchanged"), changed=None),
    "missing preview without a code": lambda: _part(
        _result("result_index_previews"), written=False
    ),
    "written preview with a code": lambda: _part(
        _result("result_index_previews"), written=True, code="preview.failed.decode"
    ),
    "unregistered preview code": lambda: _part(
        _result("result_index_previews"), written=False, code="preview.skipped.too_big"
    ),
    "previews that claim a change": lambda: _set(_result("result_index_previews"), changed=True),
    # No merge follows an unchanged scan, so the result is the only way a
    # source's state (a dropped share above all) reaches the app.
    "unchanged scan without sources": lambda: _without(
        _result("result_index_unchanged"), "sources"
    ),
    "unchanged scan with no source": lambda: _set(_result("result_index_unchanged"), sources={}),
    # Every object is closed: an extra key is where a path or an ID would travel.
    "changed with a root path": lambda: _set(
        _result("result_index"), root="/Volumes/Share/CANARY_FOLDER"
    ),
    "changed with a patient id": lambda: _set(_result("result_index"), patient_id="CANARY-ID-4711"),
    "unchanged with a root path": lambda: _set(
        _result("result_index_unchanged"), root="/Users/x/CANARY_FOLDER"
    ),
    "source with a folder label": lambda: _set(
        _result("result_index"),
        sources={"1": {"state": "complete", "files": 1, "bad_dirs": 0, "label": "CANARY_FOLDER"}},
    ),
    "files counted under a path": lambda: _files_with("/Volumes/Share/CANARY_FOLDER", 1),
    "preview with a study uid": lambda: _part(
        _result("result_index_previews"), written=True, study_uid="1.2.3"
    ),
    "previews with a root path": lambda: _set(
        _result("result_index_previews"), root="/Volumes/Share/CANARY_FOLDER"
    ),
}


@pytest.mark.parametrize("make", BAD_RESULTS.values(), ids=BAD_RESULTS.keys())
def test_malformed_results_are_refused(make: Callable[[], dict[str, Any]]) -> None:
    assert not _valid(make(), "index_result.schema.json")


# ---------------------------------------------------------------- link vectors

VECTORS = _load(FIXTURES / "link_vectors.json")


def _key_bytes(link_key: str) -> bytes:
    assert link_key.startswith("base64:")
    key = base64.b64decode(link_key.removeprefix("base64:"), validate=True)
    assert len(key) == 32
    return key


def _key_id(key: bytes) -> str:
    return hmac.new(key, b"bcoa.link-key-id", hashlib.sha256).hexdigest()[:16]


def _hmac(key: bytes, *parts: str) -> str:
    return hmac.new(key, SEPARATOR.join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _normalized(patient_id: str) -> str:
    return unicodedata.normalize("NFC", patient_id.strip(" \x00"))


@pytest.mark.parametrize("entry", VECTORS["keys"], ids=lambda e: e["link_key_id"])
def test_link_key_id(entry: dict[str, Any]) -> None:
    assert _key_id(_key_bytes(entry["link_key"])) == entry["link_key_id"]


@pytest.mark.parametrize("entry", VECTORS["keys"], ids=lambda e: e["link_key_id"])
def test_pid_links(entry: dict[str, Any]) -> None:
    key = _key_bytes(entry["link_key"])
    placeholders = {p.casefold() for p in VECTORS["placeholder_ids"]}
    for case in entry["pid"]:
        normalized = _normalized(case["patient_id"])
        assert normalized == case["normalized"], case
        if not normalized:
            state, link = "missing", None
        elif normalized.casefold() in placeholders:
            state, link = "placeholder", None
        else:
            state, link = "present", "pid:" + _hmac(key, "pid", normalized)
        assert (state, link) == (case["state"], case["link"]), case


@pytest.mark.parametrize("entry", VECTORS["keys"], ids=lambda e: e["link_key_id"])
def test_folder_links(entry: dict[str, Any]) -> None:
    key = _key_bytes(entry["link_key"])
    for case in entry["folder"]:
        components = [unicodedata.normalize("NFC", c) for c in case["components"]]
        expected = "folder:" + _hmac(
            key, "folder", str(case["source_id"]), *components[: case["level"]]
        )
        assert expected == case["link"], case


def test_the_vectors_cover_what_the_rules_name() -> None:
    first, second = VECTORS["keys"]
    states = {c["state"] for c in first["pid"]}
    assert states == {"present", "missing", "placeholder"}
    # Padding and NUL are stripped; NFD and NFC give the same link; case is
    # kept, because two IDs that differ only in case are two IDs.
    links = {c["patient_id"]: c["link"] for c in first["pid"]}
    assert links["00012345"] == links["00012345 "] == links["\x0000012345\x00"]
    assert links["Ä-0042"] == links["Ä-0042"]
    assert links["PAT-0001"] != links["pat-0001"]
    # Each folder level, each source and each key gives another link.
    folders = [c["link"] for c in first["folder"] if c["source_id"] == 1][:4]
    assert len(set(folders)) == 4
    assert {c["level"] for c in first["folder"]} == {0, 1, 2, 3}
    assert len({c["source_id"] for c in first["folder"]}) == 2
    assert first["pid"][0]["link"] != second["pid"][0]["link"]
    assert VECTORS["placeholder_ids"] == _scan()["identity"]["placeholder_ids"]
