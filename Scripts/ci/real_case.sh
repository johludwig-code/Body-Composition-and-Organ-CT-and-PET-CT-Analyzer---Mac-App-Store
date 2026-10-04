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
# The app has no conversion job yet (M3), so dcm2niix runs here, outside the
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
# The CPU half of the device comparison runs on a block of the upper abdomen:
# slices 96 to 160 (kidneys to lung bases) and 192 by 192 pixels around the
# pancreas, which holds the adrenals, kidneys, pancreas, stomach and parts of
# liver and spleen. nnU-Net pads anything shorter than its 224-slice patch,
# so fewer slices alone save nothing; the pixel crop cuts the sliding
# windows from 36 to 4. The whole CT on the runner's CPU had not finished
# after 68 minutes, nor had 64 whole slices after 34. Both devices see the
# same block.
"$PY" -I -c '
import sys, SimpleITK as sitk
ct = sitk.ReadImage(sys.argv[1])
sitk.WriteImage(ct[176:368, 176:368, 96:160], sys.argv[2], True)
' "$PROJECT/work/s_ct/ct.nii.gz" "$PROJECT/work/s_crop/ct.nii.gz"

# Why the CPU is that slow: one convolution of the organ model's first-stage
# size, timed on each device with the bundled torch, outside the sandbox.
"$PY" -I -c '
import json, time, torch
x = torch.randn(1, 32, 224, 96, 96)
conv = torch.nn.Conv3d(32, 32, 3, padding=1)
out = {"threads": torch.get_num_threads(), "mkldnn": torch.backends.mkldnn.is_available()}
for device in ("cpu", "mps"):
    if device == "mps" and not torch.backends.mps.is_available():
        continue
    c, y = conv.to(device), x.to(device)
    with torch.no_grad():
        c(y)
        if device == "mps": torch.mps.synchronize()
        start = time.perf_counter()
        for _ in range(3): c(y)
        if device == "mps": torch.mps.synchronize()
    out[device + "_s_per_conv"] = round((time.perf_counter() - start) / 3, 3)
print("[real-case] conv3d", json.dumps(out))
'
log "memory before the runs: $(sysctl -n vm.swapusage), $(memory_pressure | tail -1)"

# The worker's peak memory is what decides which Macs can run a model; the
# probe's own rusage does not include its child, so it is sampled here. RSS
# leaves out what MPS allocates through Metal: the organ model peaked at
# 2.9 GB RSS on MPS and 7.7 GB on a Linux CPU. top's MEM column is the
# memory footprint, which counts it, so both are kept; top is slow, so it is
# read every ten seconds.
# Every five minutes the sampler also says that the run is alive, because a
# step that times out prints nothing of its own.
footprint_mb() {
  top -l 1 -stats command,mem 2>/dev/null | awk '
    $1 ~ /^python3/ {
      v = $2; gsub(/[+-]$/, "", v); unit = substr(v, length(v)); n = v + 0
      if (unit == "G") n *= 1024; else if (unit == "K") n /= 1024; else if (unit == "B") n /= 1048576
      s += n
    }
    END { printf "%d", s }'
}
sample_memory() {
  local peak=0 fp_peak=0 now fp ticks=0
  while sleep 2; do
    now="$(ps -axo rss=,comm= | awk '/python3/ {s += $1} END {print s + 0}')"
    (( now > peak )) && peak=$now && echo "$peak" > "$WORK/peak_rss_kb"
    if (( ticks % 5 == 0 )); then
      fp="$(footprint_mb)"
      (( fp > fp_peak )) && fp_peak=$fp && echo "$fp_peak" > "$WORK/peak_footprint_mb"
    fi
    (( ++ticks % 150 == 0 )) && log "still running after $((ticks * 2)) s, peak RSS $((peak / 1024)) MB, footprint $fp_peak MB"
  done
}

# One run per model on the whole CT as the app would start it (device auto:
# MPS when present). For the first model, the crop once on MPS and once on
# the CPU, which is spike S1's step 3: the same image on both devices,
# compared label by label (plan §16: Dice >= 0.99, volume within 1 %).
segment() {
  local model="$1" device="$2" series="$3" tag="$4" input="$5"
  log "segmenting $input with $model on $device"
  echo 0 > "$WORK/peak_rss_kb"; echo 0 > "$WORK/peak_footprint_mb"
  sample_memory & sampler=$!
  start=$(date +%s)
  status=0
  "$PROBE" segment "{\"series_key\": \"$series\", \"input_nifti\": \"$input\",
    \"models\": [\"$model\"], \"device\": \"$device\"}" > "$WORK/events_$tag.jsonl" || status=$?
  end=$(date +%s)
  kill "$sampler" 2>/dev/null || true
  wait "$sampler" 2>/dev/null || true
  printf '{"run": "%s", "wall_s": %d, "peak_rss_gb": %.2f, "peak_footprint_gb": %.2f, "exit": %d}\n' \
    "$tag" $((end - start)) "$(echo "$(cat "$WORK/peak_rss_kb") / 1048576" | bc -l)" \
    "$(echo "$(cat "$WORK/peak_footprint_mb") / 1024" | bc -l)" "$status" | tee -a "$WORK/timings.jsonl"
  sysctl vm.swapusage
}

for model in "${MODELS[@]}"; do
  segment "$model" auto s_ct "$model" work/s_ct/ct.nii.gz
done
segment "${MODELS[0]}" mps s_crop_mps "${MODELS[0]}__crop_mps" work/s_crop/ct.nii.gz
segment "${MODELS[0]}" cpu s_crop_cpu "${MODELS[0]}__crop_cpu" work/s_crop/ct.nii.gz

"$PY" -I "$ROOT/Scripts/ci/label_summary.py" "$PROJECT" "$WORK"
