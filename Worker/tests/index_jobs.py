"""Index jobs for the tests: a job for given source folders, run in this
process or by a worker of its own, and the catalog it leaves, read back.

A job made here is the fixture `Protocol/fixtures/jobs/index_scan.json` with
the test's folders filled in, so the payload stays the one the app sends.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from bcoa_worker import worker
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.protocol import parse_job
from conftest import PROTOCOL_ROOT, WORKER_ROOT

# The key of the fixture jobs and of corpus_expected.json, with its id.
LINK_KEY = "base64:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
LINK_KEY_ID = "3683c115de125163"
JOB_ID = "j_index_test"

_SCHEMAS = PROTOCOL_ROOT / "schemas"


def index_job(
    project_dir: Path,
    sources: Mapping[int, Path],
    *,
    mode: str = "scan",
    scan: Iterable[int] | None = None,
    link_key: str | None = LINK_KEY,
    force_reread: bool = False,
    removed: Iterable[int] = (),
) -> dict[str, Any]:
    """The fixture scan job for `sources`, scanning all of them unless
    `scan` names some, with the key of the fixtures unless `link_key` is
    None."""
    raw = json.loads((PROTOCOL_ROOT / "fixtures" / "jobs" / "index_scan.json").read_text())
    raw["job_id"] = JOB_ID
    raw["project_dir"] = str(project_dir)
    raw["log_path"] = str(project_dir / "logs" / f"{JOB_ID}.log")
    raw["resources_dir"] = str(project_dir)
    payload = raw["payload"]
    payload["mode"] = mode
    payload["sources"] = [
        {"source_id": source_id, "root": str(root), "status": "active", "volume_kind": "local"}
        for source_id, root in sorted(sources.items())
    ] + [{"source_id": source_id, "root": None, "status": "removed"} for source_id in removed]
    payload["scan_sources"] = (
        [] if mode == "regroup" else sorted(scan if scan is not None else sources)
    )
    payload["force_reread"] = force_reread
    payload["link_key"] = link_key
    payload["link_key_id"] = LINK_KEY_ID if link_key is not None else None
    payload["merged_generation"] = 0
    return raw


def run_index(job: Mapping[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    """The job in this process, through the worker's own run_job: its exit
    code and every event it sent."""
    buffer = io.StringIO()
    code = worker.run_job(parse_job(dict(job)), ProtocolChannel(buffer), worker._handlers())
    return code, [json.loads(line) for line in buffer.getvalue().splitlines()]


def result_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    results = [event["payload"] for event in events if event["type"] == "result"]
    assert len(results) == 1, [event["type"] for event in events]
    return results[0]


def check_events(events: list[dict[str, Any]]) -> None:
    """Every event against the event schema, and the result against the
    index result schema."""
    event_schema = json.loads((_SCHEMAS / "event.schema.json").read_text())
    result_schema = json.loads((_SCHEMAS / "index_result.schema.json").read_text())
    for event in events:
        jsonschema.validate(event, event_schema)
        if event["type"] == "result":
            jsonschema.validate(event["payload"], result_schema)


def write_job(job: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job), encoding="utf-8")
    return path


def start_worker(job_path: Path, *, prelude: str = "", epilogue: str = "") -> subprocess.Popen:
    """A worker of its own, started as the app starts it but through a short
    script, so that a test can change a constant or wrap a function first
    (`prelude`) or record something after the job (`epilogue`). Its stdout
    is the protocol, one event per line."""
    script = "\n".join(
        [
            "import sys",
            prelude,
            "from bcoa_worker import worker",
            f"code = worker.main(['run', '--job', {str(job_path)!r}])",
            epilogue,
            "sys.exit(code)",
        ]
    )
    env = {**os.environ, "PYTHONPATH": str(WORKER_ROOT)}
    return subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env
    )


def finish(process: subprocess.Popen, timeout: float = 60) -> tuple[int, list[dict[str, Any]]]:
    out, _ = process.communicate(timeout=timeout)
    return process.returncode, [json.loads(line) for line in out.splitlines() if line]


def killed(code: int) -> bool:
    return code == -signal.SIGKILL


def catalog_path(project_dir: Path) -> Path:
    return project_dir / "index" / "catalog.sqlite"


def connect(project_dir: Path) -> sqlite3.Connection:
    """The catalog, read-only: a test must never change what it checks."""
    uri = catalog_path(project_dir).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def meta(project_dir: Path) -> dict[str, str]:
    with closing(connect(project_dir)) as db:
        return dict(db.execute("SELECT key, value FROM catalog_meta"))


def files_by_path(project_dir: Path) -> dict[tuple[int, bytes], dict[str, Any]]:
    """Every file row by (source, relative path), without its file_id, with
    its frames and DICOMDIR entries: what two scans of the same tree must
    agree on, whatever order they wrote the rows in."""
    with closing(connect(project_dir)) as db:
        db.row_factory = sqlite3.Row
        rows: dict[tuple[int, bytes], dict[str, Any]] = {}
        ids: dict[int, tuple[int, bytes]] = {}
        for row in db.execute("SELECT * FROM files"):
            values = dict(row)
            key = (values["source_id"], bytes(values["rel_path"]))
            ids[values.pop("file_id")] = key
            rows[key] = values | {"frames": [], "dicomdir": []}
        for row in db.execute("SELECT * FROM frames ORDER BY file_id, frame"):
            values = dict(row)
            rows[ids[values.pop("file_id")]]["frames"].append(values)
        for row in db.execute("SELECT * FROM dicomdir_entries ORDER BY file_id, sop_uid"):
            values = dict(row)
            rows[ids[values.pop("file_id")]]["dicomdir"].append(values)
    return rows


# The tables a regroup derives, each with its columns that name a file.
_DERIVED = {
    "cat_studies": (),
    "cat_series": ("middle_file_id",),
    "cat_checks": (),
    "cat_pairs": (),
    "cat_id_candidates": (),
    "cat_instances": ("file_id",),
    "pending_identifiers": (),
}


def derived(project_dir: Path, *, by_path: bool = True) -> dict[str, Any]:
    """Every row of the derived tables, a file named by (source, relative
    path) instead of its file_id, and the meta values of the generation they
    belong to: what two catalogs of the same tree must agree on, whatever
    file_ids their scans happened to give out. `by_path=False` keeps the
    file_ids, for one catalog before and after a change to `files`."""
    with closing(connect(project_dir)) as db:
        paths = {
            file_id: (source_id, bytes(rel_path))
            for file_id, source_id, rel_path in db.execute(
                "SELECT file_id, source_id, rel_path FROM files"
            )
        }
        tables: dict[str, Any] = {}
        for table, file_columns in _DERIVED.items():
            cursor = db.execute(f"SELECT * FROM {table}")  # noqa: S608 - names above
            names = [column[0] for column in cursor.description]
            rows = []
            for row in cursor:
                values = dict(zip(names, row, strict=True))
                for column in file_columns if by_path else ():
                    values[column] = paths.get(values[column], ("gone", values[column]))
                rows.append(tuple(sorted(values.items(), key=lambda item: item[0])))
            tables[table] = sorted(rows, key=repr)
        values = dict(db.execute("SELECT key, value FROM catalog_meta"))
    tables["meta"] = {
        key: values.get(key)
        for key in ("generation", "complete", "next_part_ref", "link_key_id", "regroup_due")
    }
    return tables


def kinds(project_dir: Path) -> dict[int, dict[str, int]]:
    with closing(connect(project_dir)) as db:
        counts: dict[int, dict[str, int]] = {}
        for source_id, kind, count in db.execute(
            "SELECT source_id, kind, count(*) FROM files GROUP BY source_id, kind"
        ):
            counts.setdefault(source_id, {})[kind] = count
    return counts


# ---------------------------------------------------------------- shared scans

# Recorded by an audit hook in the worker's own process: any socket event and
# any start of a program, whether the guard would refuse it or not.
AUDIT_PRELUDE = """
import json
_audited = []
_PROGRAMS = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
    "os.forkpty",
}
def _audit(event, args):
    if event.startswith("socket.") or event in _PROGRAMS:
        _audited.append(event)
sys.addaudithook(_audit)
"""


@dataclass(frozen=True)
class Scan:
    """One index job over a tree, and what it left."""

    project_dir: Path
    code: int
    events: list[dict[str, Any]]
    seconds: float
    # Only for a worker of its own: its log file, the audit hook's records and
    # the modules it had imported when the job ended.
    log: str = ""
    audit: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()

    @property
    def result(self) -> dict[str, Any]:
        return result_of(self.events)


def scan_in_process(folder: Path, sources: Mapping[int, Path], **options: Any) -> Scan:
    project_dir = folder / "Study.bcoaproj"
    project_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    code, events = run_index(index_job(project_dir, sources, **options))
    return Scan(project_dir, code, events, time.perf_counter() - started)


def scan_in_worker(folder: Path, sources: Mapping[int, Path], **options: Any) -> Scan:
    """The job run by `python -m bcoa_worker`'s own main, with the guard of
    ADR 0016 installed, as the app runs it."""
    project_dir = folder / "Study.bcoaproj"
    project_dir.mkdir(parents=True, exist_ok=True)
    job = index_job(project_dir, sources, **options)
    record = folder / "record.json"
    epilogue = (
        f"with open({str(record)!r}, 'w') as _out:\n"
        "    json.dump({'audit': _audited, 'modules': sorted(sys.modules)}, _out)"
    )
    started = time.perf_counter()
    process = start_worker(
        write_job(job, folder / "job.json"), prelude=AUDIT_PRELUDE, epilogue=epilogue
    )
    code, events = finish(process, timeout=120)
    seconds = time.perf_counter() - started
    recorded = json.loads(record.read_text())
    return Scan(
        project_dir,
        code,
        events,
        seconds,
        log=Path(job["log_path"]).read_text(encoding="utf-8", errors="replace"),
        audit=tuple(recorded["audit"]),
        modules=tuple(recorded["modules"]),
    )
