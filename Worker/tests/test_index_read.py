"""The header read (ADR 0022 decisions 2 to 6): what a file is recognized as,
the values the catalog keeps of it, and what never leaves the read."""

from __future__ import annotations

import errno
import io
import logging
import os
import sqlite3
import struct
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import dicom_factory
from bcoa_worker.index import read
from bcoa_worker.index.catalog import CATALOG_DDL
from bcoa_worker.index.identity import decode_link_key, issuer_link, pid_link
from bcoa_worker.index.read import FileRecord, Identity, read_file
from bcoa_worker.worker import Cancelled
from index_jobs import LINK_KEY

KEY = decode_link_key(LINK_KEY)
PLACEHOLDERS = ("ANONYMOUS", "ANONYMIZED", "UNKNOWN", "0", "-")
IDENTITY = Identity(KEY, PLACEHOLDERS)


def _read(path: Path, identity: Identity | None = IDENTITY) -> FileRecord:
    with read.quiet_pydicom():
        record = read_file(os.fsencode(path), identity=identity)
    assert record is not None
    return record


def _catalog_columns() -> set[str]:
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(CATALOG_DDL)
        return {row[1] for row in db.execute("PRAGMA table_info(files)")}


def _write(
    folder: Path,
    *,
    patient: Any = None,
    count: int = 1,
    vary: Any = None,
    **series: Any,
) -> list[Path]:
    writer = dicom_factory._Writer(folder, "read-tests")
    study = dicom_factory.Study(
        "study",
        patient or dicom_factory.Patient("PID-0001", "F", "050Y"),
        "20240102",
        "CT Read Test",
        "ACC-0001",
    )
    made = dicom_factory.Series(
        "ct", "ct", "Read Test 3.0", 2, dicom_factory.axial(count), vary=vary, **series
    )
    return [path for path, _ in writer.series(1, study, made)]


# ------------------------------------------------------------------ recognition


@pytest.mark.parametrize(
    ("relative", "kind", "code"),
    [
        ("junk/README.txt", "not_dicom", None),
        ("junk/empty.dat", "not_dicom", None),
        ("junk/random.bin", "not_dicom", None),
        ("junk/report.pdf", "not_dicom", None),
        ("junk/short.dcm", "not_dicom", None),
        # Well formed, but without the preamble the plan names as the test.
        ("junk/raw_dataset.dcm", "not_dicom", None),
        ("junk/export.zip", "archive", None),
        ("junk/series.tar", "archive", None),
        ("junk/notes.txt.gz", "archive", None),
        ("junk/backup.7z", "archive", None),
        ("junk/backup.rar", "archive", None),
        # C14: cut inside the file meta, and DICM followed by no dataset.
        ("junk/cut_meta.dcm", "unreadable", "read.invalid_dicom"),
        ("junk/dicm_garbage.dcm", "unreadable", "read.invalid_dicom"),
        # C15: a NIfTI name without a NIfTI header.
        ("nifti/broken.nii.gz", "archive", None),
        ("nifti/garbage.nii", "not_dicom", None),
        ("nifti/CT_N001.nii.gz", "nifti", None),
        ("nifti/CT_N002.nii", "nifti", None),
        ("dicomdir/complete/DICOMDIR", "dicomdir", None),
        ("selection/thin_thick/dose/SR0001.dcm", "non_image", None),
        ("splits/sop_class/SR0005.dcm", "non_image", None),
        ("enhanced/single/EN0001.dcm", "image", None),
    ],
)
def test_a_file_is_recognized_by_its_content(
    corpus: Any, relative: str, kind: str, code: str | None
) -> None:
    record = _read(corpus.sources[1] / relative)
    assert (record.kind, record.code) == (kind, code)
    if kind in ("not_dicom", "archive"):
        assert record.values == {}


def test_a_name_never_decides_alone(tmp_path: Path) -> None:
    # A DICOM file named like NIfTI is DICOM; a NIfTI header without the
    # name is not NIfTI.
    slice_path = _write(tmp_path)[0]
    renamed = slice_path.with_name("CT_X.nii")
    slice_path.rename(renamed)
    assert _read(renamed).kind == "image"
    header = dicom_factory.nifti_bytes(
        (4, 4, 2), (1.0, 1.0, 1.0), dicom_factory._nifti_volume((4, 4, 2))
    )
    (tmp_path / "volume.bin").write_bytes(header)
    assert _read(tmp_path / "volume.bin").kind == "not_dicom"


# ------------------------------------------------------------------ values


def test_a_classic_header_fills_its_columns(corpus: Any) -> None:
    path = corpus.sources[1] / "pixels" / "truncated_native" / "IM0001.dcm"
    record = _read(path)
    header = dicom_factory.read_header(path)
    values = record.values
    assert record.kind == "image"
    assert (record.size, record.mtime_ns) == (path.stat().st_size, path.stat().st_mtime_ns)
    assert values["sop_uid"] == header.SOPInstanceUID
    assert values["study_uid"] == header.StudyInstanceUID
    assert values["series_uid"] == header.SeriesInstanceUID
    assert values["for_uid"] == header.FrameOfReferenceUID
    assert values["sop_class_uid"] == "1.2.840.10008.5.1.4.1.1.2"
    assert values["transfer_syntax_uid"] == "1.2.840.10008.1.2.1"
    # C18: dates as ISO 8601, because the app's age SQL uses julianday.
    assert values["study_date"] == "2024-04-03"
    assert values["series_date"] == "2024-04-03"
    assert (values["sex"], values["age_years"], values["age_source"]) == ("M", 68.0, "age")
    assert values["modality"] == "CT"
    assert values["series_number"] == 2
    assert values["series_description"] == "Truncated Native 3.0"
    assert values["image_type"] == "ORIGINAL\\PRIMARY\\AXIAL"
    assert values["kernel"] == "Br40d"
    assert values["kvp"] == 120.0
    assert values["slice_thickness"] == 3.0
    assert (values["pixel_spacing_row"], values["pixel_spacing_col"]) == (0.7, 0.7)
    assert (values["image_rows"], values["image_columns"]) == (16, 16)
    assert read.decode_numbers(values["iop"]) == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    assert (values["ipp_x"], values["ipp_y"], values["ipp_z"]) == (-5.6, -5.6, 0.0)
    assert (values["rescale_slope"], values["rescale_intercept"]) == (1.0, -1024.0)
    assert (values["window_center"], values["window_width"]) == (40.0, 400.0)
    assert values["pixel_data"] == "ok"
    assert values["pet_json"] is None
    assert values["contrast_sequence"] == 0


def test_every_value_has_a_column(corpus: Any) -> None:
    # The scan writes values by column name and would drop a key the
    # catalog does not have, silently.
    columns = _catalog_columns()
    keys: set[str] = set()
    for relative in (
        "pixels/truncated_native/IM0001.dcm",
        "petct/pet_no_ci/IM0006.dcm",
        "enhanced/mixed/EN0001.dcm",
        "dicomdir/complete/DICOMDIR",
        "selection/thin_thick/dose/SR0001.dcm",
        "nifti/CT_N001.nii.gz",
    ):
        keys |= set(_read(corpus.sources[1] / relative).values)
    assert keys <= columns, keys - columns
    assert "patient_id" in keys and "nifti_json" in keys and "pet_json" in keys


def test_a_pet_header_keeps_presence_only(corpus: Any) -> None:
    import json

    folder = corpus.sources[1] / "petct"
    pet = next(p for p in sorted(folder.rglob("*.dcm")) if _read(p).values["modality"] == "PT")
    facts = json.loads(_read(pet).values["pet_json"])
    assert set(facts) == {
        "units",
        "decay",
        "attenuation_corrected",
        "has_dose",
        "has_start_time",
        "has_weight",
    }
    assert all(isinstance(facts[k], bool) for k in ("has_dose", "has_start_time", "has_weight"))


def test_a_malformed_value_costs_that_value_only(tmp_path: Path) -> None:
    def odd(index: int, ds: Any) -> None:
        ds.KVP = "77.25"

    path = _write(tmp_path, vary=odd)[0]
    path.write_bytes(path.read_bytes().replace(b"77.25", b"ab.cd"))
    record = _read(path)
    assert record.kind == "image"
    assert record.values["kvp"] is None
    assert record.values["slice_thickness"] == 3.0


def test_validation_says_nothing_about_values(
    tmp_path: Path, recwarn: pytest.WarningsRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    # pydicom's validation warnings quote raw values; a value can be a name.
    path = _write(tmp_path)[0]
    data = path.read_bytes()
    header = dicom_factory.read_header(path)
    good = header.FrameOfReferenceUID.encode()
    bad = (b"1.2.CANARY^NAME." + b"9" * len(good))[: len(good)]
    path.write_bytes(data.replace(good, bad))
    caplog.set_level(logging.DEBUG)
    record = _read(path)
    assert record.values["for_uid"] == bad.decode()
    assert not [w for w in recwarn if "CANARY" in str(w.message)]
    assert not [r for r in caplog.records if r.name.startswith("pydicom")]


# ------------------------------------------------------------------ identity


def test_identifiers_are_linked_with_a_key(corpus: Any) -> None:
    values = _read(corpus.sources[1] / "pixels" / "truncated_native" / "IM0001.dcm").values
    assert values["pid_state"] == "present"
    assert values["pid_link"] == pid_link(KEY, "PIX-0001")
    assert values["patient_id"] == "PIX-0001"
    assert values["accession_number"] == "PIX0001A"
    assert values["issuer_link"] is None


def test_an_issuer_is_kept_as_a_link(tmp_path: Path) -> None:
    def issuer(index: int, ds: Any) -> None:
        ds.IssuerOfPatientID = "Hospital A"

    values = _read(_write(tmp_path, vary=issuer)[0]).values
    assert values["issuer_link"] == issuer_link(KEY, "Hospital A")
    assert "Hospital A" not in values.values()


@pytest.mark.parametrize(
    ("relative", "state"),
    [
        ("CASE_017/2019/CT/IM0001.dcm", "placeholder"),
        ("CASE_019/CT/IM0001.dcm", "placeholder"),
        ("CASE_018/CT/IM0001.dcm", "missing"),
        ("CASE_020/CT/IM0001.dcm", "missing"),
    ],
)
def test_a_placeholder_or_missing_id_is_no_identifier(
    corpus: Any, relative: str, state: str
) -> None:
    values = _read(corpus.sources[1] / relative).values
    assert values["pid_state"] == state
    assert values["pid_link"] is None
    assert values["patient_id"] is None


def test_without_a_key_no_identifier_is_requested_or_kept(
    corpus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[set[int]] = []
    real = read.dcmread

    def spy(*args: Any, **kwargs: Any) -> Any:
        requested.append(set(kwargs.get("specific_tags") or ()))
        return real(*args, **kwargs)

    monkeypatch.setattr(read, "dcmread", spy)
    canary = corpus.sources[1] / "CANARY_FOLDER" / "CT" / "IM0001.dcm"
    values = _read(canary, identity=None).values
    identifier_tags = {0x00100020, 0x00100021, 0x00080050}
    assert requested and not any(tags & identifier_tags for tags in requested)
    assert values["pid_state"] == "withheld"
    for column in ("pid_link", "patient_id", "issuer_link", "accession_number"):
        assert values.get(column) is None
    # With a key, the same file asks for them.
    requested.clear()
    _read(canary)
    assert identifier_tags <= requested[0]


def test_the_birth_date_gives_an_age_and_is_never_kept(corpus: Any) -> None:
    values = _read(corpus.sources[1] / "CANARY_FOLDER" / "CT" / "IM0001.dcm").values
    assert (values["age_years"], values["age_source"]) == (123.0, "birth_date")
    text = repr(values)
    for canary in ("19010101", "1901-01-01", "CANARY^NAME"):
        assert canary not in text


def test_the_requested_tags_name_no_name() -> None:
    every = set(read.header_tags(True))
    assert 0x00100010 not in every  # PatientName
    assert 0x00101000 not in every  # OtherPatientIDs
    assert 0x00101002 not in every  # OtherPatientIDsSequence
    assert set(read.header_tags(False)) == every - set(read.IDENTIFIER_TAGS)


# ------------------------------------------------------------------ pixel data


@pytest.mark.parametrize(
    ("relative", "state"),
    [
        ("pixels/truncated_native/IM0025.dcm", "truncated"),
        ("pixels/truncated_native/IM0024.dcm", "ok"),
        ("pixels/truncated_encapsulated/IM0010.dcm", "truncated"),
        ("pixels/truncated_encapsulated/IM0009.dcm", "ok"),
        ("pixels/missing_pixels/IM0030.dcm", "missing"),
        ("syntaxes/implicit_le/IM0001.dcm", "ok"),
        ("syntaxes/explicit_be/IM0001.dcm", "ok"),
        ("syntaxes/deflated/IM0001.dcm", "ok"),
        ("syntaxes/rle/IM0001.dcm", "ok"),
        ("syntaxes/j2k/IM0001.dcm", "ok"),
        ("syntaxes/private/IM0001.dcm", "ok"),
        ("enhanced/single/EN0001.dcm", "ok"),
        ("selection/thin_thick/dose/SR0001.dcm", None),
    ],
)
def test_pixel_data_is_judged_without_reading_it(
    corpus: Any, relative: str, state: str | None
) -> None:
    assert _read(corpus.sources[1] / relative).values["pixel_data"] == state


def _element(tag: tuple[int, int], vr: bytes | None, length: int, order: str = "<") -> bytes:
    head = struct.pack(order + "HH", *tag)
    if vr is None:
        return head + struct.pack("<I", length)
    return head + vr + b"\x00\x00" + struct.pack(order + "I", length)


@pytest.mark.parametrize(
    ("element", "size", "syntax", "image", "state"),
    [
        (_element((0x7FE0, 0x0010), b"OW", 512), 12 + 512, "1.2.840.10008.1.2.1", True, "ok"),
        (
            _element((0x7FE0, 0x0010), b"OW", 512),
            12 + 511,
            "1.2.840.10008.1.2.1",
            True,
            "truncated",
        ),
        (_element((0x7FE0, 0x0010), None, 512), 8 + 512, "1.2.840.10008.1.2", True, "ok"),
        (_element((0x7FE0, 0x0010), None, 512), 8 + 100, "1.2.840.10008.1.2", True, "truncated"),
        (
            _element((0x7FE0, 0x0010), b"OW", 64, ">"),
            12 + 64,
            "1.2.840.10008.1.2.2",
            True,
            "ok",
        ),
        (_element((0x7FE0, 0x0010), b"OW", 0), 12, "1.2.840.10008.1.2.1", True, "missing"),
        (_element((0x7FE0, 0x0010), b"OW", 0), 12, "1.2.840.10008.1.2.1", False, None),
        (_element((0x7FE0, 0x0008), b"OF", 16), 12 + 16, "1.2.840.10008.1.2.1", True, "ok"),
        (_element((0x0009, 0x0010), b"OB", 16), 12 + 16, "1.2.840.10008.1.2.1", True, "missing"),
        (b"\xe0\x7f", 2, "1.2.840.10008.1.2.1", True, "missing"),
    ],
)
def test_the_element_after_the_header_decides(
    element: bytes, size: int, syntax: str, image: bool, state: str | None
) -> None:
    stream = io.BytesIO(element + bytes(max(size - len(element), 0)))
    assert read.pixel_status(stream, 0, size, syntax, image) == state


JPEG_LS = "1.2.840.10008.1.2.4.80"
_DELIMITER = b"\xfe\xff\xdd\xe0\x00\x00\x00\x00"


def _item(value: bytes, length: int | None = None) -> bytes:
    return b"\xfe\xff\x00\xe0" + struct.pack("<I", len(value) if length is None else length) + value


def _encapsulated(*fragments: bytes) -> bytes:
    """An encapsulated pixel element: an empty offset table, the fragments and
    the delimiter."""
    start = _element((0x7FE0, 0x0010), b"OB", 0xFFFFFFFF)
    return start + _item(b"") + b"".join(_item(f) for f in fragments) + _DELIMITER


def test_an_encapsulated_element_must_end_with_the_delimiter() -> None:
    whole = _encapsulated(b"\x01\x02\x03\x04")
    assert read.pixel_status(io.BytesIO(whole), 0, len(whole), JPEG_LS, True) == "ok"
    cut = whole[: -len(_DELIMITER)]
    assert read.pixel_status(io.BytesIO(cut), 0, len(cut), JPEG_LS, True) == "truncated"


# Data Set Trailing Padding, and an empty Digital Signatures Sequence: both
# may follow the pixel data, so a complete file need not end with the
# delimiter (ADR 0029).
_PADDING = struct.pack("<HH", 0xFFFC, 0xFFFC) + b"OB\x00\x00" + struct.pack("<I", 64) + bytes(64)
_SIGNATURES = struct.pack("<HH", 0xFFFA, 0xFFFA) + b"SQ\x00\x00" + struct.pack("<I", 0)


@pytest.mark.parametrize("after", [b"", _PADDING, _SIGNATURES, _SIGNATURES + _PADDING])
def test_what_follows_an_encapsulated_element_does_not_count(after: bytes, tmp_path: Path) -> None:
    whole = _encapsulated(b"\x01\x02\x03\x04", b"\x05\x06") + after
    element = read.pixel_element(io.BytesIO(whole), 0, len(whole), JPEG_LS, True)
    assert (element.status, element.fragments) == ("ok", 2)
    # The same through a descriptor, which reads the item headers with pread.
    path = tmp_path / "element.bin"
    path.write_bytes(whole)
    with path.open("rb") as stream:
        element = read.pixel_element(stream, 0, len(whole), JPEG_LS, True)
    assert (element.status, element.fragments) == ("ok", 2)


@pytest.mark.parametrize(
    ("body", "state"),
    [
        # A fragment that runs past the end of the file.
        (_item(b"") + _item(b"\x01\x02", length=4096) + _DELIMITER, "truncated"),
        # Something other than an item where the next item belongs.
        (_item(b"") + _item(b"\x01\x02") + _PADDING + _DELIMITER, "truncated"),
        # An item without a defined length.
        (_item(b"") + _item(b"", length=0xFFFFFFFF) + _DELIMITER, "truncated"),
        # An offset table and no fragment: no image.
        (_item(b"") + _DELIMITER, "missing"),
    ],
)
def test_an_encapsulated_element_is_followed_item_by_item(body: bytes, state: str) -> None:
    whole = _element((0x7FE0, 0x0010), b"OB", 0xFFFFFFFF) + body + _PADDING
    assert read.pixel_status(io.BytesIO(whole), 0, len(whole), JPEG_LS, True) == state


def test_a_deflated_pixel_element_is_judged_by_its_length(
    corpus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = read.dcmread
    seen: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        ds = real(*args, **kwargs)
        if kwargs.get("specific_tags") == read._PIXEL_TAGS:
            seen.append(ds.get_item(0x7FE00010, keep_deferred=True))
        return ds

    monkeypatch.setattr(read, "dcmread", spy)
    record = _read(corpus.sources[1] / "syntaxes" / "deflated" / "IM0001.dcm")
    assert record.values["pixel_data"] == "ok"
    # Deferred and never loaded: a 300 KB file of 300 MB of zeros took 955 MB
    # when the value was loaded beside pydicom's own inflated copy.
    assert len(seen) == 1 and seen[0].value is None and seen[0].length > 0


# ------------------------------------------------------------------ frame counts


def test_the_frames_a_pixel_element_can_hold() -> None:
    values = {"image_rows": 16, "image_columns": 16, "bits_allocated": 16}
    assert read.frame_capacity(read.PixelElement("ok", length=3 * 512), values) == 3
    assert read.frame_capacity(read.PixelElement("ok", length=3 * 512 + 1), values) == 3
    assert read.frame_capacity(read.PixelElement("ok", fragments=7), values) == 7
    # Truncated or without dimensions: only the ceiling applies.
    assert read.frame_capacity(read.PixelElement("truncated"), values) == read.FRAME_CEILING
    assert read.frame_capacity(read.PixelElement("ok", length=512), {}) == read.FRAME_CEILING
    tiny = {"image_rows": 1, "image_columns": 1, "bits_allocated": 1}
    assert read.frame_capacity(read.PixelElement("ok", length=2**31), tiny) == read.FRAME_CEILING


@pytest.mark.parametrize(
    "sop_class",
    [
        "1.2.840.10008.5.1.4.1.1.2",  # CT: every frame becomes an instance
        "1.2.840.10008.5.1.4.1.1.2.1",  # Enhanced CT without per-frame groups
    ],
)
def test_a_frame_count_the_pixels_cannot_hold_is_invalid(tmp_path: Path, sop_class: str) -> None:
    def claim(index: int, ds: Any) -> None:
        ds.SOPClassUID = sop_class
        ds.NumberOfFrames = 1_000_000

    record = _read(_write(tmp_path, vary=claim)[0])
    assert (record.kind, record.code) == ("unreadable", "read.invalid_dicom")
    assert record.frames == []


def test_a_frame_count_the_pixels_hold_is_kept(tmp_path: Path) -> None:
    def two(index: int, ds: Any) -> None:
        # The factory writes 16 × 16 pixels of 16 bits: 512 bytes, room for
        # two frames of 8 bits.
        ds.NumberOfFrames = 2
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7

    record = _read(_write(tmp_path, vary=two)[0])
    assert (record.kind, record.values["frames"]) == ("image", 2)


# ------------------------------------------------------------------ multi-frame


def test_an_enhanced_file_gets_its_frames_and_shared_values(corpus: Any) -> None:
    record = _read(corpus.sources[1] / "enhanced" / "single" / "EN0001.dcm")
    values = record.values
    assert values["frames"] == 50
    assert [frame.frame for frame in record.frames] == list(range(1, 51))
    assert {frame.stack_id for frame in record.frames} == {"1"}
    assert record.frames[1].position == (-5.6, -5.6, 3.0)
    assert read.decode_numbers(record.frames[0].orientation) == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    # C12: thickness, rescale, kernel and window from the functional groups.
    assert values["slice_thickness"] == 3.0
    assert (values["rescale_slope"], values["rescale_intercept"]) == (1.0, -1024.0)
    assert values["kernel"] == "Br40d"
    assert (values["window_center"], values["window_width"]) == (40.0, 400.0)
    assert (values["pixel_spacing_row"], values["pixel_spacing_col"]) == (0.7, 0.7)
    assert (values["ipp_x"], values["ipp_y"], values["ipp_z"]) == (-5.6, -5.6, 0.0)


def test_frames_of_other_orientation_and_stacks_are_kept_apart(corpus: Any) -> None:
    mixed = _read(corpus.sources[1] / "enhanced" / "mixed" / "EN0001.dcm")
    orientations = {frame.orientation for frame in mixed.frames}
    assert len(mixed.frames) == 52 and len(orientations) == 2
    stacks = [_read(p) for p in sorted((corpus.sources[1] / "enhanced" / "stacks").iterdir())]
    assert [{frame.stack_id for frame in record.frames} for record in stacks] == [{"1"}, {"2"}]


# ------------------------------------------------------------------ failures


def test_a_file_that_changes_while_it_is_read_is_changing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)[0]
    real = os.fstat
    calls = {"n": 0}

    def moving(fd: int) -> os.stat_result:
        calls["n"] += 1
        info = real(fd)
        if calls["n"] == 1:
            return info
        fields = list(info)
        fields[6] += 1  # st_size
        return os.stat_result(fields)

    monkeypatch.setattr(read.os, "fstat", moving)
    record = _read(path)
    assert record.kind == "changing"
    assert record.values == {} and record.code is None


def test_a_file_replaced_since_the_walk_is_never_followed_or_waited_on(tmp_path: Path) -> None:
    path = _write(tmp_path / "source")[0]
    outside = tmp_path / "outside.dcm"
    path.rename(outside)
    os.symlink(outside, path)
    assert _read(path).kind == "changing"
    path.unlink()
    path.mkdir()
    assert _read(path).kind == "changing"
    path.rmdir()
    os.mkfifo(path)
    found: list[FileRecord] = []
    # A plain open() of a FIFO waits for a writer forever; a daemon thread
    # keeps a regression from hanging the suite.
    thread = threading.Thread(target=lambda: found.append(_read(path)), daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert [record.kind for record in found] == ["changing"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            PermissionError(errno.EACCES, "Permission denied: '/Volumes/CANARY^NAME/x.dcm'"),
            "read.permission_denied",
        ),
        (OSError(errno.EPERM, "Operation not permitted"), "read.permission_denied"),
        (OSError(errno.EIO, "Input/output error: '/Volumes/CANARY^NAME'"), "read.io_error"),
    ],
)
def test_a_file_that_cannot_be_opened_is_unreadable_with_a_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, code: str
) -> None:
    path = _write(tmp_path)[0]

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(read, "_open_file", refuse)
    record = _read(path)
    assert (record.kind, record.code) == ("unreadable", code)
    assert record.size == path.stat().st_size
    assert "CANARY" not in repr(record)


def test_a_failure_inside_the_read_is_unreadable_with_a_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)[0]

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.EIO, "read failed at /Volumes/CANARY^NAME")

    monkeypatch.setattr(read, "_read_open", fail)
    record = _read(path)
    assert (record.kind, record.code) == ("unreadable", "read.io_error")
    assert "CANARY" not in repr(record)


def test_a_file_gone_since_the_walk_gets_no_record(tmp_path: Path) -> None:
    assert read_file(os.fsencode(tmp_path / "gone.dcm"), identity=IDENTITY) is None
    assert read_file(os.fsencode(tmp_path / "gone" / "x.dcm"), identity=IDENTITY) is None


def test_a_cancel_inside_the_read_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)[0]

    def cancel(*args: Any, **kwargs: Any) -> Any:
        raise Cancelled

    monkeypatch.setattr(read, "_read_open", cancel)
    with pytest.raises(Cancelled):
        read_file(os.fsencode(path), identity=IDENTITY)
    monkeypatch.undo()
    monkeypatch.setattr(read, "header_values", cancel)
    with pytest.raises(Cancelled):
        read_file(os.fsencode(path), identity=IDENTITY)


def test_a_dicom_file_without_its_uids_is_invalid(tmp_path: Path) -> None:
    def no_series(index: int, ds: Any) -> None:
        del ds.SeriesInstanceUID

    record = _read(_write(tmp_path, vary=no_series)[0])
    assert (record.kind, record.code) == ("unreadable", "read.invalid_dicom")


def test_quiet_pydicom_restores_what_it_changed() -> None:
    from pydicom import config

    logger = logging.getLogger("pydicom")
    before = (config.settings.reading_validation_mode, logger.level)
    with read.quiet_pydicom():
        assert config.settings.reading_validation_mode == config.IGNORE
    assert (config.settings.reading_validation_mode, logger.level) == before
