"""Segment one series with one or more MOOSE models, one model per call.

One call per model gives exact progress and isolates failures: a model that
fails leaves the labelmaps of the models before it in place.
"""

from __future__ import annotations

import shutil

from bcoa_worker import moose_adapter
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.environment import scratch_dir
from bcoa_worker.errors import JobFailure
from bcoa_worker.protocol import Artifact, Job, Log, Progress, Result


def run(job: Job, channel: ProtocolChannel) -> None:
    payload = job.payload
    series_key = str(payload["series_key"])
    source = job.project_path(str(payload["input_nifti"]))
    models = [str(m) for m in payload["models"]]
    requested = str(payload.get("device", "auto"))
    retry_on_cpu = bool(payload.get("retry_on_cpu", True))

    try:
        moose_adapter.harden(job.resources_dir)
        for model in models:
            moose_adapter.verify_bundled_model(model, job.resources_dir)
    except moose_adapter.AdapterError as exc:
        raise JobFailure(exc.code, str(exc)) from exc

    labels_dir = job.project_path(f"work/{series_key}/labels")
    labels_dir.mkdir(parents=True, exist_ok=True)
    accelerator = moose_adapter.choose_accelerator(requested)

    for index, model in enumerate(models, start=1):
        channel.send(
            Progress(job.job_id, "segment", None, f"Model {index} of {len(models)}", model=model)
        )
        out_dir = scratch_dir(job) / model
        device = accelerator
        try:
            produced, labels = moose_adapter.segment(source, model, out_dir, device)
        except moose_adapter.AdapterError as exc:
            raise JobFailure(exc.code, str(exc)) from exc
        except Exception:
            if device != "mps" or not retry_on_cpu:
                raise
            # MPS and CPU may give slightly different masks; the result
            # records the device actually used and auto-QC flags the fallback.
            channel.send(Log("warning", f"{model}: MPS failed, retrying on CPU"))
            device = "cpu"
            produced, labels = moose_adapter.segment(source, model, out_dir, device)

        target = labels_dir / f"{model}.nii.gz"
        shutil.move(str(produced), target)
        shutil.rmtree(out_dir, ignore_errors=True)
        relative = target.relative_to(job.project_dir.resolve()).as_posix()
        channel.send(Artifact("labelmap", relative, moose_adapter.sha256_file(target)))
        channel.send(
            Result(
                job.job_id,
                {
                    "series_key": series_key,
                    "model": model,
                    "device": device,
                    "device_fallback": device != accelerator,
                    "labels": {str(k): v for k, v in sorted(labels.items())},
                },
            )
        )
