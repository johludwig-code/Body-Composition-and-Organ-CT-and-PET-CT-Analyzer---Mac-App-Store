"""A crash behaves like a cancel (ADR 0020 decision 9): a worker killed with
SIGKILL at a batch boundary, inside a batch, or before, inside or at the end
of the regroup leaves a catalog from which the next scan converges on what an
uninterrupted scan writes: the rows of `files`, `frames` and
`dicomdir_entries`, and every table the regroup derives from them, compared
with files named by path, since the two scans need not give out the same
file_ids. A regroup killed after a generation was published leaves that
generation as it was, complete and mergeable.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import dicom_factory
from index_jobs import (
    catalog_path,
    derived,
    files_by_path,
    finish,
    index_job,
    killed,
    meta,
    result_of,
    run_index,
    start_worker,
    write_job,
)

BATCH = 10

# Each kill point wraps one function of the worker's own process and kills
# that process with SIGKILL when the point is reached: nothing unwinds, no
# rollback runs, and the rollback journal is all that is left to repair it.
_KILL = """
import os, signal
from bcoa_worker.index import group, run
run.BATCH_FILES = {batch}

def _kill_on(owner, name, nth, after):
    real = getattr(owner, name)
    calls = [0]
    def wrapper(*args, **kwargs):
        calls[0] += 1
        if calls[0] == nth and not after:
            os.kill(os.getpid(), signal.SIGKILL)
        value = real(*args, **kwargs)
        if calls[0] == nth and after:
            os.kill(os.getpid(), signal.SIGKILL)
        return value
    setattr(owner, name, wrapper)

# The catalog connection as the regroup sees it, killing the process when
# the regroup sends a statement that starts with `prefix`: at COMMIT every
# write of the regroup is in the journal and none in the catalog; at an
# INSERT the regroup is half written.
class _KillAt:
    def __init__(self, db, prefix):
        self._db = db
        self._prefix = prefix
    def _check(self, sql):
        if sql.strip().upper().startswith(self._prefix):
            os.kill(os.getpid(), signal.SIGKILL)
    def execute(self, sql, *args):
        self._check(sql)
        return self._db.execute(sql, *args)
    def executemany(self, sql, *args):
        self._check(sql)
        return self._db.executemany(sql, *args)
    def __getattr__(self, name):
        return getattr(self._db, name)

def _kill_inside_regroup(prefix):
    real = group.regroup
    group.regroup = lambda db, context: real(_KillAt(db, prefix), context)
"""

POINTS = {
    # After the second batch committed.
    "after_batch": "_kill_on(run.Scanner, '_write_batch', 2, True)",
    # Inside the third batch's transaction, four rows written.
    "inside_batch": f"_kill_on(run.Scanner, '_write_row', {2 * BATCH + 5}, False)",
    # Every file read and committed, the regroup not begun.
    "before_regroup": "_kill_on(group, 'regroup', 1, False)",
    # Inside the regroup's transaction, while it forms the parts and while
    # it describes them: nothing of it written yet.
    "regroup_parts": "_kill_on(group._Build, '_parts', 1, False)",
    "regroup_describe": "_kill_on(group._Build, '_describe', 3, False)",
    # Inside the regroup's transaction, studies and series written, checks
    # not yet.
    "inside_regroup_writes": "_kill_inside_regroup('INSERT INTO CAT_CHECKS')",
    # Inside the regroup's transaction, its writes done.
    "inside_regroup": "_kill_inside_regroup('COMMIT')",
}
# The points that die with a write of their own in the journal.
HOT_JOURNAL = {"inside_batch", "inside_regroup_writes", "inside_regroup"}
# The points that die after the last file was read.
REGROUP_POINTS = {
    "before_regroup",
    "regroup_parts",
    "regroup_describe",
    "inside_regroup_writes",
    "inside_regroup",
}


@pytest.fixture(scope="module")
def tree(corpus: Any, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """A tree with plain slices, an Enhanced file (frames), a DICOMDIR
    (entries) and a NIfTI volume, and what an uninterrupted scan writes of
    it."""
    folder = tmp_path_factory.mktemp("crash")
    root = folder / "source"
    dicom_factory.write_bulk(root, 60, per_series=20)
    for part in ("enhanced/single", "dicomdir/complete", "nifti"):
        shutil.copytree(corpus.sources[1] / part, root / part)
    reference = folder / "reference"
    code, _ = run_index(index_job(reference, {1: root}))
    assert code == 0
    tables = derived(reference)
    # Every table that this tree can fill is filled, so that a comparison
    # of empty tables cannot pass for one of the regroup's work; the tree
    # has no PET.
    assert [name for name, rows in tables.items() if not rows] == ["cat_pairs"]
    return root, {"files": files_by_path(reference), "derived": tables}


@pytest.mark.parametrize("point", sorted(POINTS))
def test_a_killed_scan_converges_on_the_uninterrupted_one(
    tree: tuple[Path, dict[str, Any]], tmp_path: Path, point: str
) -> None:
    root, reference = tree
    project = tmp_path / "Study.bcoaproj"
    job = index_job(project, {1: root})
    prelude = _KILL.format(batch=BATCH) + POINTS[point]
    code, events = finish(start_worker(write_job(job, tmp_path / "job.json"), prelude=prelude))
    assert killed(code), (code, events[-1:])
    assert not [e for e in events if e["type"] in ("result", "done")]
    journal = Path(f"{catalog_path(project)}-journal")
    # A transaction had written when the process died: its journal is hot.
    assert journal.exists() == (point in HOT_JOURNAL)

    code, events = run_index(index_job(project, {1: root}))
    assert code == 0
    result = result_of(events)
    assert not journal.exists()
    assert files_by_path(project) == reference["files"]
    assert derived(project) == reference["derived"]
    values = meta(project)
    assert (values["regroup_due"], values["complete"]) == ("0", "1")
    assert result["changed"] is True
    assert result["sources"]["1"]["state"] == "complete"
    if point in REGROUP_POINTS:
        # Nothing was left to read; the regroup the crash cost runs anyway.
        assert result["files"]["read"] == 0
        assert values["generation"] == "1"
    if point == "after_batch":
        assert result["files"]["read"] == len(reference["files"]) - 2 * BATCH


def _regroup_job(project: Path, root: Path) -> dict[str, Any]:
    # Other settings, so that the regroup changes what the first generation
    # says: with five slices enough, the bulk series qualify.
    job = index_job(project, {1: root}, mode="regroup")
    job["payload"]["selection"]["min_slices"] = 5
    return job


def test_a_regroup_killed_after_a_generation_leaves_that_generation(
    tree: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    root, reference = tree
    first = tmp_path / "first" / "Study.bcoaproj"
    assert run_index(index_job(first, {1: root}))[0] == 0
    published = derived(first)
    assert published == reference["derived"]
    killed_project = tmp_path / "killed" / "Study.bcoaproj"
    shutil.copytree(first, killed_project)
    # The same regroup, once uninterrupted and once killed half written.
    assert run_index(_regroup_job(first, root))[0] == 0
    second = derived(first)
    assert second["meta"]["generation"] == "2" and second != published

    job = write_job(_regroup_job(killed_project, root), tmp_path / "job.json")
    prelude = _KILL.format(batch=BATCH) + POINTS["inside_regroup_writes"]
    code, _ = finish(start_worker(job, prelude=prelude))
    assert killed(code)
    # The first connection that may write rolls the hot journal back, as the
    # next index job's open of the catalog does.
    with closing(sqlite3.connect(catalog_path(killed_project))) as db:
        db.execute("SELECT count(*) FROM catalog_meta").fetchone()
    assert derived(killed_project) == published

    assert run_index(_regroup_job(killed_project, root))[0] == 0
    assert derived(killed_project) == second
