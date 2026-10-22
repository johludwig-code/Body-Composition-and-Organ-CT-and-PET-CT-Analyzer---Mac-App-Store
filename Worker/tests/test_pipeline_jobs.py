"""The pipeline's jobs after the index (ADR 0031): convert, metrics and the
viewer cache, on a synthetic CT indexed by the real index job. MOOSE is not
run here; a labelmap written by the test stands in for its output, which the
S1 runs on real CTs cover (docs/benchmarks.md)."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
import pytest
import SimpleITK as sitk
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from bcoa_worker import guard, worker
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.jobs import convert
from bcoa_worker.protocol import parse_job
from index_jobs import index_job, run_index

dcm2niix = pytest.importorskip("dcm2niix")

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
ROWS = COLUMNS = 64
SLICES = 40
# The liver block: patient right (low columns), anterior (low rows), inferior
# (the first slices), so each axis of the cache can be checked on its own.
LIVER = (slice(0, 10), slice(10, 20), slice(5, 15))  # slices, rows, columns


def ct_volume(air: bool = True) -> np.ndarray:
    volume = np.full((SLICES, ROWS, COLUMNS), -1000 if air else 40, dtype=np.int16)
    yy, xx = np.mgrid[0:ROWS, 0:COLUMNS]
    body = (yy - 32) ** 2 + (xx - 32) ** 2 < 28**2
    volume[:, body] = 40
    volume[LIVER] = 60
    return volume


def write_series(folder: Path, volume: np.ndarray, *, vary_study_time: bool = False) -> None:
    folder.mkdir(parents=True)
    study, series, frame = "2.25.7001", "2.25.7002", "2.25.7003"
    for k in range(volume.shape[0]):
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CT_STORAGE
        meta.MediaStorageSOPInstanceUID = f"2.25.71{k:03d}"
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds = FileDataset(str(folder / f"{k}.dcm"), {}, file_meta=meta, preamble=b"\0" * 128)
        ds.SOPClassUID = CT_STORAGE
        ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.FrameOfReferenceUID = study, series, frame
        ds.PatientID, ds.PatientSex, ds.PatientAge = "SYN-0001", "F", "050Y"
        ds.StudyDate, ds.Modality, ds.Manufacturer = "20240102", "CT", "SIEMENS"
        # The public Pancreas-CT files each carry another StudyTime, which
        # makes dcm2niix split them; `vary_study_time` builds that case.
        ds.StudyTime = f"1200{k:02d}" if vary_study_time else "120000"
        ds.SeriesNumber, ds.InstanceNumber = 2, k + 1
        ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
        ds.SeriesDescription, ds.ConvolutionKernel = "Synthetic body", "B30f"
        ds.PatientPosition = "FFS"
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.ImagePositionPatient = [-32.0, -32.0, 2.5 * k]
        ds.PixelSpacing, ds.SliceThickness = [1.0, 1.0], 2.5
        ds.Rows, ds.Columns = ROWS, COLUMNS
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 16, 15, 1
        ds.RescaleIntercept, ds.RescaleSlope, ds.RescaleType = -1024, 1, "HU"
        ds.PixelData = (volume[k].astype(np.int32) + 1024).astype(np.int16).tobytes()
        ds.save_as(folder / f"{k}.dcm", enforce_file_format=True)


def resources(folder: Path, models: dict[str, dict[str, str]]) -> Path:
    """A resources folder laid out like the bundle's, with the test
    environment's dcm2niix and a manifest of fake models."""
    site = folder / "python" / "lib" / "python3.12" / "site-packages" / "dcm2niix"
    site.mkdir(parents=True)
    (site / "dcm2niix").symlink_to(dcm2niix.bin)
    (folder / "models").mkdir()
    manifest = {
        "models": [{"name": n, "labels": labels, "files": {}} for n, labels in models.items()]
    }
    (folder / "models" / "manifest.json").write_text(json.dumps(manifest))
    return folder


def run(project: Path, res: Path, kind: str, payload: dict[str, Any]) -> tuple[int, list[dict]]:
    raw = {
        "protocol_version": 1,
        "job_id": f"j_{kind}",
        "kind": kind,
        "project_dir": str(project),
        "log_path": str(project / "logs" / f"j_{kind}.log"),
        "resources_dir": str(res),
        "payload": payload,
    }
    buffer = io.StringIO()
    code = worker.run_job(parse_job(raw), ProtocolChannel(buffer), worker._handlers())
    return code, [json.loads(line) for line in buffer.getvalue().splitlines()]


def result(events: list[dict]) -> dict[str, Any]:
    found = [e["payload"] for e in events if e["type"] == "result"]
    assert len(found) == 1, events
    return found[0]


def error(events: list[dict]) -> str:
    return next(e["code"] for e in events if e["type"] == "error")


@pytest.fixture
def indexed(tmp_path: Path) -> Any:
    def make(volume: np.ndarray, **options: Any) -> tuple[Path, Path, dict[str, Any]]:
        source = tmp_path / "source" / "CT"
        write_series(source, volume, **options)
        project = tmp_path / "Study.bcoaproj"
        project.mkdir()
        code, _ = run_index(index_job(project, {1: source.parent}))
        assert code == 0
        with sqlite3.connect(project / "index" / "catalog.sqlite") as db:
            catalog_id = db.execute(
                "SELECT value FROM catalog_meta WHERE key = 'catalog_id'"
            ).fetchone()[0]
            (part_ref,) = db.execute("SELECT part_ref FROM cat_series").fetchone()
        res = resources(tmp_path / "Resources", {"test_organs": {"1": "liver", "2": "spleen"}})
        payload = {
            "series_key": "s_000001",
            "catalog": "index/catalog.sqlite",
            "catalog_id": catalog_id,
            "part_ref": part_ref,
            "sources": [
                {
                    "source_id": 1,
                    "root": str(source.parent),
                    "status": "active",
                    "volume_kind": "local",
                }
            ],
        }
        return project, res, payload

    return make


def test_a_ct_goes_through_convert_metrics_and_the_viewer_cache(indexed: Any) -> None:
    project, res, payload = indexed(ct_volume())
    code, events = run(project, res, "convert", payload)
    assert code == 0, events
    converted = result(events)
    assert converted["size"] == [COLUMNS, ROWS, SLICES]
    assert converted["spacing_mm"] == pytest.approx([1.0, 1.0, 2.5])
    assert converted["flags"] == []
    ct = sitk.ReadImage(str(project / converted["nifti"]))
    assert sitk.GetArrayViewFromImage(ct).min() == -1000

    # MOOSE's stand-in: the liver block as label 1 on the CT's grid.
    labels = np.zeros(sitk.GetArrayViewFromImage(ct).shape, dtype=np.uint8)
    labels[np.asarray(sitk.GetArrayViewFromImage(ct)) == 60] = 1
    labelmap = sitk.GetImageFromArray(labels)
    labelmap.CopyInformation(ct)
    (project / "work/s_000001/labels").mkdir(parents=True)
    sitk.WriteImage(labelmap, str(project / "work/s_000001/labels/test_organs.nii.gz"))
    maps = {"test_organs": "work/s_000001/labels/test_organs.nii.gz"}

    code, events = run(
        project,
        res,
        "metrics",
        {
            "series_key": "s_000001",
            "ct": converted["nifti"],
            "labelmaps": maps,
            "devices": {"test_organs": {"device": "mps", "fallback": True}},
            "flags": ["thick_slices"],
        },
    )
    assert code == 0, events
    rows = {r["label_name"]: r for r in result(events)["rows"]}
    liver, spleen = rows["liver"], rows["spleen"]
    assert liver["voxel_count"] == 10 * 10 * 10
    assert liver["volume_ml"] == pytest.approx(1000 * 2.5 / 1000)
    assert liver["hu_mean"] == pytest.approx(60.0)
    # It starts in the first slice, so it touches the volume's border.
    assert liver["flags"] == ["device_fallback", "thick_slices", "truncated"]
    assert spleen["voxel_count"] == 0 and spleen["hu_mean"] is None
    assert "empty_label" in spleen["flags"]

    code, events = run(
        project,
        res,
        "viewer_cache",
        {
            "series_key": "s_000001",
            "ct": converted["nifti"],
            "labelmaps": maps,
        },
    )
    assert code == 0, events
    folder = project / "viewer-cache" / "s_000001"
    meta = json.loads((folder / "meta.json").read_text())
    nx, ny, nz = meta["size"]
    assert (nx, ny, nz) == (COLUMNS, ROWS, SLICES) and not meta["oblique"]
    image = np.fromfile(folder / "image.i16", dtype="<i2").reshape(nz, ny, nx)
    cached = np.fromfile(folder / meta["models"]["test_organs"]["file"], dtype=np.uint8)
    cached = cached.reshape(nz, ny, nx)
    # LPS: i towards the patient's left, j posterior, k superior. The block
    # was written at low columns (right), low rows (anterior) and the first
    # slices, which lie lowest because their z is smallest.
    k, j, i = np.nonzero(cached == 1)
    assert (i.min(), i.max(), j.min(), j.max(), k.min(), k.max()) == (5, 14, 10, 19, 0, 9)
    assert image[0, 15, 6] == 60 and image[0, 0, 0] == -1000
    assert meta["models"]["test_organs"]["present"] == [1]


def test_a_stack_dcm2niix_splits_is_read_as_one(indexed: Any) -> None:
    project, res, payload = indexed(ct_volume(), vary_study_time=True)
    code, events = run(project, res, "convert", payload)
    assert code == 0, events
    converted = result(events)
    assert "converter_fallback" in converted["flags"]
    assert converted["size"] == [COLUMNS, ROWS, SLICES]
    assert converted["spacing_mm"][2] == pytest.approx(2.5)


def test_a_ct_without_air_is_refused(indexed: Any) -> None:
    project, res, payload = indexed(ct_volume(air=False))
    code, events = run(project, res, "convert", payload)
    assert code == 1
    assert error(events) == "hu_implausible"
    assert not (project / "work/s_000001/ct.nii").exists()


def test_a_rebuilt_catalog_is_refused(indexed: Any) -> None:
    project, res, payload = indexed(ct_volume())
    code, events = run(project, res, "convert", payload | {"catalog_id": "f" * 32})
    assert code == 1
    assert error(events) == "catalog_changed"
    assert next(e for e in events if e["type"] == "error")["recoverable"] is True


def test_the_converter_is_started_with_relative_arguments(indexed: Any, monkeypatch: Any) -> None:
    # Process arguments carry no paths (plan §13): the project path names the
    # user's folders.
    project, res, payload = indexed(ct_volume())
    seen: list[list[str]] = []
    real = convert.subprocess.run

    def record(args: list[str], **kwargs: Any) -> Any:
        seen.append(args)
        return real(args, **kwargs)

    monkeypatch.setattr(convert.subprocess, "run", record)
    assert run(project, res, "convert", payload)[0] == 0
    (args,) = seen
    assert args[0] == str(convert.dcm2niix_path(res))
    assert all(not a.startswith("/") for a in args[1:])


def test_programs_are_allowed_by_absolute_path_only() -> None:
    with pytest.raises(ValueError, match="absolute"):
        guard.allow_program("dcm2niix")


def test_choose_output_prefers_the_corrected_volume(tmp_path: Path) -> None:
    names = ["ct.nii", "ct_Eq_1.nii"]
    chosen, flags = convert.choose_output([tmp_path / n for n in names])
    assert chosen.name == "ct_Eq_1.nii" and flags == ["spacing_equalized"]
    with pytest.raises(convert.JobFailure):
        convert.choose_output([tmp_path / "ct.nii", tmp_path / "cta.nii"])


def test_the_dcm2niix_flags_are_the_ones_recorded() -> None:
    assert convert.DCM2NIIX_FLAGS == ("-z", "n", "-b", "y", "-ba", "y", "-i", "n", "-f", "ct")
    _ = pydicom  # imported for write_series
