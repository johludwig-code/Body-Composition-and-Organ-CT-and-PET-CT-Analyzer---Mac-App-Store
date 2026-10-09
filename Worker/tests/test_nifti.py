"""NIfTI files in a source folder (ADR 0022 decision 13, ADR 0024): the name
gives modality and patient, the content gives the UIDs."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import struct
from pathlib import Path
from typing import Any

import pytest

import dicom_factory
from bcoa_worker.index import nifti, read
from bcoa_worker.index.identity import decode_link_key, pid_link
from index_jobs import LINK_KEY

KEY = decode_link_key(LINK_KEY)
IDENTITY = read.Identity(KEY, ("ANONYMOUS", "UNKNOWN"))


def _volume(
    path: Path, shape: tuple[int, ...] = (8, 6, 4), spacing: tuple[float, ...] = (0.5, 0.75, 2.0)
) -> Path:
    data = dicom_factory.nifti_bytes(shape, spacing, dicom_factory._nifti_volume(shape))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(data, mtime=0) if path.name.lower().endswith(".gz") else data)
    return path


def _read(path: Path, identity: read.Identity | None = IDENTITY, **options: Any) -> read.FileRecord:
    with read.quiet_pydicom():
        record = read.read_file(os.fsencode(path), identity=identity, **options)
    assert record is not None
    return record


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CT_N001.nii.gz", ("CT", "N001")),
        ("PT_N001.nii.gz", ("PT", "N001")),
        ("CT_N002.nii", ("CT", "N002")),
        # The extension in any case, the prefix with its case.
        ("CT_N003.NII.GZ", ("CT", "N003")),
        ("ct_lower.nii.gz", ("OT", None)),
        ("seg_mask.nii.gz", ("OT", None)),
        ("CT_.nii.gz", ("OT", None)),
        ("CT_ .nii", ("OT", None)),
        ("MR_N001.nii.gz", ("OT", None)),
    ],
)
def test_the_name_gives_modality_and_patient(name: str, expected: tuple[str, str | None]) -> None:
    assert nifti.parse_name(name) == expected


def test_a_name_is_matched_in_nfc() -> None:
    decomposed = "CT_Müller.nii.gz".encode()
    assert nifti.parse_name(nifti.nfc_name(decomposed)) == ("CT", "Müller")


def test_synthetic_uids_follow_the_formula() -> None:
    content = hashlib.sha256(b"volume").hexdigest()
    digest = hashlib.sha256(f"bcoa.nifti.study\x1f{content}".encode()).hexdigest()
    assert nifti.synthetic_uid("study", content) == "2.25." + str(int(digest[:15], 16))
    uids = {nifti.synthetic_uid(role, content) for role in ("study", "series", "instance")}
    assert len(uids) == 3
    assert all(len(uid) <= 64 and uid.startswith("2.25.") for uid in uids)


def test_the_header_check_takes_both_byte_orders_and_both_magics() -> None:
    header = bytearray(348)
    header[:4] = struct.pack("<i", 348)
    header[344:348] = b"n+1\x00"
    assert nifti.header_ok(bytes(header))
    header[:4] = struct.pack(">i", 348)
    header[344:348] = b"ni1\x00"
    assert nifti.header_ok(bytes(header))
    header[344:348] = b"n+2\x00"
    assert not nifti.header_ok(bytes(header))
    assert not nifti.header_ok(bytes(header[:347]))


def test_the_header_inside_gzip_is_read_without_inflating_the_rest() -> None:
    data = dicom_factory.nifti_bytes(
        (8, 8, 4), (1.0, 1.0, 1.0), dicom_factory._nifti_volume((8, 8, 4))
    )
    stream = io.BytesIO(gzip.compress(data))
    assert nifti.gzip_header(stream) == data[:348]
    assert nifti.gzip_header(io.BytesIO(b"\x1f\x8b not really gzip")) == b""


def test_a_named_volume_is_read_from_its_header(corpus: Any) -> None:
    path = corpus.sources[1] / "nifti" / "CT_N001.nii.gz"
    record = _read(path)
    values = record.values
    content = hashlib.sha256(path.read_bytes()).hexdigest()
    assert record.kind == "nifti"
    assert values["modality"] == "CT"
    assert values["pid_state"] == "file"
    assert values["patient_id"] == "N001"
    assert values["pid_link"] == pid_link(KEY, "N001")
    assert values["study_uid"] == nifti.synthetic_uid("study", content)
    assert values["series_uid"] == nifti.synthetic_uid("series", content)
    assert values["sop_uid"] == nifti.synthetic_uid("instance", content)
    assert (values["image_columns"], values["image_rows"]) == (16, 16)
    assert (values["pixel_spacing_row"], values["pixel_spacing_col"]) == (0.75, 0.75)
    assert values["spacing_between_slices"] == 2.5
    facts = json.loads(values["nifti_json"])
    assert facts["dims"] == [16, 16, 60]
    assert facts["content_sha256"] == content
    assert len(read.decode_numbers(values["iop"]) or ()) == 6


def test_spacing_and_axes_keep_their_order(tmp_path: Path) -> None:
    values = _read(_volume(tmp_path / "CT_AXES.nii", (8, 6, 4), (0.5, 0.75, 2.0))).values
    # DICOM's PixelSpacing is the row spacing (along y) first.
    assert (values["image_columns"], values["image_rows"]) == (8, 6)
    assert (values["pixel_spacing_row"], values["pixel_spacing_col"]) == (0.75, 0.5)
    assert values["spacing_between_slices"] == 2.0


def test_a_four_dimensional_volume_is_read(corpus: Any) -> None:
    values = _read(corpus.sources[1] / "nifti" / "CT_N002.nii").values
    assert json.loads(values["nifti_json"])["dims"] == [18, 18, 60, 2]
    assert values["patient_id"] == "N002"


def test_an_unnamed_volume_is_ot_without_a_patient(corpus: Any) -> None:
    for name in ("ct_lower.nii.gz", "seg_mask.nii.gz"):
        values = _read(corpus.sources[1] / "nifti" / name).values
        assert (values["modality"], values["pid_state"]) == ("OT", "missing")
        assert values.get("patient_id") is None and values.get("pid_link") is None


def test_a_placeholder_name_is_no_identifier(tmp_path: Path) -> None:
    values = _read(_volume(tmp_path / "CT_ANONYMOUS.nii.gz")).values
    assert values["modality"] == "CT"
    assert values["pid_state"] == "placeholder"
    assert values.get("patient_id") is None and values.get("pid_link") is None


def test_without_a_key_no_link_is_made_from_the_name(corpus: Any) -> None:
    values = _read(corpus.sources[1] / "nifti" / "CT_N001.nii.gz", identity=None).values
    assert values["pid_state"] == "withheld"
    assert values.get("patient_id") is None and values.get("pid_link") is None
    assert values["modality"] == "CT"


def test_a_renamed_or_moved_file_keeps_its_uids(tmp_path: Path, corpus: Any) -> None:
    original = corpus.sources[1] / "nifti" / "PT_N001.nii.gz"
    moved = tmp_path / "elsewhere" / "PT_RENAMED.nii.gz"
    moved.parent.mkdir()
    shutil.copyfile(original, moved)
    before, after = _read(original).values, _read(moved).values
    for column in ("study_uid", "series_uid", "sop_uid"):
        assert before[column] == after[column]
    assert after["patient_id"] == "RENAMED"


def test_the_content_hash_is_reused_while_size_and_time_hold(tmp_path: Path) -> None:
    path = _volume(tmp_path / "CT_CACHE.nii.gz")
    info = path.stat()
    planted = json.dumps({"content_sha256": "a" * 64})
    cached = _read(path, previous_nifti=(info.st_size, info.st_mtime_ns, planted)).values
    assert json.loads(cached["nifti_json"])["content_sha256"] == "a" * 64
    moved = _read(path, previous_nifti=(info.st_size, info.st_mtime_ns + 1, planted)).values
    assert (
        json.loads(moved["nifti_json"])["content_sha256"]
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )


def test_a_name_simpleitk_cannot_take_costs_the_file_not_the_process(tmp_path: Path) -> None:
    name = b"CT_Bild_\xe9.nii"
    try:
        target = Path(os.fsdecode(os.path.join(os.fsencode(tmp_path), name)))
        _volume(target)
    except (OSError, UnicodeError):
        pytest.skip("this file system refuses names that are not UTF-8")
    # SimpleITK 2.5.6 aborts the whole process on such a path.
    record = _read(target)
    assert (record.kind, record.code) == ("unreadable", "read.unsupported")


def test_a_header_simpleitk_refuses_is_unsupported(tmp_path: Path) -> None:
    path = _volume(tmp_path / "CT_ODD.nii")
    data = bytearray(path.read_bytes())
    data[40:42] = struct.pack("<h", 9)  # dim[0] = 9 dimensions, beyond NIfTI-1's 7
    path.write_bytes(bytes(data))
    record = _read(path)
    assert (record.kind, record.code) == ("unreadable", "read.unsupported")
