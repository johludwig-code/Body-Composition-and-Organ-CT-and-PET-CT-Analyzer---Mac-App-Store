"""CI only: what the sandboxed worker made of the real case (real_case.sh).

Runs with the runtime's own interpreter, so the volumes and densities come
from the same `bcoa_worker.metrics` the app will put into the export. Prints
one line per model and the per-label values, and fails if a model failed or
its labelmap is not on the CT's grid.

    python3 -I label_summary.py <probe project> <work dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import SimpleITK as sitk

from bcoa_worker.metrics import compute_label_metrics


def events_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(project: Path, work: Path) -> int:
    ct_image = sitk.ReadImage(str(project / "work/s_ct/ct.nii.gz"))
    ct = sitk.GetArrayFromImage(ct_image).astype(np.float64)
    spacing = ct_image.GetSpacing()
    print(json.dumps({"ct_size": ct_image.GetSize(), "spacing_mm": [round(s, 4) for s in spacing]}))
    failed = []
    for events_file in sorted(work.glob("events_*.jsonl")):
        model = events_file.stem.removeprefix("events_")
        events = events_of(events_file)
        results = [e["payload"] for e in events if e.get("type") == "result"]
        status = events[-1].get("status") if events else "no output"
        if status != "ok" or not results:
            errors = [e["payload"] for e in events if e.get("type") == "error"]
            print(json.dumps({"model": model, "status": status, "errors": errors}))
            failed.append(model)
            continue
        result = results[-1]
        labels = {int(k): v for k, v in result["labels"].items()}
        label_image = sitk.ReadImage(str(project / f"work/s_ct/labels/{model}.nii.gz"))
        same_grid = (
            label_image.GetSize() == ct_image.GetSize()
            and np.allclose(label_image.GetSpacing(), spacing)
            and np.allclose(label_image.GetOrigin(), ct_image.GetOrigin())
            and np.allclose(label_image.GetDirection(), ct_image.GetDirection())
        )
        metrics = compute_label_metrics(
            ct, sitk.GetArrayFromImage(label_image), spacing, sorted(labels)
        )
        print(
            json.dumps(
                {
                    "model": model,
                    "status": status,
                    "device": result["device"],
                    "device_fallback": result["device_fallback"],
                    "same_grid": bool(same_grid),
                    "labels": len(labels),
                    "present": sum(m.voxel_count > 0 for m in metrics),
                }
            )
        )
        for m in metrics:
            hu = "-" if m.hu_mean is None else f"{m.hu_mean:.1f}"
            name = labels[m.label_id]
            print(f"  {name:<32} {m.voxel_count:>10} vox {m.volume_ml:>10.1f} ml {hu:>8} HU")
        if not same_grid:
            failed.append(model)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
