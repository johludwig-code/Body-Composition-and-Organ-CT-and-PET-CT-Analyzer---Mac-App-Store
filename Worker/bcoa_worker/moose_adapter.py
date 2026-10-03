"""The only place that knows how moosez works inside. See ADR 0011.

moosez 3.2.2 was written for a writable pip install with network access: it
keeps weights inside its own package folder, downloads them on first use,
deletes a model folder whose version file it does not recognise, and copies a
trainer file into nnU-Net's package on every call. In a signed, sandboxed,
read-only bundle each of those is either impossible or a violation, so this
module redirects them before MOOSE runs. MOOSE's sources are not modified.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
from collections.abc import Iterable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

# The adapter relies on private details of exactly this release (the default
# argument of Model.__init__, module-level `requests` imports, the name of the
# trainer helper). Any other version must fail the build, not a user's batch.
SUPPORTED_MOOSEZ = "3.2.2"

MODELS_SUBDIR = Path("models")
WEIGHTS_SUBDIR = MODELS_SUBDIR / "nnunet_trained_models"
MANIFEST_NAME = "manifest.json"
VERSION_FILE = "model_version.json"
TRAINER_FILE = "MOOSE_custom_trainers.py"


class AdapterError(RuntimeError):
    code = "moose_adapter"


class UnsupportedMooseVersionError(AdapterError):
    code = "unsupported_moosez"


class ModelNotInBundleError(AdapterError):
    code = "model_not_in_bundle"


class BundleModifiedError(AdapterError):
    code = "bundle_modified"


class _NoNetwork:
    """Stands in for the `requests` module inside moosez.

    The sandbox has no network entitlement, so a real request would fail
    anyway — but as a slow, obscure ConnectionError. This fails at once and
    says what is actually wrong.
    """

    def __getattr__(self, name: str) -> Any:
        def refuse(*args: object, **kwargs: object) -> None:
            target = next((a for a in args if isinstance(a, str)), "")
            model = target.rsplit("/", 1)[-1] if target else "unknown"
            raise ModelNotInBundleError(f"Model not found in app bundle: {model}")

        return refuse


class _Finished:
    """An AsyncResult whose task has already run."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def ready(self) -> bool:
        return True

    def get(self, timeout: float | None = None) -> Any:
        return self._value


class _InPlacePool:
    """A pool without workers: each task runs at once, in the calling thread.

    nnU-Net asks `is_alive()` of every worker in `_pool` and holds back while
    `not_ready >= len(_pool) + 2`; with no workers and every result ready,
    neither check ever stops it.
    """

    def __init__(self, processes: int | None = None) -> None:
        self._pool: list[Any] = []

    def __enter__(self) -> _InPlacePool:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def apply_async(
        self, func: Any, args: tuple[Any, ...] = (), kwds: dict[str, Any] | None = None
    ) -> _Finished:
        return _Finished(func(*args, **(kwds or {})))


class _InPlaceContext:
    # Capitalised because nnU-Net calls `get_context("spawn").Pool(...)`.
    @staticmethod
    def Pool(processes: int | None = None) -> _InPlacePool:
        return _InPlacePool(processes)


class _ExportInPlace:
    """Stands in for the `multiprocessing` module inside nnU-Net's predictor.

    nnU-Net resamples each predicted chunk in a pool of spawned processes. A
    pool needs POSIX semaphores, and the App Sandbox refuses `sem_open` for
    any name outside an app group: the first sandboxed run on Apple Silicon
    failed there with EPERM (ADR 0015). Every other attribute is the real
    module's.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(multiprocessing, name)

    @staticmethod
    def get_context(method: str | None = None) -> _InPlaceContext:
        return _InPlaceContext()


def weights_dir(resources_dir: Path) -> Path:
    return resources_dir / WEIGHTS_SUBDIR


def load_manifest(resources_dir: Path) -> dict[str, Any]:
    path = resources_dir / MODELS_SUBDIR / MANIFEST_NAME
    if not path.is_file():
        raise ModelNotInBundleError("Model manifest missing from app bundle")
    return json.loads(path.read_text(encoding="utf-8"))


def check_moosez_version() -> str:
    try:
        found = version("moosez")
    except PackageNotFoundError as exc:
        raise UnsupportedMooseVersionError("moosez is not installed in the bundle") from exc
    if found != SUPPORTED_MOOSEZ:
        raise UnsupportedMooseVersionError(
            f"moosez {found} found, adapter is written for {SUPPORTED_MOOSEZ}"
        )
    return found


def required_models(
    models: Iterable[str], workflow_registry: dict[str, list[dict[str, Any]]]
) -> list[str]:
    """Every model a selection needs, dependencies first, without duplicates.

    `clin_ct_body_composition` runs `clin_ct_fast_vertebrae` to crop its field
    of view; bundling the one without the other produces a download attempt at
    the user's desk.
    """
    ordered: list[str] = []
    for model in models:
        steps = workflow_registry.get(model, [{"model": model}])
        for step in steps:
            name = step["model"]
            if name not in ordered:
                ordered.append(name)
    return ordered


def trainer_target() -> Path:
    spec = importlib.util.find_spec("nnunetv2")
    if spec is None or not spec.submodule_search_locations:
        raise AdapterError("nnunetv2 is not installed in the bundle")
    package_dir = Path(next(iter(spec.submodule_search_locations)))
    return package_dir / "training" / "nnUNetTrainer" / "variants" / TRAINER_FILE


def _trainer_already_installed() -> str:
    """Replacement for moosez's add_custom_trainers_to_local_nnunetv2.

    The original copies a file into nnU-Net's package on every call, which in
    a signed bundle is a write into the code signature. The build installs the
    file before signing; here it is only checked.
    """
    target = trainer_target()
    if not target.is_file():
        raise BundleModifiedError(f"{TRAINER_FILE} missing from the bundled nnU-Net")
    return f"Custom trainer already installed: {target.name}."


def check_model_folder(model: str, folder: str, url: str, root: Path) -> Path:
    """The folder must exist and carry the version file MOOSE expects.

    Without `model_version.json` naming exactly `url`, MOOSE deletes the
    folder (`shutil.rmtree`) and downloads it again.
    """
    directory = root / folder
    version_file = directory / VERSION_FILE
    if not directory.is_dir():
        raise ModelNotInBundleError(f"Model not found in app bundle: {model}")
    try:
        recorded = json.loads(version_file.read_text(encoding="utf-8")).get("url")
    except (OSError, ValueError) as exc:
        raise BundleModifiedError(f"{model}: {VERSION_FILE} missing or unreadable") from exc
    if recorded != url:
        raise BundleModifiedError(
            f"{model}: bundled weights do not match moosez {SUPPORTED_MOOSEZ}"
        )
    return directory


def harden(resources_dir: Path) -> None:
    """Point moosez at the bundle and take away every way it has to change it.

    Idempotent; call before the first `moose()` of a process.
    """
    check_moosez_version()
    import moosez.download as moose_download
    import moosez.models as moose_models
    import moosez.moosez as moose_main

    root = weights_dir(resources_dir)
    init = moose_models.Model.__init__
    defaults = init.__defaults__ or ()
    if len(defaults) != 1:
        raise UnsupportedMooseVersionError("Model.__init__ no longer has one default argument")
    # The default was bound to system.MODELS_DIRECTORY_PATH at import time;
    # Workflow constructs Model without passing it. Replacing the default is
    # the only switch that reaches every Model the workflows create.
    init.__defaults__ = (str(root),)
    moose_models.requests = _NoNetwork()
    moose_download.requests = _NoNetwork()
    moose_main.add_custom_trainers_to_local_nnunetv2 = _trainer_already_installed

    import nnunetv2.inference.predict_from_raw_data as nnunet_predict

    current = getattr(nnunet_predict, "multiprocessing", None)
    if current is not multiprocessing and not isinstance(current, _ExportInPlace):
        raise AdapterError("nnU-Net's predictor no longer imports multiprocessing as a module")
    nnunet_predict.multiprocessing = _ExportInPlace()

    # tqdm, for nnU-Net's progress bars, asks for a multiprocessing lock when
    # the first bar is made. The sandbox refuses it and tqdm carries on with
    # its thread lock alone; settling that beforehand spares the refusal,
    # which the CI's sandbox report counts as a failure (ADR 0015).
    from tqdm.std import TqdmDefaultWriteLock

    TqdmDefaultWriteLock.mp_lock = None


def verify_bundled_model(model: str, resources_dir: Path) -> Path:
    import moosez.models as moose_models

    metadata = moose_models.MODEL_METADATA.get(model)
    if metadata is None:
        raise ModelNotInBundleError(f"Unknown model: {model}")
    return check_model_folder(
        model, metadata["folder_name"], metadata["url"], weights_dir(resources_dir)
    )


def choose_accelerator(requested: str) -> str:
    if requested in ("cpu", "mps"):
        return requested
    import torch

    return "mps" if torch.backends.mps.is_available() else "cpu"


def segment(
    input_nifti: Path, model: str, out_dir: Path, accelerator: str
) -> tuple[Path, dict[int, str]]:
    """Run one model on one NIfTI path. Returns the labelmap and its labels.

    A path, not a SimpleITK image: only for path input does moosez reorient to
    RAS and the result back to the input's orientation, which keeps the
    labelmap on the CT's grid.
    """
    from moosez import moose

    out_dir.mkdir(parents=True, exist_ok=True)
    outputs, used = moose(str(input_nifti), [model], str(out_dir), accelerator)
    if len(outputs) != 1:
        raise AdapterError(f"{model}: expected one labelmap, got {len(outputs)}")
    # MOOSE drops background by comparing each dataset.json value with the
    # string "0". Nine of the ten clinical models store their labels as
    # strings; the lungs model (Dataset333_HMS3dlungs) stores integers, so its
    # background came through as an organ and the export of the real case
    # carried a 217-litre "lungs / background" row. Label 0 is never a
    # structure, whatever type the model wrote it as.
    labels = {int(k): str(v) for k, v in used[0].organ_indices.items() if int(k) != 0}
    return Path(outputs[0]), labels


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()
