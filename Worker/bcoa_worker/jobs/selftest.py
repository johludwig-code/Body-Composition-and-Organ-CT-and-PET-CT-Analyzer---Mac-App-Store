"""Settings → Self-Test: checksums of every bundled weight file, MPS, and
optionally a tiny inference on a synthetic volume."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bcoa_worker import moose_adapter
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.environment import scratch_dir
from bcoa_worker.errors import JobFailure
from bcoa_worker.protocol import Job, Log, Progress, Result


def verify_manifest(resources_dir: Path, channel: ProtocolChannel, job_id: str) -> list[str]:
    """Every file of every model against the SHA-256 recorded at build time.

    Returns the relative paths that differ. A mismatch means the bundle was
    altered after signing, and a pickle-based checkpoint from an altered bundle
    must not be loaded.
    """
    manifest = moose_adapter.load_manifest(resources_dir)
    root = moose_adapter.weights_dir(resources_dir)
    files = [
        (rel, digest) for model in manifest["models"] for rel, digest in model["files"].items()
    ]
    bad: list[str] = []
    for done, (rel, expected) in enumerate(files, start=1):
        path = root / rel
        if not path.is_file() or moose_adapter.sha256_file(path) != expected:
            bad.append(rel)
        channel.send(Progress(job_id, "selftest", done / len(files), "Verifying model checksums"))
    return bad


def accelerator_report() -> dict[str, Any]:
    try:
        import torch
    except ImportError:
        return {"torch": None, "mps_built": False, "mps_available": False}
    return {
        "torch": torch.__version__,
        "mps_built": bool(torch.backends.mps.is_built()),
        "mps_available": bool(torch.backends.mps.is_available()),
    }


def tiny_inference(job: Job, model: str) -> dict[str, Any]:
    import numpy as np
    import SimpleITK as sitk

    # A water cylinder in air: enough for nnU-Net to run every stage, small
    # enough to finish in seconds. The labels are not judged, only that a
    # labelmap on the input grid comes back.
    size = 64
    _, y, x = np.mgrid[0:size, 0:size, 0:size]
    body = ((y - size / 2) ** 2 + (x - size / 2) ** 2) < (size / 3) ** 2
    volume = np.where(body, 0, -1000).astype(np.int16)
    image = sitk.GetImageFromArray(volume)
    image.SetSpacing((3.0, 3.0, 3.0))
    scratch = scratch_dir(job)
    scratch.mkdir(parents=True, exist_ok=True)
    source = scratch / "selftest_ct.nii.gz"
    sitk.WriteImage(image, str(source))
    accelerator = moose_adapter.choose_accelerator("auto")
    produced, labels = moose_adapter.segment(source, model, scratch / "selftest_out", accelerator)
    result = sitk.ReadImage(str(produced))
    return {
        "model": model,
        "device": accelerator,
        "same_grid": result.GetSize() == image.GetSize(),
        "labels": len(labels),
    }


def run(job: Job, channel: ProtocolChannel) -> None:
    report: dict[str, Any] = {"accelerator": accelerator_report()}
    try:
        report["moosez"] = moose_adapter.check_moosez_version()
        bad = verify_manifest(job.resources_dir, channel, job.job_id)
    except moose_adapter.AdapterError as exc:
        raise JobFailure(exc.code, str(exc)) from exc
    report["checksum_mismatches"] = bad
    if bad:
        channel.send(Result(job.job_id, report))
        raise JobFailure("bundle_modified", f"{len(bad)} model files differ from the manifest")
    if not report["accelerator"]["mps_available"]:
        channel.send(Log("warning", "Apple GPU (MPS) not available; segmentation will use the CPU"))

    model = job.payload.get("inference_model")
    if job.payload.get("inference") and model:
        try:
            moose_adapter.harden(job.resources_dir)
            moose_adapter.verify_bundled_model(str(model), job.resources_dir)
        except moose_adapter.AdapterError as exc:
            raise JobFailure(exc.code, str(exc)) from exc
        channel.send(Progress(job.job_id, "selftest", None, "Running a test inference"))
        report["inference"] = tiny_inference(job, str(model))
    channel.send(Result(job.job_id, report))
