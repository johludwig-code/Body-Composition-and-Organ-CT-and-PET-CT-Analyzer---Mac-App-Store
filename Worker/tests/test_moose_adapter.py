"""The adapter's assumptions about moosez 3.2.2, held against a fake with the
same shape (module layout, Model.__init__ default, module-level `requests`,
the trainer helper imported by name into moosez.moosez). The real package is
exercised by spike S1 and by Self-Test on a Mac."""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from bcoa_worker import moose_adapter

URL = "https://github.com/ENHANCE-PET/MOOSE/releases/download/moosez-v.3.1.3/clin_ct_organs_ras_07052025.zip"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


@pytest.fixture
def fake_site(tmp_path: Path) -> Iterator[Path]:
    site = tmp_path / "site-packages"
    _write(site / "moosez" / "__init__.py", "from .moosez import moose\n")
    _write(
        site / "moosez" / "models.py",
        f"""
        import requests
        MODEL_METADATA = {{
            "clin_ct_organs": {{"url": "{URL}", "folder_name": "Dataset123_Organs"}},
        }}
        class Model:
            def __init__(self, model_identifier, output_manager, base_directory="/site/moosez/models"):
                self.base_directory = base_directory
        """,
    )
    _write(site / "moosez" / "download.py", "import requests\n")
    _write(
        site / "requests" / "__init__.py",
        "def get(*a, **k):\n    raise AssertionError('network used')\n",
    )
    _write(
        site / "moosez" / "moosez.py",
        """
        from moosez.nnUNet_custom_trainer.utility import add_custom_trainers_to_local_nnunetv2
        def moose(*args, **kwargs):
            return add_custom_trainers_to_local_nnunetv2()
        """,
    )
    _write(
        site / "moosez" / "nnUNet_custom_trainer" / "utility.py",
        "def add_custom_trainers_to_local_nnunetv2():\n    raise AssertionError('wrote into nnunetv2')\n",
    )
    _write(site / "moosez" / "nnUNet_custom_trainer" / "__init__.py", "")
    _write(
        site / "moosez-3.2.2.dist-info" / "METADATA",
        "Metadata-Version: 2.4\nName: moosez\nVersion: 3.2.2\n",
    )
    _write(site / "nnunetv2" / "__init__.py", "")
    _write(site / "tqdm" / "__init__.py", "")
    # tqdm 4.70.1's lock: a multiprocessing RLock on first use, None if refused.
    _write(
        site / "tqdm" / "std.py",
        """
        from multiprocessing import RLock
        class TqdmDefaultWriteLock:
            def __init__(self):
                self.create_mp_lock()
            @classmethod
            def create_mp_lock(cls):
                if not hasattr(cls, "mp_lock"):
                    try:
                        cls.mp_lock = RLock()
                    except OSError:
                        cls.mp_lock = None
        """,
    )
    _write(site / "nnunetv2" / "inference" / "__init__.py", "")
    # The shape of nnunetv2 2.8.1's predict_from_data_iterator: a spawn pool,
    # its workers' liveness, apply_async, get with a timeout.
    _write(
        site / "nnunetv2" / "inference" / "predict_from_raw_data.py",
        """
        import multiprocessing
        def predict_from_data_iterator(chunks):
            with multiprocessing.get_context("spawn").Pool(8) as export_pool:
                worker_list = [i for i in export_pool._pool]
                r = [export_pool.apply_async(sum, (chunk,)) for chunk in chunks]
                results = []
                for result in r:
                    assert all(j.is_alive() for j in worker_list)
                    try:
                        results.append(result.get(timeout=0.1))
                    except multiprocessing.TimeoutError:
                        raise AssertionError("not finished")
            return results
        """,
    )
    sys.path.insert(0, str(site))
    yield site
    sys.path.remove(str(site))
    for name in list(sys.modules):
        if name.split(".")[0] in ("moosez", "requests", "nnunetv2", "tqdm"):
            del sys.modules[name]


@pytest.fixture
def resources(tmp_path: Path) -> Path:
    root = tmp_path / "Resources"
    model = root / "models" / "nnunet_trained_models" / "Dataset123_Organs"
    model.mkdir(parents=True)
    (model / "model_version.json").write_text(json.dumps({"url": URL}))
    return root


def _install_trainer(site: Path) -> None:
    _write(
        site / "nnunetv2" / "training" / "nnUNetTrainer" / "variants" / "MOOSE_custom_trainers.py",
        "",
    )


def test_harden_points_every_model_at_the_bundle(fake_site: Path, resources: Path) -> None:
    moose_adapter.harden(resources)
    import moosez.models

    assert moosez.models.Model("clin_ct_organs", None).base_directory == str(
        resources / "models" / "nnunet_trained_models"
    )


def test_harden_takes_the_network_away(fake_site: Path, resources: Path) -> None:
    moose_adapter.harden(resources)
    import moosez.download
    import moosez.models

    with pytest.raises(moose_adapter.ModelNotInBundleError, match="clin_ct_cardiac"):
        moosez.models.requests.get("https://example.org/x/clin_ct_cardiac.zip", stream=True)
    with pytest.raises(moose_adapter.ModelNotInBundleError):
        moosez.download.requests.get("https://example.org/data.zip")


def test_moose_never_copies_the_trainer(fake_site: Path, resources: Path) -> None:
    _install_trainer(fake_site)
    moose_adapter.harden(resources)
    from moosez import moose

    assert "already installed" in moose()


def test_missing_trainer_is_a_modified_bundle(fake_site: Path, resources: Path) -> None:
    moose_adapter.harden(resources)
    from moosez import moose

    with pytest.raises(moose_adapter.BundleModifiedError):
        moose()


@pytest.fixture
def refused(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the App Sandbox does to `sem_open` for a name outside an app group.

    The list collects every request, refused or not, so a test can hold that
    none was made at all: the kernel logs each one as a violation.
    """
    import multiprocessing.synchronize

    requests: list[str] = []

    def refuse(*args: object, **kwargs: object) -> None:
        requests.append("sem_open")
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(multiprocessing.synchronize.SemLock, "__init__", refuse)
    return requests


def test_a_spawn_pool_needs_a_semaphore(fake_site: Path, refused: list[str]) -> None:
    # The failure ADR 0015 is about, reproduced: without the adapter the
    # predictor cannot even open its pool.
    from nnunetv2.inference import predict_from_raw_data

    with pytest.raises(PermissionError):
        predict_from_raw_data.predict_from_data_iterator([[1, 2]])


def test_the_export_runs_without_a_semaphore(
    fake_site: Path, resources: Path, refused: list[str]
) -> None:
    moose_adapter.harden(resources)
    from nnunetv2.inference import predict_from_raw_data

    assert predict_from_raw_data.predict_from_data_iterator([[1, 2], [3, 4], [5]]) == [3, 7, 5]
    assert refused == []


def test_progress_bars_do_not_ask_for_a_semaphore(
    fake_site: Path, resources: Path, refused: list[str]
) -> None:
    moose_adapter.harden(resources)
    from tqdm.std import TqdmDefaultWriteLock

    TqdmDefaultWriteLock()
    assert refused == []


def test_the_predictor_keeps_the_rest_of_multiprocessing(fake_site: Path, resources: Path) -> None:
    import multiprocessing

    moose_adapter.harden(resources)
    moose_adapter.harden(resources)
    from nnunetv2.inference import predict_from_raw_data

    stand_in = predict_from_raw_data.multiprocessing
    assert stand_in is not multiprocessing
    assert stand_in.TimeoutError is multiprocessing.TimeoutError
    assert stand_in.cpu_count() == multiprocessing.cpu_count()


def test_a_predictor_of_another_shape_is_refused(fake_site: Path, resources: Path) -> None:
    _write(
        fake_site / "nnunetv2" / "inference" / "predict_from_raw_data.py",
        "from multiprocessing import get_context\n",
    )
    with pytest.raises(moose_adapter.AdapterError, match="multiprocessing"):
        moose_adapter.harden(resources)


def test_other_moosez_version_is_refused(fake_site: Path, resources: Path) -> None:
    (fake_site / "moosez-3.2.2.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: moosez\nVersion: 3.3.0\n"
    )
    with pytest.raises(moose_adapter.UnsupportedMooseVersionError):
        moose_adapter.harden(resources)


def test_bundled_model_with_matching_version_file(fake_site: Path, resources: Path) -> None:
    path = moose_adapter.verify_bundled_model("clin_ct_organs", resources)
    assert path.name == "Dataset123_Organs"


def test_version_file_with_other_url_would_make_moose_delete_the_folder(
    fake_site: Path, resources: Path
) -> None:
    version_file = resources / "models/nnunet_trained_models/Dataset123_Organs/model_version.json"
    version_file.write_text(json.dumps({"url": URL.replace("07052025", "01012025")}))
    with pytest.raises(moose_adapter.BundleModifiedError):
        moose_adapter.verify_bundled_model("clin_ct_organs", resources)


def test_model_not_in_bundle(fake_site: Path, tmp_path: Path) -> None:
    with pytest.raises(moose_adapter.ModelNotInBundleError):
        moose_adapter.verify_bundled_model("clin_ct_organs", tmp_path / "empty")
    with pytest.raises(moose_adapter.ModelNotInBundleError):
        moose_adapter.verify_bundled_model("clin_ct_unknown", tmp_path / "empty")


def test_dependencies_come_first_and_once() -> None:
    registry = {
        "clin_ct_body_composition": [
            {"model": "clin_ct_fast_vertebrae", "role": "crop_FOV"},
            {"model": "clin_ct_body_composition", "role": "segmentation"},
        ]
    }
    assert moose_adapter.required_models(
        ["clin_ct_organs", "clin_ct_body_composition", "clin_ct_fast_vertebrae"], registry
    ) == ["clin_ct_organs", "clin_ct_fast_vertebrae", "clin_ct_body_composition"]
