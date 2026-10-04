#!/usr/bin/env python3
"""`make verify`: the App Store and offline rules, checked on the built app.

    python3 Scripts/verify_bundle.py build/Release/BCOAnalyzer.app

Checks (plan §15):
  1. the string "itms-services" occurs in no file of the bundle
  2. no network entitlement; app sandbox on; helpers carry exactly
     app-sandbox and inherit
  3. every Mach-O file is signed (macOS only; reported as skipped elsewhere)
  4. model files match the manifest's SHA-256
  5. the licence report exists and is not empty
  6. no installer left in the bundle (pip, ensurepip, setuptools), nothing in
     python/bin but the interpreter, and none of the parts ADR 0014 removes
     (TinyCC, the real cc3d, gdcm)
  7. no image data (NIfTI, DICOM, …) among the models
  8. no program in Resources but the three the worker needs, each sandboxed
  9. every required-reason API the Mach-O files import is declared in
     PrivacyInfo.xcprivacy (macOS only, needs nm)
 10. a size report is written next to the app

Plain Python 3 without third-party packages, so it runs with any interpreter.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

FORBIDDEN_STRING = b"itms-services"
FORBIDDEN_ENTITLEMENTS = {
    "com.apple.security.network.client",
    "com.apple.security.network.server",
}
HELPER_ENTITLEMENTS = {"com.apple.security.app-sandbox", "com.apple.security.inherit"}
MACHO_MAGIC = {
    bytes.fromhex("cffaedfe"),
    bytes.fromhex("feedfacf"),
    bytes.fromhex("cafebabe"),
    bytes.fromhex("bebafeca"),
}
MH_EXECUTE = 2


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


def files_of(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]


def contains(path: Path, needle: bytes, chunk: int = 1 << 20) -> bool:
    overlap = len(needle) - 1
    tail = b""
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            if needle in tail + block:
                return True
            tail = block[-overlap:]
    return False


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACHO_MAGIC
    except OSError:
        return False


def macho_filetype(path: Path) -> int | None:
    """The Mach-O file type: 2 a program, 6 a dylib, 8 a loadable bundle."""
    try:
        with path.open("rb") as handle:
            head = handle.read(20)
            magic = head[:4]
            if magic == bytes.fromhex("cffaedfe"):
                return int.from_bytes(head[12:16], "little")
            if magic == bytes.fromhex("feedfacf"):
                return int.from_bytes(head[12:16], "big")
            if magic == bytes.fromhex("cafebabe") and len(head) == 20:
                # Universal: every slice of one file is the same kind of thing,
                # so the first decides.
                handle.seek(int.from_bytes(head[16:20], "big"))
                inner = handle.read(16)
                order = "little" if inner[:4] == bytes.fromhex("cffaedfe") else "big"
                return int.from_bytes(inner[12:16], order)
    except (OSError, ValueError):
        return None
    return None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def check_strings(app: Path, files: list[Path], report: Report) -> None:
    hits = [p.relative_to(app).as_posix() for p in files if contains(p, FORBIDDEN_STRING)]
    for hit in hits:
        report.fail(f"'itms-services' found in {hit}")


def entitlements_of(path: Path) -> dict[str, object] | None:
    if shutil.which("codesign") is None:
        return None
    out = subprocess.run(
        ["codesign", "-d", "--entitlements", "-", "--xml", str(path)],
        capture_output=True,
        check=False,
    )
    start = out.stdout.find(b"<?xml")
    return plistlib.loads(out.stdout[start:]) if start >= 0 else {}


def check_entitlements(app: Path, source_entitlements: Path | None, report: Report) -> None:
    declared: dict[str, object] | None = None
    if source_entitlements and source_entitlements.exists():
        declared = plistlib.loads(source_entitlements.read_bytes())
    signed = entitlements_of(app)
    for label, ent in (("declared", declared), ("signed", signed)):
        if ent is None:
            continue
        for key in FORBIDDEN_ENTITLEMENTS & set(ent):
            report.fail(f"{label} entitlements contain {key}")
        if ent.get("com.apple.security.app-sandbox") is not True:
            report.fail(f"{label} entitlements do not enable the app sandbox")
    if signed is None:
        report.note("codesign not available: signed entitlements not checked")


def check_signatures(app: Path, files: list[Path], report: Report) -> int:
    machos = [p for p in files if is_macho(p)]
    if shutil.which("codesign") is None:
        report.note(f"codesign not available: {len(machos)} Mach-O files not checked")
        return len(machos)
    main_dir = app / "Contents" / "MacOS"
    for path in machos:
        result = subprocess.run(
            ["codesign", "--verify", "--strict", str(path)], capture_output=True, check=False
        )
        if result.returncode != 0:
            report.fail(f"not signed: {path.relative_to(app)}")
            continue
        flags = subprocess.run(
            ["codesign", "-d", "--verbose=2", str(path)],
            capture_output=True,
            text=True,
            check=False,
        ).stderr
        if "runtime" not in flags:
            report.fail(f"no hardened runtime: {path.relative_to(app)}")
        # App Store Connect refuses any program in the bundle that is not
        # sandboxed (ITMS-90296), whether or not the app ever starts it.
        if path.parent != main_dir and macho_filetype(path) == MH_EXECUTE:
            keys = set(entitlements_of(path) or {})
            if keys != HELPER_ENTITLEMENTS:
                report.fail(f"helper {path.name} has entitlements {sorted(keys)}")
    return len(machos)


def check_manifest(resources: Path, report: Report) -> None:
    manifest_path = resources / "models" / "manifest.json"
    if not manifest_path.exists():
        report.fail("models/manifest.json missing")
        return
    manifest = json.loads(manifest_path.read_text())
    root = resources / "models" / "nnunet_trained_models"
    for model in manifest.get("models", []):
        for rel, expected in model["files"].items():
            path = root / rel
            if not path.is_file():
                report.fail(f"{model['name']}: {rel} missing")
            elif sha256(path) != expected:
                report.fail(f"{model['name']}: {rel} differs from the manifest")


INSTALLERS = (
    "bin/pip*",
    "lib/python3*/ensurepip",
    "lib/python3*/site-packages/pip",
    "lib/python3*/site-packages/setuptools",
)
# ADR 0014. A JIT compiler and LGPL code the worker never loads; if one of
# these reappears, a lock update brought it back and build_runtime.sh missed it.
REMOVED = (
    "lib/python3*/site-packages/blosc2/lib/libtcc*",
    "lib/python3*/site-packages/connected_components_3d-*",
    "lib/python3*/site-packages/cc3d/*.so",
    "lib/python3*/site-packages/_gdcm",
)


# The interpreter is the only program bin/ may hold. Console scripts carry the
# build machine's path in their shebang, so none of them can run from the
# bundle, and some are download tools (ADR 0014).
INTERPRETER = re.compile(r"python(3(\.\d+)?)?")


def check_installers(resources: Path, report: Report) -> None:
    python = resources / "python"
    for hit in sorted((python / "bin").glob("*")):
        if not INTERPRETER.fullmatch(hit.name):
            report.fail(f"not the interpreter: {hit.relative_to(resources)}")
    for pattern in INSTALLERS:
        for hit in python.glob(pattern):
            report.fail(f"installer left in bundle: {hit.relative_to(resources)}")
    for pattern in REMOVED:
        for hit in python.glob(pattern):
            report.fail(f"removed by ADR 0014 but present: {hit.relative_to(resources)}")


# The model archives carry nnU-Net's validation predictions, labelmaps of the
# authors' validation patients (1 030 NIfTI files in clin_ct_body alone).
# fetch_models.py prunes them; this makes sure no image ever ships.
IMAGE_SUFFIXES = (".nii", ".nii.gz", ".dcm", ".mha", ".mhd", ".nrrd")


def check_no_images_in_models(resources: Path, report: Report) -> None:
    models = resources / "models"
    for path in sorted(models.rglob("*")) if models.is_dir() else []:
        if path.is_file() and path.name.lower().endswith(IMAGE_SUFFIXES):
            report.fail(f"image data in the bundle: {path.relative_to(resources)}")


# The programs the worker needs. Every other one is a question App Review may
# ask and a binary to sign; torch_shm_manager stays because `import torch`
# checks that it exists, although nothing here starts it.
PROGRAMS = {"dcm2niix", "torch_shm_manager"}


def check_programs(app: Path, files: list[Path], report: Report) -> None:
    interpreter_dir = app / "Contents" / "Resources" / "python" / "bin"
    for path in files:
        if path.is_relative_to(app / "Contents" / "MacOS"):
            continue
        if macho_filetype(path) != MH_EXECUTE:
            continue
        if path.name in PROGRAMS or (
            path.parent == interpreter_dir and INTERPRETER.fullmatch(path.name)
        ):
            continue
        report.fail(f"program the worker does not need: {path.relative_to(app)}")


# Apple's required-reason APIs as the symbols a Mach-O file imports
# ("Describing use of required reason API"). An upload is refused
# (ITMS-91053) for a category the privacy manifest leaves out; the bundled
# interpreter alone imports the first three.
REQUIRED_REASON_SYMBOLS = {
    "NSPrivacyAccessedAPICategoryFileTimestamp": {
        "stat",
        "fstat",
        "fstatat",
        "lstat",
        "getattrlist",
        "fgetattrlist",
        "getattrlistat",
        "getattrlistbulk",
    },
    "NSPrivacyAccessedAPICategoryDiskSpace": {"statfs", "statvfs", "fstatfs", "fstatvfs"},
    "NSPrivacyAccessedAPICategorySystemBootTime": {"mach_absolute_time"},
    "NSPrivacyAccessedAPICategoryUserDefaults": {"OBJC_CLASS_$_NSUserDefaults"},
}


def imports_of(path: Path) -> set[str]:
    out = subprocess.run(
        ["nm", "-u", "-j", str(path)], capture_output=True, text=True, check=False
    ).stdout
    # `_stat$INODE64` on x86_64 is the same API as `_stat`.
    return {
        re.sub(r"\$INODE64$", "", line.strip()[1:])
        for line in out.splitlines()
        if line.startswith("_")
    }


def check_privacy_manifest(
    app: Path, files: list[Path], report: Report, imports: Callable[[Path], set[str]] | None
) -> None:
    manifest = app / "Contents" / "Resources" / "PrivacyInfo.xcprivacy"
    if not manifest.is_file():
        report.fail("PrivacyInfo.xcprivacy missing")
        return
    declared = {
        entry.get("NSPrivacyAccessedAPIType")
        for entry in plistlib.loads(manifest.read_bytes()).get("NSPrivacyAccessedAPITypes", [])
        if entry.get("NSPrivacyAccessedAPITypeReasons")
    }
    if imports is None:
        report.note("nm not available: required-reason APIs not checked")
        return
    users: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        if not is_macho(path):
            continue
        used = imports(path)
        for category, symbols in REQUIRED_REASON_SYMBOLS.items():
            if used & symbols:
                users[category].append(path)
    for category, paths in sorted(users.items()):
        example = paths[0].relative_to(app)
        if category in declared:
            report.note(f"{category}: declared; imported by {len(paths)} files")
        else:
            report.fail(
                f"{category} imported by {len(paths)} files ({example}) "
                "but not declared in PrivacyInfo.xcprivacy"
            )


def write_size_report(app: Path, files: list[Path]) -> Path:
    sizes: dict[str, int] = defaultdict(int)
    resources = app / "Contents" / "Resources"
    for path in files:
        rel = path.relative_to(app)
        parts = rel.parts
        key = "/".join(parts[:4]) if path.is_relative_to(resources) else "/".join(parts[:2])
        sizes[key] += path.stat().st_size
    out = app.parent / "size-report.txt"
    total = sum(sizes.values())
    lines = [f"{total / 1e9:8.2f} GB  total"]
    lines += [
        f"{size / 1e6:8.1f} MB  {key}" for key, size in sorted(sizes.items(), key=lambda kv: -kv[1])
    ]
    out.write_text("\n".join(lines) + "\n")
    return out


def verify(
    app: Path,
    source_entitlements: Path | None,
    imports: Callable[[Path], set[str]] | None = None,
) -> Report:
    report = Report()
    if imports is None and sys.platform == "darwin" and shutil.which("nm"):
        imports = imports_of
    resources = app / "Contents" / "Resources"
    files = files_of(app)
    check_strings(app, files, report)
    check_entitlements(app, source_entitlements, report)
    count = check_signatures(app, files, report)
    report.note(f"{count} Mach-O files in the bundle")
    check_manifest(resources, report)
    notices = resources / "licenses" / "THIRD_PARTY_NOTICES.md"
    if not notices.exists() or notices.stat().st_size < 1000:
        report.fail("licenses/THIRD_PARTY_NOTICES.md missing or empty")
    check_installers(resources, report)
    check_no_images_in_models(resources, report)
    check_programs(app, files, report)
    check_privacy_manifest(app, files, report, imports)
    report.note(f"size report: {write_size_report(app, files)}")
    return report


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    app = Path(argv[0])
    root = Path(__file__).resolve().parents[1]
    report = verify(app, root / "App" / "Resources" / "BCOAnalyzer.entitlements")
    for note in report.notes:
        print(f"[verify] {note}")
    for failure in report.failures:
        print(f"[verify] FAIL {failure}", file=sys.stderr)
    print(f"[verify] {'FAILED' if report.failures else 'ok'}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
