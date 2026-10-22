"""DICOMDIR as a completeness check (ADR 0022 decision 7): the records are
walked directly, so the instances a copy lost are still listed."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydicom.dataset import Dataset

import dicom_factory
from bcoa_worker.index import dicomdir, read


def _record(kind: str, offset: int, *, following: int = 0, child: int = 0, **values: str) -> Any:
    record = Dataset()
    record.DirectoryRecordType = kind
    record.OffsetOfTheNextDirectoryRecord = following
    record.OffsetOfReferencedLowerLevelDirectoryEntity = child
    for keyword, value in values.items():
        setattr(record, keyword, value)
    record.seq_item_tell = offset
    return record


def _image(offset: int, sop: str, following: int = 0) -> Any:
    return _record("IMAGE", offset, following=following, ReferencedSOPInstanceUIDInFile=sop)


def test_every_image_record_is_listed_with_its_series(corpus: Any) -> None:
    folder = corpus.sources[1] / "dicomdir" / "complete"
    with (folder / "DICOMDIR").open("rb") as stream:
        listed = dicomdir.read_entries(stream)
    files = sorted(p for p in folder.rglob("IM*") if p.is_file())
    headers = [dicom_factory.read_header(p) for p in files]
    assert sorted(listed) == sorted((h.SOPInstanceUID, h.SeriesInstanceUID) for h in headers)
    assert len(listed) == 5


def test_records_of_files_the_copy_lost_are_still_listed(corpus: Any) -> None:
    folder = corpus.sources[1] / "dicomdir" / "incomplete"
    with (folder / "DICOMDIR").open("rb") as stream:
        listed = dicomdir.read_entries(stream)
    on_disk = {
        dicom_factory.read_header(p).SOPInstanceUID for p in folder.rglob("IM*") if p.is_file()
    }
    # pydicom's FileSet would have dropped the two records whose file is gone.
    assert len(listed) == 5 and len(on_disk) == 3
    assert on_disk < {sop for sop, _ in listed}


def test_the_reader_records_a_dicomdir_as_its_own_kind(corpus: Any) -> None:
    path = corpus.sources[1] / "dicomdir" / "complete" / "DICOMDIR"
    with read.quiet_pydicom():
        record = read.read_file(os.fsencode(path), identity=None)
    assert record is not None
    assert record.kind == "dicomdir"
    assert len(record.dicomdir) == 5
    # No UIDs of its own: it is no instance, and must never become one.
    assert set(record.values) == {"transfer_syntax_uid"}


def test_the_offset_tree_gives_the_directory_order() -> None:
    records = [
        _record("PATIENT", 100, child=200),
        _record("STUDY", 200, child=300),
        _record("SERIES", 300, following=600, child=400, SeriesInstanceUID="1.1"),
        _image(400, "1.1.1", following=500),
        _image(500, "1.1.2"),
        _record("SERIES", 600, child=700, SeriesInstanceUID="1.2"),
        _image(700, "1.2.1"),
    ]
    # Shuffled: the tree, not the sequence, decides.
    shuffled = [records[i] for i in (6, 2, 0, 4, 1, 5, 3)]
    assert dicomdir.entries(shuffled, 100) == [
        ("1.1.1", "1.1"),
        ("1.1.2", "1.1"),
        ("1.2.1", "1.2"),
    ]


def test_a_loop_in_the_offsets_ends() -> None:
    records = [
        _record("SERIES", 10, child=20, SeriesInstanceUID="1.1"),
        _image(20, "1.1.1", following=30),
        _image(30, "1.1.2", following=20),
    ]
    assert dicomdir.entries(records, 10) == [("1.1.1", "1.1"), ("1.1.2", "1.1")]


def test_without_usable_offsets_the_sequence_order_is_used() -> None:
    records = [
        _record("PATIENT", 0),
        _image(0, "9.9.9"),  # before any series: no series to give it
        _record("SERIES", 0, SeriesInstanceUID="2.1"),
        _image(0, "2.1.1"),
        _image(0, "2.1.1"),  # listed twice, kept once
        _record("STUDY", 0),
        _image(0, "8.8.8"),  # after a new study, before its series
        _record("SERIES", 0, SeriesInstanceUID="2.2"),
        _image(0, "2.2.1"),
    ]
    assert dicomdir.entries(records, None) == [("2.1.1", "2.1"), ("2.2.1", "2.2")]
    assert dicomdir.entries(records, 999) == [("2.1.1", "2.1"), ("2.2.1", "2.2")]


def test_an_image_record_without_a_reference_is_skipped() -> None:
    records = [
        _record("SERIES", 1, child=2, SeriesInstanceUID="3.1"),
        _record("IMAGE", 2, following=3),
        _image(3, "3.1.2"),
    ]
    assert dicomdir.entries(records, 1) == [("3.1.2", "3.1")]


def test_a_damaged_dicomdir_is_unreadable(tmp_path: Path, corpus: Any) -> None:
    data = (corpus.sources[1] / "dicomdir" / "complete" / "DICOMDIR").read_bytes()
    cut = tmp_path / "DICOMDIR"
    cut.write_bytes(data[: len(data) // 2])
    with read.quiet_pydicom():
        record = read.read_file(os.fsencode(cut), identity=None)
    assert record is not None
    assert record.kind in ("dicomdir", "unreadable")
    if record.kind == "unreadable":
        assert record.code == "read.invalid_dicom"
