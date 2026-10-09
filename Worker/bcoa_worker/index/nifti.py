"""NIfTI files in a source folder (ADR 0022 decision 13).

A NIfTI file has no UIDs, no patient and no study, so it gets what it needs
from its name and its content: `CT_<id>.nii[.gz]` and `PT_<id>.nii[.gz]` name
the modality and the patient, and the UIDs are hashes of the file's bytes, so
the file keeps its study across renames and moves. A UID made from the folder
name could be brute-forced back to that name, which often is a patient's.

Any other name gets modality OT and is never selected automatically: a label
mask lying next to a CT must not be chosen as one.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import unicodedata
import zlib
from typing import IO, Any

from bcoa_worker.index.identity import normalize_id, patient_link, pid_link

HEADER_BYTES = 348
_MAGICS = (b"n+1\x00", b"ni1\x00")
_PREFIXES = {"CT_": "CT", "PT_": "PT"}
_EXTENSIONS = (".nii.gz", ".nii")
# Enough input for the 348-byte header of any sane gzip stream; a file that
# needs more is not what its name says.
_GZIP_HEADER_INPUT = 64 * 1024
_HASH_CHUNK = 1024 * 1024


class Unsupported(Exception):
    """A NIfTI file this reader cannot hand to SimpleITK (read.unsupported).
    Carries no message: the reason is the code."""


def nfc_name(name: bytes) -> str:
    """A file name for matching: decoded as the file system does, then NFC."""
    return unicodedata.normalize("NFC", os.fsdecode(name))


def has_nifti_name(name: str) -> bool:
    """`.nii` or `.nii.gz`, in any case (ADR 0022 decision 2)."""
    return name.lower().endswith(_EXTENSIONS)


def header_ok(header: bytes) -> bool:
    """A NIfTI-1 header: `sizeof_hdr` 348 in either byte order and the magic
    `n+1` (one file) or `ni1` (header and image apart)."""
    if len(header) < HEADER_BYTES:
        return False
    size = header[:4]
    if struct.unpack("<i", size)[0] != HEADER_BYTES and struct.unpack(">i", size)[0] != (
        HEADER_BYTES
    ):
        return False
    return header[344:348] in _MAGICS


def gzip_header(stream: IO[bytes]) -> bytes:
    """The first 348 bytes inside a gzip stream, read from the stream's start,
    or fewer when it holds fewer or is not gzip at all."""
    stream.seek(0)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = b""
    consumed = 0
    while len(out) < HEADER_BYTES and consumed < _GZIP_HEADER_INPUT:
        chunk = stream.read(4096)
        if not chunk:
            break
        consumed += len(chunk)
        try:
            out += inflater.decompress(chunk, HEADER_BYTES - len(out))
        except zlib.error:
            break
    return out


def parse_name(name: str) -> tuple[str, str | None]:
    """(modality, the <id> of the name) for `CT_<id>` and `PT_<id>`, else
    ("OT", None). The prefix is matched with its case, the extension without:
    "ct_lower.nii.gz" is unnamed, "CT_N003.NII.GZ" is the CT of N003."""
    lowered = name.lower()
    stem = next(
        (name[: -len(ext)] for ext in _EXTENSIONS if lowered.endswith(ext)),
        name,
    )
    for prefix, modality in _PREFIXES.items():
        if stem.startswith(prefix) and normalize_id(stem[len(prefix) :]):
            return modality, stem[len(prefix) :]
    return "OT", None


def synthetic_uid(role: str, content_sha256: str) -> str:
    """`"2.25." + int(sha256("bcoa.nifti.<role>" ␟ content_sha256)[:15])`, the
    first 15 hex digits of the hash as an integer (60 bits): a UID of at most
    24 characters that the same bytes always give."""
    text = f"bcoa.nifti.{role}\x1f{content_sha256}".encode("ascii")
    return "2.25." + str(int(hashlib.sha256(text).hexdigest()[:15], 16))


def content_sha256(stream: IO[bytes]) -> str:
    """The sha256 of the whole file, read in 1 MiB pieces so that a large
    volume never sits in memory."""
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(_HASH_CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


_sitk_quiet = False


def _reader(path: bytes) -> Any:
    global _sitk_quiet
    import SimpleITK as sitk

    if not _sitk_quiet:
        # ITK's warnings name the file, and folder names often carry patient
        # names; the job log must hold counts and codes only.
        sitk.ProcessObject_SetGlobalWarningDisplay(False)
        _sitk_quiet = True
    text = os.fsdecode(path)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # A name that is not valid UTF-8 reaches SimpleITK's C++ side as a null
        # string, which aborts the whole process (measured with SimpleITK 2.5.6
        # on Linux). APFS refuses such names, so this happens only on other
        # volumes, and only the file is lost, not the job.
        raise Unsupported from None
    reader = sitk.ImageFileReader()
    reader.SetFileName(text)
    # The file passed `header_ok`, so only the NIfTI IO is asked. Left to the
    # factory, every registered IO probes the file, and HDF5's prints its
    # error stack with the full path to fd 2, the job log, when the file
    # fails between our open and SimpleITK's (measured: 2 100 bytes on an
    # EIO, none with the NIfTI IO set).
    reader.SetImageIO("NiftiImageIO")
    try:
        reader.ReadImageInformation()
    except RuntimeError:
        # The message quotes the path.
        raise Unsupported from None
    return reader


def _int_meta(reader: Any, key: str) -> int | None:
    try:
        return int(reader.GetMetaData(key)) if reader.HasMetaDataKey(key) else None
    except (RuntimeError, ValueError):
        return None


def _finite(values: Any) -> list[float] | None:
    numbers = [float(v) for v in values]
    return numbers if all(math.isfinite(v) for v in numbers) else None


def read_nifti(
    path: bytes,
    stream: IO[bytes],
    *,
    key: bytes | None,
    placeholders: tuple[str, ...],
    cached_sha256: str | None = None,
) -> dict[str, Any]:
    """The `files` columns of a NIfTI file whose header `header_ok` accepted.

    The header comes from SimpleITK's ReadImageInformation, which reads no
    voxels. `cached_sha256` is the content hash of the previous read when the
    file's size and modification time are unchanged, so a re-read for a new
    reader version does not hash a large volume again.
    """
    reader = _reader(path)
    sha = cached_sha256 or content_sha256(stream)
    modality, named_id = parse_name(nfc_name(path.rsplit(b"/", 1)[-1]))
    size = list(reader.GetSize())
    dimension = reader.GetDimension()
    spacing = _finite(reader.GetSpacing())
    origin = _finite(reader.GetOrigin())
    direction = _finite(reader.GetDirection())
    values: dict[str, Any] = {
        "modality": modality,
        "study_uid": synthetic_uid("study", sha),
        "series_uid": synthetic_uid("series", sha),
        "sop_uid": synthetic_uid("instance", sha),
        "image_columns": size[0] if size else None,
        "image_rows": size[1] if dimension >= 2 else None,
    }
    if spacing is not None and dimension >= 2:
        # DICOM's PixelSpacing is row spacing (along y) first.
        values["pixel_spacing_row"] = spacing[1]
        values["pixel_spacing_col"] = spacing[0]
        if dimension >= 3:
            values["spacing_between_slices"] = spacing[2]
    if direction is not None and dimension >= 3:
        # SimpleITK gives the direction in LPS, as DICOM has it, as a row-major
        # matrix whose columns are the image axes: column 0 runs along a row,
        # column 1 down a column, which is ImageOrientationPatient.
        cosines = [direction[i * dimension + j] for j in (0, 1) for i in range(3)]
        values["iop"] = "\\".join(repr(v) for v in cosines)
    if origin is not None and len(origin) >= 3:
        values["ipp_x"], values["ipp_y"], values["ipp_z"] = origin[:3]
    values["nifti_json"] = json.dumps(
        {
            "dims": size,
            "spacing": spacing,
            "origin": origin,
            "direction": direction,
            "components": reader.GetNumberOfComponents(),
            "datatype": _int_meta(reader, "datatype"),
            "qform_code": _int_meta(reader, "qform_code"),
            "sform_code": _int_meta(reader, "sform_code"),
            "content_sha256": sha,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    values.update(_identity(named_id, key, placeholders))
    return values


def _identity(
    named_id: str | None, key: bytes | None, placeholders: tuple[str, ...]
) -> dict[str, Any]:
    if key is None:
        # After Remove Identifiers no link is made, also not from a file name.
        return {"pid_state": "withheld"}
    if named_id is None:
        return {"pid_state": "missing"}
    link = patient_link(key, named_id, placeholders)
    if link.state != "present":
        # A placeholder is no identifier, and must not become one in the
        # project's identifiers table.
        return {"pid_state": link.state}
    return {
        "pid_state": "file",
        "pid_link": pid_link(key, link.normalized),
        "patient_id": link.normalized,
    }


def cached_sha256(nifti_json: str | None) -> str | None:
    """The content hash a previous read stored, if any."""
    if not nifti_json:
        return None
    try:
        value = json.loads(nifti_json).get("content_sha256")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and len(value) == 64 else None
