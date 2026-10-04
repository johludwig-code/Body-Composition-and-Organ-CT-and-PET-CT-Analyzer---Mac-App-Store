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


def read_ct(path: Path) -> tuple[sitk.Image, np.ndarray]:
    image = sitk.ReadImage(str(path))
    print(
        json.dumps(
            {
                "ct": path.parent.name,
                "size": image.GetSize(),
                "spacing_mm": [round(s, 4) for s in image.GetSpacing()],
            }
        )
    )
    return image, sitk.GetArrayFromImage(image).astype(np.float64)


def main(project: Path, work: Path) -> int:
    # A run tagged <model> covers the whole CT (series s_ct); one tagged
    # <model>__crop_<device> covers the crop real_case.sh cut (s_crop).
    images = {"s_ct": read_ct(project / "work/s_ct/ct.nii.gz")}
    if (project / "work/s_crop/ct.nii.gz").exists():
        images["s_crop"] = read_ct(project / "work/s_crop/ct.nii.gz")
    failed = []
    for events_file in sorted(work.glob("events_*.jsonl")):
        tag = events_file.stem.removeprefix("events_")
        model, _, run = tag.partition("__")
        series = f"s_{run}" if run else "s_ct"
        ct_image, ct = images["s_crop" if run.startswith("crop") else "s_ct"]
        spacing = ct_image.GetSpacing()
        events = events_of(events_file)
        results = [e["payload"] for e in events if e.get("type") == "result"]
        status = events[-1].get("status") if events else "no output"
        if status != "ok" or not results:
            errors = [e["payload"] for e in events if e.get("type") == "error"]
            print(json.dumps({"run": tag, "status": status, "errors": errors}))
            failed.append(tag)
            continue
        result = results[-1]
        labels = {int(k): v for k, v in result["labels"].items()}
        label_image = sitk.ReadImage(str(project / f"work/{series}/labels/{model}.nii.gz"))
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
                    "run": tag,
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
            failed.append(tag)
        if run == "crop_cpu" and (project / f"work/s_crop_mps/labels/{model}.nii.gz").exists():
            compare_devices(project, model, labels)
    return 1 if failed else 0


def compare_devices(project: Path, model: str, labels: dict[int, str]) -> None:
    """Dice and volume difference per label between MPS and CPU on the crop.

    Reported, not failed: below the plan's thresholds the plan asks for the
    cause to be documented, which a person does.
    """
    first = sitk.GetArrayFromImage(
        sitk.ReadImage(str(project / f"work/s_crop_mps/labels/{model}.nii.gz"))
    )
    cpu = sitk.GetArrayFromImage(
        sitk.ReadImage(str(project / f"work/s_crop_cpu/labels/{model}.nii.gz"))
    )
    print(f"  {model}: MPS against CPU on the crop")
    below = []
    for label_id, name in sorted(labels.items()):
        a, b = first == label_id, cpu == label_id
        total = int(a.sum()) + int(b.sum())
        dice = 1.0 if total == 0 else 2 * int((a & b).sum()) / total
        volume = 0.0 if not b.sum() else 100 * (int(a.sum()) - int(b.sum())) / int(b.sum())
        print(f"  {name:<32} Dice {dice:.4f}  volume {volume:+.2f} %")
        if dice < 0.99 or abs(volume) > 1:
            below.append(f"{model}/{name}")
    print(json.dumps({"identical": bool((first == cpu).all()), "below_plan": below}))


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
