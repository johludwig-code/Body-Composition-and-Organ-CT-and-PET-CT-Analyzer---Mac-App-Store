#!/usr/bin/env bash
# Xcode build phase (project.yml): copy build/runtime/python and build/models
# into Contents/Resources and sign everything in them before Xcode signs the
# app bundle around it. Runs on every build; rsync makes repeats cheap.
set -euo pipefail

ROOT="${PROJECT_DIR:?run from Xcode}"
DEST="${TARGET_BUILD_DIR:?}/${UNLOCALIZED_RESOURCES_FOLDER_PATH:?}"
IDENTITY="${EXPANDED_CODE_SIGN_IDENTITY:--}"
[[ -z "$IDENTITY" ]] && IDENTITY="-"

if [[ ! -x "$ROOT/build/runtime/python/bin/python3" ]]; then
  echo "warning: no backend in build/runtime; run 'make runtime models'. Building the UI only."
  exit 0
fi

mkdir -p "$DEST"
rsync -a --delete "$ROOT/build/runtime/python/" "$DEST/python/"
# The worker of this checkout, not the copy `make worker` last put into the
# runtime: a stale copy refuses the options a newer app sends as unknown,
# and the export fails without anything in the app's code being wrong.
SITE="$DEST/python/lib/python3.12/site-packages"
rsync -a --delete --exclude __pycache__ "$ROOT/Worker/bcoa_worker/" "$SITE/bcoa_worker/"
"$DEST/python/bin/python3" -I -m compileall -q --invalidation-mode unchecked-hash "$SITE/bcoa_worker"
if [[ -d "$ROOT/build/models" ]]; then
  rsync -a --delete "$ROOT/build/models/" "$DEST/models/"
fi
if [[ -f "$ROOT/build/licenses/THIRD_PARTY_NOTICES.md" ]]; then
  mkdir -p "$DEST/licenses"
  rsync -a "$ROOT/build/licenses/" "$DEST/licenses/"
fi

"$ROOT/Scripts/sign_tree.sh" "$DEST/python" "$IDENTITY" "$ROOT/App/Resources/Helper.entitlements"
date -u +%FT%TZ > "$DEST/backend.stamp"
