"""Cancel and resume (ADR 0020 decision 9, ADR 0026's "cancel answered in
≤ 2 s" and "resume reads only the rest")."""

from __future__ import annotations

import io
import json
import signal
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import dicom_factory
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.index import read
from bcoa_worker.index import run as index_run
from bcoa_worker.index.catalog import open_catalog
from bcoa_worker.protocol import parse_job
from bcoa_worker.worker import Cancelled
from index_jobs import (
    connect,
    derived,
    finish,
    index_job,
    meta,
    result_of,
    run_index,
    start_worker,
    write_job,
)

FILES = 400
BATCH = 25

# Small batches, every progress event sent, and 4 ms per file, so that the
# test can stop the worker mid-read with batches behind and ahead of it.
_SLOW_WORKER = f"""
import time
from bcoa_worker.index import read, run
run.BATCH_FILES = {BATCH}
run.PROGRESS_SECONDS = 0.0
_read_file = read.read_file
def _slow_read(*args, **kwargs):
    time.sleep(0.004)
    return _read_file(*args, **kwargs)
read.read_file = _slow_read
"""


def _scan_state(project: Path) -> tuple[str, dict[str, Any], int]:
    with closing(connect(project)) as db:
        state, summary = db.execute("SELECT state, summary_json FROM scans").fetchone()
        rows = db.execute("SELECT count(*) FROM files").fetchone()[0]
    return state, json.loads(summary), rows


def test_sigterm_mid_read_ends_the_job_cancelled_and_the_next_scan_reads_the_rest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    dicom_factory.write_bulk(root, FILES, per_series=100)
    project = tmp_path / "Study.bcoaproj"
    job = index_job(project, {1: root})
    process = start_worker(write_job(job, tmp_path / "job.json"), prelude=_SLOW_WORKER)
    events: list[dict[str, Any]] = []
    assert process.stdout is not None
    for line in process.stdout:
        event = json.loads(line)
        events.append(event)
        detail = event.get("detail") or {}
        if detail.get("phase") == "read" and detail["done"] >= 4 * BATCH + 7:
            break
    asked = time.monotonic()
    process.send_signal(signal.SIGTERM)
    code, rest = finish(process, timeout=30)
    answered = time.monotonic() - asked
    events += rest

    assert code == 130
    assert events[-1] == {"job_id": job["job_id"], "status": "cancelled", "type": "done"}
    assert not [e for e in events if e["type"] in ("result", "error")]
    assert answered <= 2.0, answered
    state, summary, rows = _scan_state(project)
    # Committed batches stay; the batch being written was rolled back.
    assert state == "interrupted"
    assert summary == {"files": rows, "bad_dirs": 0}
    assert 4 * BATCH <= rows < FILES and rows % BATCH == 0
    assert meta(project)["regroup_due"] == "1"

    code, events = run_index(index_job(project, {1: root}))
    result = result_of(events)
    assert code == 0
    assert result["files"]["seen"] == FILES
    assert result["files"]["read"] == FILES - rows
    assert result["changed"] is True
    assert _scan_state(project)[0] == "complete"
    assert _scan_state(project)[2] == FILES


def _index(project: Path, sources: dict[int, Path], cancel: Any) -> dict[str, Any]:
    job = parse_job(index_job(project, sources))
    payload = index_run.parse_payload(job)
    reporter = index_run._Reporter(job.job_id, ProtocolChannel(io.StringIO()))
    db = open_catalog(payload.catalog)
    try:
        return index_run.index(db, payload, reporter, cancel)
    finally:
        db.close()


def test_a_cancel_that_a_library_swallowed_still_stops_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The handler raises inside whatever runs; were that inside a library
    # that catches every exception, the flag still ends the scan at the next
    # file.
    root = tmp_path / "source"
    dicom_factory.write_bulk(root, 30, per_series=10)
    project = tmp_path / "Study.bcoaproj"
    cancel = index_run._Cancel()
    real = read.read_file
    count = {"n": 0}

    def swallowing(*args: Any, **kwargs: Any) -> Any:
        count["n"] += 1
        if count["n"] == 12:
            cancel.requested = True
        return real(*args, **kwargs)

    monkeypatch.setattr(index_run, "BATCH_FILES", 5)
    monkeypatch.setattr(read, "read_file", swallowing)
    with pytest.raises(Cancelled):
        _index(project, {1: root}, cancel)
    assert count["n"] == 12
    state, summary, rows = _scan_state(project)
    assert (state, rows, summary["files"]) == ("interrupted", 10, 10)


def test_a_cancel_during_the_walk_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    dicom_factory.write_bulk(root, 6, per_series=2)
    cancel = index_run._Cancel()
    real = index_run.walk.walk

    def walk(*args: Any, **kwargs: Any) -> Any:
        cancel.requested = True
        return real(*args, **kwargs)

    monkeypatch.setattr(index_run.walk, "walk", walk)
    project = tmp_path / "Study.bcoaproj"
    with pytest.raises(Cancelled):
        _index(project, {1: root}, cancel)
    assert _scan_state(project) == ("interrupted", {"files": 0, "bad_dirs": 0}, 0)


def test_a_cancel_during_the_regroup_keeps_the_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    dicom_factory.write_bulk(root, 4, per_series=2)
    project = tmp_path / "Study.bcoaproj"
    assert result_of(run_index(index_job(project, {1: root}))[1])["generation"] == 1
    published = derived(project, by_path=False)
    (root / "bulk" / "00000" / "IM0001.dcm").unlink()
    cancel = index_run._Cancel()
    real = index_run.group._meta

    def cancel_inside(db: Any, key: str) -> Any:
        cancel.requested = True
        return real(db, key)

    with monkeypatch.context() as patch:
        patch.setattr(index_run.group, "_meta", cancel_inside)
        with pytest.raises(Cancelled):
            _index(project, {1: root}, cancel)
    values = meta(project)
    assert (values["generation"], values["complete"], values["regroup_due"]) == ("1", "1", "1")
    # The first generation is still what a merge would read, row for row,
    # although the scan has already forgotten the deleted file.
    after = derived(project, by_path=False)
    assert {k: v for k, v in after.items() if k != "meta"} == {
        k: v for k, v in published.items() if k != "meta"
    }
    # The regroup the cancel cost runs at the next scan.
    result = result_of(run_index(index_job(project, {1: root}))[1])
    assert (result["changed"], result["generation"], result["files"]["read"]) == (True, 2, 0)
