from __future__ import annotations

import json
import re
import typing
from pathlib import Path

import jsonschema
import pytest

from bcoa_worker import protocol
from bcoa_worker.protocol import (
    Done,
    Heartbeat,
    Progress,
    ProgressDetail,
    ProtocolError,
    event_to_line,
    parse_event,
    parse_job,
)
from conftest import PROTOCOL_ROOT, WORKER_ROOT

EVENT_FIXTURES = sorted((PROTOCOL_ROOT / "fixtures" / "events").glob("*.json"))
JOB_FIXTURES = sorted((PROTOCOL_ROOT / "fixtures" / "jobs").glob("*.json"))


def _schema(name: str) -> dict:
    return json.loads((PROTOCOL_ROOT / "schemas" / name).read_text())


def test_there_is_a_fixture_for_every_event_type() -> None:
    types = {json.loads(p.read_text())["type"] for p in EVENT_FIXTURES}
    assert types == {"hello", "progress", "heartbeat", "log", "artifact", "result", "error", "done"}


@pytest.mark.parametrize("path", EVENT_FIXTURES, ids=lambda p: p.stem)
def test_event_fixture_matches_schema_and_parses(path: Path) -> None:
    raw = json.loads(path.read_text())
    jsonschema.validate(raw, _schema("event.schema.json"))
    event = parse_event(raw)
    # What the worker would send for this event is the fixture again.
    assert json.loads(event_to_line(event)) == raw


@pytest.mark.parametrize("path", JOB_FIXTURES, ids=lambda p: p.stem)
def test_job_fixture_matches_schema_and_parses(path: Path) -> None:
    raw = json.loads(path.read_text())
    jsonschema.validate(raw, _schema("job.schema.json"))
    job = parse_job(raw)
    assert job.job_id == raw["job_id"]


def test_events_the_worker_sends_match_the_schema() -> None:
    schema = _schema("event.schema.json")
    for event in (
        Heartbeat.now("j_1"),
        Progress("j_1", "segment", None, "Model 1 of 1", model="clin_ct_organs"),
        Progress("j_1", "index", 0.5, "Half"),
        Progress("j_1", "index", None, "Found 3 files", detail=ProgressDetail("walk", 3, None)),
        Progress("j_1", "index", 0.5, "Read 1 of 2", detail=ProgressDetail("read", 1, 2)),
        Done("j_1", "cancelled"),
    ):
        jsonschema.validate(json.loads(event_to_line(event)), schema)


def test_null_fraction_is_sent_not_omitted() -> None:
    line = event_to_line(Progress("j_1", "segment", None, "x"))
    assert '"fraction":null' in line
    assert "model" not in line
    assert "detail" not in line


def test_detail_with_an_unknown_total_sends_null() -> None:
    # The schema requires `total`: an unknown total is sent as null, never left out.
    line = event_to_line(
        Progress("j_1", "index", None, "x", detail=ProgressDetail("walk", 7, None))
    )
    assert '"detail":{"phase":"walk","done":7,"total":null}' in line
    assert parse_event(line) == Progress(
        "j_1", "index", None, "x", detail=ProgressDetail("walk", 7, None)
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "nonsense"},
        {"type": "progress", "job_id": "j", "stage": "dance", "fraction": None, "message": ""},
        {"type": "progress", "job_id": "j", "stage": "index", "fraction": 1.5, "message": ""},
        {"type": "done", "job_id": "j"},
    ],
)
def test_malformed_events_are_refused(raw: dict) -> None:
    with pytest.raises(ProtocolError):
        parse_event(raw)


@pytest.mark.parametrize(
    "detail",
    [
        {"phase": "dance", "done": 1, "total": 2},
        {"phase": "read", "done": -1, "total": 2},
        {"phase": "read", "done": True, "total": 2},
        {"phase": "read", "done": 1.5, "total": 2},
        {"phase": "read", "done": 1, "total": "2"},
        {"phase": "read", "done": 1},
        # Closed in the schema as in parse_event: an extra key is where a
        # path would travel.
        {"phase": "read", "done": 1, "total": 2, "path": "/Volumes/Share/CANARY_FOLDER"},
        "read 1 of 2",
    ],
)
def test_malformed_progress_details_are_refused(detail: object) -> None:
    raw = {
        "type": "progress",
        "job_id": "j",
        "stage": "index",
        "fraction": None,
        "message": "",
        "detail": detail,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(raw, _schema("event.schema.json"))
    with pytest.raises(ProtocolError):
        parse_event(raw)


def _schema_enum(schema: str, definition: str | None, field: str) -> set[str]:
    raw = _schema(schema)
    node = raw["$defs"][definition] if definition else raw
    return set(node["properties"][field]["enum"])


def _swift_cases(name: str) -> set[str]:
    """The raw values of a `String` enum in WorkerProtocol.swift."""
    text = (
        WORKER_ROOT.parent / "Packages/BCOAKit/Sources/BCOAKit/WorkerProtocol.swift"
    ).read_text()
    match = re.search(
        rf"^( *)public enum {name}: String\b[^{{]*\{{\n(.*?)^\1\}}", text, re.S | re.M
    )
    assert match, f"enum {name} not found"
    values: set[str] = set()
    for line in match.group(2).splitlines():
        if line.strip().startswith("case "):
            for item in line.strip()[len("case ") :].split(","):
                name_part, _, raw_value = item.partition("=")
                values.add(raw_value.strip().strip('"') or name_part.strip())
    return values


@pytest.mark.parametrize(
    ("literal", "schema", "definition", "field", "swift"),
    [
        (protocol.Stage, "event.schema.json", "progress", "stage", "WorkerStage"),
        (protocol.ProgressPhase, "event.schema.json", "progress_detail", "phase", "Phase"),
        (protocol.LogLevel, "event.schema.json", "log", "level", "LogLevel"),
        (protocol.ArtifactKind, "event.schema.json", "artifact", "kind", "ArtifactKind"),
        (protocol.DoneStatus, "event.schema.json", "done", "status", "DoneStatus"),
        (protocol.JobKind, "job.schema.json", None, "kind", "JobKind"),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_enums_agree_across_python_schema_and_swift(
    literal: object, schema: str, definition: str | None, field: str, swift: str
) -> None:
    # A value added on one side only decodes there and fails on the other,
    # at run time, in the middle of a job.
    python = set(typing.get_args(literal))
    assert python == _schema_enum(schema, definition, field)
    assert python == _swift_cases(swift)


def _job(**overrides: object) -> dict:
    raw = json.loads((PROTOCOL_ROOT / "fixtures" / "jobs" / "segment.json").read_text())
    raw.update(overrides)
    return raw


def test_job_with_other_protocol_version_is_refused() -> None:
    with pytest.raises(ProtocolError):
        parse_job(_job(protocol_version=2))


def test_project_paths_cannot_leave_the_project(tmp_path: Path) -> None:
    job = parse_job(_job(project_dir=str(tmp_path)))
    assert job.project_path("work/s_1/ct.nii.gz") == (tmp_path / "work/s_1/ct.nii.gz").resolve()
    with pytest.raises(ProtocolError):
        job.project_path("../outside.txt")
    with pytest.raises(ProtocolError):
        job.project_path("/etc/passwd")
