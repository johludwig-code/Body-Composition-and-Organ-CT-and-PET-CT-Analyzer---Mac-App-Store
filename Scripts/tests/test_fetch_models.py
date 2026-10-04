"""What `fetch_models.prune` removes from a model folder before it is signed."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_models


def test_prune_keeps_what_nnunet_reads_and_drops_the_rest(tmp_path: Path) -> None:
    trainer = tmp_path / "Dataset778_Body_composition" / "nnUNetTrainer__nnUNetPlans__3d_fullres"
    fold = trainer / "fold_all"
    (fold / "validation").mkdir(parents=True)
    (fold / "validation" / "case.nii.gz").write_bytes(b"prediction")
    for name in ("checkpoint_final.pth", "checkpoint_best.pth", "debug.json", "progress.png"):
        (fold / name).write_bytes(b"x")
    for name in ("dataset.json", "plans.json", "dataset_fingerprint.json"):
        (trainer / name).write_text("{}")
    # AppleDouble files exactly as the MOOSE archives carry them.
    model = tmp_path / "Dataset778_Body_composition"
    (model / "._nnUNetTrainer__nnUNetPlans__3d_fullres").write_bytes(b"\0" * 4096)
    (trainer / "._plans.json").write_bytes(b"\0" * 4096)
    (fold / "._checkpoint_final.pth").write_bytes(b"\0" * 4096)
    (trainer / ".DS_Store").write_bytes(b"\0")

    fetch_models.prune(tmp_path / "Dataset778_Body_composition")

    left = sorted(p.relative_to(trainer).as_posix() for p in trainer.rglob("*") if p.is_file())
    assert left == [
        "dataset.json",
        "dataset_fingerprint.json",
        "fold_all/checkpoint_final.pth",
        "plans.json",
    ]
    assert not list(tmp_path.rglob("._*"))
