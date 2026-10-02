#!/usr/bin/env bash
# CI only, on request: the CT of one public whole-body FDG PET/CT, segmented
# by the signed app's worker inside the sandbox, on Apple Silicon. The PET is
# not used: the app reads no PET yet (SUV is phase 2).
#
#   Scripts/ci/real_case.sh <app> [model ...]
#
# The case is from ACRIN-NSCLC-FDG-PET (CC BY 3.0, doi:10.7937/tcia.2019.30ilqfcl),
# fetched from the Imaging Data Commons' public bucket when the job runs.
# Nothing of it enters the repository, and the log shows timings, volumes and
# densities only. The same series was segmented on Linux in the cloud, so the
# numbers can be compared across platforms (docs/benchmarks.md).
#
# The app does not import DICOM yet (M2), so dcm2niix runs here, outside the
# sandbox; the copy in build/runtime is the same binary the app carries, but
# unsigned, because the signed one has the inherit entitlement and the kernel
# stops it unless a sandboxed parent starts it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# Absolute, because the sandboxed probe sees the container as its working
# directory and could not find the bundle from a relative path.
APP="$(cd "$1" && pwd)"; shift
MODELS=("${@:-clin_ct_organs}")
WORK="$ROOT/build/real"
CT_SERIES="1.3.6.1.4.1.14519.5.2.1.7009.2403.292064430427945429126463039560"
PY="$ROOT/build/runtime/python/bin/python3"
PROBE="$APP/Contents/MacOS/bcoa-probe"
# The probe is signed as de.ludwig.bcoanalyzer.probe; its sandbox container
# is where it puts its project, and the only place it can read without a grant.
PROJECT="$HOME/Library/Containers/de.ludwig.bcoanalyzer.probe/Data/Probe.bcoaproj"

log() { printf '[real-case] %s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

rm -rf "$WORK"; mkdir -p "$WORK"
python3 -m venv "$WORK/idc"
"$WORK/idc/bin/pip" install -q "idc-index==0.12.5"
log "downloading the CT series from IDC"
"$WORK/idc/bin/idc" download-from-selection --series-instance-uid "$CT_SERIES" \
  --download-dir "$WORK/dicom" --dir-template "%Modality" --quiet True \
  --show-progress-bar False >/dev/null
log "$(find "$WORK/dicom" -name '*.dcm' | wc -l | tr -d ' ') DICOM files"

D2N="$(ls "$ROOT"/build/runtime/python/lib/python3.*/site-packages/dcm2niix/dcm2niix)"
mkdir -p "$WORK/nifti"
"$D2N" -z y -b y -ba y -f ct -o "$WORK/nifti" "$WORK/dicom/CT" >/dev/null
mkdir -p "$PROJECT/work/s_ct" "$PROJECT/work/s_crop"
cp "$WORK/nifti/ct.nii.gz" "$PROJECT/work/s_ct/ct.nii.gz"
# The CPU half of the device comparison runs on 64 of the 356 slices, from
# the kidneys to the lung bases (liver, spleen, pancreas, stomach, adrenals):
# the whole CT on the runner's CPU did not finish within 68 minutes, where
# MPS took under seven. Both devices see the same crop.
"$PY" -I -c '
import sys, SimpleITK as sitk
ct = sitk.ReadImage(sys.argv[1])
sitk.WriteImage(ct[:, :, 96:160], sys.argv[2], True)
' "$PROJECT/work/s_ct/ct.nii.gz" "$PROJECT/work/s_crop/ct.nii.gz"

# The worker's peak memory is what decides which Macs can run a model; the
# probe's own rusage does not include its child, so it is sampled here.
# Every five minutes it also says that the run is alive, because a step that
# times out prints nothing of its own.
sample_memory() {
  local peak=0 now ticks=0
  while sleep 2; do
    now="$(ps -axo rss=,comm= | awk '/python3/ {s += $1} END {print s + 0}')"
    (( now > peak )) && peak=$now && echo "$peak" > "$WORK/peak_rss_kb"
    (( ++ticks % 150 == 0 )) && log "still running after $((ticks * 2)) s, peak $((peak / 1024)) MB"
  done
}

# One run per model on the whole CT as the app would start it (device auto:
# MPS when present). For the first model, the crop once on MPS and once on
# the CPU, which is spike S1's step 3: the same image on both devices,
# compared label by label (plan §16: Dice >= 0.99, volume within 1 %).
segment() {
  local model="$1" device="$2" series="$3" tag="$4" input="$5"
  log "segmenting $input with $model on $device"
  echo 0 > "$WORK/peak_rss_kb"
  sample_memory & sampler=$!
  start=$(date +%s)
  status=0
  "$PROBE" segment "{\"series_key\": \"$series\", \"input_nifti\": \"$input\",
    \"models\": [\"$model\"], \"device\": \"$device\"}" > "$WORK/events_$tag.jsonl" || status=$?
  end=$(date +%s)
  kill "$sampler" 2>/dev/null || true
  wait "$sampler" 2>/dev/null || true
  printf '{"run": "%s", "wall_s": %d, "peak_rss_gb": %.2f, "exit": %d}\n' \
    "$tag" $((end - start)) "$(echo "$(cat "$WORK/peak_rss_kb") / 1048576" | bc -l)" \
    "$status" | tee -a "$WORK/timings.jsonl"
  sysctl vm.swapusage
}

for model in "${MODELS[@]}"; do
  segment "$model" auto s_ct "$model" work/s_ct/ct.nii.gz
done
segment "${MODELS[0]}" mps s_crop_mps "${MODELS[0]}__crop_mps" work/s_crop/ct.nii.gz
segment "${MODELS[0]}" cpu s_crop_cpu "${MODELS[0]}__crop_cpu" work/s_crop/ct.nii.gz

"$PY" -I "$ROOT/Scripts/ci/label_summary.py" "$PROJECT" "$WORK"
