"""The viewer's copy of one series, in canonical LPS (plan §8 stage 5, ADR 0007).

Payload: `series_key`, `ct` and `labelmaps` (model → path), all
project-relative, and `cache_dir`, the run's folder for the series
(`viewer-cache/<run_id>/<series_key>`, ADR 0031 decision 11). Writes there: `image.i16` (the CT,
little-endian int16), `labels_<model>.u8` or `.u16`, and `meta.json` with the
grid. The app memory-maps the raw files and never reads NIfTI, so every
question of orientation is answered here, once: after `DICOMOrient(…, "LPS")`
index i runs towards the patient's left, j towards posterior, k towards
superior, with i fastest in memory.
"""

from __future__ import annotations

import json
import shutil
from typing import Any

from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.jobs.metrics import on_ct_grid
from bcoa_worker.moose_adapter import sha256_file
from bcoa_worker.protocol import Artifact, Job, Progress, ProtocolError, Result

CACHE_VERSION = 1


def lps(image: Any) -> Any:
    import SimpleITK as sitk

    return sitk.DICOMOrient(image, "LPS")


def is_identity(direction: tuple[float, ...]) -> bool:
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    return all(abs(a - b) < 1e-4 for a, b in zip(direction, identity, strict=True))


def run(job: Job, channel: ProtocolChannel) -> None:
    import numpy as np
    import SimpleITK as sitk

    payload = job.payload
    try:
        series_key = str(payload["series_key"])
        ct_path = job.project_path(str(payload["ct"]))
        labelmaps = {
            str(m): job.project_path(str(p)) for m, p in (payload.get("labelmaps") or {}).items()
        }
    except (KeyError, TypeError, AttributeError) as exc:
        raise ProtocolError(f"viewer cache payload is incomplete: {exc}") from exc

    folder = job.project_path(str(payload.get("cache_dir") or f"viewer-cache/{series_key}"))
    partial = folder.with_name(f".{series_key}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)

    channel.send(Progress(job.job_id, "viewer_cache", 0.0, "Reorienting the CT"))
    ct = sitk.ReadImage(str(ct_path))
    oriented = lps(ct)
    array = sitk.GetArrayViewFromImage(oriented)
    np.clip(array, -32768, 32767).astype("<i2").tofile(partial / "image.i16")

    models: dict[str, Any] = {}
    for index, (model, path) in enumerate(sorted(labelmaps.items()), start=1):
        channel.send(
            Progress(
                job.job_id, "viewer_cache", index / (len(labelmaps) + 1), "Labels", model=model
            )
        )
        labelmap, resampled = on_ct_grid(sitk.ReadImage(str(path)), ct)
        # A copy, not a view: a view of the temporary that lps() returns points
        # into memory freed with it. On Linux CI that read back as noise;
        # on the Mac it happened to survive.
        oriented_labels = lps(labelmap)
        labels = sitk.GetArrayFromImage(oriented_labels)
        top = int(labels.max()) if labels.size else 0
        dtype, suffix = ("<u1", "u8") if top < 256 else ("<u2", "u16")
        name = f"labels_{model}.{suffix}"
        labels.astype(dtype).tofile(partial / name)
        present = sorted(int(v) for v in np.unique(labels) if v != 0)
        models[model] = {"file": name, "present": present, "resampled": resampled}

    meta = {
        "version": CACHE_VERSION,
        "series_key": series_key,
        # x, y, z as SimpleITK counts them: i fastest.
        "size": list(oriented.GetSize()),
        "spacing_mm": [float(s) for s in oriented.GetSpacing()],
        "origin_mm": [float(o) for o in oriented.GetOrigin()],
        "direction": [float(d) for d in oriented.GetDirection()],
        # An oblique acquisition stays oblique after reorienting; the viewer
        # says so instead of drawing it as if it were axial (plan §8).
        "oblique": not is_identity(oriented.GetDirection()),
        "image": {"file": "image.i16", "dtype": "int16"},
        "models": models,
    }
    (partial / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    shutil.rmtree(folder, ignore_errors=True)
    partial.rename(folder)
    relative = (folder / "meta.json").relative_to(job.project_dir.resolve()).as_posix()
    channel.send(Artifact("viewer_cache", relative, sha256_file(folder / "meta.json")))
    channel.send(
        Result(
            job.job_id,
            {
                "series_key": series_key,
                "meta": relative,
                "oblique": meta["oblique"],
                "models": sorted(models),
            },
        )
    )
