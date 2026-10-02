#!/usr/bin/env bash
# Spike S1 outside the app: run the worker's self-test against build/, laid
# out like Contents/Resources (python/ and models/ side by side).
#
#   Scripts/run_selftest.sh [model] [device]
#
# Prints the protocol lines; the log is in build/s1/Study.bcoaproj/logs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL="${1:-clin_ct_organs}"
RES="$ROOT/build/s1/Resources"
PROJECT="$ROOT/build/s1/Study.bcoaproj"

mkdir -p "$RES" "$PROJECT"
ln -sfn "$ROOT/build/runtime/python" "$RES/python"
ln -sfn "$ROOT/build/models" "$RES/models"

cat > "$ROOT/build/s1/job.json" <<JSON
{"protocol_version": 1, "job_id": "j_s1_selftest", "kind": "selftest",
 "project_dir": "$PROJECT", "log_path": "$PROJECT/logs/j_s1_selftest.log",
 "resources_dir": "$RES",
 "payload": {"inference": true, "inference_model": "$MODEL"}}
JSON

exec "$RES/python/bin/python3" -I -m bcoa_worker run --job "$ROOT/build/s1/job.json"
