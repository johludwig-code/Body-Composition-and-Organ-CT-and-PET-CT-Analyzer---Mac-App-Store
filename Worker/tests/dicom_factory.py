"""The synthetic corpus of the import (ADR 0026 decision 1).

`build_corpus` writes two source folders of synthetic files that cover the
rules of ADRs 0022 to 0024: what the walk counts and skips, what the reader
recognizes, how series are grouped, split and deduplicated, which part the
automatic selection takes and why, which checks fire, how studies find their
patients, and which values must never leave the catalog.
`corpus_expected.json` says what the index must make of it, by scenario and
series description, and `corpus_check.compare` holds a merged project to it.

Everything comes from a seed: UIDs are `2.25.` integers of a hash, pixel
values are a pattern of the slice index, archives carry fixed dates, and
nothing reads the clock, so two builds are the same bytes and a failure can be
reproduced from the test alone. Images are 16 × 16 pixels (larger only where
the matrix is the point), because the index reads headers; the corpus's
1 950-odd files are written in 3 to 4 s (Linux, Xeon at 2.1 GHz).

No value here belongs to a person. The canary values (CANARY^NAME,
CANARY-ID-4711, CANARYACC, 19010101, CANARY_FOLDER) exist so that a test can
search every output of the index for them (ADR 0024).
"""

from __future__ import annotations

# The index package blocks `requests` before pydicom is first imported, which
# keeps urllib3 and its IPv6 probe out of the test process as well (ADR 0022).
import bcoa_worker.index  # noqa: F401

# isort: split

import gzip
import hashlib
import io
import os
import stat
import struct
import tarfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from math import cos, radians, sin
from pathlib import Path
from typing import Any

import imagecodecs
import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.filebase import DicomBytesIO
from pydicom.filereader import dcmread
from pydicom.fileset import FileSet
from pydicom.filewriter import write_dataset
from pydicom.sequence import Sequence as DicomSequence
from pydicom.uid import (
    JPEG2000,
    UID,
    CTImageStorage,
    DeflatedExplicitVRLittleEndian,
    EnhancedCTImageStorage,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEG2000Lossless,
    JPEGBaseline8Bit,
    JPEGExtended12Bit,
    JPEGLossless,
    JPEGLosslessSV1,
    JPEGLSLossless,
    JPEGLSNearLossless,
    PositronEmissionTomographyImageStorage,
    RLELossless,
    SecondaryCaptureImageStorage,
)

SEED = "bcoa-corpus-1"
SOURCE_FOLDERS = {1: "source1", 2: "source2"}

COMPREHENSIVE_SR = "1.2.840.10008.5.1.4.1.1.88.33"
XRAY_DOSE_SR = "1.2.840.10008.5.1.4.1.1.88.67"

# Optional parts of the corpus, built only where the platform can hold them.
# `corpus_expected.json` marks what depends on each.
PERMISSIONS = "permissions"
NON_UTF8_NAMES = "non_utf8_names"

AXIAL = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
CORONAL = (1.0, 0.0, 0.0, 0.0, 0.0, -1.0)
SAGITTAL = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
# The top-left corner of a 16 × 16 image of 0.7 mm pixels centered on the
# table axis.
CORNER = -5.6


def _ds(value: float) -> str:
    """A DS value: at most 16 characters, nine decimals at most, no -0."""
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _ds_list(values: Sequence[float]) -> list[str]:
    return [_ds(v) for v in values]


def axial(count: int, start: float = 0.0, step: float = 3.0) -> list[tuple[float, float, float]]:
    """Positions of an axial stack along z."""
    return [(CORNER, CORNER, start + k * step) for k in range(count)]


def axial_at(zs: Sequence[float]) -> list[tuple[float, float, float]]:
    return [(CORNER, CORNER, z) for z in zs]


def tilted_orientation(degrees: float) -> tuple[float, ...]:
    """Rows along x, columns turned by `degrees` about x: the slice normal is
    (0, sin, cos), so a stack is still axial up to 18.2°."""
    angle = radians(degrees)
    return (1.0, 0.0, 0.0, 0.0, cos(angle), -sin(angle))


def along_normal(
    orientation: Sequence[float], count: int, step: float
) -> list[tuple[float, float, float]]:
    """Positions that advance along the slice normal: no shear."""
    r, c = orientation[:3], orientation[3:]
    n = (r[1] * c[2] - r[2] * c[1], r[2] * c[0] - r[0] * c[2], r[0] * c[1] - r[1] * c[0])
    return [
        (CORNER + k * step * n[0], CORNER + k * step * n[1], k * step * n[2]) for k in range(count)
    ]


@dataclass(frozen=True)
class Patient:
    # None leaves the element out; "" writes it empty.
    patient_id: str | None
    sex: str | None = "F"
    age: str | None = None
    birth_date: str | None = None
    name: str = "SYNTHETIC^CORPUS"
    weight: float | None = None


@dataclass(frozen=True)
class Study:
    key: str
    patient: Patient
    date: str
    description: str
    accession: str = ""


@dataclass
class Series:
    """One series as the scanner would write it, one file per position."""

    key: str
    folder: str
    description: str | None
    number: int | None
    positions: Sequence[tuple[float, float, float] | None]
    orientation: Sequence[float] | None = AXIAL
    modality: str = "CT"
    sop_class: str = CTImageStorage
    image_type: Sequence[str] | None = ("ORIGINAL", "PRIMARY", "AXIAL")
    thickness: float | None = 3.0
    pixel_spacing: Sequence[float] | None = (0.7, 0.7)
    rows: int = 16
    columns: int = 16
    kernel: str | None = "Br40d"
    manufacturer: str = "SIEMENS"
    model: str = "SOMATOM Synthetic"
    rescale: tuple[float, float] | None = (1.0, -1024.0)
    syntax: str = ExplicitVRLittleEndian
    frame_of_reference: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # Called with the 0-based instance index and its dataset, after the
    # defaults are set: per-file differences (acquisition numbers, a second
    # PatientID, a rescale that changes).
    vary: Callable[[int, Dataset], None] | None = None
    private_groups: bool = False
    file_names: Callable[[int], str] | None = None


@dataclass(frozen=True)
class Corpus:
    root: Path
    sources: dict[int, Path]
    features: frozenset[str]
    # Folders and files made unreadable on purpose; `unlock` gives them back
    # to the owner so the tree can be compared and deleted.
    locked: tuple[Path, ...] = ()

    def unlock(self) -> None:
        for path in self.locked:
            if path.exists() or path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)


class _Writer:
    def __init__(self, root: Path, seed: str) -> None:
        self.root = root
        self.seed = seed
        self.implementation_uid = self.uid("implementation")
        self.locked: list[Path] = []
        self.features: set[str] = set()

    # ------------------------------------------------------------ basics

    def uid(self, *parts: object) -> str:
        """A `2.25.` UID from the seed: 128 bits of a hash, as the standard
        allows for UIDs made without a registered root."""
        text = "\x1f".join([self.seed, *map(str, parts)]).encode()
        return "2.25." + str(int.from_bytes(hashlib.sha256(text).digest()[:16], "big"))

    def noise(self, size: int, *parts: object) -> bytes:
        """Bytes that look random and are the same in every build."""
        chunks = []
        counter = 0
        while sum(map(len, chunks)) < size:
            label = "\x1f".join([self.seed, "noise", *map(str, parts), str(counter)])
            chunks.append(hashlib.sha256(label.encode()).digest())
            counter += 1
        return b"".join(chunks)[:size]

    def path(self, source: int, relative: str | bytes) -> Path:
        base = os.fsencode(self.root / SOURCE_FOLDERS[source])
        target = Path(os.fsdecode(os.path.join(base, os.fsencode(relative))))
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def raw(self, source: int, relative: str | bytes, data: bytes) -> Path:
        target = self.path(source, relative)
        target.write_bytes(data)
        return target

    # ------------------------------------------------------------ datasets

    def meta(self, sop_class: str, sop_uid: str, syntax: str) -> FileMetaDataset:
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = sop_class
        meta.MediaStorageSOPInstanceUID = sop_uid
        meta.TransferSyntaxUID = syntax
        meta.ImplementationClassUID = self.implementation_uid
        meta.ImplementationVersionName = "BCOA_CORPUS_1"
        return meta

    def common(self, ds: Dataset, study: Study) -> None:
        patient = study.patient
        ds.SpecificCharacterSet = "ISO_IR 192"
        ds.PatientName = patient.name
        if patient.patient_id is not None:
            ds.PatientID = patient.patient_id
        if patient.sex is not None:
            ds.PatientSex = patient.sex
        if patient.age is not None:
            ds.PatientAge = patient.age
        if patient.birth_date is not None:
            ds.PatientBirthDate = patient.birth_date
        if patient.weight is not None:
            ds.PatientWeight = _ds(patient.weight)
        ds.StudyInstanceUID = self.uid(study.key, "study")
        ds.StudyDate = study.date
        ds.StudyTime = "120000"
        ds.StudyDescription = study.description
        ds.StudyID = "1"
        ds.AccessionNumber = study.accession
        ds.ReferringPhysicianName = ""

    def image(self, study: Study, series: Series, index: int) -> Dataset:
        sop_uid = self.uid(study.key, series.key, "sop", index)
        ds = Dataset()
        ds.file_meta = self.meta(series.sop_class, sop_uid, series.syntax)
        self.common(ds, study)
        ds.SOPClassUID = series.sop_class
        ds.SOPInstanceUID = sop_uid
        ds.Modality = series.modality
        ds.Manufacturer = series.manufacturer
        ds.ManufacturerModelName = series.model
        ds.SeriesInstanceUID = self.uid(study.key, series.key, "series")
        if series.number is not None:
            ds.SeriesNumber = series.number
        if series.description is not None:
            ds.SeriesDescription = series.description
        ds.SeriesDate = study.date
        ds.SeriesTime = "120000"
        ds.InstanceNumber = index + 1
        ds.AcquisitionNumber = 1
        if series.image_type is not None:
            ds.ImageType = list(series.image_type)
        ds.FrameOfReferenceUID = self.uid(study.key, series.frame_of_reference or "for")
        position = series.positions[index]
        if series.orientation is not None:
            ds.ImageOrientationPatient = _ds_list(series.orientation)
        if position is not None:
            ds.ImagePositionPatient = _ds_list(position)
        if series.thickness is not None:
            ds.SliceThickness = _ds(series.thickness)
        if series.pixel_spacing is not None:
            ds.PixelSpacing = _ds_list(series.pixel_spacing)
        if series.kernel is not None:
            ds.ConvolutionKernel = series.kernel
        if series.modality in ("CT", "PT"):
            ds.PatientPosition = "HFS"
        if series.modality == "CT":
            ds.KVP = "120"
        if series.rescale is not None:
            ds.RescaleSlope = _ds(series.rescale[0])
            ds.RescaleIntercept = _ds(series.rescale[1])
        ds.WindowCenter = "40"
        ds.WindowWidth = "400"
        ds.Rows = series.rows
        ds.Columns = series.columns
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 12
        ds.HighBit = 11
        ds.PixelRepresentation = 0
        for keyword, value in series.extra.items():
            setattr(ds, keyword, value)
        if series.private_groups:
            self.private_groups(ds, index)
        if series.vary is not None:
            series.vary(index, ds)
        # The matrix as `vary` left it, so that a file it resized holds an
        # image of its own size.
        _set_pixels(ds, pixels(ds.Rows, ds.Columns, index), series.syntax)
        return ds

    def private_groups(self, ds: Dataset, index: int) -> None:
        """Private blocks in the shape GE writes them: three creators, some
        forty short values and an 8 KB binary element, which is what makes
        GE headers slow to parse (ADR 0020)."""
        ds.add_new(0x00090010, "LO", "GEMS_IDEN_01")
        ds.add_new(0x00091001, "LO", "CT_LIGHTSPEED")
        ds.add_new(0x00091002, "SH", "CT01")
        ds.add_new(0x00191010, "LO", "GEMS_ACQU_01")
        for offset in range(40):
            ds.add_new(0x00191000 + 0x1002 + offset, "DS", _ds(offset * 0.5 + index))
        ds.add_new(0x00430010, "LO", "GEMS_PARM_01")
        ds.add_new(0x00431028, "OB", self.noise(8192, "ge-private", index))
        ds.add_new(0x00450010, "LO", "GEMS_HELIOS_01")
        ds.add_new(0x00451001, "SS", 16)

    def dataset_bytes(self, ds: Dataset) -> bytes:
        buffer = io.BytesIO()
        if UID(ds.file_meta.TransferSyntaxUID).is_private:
            # pydicom cannot know how a private syntax encodes the dataset;
            # explicit little endian is what every compressed syntax uses.
            ds.save_as(buffer, enforce_file_format=True, implicit_vr=False, little_endian=True)
        else:
            ds.save_as(buffer, enforce_file_format=True)
        return buffer.getvalue()

    def series(self, source: int, study: Study, series: Series) -> list[tuple[Path, bytes]]:
        written = []
        for index in range(len(series.positions)):
            name = series.file_names(index) if series.file_names else f"IM{index + 1:04d}.dcm"
            data = self.dataset_bytes(self.image(study, series, index))
            written.append((self.raw(source, f"{series.folder}/{name}", data), data))
        return written

    def copy(
        self, files: Sequence[tuple[Path, bytes]], source: int, folder: str
    ) -> list[tuple[Path, bytes]]:
        return [(self.raw(source, f"{folder}/{path.name}", data), data) for path, data in files]

    def sr(
        self,
        source: int,
        relative: str,
        study: Study,
        *,
        sop_class: str,
        series_key: str,
        description: str,
        number: int,
        instance: int,
    ) -> Path:
        """A structured report: no pixel data, no geometry, no ImageType."""
        sop_uid = self.uid(study.key, series_key, "sop", instance)
        ds = Dataset()
        ds.file_meta = self.meta(sop_class, sop_uid, ExplicitVRLittleEndian)
        self.common(ds, study)
        ds.SOPClassUID = sop_class
        ds.SOPInstanceUID = sop_uid
        ds.Modality = "SR"
        ds.Manufacturer = "SIEMENS"
        ds.SeriesInstanceUID = self.uid(study.key, series_key, "series")
        ds.SeriesNumber = number
        ds.SeriesDescription = description
        ds.InstanceNumber = instance + 1
        ds.ValueType = "CONTAINER"
        concept = Dataset()
        concept.CodeValue = "113701"
        concept.CodingSchemeDesignator = "DCM"
        concept.CodeMeaning = "X-Ray Radiation Dose Report"
        ds.ConceptNameCodeSequence = DicomSequence([concept])
        ds.ContinuityOfContent = "SEPARATE"
        ds.CompletionFlag = "COMPLETE"
        ds.VerificationFlag = "UNVERIFIED"
        return self.raw(source, relative, self.dataset_bytes(ds))

    def enhanced(
        self,
        study: Study,
        *,
        series_key: str,
        file_key: str,
        description: str,
        number: int,
        instance: int,
        frames: Sequence[tuple[tuple[float, float, float], Sequence[float], str]],
    ) -> Dataset:
        """An Enhanced CT file: geometry per frame in the functional groups,
        as the reader reads it a second time (ADR 0022 decision 3)."""
        sop_uid = self.uid(study.key, series_key, file_key, "sop")
        ds = Dataset()
        ds.file_meta = self.meta(EnhancedCTImageStorage, sop_uid, ExplicitVRLittleEndian)
        self.common(ds, study)
        ds.SOPClassUID = EnhancedCTImageStorage
        ds.SOPInstanceUID = sop_uid
        ds.Modality = "CT"
        ds.PatientPosition = "HFS"
        ds.Manufacturer = "SIEMENS"
        ds.ManufacturerModelName = "SOMATOM Synthetic"
        ds.SeriesInstanceUID = self.uid(study.key, series_key, "series")
        ds.SeriesNumber = number
        ds.SeriesDescription = description
        ds.InstanceNumber = instance
        ds.ImageType = ["ORIGINAL", "PRIMARY", "VOLUME", "NONE"]
        ds.FrameOfReferenceUID = self.uid(study.key, "for")
        ds.ContentQualification = "PRODUCT"
        ds.NumberOfFrames = len(frames)
        ds.Rows = 16
        ds.Columns = 16
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 12
        ds.HighBit = 11
        ds.PixelRepresentation = 0
        orientations = {tuple(orientation) for _, orientation, _ in frames}
        shared = Dataset()
        shared.PixelMeasuresSequence = DicomSequence(
            [_item(PixelSpacing=["0.7", "0.7"], SliceThickness="3")]
        )
        shared.PixelValueTransformationSequence = DicomSequence(
            [_item(RescaleSlope="1", RescaleIntercept="-1024", RescaleType="HU")]
        )
        shared.CTReconstructionSequence = DicomSequence(
            [_item(ConvolutionKernel="Br40d", ReconstructionAlgorithm="FILTER_BACK_PROJ")]
        )
        shared.FrameVOILUTSequence = DicomSequence([_item(WindowCenter="40", WindowWidth="400")])
        if len(orientations) == 1:
            shared.PlaneOrientationSequence = DicomSequence(
                [_item(ImageOrientationPatient=_ds_list(frames[0][1]))]
            )
        ds.SharedFunctionalGroupsSequence = DicomSequence([shared])
        per_frame = []
        for k, (position, orientation, stack_id) in enumerate(frames):
            item = Dataset()
            item.FrameContentSequence = DicomSequence(
                [_item(StackID=stack_id, InStackPositionNumber=k + 1, FrameAcquisitionNumber=1)]
            )
            item.PlanePositionSequence = DicomSequence(
                [_item(ImagePositionPatient=_ds_list(position))]
            )
            if len(orientations) > 1:
                item.PlaneOrientationSequence = DicomSequence(
                    [_item(ImageOrientationPatient=_ds_list(orientation))]
                )
            per_frame.append(item)
        ds.PerFrameFunctionalGroupsSequence = DicomSequence(per_frame)
        volume = np.stack([pixels(16, 16, k) for k in range(len(frames))])
        ds.PixelData = volume.tobytes()
        return ds


def _item(**values: Any) -> Dataset:
    item = Dataset()
    for keyword, value in values.items():
        setattr(item, keyword, value)
    return item


def pixels(rows: int, columns: int, index: int) -> np.ndarray:
    """A 12-bit pattern that differs from slice to slice, so that a codec
    has something to compress and a decoder something to get wrong."""
    i, j = np.mgrid[0:rows, 0:columns]
    return ((i * 7 + j * 3 + index * 11) % 2000 + 24).astype(np.uint16)


_LOSSY = {JPEGLSNearLossless, JPEG2000, JPEGBaseline8Bit, JPEGExtended12Bit}


def _encapsulated(ds: Dataset, frame: bytes) -> None:
    ds.PixelData = encapsulate([frame])
    ds["PixelData"].VR = "OB"
    ds["PixelData"].is_undefined_length = True


def _set_pixels(ds: Dataset, array: np.ndarray, syntax: str) -> None:
    """Pixel data in `syntax`. The compressed streams come from imagecodecs,
    the codecs the bundle decodes with, and are wrapped by pydicom; RLE uses
    pydicom's own encoder."""
    if syntax in (ExplicitVRLittleEndian, ImplicitVRLittleEndian, DeflatedExplicitVRLittleEndian):
        ds.PixelData = array.astype("<u2").tobytes()
    elif syntax == ExplicitVRBigEndian:
        ds.PixelData = array.astype(">u2").tobytes()
    elif syntax == RLELossless:
        ds.PixelData = array.astype("<u2").tobytes()
        # compress() gives the instance a new random UID unless told not to.
        ds.compress(RLELossless, array, generate_instance_uid=False)
    elif syntax == JPEGLosslessSV1:
        _encapsulated(
            ds, imagecodecs.jpeg8_encode(array, lossless=True, predictor=1, bitspersample=12)
        )
    elif syntax == JPEGLossless:
        # Process 14 with a predictor other than 1, which only …4.57 allows.
        _encapsulated(
            ds, imagecodecs.jpeg8_encode(array, lossless=True, predictor=6, bitspersample=12)
        )
    elif syntax == JPEGLSLossless:
        _encapsulated(ds, imagecodecs.jpegls_encode(array))
    elif syntax == JPEGLSNearLossless:
        _encapsulated(ds, imagecodecs.jpegls_encode(array, level=2))
    elif syntax == JPEG2000Lossless:
        _encapsulated(
            ds, imagecodecs.jpeg2k_encode(array, level=0, codecformat="J2K", reversible=True)
        )
    elif syntax == JPEG2000:
        _encapsulated(
            ds, imagecodecs.jpeg2k_encode(array, level=50, codecformat="J2K", reversible=False)
        )
    elif syntax == JPEGBaseline8Bit:
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        _encapsulated(ds, imagecodecs.jpeg8_encode((array >> 3).astype(np.uint8), level=95))
    elif syntax == JPEGExtended12Bit:
        _encapsulated(ds, imagecodecs.jpeg8_encode(array, level=95, bitspersample=12))
    elif UID(syntax).is_private:
        # What a private syntax holds is anyone's guess; the reader only has
        # to see that the converter cannot read it.
        _encapsulated(ds, array.astype("<u2").tobytes())
    else:
        raise ValueError(f"no encoder for {syntax}")
    if syntax in _LOSSY:
        ds.LossyImageCompression = "01"


# ---------------------------------------------------------------- NIfTI

_NIFTI = struct.Struct("<i10s18sihcc8hfffhhhh8ffffhccffffii80s24shhffffff4f4f4f16s4s")
_NIFTI_INT16 = 4
_NIFTI_UINT8 = 2


def nifti_bytes(shape: Sequence[int], spacing: Sequence[float], data: np.ndarray) -> bytes:
    """A NIfTI-1 file written field by field: SimpleITK's writer stamps
    nothing variable either, but this way no library decides the bytes."""
    code, bits = (_NIFTI_UINT8, 8) if data.dtype == np.uint8 else (_NIFTI_INT16, 16)
    dim = [len(shape), *shape] + [1] * (7 - len(shape))
    pixdim = [1.0, *spacing] + [1.0] * (7 - len(spacing))
    sx, sy, sz = spacing[:3]
    header = _NIFTI.pack(
        348, b"", b"", 0, 0, b"\0", b"\0", *dim,
        0.0, 0.0, 0.0, 0, code, bits, 0,
        *pixdim,
        352.0, 1.0, 0.0, 0, b"\0", b"\x0a",
        0.0, 0.0, 0.0, 0.0, 0, 0,
        b"synthetic", b"",
        1, 1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        sx, 0.0, 0.0, 0.0, 0.0, sy, 0.0, 0.0, 0.0, 0.0, sz, 0.0,
        b"", b"n+1\0",
    )  # fmt: skip
    return header + b"\0\0\0\0" + data.tobytes(order="F")


def _nifti_volume(shape: Sequence[int], dtype: type = np.int16) -> np.ndarray:
    count = int(np.prod(shape))
    return (np.arange(count) % 1000).astype(dtype).reshape(tuple(shape), order="F")


# ---------------------------------------------------------------- the corpus


def permissions_enforced(folder: Path) -> bool:
    """Whether a file with mode 000 is refused here. Root ignores the mode,
    and so do some file systems; the corpus then leaves those cases out
    rather than expecting a refusal that cannot happen."""
    probe = folder / ".permission-probe"
    probe.write_bytes(b"probe")
    probe.chmod(0)
    try:
        with probe.open("rb"):
            return False
    except PermissionError:
        return True
    finally:
        probe.chmod(0o600)
        probe.unlink()


def build_corpus(root: Path, *, seed: str = SEED) -> Corpus:
    """Write the corpus below `root` (which must be empty or missing) and say
    which optional parts this platform could hold."""
    root.mkdir(parents=True, exist_ok=True)
    writer = _Writer(root, seed)
    for folder in SOURCE_FOLDERS.values():
        (root / folder).mkdir(exist_ok=True)
    if permissions_enforced(root):
        writer.features.add(PERMISSIONS)
    for scenario in (
        _selection,
        _splits,
        _geometry,
        _pixel_data,
        _transfer_syntaxes,
        _duplicates,
        _enhanced,
        _pet_ct,
        _identity,
        _anonymous,
        _canary,
        _kernels,
        _dicomdir,
        _names,
        _nifti,
        _junk,
    ):
        scenario(writer)
    return Corpus(
        root=root,
        sources={source: root / folder for source, folder in SOURCE_FOLDERS.items()},
        features=frozenset(writer.features),
        locked=tuple(writer.locked),
    )


def _selection(w: _Writer) -> None:
    thin_thick = Study(
        "selection.thin_thick",
        Patient("SEL-0001", "F", "063Y"),
        "20240301",
        "CT Thorax",
        "SEL0001A",
    )
    w.series(
        1,
        thin_thick,
        Series(
            "localizer",
            "selection/thin_thick/topogram",
            "Topogram 0.6 T20s",
            1,
            [(CORNER, 0.0, 50.0)],
            orientation=CORONAL,
            image_type=("ORIGINAL", "PRIMARY", "LOCALIZER"),
            thickness=None,
            kernel="T20s",
        ),
    )
    w.series(
        1,
        thin_thick,
        Series(
            "thin",
            "selection/thin_thick/thin",
            "Thorax 1.0 Br40",
            2,
            axial(101, 0, 1.0),
            thickness=1.0,
        ),
    )
    w.series(
        1,
        thin_thick,
        Series(
            "thick",
            "selection/thin_thick/thick",
            "Thorax 2.0 Br40",
            3,
            axial(51, 0, 2.0),
            thickness=2.0,
        ),
    )
    w.sr(
        1,
        "selection/thin_thick/dose/SR0001.dcm",
        thin_thick,
        sop_class=XRAY_DOSE_SR,
        series_key="dose",
        description="Dose Report",
        number=990,
        instance=0,
    )
    # A Mac that copied this folder to an exFAT stick left its resource fork
    # beside the slice; the walk skips it without a row (ADR 0022).
    w.raw(
        1,
        "selection/thin_thick/thin/._IM0001.dcm",
        b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + w.noise(64, "appledouble"),
    )

    coverage = Study(
        "selection.coverage", Patient("SEL-0002", "M", "058Y"), "20240302", "CT Abdomen", "SEL0002A"
    )
    w.series(
        1,
        coverage,
        Series(
            "abdomen",
            "selection/coverage/abdomen",
            "Abdomen 5.0 Br40",
            2,
            axial(61, -300.0, 5.0),
            thickness=5.0,
            # A sequential scan numbers each slice as its own acquisition;
            # the positions never repeat, so this is no reason to split.
            vary=lambda k, ds: setattr(ds, "AcquisitionNumber", k + 1),
        ),
    )
    w.series(
        1,
        coverage,
        Series(
            "liver",
            "selection/coverage/liver",
            "Liver 2.0 Br40",
            3,
            axial(51, -150.0, 2.0),
            thickness=2.0,
        ),
    )

    kernel = Study(
        "selection.kernel",
        Patient("SEL-0003", "F", "071Y"),
        "20240303",
        "CT Abdomen Kernels",
        "SEL0003A",
    )
    for key, description, number, value in (
        ("sharp", "Abdomen 3.0 Br64", 2, "Br64d"),
        ("soft", "Abdomen 3.0 Br40", 3, "Br40d"),
        ("unknown", "Abdomen 3.0 Bv38", 4, "Bv38"),
        ("missing", "Abdomen 3.0 No Kernel", 5, None),
    ):
        w.series(
            1,
            kernel,
            Series(key, f"selection/kernel/{key}", description, number, axial(50), kernel=value),
        )

    not_sharp = Study(
        "selection.kernel_not_sharp",
        Patient("SEL-0010", "M", "054Y"),
        "20240310",
        "CT Thorax Kernels",
        "SEL0010A",
    )
    for key, description, number, value in (
        ("sharp", "Thorax 3.0 Br64", 2, "Br64d"),
        ("unknown", "Thorax 3.0 Bv38", 3, "Bv38"),
    ):
        w.series(
            1,
            not_sharp,
            Series(
                key,
                f"selection/kernel_not_sharp/{key}",
                description,
                number,
                axial(50),
                kernel=value,
            ),
        )

    warnings = Study(
        "selection.warnings",
        Patient("SEL-0004", "M", "066Y"),
        "20240304",
        "CT Abdomen Warnings",
        "SEL0004A",
    )
    w.series(
        1,
        warnings,
        Series(
            "a",
            "selection/warnings/a",
            "Abdomen A 3.0 B30f",
            2,
            axial(50),
            kernel="B30f",
            rescale=None,
        ),
    )
    w.series(
        1,
        warnings,
        Series("b", "selection/warnings/b", "Abdomen B 3.0 B30f", 3, axial(50), kernel="B30f"),
    )

    numbers = Study(
        "selection.series_number",
        Patient("SEL-0005", "F", "049Y"),
        "20240305",
        "CT Thorax Numbers",
        "SEL0005A",
    )
    w.series(
        1,
        numbers,
        Series("a", "selection/series_number/a", "Thorax A 3.0 B31f", 7, axial(50), kernel="B31f"),
    )
    w.series(
        1,
        numbers,
        Series("b", "selection/series_number/b", "Thorax B 3.0 B31f", 6, axial(50), kernel="B31f"),
    )

    tie = Study(
        "selection.tie", Patient("SEL-0006", "M", "052Y"), "20240306", "CT Thorax Tie", "SEL0006A"
    )
    for key in ("a", "b"):
        w.series(
            1,
            tie,
            Series(
                key,
                f"selection/tie/{key}",
                f"Thorax {key.upper()} 3.0 I30f",
                8,
                axial(50),
                kernel="I30f",
            ),
        )

    multiphase = Study(
        "selection.multiphase",
        Patient("SEL-0007", "F", "077Y"),
        "20240307",
        "CT Liver Multiphase",
        "SEL0007A",
    )
    # One series UID holding two phases at the same positions, told apart by
    # AcquisitionNumber only (ADR 0022 decision 9, step e).
    w.series(
        1,
        multiphase,
        Series(
            "phases",
            "selection/multiphase/phases",
            "Liver 3Phase 3.0 B30f",
            4,
            axial(50) + axial(50),
            kernel="B30f",
            vary=lambda k, ds: setattr(ds, "AcquisitionNumber", 1 if k < 50 else 2),
        ),
    )

    terms = Study(
        "selection.description",
        Patient("SEL-0008", "M", "044Y"),
        "20240308",
        "CT Description Terms",
        "SEL0008A",
    )
    for number, (key, description, image_type) in enumerate(
        (
            ("mpr", "Thorax MPR 3.0", None),
            ("compressed", "Abdomen Compressed 1.0", None),
            ("cor", "Thorax_Cor 1.0 Br40", None),
            ("mip", "Thorax MIP 10mm", ("ORIGINAL", "PRIMARY", "AXIAL", "MIP")),
            ("three_d", "Abdomen 3D 1.0", None),
        ),
        start=2,
    ):
        w.series(
            1,
            terms,
            Series(
                key,
                f"selection/description/{key}",
                description,
                number,
                axial(3),
                image_type=image_type or ("ORIGINAL", "PRIMARY", "AXIAL"),
            ),
        )

    types = Study(
        "selection.image_type",
        Patient("SEL-0009", "F", "039Y"),
        "20240309",
        "CT Image Types",
        "SEL0009A",
    )
    for number, (key, description, image_type) in enumerate(
        (
            ("missing", "No Image Type", None),
            ("unknown", "Value0 Unknown", ("UNKNOWN", "PRIMARY", "AXIAL")),
            ("two", "Two Values", ("ORIGINAL", "PRIMARY")),
            ("volume", "Classic Volume", ("ORIGINAL", "PRIMARY", "VOLUME")),
            ("reformatted", "Reformatted", ("DERIVED", "PRIMARY", "REFORMATTED")),
            ("secondary", "Secondary Axial", ("DERIVED", "SECONDARY", "AXIAL")),
        ),
        start=2,
    ):
        w.series(
            1,
            types,
            Series(
                key,
                f"selection/image_type/{key}",
                description,
                number,
                axial(3),
                image_type=image_type,
            ),
        )
    w.series(
        1,
        types,
        Series(
            "screen_save",
            "selection/image_type/screen_save",
            "Screen Save",
            9,
            [None],
            orientation=None,
            modality="OT",
            sop_class=SecondaryCaptureImageStorage,
            image_type=("DERIVED", "SECONDARY", "SCREEN SAVE"),
            thickness=None,
            pixel_spacing=None,
            kernel=None,
            rescale=None,
            extra={"ConversionType": "WSD"},
        ),
    )


def _splits(w: _Writer) -> None:
    study = Study(
        "split", Patient("SPLIT-0001", "M", "055Y"), "20240401", "CT Split Reasons", "SPL0001A"
    )

    w.series(1, study, Series("sop_class", "splits/sop_class", "Split SOP Class", 2, axial(4)))
    w.sr(
        1,
        "splits/sop_class/SR0005.dcm",
        study,
        sop_class=COMPREHENSIVE_SR,
        series_key="sop_class",
        description="Split SOP Class",
        number=2,
        instance=4,
    )
    coronal = [(CORNER, 3.0 * k, 5.6) for k in range(4)]
    w.series(
        1,
        study,
        Series(
            "orientation",
            "splits/orientation",
            "Split Orientation",
            3,
            axial(4) + coronal,
            vary=lambda k, ds: setattr(
                ds, "ImageOrientationPatient", _ds_list(AXIAL if k < 4 else CORONAL)
            ),
        ),
    )

    def size(k: int, ds: Dataset) -> None:
        if k >= 4:
            ds.Rows = ds.Columns = 20

    w.series(
        1, study, Series("size", "splits/size", "Split Size", 4, axial(4) + axial(4), vary=size)
    )

    def spacing(values: tuple[str, str]) -> Callable[[int, Dataset], None]:
        def vary(k: int, ds: Dataset) -> None:
            value = values[0] if k < 4 else values[1]
            ds.PixelSpacing = [value, value]

        return vary

    w.series(
        1,
        study,
        Series(
            "pixel_spacing",
            "splits/pixel_spacing",
            "Split Pixel Spacing",
            5,
            axial(4) + axial(4),
            vary=spacing(("0.7", "0.75")),
        ),
    )
    w.series(
        1,
        study,
        Series(
            "no_split_spacing",
            "splits/no_split_spacing",
            "No Split Spacing",
            6,
            axial(8),
            vary=spacing(("0.7", "0.704")),
        ),
    )

    def tag(keyword: str) -> Callable[[int, Dataset], None]:
        return lambda k, ds: setattr(ds, keyword, 1 if k < 4 else 2)

    w.series(
        1,
        study,
        Series(
            "acquisition",
            "splits/acquisition",
            "Split Acquisition",
            7,
            axial(4) + axial(4),
            vary=tag("AcquisitionNumber"),
        ),
    )
    w.series(
        1,
        study,
        Series(
            "temporal",
            "splits/temporal",
            "Split Temporal Position",
            8,
            axial(4) + axial(4),
            vary=tag("TemporalPositionIdentifier"),
        ),
    )
    w.series(
        1,
        study,
        Series(
            "echo", "splits/echo", "Split Echo", 9, axial(4) + axial(4), vary=tag("EchoNumbers")
        ),
    )
    w.series(
        1,
        study,
        Series(
            "no_split_acquisition",
            "splits/no_split_acquisition",
            "No Split Acquisition",
            10,
            axial(8),
            vary=lambda k, ds: setattr(ds, "AcquisitionNumber", k + 1),
        ),
    )
    w.series(
        1,
        study,
        Series(
            "duplicate_positions",
            "splits/duplicate_positions",
            "Duplicate Positions",
            11,
            axial_at([0, 3, 6, 9, 9, 12, 15, 18]),
        ),
    )


def _geometry(w: _Writer) -> None:
    study = Study(
        "geometry", Patient("GEO-0001", "F", "061Y"), "20240402", "CT Geometry Checks", "GEO0001A"
    )
    tilt15 = tilted_orientation(15.0)
    cases: list[Series] = [
        Series("gap", "geometry/gap", "Gap", 2, axial_at([0, 1, 2, 6, 7, 8]), thickness=None),
        Series(
            "uneven",
            "geometry/uneven",
            "Uneven",
            3,
            axial_at([0, 1, 2, 3.25, 4.25, 5.25]),
            thickness=1.0,
        ),
        # The gantry tilts the image plane; the table still moves along z,
        # so the stack is sheared by the same 15°.
        Series(
            "tilt",
            "geometry/tilt",
            "Gantry Tilt 15",
            4,
            axial(6),
            orientation=tilt15,
            extra={"GantryDetectorTilt": "15"},
        ),
        # Resampled onto an upright grid already: the tag stays, the shear
        # is gone.
        Series(
            "tilt_tag",
            "geometry/tilt_tag",
            "Tilt Tag Only",
            5,
            axial(6),
            extra={"GantryDetectorTilt": "12"},
        ),
        Series(
            "oblique10",
            "geometry/oblique10",
            "Oblique 10",
            6,
            along_normal(tilted_orientation(10.0), 6, 3.0),
            orientation=tilted_orientation(10.0),
        ),
        Series(
            "oblique25",
            "geometry/oblique25",
            "Oblique 25",
            7,
            along_normal(tilted_orientation(25.0), 6, 3.0),
            orientation=tilted_orientation(25.0),
        ),
        Series(
            "coronal",
            "geometry/coronal",
            "Coronal",
            8,
            [(CORNER, 3.0 * k, 5.6) for k in range(6)],
            orientation=CORONAL,
        ),
        Series(
            "sagittal",
            "geometry/sagittal",
            "Sagittal",
            9,
            [(3.0 * k, CORNER, 5.6) for k in range(6)],
            orientation=SAGITTAL,
        ),
        Series(
            "invalid_orientation",
            "geometry/invalid_orientation",
            "Invalid Orientation",
            10,
            axial(6),
            orientation=(1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        ),
        Series("missing_position", "geometry/missing_position", "Missing Position", 11, [None] * 6),
        Series(
            "non_square",
            "geometry/non_square",
            "Non Square",
            12,
            axial(6),
            pixel_spacing=(0.7, 0.8),
        ),
        Series(
            "values_vary",
            "geometry/values_vary",
            "Values Vary",
            13,
            axial(6),
            vary=lambda k, ds: setattr(ds, "RescaleIntercept", "-1024" if k < 3 else "-1000"),
        ),
        Series("no_kernel", "geometry/no_kernel", "No Kernel", 14, axial(6), kernel=None),
        Series(
            "burned_in",
            "geometry/burned_in",
            "Burned In",
            15,
            axial(6),
            extra={"BurnedInAnnotation": "YES"},
        ),
    ]
    for series in cases:
        w.series(1, study, series)


def _pixel_data(w: _Writer) -> None:
    study = Study(
        "pixel_data", Patient("PIX-0001", "M", "068Y"), "20240403", "CT Pixel Data", "PIX0001A"
    )
    native = w.series(
        1,
        study,
        Series("truncated_native", "pixels/truncated_native", "Truncated Native 3.0", 2, axial(50)),
    )
    # Cut inside the pixel data: the header reads without an error, which is
    # why the reader looks at the length of the pixel element itself.
    path, data = native[24]
    path.write_bytes(data[:-256])

    def trailing_padding(index: int, ds: Dataset) -> None:
        # Data Set Trailing Padding may legally follow the pixel data, so the
        # file's last bytes are not the Sequence Delimitation Item although
        # nothing is missing; this one file must stay `ok` (ADR 0029).
        if index == 19:
            ds.add_new(0xFFFCFFFC, "OB", bytes(64))

    encapsulated = w.series(
        1,
        study,
        Series(
            "truncated_encapsulated",
            "pixels/truncated_encapsulated",
            "Truncated JPEG-LS 3.0",
            3,
            axial(50),
            syntax=JPEGLSLossless,
            vary=trailing_padding,
        ),
    )
    # Cut by 20 bytes: the Sequence Delimitation Item and the end of the
    # fragment are gone.
    path, data = encapsulated[9]
    path.write_bytes(data[:-20])

    missing = Series("missing_pixels", "pixels/missing_pixels", "Missing Pixels 3.0", 4, axial(50))
    path, _ = w.series(1, study, missing)[29]
    # A header without its image, as an interrupted transfer can leave it.
    ds = w.image(study, missing, 29)
    del ds.PixelData
    path.write_bytes(w.dataset_bytes(ds))


def _transfer_syntaxes(w: _Writer) -> None:
    study = Study(
        "transfer_syntax",
        Patient("TS-0001", "F", "050Y"),
        "20240404",
        "CT Transfer Syntaxes",
        "TS0001A",
    )
    private = w.uid("private-syntax")
    for number, (key, description, syntax) in enumerate(
        (
            ("explicit_le", "Explicit VR Little Endian", ExplicitVRLittleEndian),
            ("implicit_le", "Implicit VR Little Endian", ImplicitVRLittleEndian),
            ("explicit_be", "Explicit VR Big Endian", ExplicitVRBigEndian),
            ("deflated", "Deflated Explicit VR Little Endian", DeflatedExplicitVRLittleEndian),
            ("rle", "RLE Lossless", RLELossless),
            ("jpeg_sv1", "JPEG Lossless SV1", JPEGLosslessSV1),
            ("jpeg_p14", "JPEG Lossless Process 14", JPEGLossless),
            ("jpeg_ls", "JPEG-LS Lossless", JPEGLSLossless),
            ("jpeg_ls_near", "JPEG-LS Near-Lossless", JPEGLSNearLossless),
            ("j2k_lossless", "JPEG 2000 Lossless", JPEG2000Lossless),
            ("j2k", "JPEG 2000", JPEG2000),
            ("jpeg_baseline", "JPEG Baseline 8-bit", JPEGBaseline8Bit),
            ("jpeg_ext12", "JPEG Extended 12-bit", JPEGExtended12Bit),
            ("private", "Private Syntax", private),
        ),
        start=2,
    ):
        w.series(
            1, study, Series(key, f"syntaxes/{key}", description, number, axial(3), syntax=syntax)
        )


def _duplicates(w: _Writer) -> None:
    study = Study(
        "duplicates", Patient("DUP-0001", "M", "059Y"), "20240405", "CT Duplicates", "DUP0001A"
    )
    folders = Series(
        "folders", "duplicates/copy_a", "Dup Folders 3.0 B30f", 2, axial(50), kernel="B30f"
    )
    copy_a = w.series(1, study, folders)
    w.copy(copy_a, 1, "duplicates/copy_b")
    # The first copy of slice 7 lost its end; the complete copy in copy_b must
    # win although its path sorts later (ADR 0022 decision 8).
    path, data = copy_a[6]
    path.write_bytes(data[:-256])
    # Slice 12's SOP Instance UID again, under another series UID: a UID
    # conflict, decided for the file that sorts first.
    stray = w.image(study, Series("stray", "duplicates/z_stray", "Stray Conflict", 90, axial(1)), 0)
    stray.SOPInstanceUID = w.uid(study.key, "folders", "sop", 11)
    stray.file_meta.MediaStorageSOPInstanceUID = stray.SOPInstanceUID
    w.raw(1, "duplicates/z_stray/IM0001.dcm", w.dataset_bytes(stray))

    # The same instances twice, Deflated in the folder that sorts first: the
    # copy the converter can read wins.
    syntax = Series(
        "syntax",
        "duplicates/a_deflated",
        "Dup Syntax 3.0",
        3,
        axial(5),
        syntax=DeflatedExplicitVRLittleEndian,
    )
    w.series(1, study, syntax)
    syntax.folder = "duplicates/b_explicit"
    syntax.syntax = ExplicitVRLittleEndian
    w.series(1, study, syntax)

    sources = w.series(
        1, study, Series("sources", "duplicates/sources", "Dup Sources 3.0", 4, axial(5))
    )
    w.copy(sources, 2, "dup_sources")
    w.series(2, study, Series("source_two", "source_two_only", "Source Two Only 3.0", 5, axial(5)))


def _enhanced(w: _Writer) -> None:
    patient = Patient("ENH-0001", "F", "057Y")
    single = Study("enhanced.single", patient, "20240406", "CT Enhanced Single", "ENH0001A")
    frames = [(position, AXIAL, "1") for position in axial(50)]
    ds = w.enhanced(
        single,
        series_key="volume",
        file_key="file",
        description="Enhanced Volume 3.0",
        number=2,
        instance=1,
        frames=frames,
    )
    w.raw(1, "enhanced/single/EN0001.dcm", w.dataset_bytes(ds))

    stacks = Study("enhanced.stacks", patient, "20240407", "CT Enhanced Stacks", "ENH0001B")
    for instance, stack in ((1, "1"), (2, "2")):
        ds = w.enhanced(
            stacks,
            series_key="stacks",
            file_key=f"stack{stack}",
            description="Enhanced Stacks 3.0",
            number=2,
            instance=instance,
            frames=[(position, AXIAL, stack) for position in axial(5)],
        )
        w.raw(1, f"enhanced/stacks/EN{instance:04d}.dcm", w.dataset_bytes(ds))

    mixed = Study("enhanced.mixed", patient, "20240408", "CT Enhanced Mixed", "ENH0001C")
    frames = [(position, AXIAL, "1") for position in axial(50)]
    frames += [((CORNER, 3.0 * k, 150.0), CORONAL, "1") for k in range(2)]
    ds = w.enhanced(
        mixed,
        series_key="mixed",
        file_key="file",
        description="Enhanced Mixed 3.0",
        number=2,
        instance=1,
        frames=frames,
    )
    w.raw(1, "enhanced/mixed/EN0001.dcm", w.dataset_bytes(ds))


def _pet_ct(w: _Writer) -> None:
    study = Study(
        "petct",
        Patient("PET-0001", "M", "064Y", weight=80.0),
        "20240510",
        "PET-CT Whole Body",
        "PET0001A",
    )
    ct = axial(50, -245.0, 5.0)
    w.series(
        1,
        study,
        Series(
            "ct_low_dose",
            "petct/ct_low_dose",
            "CT WB 5.0 B30f LowDose",
            2,
            ct,
            image_type=("DERIVED", "CT_SOM5 SPI", "PRIMARY", "AXIAL"),
            thickness=5.0,
            kernel="B30f",
        ),
    )
    w.series(
        1,
        study,
        Series(
            "ct_original",
            "petct/ct_original",
            "CT WB 5.0 Br38f",
            3,
            ct,
            thickness=5.0,
            kernel="Br38f",
        ),
    )

    def pet(
        key: str,
        description: str,
        number: int,
        positions: list,
        *,
        corrected: list[str] | None,
        units: str = "BQML",
        decay: str = "START",
        dose: bool = True,
        frame_of_reference: str | None = None,
        weight: bool = True,
    ) -> None:
        extra: dict[str, Any] = {
            "Units": units,
            "DecayCorrection": decay,
            "SeriesType": ["WHOLE BODY", "IMAGE"],
        }
        if corrected is not None:
            extra["CorrectedImage"] = corrected
        if dose:
            extra["RadiopharmaceuticalInformationSequence"] = DicomSequence(
                [
                    _item(
                        Radiopharmaceutical="Fluorodeoxyglucose",
                        RadionuclideTotalDose="350000000",
                        RadionuclideHalfLife="6586.2",
                        RadiopharmaceuticalStartDateTime="20240510110000",
                    )
                ]
            )

        def drop_weight(k: int, ds: Dataset) -> None:
            if not weight and "PatientWeight" in ds:
                del ds.PatientWeight

        w.series(
            1,
            study,
            Series(
                key,
                f"petct/{key}",
                description,
                number,
                positions,
                modality="PT",
                sop_class=PositronEmissionTomographyImageStorage,
                image_type=("ORIGINAL", "PRIMARY"),
                thickness=4.0,
                pixel_spacing=(4.0, 4.0),
                kernel=None,
                manufacturer="SIEMENS",
                rescale=(0.5, 0.0),
                frame_of_reference=frame_of_reference,
                extra=extra,
                vary=drop_weight,
            ),
        )

    pet_positions = axial(6, -240.0, 40.0)
    pet(
        "pet_ac",
        "PET WB AC",
        4,
        pet_positions,
        corrected=["DECY", "ATTN", "SCAT", "DTIM", "RAN", "NORM"],
    )
    pet("pet_nac", "PET WB NAC", 5, pet_positions, corrected=["DECY", "RAN", "NORM"])
    pet("pet_no_ci", "PET WB No CorrectedImage", 6, pet_positions, corrected=None)
    pet(
        "pet_other_for",
        "PET Other FoR",
        7,
        pet_positions,
        corrected=["ATTN"],
        units="CNTS",
        decay="NONE",
        dose=False,
        frame_of_reference="for-pet",
        weight=False,
    )
    pet("pet_far", "PET Far", 8, axial(6, 100.0, 10.0), corrected=["DECY", "ATTN"])


def _identity(w: _Writer) -> None:
    one = Study(
        "identity.one",
        Patient("PAT-\u00c901", "F", "060Y"),
        "20210504",
        "CT Identity One",
        "IDN0001",
    )
    # The same ID with a leading space and in NFD: stripped and NFC, it is
    # the same link (ADR 0024 decision 1).
    two = Study(
        "identity.two",
        Patient(" PAT-E\u030101", "M", "050Y"),
        "20230504",
        "CT Identity Two",
        "IDN0002",
    )
    w.series(1, one, Series("one", "identity/normalization/one", "Identity One 3.0", 2, axial(3)))
    w.series(1, two, Series("two", "identity/normalization/two", "Identity Two 3.0", 2, axial(3)))

    conflict = Study(
        "identity.age_conflict",
        Patient("AGE-0001", "F", "060Y", birth_date="19660815"),
        "20240601",
        "CT Age Conflict",
        "AGE0001A",
    )
    leap = Study(
        "identity.leap_day",
        Patient("AGE-0001", "F", None, birth_date="19640229"),
        "20230228",
        "CT Age Leap Day",
        "AGE0001B",
    )
    w.series(1, conflict, Series("age", "identity/age/conflict", "Age Conflict 3.0", 2, axial(3)))
    w.series(1, leap, Series("age", "identity/age/leap", "Age Leap Day 3.0", 2, axial(3)))

    for key, patient_id, sex, age, date, description in (
        ("days", "AGE-D", "U", "021D", "20240602", "CT Age Days"),
        ("weeks", "AGE-W", "O", "006W", "20240603", "CT Age Weeks"),
        ("months", "AGE-M", "M", "018M", "20240604", "CT Age Months"),
    ):
        study = Study(f"identity.{key}", Patient(patient_id, sex, age), date, description)
        w.series(
            1, study, Series("age", f"identity/age/{key}", f"Age {key.title()} 3.0", 2, axial(3))
        )

    patients = Study(
        "identity.patient_conflict",
        Patient("CONF-A1", "F", "045Y"),
        "20240605",
        "CT Patient Conflict",
        "CONF0001",
    )
    w.series(
        1,
        patients,
        Series(
            "conflict",
            "identity/patient_conflict",
            "Conflict 3.0",
            2,
            axial(6),
            vary=lambda k, ds: setattr(ds, "PatientID", "CONF-A1" if k < 4 else "CONF-B2"),
        ),
    )
    issuers = Study(
        "identity.issuer_conflict",
        Patient("ISS-0001", "M", "047Y"),
        "20240606",
        "CT Issuer Conflict",
        "ISS0001",
    )
    w.series(
        1,
        issuers,
        Series(
            "issuer",
            "identity/issuer_conflict",
            "Issuer 3.0",
            2,
            axial(6),
            vary=lambda k, ds: setattr(ds, "IssuerOfPatientID", "HOSP-A" if k < 4 else "HOSP-B"),
        ),
    )


def _anonymous(w: _Writer) -> None:
    # Directly below the source root, so that level 1 of the folder IDs is the
    # case folder (ADR 0024 decision 3).
    for key, patient_id, date, description, folder, count in (
        ("017a", "ANONYMOUS", "20190301", "CT Anon 017 A", "CASE_017/2019/CT", 50),
        ("017b", "ANONYMOUS", "20200301", "CT Anon 017 B", "CASE_017/2020/CT", 3),
        ("018", "", "20190401", "CT Anon 018", "CASE_018/CT", 3),
        ("019", "anonymized", "20190501", "CT Anon 019", "CASE_019/CT", 3),
        ("020", None, "20190601", "CT Anon 020", "CASE_020/CT", 3),
    ):
        study = Study(f"anonymous.{key}", Patient(patient_id, "F", "040Y"), date, description)
        w.series(1, study, Series("ct", folder, f"Anon {key} 3.0 Br40", 2, axial(count)))


def _canary(w: _Writer) -> None:
    study = Study(
        "canary",
        Patient("CANARY-ID-4711", "M", None, birth_date="19010101", name="CANARY^NAME"),
        "20240115",
        "CT Canary",
        "CANARYACC",
    )
    w.series(1, study, Series("ct", "CANARY_FOLDER/CT", "Canary 3.0", 2, axial(3)))


def _kernels(w: _Writer) -> None:
    study = Study(
        "kernels", Patient("KRN-0001", "M", "062Y"), "20240801", "CT Kernel Vendors", "KRN0001A"
    )
    for number, (key, description, manufacturer, kernel) in enumerate(
        (
            ("ge_standard", "GE Standard", "GE MEDICAL SYSTEMS", "STANDARD"),
            ("ge_bone", "GE Bone", "GE MEDICAL SYSTEMS", "BONE"),
            ("ge_body", "GE Body", "GE MEDICAL SYSTEMS", "BODY"),
            ("philips_b", "Philips B", "Philips", "B"),
            ("philips_yc", "Philips YC", "Philips", "YC"),
            ("philips_smoothd", "Philips Smoothd", "Philips", "SMOOTHD"),
            ("canon_fc08", "Canon FC08", "CANON_MEC", "FC08"),
            ("toshiba_fc30", "Toshiba FC30", "TOSHIBA", "FC30"),
            ("canon_fc99", "Canon FC99", "CANON_MEC", "FC99"),
            ("other_b30f", "Other Vendor B30f", "ACME IMAGING", "B30f"),
        ),
        start=2,
    ):
        w.series(
            1,
            study,
            Series(
                key,
                f"kernels/{key}",
                description,
                number,
                axial(3),
                kernel=kernel,
                manufacturer=manufacturer,
                private_groups=manufacturer.startswith("GE "),
            ),
        )


def _dicomdir(w: _Writer) -> None:
    patient = Patient("DIR-0001", "F", "053Y")
    for key, date, description, folder, removed in (
        ("complete", "20240701", "CT DICOMDIR Complete", "dicomdir/complete", ()),
        ("incomplete", "20240702", "CT DICOMDIR Incomplete", "dicomdir/incomplete", (1, 3)),
    ):
        study = Study(f"dicomdir.{key}", patient, date, description, f"DIR0001{key[0].upper()}")
        series = Series(key, folder, f"Dir {key.title()} 3.0", 2, axial(5))
        file_set = FileSet()
        # A fixed File-set UID: FileSet makes a random one otherwise, and the
        # DICOMDIR would differ from build to build.
        file_set.UID = w.uid(study.key, "file-set")
        file_set.ID = "BCOA"
        for index in range(5):
            file_set.add(w.image(study, series, index))
        target = w.path(1, f"{folder}/DICOMDIR").parent
        file_set.write(target)
        # Files the DICOMDIR lists and the copy lost (ADR 0022 decision 7).
        for index in removed:
            (target / "PT000000" / "ST000000" / "SE000000" / f"IM{index:06d}").unlink()


def _names(w: _Writer) -> None:
    study = Study("names", Patient("NFD-0001", "M", "048Y"), "20240703", "CT NFD Names", "NFD0001A")
    # "Gefäße" and "Schädel" decomposed, as a Mac writes them to some
    # volumes: the bytes of the name are the key, and NFC is only for
    # matching and display (ADR 0022 decision 1).
    w.series(
        1,
        study,
        Series(
            "nfd",
            "nfd/Gefa\u0308\u00dfe",
            "NFD Names 3.0",
            2,
            axial(3),
            file_names=lambda k: f"Scha\u0308del_{k + 1:02d}.dcm",
        ),
    )
    latin1 = b"nfd/latin1/Bild_\xe9.dcm"
    try:
        probe = w.path(1, latin1)
        probe.write_bytes(b"")
    except (OSError, UnicodeError):
        # APFS refuses names that are not UTF-8; the catalog's raw bytes are
        # then never put to that test here.
        return
    w.features.add(NON_UTF8_NAMES)
    names = {0: "IM0001.dcm", 1: os.fsdecode(b"Bild_\xe9.dcm"), 2: "IM0003.dcm"}
    w.series(
        1,
        study,
        Series(
            "latin1", "nfd/latin1", "Latin1 Name 3.0", 3, axial(3), file_names=names.__getitem__
        ),
    )


def _nifti(w: _Writer) -> None:
    # Each file has a matrix of its own, so that the expectation can tell
    # them apart in a project that holds no file names.
    def write(
        name: str, shape: Sequence[int], spacing: Sequence[float], dtype: type = np.int16
    ) -> None:
        data = nifti_bytes(shape, spacing, _nifti_volume(shape, dtype))
        compressed = name.lower().endswith(".gz")
        w.raw(1, f"nifti/{name}", gzip.compress(data, mtime=0) if compressed else data)

    write("CT_N001.nii.gz", (16, 16, 60), (0.75, 0.75, 2.5))
    write("PT_N001.nii.gz", (16, 16, 30), (2.0, 2.0, 4.0))
    write("CT_N002.nii", (18, 18, 60, 2), (0.75, 0.75, 2.5, 1.0))
    # The extension is matched without regard to case, the prefix with it.
    write("CT_N003.NII.GZ", (22, 22, 6), (0.75, 0.75, 2.5))
    write("ct_lower.nii.gz", (20, 20, 60), (0.75, 0.75, 2.5))
    # A label mask lying next to a CT must never be chosen as one.
    write("seg_mask.nii.gz", (24, 24, 60), (0.75, 0.75, 2.5), np.uint8)
    w.raw(1, "nifti/broken.nii.gz", gzip.compress(b"not a NIfTI header\n" * 40, mtime=0))
    w.raw(1, "nifti/garbage.nii", w.noise(600, "garbage-nii"))


def _junk(w: _Writer) -> None:
    hidden = Study("hidden", Patient("HIDDEN-0001", "F", "030Y"), "20240901", "CT Hidden")
    # Valid DICOM inside folders the walk must skip: if any of these is read,
    # a patient appears that the expectation does not list.
    for key, folder in (
        ("spotlight", ".Spotlight-V100/Store-V2"),
        ("trash", ".Trashes/501"),
        ("temporary", ".TemporaryItems/folders.501"),
    ):
        w.series(1, hidden, Series(key, folder, f"Hidden {key}", 2, axial(1)))
    w.raw(1, ".fseventsd/0000000000000001", w.noise(256, "fseventsd"))
    w.raw(1, ".DocumentRevisions-V100/PerUID/501/x", w.noise(256, "revisions"))
    w.raw(1, "junk/.DS_Store", b"\x00\x00\x00\x01Bud1" + w.noise(256, "ds_store"))
    w.raw(1, "junk/Thumbs.db", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + w.noise(256, "thumbs"))
    w.raw(1, "junk/desktop.ini", b"[.ShellClassInfo]\r\nIconResource=x\r\n")

    # Neither DICOM nor NIfTI.
    w.raw(1, "junk/README.txt", b"Synthetic corpus of the BCOA import tests.\n")
    w.raw(1, "junk/empty.dat", b"")
    random_bytes = w.noise(4096, "random")
    assert random_bytes[128:132] != b"DICM"
    w.raw(1, "junk/random.bin", random_bytes)
    w.raw(1, "junk/report.pdf", b"%PDF-1.4\n%synthetic\n" + w.noise(512, "pdf"))
    w.raw(1, "junk/short.dcm", w.noise(100, "short"))
    raw = w.image(hidden, Series("raw", "junk", "Raw Dataset", 3, axial(1)), 0)
    del raw.file_meta
    buffer = DicomBytesIO()
    buffer.is_little_endian = True
    buffer.is_implicit_VR = True
    write_dataset(buffer, raw)
    # A dataset without the 128-byte preamble and "DICM": not DICOM by the
    # plan's test, however well formed (ADR 0022 decision 2).
    w.raw(1, "junk/raw_dataset.dcm", buffer.getvalue())

    # Archives: counted, never opened.
    slice_bytes = w.dataset_bytes(w.image(hidden, Series("zip", "junk", "Zipped", 4, axial(1)), 0))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        info = zipfile.ZipInfo("export/IM0001.dcm", date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        zipped.writestr(info, slice_bytes)
    w.raw(1, "junk/export.zip", archive.getvalue())
    tarred = io.BytesIO()
    with tarfile.open(fileobj=tarred, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        member = tarfile.TarInfo("series/IM0001.dcm")
        member.size = len(slice_bytes)
        member.mtime = 0
        member.mode = 0o644
        tar.addfile(member, io.BytesIO(slice_bytes))
    w.raw(1, "junk/series.tar", tarred.getvalue())
    w.raw(1, "junk/notes.txt.gz", gzip.compress(b"synthetic notes\n" * 20, mtime=0))
    w.raw(1, "junk/backup.7z", b"7z\xbc\xaf\x27\x1c\x00\x04" + w.noise(256, "7z"))
    w.raw(1, "junk/backup.rar", b"Rar!\x1a\x07\x01\x00" + w.noise(256, "rar"))

    # Unreadable: cut inside the file meta (pydicom raises), and the
    # preamble and "DICM" followed by bytes that are no dataset (pydicom
    # returns an empty one, without SOP Class or Instance UID).
    w.raw(1, "junk/cut_meta.dcm", slice_bytes[: 132 + 20])
    w.raw(1, "junk/dicm_garbage.dcm", bytes(128) + b"DICM" + w.noise(2000, "dicm-garbage"))

    os.symlink("../selection/thin_thick/thin/IM0001.dcm", w.path(1, "junk/link_to_slice.dcm"))
    os.symlink("..", w.path(1, "junk/loop"))

    if PERMISSIONS in w.features:
        locked = Study("locked", Patient("LOCK-0001", "M", "033Y"), "20240902", "CT Locked")
        locked_file = Series(
            "file", "junk", "Locked File", 2, axial(1), file_names=lambda k: "locked_file.dcm"
        )
        file_path = w.series(1, locked, locked_file)[0][0]
        inside = w.series(1, locked, Series("dir", "junk/locked_dir", "Locked Folder", 3, axial(1)))
        inside_path = inside[0][0]
        file_path.chmod(0)
        inside_path.parent.chmod(0)
        w.locked.extend([file_path, inside_path.parent])


# ---------------------------------------------------------------- bulk


def write_bulk(
    root: Path,
    files: int,
    *,
    seed: str = SEED,
    per_series: int = 250,
    private_groups: bool = False,
) -> int:
    """`files` plain CT slices for the budgets of ADR 0026 decision 3, one
    patient and study per series of `per_series` slices; with
    `private_groups`, each slice carries GE-like private blocks, the headers
    that were measured slowest (100–111 s per 100 000 files)."""
    writer = _Writer(root, seed)
    written = 0
    series_index = 0
    while written < files:
        count = min(per_series, files - written)
        study = Study(
            f"bulk.{series_index}",
            Patient(f"BULK-{series_index:05d}", "F", "050Y"),
            "20240101",
            "CT Bulk",
        )
        series = Series(
            "ct",
            f"bulk/{series_index:05d}",
            "Bulk 1.0 Br40",
            2,
            axial(count, 0.0, 1.0),
            thickness=1.0,
            private_groups=private_groups,
        )
        for index in range(count):
            data = writer.dataset_bytes(writer.image(study, series, index))
            target = root / series.folder / f"IM{index + 1:04d}.dcm"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        written += count
        series_index += 1
    return written


# ---------------------------------------------------------------- reading back


def read_header(path: str | os.PathLike[str]) -> Dataset:
    """A file's header as pydicom reads it, for the tests that look at what
    the generator wrote. pydicom is imported here, after the index package
    has blocked `requests`, so a test never has to import it first."""
    return dcmread(path, stop_before_pixels=True)


# ---------------------------------------------------------------- comparing trees


def tree_digest(root: Path) -> list[tuple[bytes, str, int, str]]:
    """Every entry below `root` as (relative path in bytes, kind, permission
    bits, content): the hash of a file, the target of a link, nothing for a
    folder. Locked entries must be unlocked first (`Corpus.unlock`)."""
    base = os.fsencode(root)
    result: list[tuple[bytes, str, int, str]] = []
    for directory, folders, files in os.walk(base):
        for name in sorted(folders + files):
            path = os.path.join(directory, name)
            info = os.lstat(path)
            relative = os.path.relpath(path, base)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                result.append((relative, "symlink", mode, os.fsdecode(os.readlink(path))))
            elif stat.S_ISDIR(info.st_mode):
                result.append((relative, "dir", mode, ""))
            else:
                with open(path, "rb") as handle:
                    content = hashlib.sha256(handle.read()).hexdigest()
                result.append((relative, "file", mode, content))
    return sorted(result)
