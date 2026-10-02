"""The portable checks of verify_bundle.py on a fake bundle. The codesign
checks run only on macOS and are exercised by `make verify` there."""

from __future__ import annotations

import hashlib
import json
import plistlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_bundle


@pytest.fixture
def app(tmp_path: Path) -> Path:
    app = tmp_path / "BCOAnalyzer.app"
    resources = app / "Contents" / "Resources"
    weights = resources / "models" / "nnunet_trained_models" / "Dataset123_Organs"
    weights.mkdir(parents=True)
    checkpoint = weights / "checkpoint_final.pth"
    checkpoint.write_bytes(b"weights")
    manifest = {
        "models": [
            {
                "name": "clin_ct_organs",
                "files": {
                    "Dataset123_Organs/checkpoint_final.pth": hashlib.sha256(b"weights").hexdigest()
                },
            }
        ]
    }
    (resources / "models" / "manifest.json").write_text(json.dumps(manifest))
    (resources / "licenses").mkdir()
    (resources / "licenses" / "THIRD_PARTY_NOTICES.md").write_text("x" * 2000)
    (resources / "python" / "bin").mkdir(parents=True)
    (resources / "PrivacyInfo.xcprivacy").write_bytes(SHIPPED_PRIVACY.read_bytes())
    return app


ROOT = Path(__file__).resolve().parents[2]
SHIPPED_PRIVACY = ROOT / "App" / "Resources" / "PrivacyInfo.xcprivacy"
SITE = "Contents/Resources/python/lib/python3.12/site-packages"


def _macho(path: Path, filetype: int, universal: bool = False) -> Path:
    """A Mach-O header with nothing behind it: enough for the file type."""
    thin = bytes.fromhex("cffaedfe") + bytes.fromhex("0c000001") + bytes(4)
    thin += filetype.to_bytes(4, "little") + bytes(16)
    if universal:
        fat = bytes.fromhex("cafebabe") + (1).to_bytes(4, "big")
        fat += bytes.fromhex("0100000c") + bytes(4) + (64).to_bytes(4, "big")
        fat += len(thin).to_bytes(4, "big") + (14).to_bytes(4, "big")
        thin = fat.ljust(64, b"\0") + thin
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(thin)
    return path


def _entitlements(tmp_path: Path, **extra: bool) -> Path:
    path = tmp_path / "app.entitlements"
    path.write_bytes(plistlib.dumps({"com.apple.security.app-sandbox": True, **extra}))
    return path


def test_clean_bundle_passes(app: Path, tmp_path: Path) -> None:
    assert verify_bundle.verify(app, _entitlements(tmp_path)).failures == []
    assert (app.parent / "size-report.txt").exists()


def test_itms_services_anywhere_fails(app: Path, tmp_path: Path) -> None:
    lib = app / "Contents/Resources/python/lib/python3.12/urllib"
    lib.mkdir(parents=True)
    # Across a chunk boundary as well, which a naive per-chunk search misses.
    (lib / "parse.py").write_bytes(b"a" * ((1 << 20) - 5) + b"itms-services']")
    failures = verify_bundle.verify(app, _entitlements(tmp_path)).failures
    assert any("itms-services" in f for f in failures)


def test_network_entitlement_fails(app: Path, tmp_path: Path) -> None:
    entitlements = _entitlements(tmp_path, **{"com.apple.security.network.client": True})
    failures = verify_bundle.verify(app, entitlements).failures
    assert any("network.client" in f for f in failures)


def test_altered_weights_fail(app: Path, tmp_path: Path) -> None:
    checkpoint = app / "Contents/Resources/models/nnunet_trained_models/Dataset123_Organs"
    (checkpoint / "checkpoint_final.pth").write_bytes(b"tampered")
    failures = verify_bundle.verify(app, _entitlements(tmp_path)).failures
    assert any("differs from the manifest" in f for f in failures)


def test_pip_left_in_bundle_fails(app: Path, tmp_path: Path) -> None:
    (app / "Contents/Resources/python/bin/pip3").write_text("#!/bin/sh\n")
    failures = verify_bundle.verify(app, _entitlements(tmp_path)).failures
    assert any("installer" in f for f in failures)


def test_the_interpreter_is_allowed_in_bin(app: Path, tmp_path: Path) -> None:
    bin_dir = app / "Contents/Resources/python/bin"
    for name in ("python", "python3", "python3.12"):
        (bin_dir / name).write_bytes(b"\xcf\xfa\xed\xfe")
    assert verify_bundle.verify(app, _entitlements(tmp_path)).failures == []


@pytest.mark.parametrize(
    "name", ["nnUNetv2_download_pretrained_model_by_url", "hf", "dcm2niix", "python3-config"]
)
def test_console_scripts_in_bin_fail(app: Path, tmp_path: Path, name: str) -> None:
    # A real one from a macOS build: the shebang names the build machine.
    script = "#!/Users/runner/work/x/build/runtime/python/bin/python3\n"
    (app / "Contents/Resources/python/bin" / name).write_text(script)
    failures = verify_bundle.verify(app, _entitlements(tmp_path)).failures
    assert failures == [f"not the interpreter: python/bin/{name}"]


@pytest.mark.parametrize(
    "path",
    [
        "lib/python3.12/site-packages/blosc2/lib/libtcc.dylib",
        "lib/python3.12/site-packages/cc3d/cc3d.cpython-312-darwin.so",
        "lib/python3.12/site-packages/setuptools/__init__.py",
    ],
)
def test_parts_removed_by_adr_0014_fail_if_they_come_back(
    app: Path, tmp_path: Path, path: str
) -> None:
    target = app / "Contents/Resources/python" / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    failures = verify_bundle.verify(app, _entitlements(tmp_path)).failures
    assert any(path.split("/")[3] in f for f in failures)


def test_the_cc3d_stub_is_allowed(app: Path, tmp_path: Path) -> None:
    stub = app / "Contents/Resources/python/lib/python3.12/site-packages/cc3d"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("# ADR 0014 stub\n")
    assert verify_bundle.verify(app, _entitlements(tmp_path)).failures == []


def test_the_shipped_entitlements_pass() -> None:
    root = Path(__file__).resolve().parents[2]
    report = verify_bundle.Report()
    verify_bundle.check_entitlements(
        Path("/nonexistent"), root / "App/Resources/BCOAnalyzer.entitlements", report
    )
    assert report.failures == []
    helper = plistlib.loads((root / "App/Resources/Helper.entitlements").read_bytes())
    assert set(helper) == verify_bundle.HELPER_ENTITLEMENTS


def test_a_validation_prediction_in_the_models_fails(app: Path, tmp_path: Path) -> None:
    folder = "models/nnunet_trained_models/Dataset123_Organs/fold_all/validation"
    target = app / "Contents/Resources" / folder / "0075.nii.gz"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x1f\x8b")
    failures = verify_bundle.verify(app, _entitlements(tmp_path)).failures
    assert failures == [f"image data in the bundle: {folder}/0075.nii.gz"]


def test_mach_o_file_types_are_read_from_thin_and_universal_files(tmp_path: Path) -> None:
    assert verify_bundle.macho_filetype(_macho(tmp_path / "a", 2)) == verify_bundle.MH_EXECUTE
    assert verify_bundle.macho_filetype(_macho(tmp_path / "b", 6)) == 6
    assert verify_bundle.macho_filetype(_macho(tmp_path / "c", 2, universal=True)) == 2
    (tmp_path / "d").write_text("#!/bin/sh\n")
    assert verify_bundle.macho_filetype(tmp_path / "d") is None


def test_a_program_the_worker_does_not_need_fails(app: Path, tmp_path: Path) -> None:
    # torch's wheel carries protoc, a compiler nothing in the app runs.
    _macho(app / SITE / "torch/bin/protoc", 2, universal=True)
    failures = verify_bundle.verify(app, _entitlements(tmp_path)).failures
    assert any("torch/bin/protoc" in f for f in failures)


def test_the_programs_the_worker_needs_pass(app: Path, tmp_path: Path) -> None:
    _macho(app / "Contents/Resources/python/bin/python3.12", 2)
    _macho(app / SITE / "dcm2niix/dcm2niix", 2)
    _macho(app / SITE / "torch/bin/torch_shm_manager", 2)
    _macho(app / SITE / "torch/lib/libtorch_cpu.dylib", 6)
    _macho(app / "Contents/MacOS/BCOAnalyzer", 2)
    assert verify_bundle.verify(app, _entitlements(tmp_path)).failures == []


def test_an_undeclared_required_reason_api_fails(app: Path, tmp_path: Path) -> None:
    library = _macho(app / "Contents/Resources/python/lib/libpython3.12.dylib", 6)
    manifest = app / "Contents/Resources/PrivacyInfo.xcprivacy"
    content = plistlib.loads(manifest.read_bytes())
    content["NSPrivacyAccessedAPITypes"] = [
        e
        for e in content["NSPrivacyAccessedAPITypes"]
        if e["NSPrivacyAccessedAPIType"] != "NSPrivacyAccessedAPICategorySystemBootTime"
    ]
    manifest.write_bytes(plistlib.dumps(content))

    def imports(path: Path) -> set[str]:
        return {"mach_absolute_time", "open"} if path == library else set()

    failures = verify_bundle.verify(app, _entitlements(tmp_path), imports).failures
    assert any("SystemBootTime" in f and "libpython3.12.dylib" in f for f in failures)


def test_the_shipped_manifest_declares_what_the_runtime_imports(app: Path, tmp_path: Path) -> None:
    # The categories the macOS interpreter and packages import, found with a
    # Mach-O reader over the arm64 wheels on 2 October 2026.
    library = _macho(app / "Contents/Resources/python/lib/libpython3.12.dylib", 6)
    imported = {"stat", "fstatat", "statfs", "statvfs", "mach_absolute_time"}

    def imports(path: Path) -> set[str]:
        return imported if path == library else set()

    assert verify_bundle.verify(app, _entitlements(tmp_path), imports).failures == []


def test_a_missing_privacy_manifest_fails(app: Path, tmp_path: Path) -> None:
    (app / "Contents/Resources/PrivacyInfo.xcprivacy").unlink()
    failures = verify_bundle.verify(app, _entitlements(tmp_path)).failures
    assert "PrivacyInfo.xcprivacy missing" in failures
