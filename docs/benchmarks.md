# Benchmarks

Measured, not estimated. Every row names the machine, the date and the
versions. Minimum device: M1 with 16 GB (plan §16).

## Model size in the bundle

| model | archive | unpacked | pruned | slimmed | measured on |
|---|---|---|---|---|---|
| clin_ct_organs | 459 MB | 475 MB | 237 MB | 118 MB | 2026-10-02, Linux build container, moosez 3.2.2 |

## Runtime per model

| model | volume | device | wall time | peak RSS | measured on |
|---|---|---|---|---|---|
| clin_ct_organs | 512×512×200, 1.17×1.17×2.0 mm | MPS | 91 s | – | BOCARTA-MOOSE, docs/moose-phase0.md, 2026-10-01, Apple Silicon |
| clin_ct_organs | 192×192×100 phantom, 1.5×1.5×2.5 mm | CPU, 4 cores | 79 s | 3.8 GB | 2026-10-02, Linux build container, torch 2.14.1, offline, slimmed weights |
| clin_ct_organs | 512×512×356, 0.98×0.98×2.5 mm, the real case below | MPS, sandboxed worker of the signed Release app | 395 s | 3.79 GB, no swap | 2026-10-02, GitHub `macos-15` runner: Apple M2 Pro (virtual), 5 cores, 14 GiB, macOS 15.7.9; torch 2.14.1 |
| clin_ct_organs | same | same | 1 945 s | 2.90 GB, 558 MB swapped | 2026-10-03, another runner of the same type |
| clin_ct_organs | same | CPU, same runner and sandbox | not finished after 68 min | – | 2026-10-02, same |
| clin_ct_organs | 512×512×64 slab of it | MPS, same | 479 s | 2.36 GB | 2026-10-03, same as the 1 945 s run |
| clin_ct_organs | same slab | CPU, same | not finished after 34 min | 4.42 GB so far | same |
| clin_ct_organs | the whole real case | MPS, same | 2 163 s | 2.88 GB RSS, **8.47 GB footprint**, 783 MB swapped | 2026-10-03, a third runner of the type |
| clin_ct_organs | 192×192×64 abdominal block of it | MPS, same | 74 s | 1.15 GB RSS, 5.01 GB footprint | same |
| clin_ct_organs | same block | CPU, same | 926 s | 4.50 GB RSS, 15.0 GB footprint, 3.26 GB swapped | same |
| clin_ct_organs | the real case | CPU, 4 cores, as the app runs it | 753 s | 7.74 GB | 2026-10-02, Linux build container (below) |
| clin_ct_lungs | same | same | 728 s | 5.70 GB | same |
| clin_ct_digestive | same | same | 5 339 s | 5.81 GB | same; eight passes per window, see "Test-time mirroring" |
| clin_ct_muscles | same | same | 550 s | 6.46 GB | same |
| clin_ct_ribs | same | same | 593 s | 9.80 GB | same |
| clin_ct_peripheral_bones | same | same | 581 s | 10.8 GB | same |
| clin_ct_body | same | same | 122 s | 3.88 GB | same |
| clin_ct_vertebrae | same | same | 582 s | 9.99 GB | same |
| clin_ct_cardiac | same | same | 545 s | 6.91 GB | same |
| clin_ct_body_composition | same | same | 916 s | 4.74 GB | same; includes the fast vertebra model MOOSE crops the field of view with |

Linux rows from the real case: Intel Xeon at 2.1 GHz, 4 cores, 15 GB,
torch 2.14.1 CPU, run as the app runs the worker (ADR 0014's trimmed runtime,
no network namespace, semaphores refused, nnU-Net's export in place per
ADR 0015; the guard of ADR 0016 from the muscle model on, the organ, lungs
and digestive runs came before it). Peak RSS is the worker's high-water mark
from `/proc`. All ten models one after another: 10 709 s, just under three
hours, half of it the digestive model.

On the Mac, RSS leaves out what Metal allocates for MPS, so it understates
what a Mac needs; `top`'s footprint counts it, and also what was compressed
or swapped out. The organ model on the whole CT needs about 8.5 GB on MPS by
that measure, which a 16 GB Mac has room for. The three MPS runs of the same
job on the same runner type took 395, 1 945 and 2 163 s, and the slow ones
swapped: a hosted runner shares its host, so a time from CI is an upper
bound, not the app's speed. The minimum device (an M1 with 16 GB) still has
to be measured.

### MPS, the Mac's CPU and the Linux CPU give the same labelmap

On the abdominal block, the organ model's labelmap on MPS and on the Mac's
CPU is **identical**: Dice 1.0 and the same volume for all 19 labels. On the
whole CT, the MPS labelmap from the Mac and the CPU labelmap from Linux have
the same number of voxels in every one of the 19 labels (3 182 500 labelled
voxels, no label differs by one; the Mac's labelmap stays on the runner, so
this compares counts, not positions). The device does not change the
result; it changes only the time.

### Why the Mac's CPU is that slow

One convolution of the organ model's first-stage size (32 to 32 channels,
3×3×3, on 224×96×96), timed with the bundled torch:

| where | oneDNN | threads | per convolution |
|---|---|---|---|
| M2 Pro runner, MPS | – | – | 1.73 s |
| M2 Pro runner, CPU | not in the macOS arm64 wheel | 3 | 87.6 s |
| Linux Xeon, CPU | yes | 4 | 0.32 s |
| Linux Xeon, CPU, oneDNN switched off | no | 4 | 3.7 s |

torch's macOS arm64 wheel has no oneDNN, so a 3D convolution on the CPU
takes torch's generic path, which unfolds the input into a buffer 27 times
its size: 7.1 GB for this one convolution. The 15 GB footprint of the CPU
run on the block fits that (inferred, not traced). On a 14 GB runner that
swaps, and the CPU is 50 times slower than MPS. Falling
back to the CPU on a Mac is therefore a last resort for a small image, not
a way to finish a whole CT (OPEN_QUESTIONS #15).

## The real case

The CT of one public whole-body FDG PET/CT from ACRIN-NSCLC-FDG-PET (CC BY
3.0, doi:10.7937/tcia.2019.30ilqfcl), fetched from the Imaging Data Commons
when a job runs; nothing of it is in the repository. 356 slices, converted
with the bundled dcm2niix. The patient weighed 42 kg, which is worth knowing
before reading the volumes: liver 820 mL, kidneys 65 and 85 mL, pancreas
12 mL, spleen 134 mL. All ten models ran offline; every labelmap is on the
CT's grid; 128 of the 144 structures are present, and the 16 missing ones
are bones outside the field of view (13 peripheral bones, two ribs, one
vertebra), which the export flags as `empty_label`.

### An independent check: TotalSegmentator

TotalSegmentator 2.18.0, a separately trained model, on the same CT (CPU,
no network, 100 s for twelve organs). It decides whether a small volume is
the patient or our chain (slimmed weights, trimmed runtime, the export in
place, the way back to the CT's grid).

| organ | MOOSE | TotalSegmentator | Dice |
|---|---|---|---|
| liver | 819.7 mL | 820.2 mL | 0.962 |
| spleen | 134.0 mL | 133.3 mL | 0.946 |
| trachea | 39.1 mL | 38.4 mL | 0.918 |
| kidney, left | 84.9 mL | 75.4 mL | 0.874 |
| kidney, right | 65.2 mL | 88.6 mL | 0.808 |
| adrenal gland, left | 1.1 mL | 1.3 mL | 0.758 |
| thyroid | 7.8 mL | 7.5 mL | 0.732 |
| pancreas | 12.4 mL | 16.7 mL | 0.654 |
| stomach | 87.6 mL | 197.1 mL | 0.589 |
| adrenal gland, right | 0.5 mL | 0.5 mL | 0.537 |
| urinary bladder | 10.0 mL | 79.2 mL | 0.117 |
| gallbladder | 1.1 mL | none | – |

Liver, spleen and trachea agree within 2 %: the chain does not distort the
result, and the small volumes are the patient. Where the two models
disagree, one case cannot say which is right. The voxels only
TotalSegmentator calls bladder average 240 HU (95th percentile 377), MOOSE's
bladder 13 HU, which is urine; the stomach voxels only TotalSegmentator
includes average 92 HU. This is what local validation before a study is for
(plan §16); the app shows both numbers only if both models run, and v1 runs
MOOSE alone.

PET, checked outside the app (SUV is phase 2): the organ labelmap resampled
to the PET grid by nearest neighbour gives liver SUVmean 1.48, spleen 1.37,
brain 4.50, bladder 27.4, and SUVmax 13.6 in the left lower lobe, where the
collection's own tumour segmentation (AIMI, BAMF) puts a 104 mL lesion. The
lungs measure 5 070 mL in MOOSE's five lobes and 5 303 mL in that
independent segmentation (−4.4 %).

## Trimmed runtime against the full one

Four models on the real CT, once with the full runtime and nnU-Net's process
pool, once as the app runs them (ADR 0014's removals, no network, semaphores
refused, the export in place; the vertebra, cardiac and body composition
runs also under the guard): **bit-identical labelmaps** for all four
(93 323 264 voxels each, none differ). As the app runs them, none was
slower:

| model | full runtime | as the app runs it |
|---|---|---|
| clin_ct_organs | 834 s, 7.82 GB | 753 s, 7.74 GB |
| clin_ct_vertebrae | 766 s, 9.93 GB | 582 s, 9.99 GB |
| clin_ct_cardiac | 741 s | 545 s, 6.91 GB |
| clin_ct_body_composition | 1 151 s | 916 s, 4.74 GB |

The runs shared a container with other work, so the differences in time say
only that the export in place (ADR 0015) and the guard (ADR 0016) cost
nothing measurable. The guard sees 115 997 audit events in a 79 s
inference, half of them `builtins.id`, and returns at once for every event
it does not refuse.

## Test-time mirroring

Two of the ten models, digestive and body composition, were trained with
nnU-Net's default trainer; the other eight use a NoMirroring trainer. MOOSE
leaves nnU-Net's predictor at its default, `use_mirroring=True`, so those two
predict every window eight times, once per combination of flipped axes. On
the real case the digestive model took 5 339 s with mirroring and 632 s
without, and the result is not the same:

| structure | mirrored | not mirrored | Dice |
|---|---|---|---|
| colon | 989.5 mL | 1 013.5 mL | 0.969 |
| esophagus | 28.9 mL | 28.8 mL | 0.958 |
| small bowel | 162.2 mL | 171.5 mL | 0.848 |
| duodenum | 18.3 mL | 11.2 mL | 0.550 |

The app keeps mirroring: a number that differs from what MOOSE gives
elsewhere for the same CT would be a number nobody can reproduce
(OPEN_QUESTIONS #16).

## The export, read back

The real case's results of all ten models, exported by the export job of
M5 (PR #2): wide 1 row × 1 461 columns, long 144 rows × 22 columns, seven
sheets each. Every sheet of the XLSX was read back and compared with the
export's own CSV files:

| reader | result |
|---|---|
| LibreOffice 24.2, headless, every sheet to CSV | same shape and names on all 14 sheets; numbers equal to 4.4 × 10⁻¹⁵ relative; no text differs |
| R 4, readxl | same names; largest difference 3.6 × 10⁻¹², the last bit of a double; pseudonyms stay text |
| pandas 2 with openpyxl, warnings as errors | no warning; only booleans (a boolean cell against `TRUE`), the export time and the format in the provenance differ |

Not checked yet: Excel itself and Numbers. Numbers is what opens an XLSX on
a Mac without Office, and it is said to hold at most 1 000 columns per table
(a secondary source; OPEN_QUESTIONS #17).

## Viewer scroll rate

Target ≥ 30 frames/s at 512×512 on an M1 (plan §9). Not measured yet (M4).
