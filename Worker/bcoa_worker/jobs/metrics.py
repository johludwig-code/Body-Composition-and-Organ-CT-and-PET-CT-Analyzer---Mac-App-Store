"""Metrics and automatic QC flags of one series (plan §8 stage 4, §10, §12).

Payload: `series_key`, `ct` (the converted CT, project-relative), `labelmaps`
(model → project-relative labelmap), optionally `devices` (model → the device
the segmentation used and whether it fell back to the CPU) and `flags`, the
series' flags from the conversion. Every label of every model's manifest
entry gets a row, so a structure MOOSE did not find is an empty row with
`empty_label`, never a missing one.
"""

from __future__ import annotations

import json
from typing import Any

from bcoa_worker import moose_adapter
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.errors import JobFailure
from bcoa_worker.metrics import compute_label_metrics
from bcoa_worker.protocol import Artifact, Job, Progress, ProtocolError, Result


def manifest_labels(job: Job) -> dict[str, dict[int, str]]:
    manifest = moose_adapter.load_manifest(job.resources_dir)
    return {
        str(entry["name"]): {int(k): str(v) for k, v in entry.get("labels", {}).items()}
        for entry in manifest["models"]
    }


def _same_grid(a: Any, b: Any) -> bool:
    import numpy as np

    return (
        a.GetSize() == b.GetSize()
        and np.allclose(a.GetSpacing(), b.GetSpacing(), atol=1e-4)
        and np.allclose(a.GetOrigin(), b.GetOrigin(), atol=1e-3)
        and np.allclose(a.GetDirection(), b.GetDirection(), atol=1e-5)
    )


def on_ct_grid(labelmap: Any, ct: Any) -> tuple[Any, bool]:
    """The labelmap on the CT's grid, and whether it had to be resampled
    (nearest neighbour, plan §8 stage 4)."""
    import SimpleITK as sitk

    if _same_grid(labelmap, ct):
        return labelmap, False
    resampled = sitk.Resample(
        labelmap, ct, sitk.Transform(), sitk.sitkNearestNeighbor, 0, labelmap.GetPixelID()
    )
    return resampled, True


def run(job: Job, channel: ProtocolChannel) -> None:
    import SimpleITK as sitk

    payload = job.payload
    try:
        series_key = str(payload["series_key"])
        ct_path = job.project_path(str(payload["ct"]))
        labelmaps = {str(m): job.project_path(str(p)) for m, p in payload["labelmaps"].items()}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ProtocolError(f"metrics payload is incomplete: {exc}") from exc
    devices: dict[str, Any] = dict(payload.get("devices") or {})
    series_flags = [str(f) for f in payload.get("flags") or []]

    labels_by_model = manifest_labels(job)
    ct = sitk.ReadImage(str(ct_path))
    ct_array = sitk.GetArrayViewFromImage(ct)
    spacing = ct.GetSpacing()

    rows: list[dict[str, Any]] = []
    for index, (model, path) in enumerate(sorted(labelmaps.items()), start=1):
        channel.send(
            Progress(job.job_id, "metrics", (index - 1) / len(labelmaps), "Measuring", model=model)
        )
        if not path.is_file():
            raise JobFailure("labelmap_missing", f"The labelmap of {model} is missing")
        labelmap, resampled = on_ct_grid(sitk.ReadImage(str(path)), ct)
        names = labels_by_model.get(model)
        if not names:
            raise JobFailure("model_unknown", f"{model} is not in the bundle's manifest")
        device = devices.get(model, {})
        model_flags = list(series_flags)
        if resampled:
            model_flags.append("labelmap_resampled")
        if device.get("fallback"):
            model_flags.append("device_fallback")
        measured = compute_label_metrics(
            ct_array, sitk.GetArrayViewFromImage(labelmap), spacing, sorted(names)
        )
        for metrics in measured:
            row = metrics.as_row()
            flags = [f for f in row.pop("flags").split(",") if f] + model_flags
            rows.append(
                {
                    "model": model,
                    "label_name": names[metrics.label_id],
                    **row,
                    "device": str(device.get("device", "unknown")),
                    "flags": sorted(set(flags)),
                }
            )

    target = job.project_path(f"work/{series_key}/metrics.json")
    target.write_text(json.dumps({"series_key": series_key, "rows": rows}, indent=1), "utf-8")
    channel.send(
        Artifact(
            "metrics",
            target.relative_to(job.project_dir.resolve()).as_posix(),
            moose_adapter.sha256_file(target),
        )
    )
    channel.send(Result(job.job_id, {"series_key": series_key, "rows": rows}))
