from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from bcoa_worker.protocol import (
    Done,
    Heartbeat,
    Progress,
    ProtocolError,
    event_to_line,
    parse_event,
    parse_job,
)
from conftest import PROTOCOL_ROOT

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
        Done("j_1", "cancelled"),
    ):
        jsonschema.validate(json.loads(event_to_line(event)), schema)


def test_null_fraction_is_sent_not_omitted() -> None:
    line = event_to_line(Progress("j_1", "segment", None, "x"))
    assert '"fraction":null' in line
    assert "model" not in line


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
