"""The header read: what one file is, and the values the catalog keeps of it
(ADR 0022 decisions 2 to 6).

A file is recognized by its content, never by its name alone: bytes 128-131
`DICM` make it DICOM, a NIfTI-1 header under a `.nii` or `.nii.gz` name makes
it NIfTI, the signatures of ZIP, gzip, tar, 7z and RAR make it an archive
(counted, never opened), and anything else is `not_dicom`, raw datasets
without a preamble included, because the plan names the preamble as the
test. `dcmread(force=True)` returned a dataset for an empty file, a ZIP, a
NIfTI file and random bytes alike, so it is never used.

A DICOM file is read once with `stop_before_pixels` and only the tags below,
through a 64 KB buffer: 62 s per 100 000 files warm, 80 s cold, 100-111 s with
GE's large private headers (Linux, Xeon at 2.1 GHz). Threads made it slower
(0.70 ms per file on one, 3.0 ms on two), and process pools fail in the
sandbox (ADR 0015), so the read is serial. Enhanced and Legacy Converted
multi-frame objects are read a second time for their functional groups.

Nothing a file says leaves this module as text that could reach a log: an
exception becomes a code from a fixed table and its message is dropped,
because pydicom's and the operating system's messages quote paths and values.
The birth date is read to compute the age and is never returned.

Column formats the regroup relies on: multi-valued strings (ImageType,
ConvolutionKernel) are their values joined by a backslash as in DICOM;
`iop` is six numbers joined the same way (`decode_numbers` reads it back);
dates are ISO 8601 (YYYY-MM-DD), because the app's age SQL uses `julianday`;
frames of a multi-frame file are numbered from 1, so that frame 0 can stand
for a file of one frame in `cat_instances`.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import math
import os
import stat
import struct
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import IO, Any, Literal

from pydicom import config, dcmread
from pydicom.datadict import tag_for_keyword
from pydicom.dataset import Dataset

from bcoa_worker.index import dicomdir, nifti
from bcoa_worker.index.age import parse_date, study_age
from bcoa_worker.index.identity import issuer_link, normalize_id, patient_link
from bcoa_worker.worker import Cancelled

# Raised whenever what the reader stores, or how it decides a kind, changes, so
# that every row an older reader wrote is read again, in resumable batches
# (ADR 0022 decision 6). A newer number is never a reason to drop the catalog.
# 2: an encapsulated pixel element is judged by its items, so that trailing
# padding after it no longer makes a complete file `truncated`, and a frame
# count the pixel element cannot hold makes the file invalid (ADR 0029).
READER_VERSION = 2

# Python's own buffer, sized to hold a whole classic header in one read.
BUFFER_BYTES = 64 * 1024
# Enough for every signature below: tar's `ustar` sits at 257.
HEAD_BYTES = 512

Kind = Literal[
    "image",
    "non_image",
    "dicomdir",
    "nifti",
    "not_dicom",
    "unreadable",
    "archive",
    "symlink",
    "changing",
]
ReadCode = Literal[
    "read.permission_denied", "read.io_error", "read.invalid_dicom", "read.unsupported"
]

_HEADER_KEYWORDS = (
    "SpecificCharacterSet",
    "SOPClassUID",
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "FrameOfReferenceUID",
    "Modality",
    "NumberOfFrames",
    "ImageType",
    "StudyDate",
    "StudyTime",
    "StudyDescription",
    "PatientSex",
    "PatientAge",
    # Read to compute the age in memory, never stored (ADR 0024 decision 5).
    "PatientBirthDate",
    # Presence only, for the SUV checks of a PET (`pet_json.has_weight`).
    "PatientWeight",
    "SeriesNumber",
    "SeriesDate",
    "SeriesDescription",
    "ProtocolName",
    "BodyPartExamined",
    "ConvolutionKernel",
    "Manufacturer",
    "ManufacturerModelName",
    "KVP",
    "ContrastBolusAgent",
    "ContrastBolusAgentSequence",
    "BurnedInAnnotation",
    "SliceThickness",
    "SpacingBetweenSlices",
    "PixelSpacing",
    "Rows",
    "Columns",
    "ImageOrientationPatient",
    "ImagePositionPatient",
    "InstanceNumber",
    "AcquisitionNumber",
    "TemporalPositionIdentifier",
    "EchoNumbers",
    "GantryDetectorTilt",
    "RescaleSlope",
    "RescaleIntercept",
    "WindowCenter",
    "WindowWidth",
    "PhotometricInterpretation",
    "SamplesPerPixel",
    "BitsAllocated",
    "PixelRepresentation",
    "Units",
    "DecayCorrection",
    "CorrectedImage",
    "RadiopharmaceuticalInformationSequence",
)
# Requested only when the job carries a link key. After Remove Identifiers the
# file's identifiers are not even read (ADR 0024 decision 9.5).
IDENTIFIER_KEYWORDS = ("PatientID", "IssuerOfPatientID", "AccessionNumber")
_FUNCTIONAL_GROUP_KEYWORDS = ("SharedFunctionalGroupsSequence", "PerFrameFunctionalGroupsSequence")


def _tags(keywords: Sequence[str]) -> list[int]:
    tags = [tag_for_keyword(keyword) for keyword in keywords]
    assert all(tags), keywords
    return [tag for tag in tags if tag is not None]


HEADER_TAGS = _tags(_HEADER_KEYWORDS)
IDENTIFIER_TAGS = _tags(IDENTIFIER_KEYWORDS)
_FUNCTIONAL_GROUP_TAGS = _tags(_FUNCTIONAL_GROUP_KEYWORDS)
_PIXEL_TAGS = _tags(("PixelData", "FloatPixelData", "DoubleFloatPixelData"))


def header_tags(with_identifiers: bool) -> list[int]:
    return HEADER_TAGS + IDENTIFIER_TAGS if with_identifiers else list(HEADER_TAGS)


# Multi-frame objects whose geometry is in functional groups, per frame.
FUNCTIONAL_GROUP_SOP_CLASSES = frozenset(
    {
        "1.2.840.10008.5.1.4.1.1.2.1",  # Enhanced CT
        "1.2.840.10008.5.1.4.1.1.2.2",  # Legacy Converted Enhanced CT
        "1.2.840.10008.5.1.4.1.1.4.1",  # Enhanced MR
        "1.2.840.10008.5.1.4.1.1.4.3",  # Enhanced MR Color
        "1.2.840.10008.5.1.4.1.1.4.4",  # Legacy Converted Enhanced MR
        "1.2.840.10008.5.1.4.1.1.128.1",  # Legacy Converted Enhanced PET
        "1.2.840.10008.5.1.4.1.1.130",  # Enhanced PET
    }
)

# Storage classes that hold no image (ADR 0023: dose reports, SR, RTSTRUCT,
# SEG, presentation states, raw data). Any class not listed counts as an
# image: an unknown class is then shown and refused by modality, where a class
# wrongly taken for a non-image would never be reported as missing its pixels.
_NON_IMAGE_PREFIXES = (
    "1.2.840.10008.5.1.4.1.1.88.",  # structured reports, dose reports, key objects
    "1.2.840.10008.5.1.4.1.1.11.",  # presentation states
    "1.2.840.10008.5.1.4.1.1.9.",  # waveforms
    "1.2.840.10008.5.1.4.1.1.104.",  # encapsulated PDF, CDA, STL and the like
    "1.2.840.10008.5.1.4.1.1.66.",  # registrations, fiducials, SEG, surfaces
    "1.2.840.10008.5.1.4.1.1.68.",  # surface scans
    "1.2.840.10008.5.1.4.1.1.200.",  # protocols
    "1.2.840.10008.5.1.4.34.",  # RT instructions and records of the second generation
)
_NON_IMAGE_CLASSES = frozenset(
    {
        "1.2.840.10008.5.1.4.1.1.66",  # raw data
        "1.2.840.10008.5.1.4.1.1.67",  # real world value mapping
        "1.2.840.10008.5.1.4.1.1.481.3",  # RT structure set
        "1.2.840.10008.5.1.4.1.1.481.4",  # RT beams treatment record
        "1.2.840.10008.5.1.4.1.1.481.5",  # RT plan
        "1.2.840.10008.5.1.4.1.1.481.6",  # RT brachy treatment record
        "1.2.840.10008.5.1.4.1.1.481.7",  # RT treatment summary record
        "1.2.840.10008.5.1.4.1.1.481.8",  # RT ion plan
        "1.2.840.10008.5.1.4.1.1.481.9",  # RT ion beams treatment record
        "1.2.840.10008.5.1.4.1.1.481.10",  # RT physician intent
        "1.2.840.10008.5.1.4.1.1.481.11",  # RT segment annotation
    }
)


def is_image_class(sop_class_uid: str | None) -> bool:
    if not sop_class_uid:
        return True
    return sop_class_uid not in _NON_IMAGE_CLASSES and not sop_class_uid.startswith(
        _NON_IMAGE_PREFIXES
    )


_IMPLICIT_LE = "1.2.840.10008.1.2"
_EXPLICIT_BE = "1.2.840.10008.1.2.2"
_DEFLATED = "1.2.840.10008.1.2.1.99"
# Explicit VRs whose length takes 4 bytes after 2 reserved ones.
_LONG_VRS = frozenset(
    {b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"SV", b"UC", b"UN", b"UR", b"UT", b"UV"}
)
_UNDEFINED_LENGTH = 0xFFFFFFFF
_ITEM_GROUP = 0xFFFE
_ITEM = 0xE000
_SEQUENCE_DELIMITER = 0xE0DD

# No file is believed to hold more frames than this, whatever it says. A frame
# costs about 470 bytes in the regroup (measured with 1 000 000 claimed frames:
# 503 MB), so a damaged NumberOfFrames of 2^31 - 1 would need a terabyte and
# would fail every later regroup. The largest multi-frame objects of the
# modalities read here, a 4D MR of 72 slices and 1 200 time points, stay below
# it.
FRAME_CEILING = 100_000

_ARCHIVE_SIGNATURES = (
    b"PK\x03\x04",  # ZIP
    b"PK\x05\x06",  # empty ZIP
    b"PK\x07\x08",  # spanned ZIP
    b"\x1f\x8b",  # gzip
    b"7z\xbc\xaf\x27\x1c",  # 7z
    b"Rar!\x1a\x07",  # RAR 1.5 to 5
)


@dataclass(frozen=True, slots=True)
class Identity:
    """The link key of the job and the IDs that count as missing."""

    key: bytes
    placeholders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Frame:
    """One frame of a multi-frame file, numbered from 1."""

    frame: int
    position: tuple[float, float, float] | None
    orientation: str | None
    stack_id: str | None


@dataclass(slots=True)
class FileRecord:
    """What one read found: the kind, a read code for an unreadable file, and
    the `files` columns, frames and DICOMDIR entries of the rest."""

    kind: Kind
    size: int
    mtime_ns: int
    code: ReadCode | None = None
    values: dict[str, Any] = field(default_factory=dict)
    frames: list[Frame] = field(default_factory=list)
    dicomdir: list[tuple[str, str]] = field(default_factory=list)


class _Invalid(Exception):
    """A DICOM file without what makes it one: no SOP Instance, Study or
    Series UID. pydicom returns an empty dataset for "DICM" followed by bytes
    that are no dataset, rather than raising."""


def read_code(error: BaseException) -> ReadCode:
    """The fixed code of an exception; its message is never kept."""
    if isinstance(error, PermissionError) or (
        isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EPERM)
    ):
        return "read.permission_denied"
    if isinstance(error, nifti.Unsupported | NotImplementedError):
        return "read.unsupported"
    if isinstance(error, OSError) and not isinstance(error, _Invalid):
        return "read.io_error"
    return "read.invalid_dicom"


@contextmanager
def quiet_pydicom() -> Iterator[None]:
    """pydicom's validation off, and its warnings and log silenced, for as long
    as the read runs. Validation warnings quote raw values, for example
    `Invalid value for VR UI: '1.2.x'`, and a value can be a name."""
    logger = logging.getLogger("pydicom")
    level = logger.level
    mode = config.settings.reading_validation_mode
    config.settings.reading_validation_mode = config.IGNORE
    logger.setLevel(logging.CRITICAL)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logger.setLevel(level)
        config.settings.reading_validation_mode = mode


# ------------------------------------------------------------------ values


class Flat:
    """The top-level values of a dataset by keyword, each converted once.

    `Dataset.get(keyword)` resolves the keyword and runs the element's
    conversion on every call; reading the 45 values of a header that way took
    two thirds of the read (cProfile over 10 000 files). A value that does
    not convert, such as a DS of "abc", is left out: it costs that value, not
    the file.
    """

    __slots__ = ("_values",)

    def __init__(self, ds: Dataset) -> None:
        self._values: dict[str, Any] = {}
        for tag in list(ds.keys()):
            try:
                element = ds[tag]
            except Cancelled:
                raise
            except Exception:  # noqa: S112 - a malformed value is a missing one
                continue
            if element.keyword:
                self._values[element.keyword] = element.value

    def get(self, keyword: str) -> Any:
        return self._values.get(keyword)


# What the value helpers read from: a converted top level, or a sequence item.
Values = Dataset | Flat


def _get(ds: Values, keyword: str) -> Any:
    try:
        return ds.get(keyword)
    except Cancelled:
        raise
    except Exception:
        # One malformed value (a DS of "abc") costs that value, not the file.
        return None


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        return [value]
    return list(value)


def text(ds: Values, keyword: str) -> str | None:
    """A string value, multiple values joined by a backslash; None when
    empty."""
    items = [str(item).strip(" \x00") for item in _values(_get(ds, keyword))]
    joined = "\\".join(items)
    return joined if joined.strip("\\") else None


def number(ds: Values, keyword: str) -> float | None:
    """The first value as a finite float, or None."""
    items = _values(_get(ds, keyword))
    if not items:
        return None
    try:
        value = float(items[0])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def integer(ds: Values, keyword: str) -> int | None:
    value = number(ds, keyword)
    return int(value) if value is not None and value.is_integer() else None


def numbers(ds: Values, keyword: str, count: int) -> tuple[float, ...] | None:
    """Exactly `count` finite numbers, or None."""
    items = _values(_get(ds, keyword))
    if len(items) != count:
        return None
    try:
        values = tuple(float(item) for item in items)
    except (TypeError, ValueError):
        return None
    return values if all(math.isfinite(v) for v in values) else None


def encode_numbers(values: Sequence[float] | None) -> str | None:
    return None if values is None else "\\".join(repr(float(v)) for v in values)


def decode_numbers(value: str | None) -> tuple[float, ...] | None:
    """The numbers of an `iop` column, as `encode_numbers` wrote them."""
    if not value:
        return None
    try:
        return tuple(float(item) for item in value.split("\\"))
    except ValueError:
        return None


def _iso_date(value: str | None) -> str | None:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed is not None else None


def _sex(value: str | None) -> str | None:
    value = (value or "").strip().upper()
    return value if value in ("F", "M", "O") else None


def _first_item(ds: Values | None, keyword: str) -> Dataset | None:
    if ds is None:
        return None
    items = _values(_get(ds, keyword))
    return items[0] if items and isinstance(items[0], Dataset) else None


def _pet(ds: Values) -> str:
    """`pet_json`: what the PET checks and the pairing need, and no value that
    could identify anyone: the weight and the dose as presence only."""
    corrected = _get(ds, "CorrectedImage")
    if corrected is None:
        attenuation = None
    else:
        flags = {str(item).strip().upper() for item in _values(corrected)}
        attenuation = 1 if "ATTN" in flags else 0
    drug = _first_item(ds, "RadiopharmaceuticalInformationSequence")
    dose = number(drug, "RadionuclideTotalDose") if drug is not None else None
    start = None
    if drug is not None:
        start = text(drug, "RadiopharmaceuticalStartDateTime") or text(
            drug, "RadiopharmaceuticalStartTime"
        )
    weight = number(ds, "PatientWeight")
    return json.dumps(
        {
            "units": text(ds, "Units"),
            "decay": text(ds, "DecayCorrection"),
            "attenuation_corrected": attenuation,
            "has_dose": dose is not None and dose > 0,
            "has_start_time": start is not None,
            "has_weight": weight is not None and weight > 0,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _identity_values(ds: Values, identity: Identity | None) -> dict[str, Any]:
    if identity is None:
        return {"pid_state": "withheld"}
    link = patient_link(identity.key, text(ds, "PatientID"), identity.placeholders)
    accession = normalize_id(text(ds, "AccessionNumber")) or None
    return {
        "pid_state": link.state,
        "pid_link": link.link,
        # A placeholder is no identifier and must not become one.
        "patient_id": link.normalized if link.state == "present" else None,
        "issuer_link": issuer_link(identity.key, text(ds, "IssuerOfPatientID")),
        "accession_number": accession,
    }


def header_values(ds: Values, identity: Identity | None) -> dict[str, Any]:
    """The `files` columns a classic header fills."""
    study_date = text(ds, "StudyDate")
    age = study_age(text(ds, "PatientAge"), text(ds, "PatientBirthDate"), study_date)
    spacing = numbers(ds, "PixelSpacing", 2)
    position = numbers(ds, "ImagePositionPatient", 3)
    modality = text(ds, "Modality")
    agent_sequence = _values(_get(ds, "ContrastBolusAgentSequence"))
    values: dict[str, Any] = {
        "sop_class_uid": text(ds, "SOPClassUID"),
        "sop_uid": text(ds, "SOPInstanceUID"),
        "study_uid": text(ds, "StudyInstanceUID"),
        "series_uid": text(ds, "SeriesInstanceUID"),
        "for_uid": text(ds, "FrameOfReferenceUID"),
        "frames": integer(ds, "NumberOfFrames"),
        "sex": _sex(text(ds, "PatientSex")),
        "age_years": age.years,
        "age_source": age.source,
        "age_conflict": int(age.conflict),
        "study_date": _iso_date(study_date),
        "study_time": text(ds, "StudyTime"),
        "study_description": text(ds, "StudyDescription"),
        "modality": modality,
        "series_number": integer(ds, "SeriesNumber"),
        "series_date": _iso_date(text(ds, "SeriesDate")),
        "series_description": text(ds, "SeriesDescription"),
        "protocol": text(ds, "ProtocolName"),
        "body_part": text(ds, "BodyPartExamined"),
        "image_type": text(ds, "ImageType"),
        "kernel": text(ds, "ConvolutionKernel"),
        "manufacturer": text(ds, "Manufacturer"),
        "scanner_model": text(ds, "ManufacturerModelName"),
        "kvp": number(ds, "KVP"),
        "contrast_agent": text(ds, "ContrastBolusAgent"),
        "contrast_sequence": int(bool(agent_sequence)),
        "slice_thickness": number(ds, "SliceThickness"),
        "spacing_between_slices": number(ds, "SpacingBetweenSlices"),
        "pixel_spacing_row": spacing[0] if spacing else None,
        "pixel_spacing_col": spacing[1] if spacing else None,
        "image_rows": integer(ds, "Rows"),
        "image_columns": integer(ds, "Columns"),
        "iop": encode_numbers(numbers(ds, "ImageOrientationPatient", 6)),
        "ipp_x": position[0] if position else None,
        "ipp_y": position[1] if position else None,
        "ipp_z": position[2] if position else None,
        "instance_number": integer(ds, "InstanceNumber"),
        "acquisition_number": integer(ds, "AcquisitionNumber"),
        "temporal_position": integer(ds, "TemporalPositionIdentifier"),
        "echo_number": integer(ds, "EchoNumbers"),
        "gantry_tilt": number(ds, "GantryDetectorTilt"),
        "rescale_slope": number(ds, "RescaleSlope"),
        "rescale_intercept": number(ds, "RescaleIntercept"),
        "window_center": number(ds, "WindowCenter"),
        "window_width": number(ds, "WindowWidth"),
        "photometric": text(ds, "PhotometricInterpretation"),
        "samples_per_pixel": integer(ds, "SamplesPerPixel"),
        "bits_allocated": integer(ds, "BitsAllocated"),
        "pixel_representation": integer(ds, "PixelRepresentation"),
        "burned_in": text(ds, "BurnedInAnnotation"),
        "pet_json": _pet(ds) if (modality or "").upper() == "PT" else None,
    }
    values.update(_identity_values(ds, identity))
    return values


# ------------------------------------------------------------------ pixel data


@dataclass(frozen=True, slots=True)
class PixelElement:
    """What the pixel element after the header says, without a pixel read:
    its status, the value length of a native element, and the fragments of an
    encapsulated one (its Basic Offset Table not counted)."""

    status: str | None
    length: int | None = None
    fragments: int | None = None


def pixel_status(
    stream: IO[bytes], offset: int, size: int, syntax: str | None, image: bool
) -> str | None:
    """'ok', 'truncated' or 'missing' for the element at `offset`; see
    `pixel_element`."""
    return pixel_element(stream, offset, size, syntax, image).status


def pixel_element(
    stream: IO[bytes], offset: int, size: int, syntax: str | None, image: bool
) -> PixelElement:
    """The element at `offset`, the file position where the header read
    stopped (ADR 0022 decision 4, corrected by ADR 0027 and ADR 0029).

    A file cut inside its pixel data reads its header without an error, so
    the length of a native element is held against the size of the file. An
    encapsulated element is followed item by item up to its Sequence
    Delimitation Item: Data Set Trailing Padding and a Digital Signatures
    Sequence may legally follow it, so the file's last bytes say nothing. A
    file of a class without images and without pixels is neither: NULL.
    """
    absent = PixelElement("missing" if image else None)
    stream.seek(offset)
    header = stream.read(12)
    if len(header) < 8:
        return absent
    order = ">" if syntax == _EXPLICIT_BE else "<"
    group, element = struct.unpack(order + "HH", header[:4])
    if (group << 16 | element) not in _PIXEL_TAGS:
        return absent
    if syntax == _IMPLICIT_LE:
        length = struct.unpack("<I", header[4:8])[0]
        start = offset + 8
    elif header[4:6] in _LONG_VRS:
        if len(header) < 12:
            return PixelElement("truncated")
        length = struct.unpack(order + "I", header[8:12])[0]
        start = offset + 12
    else:
        length = struct.unpack(order + "H", header[6:8])[0]
        start = offset + 8
    if length == _UNDEFINED_LENGTH:
        fragments = _fragments(stream, start, size, order)
        if fragments is None:
            return PixelElement("truncated")
        if fragments == 0:
            # An offset table and nothing after it is no image either.
            return absent
        return PixelElement("ok", fragments=fragments)
    if length == 0:
        # An element that holds nothing is no image either.
        return absent
    return PixelElement("truncated" if start + length > size else "ok", length=length)


def _fragments(stream: IO[bytes], start: int, size: int, order: str) -> int | None:
    """The items of the encapsulated value at `start` less its offset table, or
    None when an item runs past the end of the file or anything but an item
    stands where the next one belongs.

    Only the 8-byte item headers are read, with `pread` where the stream has a
    descriptor: a seek past Python's buffer would refill all 64 KB of it for
    every fragment of a multi-frame file.
    """
    try:
        descriptor: int | None = stream.fileno()
    except (OSError, ValueError):
        descriptor = None
    position = start
    items = 0
    while position + 8 <= size:
        if descriptor is not None:
            header = os.pread(descriptor, 8, position)
        else:
            stream.seek(position)
            header = stream.read(8)
        if len(header) < 8:
            return None
        group, element, length = struct.unpack(order + "HHI", header)
        if group != _ITEM_GROUP:
            return None
        if element == _SEQUENCE_DELIMITER:
            return max(items - 1, 0)
        if element != _ITEM or length == _UNDEFINED_LENGTH:
            return None
        position += 8 + length
        items += 1
    return None


def _deflated_pixel_element(stream: IO[bytes], image: bool) -> PixelElement:
    # A Deflated file is inflated whole by pydicom, so the position after the
    # header says nothing about the file. Its stream is complete, or reading
    # the header would have failed; what is left to know is whether it holds
    # pixels. The element is deferred and only its length is looked at: its
    # value loaded on top of pydicom's own inflated copy took 955 MB for a
    # 300 KB file of 300 MB of zeros.
    stream.seek(0)
    ds = dcmread(stream, specific_tags=_PIXEL_TAGS, defer_size=256)
    for tag in _PIXEL_TAGS:
        if tag in ds:
            length = getattr(ds.get_item(tag, keep_deferred=True), "length", None)
            if not length:
                break
            if length == _UNDEFINED_LENGTH:
                return PixelElement("ok")
            return PixelElement("ok", length=length)
    return PixelElement("missing" if image else None)


def frame_capacity(pixels: PixelElement, values: dict[str, Any]) -> int:
    """How many frames the pixel element can hold, at most `FRAME_CEILING`.

    Each frame of an encapsulated element starts a fragment of its own. A
    native frame holds at least Rows × Columns values of BitsAllocated bits;
    SamplesPerPixel is left out on purpose, because YBR_FULL_422 stores three
    samples in the space of two, and a bound that counted them would refuse
    such a file. Where neither is known, only the ceiling applies.
    """
    if pixels.fragments is not None:
        return min(pixels.fragments, FRAME_CEILING)
    rows = values.get("image_rows") or 0
    columns = values.get("image_columns") or 0
    bits = values.get("bits_allocated") or 0
    if pixels.length and rows > 0 and columns > 0 and bits > 0:
        return min(pixels.length * 8 // (rows * columns * bits), FRAME_CEILING)
    return FRAME_CEILING


# ------------------------------------------------------------------ multi-frame


def _macro(item: Dataset | None, shared: Dataset | None, keyword: str) -> Dataset | None:
    """A functional group's macro for one frame: its own, else the shared one."""
    return _first_item(item, keyword) or _first_item(shared, keyword)


def functional_groups(stream: IO[bytes], values: dict[str, Any]) -> list[Frame]:
    """The frames of an Enhanced or Legacy Converted object, read a second
    time with its functional groups (21-115 ms per file, measured), and the
    file's thickness, spacing, rescale, kernel and window from them where the
    top level has none."""
    stream.seek(0)
    ds = dcmread(stream, stop_before_pixels=True, specific_tags=_FUNCTIONAL_GROUP_TAGS)
    shared = _first_item(ds, "SharedFunctionalGroupsSequence")
    per_frame = _values(_get(ds, "PerFrameFunctionalGroupsSequence"))
    items = [item for item in per_frame if isinstance(item, Dataset)]
    count = len(items) or values.get("frames") or 0
    frames = []
    for index in range(count):
        item = items[index] if index < len(items) else None
        plane = _macro(item, shared, "PlanePositionSequence")
        orientation = _macro(item, shared, "PlaneOrientationSequence")
        content = _macro(item, shared, "FrameContentSequence")
        position = numbers(plane, "ImagePositionPatient", 3) if plane is not None else None
        frames.append(
            Frame(
                frame=index + 1,
                position=position,  # type: ignore[arg-type]
                orientation=encode_numbers(
                    numbers(orientation, "ImageOrientationPatient", 6)
                    if orientation is not None
                    else None
                ),
                stack_id=text(content, "StackID") if content is not None else None,
            )
        )
    first = items[0] if items else None
    measures = _macro(first, shared, "PixelMeasuresSequence")
    rescale = _macro(first, shared, "PixelValueTransformationSequence")
    reconstruction = _macro(first, shared, "CTReconstructionSequence")
    window = _macro(first, shared, "FrameVOILUTSequence")
    filled: dict[str, Any] = {}
    if measures is not None:
        spacing = numbers(measures, "PixelSpacing", 2)
        filled["slice_thickness"] = number(measures, "SliceThickness")
        filled["spacing_between_slices"] = number(measures, "SpacingBetweenSlices")
        if spacing:
            filled["pixel_spacing_row"], filled["pixel_spacing_col"] = spacing
    if rescale is not None:
        filled["rescale_slope"] = number(rescale, "RescaleSlope")
        filled["rescale_intercept"] = number(rescale, "RescaleIntercept")
    if reconstruction is not None:
        filled["kernel"] = text(reconstruction, "ConvolutionKernel")
    if window is not None:
        filled["window_center"] = number(window, "WindowCenter")
        filled["window_width"] = number(window, "WindowWidth")
    if frames:
        filled["iop"] = frames[0].orientation
        if frames[0].position is not None:
            filled["ipp_x"], filled["ipp_y"], filled["ipp_z"] = frames[0].position
    for column, value in filled.items():
        if values.get(column) is None and value is not None:
            values[column] = value
    if not values.get("frames"):
        values["frames"] = count or None
    return frames


# ------------------------------------------------------------------ the read


def classify_head(head: bytes) -> Literal["dicom", "archive", "other"]:
    if len(head) >= 132 and head[128:132] == b"DICM":
        return "dicom"
    if head.startswith(_ARCHIVE_SIGNATURES) or head[257:262] == b"ustar":
        return "archive"
    return "other"


def _dicomdir_record(stream: IO[bytes], syntax: str | None) -> FileRecord:
    record = FileRecord("dicomdir", 0, 0, values={"transfer_syntax_uid": syntax})
    record.dicomdir = dicomdir.read_entries(stream)
    return record


def _read_dicom(stream: IO[bytes], size: int, identity: Identity | None, head: bytes) -> FileRecord:
    if dicomdir.DICOMDIR_MARK in head:
        # A DICOMDIR never reaches pydicom: its records hold PatientID and
        # PatientName, and pydicom parses every item of a sequence it reads.
        meta = dicomdir.file_meta(stream)
        if meta is not None and meta.media_class == dicomdir.DICOMDIR_SOP_CLASS:
            return _dicomdir_record(stream, meta.syntax)
    stream.seek(0)
    ds = dcmread(stream, stop_before_pixels=True, specific_tags=header_tags(identity is not None))
    stopped_at = stream.tell()
    meta = ds.file_meta
    media_class = text(meta, "MediaStorageSOPClassUID")
    syntax = text(meta, "TransferSyntaxUID")
    if media_class == dicomdir.DICOMDIR_SOP_CLASS:
        return _dicomdir_record(stream, syntax)
    values = header_values(Flat(ds), identity)
    values["sop_class_uid"] = values["sop_class_uid"] or media_class
    values["sop_uid"] = values["sop_uid"] or text(meta, "MediaStorageSOPInstanceUID")
    values["transfer_syntax_uid"] = syntax
    if not (values["sop_uid"] and values["study_uid"] and values["series_uid"]):
        raise _Invalid
    image = is_image_class(values["sop_class_uid"])
    if syntax == _DEFLATED:
        pixels = _deflated_pixel_element(stream, image)
    else:
        pixels = pixel_element(stream, stopped_at, size, syntax, image)
    values["pixel_data"] = pixels.status
    # NumberOfFrames is an IS of up to 2^31 - 1, and every frame becomes an
    # instance: one damaged 1.5 KB file claiming 4 000 000 frames took 1.9 GB
    # in the regroup, and would again at every later one. A claim the pixel
    # element cannot hold is a broken file, kept out of grouping like any
    # other (ADR 0029).
    capacity = frame_capacity(pixels, values)
    if (values["frames"] or 0) > max(capacity, 1):
        raise _Invalid
    record = FileRecord("image" if image else "non_image", 0, 0, values=values)
    if values["sop_class_uid"] in FUNCTIONAL_GROUP_SOP_CLASSES:
        record.frames = functional_groups(stream, values)
        if len(record.frames) > max(capacity, 1):
            raise _Invalid
    return record


def _nifti_header(stream: IO[bytes], head: bytes) -> bytes:
    if head.startswith(b"\x1f\x8b"):
        return nifti.gzip_header(stream)
    return head[: nifti.HEADER_BYTES]


def read_file(
    path: bytes,
    *,
    identity: Identity | None,
    previous_nifti: tuple[int, int, str | None] | None = None,
) -> FileRecord | None:
    """Open `path`, decide its kind and read what the catalog keeps of it;
    None when the file is gone since the walk listed it.

    The file is stat'ed when it is opened and again after it is read; a size
    or modification time that moved in between makes it `changing`, to be
    read again at the next scan (ADR 0022 decision 5). Every failure becomes
    `unreadable` with a code. `previous_nifti` is (size, mtime_ns, nifti_json)
    of the file's previous row, whose content hash is reused when the size
    and time still match.
    """
    try:
        stream = _open_file(path)
    except Cancelled:
        raise
    except (FileNotFoundError, NotADirectoryError):
        # Deleted, or its folder replaced, between the walk and the read: a
        # row for it would say "unreadable" about a file that is not there.
        return None
    except OSError as error:
        info = _lstat(path)
        if error.errno in (errno.ELOOP, errno.EMLINK, errno.EISDIR):
            # Replaced by a symbolic link since the walk (O_NOFOLLOW answers
            # ELOOP, EMLINK on FreeBSD), or by a folder, which the descriptor
            # opens and Python's file object refuses: never followed, and the
            # next walk records what is there now.
            return FileRecord("changing", info[0], info[1])
        return FileRecord("unreadable", info[0], info[1], code=read_code(error))
    with stream:
        before = os.fstat(stream.fileno())
        size, mtime_ns = before.st_size, before.st_mtime_ns
        if not stat.S_ISREG(before.st_mode):
            # A FIFO or a device where the walk saw a file.
            return FileRecord("changing", size, mtime_ns)
        try:
            record = _read_open(path, stream, size, mtime_ns, identity, previous_nifti)
        except Cancelled:
            raise
        except Exception as error:
            record = FileRecord("unreadable", 0, 0, code=read_code(error))
        after = os.fstat(stream.fileno())
    record.size, record.mtime_ns = size, mtime_ns
    if (after.st_size, after.st_mtime_ns) != (size, mtime_ns):
        return FileRecord("changing", size, mtime_ns)
    return record


def _open_file(path: bytes) -> IO[bytes]:
    """`path` opened for reading without following a symbolic link and without
    waiting on a FIFO, both of which a plain `open` does.

    The walk recorded a regular file, but by the time it is read it may have
    been replaced: a link would be read through, possibly out of the folder
    the user granted, and a FIFO blocked the scan until it was cancelled
    (measured). O_NONBLOCK matters for the open only and is cleared again:
    the caller reads nothing before it has seen that the file is regular, and
    a read must wait for a slow share rather than fail with EAGAIN.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        fcntl.fcntl(descriptor, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        return os.fdopen(descriptor, "rb", buffering=BUFFER_BYTES)
    except BaseException:
        os.close(descriptor)
        raise


def _read_open(
    path: bytes,
    stream: IO[bytes],
    size: int,
    mtime_ns: int,
    identity: Identity | None,
    previous_nifti: tuple[int, int, str | None] | None,
) -> FileRecord:
    head = stream.read(HEAD_BYTES)
    kind = classify_head(head)
    if kind == "dicom":
        return _read_dicom(stream, size, identity, head)
    name = nifti.nfc_name(path.rsplit(b"/", 1)[-1])
    if nifti.has_nifti_name(name) and nifti.header_ok(_nifti_header(stream, head)):
        cached = None
        if previous_nifti is not None and previous_nifti[:2] == (size, mtime_ns):
            cached = nifti.cached_sha256(previous_nifti[2])
        values = nifti.read_nifti(
            path,
            stream,
            key=identity.key if identity else None,
            placeholders=identity.placeholders if identity else (),
            cached_sha256=cached,
        )
        return FileRecord("nifti", size, mtime_ns, values=values)
    if kind == "archive":
        return FileRecord("archive", size, mtime_ns)
    return FileRecord("not_dicom", size, mtime_ns)


def _lstat(path: bytes) -> tuple[int, int]:
    try:
        info = os.lstat(path)
    except OSError:
        return 0, 0
    return info.st_size, info.st_mtime_ns


def symlink_record(path: bytes) -> FileRecord:
    """A symbolic link is recorded, never followed: links make loops and can
    leave the folder the user granted (ADR 0022 decision 1)."""
    size, mtime_ns = _lstat(path)
    return FileRecord("symlink", size, mtime_ns)
