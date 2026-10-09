"""The index job (ADR 0020 decision 10): walk the sources the payload names,
read what changed into the catalog, and regroup.

A scan is minutes of work that a cancel, a crash or a dropped share can
interrupt, so everything it learns goes to disk in batches, and the catalog
is its resume state: a file whose row matches its size, modification time and
the reader version is never read again (ADR 0020 decision 8). A batch is at
most 1 000 files or 2 s of reading, one transaction each; SIGTERM rolls back
the batch being written, marks the source `interrupted` and ends the job
`cancelled`, and the next scan reads only what is left. A SIGKILL leaves an
open transaction that the rollback journal undoes at the next open, so a
crash behaves like a cancel (decision 9).

Only counts and codes leave this module (ADR 0024 decision 7). Every
exception is caught here and becomes `index_failed`; the job log gets the
frames of its traceback (file, line, function) and the exception's type,
never its message or locals, because pydicom's, SimpleITK's and the operating
system's messages quote paths and values, and folder names often carry
patient names.
"""

from __future__ import annotations

import bisect
import errno
import fcntl
import json
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, NamedTuple

from bcoa_worker import __version__
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.errors import JobFailure
from bcoa_worker.index import group, read, walk
from bcoa_worker.index.catalog import (
    execute_waiting,
    is_busy,
    open_catalog,
    private_folder,
    wait_briefly,
)
from bcoa_worker.index.identity import (
    IdentityConfig,
    LinkKeyMismatch,
    folded_placeholders,
    verify_link_key,
)
from bcoa_worker.index.select import SelectionConfig
from bcoa_worker.protocol import (
    Job,
    Log,
    Progress,
    ProgressDetail,
    ProtocolError,
    Result,
)
from bcoa_worker.worker import Cancelled

BATCH_FILES = 1000
BATCH_SECONDS = 2.0
# The app redraws from these events; more than two a second is noise on the
# protocol and costs the read time.
PROGRESS_SECONDS = 0.5
LOCK_NAME = "catalog.lock"

# Unreadable files read again at every scan whatever their size and time.
# An I/O error can pass with the share that caused it (ADR 0020 decision 8),
# and so can a permission: `chmod` changes the time of the inode, not the
# modification time the walk compares, so a file the user made readable
# would otherwise stay unreadable for good. Either costs one failed open.
_RETRY_CODES = frozenset({"read.io_error", "read.permission_denied"})

# Kinds whose row holds no value of the read but its kind and code: a re-read
# that finds the same again changes nothing, and must not make a generation.
# A file that stays unreadable is read again at every scan (above), and each
# of those scans wrote a new generation (measured: three unchanged scans,
# three generations).
_VALUELESS_KINDS = frozenset({"unreadable", "changing", "not_dicom", "archive", "symlink"})
# The read code of an entry the walk could not describe, by the walk's code.
_UNDESCRIBED_CODES = {"permission_denied": "read.permission_denied", "io_error": "read.io_error"}

# The columns of `files` that a scan writes itself; the rest come from the
# reader's values.
_FIXED_COLUMNS = ("source_id", "rel_path", "size", "mtime_ns", "reader_version", "kind", "code")
_EMPTY_FILE_COUNTS = (
    "seen",
    "read",
    "unchanged",
    "image",
    "non_image",
    "dicomdir",
    "nifti",
    "not_dicom",
    "unreadable",
    "archive",
    "symlink",
    "changing",
    "duplicates",
)
_KINDS = frozenset(_EMPTY_FILE_COUNTS[3:12])


# ------------------------------------------------------------------ the payload


@dataclass(frozen=True, slots=True)
class Payload:
    """An index payload in modes scan and regroup, checked before the catalog
    is touched: a payload that cannot run must not cost the catalog a write."""

    mode: str
    catalog: Path
    # Never walked when it lies below a source root (ADR 0029).
    project_dir: Path
    # None when the payload names no folder: no preview is deleted then.
    previews: Path | None
    # In the order the payload lists them, with their roots as bytes.
    roots: dict[int, bytes]
    # The name of each listed source's folder, the label of level 0 of the
    # folder candidates (ADR 0024 decision 3); a source without a root in
    # this payload has ''.
    labels: dict[int, str]
    removed_sources: frozenset[int]
    force_reread: bool
    key: bytes | None
    # The key's id; '' without a key, as catalog_meta keeps it.
    key_id: str
    selection: SelectionConfig
    identity: IdentityConfig


def parse_payload(job: Job) -> Payload:
    raw = job.payload
    mode = raw.get("mode")
    if mode == "previews":
        # ADR 0025 builds them; until then the app gets the same answer as
        # for any job kind that is not built yet.
        raise JobFailure("unsupported_job", "Index mode 'previews' is not built yet")
    if mode not in ("scan", "regroup"):
        raise ProtocolError("unknown index mode")
    catalog = raw.get("catalog")
    if not isinstance(catalog, str) or not catalog:
        raise ProtocolError("the index payload names no catalog")
    scan_sources = raw.get("scan_sources") or []
    if not isinstance(scan_sources, list) or not all(_is_id(item) for item in scan_sources):
        raise ProtocolError("scan_sources is not a list of source ids")
    if mode == "scan" and not scan_sources:
        raise ProtocolError("a scan names no source")
    if mode == "regroup" and scan_sources:
        raise ProtocolError("a regroup scans no source")
    previews = raw.get("previews_dir")
    if previews is not None and (not isinstance(previews, str) or not previews):
        raise ProtocolError("previews_dir is not a relative path")
    key, key_id = _link_key(raw.get("link_key"), raw.get("link_key_id"))
    removed = frozenset(
        entry["source_id"]
        for entry in raw.get("sources") or []
        if isinstance(entry, dict)
        and _is_id(entry.get("source_id"))
        and entry.get("status") == "removed"
    )
    return Payload(
        mode=mode,
        catalog=job.project_path(catalog),
        project_dir=job.project_dir,
        previews=job.project_path(previews) if previews is not None else None,
        roots={
            source_id: os.fsencode(str(job.source_root(source_id)))
            for source_id in dict.fromkeys(scan_sources)
        },
        labels=_labels(raw.get("sources")),
        removed_sources=removed,
        force_reread=raw.get("force_reread") is True,
        key=key,
        key_id=key_id,
        selection=SelectionConfig.from_json(raw.get("selection")),
        identity=IdentityConfig.from_json(raw.get("identity")),
    )


def _labels(sources: object) -> dict[int, str]:
    labels: dict[int, str] = {}
    for entry in sources if isinstance(sources, list) else []:
        if isinstance(entry, dict) and _is_id(entry.get("source_id")):
            root = entry.get("root")
            name = os.path.basename(root.rstrip("/")) if isinstance(root, str) else ""
            labels[entry["source_id"]] = name
    return labels


def _is_id(value: object) -> bool:
    return type(value) is int and value >= 1


def _link_key(link_key: object, link_key_id: object) -> tuple[bytes | None, str]:
    mismatch = JobFailure("link_key_mismatch", "The link key does not match its id")
    if link_key is None:
        if link_key_id is not None:
            raise mismatch
        return None, ""
    if not isinstance(link_key, str) or not isinstance(link_key_id, str):
        raise mismatch
    try:
        return verify_link_key(link_key, link_key_id), link_key_id
    except LinkKeyMismatch:
        raise mismatch from None


# ------------------------------------------------------------------ events


_PROGRESS_MESSAGES = {
    "walk": "Listing files: {done} found",
    "read": "Reading headers: {done} of {total}",
    "group": "Grouping series",
}


class _Reporter:
    """Progress as numbers (ADR 0020 decision 10), at most one event every
    PROGRESS_SECONDS, except the first of each phase, so that the app shows
    the phase change at once. Messages hold counts, never a path."""

    def __init__(self, job_id: str, channel: ProtocolChannel) -> None:
        self._job_id = job_id
        self._channel = channel
        self._last = float("-inf")

    def progress(self, phase: str, done: int, total: int | None, *, first: bool = False) -> None:
        now = time.monotonic()
        if not first and now - self._last < PROGRESS_SECONDS:
            return
        self._last = now
        fraction = min(done / total, 1.0) if total else None
        message = _PROGRESS_MESSAGES[phase].format(done=done, total=total)
        detail = ProgressDetail(phase, done, total)  # type: ignore[arg-type]
        self._channel.send(Progress(self._job_id, "index", fraction, message, detail=detail))

    def log(self, message: str) -> None:
        self._channel.send(Log("info", message))


# ------------------------------------------------------------------ cancel


class _Cancel:
    """SIGTERM, noticed twice over. The handler raises Cancelled where the
    job happens to be, which also ends a read blocked on a slow share; and it
    sets a flag that the scan checks between files, because a library that
    catches every exception (pydicom does in places) would otherwise swallow
    the cancel and the job would run on."""

    def __init__(self) -> None:
        self.requested = False

    def handler(self, signum: int, frame: FrameType | None) -> None:
        self.requested = True
        raise Cancelled

    def check(self) -> None:
        if self.requested:
            raise Cancelled


@contextmanager
def _cancel_on_sigterm() -> Iterator[_Cancel]:
    cancel = _Cancel()
    if threading.current_thread() is not threading.main_thread():
        # Signal handlers can only be set from the main thread; a test that
        # runs the job elsewhere cancels through the flag.
        yield cancel
        return
    previous = signal.signal(signal.SIGTERM, cancel.handler)
    try:
        yield cancel
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def _sigterm_deferred() -> Iterator[None]:
    """A second SIGTERM must not break off the rollback and the short write
    that record a cancel; it is delivered when they are done."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


# ------------------------------------------------------------------ the catalog


@contextmanager
def _catalog_lock(catalog: Path) -> Iterator[None]:
    """The second guard of ADR 0021 decision 5: the job queue already runs one
    index job at a time per project. Closing the descriptor releases it."""
    private_folder(catalog.parent)
    fd = os.open(catalog.parent / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JobFailure(
                "catalog_busy", "Another index job is writing the catalog", recoverable=True
            ) from None
        except OSError as error:
            # Some network file systems have no flock; there it is best
            # effort, and the queue is the guarantee.
            if error.errno not in (errno.ENOLCK, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
        yield
    finally:
        os.close(fd)


def _busy() -> JobFailure:
    return JobFailure("catalog_busy", "The catalog is in use", recoverable=True)


def _open(path: Path) -> sqlite3.Connection:
    try:
        db = open_catalog(path)
    except sqlite3.OperationalError as error:
        if is_busy(error):
            raise _busy() from None
        raise
    wait_briefly(db)
    return db


def _no_check() -> None:
    pass


@contextmanager
def _transaction(db: sqlite3.Connection, check: Callable[[], None] = _no_check) -> Iterator[None]:
    # BEGIN and COMMIT wait for another program's lock in short steps, so that
    # a cancel is answered while they wait (`catalog.execute_waiting`).
    execute_waiting(db, "BEGIN IMMEDIATE", check)
    try:
        yield
        execute_waiting(db, "COMMIT", check)
    except BaseException:
        # A cancel that arrived after COMMIT returned finds no transaction:
        # the batch is written, which is what a later scan expects.
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def _meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM catalog_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO catalog_meta (key, value) VALUES (?, ?)", (key, value))


def _regroup_due(db: sqlite3.Connection) -> None:
    # Set in the transaction of every write to files or bad_dirs, so that a
    # scan cancelled after its last batch, or a regroup that failed, still
    # regroups next time although no file has changed by then.
    _set_meta(db, "regroup_due", "1")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ids(ids: Iterable[int]) -> str:
    # One parameter for a set of source ids, read with json_each, so that no
    # statement is built from strings.
    return json.dumps(sorted(ids))


# ------------------------------------------------------------------ one source


class _Row(NamedTuple):
    file_id: int
    size: int
    mtime_ns: int
    reader_version: int
    kind: str
    code: str | None
    nifti_json: str | None


@dataclass(slots=True)
class SourceScan:
    """What the scan of one source did, for the result and `scans`."""

    state: str = "interrupted"
    files: int = 0
    bad_dirs: int = 0
    code: str | None = None
    seen: int = 0
    read: int = 0
    unchanged: int = 0
    # Rows whose values were kept after an I/O error, for the log.
    kept: int = 0
    walk_seconds: float = 0.0
    read_seconds: float = 0.0

    def entry(self) -> dict[str, Any]:
        """The source's entry in the result (index_result.schema.json)."""
        entry: dict[str, Any] = {"state": self.state, **self.summary()}
        return entry

    def summary(self) -> dict[str, Any]:
        """`scans.summary_json`, which the merge copies into
        `sources.summary_json`: the entry's counts and code, as the app
        writes them from an unchanged result (ADR 0020 decision 8)."""
        summary: dict[str, Any] = {"files": self.files, "bad_dirs": self.bad_dirs}
        if self.code is not None:
            summary["code"] = self.code
        return summary


@dataclass(slots=True)
class _Plan:
    to_read: list[walk.Entry] = field(default_factory=list)
    stale: list[int] = field(default_factory=list)


class _SourceLost(Exception):
    """The source's root went away while its files were read."""


class Scanner:
    """Scans sources into one catalog connection."""

    def __init__(
        self,
        db: sqlite3.Connection,
        payload: Payload,
        reporter: _Reporter,
        cancel: _Cancel,
    ) -> None:
        self.db = db
        self.payload = payload
        self.reporter = reporter
        self.cancel = cancel
        self.identity = (
            read.Identity(payload.key, payload.identity.placeholder_ids)
            if payload.key is not None
            else None
        )
        self.project = walk.identity(os.fsencode(str(payload.project_dir)))
        fixed = set(_FIXED_COLUMNS) | {"file_id"}
        self.columns = [
            row[1] for row in db.execute("PRAGMA table_info(files)") if row[1] not in fixed
        ]
        names = [*_FIXED_COLUMNS, *self.columns]
        updates = ", ".join(f"{name} = excluded.{name}" for name in names[2:])
        # Every column is written on update as well, so that a file read
        # again as another kind keeps nothing of what its old read found. The
        # names come from the catalog's own DDL, never from a file.
        listed, marks = ", ".join(names), ", ".join("?" * len(names))
        self.upsert = (
            f"INSERT INTO files ({listed}) VALUES ({marks}) "  # noqa: S608
            f"ON CONFLICT (source_id, rel_path) DO UPDATE SET {updates} RETURNING file_id"
        )

    def scan(self, source_id: int, root: bytes) -> SourceScan:
        outcome = SourceScan()
        self._start(source_id)
        try:
            self._scan(source_id, root, outcome)
        except BaseException as error:
            with _sigterm_deferred():
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                if isinstance(error, Cancelled):
                    self._finish(source_id, outcome, "interrupted")
            raise
        return outcome

    def _scan(self, source_id: int, root: bytes, outcome: SourceScan) -> None:
        started = time.monotonic()
        self.reporter.progress("walk", 0, None, first=True)
        walked = walk.walk(root, progress=self._walk_progress, skip=self.project)
        outcome.walk_seconds = time.monotonic() - started
        self.cancel.check()
        existing = self._rows(source_id)
        if not walked.reachable or (walked.empty and existing):
            # Nothing of the source is deleted: a share that dropped comes
            # back, and to drop a source's series the user removes it.
            if walked.reachable:
                outcome.code = "source.empty_walk"
            self._finish(source_id, outcome, "unreachable")
            return
        plan = self._plan(walked, existing)
        outcome.seen = len(walked.entries)
        outcome.unchanged = outcome.seen - len(plan.to_read)
        self._forget(source_id, plan.stale, walked, existing)
        self.db.execute("UPDATE scans SET state = 'reading' WHERE source_id = ?", (source_id,))
        started = time.monotonic()
        try:
            self._read(source_id, root, plan.to_read, existing, outcome)
        except _SourceLost:
            # What was written before the root went is what was read before
            # it went; the batch read since is dropped, and nothing is
            # deleted (ADR 0022 decision 1, ADR 0029).
            outcome.read_seconds = time.monotonic() - started
            self._finish(source_id, outcome, "unreachable")
            self.reporter.log(f"Source {source_id}: gone after {outcome.read} files read")
            return
        outcome.read_seconds = time.monotonic() - started
        self._finish(source_id, outcome, "complete")
        self.reporter.log(
            f"Source {source_id}: {outcome.seen} files listed, {outcome.read} read, "
            f"{len(plan.stale)} gone, {len(walked.bad_dirs)} folders not listed, "
            f"{len(walked.unknown)} entries not described, {walked.skipped} skipped, "
            f"{outcome.kept} kept after an I/O error"
        )

    def _walk_progress(self, found: int) -> None:
        self.cancel.check()
        self.reporter.progress("walk", found, None)

    def _rows(self, source_id: int) -> dict[bytes, _Row]:
        return {
            bytes(rel_path): _Row(*rest)
            for rel_path, *rest in self.db.execute(
                "SELECT rel_path, file_id, size, mtime_ns, reader_version, kind, code, "
                "CASE kind WHEN 'nifti' THEN nifti_json END FROM files WHERE source_id = ?",
                (source_id,),
            )
        }

    def _plan(self, walked: walk.Walk, existing: dict[bytes, _Row]) -> _Plan:
        plan = _Plan()
        force = self.payload.force_reread
        for entry in walked.entries:
            row = existing.get(entry.rel_path)
            if force or row is None or _needs_read(entry, row):
                plan.to_read.append(entry)
        seen = {entry.rel_path for entry in walked.entries}
        careful = bool(walked.bad_dirs or walked.unknown)
        plan.stale = [
            row.file_id
            for rel_path, row in existing.items()
            if rel_path not in seen and not (careful and walk.kept_unseen(rel_path, walked))
        ]
        return plan

    def _forget(
        self,
        source_id: int,
        stale: list[int],
        walked: walk.Walk,
        existing: dict[bytes, _Row],
    ) -> None:
        """Delete the rows of files that are gone, record the folders that
        could not be listed, and give each entry that could not be described
        an `unreadable` row unless rows at or below it are kept, in one
        transaction before the reads."""
        bad_dirs = walked.bad_dirs
        known = {
            bytes(rel_dir): code
            for rel_dir, code in self.db.execute(
                "SELECT rel_dir, code FROM bad_dirs WHERE source_id = ?", (source_id,)
            )
        }
        undescribed = _undescribed(walked.unknown, existing)
        if not stale and known == bad_dirs and not undescribed:
            return
        with _transaction(self.db, self.cancel.check):
            # frames and dicomdir_entries go with their file (ON DELETE CASCADE).
            self.db.executemany("DELETE FROM files WHERE file_id = ?", [(i,) for i in stale])
            if known != bad_dirs:
                self.db.execute("DELETE FROM bad_dirs WHERE source_id = ?", (source_id,))
                self.db.executemany(
                    "INSERT INTO bad_dirs (source_id, rel_dir, code) VALUES (?, ?, ?)",
                    [(source_id, rel_dir, code) for rel_dir, code in bad_dirs.items()],
                )
            for rel_path, code in undescribed:
                # Size and time 0, and a code that is tried again: the next
                # walk that can describe the entry reads it.
                values = [source_id, rel_path, 0, 0, read.READER_VERSION, "unreadable", code]
                self.db.execute(self.upsert, values + [None] * len(self.columns)).fetchall()
            _regroup_due(self.db)

    def _read(
        self,
        source_id: int,
        root: bytes,
        entries: list[walk.Entry],
        existing: dict[bytes, _Row],
        outcome: SourceScan,
    ) -> None:
        total = len(entries)
        self.reporter.progress("read", 0, total, first=True)
        batch: list[tuple[walk.Entry, read.FileRecord | None]] = []
        batch_started = 0.0
        with read.quiet_pydicom():
            for entry in entries:
                self.cancel.check()
                if not batch:
                    batch_started = time.monotonic()
                batch.append((entry, self._read_one(root, entry, existing.get(entry.rel_path))))
                outcome.read += 1
                self.reporter.progress("read", outcome.read, total)
                if len(batch) >= BATCH_FILES or time.monotonic() - batch_started >= BATCH_SECONDS:
                    self._write_batch(source_id, root, batch, existing, outcome)
                    batch = []
            if batch:
                self._write_batch(source_id, root, batch, existing, outcome)

    def _read_one(self, root: bytes, entry: walk.Entry, row: _Row | None) -> read.FileRecord | None:
        if entry.symlink:
            # Recorded with the walk's lstat, never followed and never opened.
            return read.FileRecord("symlink", entry.size, entry.mtime_ns)
        previous = (row.size, row.mtime_ns, row.nifti_json) if row and row.nifti_json else None
        return read.read_file(
            os.path.join(root, entry.rel_path), identity=self.identity, previous_nifti=previous
        )

    def _write_batch(
        self,
        source_id: int,
        root: bytes,
        batch: list[tuple[walk.Entry, read.FileRecord | None]],
        existing: dict[bytes, _Row],
        outcome: SourceScan,
    ) -> None:
        self.cancel.check()
        writes = [(entry, record, existing.get(entry.rel_path)) for entry, record in batch]
        if any(_doubtful(record, row) for _, record, row in writes) and not walk.root_present(root):
            # A share that drops mid-read looks file by file like files that
            # vanished or failed: 90 of 120 rows were deleted or blanked that
            # way, and the source still ended complete (measured).
            raise _SourceLost
        writes = [write for write in writes if not _unchanged(write[1], write[2])]
        if not writes:
            return
        with _transaction(self.db, self.cancel.check):
            changed = False
            for entry, record, row in writes:
                changed |= self._write_row(source_id, root, entry, record, row, outcome)
            if changed:
                _regroup_due(self.db)

    def _write_row(
        self,
        source_id: int,
        root: bytes,
        entry: walk.Entry,
        record: read.FileRecord | None,
        row: _Row | None,
        outcome: SourceScan,
    ) -> bool:
        """Write one file's read; whether the rows the regroup reads changed."""
        if record is None:
            # Gone between the walk and the read, if its folder says so.
            if row is None or not _gone(root, entry.rel_path):
                return False
            self.db.execute("DELETE FROM files WHERE file_id = ?", (row.file_id,))
            return True
        if (
            row is not None
            and record.code == "read.io_error"
            and row.kind not in ("unreadable", "changing")
        ):
            # An I/O error passes with the share that caused it: the values
            # of the last good read stay, and the file is read again at the
            # next scan.
            self.db.execute("UPDATE files SET reader_version = 0 WHERE file_id = ?", (row.file_id,))
            outcome.kept += 1
            return False
        values = [
            source_id,
            entry.rel_path,
            record.size,
            record.mtime_ns,
            read.READER_VERSION,
            record.kind,
            record.code,
            *(record.values.get(column) for column in self.columns),
        ]
        # fetchall, not fetchone: the statement must run to its end before
        # the batch commits.
        file_id = self.db.execute(self.upsert, values).fetchall()[0][0]
        if row is not None:
            self.db.execute("DELETE FROM frames WHERE file_id = ?", (file_id,))
            self.db.execute("DELETE FROM dicomdir_entries WHERE file_id = ?", (file_id,))
        if record.frames:
            self.db.executemany(
                "INSERT INTO frames (file_id, frame, ipp_x, ipp_y, ipp_z, iop, stack_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        file_id,
                        frame.frame,
                        *(frame.position or (None, None, None)),
                        frame.orientation,
                        frame.stack_id,
                    )
                    for frame in record.frames
                ],
            )
        if record.dicomdir:
            self.db.executemany(
                "INSERT INTO dicomdir_entries (file_id, sop_uid, series_uid) VALUES (?, ?, ?)",
                [(file_id, sop_uid, series_uid) for sop_uid, series_uid in record.dicomdir],
            )
        return True

    def _start(self, source_id: int) -> None:
        # The previous summary stays until this scan has one of its own.
        with _transaction(self.db, self.cancel.check):
            self.db.execute(
                "INSERT INTO scans (source_id, state, started_at, finished_at) "
                "VALUES (?, 'walking', ?, NULL) ON CONFLICT (source_id) DO UPDATE SET "
                "state = excluded.state, started_at = excluded.started_at, finished_at = NULL",
                (source_id, _now()),
            )

    def _finish(self, source_id: int, outcome: SourceScan, state: str) -> None:
        outcome.state = state
        outcome.files = self.db.execute(
            "SELECT count(*) FROM files WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
        outcome.bad_dirs = self.db.execute(
            "SELECT count(*) FROM bad_dirs WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
        # An interrupted scan keeps the time of the last one that finished,
        # which the merge carries into sources.indexed_at.
        finished = None if state == "interrupted" else _now()
        with _transaction(self.db):
            self.db.execute(
                "UPDATE scans SET state = ?, finished_at = ?, summary_json = ? WHERE source_id = ?",
                (state, finished, json.dumps(outcome.summary(), sort_keys=True), source_id),
            )


def _doubtful(record: read.FileRecord | None, row: _Row | None) -> bool:
    """A file with a row that vanished or failed to open: a fact about the
    file, or a share that went away, which only the root can tell apart."""
    if row is None:
        return False
    return record is None or (record.kind == "unreadable" and record.code in _RETRY_CODES)


def _unchanged(record: read.FileRecord | None, row: _Row | None) -> bool:
    """Whether a read found what the row already says, for a kind whose row
    holds nothing else."""
    return (
        record is not None
        and row is not None
        and record.kind in _VALUELESS_KINDS
        and row.reader_version == read.READER_VERSION
        and (record.kind, record.code, record.size, record.mtime_ns)
        == (row.kind, row.code, row.size, row.mtime_ns)
    )


def _gone(root: bytes, rel_path: bytes) -> bool:
    """Whether a file missing since the walk is really gone: its folder is
    there to say so, or is gone as well. A folder that cannot be asked says
    nothing, and the row stays."""
    try:
        os.stat(os.path.dirname(os.path.join(root, rel_path)))
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return True


def _undescribed(unknown: dict[bytes, str], existing: dict[bytes, _Row]) -> list[tuple[bytes, str]]:
    """The entries the walk could not describe that have no row at or below
    them, with their read code. Without a row they were counted nowhere and
    could not be shown (measured: 9 of 10 files, and the source said
    complete)."""
    if not unknown:
        return []
    paths = sorted(existing)
    found = []
    for rel_path, code in unknown.items():
        if rel_path in existing:
            continue
        below = rel_path + b"/"
        index = bisect.bisect_left(paths, below)
        if index < len(paths) and paths[index].startswith(below):
            continue
        found.append((rel_path, _UNDESCRIBED_CODES[code]))
    return found


def _needs_read(entry: walk.Entry, row: _Row) -> bool:
    return (
        (row.size, row.mtime_ns) != (entry.size, entry.mtime_ns)
        or row.reader_version != read.READER_VERSION
        or row.kind == "changing"
        or (row.kind == "unreadable" and row.code in _RETRY_CODES)
        or entry.symlink != (row.kind == "symlink")
    )


# ------------------------------------------------------------------ the job


def _adopt_key(db: sqlite3.Connection, payload: Payload, check: Callable[[], None]) -> None:
    """Rows read under another link key (or none) hold links that are worth
    nothing now, and after Remove Identifiers they must not survive at all;
    they lose their identifiers and are read again under this job's key.

    The identifiers the last regroup derived from them go in the same
    transaction: PatientIDs and accession numbers in `pending_identifiers`,
    folder labels and links in `cat_id_candidates`, links in `cat_studies`.
    Left for the regroup, they survived every job that was cancelled before
    it (measured: CANARY-ID-4711 still in the catalog's bytes). The
    generation they belonged to is then no longer whole, and is marked so
    until the next regroup. secure_delete overwrites the freed bytes on a
    runtime whose SQLite does not do so by default.
    """
    if _meta(db, "files_link_key_id") == payload.key_id:
        return
    previous = int(db.execute("PRAGMA secure_delete").fetchone()[0])
    db.execute("PRAGMA secure_delete = ON")
    try:
        with _transaction(db, check):
            stripped = db.execute(
                "UPDATE files SET pid_link = NULL, issuer_link = NULL, patient_id = NULL, "
                "accession_number = NULL, reader_version = 0, "
                "pid_state = CASE WHEN pid_state IS NULL THEN NULL ELSE 'withheld' END"
            ).rowcount
            stripped += db.execute("DELETE FROM pending_identifiers").rowcount
            stripped += db.execute("DELETE FROM cat_id_candidates").rowcount
            stripped += db.execute(
                "UPDATE cat_studies SET pid_link = NULL WHERE pid_link IS NOT NULL"
            ).rowcount
            _set_meta(db, "files_link_key_id", payload.key_id)
            # The rows are read again with this job's placeholders as well.
            _set_meta(db, "files_placeholders_sha256", payload.identity.placeholders_sha256())
            if stripped:
                _set_meta(db, "complete", "0")
                _regroup_due(db)
    finally:
        db.execute(f"PRAGMA secure_delete = {previous}")


def _adopt_placeholders(
    db: sqlite3.Connection, payload: Payload, check: Callable[[], None]
) -> None:
    """A PatientID's state was decided at its read by the placeholder IDs of
    that job, and an unchanged file is never read again; without this, an ID
    added as a placeholder stayed `present` and reached `identifiers`, and one
    removed stayed a placeholder (ADR 0029).

    A row whose ID is now a placeholder is demoted at once, because its ID is
    stored. A placeholder row is read again, because its ID is not: a scan
    reads it, the rows of a source that is not scanned keep their state
    until it is.
    """
    if payload.key is None:
        # No ID is read without a key, so no placeholder decides anything.
        return
    current = payload.identity.placeholders_sha256()
    if _meta(db, "files_placeholders_sha256") == current:
        return
    folded = folded_placeholders(payload.identity.placeholder_ids)
    demoted = [
        (file_id,)
        for file_id, patient_id in db.execute(
            "SELECT file_id, patient_id FROM files WHERE pid_state = 'present'"
        )
        if patient_id is not None and patient_id.casefold() in folded
    ]
    with _transaction(db, check):
        db.execute("UPDATE files SET reader_version = 0 WHERE pid_state = 'placeholder'")
        db.executemany(
            "UPDATE files SET pid_state = 'placeholder', pid_link = NULL, patient_id = NULL "
            "WHERE file_id = ?",
            demoted,
        )
        _set_meta(db, "files_placeholders_sha256", current)
        if demoted:
            _regroup_due(db)


def _drop_removed(
    db: sqlite3.Connection, removed: frozenset[int], check: Callable[[], None]
) -> None:
    """A removed source's files leave the catalog, so that the next
    generation no longer holds its series (ADR 0022 decision 1)."""
    if not removed:
        return
    ids = (_ids(removed),)
    with _transaction(db, check):
        dropped = db.execute(
            "DELETE FROM files WHERE source_id IN (SELECT value FROM json_each(?))", ids
        ).rowcount
        dropped += db.execute(
            "DELETE FROM bad_dirs WHERE source_id IN (SELECT value FROM json_each(?))", ids
        ).rowcount
        db.execute("DELETE FROM scans WHERE source_id IN (SELECT value FROM json_each(?))", ids)
        if dropped:
            _regroup_due(db)


def _settings_moved(db: sqlite3.Connection, payload: Payload) -> bool:
    """Whether the last generation was made with other settings, a key or a
    worker than this job's: then it is made again although no file changed."""
    if _meta(db, "generation") in (None, "0"):
        return False
    return (
        _meta(db, "selection_config_sha256") != payload.selection.sha256()
        or _meta(db, "identity_config_sha256") != payload.identity.sha256()
        or _meta(db, "link_key_id") != payload.key_id
        or _meta(db, "worker_version") != __version__
    )


def _file_counts(db: sqlite3.Connection, scans: dict[int, SourceScan]) -> dict[str, int]:
    counts = dict.fromkeys(_EMPTY_FILE_COUNTS, 0)
    for scan in scans.values():
        counts["seen"] += scan.seen
        counts["read"] += scan.read
        counts["unchanged"] += scan.unchanged
    if scans:
        for kind, count in db.execute(
            "SELECT kind, count(*) FROM files "
            "WHERE source_id IN (SELECT value FROM json_each(?)) GROUP BY kind",
            (_ids(scans),),
        ):
            if kind in _KINDS:
                counts[kind] = count
    return counts


def index(
    db: sqlite3.Connection, payload: Payload, reporter: _Reporter, cancel: _Cancel
) -> dict[str, Any]:
    """Scan, regroup when something changed, and return the result payload."""
    cancel.check()
    _adopt_key(db, payload, cancel.check)
    _adopt_placeholders(db, payload, cancel.check)
    _drop_removed(db, payload.removed_sources, cancel.check)
    scanner = Scanner(db, payload, reporter, cancel)
    scans = {source_id: scanner.scan(source_id, root) for source_id, root in payload.roots.items()}
    seconds = {
        "walk": round(sum(scan.walk_seconds for scan in scans.values()), 3),
        "read": round(sum(scan.read_seconds for scan in scans.values()), 3),
        "group": 0.0,
    }
    files = _file_counts(db, scans)
    sources = {str(source_id): scan.entry() for source_id, scan in scans.items()}
    due = (
        payload.mode == "regroup" or _meta(db, "regroup_due") == "1" or _settings_moved(db, payload)
    )
    if not due:
        return {"changed": False, "sources": sources, "files": files, "seconds": seconds}
    reporter.progress("group", 0, None, first=True)
    started = time.monotonic()
    outcome = group.regroup(
        db,
        group.RegroupContext(
            selection=payload.selection,
            identity=payload.identity,
            key=payload.key,
            link_key_id=payload.key_id,
            check_cancelled=cancel.check,
            source_labels=payload.labels,
            previews_dir=payload.previews,
            rollback_guard=_sigterm_deferred,
        ),
    )
    seconds["group"] = round(time.monotonic() - started, 3)
    files["duplicates"] = outcome.duplicates
    return {
        "changed": True,
        "catalog_id": _meta(db, "catalog_id"),
        "generation": int(_meta(db, "generation") or "0"),
        "sources": sources,
        "files": files,
        "studies": outcome.studies,
        "series": outcome.series,
        "parts_split": outcome.parts_split,
        "seconds": seconds,
    }


def run(job: Job, channel: ProtocolChannel) -> None:
    """The handler of job kind `index`."""
    with _cancel_on_sigterm() as cancel:
        try:
            result = _run(job, channel, cancel)
        except (Cancelled, JobFailure):
            raise
        except Exception as error:
            if is_busy(error):
                # Another program held the catalog longer than the job waits.
                # Every batch rolled back whole, so the job can simply run
                # again (ADR 0029).
                raise _busy() from None
            _log_frames(error)
            raise JobFailure(
                "index_failed", "The index job failed; the job log names where"
            ) from None
    channel.send(Result(job.job_id, result))


def _run(job: Job, channel: ProtocolChannel, cancel: _Cancel) -> dict[str, Any]:
    payload = parse_payload(job)
    reporter = _Reporter(job.job_id, channel)
    with _catalog_lock(payload.catalog):
        db = _open(payload.catalog)
        try:
            return index(db, payload, reporter, cancel)
        finally:
            db.close()


# ------------------------------------------------------------------ the log


def _log_frames(error: BaseException) -> None:
    """The traceback as frames only, into the job log (ADR 0024 decision 7).
    The type names the kind of failure and holds no value."""
    lines = [f"index_failed: {type(error).__name__}"]
    lines += [
        f"  {_code_path(frame.filename)}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)


_PACKAGE_PARENT = str(Path(__file__).resolve().parents[2]) + os.sep


def _code_path(filename: str) -> str:
    # Relative to the bundle's library folders: the absolute path of the
    # bundle can sit in a home folder, whose name is a person's.
    if "site-packages" + os.sep in filename:
        return filename.rsplit("site-packages" + os.sep, 1)[1]
    if filename.startswith(_PACKAGE_PARENT):
        return filename[len(_PACKAGE_PARENT) :]
    return os.path.basename(filename)
