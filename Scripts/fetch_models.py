#!/usr/bin/env python3
"""Fetch MOOSE weights at build time and lay them out for the bundle.

Run with the bundled interpreter (it has the pinned moosez, whose
MODEL_METADATA is the only source of model URLs):

    build/runtime/python/bin/python3 Scripts/fetch_models.py clin_ct_organs
    build/runtime/python/bin/python3 Scripts/fetch_models.py --clinical

Produces build/models/nnunet_trained_models/<Dataset…>/ and
build/models/manifest.json. Every zip is checked against
Models/manifest.lock.json; a model that is not in the lock fails the build
unless --update-lock is given, which records it (trust on first use, done once
by a person and committed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Worker"))

from bcoa_worker.moose_adapter import (  # noqa: E402
    SUPPORTED_MOOSEZ,
    VERSION_FILE,
    check_moosez_version,
    required_models,
)

# The clinical CT models of plan appendix A. 144 labels together.
CLINICAL = [
    "clin_ct_organs",
    "clin_ct_cardiac",
    "clin_ct_lungs",
    "clin_ct_digestive",
    "clin_ct_muscles",
    "clin_ct_ribs",
    "clin_ct_vertebrae",
    "clin_ct_peripheral_bones",
    "clin_ct_body",
    "clin_ct_body_composition",
]

# Never needed for inference, and some are large: nnU-Net keeps its
# validation predictions (NIfTI) inside every fold. Whether removing them
# changes nothing is checked in spike S1 (identical labelmaps before/after).
PRUNE_DIRS = {"validation"}
PRUNE_FILES = {"checkpoint_best.pth", "checkpoint_latest.pth", "progress.png", "debug.json"}
PRUNE_SUFFIXES = (".log", ".txt")
# The archives were zipped on a Mac and carry AppleDouble files (`._name`),
# 4 KB each, next to their entries. They are resource-fork
# leftovers, not data, and nothing in a signed bundle should be unexplained.
PRUNE_PREFIXES = ("._",)
PRUNE_NAMES = {".DS_Store"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    print(f"[models] downloading {target.name}", flush=True)
    with urllib.request.urlopen(url) as response, partial.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out, 1 << 20)
    partial.rename(target)


def prune(folder: Path) -> list[str]:
    removed: list[str] = []
    for path in sorted(folder.rglob("*"), reverse=True):
        if path.is_dir() and path.name in PRUNE_DIRS:
            shutil.rmtree(path)
            removed.append(path.relative_to(folder).as_posix())
        elif path.is_file() and (
            path.name in PRUNE_FILES
            or path.name in PRUNE_NAMES
            or path.name.endswith(PRUNE_SUFFIXES)
            or path.name.startswith(PRUNE_PREFIXES)
        ):
            path.unlink()
            removed.append(path.relative_to(folder).as_posix())
    return removed


def labels_of(folder: Path) -> dict[str, str]:
    for dataset in folder.rglob("dataset.json"):
        labels = json.loads(dataset.read_text()).get("labels", {})
        return {str(v): k for k, v in labels.items() if str(v) != "0"}
    raise SystemExit(f"{folder.name}: no dataset.json; MOOSE cannot run this model")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="*")
    parser.add_argument("--clinical", action="store_true", help="all clinical CT models")
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "models")
    parser.add_argument("--cache", type=Path, default=ROOT / "build" / "cache" / "models")
    parser.add_argument("--lock", type=Path, default=ROOT / "Models" / "manifest.lock.json")
    parser.add_argument("--update-lock", action="store_true")
    parser.add_argument("--no-slim", action="store_true", help="keep optimizer state")
    args = parser.parse_args()

    check_moosez_version()
    from moosez.models import MODEL_METADATA
    from moosez.workflows import WORKFLOW_REGISTRY

    wanted = (CLINICAL if args.clinical else []) + args.models
    if not wanted:
        parser.error("name models or use --clinical")
    models = required_models(wanted, WORKFLOW_REGISTRY)

    lock: dict[str, Any] = (
        json.loads(args.lock.read_text())
        if args.lock.exists()
        else {"moosez": SUPPORTED_MOOSEZ, "models": {}}
    )
    if lock.get("moosez") != SUPPORTED_MOOSEZ:
        raise SystemExit(f"lock is for moosez {lock.get('moosez')}, adapter for {SUPPORTED_MOOSEZ}")

    weights = args.out / "nnunet_trained_models"
    weights.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for model in models:
        meta = MODEL_METADATA[model]
        url, folder_name = meta["url"], meta["folder_name"]
        archive = args.cache / url.rsplit("/", 1)[-1]
        if not archive.exists():
            download(url, archive)
        digest = sha256(archive)
        locked = lock["models"].get(model)
        if locked is None:
            if not args.update_lock:
                raise SystemExit(f"{model} is not in {args.lock.name}; rerun with --update-lock")
            lock["models"][model] = {"url": url, "zip_sha256": digest}
        elif locked["url"] != url or locked["zip_sha256"] != digest:
            raise SystemExit(f"{model}: archive differs from {args.lock.name}")

        folder = weights / folder_name
        shutil.rmtree(folder, ignore_errors=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(weights)
        if not folder.is_dir():
            raise SystemExit(f"{model}: archive did not contain {folder_name}/")
        # Without this exact file MOOSE deletes the folder and downloads it
        # again (ADR 0011).
        (folder / VERSION_FILE).write_text(json.dumps({"url": url}))
        removed = prune(folder)

        slimmed: list[dict[str, str]] = []
        if not args.no_slim:
            from slim_checkpoints import slim

            for checkpoint in sorted(folder.rglob("checkpoint_final.pth")):
                before, after = slim(checkpoint)
                rel = checkpoint.relative_to(weights).as_posix()
                slimmed.append({"file": rel, "sha256_before": before, "sha256_after": after})

        files = {
            p.relative_to(weights).as_posix(): sha256(p)
            for p in sorted(folder.rglob("*"))
            if p.is_file()
        }
        entries.append(
            {
                "name": model,
                "folder": folder_name,
                "url": url,
                "zip_sha256": digest,
                "requires": [
                    s["model"] for s in WORKFLOW_REGISTRY.get(model, []) if s["model"] != model
                ],
                "labels": labels_of(folder),
                "pruned": removed,
                "slimmed": slimmed,
                "files": files,
                "licence": "CC-BY-4.0",
            }
        )
        print(f"[models] {model}: {len(files)} files", flush=True)

    manifest = {"manifest_version": 1, "moosez": SUPPORTED_MOOSEZ, "models": entries}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    if args.update_lock:
        args.lock.write_text(json.dumps(lock, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
