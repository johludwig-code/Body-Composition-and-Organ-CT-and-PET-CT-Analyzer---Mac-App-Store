"""The index job (ADR 0020 decisions 8 to 10, ADR 0021, ADR 0022 decision 1):
what a scan writes, what a rescan reads again, and how the job refuses."""

from __future__ import annotations

import errno
import fcntl
import itertools
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import stat
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import dicom_factory
from bcoa_worker.index import catalog as index_catalog
from bcoa_worker.index import read
from bcoa_worker.index import run as index_run
from bcoa_worker.worker import Cancelled
from corpus_check import load_expected
from index_jobs import (
    catalog_path,
    check_events,
    connect,
    files_by_path,
    index_job,
    kinds,
    meta,
    result_of,
    run_index,
)

EXPECTED = load_expected()


def _bulk(root: Path, files: int, per_series: int = 5) -> Path:
    dicom_factory.write_bulk(root, files, per_series=per_series)
    return root


def _scan(project: Path, sources: dict[int, Path], **options: Any) -> dict[str, Any]:
    code, events = run_index(index_job(project, sources, **options))
    check_events(events)
    assert code == 0, events[-2:]
    return result_of(events)


def _failure(project: Path, job: dict[str, Any]) -> dict[str, Any]:
    code, events = run_index(job)
    check_events(events)
    assert code == 1
    error, done = events[-2:]
    assert done["status"] == "failed"
    return error


# ------------------------------------------------------------------ the corpus


def _expected_kinds(features: frozenset[str]) -> dict[int, dict[str, int]]:
    catalog = EXPECTED["catalog"]
    wanted = {int(s): dict(counts) for s, counts in catalog["files_by_kind"].items()}
    for feature, deltas in catalog.get("files_by_kind_with", {}).items():
        if feature in features:
            for source_id, delta in deltas.items():
                for kind, count in delta.items():
                    counts = wanted.setdefault(int(source_id), {})
                    counts[kind] = counts.get(kind, 0) + count
    return wanted


def test_a_full_scan_records_every_file_of_the_corpus(corpus: Any, scanned_corpus: Any) -> None:
    assert scanned_corpus.code == 0
    check_events(scanned_corpus.events)
    result = scanned_corpus.result
    project = scanned_corpus.project_dir
    wanted = _expected_kinds(corpus.features)
    assert kinds(project) == wanted
    total = sum(sum(counts.values()) for counts in wanted.values())
    assert result["changed"] is True
    assert result["generation"] == 1
    assert result["catalog_id"] == meta(project)["catalog_id"]
    files = result["files"]
    assert (files["seen"], files["read"], files["unchanged"]) == (total, total, 0)
    for kind in ("image", "non_image", "dicomdir", "nifti", "not_dicom", "archive", "symlink"):
        assert files[kind] == sum(counts.get(kind, 0) for counts in wanted.values()), kind
    # The locked folder of the permissions feature cannot be listed: one bad
    # directory in source 1 wherever modes are enforced (not as root).
    locked_dirs = {1: 1} if dicom_factory.PERMISSIONS in corpus.features else {}
    assert result["sources"] == {
        str(source_id): {
            "state": "complete",
            "files": sum(counts.values()),
            "bad_dirs": locked_dirs.get(source_id, 0),
        }
        for source_id, counts in wanted.items()
    }
    with closing(connect(project)) as db:
        pixels = dict(
            db.execute(
                "SELECT pixel_data, count(*) FROM files WHERE source_id = 1 "
                "AND pixel_data IN ('truncated', 'missing') GROUP BY pixel_data"
            )
        )
        assert pixels == EXPECTED["catalog"]["pixel_data"]["1"]
        assert db.execute("SELECT count(*) FROM frames").fetchone()[0] == 50 + 5 + 5 + 52
        assert db.execute("SELECT count(*) FROM dicomdir_entries").fetchone()[0] == 10
        versions = {row[0] for row in db.execute("SELECT DISTINCT reader_version FROM files")}
        assert versions == {read.READER_VERSION}
        scans = {
            row[0]: (row[1], json.loads(row[2]), row[3] is not None)
            for row in db.execute("SELECT source_id, state, summary_json, finished_at FROM scans")
        }
    for source_id, entry in result["sources"].items():
        summary = {key: value for key, value in entry.items() if key != "state"}
        assert scans[int(source_id)] == ("complete", summary, True)


def test_the_scan_records_what_the_regroup_needs(scanned_corpus: Any) -> None:
    values = meta(scanned_corpus.project_dir)
    assert values["files_link_key_id"] == "3683c115de125163"
    assert values["link_key_id"] == "3683c115de125163"
    assert values["regroup_due"] == "0"
    assert values["reader_version"] == str(read.READER_VERSION)
    assert (values["generation"], values["complete"]) == ("1", "1")
    with closing(connect(scanned_corpus.project_dir)) as db:
        series = db.execute("SELECT count(*), max(part_ref) FROM cat_series").fetchone()
    # Every part has a part_ref of its own, and the next one is beyond them.
    assert series[0] == series[1] == int(values["next_part_ref"]) - 1


def test_progress_comes_as_numbers_in_phase_order(scanned_corpus: Any) -> None:
    progress = [e for e in scanned_corpus.events if e["type"] == "progress"]
    templates = re.compile(
        r"^(Listing files: \d+ found|Reading headers: \d+ of \d+|Grouping series)$"
    )
    for event in progress:
        detail = event["detail"]
        assert event["stage"] == "index"
        assert templates.match(event["message"]), event["message"]
        if detail["phase"] == "read" and detail["total"]:
            assert 0 <= detail["done"] <= detail["total"]
            assert event["fraction"] == pytest.approx(detail["done"] / detail["total"])
        else:
            assert event["fraction"] is None
    # A walk and a read per source, each announced at once, then one regroup.
    phases = [phase for phase, _ in itertools.groupby(e["detail"]["phase"] for e in progress)]
    assert phases == ["walk", "read", "walk", "read", "group"]


def test_an_unchanged_rescan_reads_nothing_and_writes_no_generation(
    scanned_corpus: Any, corpus: Any, tmp_path: Path
) -> None:
    project = tmp_path / "Study.bcoaproj"
    shutil.copytree(scanned_corpus.project_dir, project)
    before = files_by_path(project)
    result = _scan(project, corpus.sources)
    seen = scanned_corpus.result["files"]["seen"]
    # A file refused for want of permission is tried again at every scan
    # (ADR 0027 decision 2); trying it changes nothing while it stays locked.
    retried = 1 if dicom_factory.PERMISSIONS in corpus.features else 0
    assert result["changed"] is False
    assert (result["files"]["seen"], result["files"]["read"]) == (seen, retried)
    assert result["files"]["unchanged"] == seen - retried
    assert {s["state"] for s in result["sources"].values()} == {"complete"}
    assert meta(project)["generation"] == "1"
    assert files_by_path(project) == before


# ------------------------------------------------------------------ rescans


def test_a_rescan_reads_what_changed_and_forgets_what_is_gone(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 10)
    project = tmp_path / "Study.bcoaproj"
    first = _scan(project, {1: root})
    assert first["files"]["read"] == 10
    series = root / "bulk" / "00000"
    (series / "IM0001.dcm").unlink()
    shutil.copyfile(series / "IM0002.dcm", series / "IM0099.dcm")
    changed = series / "IM0003.dcm"
    changed.write_bytes(changed.read_bytes() + b"\x00\x00")
    result = _scan(project, {1: root})
    assert result["changed"] is True
    assert result["generation"] == 2
    files = result["files"]
    assert (files["seen"], files["read"], files["unchanged"]) == (10, 2, 8)
    rows = files_by_path(project)
    assert (1, b"bulk/00000/IM0001.dcm") not in rows
    assert (1, b"bulk/00000/IM0099.dcm") in rows
    assert rows[(1, b"bulk/00000/IM0003.dcm")]["size"] == changed.stat().st_size


def test_a_newer_reader_reads_every_row_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bulk(tmp_path / "source", 6)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    monkeypatch.setattr(read, "READER_VERSION", read.READER_VERSION + 1)
    result = _scan(project, {1: root})
    assert result["files"]["read"] == 6
    with closing(connect(project)) as db:
        assert {r[0] for r in db.execute("SELECT reader_version FROM files")} == {
            read.READER_VERSION
        }


def test_force_reread_reads_every_file(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 4)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    assert _scan(project, {1: root}, force_reread=True)["files"]["read"] == 4


def test_an_unreadable_file_is_tried_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _bulk(tmp_path / "source", 3)
    project = tmp_path / "Study.bcoaproj"
    real = read._open_file

    def locked(path: bytes) -> Any:
        if os.fsdecode(path).endswith("IM0002.dcm"):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(read, "_open_file", locked)
    first = _scan(project, {1: root})
    assert first["files"]["unreadable"] == 1
    monkeypatch.undo()
    # The size and time are unchanged, as after a chmod; the file is read
    # again because of its code.
    second = _scan(project, {1: root})
    assert second["files"]["read"] == 1
    assert (second["files"]["unreadable"], second["files"]["image"]) == (0, 3)


def test_a_file_that_stays_unreadable_writes_no_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bulk(tmp_path / "source", 3)
    project = tmp_path / "Study.bcoaproj"
    real = read._open_file

    def locked(path: bytes) -> Any:
        if os.fsdecode(path).endswith("IM0002.dcm"):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(read, "_open_file", locked)
    assert _scan(project, {1: root})["files"]["unreadable"] == 1
    for _ in range(2):
        # Read again, because a permission can come back, and found as it
        # was: nothing for a regroup or a merge (ADR 0020 decision 8).
        again = _scan(project, {1: root})
        assert (again["changed"], again["files"]["read"]) == (False, 1)
    assert meta(project)["generation"] == "1"


def test_a_file_gone_between_walk_and_read_gets_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bulk(tmp_path / "source", 4)
    real = index_run.walk.walk

    def walk_then_delete(*args: Any, **kwargs: Any) -> Any:
        walked = real(*args, **kwargs)
        (root / "bulk" / "00000" / "IM0002.dcm").unlink()
        return walked

    monkeypatch.setattr(index_run.walk, "walk", walk_then_delete)
    project = tmp_path / "Study.bcoaproj"
    result = _scan(project, {1: root})
    assert result["files"]["seen"] == 4
    assert result["sources"]["1"]["files"] == 3
    assert (1, b"bulk/00000/IM0002.dcm") not in files_by_path(project)


def test_reads_are_written_in_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _bulk(tmp_path / "source", 10)
    batches: list[int] = []
    real = index_run.Scanner._write_batch

    def spy(self: Any, source_id: int, root: bytes, batch: list[Any], *rest: Any) -> None:
        batches.append(len(batch))
        real(self, source_id, root, batch, *rest)

    monkeypatch.setattr(index_run, "BATCH_FILES", 4)
    monkeypatch.setattr(index_run.Scanner, "_write_batch", spy)
    _scan(tmp_path / "Study.bcoaproj", {1: root})
    assert batches == [4, 4, 2]
    assert math.fsum(batches) == 10


# ------------------------------------------------------------------ reachability


def test_an_empty_walk_where_files_were_keeps_them(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 3)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    shutil.move(root / "bulk", tmp_path / "away")
    result = _scan(project, {1: root})
    assert result == {
        "changed": False,
        "sources": {
            "1": {"state": "unreachable", "files": 3, "bad_dirs": 0, "code": "source.empty_walk"}
        },
        "files": result["files"],
        "seconds": result["seconds"],
    }
    assert len(files_by_path(project)) == 3
    with closing(connect(project)) as db:
        state, summary = db.execute("SELECT state, summary_json FROM scans").fetchone()
    assert (state, json.loads(summary)) == (
        "unreachable",
        {"files": 3, "bad_dirs": 0, "code": "source.empty_walk"},
    )


def test_a_root_that_is_gone_is_unreachable_and_keeps_its_rows(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 3)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    root.rename(tmp_path / "unplugged")
    result = _scan(project, {1: root})
    assert result["sources"]["1"] == {"state": "unreachable", "files": 3, "bad_dirs": 0}
    assert len(files_by_path(project)) == 3


def test_the_first_scan_of_an_empty_folder_is_complete(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    result = _scan(tmp_path / "Study.bcoaproj", {1: tmp_path / "source"})
    assert result["changed"] is False
    assert result["sources"]["1"] == {"state": "complete", "files": 0, "bad_dirs": 0}


def test_a_folder_that_cannot_be_listed_keeps_its_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bulk(tmp_path / "source", 10)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    real = os.scandir

    def scandir(path: Any) -> Any:
        if os.fsencode(path).endswith(b"bulk/00001"):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(index_run.walk.os, "scandir", scandir)
    blocked = _scan(project, {1: root})
    assert blocked["changed"] is True
    assert blocked["sources"]["1"] == {"state": "complete", "files": 10, "bad_dirs": 1}
    assert blocked["files"]["seen"] == 5
    with closing(connect(project)) as db:
        assert db.execute("SELECT rel_dir, code FROM bad_dirs").fetchall() == [
            (b"bulk/00001", "permission_denied")
        ]
    monkeypatch.undo()
    again = _scan(project, {1: root})
    assert again["changed"] is True
    assert again["sources"]["1"] == {"state": "complete", "files": 10, "bad_dirs": 0}


@pytest.mark.parametrize("failure", ["vanished", "io_error"])
def test_a_source_that_drops_during_a_reread_loses_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = _bulk(tmp_path / "source", 30, per_series=10)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    before = files_by_path(project)
    unmounted = tmp_path / "unmounted"
    real_write = index_run.Scanner._write_batch
    real_open = read._open_file
    written: list[int] = []

    def drop_after_first(
        self: Any, source_id: int, root_bytes: bytes, batch: Any, *rest: Any
    ) -> None:
        real_write(self, source_id, root_bytes, batch, *rest)
        written.append(len(batch))
        if len(written) == 1:
            root.rename(unmounted)

    def failing_share(path: bytes) -> Any:
        if not root.exists():
            # A share that went away answers EIO rather than "not found".
            raise OSError(errno.EIO, "Input/output error")
        return real_open(path)

    monkeypatch.setattr(index_run, "BATCH_FILES", 10)
    monkeypatch.setattr(index_run.Scanner, "_write_batch", drop_after_first)
    if failure == "io_error":
        monkeypatch.setattr(read, "_open_file", failing_share)
    result = _scan(project, {1: root}, force_reread=True)
    assert written == [10]
    assert result["sources"]["1"] == {"state": "unreachable", "files": 30, "bad_dirs": 0}
    assert files_by_path(project) == before
    with closing(connect(project)) as db:
        assert db.execute("SELECT count(*) FROM cat_series").fetchone()[0] == 3
        assert db.execute("SELECT state FROM scans").fetchone()[0] == "unreachable"


def test_an_io_error_on_a_file_that_read_before_keeps_its_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bulk(tmp_path / "source", 10)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    before = files_by_path(project)
    real = read._open_file

    def flaky(path: bytes) -> Any:
        if b"/00001/" in path:
            raise OSError(errno.EIO, "Input/output error")
        return real(path)

    monkeypatch.setattr(read, "_open_file", flaky)
    result = _scan(project, {1: root}, force_reread=True)
    assert result["sources"]["1"]["state"] == "complete"
    assert result["files"]["image"] == 10
    after = files_by_path(project)
    flaky_rows = [key for key in after if b"/00001/" in key[1]]
    assert len(flaky_rows) == 5
    for key in flaky_rows:
        # The values of the last good read, and a reader version that makes
        # the next scan try again.
        assert after[key] | {"reader_version": 0} == before[key] | {"reader_version": 0}
        assert after[key]["reader_version"] == 0
    monkeypatch.undo()
    assert _scan(project, {1: root})["files"]["read"] == 5


def test_the_project_folder_inside_a_source_is_never_indexed(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "Data", 5)
    project = root / "Study.bcoaproj"
    first = _scan(project, {1: root})
    assert first["files"]["seen"] == 5
    # The catalog changes with every commit: read along, it made every
    # rescan a new generation.
    second = _scan(project, {1: root})
    assert (second["changed"], second["files"]["read"]) == (False, 0)
    assert not [path for _, path in files_by_path(project) if path.startswith(b"Study.bcoaproj")]


def test_an_entry_the_walk_cannot_describe_is_counted_as_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bulk(tmp_path / "source", 10)
    project = tmp_path / "Study.bcoaproj"
    real = index_run.walk.walk
    odd = b"bulk/00001/IM0003.dcm"

    def undescribed(*args: Any, **kwargs: Any) -> Any:
        walked = real(*args, **kwargs)
        walked.entries = [entry for entry in walked.entries if entry.rel_path != odd]
        walked.unknown[odd] = "io_error"
        return walked

    monkeypatch.setattr(index_run.walk, "walk", undescribed)
    first = _scan(project, {1: root})
    assert (first["files"]["seen"], first["files"]["unreadable"]) == (9, 1)
    assert files_by_path(project)[(1, odd)]["code"] == "read.io_error"
    assert _scan(project, {1: root})["changed"] is False
    monkeypatch.undo()
    again = _scan(project, {1: root})
    assert (again["files"]["read"], again["files"]["unreadable"]) == (1, 0)


# ------------------------------------------------------------------ the catalog's lock


def _hold_shared_lock(project: Path) -> sqlite3.Connection:
    """A reader in the middle of a transaction, as the app's label reads and a
    previews job are: it blocks every COMMIT until it ends."""
    reader = sqlite3.connect(catalog_path(project), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM files").fetchone()
    return reader


def test_a_reader_that_holds_the_catalog_too_long_makes_the_job_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bulk(tmp_path / "source", 3)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    monkeypatch.setattr(index_catalog, "BUSY_SECONDS", 0.5)
    with closing(_hold_shared_lock(project)):
        error = _failure(project, index_job(project, {1: root}, force_reread=True))
    # Every batch rolled back whole, so the job can simply run again.
    assert (error["code"], error["recoverable"]) == ("catalog_busy", True)
    assert _scan(project, {1: root}, force_reread=True)["files"]["read"] == 3


def test_a_cancel_is_answered_while_the_catalog_is_busy(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 3)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    # A SIGTERM that came after the job must not end the test run.
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: None)
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM))
    try:
        with closing(_hold_shared_lock(project)):
            started = time.monotonic()
            timer.start()
            code, events = run_index(index_job(project, {1: root}, force_reread=True))
            elapsed = time.monotonic() - started
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, previous)
    # SQLite waits 10 s in all, but in steps of 0.2 s, between which the
    # handler runs: 8.3 s were measured with the whole wait in one step.
    assert (code, events[-1]["status"]) == (130, "cancelled")
    assert elapsed < 2.0


def test_the_catalog_is_the_owners_only(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 2)
    project = tmp_path / "Study.bcoaproj"
    previous = os.umask(0o022)
    try:
        _scan(project, {1: root})
        index, catalog = project / "index", catalog_path(project)
        assert stat.S_IMODE(index.stat().st_mode) == 0o700
        assert stat.S_IMODE(catalog.stat().st_mode) == 0o600
        # One made before, with the umask, is made private at the next open.
        index.chmod(0o755)
        catalog.chmod(0o644)
        _scan(project, {1: root})
        assert stat.S_IMODE(index.stat().st_mode) == 0o700
        assert stat.S_IMODE(catalog.stat().st_mode) == 0o600
    finally:
        os.umask(previous)


# ------------------------------------------------------------------ modes


def test_a_removed_source_leaves_the_catalog(tmp_path: Path) -> None:
    first, second = _bulk(tmp_path / "one", 3), _bulk(tmp_path / "two", 2)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: first, 2: second})
    result = _scan(project, {1: first}, mode="regroup", removed=[2])
    assert result["changed"] is True and result["sources"] == {}
    assert result["generation"] == 2
    assert kinds(project) == {1: {"image": 3}}
    with closing(connect(project)) as db:
        assert [r[0] for r in db.execute("SELECT source_id FROM scans")] == [1]


def test_a_regroup_always_writes_a_generation(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 2)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    result = _scan(project, {1: root}, mode="regroup")
    assert (result["changed"], result["generation"]) == (True, 2)
    assert result["files"]["seen"] == 0


def test_other_selection_settings_regroup_without_reading(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 2)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    job = index_job(project, {1: root})
    job["payload"]["selection"]["min_slices"] = 20
    code, events = run_index(job)
    result = result_of(events)
    assert (code, result["changed"], result["files"]["read"]) == (0, True, 0)


def _with_identity(project: Path, root: Path, **identity: Any) -> dict[str, Any]:
    job = index_job(project, {1: root})
    job["payload"]["identity"].update(identity)
    code, events = run_index(job)
    assert code == 0, events[-2:]
    return result_of(events)


def _id_states(project: Path) -> dict[str, int]:
    with closing(connect(project)) as db:
        return dict(db.execute("SELECT pid_state, count(*) FROM files GROUP BY pid_state"))


def test_a_new_placeholder_id_takes_effect_without_a_changed_file(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 10)
    project = tmp_path / "Study.bcoaproj"
    defaults = index_job(project, {1: root})["payload"]["identity"]["placeholder_ids"]
    _scan(project, {1: root})
    assert _id_states(project) == {"present": 10}
    # An ID is compared without regard to case, as at the read.
    result = _with_identity(project, root, placeholder_ids=[*defaults, "bulk-00001"])
    assert (result["changed"], result["files"]["read"]) == (True, 0)
    assert _id_states(project) == {"present": 5, "placeholder": 5}
    with closing(connect(project)) as db:
        assert not db.execute(
            "SELECT count(*) FROM files WHERE pid_state = 'placeholder' "
            "AND (patient_id IS NOT NULL OR pid_link IS NOT NULL)"
        ).fetchone()[0]
        assert db.execute("SELECT count(*) FROM cat_id_candidates").fetchone()[0]
    # Taken back, the placeholder rows are read again: their ID is not kept.
    result = _scan(project, {1: root})
    assert (result["changed"], result["files"]["read"]) == (True, 5)
    assert _id_states(project) == {"present": 10}


def test_other_folder_id_settings_regroup_without_reading(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 10)
    project = tmp_path / "Study.bcoaproj"
    placeholders = [*index_job(project, {1: root})["payload"]["identity"]["placeholder_ids"]]
    placeholders.append("BULK-00001")
    _with_identity(project, root, placeholder_ids=placeholders)

    def candidates() -> int:
        with closing(connect(project)) as db:
            return int(db.execute("SELECT count(*) FROM cat_id_candidates").fetchone()[0])

    assert candidates() > 0
    result = _with_identity(project, root, placeholder_ids=placeholders, folder_ids=False)
    assert (result["changed"], result["files"]["read"], candidates()) == (True, 0, 0)
    again = _with_identity(project, root, placeholder_ids=placeholders, folder_ids=False)
    assert again["changed"] is False


def test_previews_are_not_built_yet(tmp_path: Path) -> None:
    job = index_job(tmp_path / "Study.bcoaproj", {})
    payload = job["payload"]
    payload.update(mode="previews", parts=[{"catalog_id": "0" * 32, "part_ref": 1}])
    for key in ("link_key", "link_key_id", "scan_sources"):
        payload.pop(key)
    error = _failure(tmp_path, job)
    assert (error["code"], error["recoverable"]) == ("unsupported_job", False)


# ------------------------------------------------------------------ refusals


def test_a_source_the_payload_does_not_list_is_refused(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 1)
    project = tmp_path / "Study.bcoaproj"
    job = index_job(project, {1: root}, scan=[1, 3])
    error = _failure(project, job)
    assert (error["code"], error["recoverable"]) == ("index_failed", False)
    assert str(tmp_path) not in json.dumps(error)
    assert not (project / "index" / "catalog.sqlite").exists()
    job = index_job(project, {1: root})
    job["payload"]["sources"][0].update(status="unavailable", root=None)
    assert _failure(project, job)["code"] == "index_failed"


def test_a_catalog_path_that_leaves_the_project_is_refused(tmp_path: Path) -> None:
    job = index_job(tmp_path / "Study.bcoaproj", {1: _bulk(tmp_path / "source", 1)})
    job["payload"]["catalog"] = "../elsewhere/catalog.sqlite"
    assert _failure(tmp_path, job)["code"] == "index_failed"
    assert not (tmp_path / "elsewhere").exists()


def test_a_damaged_key_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    project = tmp_path / "Study.bcoaproj"
    job = index_job(project, {1: _bulk(tmp_path / "source", 1)})
    job["payload"]["link_key_id"] = "0000000000000000"
    error = _failure(project, job)
    assert (error["code"], error["recoverable"]) == ("link_key_mismatch", False)
    assert "AAEC" not in error["message"]
    assert not (project / "index").exists()
    job["payload"]["link_key"] = None
    assert _failure(project, job)["code"] == "link_key_mismatch"


def test_a_catalog_another_job_holds_is_busy(tmp_path: Path) -> None:
    project = tmp_path / "Study.bcoaproj"
    (project / "index").mkdir(parents=True)
    with open(project / "index" / "catalog.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        error = _failure(project, index_job(project, {1: _bulk(tmp_path / "source", 1)}))
    assert (error["code"], error["recoverable"]) == ("catalog_busy", True)


# ------------------------------------------------------------------ the key


def test_rows_read_under_another_key_lose_their_identifiers(tmp_path: Path) -> None:
    root = _bulk(tmp_path / "source", 3)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: root})
    with closing(connect(project)) as db:
        assert db.execute("SELECT count(*) FROM files WHERE patient_id IS NOT NULL").fetchone()[0]
    # After Remove Identifiers the catalog is deleted; should one survive,
    # a scan without the key must still leave nothing of the old reads.
    result = _scan(project, {1: root}, link_key=None)
    assert result["changed"] is True and result["files"]["read"] == 3
    with closing(connect(project)) as db:
        rows = db.execute(
            "SELECT pid_state, pid_link, issuer_link, patient_id, accession_number FROM files"
        ).fetchall()
    assert set(rows) == {("withheld", None, None, None, None)}
    assert meta(project)["files_link_key_id"] == ""


def test_a_keyless_job_cancelled_before_its_regroup_leaves_no_identifier(
    scanned_corpus: Any, corpus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "Study.bcoaproj"
    shutil.copytree(scanned_corpus.project_dir, project)
    derived = (
        "SELECT (SELECT count(*) FROM pending_identifiers), "
        "(SELECT count(*) FROM cat_id_candidates), "
        "(SELECT count(*) FROM cat_studies WHERE pid_link IS NOT NULL)"
    )
    with closing(connect(project)) as db:
        assert all(db.execute(derived).fetchone())

    def cancel(*args: Any, **kwargs: Any) -> Any:
        raise Cancelled

    monkeypatch.setattr(index_run.read, "read_file", cancel)
    code, events = run_index(index_job(project, corpus.sources, link_key=None))
    assert (code, events[-1]["status"]) == (130, "cancelled")
    # Left for the regroup, the identifiers it had derived survived every job
    # cancelled before it.
    with closing(connect(project)) as db:
        assert db.execute(derived).fetchone() == (0, 0, 0)
    assert meta(project)["complete"] == "0"
    data = catalog_path(project).read_bytes()
    for value in (b"CANARY-ID-4711", b"CANARYACC"):
        assert value not in data, value


def test_a_scan_of_one_source_strips_the_rows_of_the_others(tmp_path: Path) -> None:
    first, second = _bulk(tmp_path / "one", 2), _bulk(tmp_path / "two", 2)
    project = tmp_path / "Study.bcoaproj"
    _scan(project, {1: first, 2: second})
    _scan(project, {1: first, 2: second}, scan=[1], link_key=None)
    with closing(connect(project)) as db:
        db.row_factory = sqlite3.Row
        unread = db.execute("SELECT * FROM files WHERE source_id = 2").fetchall()
    assert {(row["pid_state"], row["patient_id"], row["reader_version"]) for row in unread} == {
        ("withheld", None, 0)
    }
