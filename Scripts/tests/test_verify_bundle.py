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
    return app


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
