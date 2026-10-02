#!/usr/bin/env bash
# Signs every Mach-O file below a directory, deepest first, each one on its
# own (never `codesign --deep`), with hardened runtime and a secure timestamp.
# Executables the app starts (python3, dcm2niix) get Helper.entitlements:
# exactly app-sandbox and inherit. Libraries get no entitlements.
#
#   sign_tree.sh <directory> <identity> <helper-entitlements>
#
# Identity "-" signs ad hoc (local builds); no timestamp then, because the
# timestamp server is only reachable for real identities.
set -euo pipefail

DIR="$1"
IDENTITY="$2"
HELPER_ENTITLEMENTS="$3"

timestamp=(--timestamp)
[[ "$IDENTITY" == "-" ]] && timestamp=(--timestamp=none)

is_macho() {
  # Thin arm64/x86_64 (feedfacf / cffaedfe) and universal (cafebabe) binaries.
  local magic
  magic="$(xxd -p -l 4 "$1" 2>/dev/null || true)"
  [[ "$magic" == "cffaedfe" || "$magic" == "feedfacf" || "$magic" == "cafebabe" || "$magic" == "bebafeca" ]]
}

count=0
# Deepest paths first, so a library is signed before anything that loads it.
while IFS= read -r file; do
  is_macho "$file" || continue
  if [[ -x "$file" && "$file" != *.so && "$file" != *.dylib ]]; then
    codesign --force --sign "$IDENTITY" "${timestamp[@]}" --options runtime \
      --entitlements "$HELPER_ENTITLEMENTS" "$file"
  else
    codesign --force --sign "$IDENTITY" "${timestamp[@]}" --options runtime "$file"
  fi
  count=$((count + 1))
done < <(find "$DIR" -type f \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) \
           | awk -F/ '{ print NF "\t" $0 }' | sort -rn | cut -f2-)

echo "[sign] signed $count Mach-O files under $DIR"
