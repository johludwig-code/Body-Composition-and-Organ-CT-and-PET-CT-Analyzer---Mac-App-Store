#!/usr/bin/env python3
"""Remove optimizer state from nnU-Net checkpoints; keep the network unchanged.

nnU-Net 2.8.1 reads exactly four keys at inference
(`nnunetv2/inference/predict_from_raw_data.py`, lines 87–95):
`trainer_name`, `init_args` (for `configuration`),
`inference_allowed_mirroring_axes` and `network_weights`. The optimizer state
is about as large as the weights again and is dropped. Spike S1 proves the
step harmless: labelmaps before and after must be bit-identical.

The modification is recorded in the manifest (SHA-256 before and after) and
in the attribution, as CC BY 4.0 requires an indication of changes.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

KEEP = ("trainer_name", "init_args", "inference_allowed_mirroring_axes", "network_weights")
REQUIRED = ("trainer_name", "init_args", "network_weights")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def slim(path: Path) -> tuple[str, str]:
    import torch

    before = _sha256(path)
    # Pickle: loaded here only because this runs at build time on the
    # official release archive, verified against the lock before extraction.
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    missing = [key for key in REQUIRED if key not in checkpoint]
    if missing:
        raise SystemExit(f"{path}: checkpoint lacks {', '.join(missing)}")
    slimmed = {key: checkpoint[key] for key in KEEP if key in checkpoint}
    torch.save(slimmed, path)
    return before, _sha256(path)


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        old, new = slim(Path(argument))
        print(f"{argument}: {old[:12]} -> {new[:12]}")
