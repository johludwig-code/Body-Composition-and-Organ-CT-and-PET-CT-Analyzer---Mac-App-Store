"""Protocol version 1: the job file the app writes and the events the worker sends.

The JSON schemas in /Protocol/schemas are the contract; this module is the
Python side of it. Every fixture in /Protocol/fixtures must parse here and in
the Swift `WorkerEvent` decoder.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bcoa_worker import PROTOCOL_VERSION

Stage = Literal[
    "index", "convert", "check", "segment", "metrics", "viewer_cache", "export", "selftest"
]
LogLevel = Literal["debug", "info", "warning", "error"]
ArtifactKind = Literal["nifti", "labelmap", "viewer_cache", "metrics", "export", "log", "report"]
DoneStatus = Literal["ok", "failed", "cancelled"]
JobKind = Literal[
    "index", "convert", "segment", "metrics", "viewer_cache", "export", "selftest", "spike_s2"
]

STAGES: frozenset[str] = frozenset(Stage.__args__)  # type: ignore[attr-defined]
JOB_KINDS: frozenset[str] = frozenset(JobKind.__args__)  # type: ignore[attr-defined]


class ProtocolError(ValueError):
    """A job file or event line that does not follow protocol version 1."""


@dataclass(frozen=True)
class Hello:
    versions: dict[str, str]
    protocol_version: int = PROTOCOL_VERSION
    type: Literal["hello"] = "hello"


@dataclass(frozen=True)
class Progress:
    job_id: str
    stage: Stage
    fraction: float | None
    message: str
    model: str | None = None
    type: Literal["progress"] = "progress"


@dataclass(frozen=True)
class Heartbeat:
    job_id: str
    ts: str
    type: Literal["heartbeat"] = "heartbeat"

    @staticmethod
    def now(job_id: str) -> Heartbeat:
        stamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return Heartbeat(job_id=job_id, ts=stamp)


@dataclass(frozen=True)
class Log:
    level: LogLevel
    message: str
    type: Literal["log"] = "log"


@dataclass(frozen=True)
class Artifact:
    kind: ArtifactKind
    path: str
    sha256: str
    type: Literal["artifact"] = "artifact"


@dataclass(frozen=True)
class Result:
    job_id: str
    payload: dict[str, Any]
    type: Literal["result"] = "result"


@dataclass(frozen=True)
class Error:
    code: str
    message: str
    recoverable: bool
    type: Literal["error"] = "error"


@dataclass(frozen=True)
class Done:
    job_id: str
    status: DoneStatus
    type: Literal["done"] = "done"


Event = Hello | Progress | Heartbeat | Log | Artifact | Result | Error | Done

_EVENT_TYPES: dict[str, type] = {
    "hello": Hello,
    "progress": Progress,
    "heartbeat": Heartbeat,
    "log": Log,
    "artifact": Artifact,
    "result": Result,
    "error": Error,
    "done": Done,
}


def event_to_line(event: Event) -> str:
    """One event as one line. Optional fields that are unset are left out
    rather than sent as null, except `fraction`, whose null is meaningful."""
    raw = asdict(event)
    if isinstance(event, Progress) and event.model is None:
        raw.pop("model")
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def parse_event(line: str | dict[str, Any]) -> Event:
    raw = json.loads(line) if isinstance(line, str) else dict(line)
    kind = raw.get("type")
    cls = _EVENT_TYPES.get(kind)  # type: ignore[arg-type]
    if cls is None:
        raise ProtocolError(f"unknown event type: {kind!r}")
    try:
        event = cls(**raw)
    except TypeError as exc:
        raise ProtocolError(f"malformed {kind} event: {exc}") from exc
    if isinstance(event, Progress):
        if event.stage not in STAGES:
            raise ProtocolError(f"unknown stage: {event.stage!r}")
        if event.fraction is not None and not 0.0 <= event.fraction <= 1.0:
            raise ProtocolError(f"fraction out of range: {event.fraction}")
    return event


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: JobKind
    project_dir: Path
    log_path: Path
    resources_dir: Path
    payload: dict[str, Any] = field(default_factory=dict)
    heartbeat_seconds: float = 10.0
    protocol_version: int = PROTOCOL_VERSION

    def project_path(self, relative: str) -> Path:
        """Resolve a project-relative path from the payload, refusing anything
        that would leave the project folder. The worker writes nowhere else."""
        candidate = (self.project_dir / relative).resolve()
        root = self.project_dir.resolve()
        if candidate != root and root not in candidate.parents:
            raise ProtocolError(f"path leaves the project folder: {relative!r}")
        return candidate


def parse_job(raw: dict[str, Any]) -> Job:
    if raw.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(
            f"job speaks protocol {raw.get('protocol_version')!r}, worker speaks {PROTOCOL_VERSION}"
        )
    missing = [
        key
        for key in ("job_id", "kind", "project_dir", "log_path", "resources_dir", "payload")
        if key not in raw
    ]
    if missing:
        raise ProtocolError(f"job is missing {', '.join(missing)}")
    if raw["kind"] not in JOB_KINDS:
        raise ProtocolError(f"unknown job kind: {raw['kind']!r}")
    return Job(
        job_id=str(raw["job_id"]),
        kind=raw["kind"],
        project_dir=Path(raw["project_dir"]),
        log_path=Path(raw["log_path"]),
        resources_dir=Path(raw["resources_dir"]),
        payload=dict(raw["payload"]),
        heartbeat_seconds=float(raw.get("heartbeat_seconds", 10.0)),
    )


def load_job(path: Path) -> Job:
    return parse_job(json.loads(path.read_text(encoding="utf-8")))
