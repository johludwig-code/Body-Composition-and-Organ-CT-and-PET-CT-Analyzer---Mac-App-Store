"""DICOMDIR as a completeness check (ADR 0022 decision 7).

The walk already finds every file, and a DICOMDIR can be stale, so it never
drives discovery. What it adds is the list of instances the media was meant
to hold: each IMAGE record's ReferencedSOPInstanceUIDInFile, with the
SeriesInstanceUID of the SERIES record above it, becomes a row of
`dicomdir_entries`, and the regroup reports per series the instances listed
there and found nowhere in the same source (`check.dicomdir_incomplete`).
Matching by SOP UID makes the case and Unicode normalization of
ReferencedFileID irrelevant.

The records are walked directly. pydicom's FileSet drops the records whose
file is missing, which is exactly what is worth reporting (14 of 15 records
loaded with one file deleted, 0 of 15 with lower-case names on a
case-sensitive volume), and took 1.37 s for 2 000 records where walking them
takes 174 ms.

Nor does pydicom read the file at all: it cannot filter the elements of
nested items, so requesting DirectoryRecordSequence parsed every PATIENT
record's PatientID and PatientName, also in a job without a link key, where
the worker must not even read them (ADR 0024 decision 9.5). The element
headers are walked here instead, and only the five values the entries need
are ever read; every other value, the PATIENT records' included, is skipped
by its length (ADR 0029).
"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import IO, Any

DICOMDIR_SOP_CLASS = "1.2.840.10008.1.3.10"
# What a file's head must contain before its meta is parsed a second time to
# see whether it is a DICOMDIR: the class is the third element of the meta,
# well inside the first 512 bytes, and the test costs nothing for the others.
DICOMDIR_MARK = DICOMDIR_SOP_CLASS.encode("ascii")

_MEDIA_CLASS = 0x00020002  # MediaStorageSOPClassUID
_SYNTAX = 0x00020010  # TransferSyntaxUID
_ROOT_OFFSET = 0x00041200  # OffsetOfTheFirstDirectoryRecordOfTheRootDirectoryEntity
_RECORDS = 0x00041220  # DirectoryRecordSequence
# The only values of a record that are read; a record is no Dataset, so the
# keywords are those `entries` asks for.
_RECORD_VALUES = {
    0x00041400: "OffsetOfTheNextDirectoryRecord",
    0x00041420: "OffsetOfReferencedLowerLevelDirectoryEntity",
    0x00041430: "DirectoryRecordType",
    0x0020000E: "SeriesInstanceUID",
    0x00041511: "ReferencedSOPInstanceUIDInFile",
}
_OFFSETS = frozenset({_ROOT_OFFSET, 0x00041400, 0x00041420})
# A UID or a record type is at most 64 bytes; a longer value is a damaged one
# and is skipped like any other.
_LONGEST_VALUE = 64

_ITEM = 0xFFFEE000
_ITEM_DELIMITER = 0xFFFEE00D
_SEQUENCE_DELIMITER = 0xFFFEE0DD
_UNDEFINED_LENGTH = 0xFFFFFFFF
# Explicit VRs whose length takes 4 bytes after 2 reserved ones.
_LONG_VRS = frozenset(
    {b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"SV", b"UC", b"UN", b"UR", b"UT", b"UV"}
)
# (explicit VR, byte order) of the dataset; a Deflated DICOMDIR would need
# inflating first and is reported as unsupported instead.
_SYNTAXES = {
    "1.2.840.10008.1.2": (False, "<"),
    "1.2.840.10008.1.2.1": (True, "<"),
    "1.2.840.10008.1.2.2": (True, ">"),
}


class Damaged(ValueError):
    """A DICOMDIR that ends inside an element, or holds what no element is."""


@dataclass(frozen=True, slots=True)
class Meta:
    media_class: str | None
    syntax: str | None


class _Record:
    """A directory record: the offset of its item and the values `entries`
    reads, by keyword, as pydicom's Dataset offers them."""

    __slots__ = ("_values", "seq_item_tell")

    def __init__(self, offset: int, values: dict[str, Any]) -> None:
        self.seq_item_tell = offset
        self._values = values

    def get(self, keyword: str) -> Any:
        return self._values.get(keyword)


def _text(record: Any, keyword: str) -> str:
    try:
        value = record.get(keyword)
    except Exception:
        # A malformed value is a missing one.
        return ""
    return str(value).strip(" \x00") if value is not None else ""


def _offset(record: Any, keyword: str) -> int:
    try:
        value = record.get(keyword)
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def entries(records: Sequence[Any], root_offset: int | None) -> list[tuple[str, str]]:
    """(SOP Instance UID, Series Instance UID) of every IMAGE record below a
    SERIES record, in the order the directory lists them, without repeats.

    The records form a tree through their offsets: each points to its next
    sibling and to its first child, as file positions. That tree is followed
    from the root record; where the offsets do not lead anywhere (a directory
    written without them, or edited by hand), the sequence order is used
    instead, in which each record follows its parent.
    """
    by_offset = {
        tell: record
        for record in records
        if isinstance(tell := getattr(record, "seq_item_tell", None), int)
    }
    found = _by_offsets(by_offset, root_offset) if root_offset in by_offset else None
    if not found:
        found = _in_sequence(records)
    seen: set[str] = set()
    unique = []
    for sop_uid, series_uid in found:
        if sop_uid not in seen:
            seen.add(sop_uid)
            unique.append((sop_uid, series_uid))
    return unique


def _by_offsets(by_offset: dict[int, Any], root_offset: int | None) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    visited: set[int] = set()
    # (offset of the first record of a level, series UID of the level above)
    stack: list[tuple[int, str]] = [(root_offset or 0, "")]
    while stack:
        offset, series_uid = stack.pop()
        # A loop in the offsets of a damaged directory must end, not spin.
        if offset in by_offset and offset not in visited:
            visited.add(offset)
            record = by_offset[offset]
            kind = _text(record, "DirectoryRecordType").upper()
            below = series_uid
            if kind == "SERIES":
                below = _text(record, "SeriesInstanceUID")
            elif kind == "IMAGE" and series_uid:
                sop_uid = _text(record, "ReferencedSOPInstanceUIDInFile")
                if sop_uid:
                    found.append((sop_uid, series_uid))
            child = _offset(record, "OffsetOfReferencedLowerLevelDirectoryEntity")
            following = _offset(record, "OffsetOfTheNextDirectoryRecord")
            if following:
                # Depth first, so that the order is the directory's own.
                stack.append((following, series_uid))
            if child:
                stack.append((child, below))
    return found


def _in_sequence(records: Iterable[Any]) -> list[tuple[str, str]]:
    found = []
    series_uid = ""
    for record in records:
        kind = _text(record, "DirectoryRecordType").upper()
        if kind in ("PATIENT", "STUDY"):
            series_uid = ""
        elif kind == "SERIES":
            series_uid = _text(record, "SeriesInstanceUID")
        elif kind == "IMAGE" and series_uid:
            sop_uid = _text(record, "ReferencedSOPInstanceUIDInFile")
            if sop_uid:
                found.append((sop_uid, series_uid))
    return found


class _Elements:
    """Element headers read one after the other, each value read only when
    asked for and otherwise skipped by its length."""

    def __init__(self, stream: IO[bytes], explicit: bool, order: str) -> None:
        self.stream = stream
        self.explicit = explicit
        self.order = order

    def read(self, count: int) -> bytes:
        data = self.stream.read(count)
        if len(data) < count:
            raise Damaged
        return data

    def skip(self, count: int) -> None:
        self.stream.seek(count, os.SEEK_CUR)

    def header(self, explicit: bool | None = None) -> tuple[int, bytes | None, int] | None:
        """(tag, VR, length) of the next element, or None at the end of the
        file. Item and delimiter headers carry no VR in any syntax."""
        head = self.stream.read(4)
        if not head:
            return None
        if len(head) < 4:
            raise Damaged
        group, element = struct.unpack(self.order + "HH", head)
        tag = group << 16 | element
        if group == 0xFFFE or not (self.explicit if explicit is None else explicit):
            return tag, None, struct.unpack(self.order + "I", self.read(4))[0]
        vr = self.read(2)
        if vr in _LONG_VRS:
            return tag, vr, struct.unpack(self.order + "I", self.read(6)[2:])[0]
        return tag, vr, struct.unpack(self.order + "H", self.read(2))[0]

    def value(self, tag: int, length: int) -> Any:
        data = self.read(length)
        if tag in _OFFSETS:
            return struct.unpack(self.order + "I", data)[0] if length == 4 else None
        return data.decode("ascii", "replace").strip(" \x00")

    def skip_value(self, vr: bytes | None, length: int, explicit: bool) -> None:
        if length != _UNDEFINED_LENGTH:
            self.skip(length)
            return
        # A sequence of undefined length, or an UN one, whose content is
        # implicit little endian whatever the file's syntax (PS3.5 6.2.2).
        self.skip_items(explicit and vr != b"UN")

    def skip_items(self, explicit: bool) -> None:
        while True:
            found = self.header(explicit)
            if found is None:
                raise Damaged
            tag, _, length = found
            if tag == _SEQUENCE_DELIMITER:
                return
            if tag != _ITEM:
                raise Damaged
            if length == _UNDEFINED_LENGTH:
                self.skip_item(explicit)
            else:
                self.skip(length)

    def skip_item(self, explicit: bool) -> None:
        while True:
            found = self.header(explicit)
            if found is None:
                raise Damaged
            tag, vr, length = found
            if tag == _ITEM_DELIMITER:
                return
            self.skip_value(vr, length, explicit)


def file_meta(stream: IO[bytes]) -> Meta | None:
    """The media storage class and transfer syntax of a DICOM file, read from
    its file meta without pydicom, the stream left at the dataset; None when
    the meta is not readable this way."""
    stream.seek(128)
    if stream.read(4) != b"DICM":
        return None
    elements = _Elements(stream, explicit=True, order="<")
    values: dict[int, str] = {}
    try:
        while True:
            position = stream.tell()
            found = elements.header()
            if found is None:
                break
            tag, _, length = found
            if tag >> 16 != 0x0002:
                stream.seek(position)
                break
            if length == _UNDEFINED_LENGTH:
                return None
            if tag in (_MEDIA_CLASS, _SYNTAX) and length <= _LONGEST_VALUE:
                values[tag] = elements.value(tag, length)
            else:
                elements.skip(length)
    except Damaged:
        return None
    return Meta(values.get(_MEDIA_CLASS) or None, values.get(_SYNTAX) or None)


def read_entries(stream: IO[bytes]) -> list[tuple[str, str]]:
    """The entries of the DICOMDIR open in `stream`, read from its start
    without pydicom. Raises Damaged for a file that ends inside an element,
    and NotImplementedError for a transfer syntax walked here never."""
    meta = file_meta(stream)
    if meta is None:
        raise Damaged
    if meta.syntax not in _SYNTAXES:
        raise NotImplementedError
    explicit, order = _SYNTAXES[meta.syntax]
    elements = _Elements(stream, explicit, order)
    root_offset: int | None = None
    while (found := elements.header()) is not None:
        tag, vr, length = found
        if tag == _ROOT_OFFSET and length == 4:
            root_offset = elements.value(tag, length)
        elif tag == _RECORDS:
            return entries(_records(elements, length), root_offset)
        else:
            elements.skip_value(vr, length, explicit)
    return []


def _records(elements: _Elements, length: int) -> list[_Record]:
    """The items of DirectoryRecordSequence, each with the file position of
    its item tag, which is what the records' offsets point to."""
    stream = elements.stream
    end = None if length == _UNDEFINED_LENGTH else stream.tell() + length
    records = []
    while end is None or stream.tell() < end:
        offset = stream.tell()
        found = elements.header()
        if found is None:
            raise Damaged
        tag, _, item_length = found
        if tag == _SEQUENCE_DELIMITER:
            break
        if tag != _ITEM:
            raise Damaged
        item_end = None if item_length == _UNDEFINED_LENGTH else stream.tell() + item_length
        records.append(_Record(offset, _record_values(elements, item_end)))
    return records


def _record_values(elements: _Elements, end: int | None) -> dict[str, Any]:
    stream = elements.stream
    values: dict[str, Any] = {}
    while end is None or stream.tell() < end:
        found = elements.header()
        if found is None:
            raise Damaged
        tag, vr, length = found
        if tag == _ITEM_DELIMITER:
            break
        keyword = _RECORD_VALUES.get(tag)
        if keyword is not None and length <= _LONGEST_VALUE:
            values[keyword] = elements.value(tag, length)
        else:
            elements.skip_value(vr, length, elements.explicit)
    return values
