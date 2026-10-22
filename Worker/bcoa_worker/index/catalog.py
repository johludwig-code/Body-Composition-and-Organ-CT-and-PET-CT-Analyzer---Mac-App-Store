"""The index catalog, `<project>/index/catalog.sqlite` (ADR 0020).

One row per file of every source, with its size, modification time and the
reader version that read it: that makes the catalog the resume state of a
scan, its header cache and the input of grouping. Beside them, the derived
`cat_*` tables of the last complete generation, which the app merges into
project.sqlite with `IndexSQL.merge`.

The worker is the only writer. The app attaches the catalog read-only for the
merge and reads labels briefly, so the catalog always runs in DELETE journal
mode: it may live on a network volume, where the shared memory of WAL is not
reliable (ADR 0021).

The catalog is a cache and is never migrated. A catalog of another format, or
one SQLite reports as damaged, is deleted and built again; that costs one full
read and nothing else, because the merge finds the series again by
fingerprint.

No column holds a name or a birth date, but much here can still identify a
patient, which is why Remove Identifiers deletes the whole file: PatientIDs
and accession numbers in plain text in `pending_identifiers` and, per file,
in `files.patient_id` and `files.accession_number`; folder names,
which often carry patient names, in plain text in `cat_id_candidates.label`
and as raw bytes in `files.rel_path` and `bad_dirs.rel_dir`; free text as the
scanner wrote it (study and series descriptions, protocol names); and HMAC
links (`pid_link`, `issuer_link`, the candidates' links). The scan that
rebuilds the catalog without a link key writes no identifier, link or
candidate label, but it records relative paths again, because they are what
opens a file. For the same reason the catalog is the owner's only: the file
is 0600 and `index/` 0700, as the job files are, because a project folder can
sit on a share that other users read (ADR 0029). The rollback journal takes
the catalog's mode.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path

CATALOG_FORMAT = 1

CATALOG_DDL = """
CREATE TABLE bad_dirs (
    source_id INTEGER NOT NULL,
    rel_dir BLOB NOT NULL,
    code TEXT NOT NULL CHECK (code IN ('permission_denied', 'io_error')),
    PRIMARY KEY (source_id, rel_dir)
);
-- rel_path is the raw bytes of the path below the source root: it opens the
-- file and is its key, so a name that is not valid UTF-8, or NFD on one volume
-- and NFC on another, never turns into a second file or a missing one.
CREATE TABLE files (
    file_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    rel_path BLOB NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    reader_version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'non_image', 'dicomdir', 'nifti', 'not_dicom',
        'unreadable', 'archive', 'symlink', 'changing')),
    code TEXT,
    sop_class_uid TEXT, sop_uid TEXT, study_uid TEXT, series_uid TEXT, for_uid TEXT,
    transfer_syntax_uid TEXT, frames INTEGER,
    pid_link TEXT,
    -- IssuerOfPatientID names the institution that assigned the ID, and only
    -- whether two differ matters, so it is kept as an HMAC under link_key like
    -- pid_link: "issuer:" + hex(HMAC-SHA256(link_key, "issuer" ␟ value)) after
    -- the same stripping and NFC. Kept per file because check.issuer_conflict
    -- is computed at every regroup and unchanged files are never read again.
    -- NULL, and the tag not requested, without a link key.
    issuer_link TEXT,
    pid_state TEXT CHECK (pid_state IN ('present', 'file', 'missing', 'placeholder', 'withheld')
        OR pid_state IS NULL),
    -- The PatientID as linked (stripped, NFC; for a NIfTI file the <id> of its
    -- name) and the AccessionNumber, from which the regroup writes each
    -- study's pending_identifiers. Per file, because a study's ID is that of
    -- its most frequent link, which only the counts over all its files decide,
    -- and because a file read again with a corrected ID must replace what the
    -- old read left. NULL, and the tags not requested, without a link key.
    patient_id TEXT, accession_number TEXT,
    sex TEXT, age_years REAL, age_source TEXT, age_conflict INTEGER,
    study_date TEXT, study_time TEXT, study_description TEXT, modality TEXT, series_number INTEGER,
    series_date TEXT, series_description TEXT, protocol TEXT, body_part TEXT,
    image_type TEXT, kernel TEXT, manufacturer TEXT, scanner_model TEXT, kvp REAL,
    contrast_agent TEXT, contrast_sequence INTEGER,
    slice_thickness REAL, spacing_between_slices REAL,
    pixel_spacing_row REAL, pixel_spacing_col REAL,
    image_rows INTEGER, image_columns INTEGER, iop TEXT, ipp_x REAL, ipp_y REAL, ipp_z REAL,
    instance_number INTEGER, acquisition_number INTEGER, temporal_position INTEGER,
    echo_number INTEGER,
    gantry_tilt REAL, rescale_slope REAL, rescale_intercept REAL,
    window_center REAL, window_width REAL,
    photometric TEXT, samples_per_pixel INTEGER, bits_allocated INTEGER,
    pixel_representation INTEGER,
    burned_in TEXT,
    pixel_data TEXT CHECK (pixel_data IN ('ok', 'truncated', 'missing') OR pixel_data IS NULL),
    pet_json TEXT, nifti_json TEXT,
    UNIQUE (source_id, rel_path)
);
CREATE INDEX files_by_series ON files(series_uid, sop_uid)
    WHERE kind IN ('image', 'non_image', 'nifti');
CREATE TABLE frames (
    file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    frame INTEGER NOT NULL,
    ipp_x REAL, ipp_y REAL, ipp_z REAL, iop TEXT, stack_id TEXT,
    PRIMARY KEY (file_id, frame)
) WITHOUT ROWID;
CREATE TABLE dicomdir_entries (
    file_id INTEGER NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    sop_uid TEXT NOT NULL,
    series_uid TEXT NOT NULL,
    PRIMARY KEY (file_id, sop_uid)
) WITHOUT ROWID;
-- The members of every part of the last generation, so that the next regroup
-- can carry a part_ref over by instance overlap when a split renumbers parts.
CREATE TABLE cat_instances (
    part_ref INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    frame INTEGER NOT NULL DEFAULT 0,
    sop_key TEXT NOT NULL,
    position_mm REAL,
    PRIMARY KEY (part_ref, ordinal)
) WITHOUT ROWID;

-- What the merge reads. catalog_meta keys: format, catalog_id (uuid4 hex),
-- generation, complete ('1' once the derived tables of that generation are
-- written), next_part_ref (never reused), reader_version, worker_version,
-- link_key_id, selection_config_sha256, identity_config_sha256. Three more
-- belong to the scan: regroup_due ('1' from the first write to files or
-- bad_dirs after a regroup until the next regroup clears it, so a scan
-- cancelled after its last batch still regroups next time), files_link_key_id
-- (the key under which the rows of files were read; '' without a key) and
-- files_placeholders_sha256 (the placeholder IDs they were read with).
CREATE TABLE catalog_meta (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE scans (
    source_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL
        CHECK (state IN ('walking', 'reading', 'complete', 'interrupted', 'unreachable')),
    started_at TEXT, finished_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
-- SQLite accepts NULL in a primary key that is not an INTEGER one, so these
-- say NOT NULL: a single NULL study_uid would make the merge's
-- `study_uid NOT IN (SELECT study_uid FROM idx.cat_studies)` NULL for every
-- study, and no study would ever be marked gone again.
CREATE TABLE pending_identifiers (
    study_uid TEXT NOT NULL PRIMARY KEY,
    patient_id TEXT,
    accession_number TEXT,
    id_source TEXT NOT NULL CHECK (id_source IN ('dicom', 'file')),
    generation INTEGER NOT NULL
);
CREATE TABLE cat_studies (
    study_uid TEXT NOT NULL PRIMARY KEY,
    pid_link TEXT,
    pid_state TEXT NOT NULL CHECK (pid_state IN
        ('present', 'file', 'missing', 'placeholder', 'withheld')),
    sex TEXT CHECK (sex IN ('F', 'M', 'O') OR sex IS NULL),
    age_years REAL,
    study_date TEXT,
    description TEXT
);
CREATE TABLE cat_series (
    part_ref INTEGER PRIMARY KEY,
    series_uid TEXT NOT NULL, part INTEGER NOT NULL, study_uid TEXT NOT NULL,
    modality TEXT NOT NULL, description TEXT, image_count INTEGER NOT NULL,
    slice_thickness_mm REAL, pixel_spacing_mm REAL, kernel TEXT, kernel_class TEXT,
    manufacturer TEXT, kvp REAL, contrast_agent TEXT, image_type TEXT,
    frame_of_reference_uid TEXT, fingerprint TEXT NOT NULL, series_number INTEGER,
    sop_class_uid TEXT, scanner_model TEXT, slice_count INTEGER, slice_spacing_mm REAL,
    z_extent_mm REAL, orientation TEXT, image_rows INTEGER, image_columns INTEGER,
    transfer_syntax_uid TEXT, middle_file_id INTEGER, middle_frame INTEGER,
    auto_rank INTEGER, auto_selected INTEGER NOT NULL, reason_json TEXT NOT NULL,
    UNIQUE (series_uid, part)
);
CREATE TABLE cat_checks (
    object_kind TEXT NOT NULL CHECK (object_kind IN ('source', 'study', 'series')),
    object_ref TEXT NOT NULL,
    code TEXT NOT NULL, level TEXT NOT NULL, params_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (object_kind, object_ref, code)
);
CREATE TABLE cat_pairs (
    pet_part_ref INTEGER NOT NULL, ct_part_ref INTEGER NOT NULL,
    pet_attenuation_corrected INTEGER, z_overlap_mm REAL,
    PRIMARY KEY (pet_part_ref, ct_part_ref)
);
-- Folder-name candidates for studies without a usable PatientID. The labels
-- live here only; project.sqlite gets the HMAC link once the user confirms.
CREATE TABLE cat_id_candidates (
    study_uid TEXT NOT NULL, source_id INTEGER NOT NULL, level INTEGER NOT NULL,
    label TEXT NOT NULL, link TEXT NOT NULL,
    PRIMARY KEY (study_uid, level)
);
"""

# Errors that mean the file itself is unusable. Anything else, a locked
# catalog above all, is raised: deleting a catalog that another process is
# writing would lose its work and break its transaction.
_DAMAGED = frozenset({sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT})
_COMPANIONS = ("-journal", "-wal", "-shm")


# How long the job waits in all for another program's lock on the catalog
# (ADR 0021: 10 s on a local volume), and how long one wait inside SQLite
# lasts. No signal handler runs while SQLite waits, so the step is what a
# cancel can be late by: with the whole 10 s in one step it was 8.3 s against
# the 2 s of ADR 0026.
BUSY_SECONDS = 10.0
BUSY_STEP_MS = 200


def private_folder(folder: Path) -> None:
    """Create `folder` for the owner only, and make an existing one so."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod(folder, 0o700)


def _private_file(path: Path) -> None:
    os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600))
    _chmod(path, 0o600)


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as error:
        # Volumes that keep no modes (FAT, some shares) refuse; the file is
        # then as private as that volume can make it, as for the job files.
        if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise


def open_catalog(path: Path, *, timeout: float = 10.0) -> sqlite3.Connection:
    """Open the catalog for writing: create it when it is missing, build it
    again when it is of another format or damaged.

    The connection is in autocommit mode (`isolation_level=None`) and every
    transaction is begun explicitly, because a batch of the read, and the
    whole regroup, is exactly one transaction that must roll back as one.
    """
    private_folder(path.parent)
    _private_file(path)
    connection = _connect(path, timeout)
    try:
        state = _state(connection)
    except sqlite3.DatabaseError as exc:
        if exc.sqlite_errorcode not in _DAMAGED:
            connection.close()
            raise
        state = "damaged"
    if state == "current":
        return connection
    if state != "empty":
        connection.close()
        for suffix in ("", *_COMPANIONS):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        _private_file(path)
        connection = _connect(path, timeout)
    try:
        _configure(connection)
        _create(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _connect(path: Path, timeout: float) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=timeout, isolation_level=None)


def is_busy(error: BaseException) -> bool:
    """Whether an SQLite error means that another connection holds a lock."""
    code = getattr(error, "sqlite_errorcode", None) or 0
    return isinstance(error, sqlite3.OperationalError) and (code & 0xFF) in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )


def wait_briefly(connection: sqlite3.Connection) -> None:
    """Make every statement of the job's connection wait BUSY_STEP_MS for a
    lock, and let only BEGIN IMMEDIATE and COMMIT need one.

    A reader of the app (a label, Show Files, a previews job) holds a shared
    lock that blocks a COMMIT. With cache spill on, a statement in the middle
    of a large transaction can need the exclusive lock as well, and SQLite
    then fails the statement for good; with it off, the dirty pages stay in
    memory until COMMIT, which `execute_waiting` can retry.
    """
    connection.execute(f"PRAGMA busy_timeout = {BUSY_STEP_MS}")
    connection.execute("PRAGMA cache_spill = OFF")


def execute_waiting(
    connection: sqlite3.Connection, statement: str, check: Callable[[], None]
) -> None:
    """Run BEGIN IMMEDIATE or COMMIT, which may be retried after SQLITE_BUSY,
    for up to BUSY_SECONDS, calling `check` between tries so that a cancel
    ends the wait. The last SQLITE_BUSY is raised."""
    deadline = time.monotonic() + BUSY_SECONDS
    while True:
        try:
            connection.execute(statement)
            return
        except sqlite3.OperationalError as error:
            if not is_busy(error) or time.monotonic() >= deadline:
                raise
        check()


def _configure(connection: sqlite3.Connection) -> None:
    # Set at every open: a catalog that something left in WAL mode must not
    # stay there on a network volume.
    connection.execute("PRAGMA journal_mode = DELETE")
    # frames and dicomdir_entries go with their file when a stale file row is
    # deleted.
    connection.execute("PRAGMA foreign_keys = ON")


def _state(connection: sqlite3.Connection) -> str:
    _configure(connection)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    if version == 0 and not tables:
        return "empty"
    if version != CATALOG_FORMAT or "catalog_meta" not in tables:
        return "other"
    row = connection.execute("SELECT value FROM catalog_meta WHERE key = 'format'").fetchone()
    if row is None or row[0] != str(CATALOG_FORMAT):
        return "other"
    # Measured on a 100 000-file catalog (48 MB, Linux, warm): 0.26-0.28 s,
    # a small part of the 10 s budget of an unchanged rescan. Without it, a
    # damaged page surfaces mid-job as index_failed, and does so on every
    # later scan as well.
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        return "damaged"
    return "current"


def _create(connection: sqlite3.Connection) -> None:
    # One transaction: a catalog interrupted while being created is empty at
    # the next open and is created again, never half made. The script begins
    # it itself, because executescript commits a transaction that is already
    # open before it runs.
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + CATALOG_DDL)
        connection.executemany(
            "INSERT INTO catalog_meta (key, value) VALUES (?, ?)",
            [
                ("format", str(CATALOG_FORMAT)),
                # A rebuilt catalog gets a new id, which the merge uses to tell
                # its part_refs from those of the catalog it replaced.
                ("catalog_id", uuid.uuid4().hex),
                ("generation", "0"),
                ("complete", "0"),
                ("next_part_ref", "1"),
            ],
        )
        connection.execute(f"PRAGMA user_version = {CATALOG_FORMAT}")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
