"""Convert one series part to NIfTI and check it (plan §8, stages 1 and 2; ADR 0031).

The index decided which files form the part; the job takes exactly those from
the catalog, copies them into its scratch folder under neutral names and runs
the bundled dcm2niix there. Copies rather than the source folder itself,
because a source folder holds other series, and dcm2niix would convert them
too; and neutral names, because file and folder names often carry patient
names, and dcm2niix prints the names it reads.

Payload: `series_key`, `catalog`, `catalog_id`, `part_ref` and `sources` as
an index payload lists them. Writes `work/<series_key>/ct.nii` and its
sidecar `ct.json`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from bcoa_worker import guard
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.environment import scratch_dir
from bcoa_worker.errors import JobFailure
from bcoa_worker.moose_adapter import sha256_file
from bcoa_worker.protocol import Artifact, Job, Log, Progress, ProtocolError, Result

DCM2NIIX_VERSION = "1.0.20260724"
# Fixed, and part of the pipeline version (plan §8): no compression, because
# dcm2niix looks for pigz to compress and the worker starts no program but
# dcm2niix (ADR 0016); a BIDS sidecar without identifying fields; and every
# image kept, derived ones too, because the user may have chosen a derived
# series by hand.
DCM2NIIX_FLAGS: tuple[str, ...] = ("-z", "n", "-b", "y", "-ba", "y", "-i", "n", "-f", "ct")
PIPELINE_VERSION = "1"

# Air outside the body is −1000 HU; reconstructions pad the field of view with
# −1024 to −3024. A CT whose darkest percent is above this has no air in it
# and almost always a wrong rescale (plan §8: "Check rescale slope/intercept").
AIR_PERCENTILE_LIMIT_HU = -900.0
MIN_SIZE = 16
MIN_SLICES = 3
THICK_SLICES_MM = 3.0

# Warnings of dcm2niix that become QC flags, by a phrase of their text. The
# text itself never reaches a log: it can quote what the files say.
_WARNING_FLAGS = {
    "equidistant": "spacing_irregular",
    "slice spacing": "spacing_irregular",
    "missing images": "spacing_irregular",
    "gantry tilt": "gantry_tilt",
}


def dcm2niix_path(resources_dir: Path) -> Path:
    """The bundled executable, the one program the worker may start."""
    site = resources_dir / "python" / "lib" / "python3.12" / "site-packages"
    return site / "dcm2niix" / "dcm2niix"


def part_files(catalog: Path, catalog_id: str, part_ref: int) -> list[tuple[int, bytes, str]]:
    """(source_id, rel_path, kind) of every file of the part, in the order the
    index sorted its instances, each file once (a multi-frame file holds many)."""
    uri = f"{catalog.resolve().as_uri()}?mode=ro"
    deadline = time.monotonic() + 30.0
    while True:
        try:
            with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
                meta = dict(db.execute("SELECT key, value FROM catalog_meta").fetchall())
                if meta.get("catalog_id") != catalog_id:
                    # Rebuilt since the series was merged: its part refs mean
                    # other parts now. The app merges and queues again.
                    raise JobFailure(
                        "catalog_changed",
                        "The index changed; queue the series again",
                        recoverable=True,
                    )
                rows = db.execute(
                    """
                    SELECT f.source_id, f.rel_path, f.kind, min(i.ordinal) AS first
                    FROM cat_instances AS i JOIN files AS f USING (file_id)
                    WHERE i.part_ref = ? GROUP BY f.file_id ORDER BY first
                    """,
                    (part_ref,),
                ).fetchall()
                return [(int(r[0]), bytes(r[1]), str(r[2])) for r in rows]
        except sqlite3.OperationalError as exc:
            # A scan holds the catalog for at most one batch (ADR 0021).
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise JobFailure(
                    "catalog_busy", "The index is busy; try again", recoverable=True
                ) from exc
            time.sleep(0.5)


def _source_file(job: Job, source_id: int, rel_path: bytes) -> Path:
    root = os.fsencode(job.source_root(source_id))
    return Path(os.fsdecode(os.path.join(root, rel_path)))


def _copy_inputs(job: Job, files: list[tuple[int, bytes, str]], target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    width = len(str(len(files)))
    for number, (source_id, rel_path, _) in enumerate(files, start=1):
        # Read only, never followed through a link: the index did not follow
        # one either, so a link here would be a file the index never saw.
        source = _source_file(job, source_id, rel_path)
        if source.is_symlink():
            raise JobFailure(
                "source_changed", "A file of the series became a link", recoverable=True
            )
        try:
            shutil.copyfile(source, target / f"{number:0{width}d}.dcm")
        except FileNotFoundError as exc:
            raise JobFailure(
                "source_changed",
                "A file of the series is gone; scan the source again",
                recoverable=True,
            ) from exc
        except PermissionError as exc:
            raise JobFailure("source_unreadable", "A file of the series cannot be read") from exc


def _run_dcm2niix(job: Job, scratch: Path) -> tuple[list[Path], list[str]]:
    executable = str(dcm2niix_path(job.resources_dir))
    guard.allow_program(executable)
    out = scratch / "out"
    out.mkdir(parents=True, exist_ok=True)
    # Relative arguments with the scratch folder as working directory: the
    # project path names the user's folders, and process arguments carry no
    # paths (plan §13).
    completed = subprocess.run(
        [executable, *DCM2NIIX_FLAGS, "-o", "out", "dicom"],
        cwd=scratch,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
    )
    lines = (completed.stdout + completed.stderr).splitlines()
    flags = sorted(
        {
            flag
            for line in lines
            if line.lower().startswith("warning")
            for phrase, flag in _WARNING_FLAGS.items()
            if phrase in line.lower()
        }
    )
    warnings = sum(1 for line in lines if line.lower().startswith("warning"))
    print(f"[convert] dcm2niix exit {completed.returncode}, {warnings} warnings", flush=True)
    if completed.returncode != 0:
        raise JobFailure("convert_failed", f"dcm2niix ended with {completed.returncode}")
    return sorted(out.glob("*.nii")), flags


def convert_stack_with_simpleitk(folder: Path, target: Path) -> None:
    """The fallback for a stack of single-frame files that dcm2niix splits
    although the index found one stack: one orientation, size, spacing and a
    position per file (ADR 0031). Seen on the public Pancreas-CT collection,
    whose files each carry another StudyTime, which dcm2niix reads as another
    acquisition. The copies are numbered in the index's order along the stack;
    GDCM applies the rescale and SimpleITK takes the spacing from the
    positions."""
    import numpy as np
    import SimpleITK as sitk

    names = sorted(str(p) for p in folder.glob("*.dcm"))
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(names)
    image = reader.Execute()
    if image.GetDimension() != 3 or image.GetSize()[2] != len(names):
        raise JobFailure("convert_ambiguous", "The files of the series do not form one volume")
    values = sitk.GetArrayViewFromImage(image)
    # Rescaled CT is whole Hounsfield units in practice; int16 halves the
    # file against float32 and is what dcm2niix writes for it.
    if (
        image.GetPixelID() != sitk.sitkInt16
        and np.all(np.mod(values, 1) == 0)
        and values.min() >= -32768
        and values.max() <= 32767
    ):
        image = sitk.Cast(image, sitk.sitkInt16)
    sitk.WriteImage(image, str(target))


def choose_output(outputs: list[Path]) -> tuple[Path, list[str]]:
    """The one volume of the part. dcm2niix writes a corrected copy beside the
    original for a gantry tilt (`_Tilt_`) or uneven spacing (`_Eq_`); the
    corrected one is the volume the metrics need, and its correction a flag."""
    if not outputs:
        raise JobFailure("convert_empty", "dcm2niix wrote no volume")
    for marker, flag in (("_Tilt_", "gantry_tilt_corrected"), ("_Eq_", "spacing_equalized")):
        corrected = [p for p in outputs if marker in p.name]
        if len(corrected) == 1:
            return corrected[0], [flag]
    plain = [p for p in outputs if p.stem == "ct"]
    if len(outputs) == 1:
        return outputs[0], []
    if len(plain) == 1 and all("_ROI" in p.name for p in outputs if p != plain[0]):
        return plain[0], []
    raise JobFailure("convert_ambiguous", f"dcm2niix wrote {len(outputs)} volumes for one series")


def check_volume(path: Path) -> dict[str, Any]:
    """Stage 2 (plan §8): size, air, slice thickness. Raises for an error,
    returns the geometry and the flags of a warning."""
    import numpy as np
    import SimpleITK as sitk

    image = sitk.ReadImage(str(path))
    size = list(image.GetSize())
    if image.GetDimension() != 3:
        raise JobFailure("not_3d", "The series is not one 3D volume")
    if min(size[:2]) < MIN_SIZE or size[2] < MIN_SLICES:
        raise JobFailure("too_small", f"The volume is too small ({size[0]}×{size[1]}×{size[2]})")
    array = sitk.GetArrayViewFromImage(image)
    # Every fourth voxel in each direction is plenty for a percentile.
    sample = np.asarray(array[::4, ::4, ::4], dtype=np.float64)
    darkest = float(np.percentile(sample, 1.0))
    if darkest > AIR_PERCENTILE_LIMIT_HU:
        raise JobFailure(
            "hu_implausible",
            f"No air in the image (1st percentile {darkest:.0f} HU); check rescale slope/intercept",
        )
    spacing = [float(s) for s in image.GetSpacing()]
    flags = ["thick_slices"] if spacing[2] > THICK_SLICES_MM + 0.02 else []
    return {
        "size": size,
        "spacing_mm": spacing,
        "origin_mm": [float(o) for o in image.GetOrigin()],
        "direction": [float(d) for d in image.GetDirection()],
        "air_p01_hu": darkest,
        "flags": flags,
    }


def _convert_nifti(job: Job, files: list[tuple[int, bytes, str]], target: Path) -> None:
    import SimpleITK as sitk

    if len(files) != 1:
        raise JobFailure("convert_ambiguous", "A NIfTI part must be one file")
    source_id, rel_path, _ = files[0]
    image = sitk.ReadImage(str(_source_file(job, source_id, rel_path)), imageIO="NiftiImageIO")
    sitk.WriteImage(image, str(target))


def run(job: Job, channel: ProtocolChannel) -> None:
    payload = job.payload
    try:
        series_key = job.series_key()
        catalog = job.project_path(str(payload["catalog"]))
        catalog_id = str(payload["catalog_id"])
        part_ref = int(payload["part_ref"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"convert payload is incomplete: {exc}") from exc

    channel.send(Progress(job.job_id, "convert", None, "Reading the index"))
    files = part_files(catalog, catalog_id, part_ref)
    if not files:
        raise JobFailure(
            "part_not_found", "The index has no files for this series", recoverable=True
        )

    work = job.project_path(f"work/{series_key}")
    work.mkdir(parents=True, exist_ok=True)
    scratch = scratch_dir(job)
    scratch.mkdir(parents=True, exist_ok=True)
    target = work / "ct.nii"
    # Hidden while it is checked, and with the extension SimpleITK reads by.
    partial = work / ".partial.ct.nii"
    flags: list[str] = []

    if all(kind == "nifti" for _, _, kind in files):
        channel.send(Progress(job.job_id, "convert", None, "Reading the NIfTI file"))
        _convert_nifti(job, files, partial)
    else:
        channel.send(
            Progress(job.job_id, "convert", 0.0, f"Copying {len(files)} files of the series")
        )
        _copy_inputs(job, files, scratch / "dicom")
        channel.send(Progress(job.job_id, "convert", 0.5, "Converting with dcm2niix"))
        try:
            outputs, warning_flags = _run_dcm2niix(job, scratch)
            chosen, chosen_flags = choose_output(outputs)
        except JobFailure as failure:
            # A multi-frame file has no stack for SimpleITK to read; dcm2niix
            # is its only converter.
            if failure.code not in ("convert_ambiguous", "convert_failed") or len(files) < 2:
                raise
            channel.send(
                Log("warning", f"{series_key}: dcm2niix split the series; read as one stack")
            )
            convert_stack_with_simpleitk(scratch / "dicom", partial)
            flags.append("converter_fallback")
        else:
            flags += warning_flags + chosen_flags
            shutil.move(str(chosen), partial)
            sidecar = chosen.with_suffix(".json")
            if sidecar.is_file():
                shutil.move(str(sidecar), work / "ct.json")

    channel.send(Progress(job.job_id, "check", None, "Checking geometry and Hounsfield units"))
    try:
        checked = check_volume(partial)
    except JobFailure:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(target)
    flags = sorted(set(flags + checked.pop("flags")))
    if flags:
        channel.send(Log("info", f"{series_key}: flags {', '.join(flags)}"))
    (work / "convert.json").write_text(
        json.dumps(
            {
                "dcm2niix": DCM2NIIX_VERSION,
                "flags_used": list(DCM2NIIX_FLAGS),
                "pipeline_version": PIPELINE_VERSION,
                "files": len(files),
                "qc_flags": flags,
                **checked,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    relative = target.relative_to(job.project_dir.resolve()).as_posix()
    digest = sha256_file(target)
    channel.send(Artifact("nifti", relative, digest))
    channel.send(
        Result(
            job.job_id,
            {
                "series_key": series_key,
                "nifti": relative,
                "sha256": digest,
                "files": len(files),
                "flags": flags,
                "dcm2niix": DCM2NIIX_VERSION,
                **checked,
            },
        )
    )
