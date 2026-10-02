#!/usr/bin/env bash
# Builds the Python runtime that ships inside the app: pinned CPython 3.12
# (ADR 0004), the locked packages (ADR 0010), the worker package and MOOSE's
# custom trainer, precompiled and stripped. Output: build/runtime/python.
#
# Every input is pinned by URL and SHA-256 or by the hashed lock; running it
# twice gives the same tree. Network is used here, at build time only.
#
# Overridable for testing the script on another platform:
#   PBS_URL, PBS_SHA256, LOCK_FILE, OUT_DIR
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PBS_URL="${PBS_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/20250902/cpython-3.12.11%2B20250902-aarch64-apple-darwin-install_only_stripped.tar.gz}"
PBS_SHA256="${PBS_SHA256:-17aa38f6a06eefbaa7757d7f8aa9d7941f169aa9571127b8d346141d2aa532d1}"
LOCK_FILE="${LOCK_FILE:-$ROOT/Worker/requirements.lock}"
OUT_DIR="${OUT_DIR:-$ROOT/build/runtime}"
CACHE_DIR="$ROOT/build/cache"
PY_DIR="$OUT_DIR/python"
PY="$PY_DIR/bin/python3"

log() { printf '[runtime] %s\n' "$*"; }

sha256() {
  if command -v shasum >/dev/null; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi
}

mkdir -p "$CACHE_DIR"
ARCHIVE="$CACHE_DIR/$(basename "${PBS_URL//%2B/+}")"
if [[ ! -f "$ARCHIVE" ]]; then
  log "downloading $(basename "$ARCHIVE")"
  curl --fail --location --silent --show-error -o "$ARCHIVE.part" "$PBS_URL"
  mv "$ARCHIVE.part" "$ARCHIVE"
fi
actual="$(sha256 "$ARCHIVE")"
if [[ "$actual" != "$PBS_SHA256" ]]; then
  log "SHA-256 mismatch for $(basename "$ARCHIVE"): $actual"
  exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
tar -xzf "$ARCHIVE" -C "$OUT_DIR"
VERSION_DIR="$(cd "$PY_DIR/lib" && ls -d python3.* | head -1)"
STDLIB="$PY_DIR/lib/$VERSION_DIR"

# App Store: Apple's automated review rejects any Mac app that contains the
# string "itms-services". CPython lists it in urllib.parse.uses_netloc; this
# is the same edit CPython's --with-app-store-compliance makes.
log "removing itms-services from urllib.parse"
"$PY" - "$STDLIB/urllib/parse.py" <<'PYEOF'
import sys, pathlib
path = pathlib.Path(sys.argv[1])
text = path.read_text()
patched = text.replace(", 'itms-services']", "]")
if patched == text and "itms-services" in text:
    sys.exit("itms-services found in an unexpected form; update build_runtime.sh")
path.write_text(patched)
PYEOF

# Not needed at run time, and each is either GUI code that cannot run in the
# worker or an installer that must not exist in a bundle that never installs.
log "removing unneeded parts of the standard library"
rm -rf "$STDLIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,turtle.py}
rm -f "$STDLIB"/lib-dynload/_tkinter*.so
rm -rf "$PY_DIR"/lib/{libtcl*,libtk*,tcl*,tk*,itcl*,thread*}
rm -f "$PY_DIR"/bin/{idle3*,pydoc3*,2to3*}

log "installing locked packages"
uv pip install --python "$PY" --break-system-packages --require-hashes --no-deps \
  --no-cache -r "$LOCK_FILE"

SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"

# Copied, not pip-installed: building a wheel would pull a build backend from
# the network outside the hashed lock. The worker runs with -I, which ignores
# PYTHONPATH, so site-packages is where it has to be.
log "installing bcoa_worker"
rm -rf "$SITE/bcoa_worker"
cp -R "$ROOT/Worker/bcoa_worker" "$SITE/bcoa_worker"

# MOOSE copies its custom trainer into nnU-Net on every call; in the signed
# bundle that would be a write into the code signature. Installed here,
# before signing, MOOSE finds it and skips the copy (ADR 0011).
if [[ -f "$SITE/moosez/nnUNet_custom_trainer/MOOSE_custom_trainers.py" ]]; then
  log "installing MOOSE custom trainer into nnunetv2"
  cp "$SITE/moosez/nnUNet_custom_trainer/MOOSE_custom_trainers.py" \
     "$SITE/nnunetv2/training/nnUNetTrainer/variants/MOOSE_custom_trainers.py"
fi

# pip and the package installers' own metadata are not needed to run, and a
# bundle that contains pip invites the question whether it installs code.
rm -rf "$SITE"/pip "$SITE"/pip-*.dist-info "$PY_DIR"/bin/pip*

log "stripping tests, headers and static libraries"
find "$SITE" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +
rm -rf "$PY_DIR/include" "$PY_DIR/share"
find "$PY_DIR" -name '*.a' -delete
find "$PY_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

# The bundle is read-only and must stay byte-identical, so the worker runs
# with PYTHONDONTWRITEBYTECODE. Precompiled with unchecked-hash: the .pyc is
# trusted without comparing source timestamps, which signing may change.
log "precompiling"
"$PY" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$PY_DIR/lib" >/dev/null || {
  log "compileall reported errors (vendored Python 2 files are expected); continuing"
}

log "size report"
{
  echo "runtime $(du -sh "$PY_DIR" | cut -f1)"
  du -sh "$SITE"/* 2>/dev/null | sort -rh | head -25
} | tee "$OUT_DIR/size-report.txt"
log "done: $PY"
