"""What the scan lets out (ADR 0024 decisions 7 and 9.5): events, the result
and the job log hold counts and codes only, the catalog never holds a name or
a birth date, and without a link key the identifiers are not even read.

The canary values of the corpus exist for this: a value that reaches an
output shows up here by name.
"""

from __future__ import annotations

import json
import shutil
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import dicom_factory
from bcoa_worker.index import read
from index_jobs import (
    catalog_path,
    check_events,
    connect,
    finish,
    index_job,
    run_index,
    scan_in_process,
    start_worker,
    write_job,
)

CANARIES = ("CANARY-ID-4711", "CANARY^NAME", "CANARY_FOLDER", "19010101", "1901-01-01", "CANARYACC")
NEVER_STORED = (b"CANARY^NAME", b"SYNTHETIC^CORPUS", b"19010101", b"1901-01-01")
IDENTIFIER_TAGS = {0x00100020, 0x00100021, 0x00080050}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (k, *_strings(v))]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def test_the_worker_lets_no_canary_out(worker_scanned_corpus: Any, corpus: Any) -> None:
    scan = worker_scanned_corpus
    assert scan.code == 0, scan.log[-2000:]
    check_events(scan.events)
    protocol = json.dumps(scan.events, ensure_ascii=False)
    for canary in (*CANARIES, "SYNTHETIC^CORPUS", "PIX-0001", str(corpus.root)):
        assert canary not in protocol, canary
        assert canary not in scan.log, canary


def test_the_result_holds_counts_and_codes_only(worker_scanned_corpus: Any) -> None:
    result = worker_scanned_corpus.result
    words = set(_strings(result))
    keys = {"changed", "catalog_id", "generation", "sources", "files", "studies", "series"}
    keys |= {"parts_split", "seconds", "state", "bad_dirs", "code", "walk", "read", "group"}
    keys |= set(result["files"]) | set(result["sources"])
    values = {"complete", "interrupted", "unreachable", "source.empty_walk", result["catalog_id"]}
    assert words <= keys | values, words - keys - values


def test_the_catalog_never_holds_a_name_or_a_birth_date(scanned_corpus: Any) -> None:
    data = catalog_path(scanned_corpus.project_dir).read_bytes()
    for value in NEVER_STORED:
        assert value not in data, value
    # With a key, the PatientID is kept for the merge's identifiers.
    assert b"CANARY-ID-4711" in data


def test_without_a_key_no_identifier_is_requested_or_written(
    corpus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[set[int]] = []
    real = read.dcmread

    def spy(*args: Any, **kwargs: Any) -> Any:
        requested.append(set(kwargs.get("specific_tags") or ()))
        return real(*args, **kwargs)

    monkeypatch.setattr(read, "dcmread", spy)
    scan = scan_in_process(tmp_path, corpus.sources, link_key=None)
    assert scan.code == 0
    assert len(requested) > 1000
    assert not [tags for tags in requested if tags & IDENTIFIER_TAGS]
    data = catalog_path(scan.project_dir).read_bytes()
    for value in (*NEVER_STORED, b"CANARY-ID-4711", b"CANARYACC"):
        assert value not in data, value
    # "pid:" and "issuer:" are in the catalog's own DDL comments, so the rows
    # are asked instead of the bytes.
    with closing(connect(scan.project_dir)) as db:
        linked = db.execute(
            "SELECT count(*) FROM files WHERE pid_link IS NOT NULL OR issuer_link IS NOT NULL "
            "OR patient_id IS NOT NULL OR accession_number IS NOT NULL"
        ).fetchone()[0]
        states = {row[0] for row in db.execute("SELECT DISTINCT pid_state FROM files")}
    assert linked == 0
    assert states == {"withheld", None}
    # The folder names come back as the relative paths that open the files.
    assert b"CANARY_FOLDER" in data


def _identifier_tags(ds: Any) -> set[int]:
    """The identifier tags a dataset holds at any depth, nested items included."""
    found = set()
    for tag in list(ds.keys()):
        if tag in (0x00100010, 0x00100020):
            found.add(int(tag))
        element = ds[tag]
        if element.VR == "SQ":
            for item in element.value or ():
                found |= _identifier_tags(item)
    return found


def test_a_dicomdir_without_a_key_gives_no_identifier_to_any_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydicom.fileset import FileSet

    writer = dicom_factory._Writer(tmp_path / "corpus", "dicomdir-canary")
    patient = dicom_factory.Patient("CANARY-DIR-ID", name="CANARY^DIRNAME")
    study = dicom_factory.Study("canary", patient, "20240701", "CT Canary", "CANARYDIRACC")
    series = dicom_factory.Series("ct", "dir", "Dir 3.0", 2, dicom_factory.axial(5))
    file_set = FileSet()
    file_set.UID = writer.uid("file-set")
    for index in range(5):
        file_set.add(writer.image(study, series, index))
    target = writer.path(1, "dir/DICOMDIR").parent
    file_set.write(target)
    parsed: list[Any] = []
    real = read.dcmread

    def spy(*args: Any, **kwargs: Any) -> Any:
        ds = real(*args, **kwargs)
        parsed.append(ds)
        return ds

    monkeypatch.setattr(read, "dcmread", spy)
    # The DICOMDIR is walked without pydicom, which parsed every PATIENT
    # record of the sequence it was asked for.
    assert not hasattr(read.dicomdir, "dcmread")
    scan = scan_in_process(tmp_path, {1: target}, link_key=None)
    assert scan.code == 0
    assert len(parsed) == 5
    assert not [ds for ds in parsed if _identifier_tags(ds)]
    with closing(connect(scan.project_dir)) as db:
        assert db.execute("SELECT count(*) FROM dicomdir_entries").fetchone()[0] == 5
    data = catalog_path(scan.project_dir).read_bytes()
    for value in (b"CANARY-DIR-ID", b"CANARY^DIRNAME", b"CANARYDIRACC"):
        assert value not in data, value


def test_a_failure_reports_where_and_never_what(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    dicom_factory.write_bulk(root / "CANARY_FOLDER", 2, per_series=2)

    def fail(path: bytes, **kwargs: Any) -> Any:
        secret = {"patient": "CANARY^NAME"}  # a local the log must not show
        raise RuntimeError(f"cannot read {path!r} of {secret}")

    monkeypatch.setattr(read, "read_file", fail)
    code, events = run_index(index_job(tmp_path / "Study.bcoaproj", {1: root}))
    check_events(events)
    error, done = events[-2:]
    assert (code, error["code"], error["recoverable"]) == (1, "index_failed", False)
    assert done["status"] == "failed"
    log = capfd.readouterr().err
    assert "index_failed: RuntimeError" in log
    assert "bcoa_worker/index/run.py:" in log and " in _read_one" in log
    for text in (json.dumps(events), log):
        assert "CANARY" not in text
        assert str(tmp_path) not in text


def test_an_unexpected_error_in_sqlite_is_index_failed_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    dicom_factory.write_bulk(root, 1, per_series=1)
    project = tmp_path / "Study.bcoaproj"
    run_index(index_job(project, {1: root}))
    # A catalog file another program turned into a folder.
    shutil.rmtree(project / "index")
    (project / "index" / "catalog.sqlite").mkdir(parents=True)
    code, events = run_index(index_job(project, {1: root}))
    assert (code, events[-2]["code"]) == (1, "index_failed")
    assert str(tmp_path) not in json.dumps(events)


# Between the reader's own open and SimpleITK's open by name, the file becomes
# a folder, as a sync client or a second program can make it.
_SWAP_PRELUDE = """
import os
from bcoa_worker.index import nifti as _nifti
_real_reader = _nifti._reader
def _swapped(path):
    if path.endswith(b"CT_RACE.nii"):
        os.remove(path)
        os.mkdir(path)
    return _real_reader(path)
_nifti._reader = _swapped
"""


def test_a_nifti_file_that_fails_in_simpleitk_names_nothing_in_the_log(tmp_path: Path) -> None:
    root = tmp_path / "source"
    folder = root / "CANARY_FOLDER"
    folder.mkdir(parents=True)
    shape, spacing = (16, 16, 20), (0.75, 0.75, 2.5)
    data = dicom_factory.nifti_bytes(shape, spacing, dicom_factory._nifti_volume(shape))
    (folder / "CT_RACE.nii").write_bytes(data)
    (folder / "CT_OK.nii").write_bytes(data)
    job = index_job(tmp_path / "Study.bcoaproj", {1: root})
    code, events = finish(
        start_worker(write_job(job, tmp_path / "job.json"), prelude=_SWAP_PRELUDE)
    )
    assert code == 0
    files = next(event for event in events if event["type"] == "result")["payload"]["files"]
    assert (files["nifti"], files["unreadable"]) == (1, 1)
    log = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")
    # Left to ITK's factory, HDF5's IO printed its error stack with the path.
    assert "HDF5" not in log
    assert "CANARY" not in log and str(tmp_path) not in log
