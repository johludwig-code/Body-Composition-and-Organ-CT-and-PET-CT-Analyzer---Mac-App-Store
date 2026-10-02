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
    sys.path.insert(0, str(site))
    yield site
    sys.path.remove(str(site))
    for name in list(sys.modules):
        if name.split(".")[0] in ("moosez", "requests", "nnunetv2"):
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
