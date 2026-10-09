"""What the merge lets into the project (ADR 0024, Consequences, first and
third moment; ADR 0026 decision 1): outside `identifiers`, no column of
`project.sqlite` holds a canary, and no file in the project folder holds a
name or a birth date.

The merge is the app's own SQL, extracted from BCOAStore and run by
`corpus_check.merge` as `IndexStore` runs it, so this holds what the app
would write. What the scan itself lets out is held by
`test_index_privacy_read.py`; the moment right after Remove Identifiers,
before its scan, needs the Swift side and is not covered here.

Beside the corpus, a third source carries the canaries the corpus does not:
a patient that only a DICOMDIR names, an anonymous study whose folder is the
patient candidate, and a NIfTI file whose name holds a PatientID. Each is a
different way for a value to reach the merge.
"""

from __future__ import annotations

import gzip
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import dicom_factory
from corpus_check import merge, new_project
from index_jobs import LINK_KEY_ID, Scan, catalog_path, connect, scan_in_process, scan_in_worker

EXTRA = 3
# Found anywhere outside `identifiers`, any of these is a leak; "CANARY"
# covers every canary of the corpus and of the third source by its prefix.
COLUMN_CANARIES = ("CANARY", "19010101", "1901-01-01")
# No file of the project may hold these at all, `identifiers` included.
NEVER_STORED = (
    b"CANARY^NAME",
    b"CANARY^DIRNAME",
    b"SYNTHETIC^CORPUS",
    b"19010101",
    b"1901-01-01",
    b"CANARY_FOLDER",
    b"CANARY_PATIENT_FOLDER",
)


def _third_source(folder: Path) -> Path:
    from pydicom.fileset import FileSet

    writer = dicom_factory._Writer(folder, "privacy-merge")
    named = dicom_factory.Patient("CANARY-DIR-ID", birth_date="19010101", name="CANARY^DIRNAME")
    study = dicom_factory.Study("dir", named, "20240701", "CT Canary Dir", "CANARYDIRACC")
    series = dicom_factory.Series("ct", "dir", "Dir 3.0", 2, dicom_factory.axial(5))
    file_set = FileSet()
    file_set.UID = writer.uid("file-set")
    for index in range(5):
        file_set.add(writer.image(study, series, index))
    file_set.write(writer.path(1, "dir/DICOMDIR").parent)
    anonymous = dicom_factory.Study(
        "anonymous", dicom_factory.Patient("ANONYMOUS", "F", "040Y"), "20240702", "CT Canary Anon"
    )
    writer.series(
        1,
        anonymous,
        dicom_factory.Series(
            "ct", "CANARY_PATIENT_FOLDER/CT", "Canary Anon 3.0", 2, dicom_factory.axial(3)
        ),
    )
    shape = (16, 16, 20)
    volume = dicom_factory.nifti_bytes(shape, (0.75, 0.75, 2.5), dicom_factory._nifti_volume(shape))
    writer.raw(1, "nifti/CT_CANARY-NII-ID.nii.gz", gzip.compress(volume, mtime=0))
    return writer.path(1, "")


@pytest.fixture(scope="module")
def sources(corpus: Any, tmp_path_factory: pytest.TempPathFactory) -> dict[int, Path]:
    third = _third_source(tmp_path_factory.mktemp("privacy-third-source"))
    return {**corpus.sources, EXTRA: third}


def _merged(folder: Path, sources: dict[int, Path], scan: Scan, **options: Any) -> Path:
    """A new project in a folder of its own, with the scan's catalog merged
    into it; returns the folder."""
    assert scan.code == 0, scan.events[-2:]
    project_dir = folder / "Merged.bcoaproj"
    project_dir.mkdir(parents=True)
    project = new_project(
        project_dir / "project.sqlite", {source_id: f"source{source_id}" for source_id in sources}
    )
    merge(project, catalog_path(scan.project_dir), **options)
    return project_dir


def _cells(project: Path) -> Iterator[tuple[str, str, str]]:
    """Every value of every column of every table as text, with its table
    and column; blobs are decoded leniently so that a value stored as bytes
    is read too."""
    with closing(sqlite3.connect(project.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")
        ]
        for table in tables:
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            # The names come from sqlite_master above, not from a value.
            for row in db.execute(f'SELECT * FROM "{table}"'):  # noqa: S608
                for column, value in zip(columns, row, strict=True):
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    if value is not None:
                        yield table, column, str(value)


def _leaks(project: Path, *, skip: frozenset[str]) -> list[tuple[str, str, str]]:
    return [
        (table, column, value)
        for table, column, value in _cells(project)
        if table not in skip and any(canary in value for canary in COLUMN_CANARIES)
    ]


def _bytes_of_every_file(project_dir: Path) -> dict[str, bytes]:
    # Every file, so that a -journal or -wal left beside the database is
    # read as well as the database itself.
    return {
        str(path.relative_to(project_dir)): path.read_bytes()
        for path in sorted(project_dir.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def keyed(sources: dict[int, Path], tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Scan]:
    folder = tmp_path_factory.mktemp("privacy-keyed")
    scan = scan_in_worker(folder / "scan", sources)
    return _merged(folder, sources, scan, link_key_id=LINK_KEY_ID), scan


def test_with_a_key_no_column_outside_identifiers_holds_a_canary(keyed: tuple[Path, Scan]) -> None:
    project_dir, _ = keyed
    project = project_dir / "project.sqlite"
    assert _leaks(project, skip=frozenset({"identifiers"})) == []
    # The canaries did reach the merge: each PatientID is where decision 6
    # keeps it, so the check above had something to miss.
    with closing(sqlite3.connect(project)) as db:
        ids = {row[0] for row in db.execute("SELECT patient_id FROM identifiers")}
        numbers = " ".join(
            row[0] or "" for row in db.execute("SELECT accession_numbers FROM identifiers")
        )
    assert {"CANARY-ID-4711", "CANARY-DIR-ID", "CANARY-NII-ID"} <= ids
    assert "CANARYACC" in numbers and "CANARYDIRACC" in numbers


def test_with_a_key_no_file_of_the_project_holds_a_name_a_birth_date_or_a_folder(
    keyed: tuple[Path, Scan],
) -> None:
    project_dir, scan = keyed
    files = _bytes_of_every_file(project_dir)
    assert "project.sqlite" in files
    for name, data in files.items():
        for value in NEVER_STORED:
            assert value not in data, (name, value)
    # The folder was the anonymous study's patient candidate, so its label
    # sat in the catalog that was merged and stayed there.
    with closing(connect(scan.project_dir)) as db:
        labels = {row[0] for row in db.execute("SELECT label FROM cat_id_candidates")}
    assert "CANARY_PATIENT_FOLDER" in labels


def test_without_a_key_and_identifiers_the_project_holds_no_canary_anywhere(
    sources: dict[int, Path], tmp_path: Path
) -> None:
    scan = scan_in_process(tmp_path / "scan", sources, link_key=None)
    # A merge that refuses new studies would add nothing to a new project
    # and so hold nothing to look at; the policy that adds them as unlinked
    # patients (#26) lets every study through.
    project_dir = _merged(
        tmp_path,
        sources,
        scan,
        link_key_id=None,
        keep_identifiers=0,
        new_study_policy="add_unlinked",
    )
    project = project_dir / "project.sqlite"
    with closing(sqlite3.connect(project)) as db:
        studies = db.execute("SELECT count(*) FROM studies").fetchone()[0]
        identifiers = db.execute("SELECT count(*) FROM identifiers").fetchone()[0]
    assert studies > 10 and identifiers == 0
    assert _leaks(project, skip=frozenset()) == []
    for name, data in _bytes_of_every_file(project_dir).items():
        assert b"CANARY" not in data, name
        for value in NEVER_STORED:
            assert value not in data, (name, value)
