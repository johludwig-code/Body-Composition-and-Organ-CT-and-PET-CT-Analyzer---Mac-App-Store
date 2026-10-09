# Benchmarks

Measured, not estimated. Every row names the machine, the date and the
versions. Minimum device: M1 with 16 GB (plan §16).

## Model size in the bundle

Decimal megabytes (10^6 bytes). "Slimmed" is what goes into the bundle:
validation folders and logs pruned, optimizer state removed from
`checkpoint_final.pth`, network weights unchanged (`Scripts/fetch_models.py`).
The first measurement of `clin_ct_organs` said 118 MB; that was MiB, the same
122.7 × 10^6 bytes.

| model | labels | archive | slimmed | measured on |
|---|---|---|---|---|
| clin_ct_organs | 19 | 459 MB | 123 MB | 2026-10-02, Linux build container, moosez 3.2.2 |
| clin_ct_cardiac | 13 | 459 MB | 123 MB | same |
| clin_ct_lungs | 5 | 230 MB | 123 MB | same |
| clin_ct_digestive | 4 | 457 MB | 123 MB | same |
| clin_ct_muscles | 10 | 459 MB | 123 MB | same |
| clin_ct_ribs | 27 | 460 MB | 123 MB | same |
| clin_ct_vertebrae | 28 | 459 MB | 123 MB | same |
| clin_ct_peripheral_bones | 31 | 461 MB | 123 MB | same |
| clin_ct_body | 4 | 469 MB | 123 MB | same |
| clin_ct_body_composition | 3 | 672 MB | 179 MB | same |
| clin_ct_fast_vertebrae (needed by body_composition) | 28 | 459 MB | 123 MB | same |
| **all clinical models** | 144 + 28 | 5 045 MB | **1 406 MB** | same |

Ten of the networks have the same size, 122.4 MB per `checkpoint_final.pth`;
the body composition network is larger (178.9 MB). The weights are float32 and barely compress: gzip takes 7 % off one
checkpoint, so the download is close to the installed size.

## Runtime and packages in the bundle

| part | installed | gzip | measured on |
|---|---|---|---|
| CPython 3.12.11 (python-build-standalone, stripped) | 45 MB | 15 MB | 2026-10-02, macOS arm64 archive |
| site-packages from `requirements.lock`, macOS arm64 wheels, before ADR 0014's trim | 1 170 MB | 297 MB | 2026-10-02, `uv pip install --target`, wheels for `macosx_14_0_arm64` |
| of which torch | 529 MB | | |
| of which SimpleITK | 170 MB | | |
| the Release app from `bundle.yml`, after the trim, with `clin_ct_organs` only | 1 392 MB: Python 1 253 MB, weights 123 MB, signatures 8 MB, app 6 MB, notices 1 MB | | 2026-10-04, macos-15 arm64 runner, run 37200011634, the size report of `verify_bundle.py` |

**The whole app with every clinical model: about 2.7 GB installed, about
1.6 GB to download.** The installed size is the measured Release app with the
other ten models added (1 392 − 123 + 1 406 MB); the download is estimated from
the gzip column. Still to be checked against App Store Connect's limits in S3.
The first two rows are from before ADR 0014's trim, which has since removed
python-gdcm, setuptools, blosc2's `libtcc`, the real cc3d (a stub instead), the
console scripts with the `nnUNetv2_*` entry points, and torch's headers and
protobuf compiler. Still in the runtime and candidates for trimming:
`huggingface_hub` and `hf_xet` (15 MiB together by `du` in the same run, about 16 MB) and `httpx`.
`requests` and `urllib3` stay, because moosez imports them (ADR 0016).

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
before reading the volumes: liver 809 mL, kidneys 62 and 80 mL, pancreas
11 mL, spleen 132 mL. All ten models ran offline; every labelmap is on the
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
| liver | 809.1 mL | 820.2 mL | 0.961 |
| spleen | 131.9 mL | 133.3 mL | 0.944 |
| trachea | 39.3 mL | 38.4 mL | 0.917 |
| kidney, left | 80.1 mL | 75.4 mL | 0.864 |
| kidney, right | 62.1 mL | 88.6 mL | 0.788 |
| adrenal gland, left | 1.1 mL | 1.3 mL | 0.752 |
| thyroid | 7.7 mL | 7.5 mL | 0.733 |
| pancreas | 11.1 mL | 16.7 mL | 0.618 |
| stomach | 82.6 mL | 197.1 mL | 0.570 |
| adrenal gland, right | 0.6 mL | 0.5 mL | 0.546 |
| urinary bladder | 10.0 mL | 79.2 mL | 0.117 |
| gallbladder | 0.7 mL | none | – |

MOOSE's column is the organ model with both corrections of ADR 0018; the
numbers before them are in "Two faults of moosez 3.2.2" below.

Liver, spleen and trachea agree within 2.5 %: the chain does not distort the
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
lungs measure 5 262 mL in MOOSE's five lobes and 5 303 mL in that
independent segmentation (−0.8 %; −4.4 % before ADR 0018).

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

## Two faults of moosez 3.2.2

Both found on the real case while building the sample PDF report, both
corrected in the adapter (ADR 0018), both measured by running the models
again on the same CT with the corrected adapter.

**The lungs model sees the CT mirrored.** Its lobes against the organ
model's lobes of the same name, before and after the adapter mirrors the CT
for it:

| lobe | lungs model, before | after | organ model, before | after | Dice, before | after |
|---|---|---|---|---|---|---|
| upper lobe, left | 1 180.7 mL | 1 429.3 mL | 1 427.3 mL | 1 442.8 mL | 0.00 | 0.981 |
| lower lobe, left | 1 601.8 mL | 1 029.6 mL | 879.3 mL | 988.9 mL | 0.00 | 0.956 |
| upper lobe, right | 1 225.3 mL | 871.6 mL | 859.2 mL | 858.1 mL | 0.00 | 0.973 |
| middle lobe, right | 155.0 mL | 318.1 mL | 307.1 mL | 332.2 mL | 0.00 | 0.956 |
| lower lobe, right | 1 055.5 mL | 1 636.2 mL | 1 597.6 mL | 1 640.0 mL | 0.00 | 0.986 |

Before, the lungs model's "left" lobes lay in the right lung and its left
lung had three lobes; the organ model's columns change because of the
second fault.

**Block edges of the resampled image are water.** moosez resamples in
blocks, and the last output slice of each block lay beyond the block's
input and came out as 0 HU: resampled slices 296 and 593 of 593, one in the
chest (CT slice 178), one in the last slice, just above the brain. Lung
voxels per CT slice:

| CT slice | lungs model, before | after | organ model's lobes, before | after |
|---|---|---|---|---|
| 176 | 22 939 | 23 649 | 21 230 | 23 513 |
| 177 | 21 523 | 23 687 | 12 679 | 23 606 |
| 178 | 1 304 | 23 712 | 1 506 | 23 482 |
| 179 | 1 407 | 23 629 | 6 299 | 23 502 |
| 180 | 24 017 | 23 931 | 19 871 | 23 802 |
| 181 | 24 291 | 23 969 | 23 637 | 23 883 |

(The lungs model's "before" is the mirrored run, so its slices compare
counts, not lobes.) The organ model's five lobes grew from 5 070 to
5 262 mL, against 5 303 mL in the collection's independent segmentation.

The plane reached further than its own slice. nnU-Net predicts in
overlapping windows, and every window that held the plane predicted
differently: the organ labelmap changed in CT slices 100 to 258 around the
chest plane and 300 to 348 below the top one, and in no other slice. So
the organs within a window of the chest moved too:

| organ | before | after | Dice, before against after |
|---|---|---|---|
| liver | 819.7 mL | 809.1 mL | 0.991 |
| kidney, left | 84.9 mL | 80.1 mL | 0.967 |
| kidney, right | 65.2 mL | 62.1 mL | 0.972 |
| stomach | 87.6 mL | 82.6 mL | 0.949 |
| pancreas | 12.4 mL | 11.1 mL | 0.927 |
| spleen | 134.0 mL | 131.9 mL | 0.991 |
| brain | 1 253.9 mL | 1 253.3 mL | 0.997 |
| urinary bladder | 10.0 mL | 10.0 mL | 1.000 |

The bladder, in CT slices 32 to 39, is out of every window's reach. The
change does not bring the abdominal organs closer to TotalSegmentator
(kidney, left: Dice 0.874 before, 0.864 after; pancreas 0.654 and 0.618):
it is the model's answer to a CT without a plane of water in it, not a
better model.

The other eight models, run again with both corrections (12 226 s for the
last five on four cores, peak 10.6 GB):

| model | voxels that differ | largest change |
|---|---|---|
| peripheral bones | 12 142 | right metacarpals 4.2 to 3.5 mL |
| cardiac | 11 210 | left ventricle 47.7 to 50.5 mL, myocardium 44.8 to 47.2 mL |
| vertebrae | 4 558 | T10 30.7 to 33.5 mL |
| ribs | 2 720 | no rib by more than 0.2 mL |
| muscles | 1 953 | left paraspinal muscles 280.3 to 278.9 mL |
| digestive | 891 | duodenum 18.3 to 17.3 mL |
| body | 0 | – |
| body composition | 0 | – |

Two models have no plane to fill: the body model resamples to 5 mm, and
445 mm of input make exactly 89 slices; the body composition model works on
a crop around L3, shorter than one block, that its fast vertebra model
(3 mm, 148 slices per block, the last one inside the input) finds first. Of
the 144 structures, 128 are present, as before.

Two things were tried first and did not help or were not taken:

| attempt | result |
|---|---|
| a wider overlap between moosez's inference chunks (112 slices instead of 20, half a patch) | still 662 and 747 lung voxels in CT slices 178 and 179; peak 6.50 GB. The plane is already in the resampled image |
| resampling the whole image in one piece instead of in blocks | the plane is gone, and so is the block grid's shift of a third of a voxel; the organs move a little further (liver 801.3 mL, kidneys 78.2 and 61.7 mL); 1 777 s and 7.72 GB for lungs and organs. Not taken: it moves the numbers away from MOOSE's beyond what the fault needs |

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

## Import scan

The scan side of the index job (walk, header read, catalog writes) against the
budgets of ADR 0026 decision 3. Linux build container, Intel Xeon at 2.1 GHz,
4 cores, 15 GB; Python 3.12, pydicom 3.0.2, SimpleITK 2.5.6; 2026-10-09. The
trees were written just before, so the page cache was warm. The rows below
were measured while the regroup was still a stub; the regroup's own numbers
follow the table.

| tree | full scan | unchanged rescan | peak RSS | budget |
|---|---|---|---|---|
| synthetic corpus, 1 780 files, in process | 2.7 s | 0.06 s | – | – |
| same, worker child as the app starts it | 2.3 s | – | – | – |
| 10 000 files, one series copied | 9.1 s | 0.17 s | 88 MB | < 60 s and < 2 s |
| 10 000 files with GE-like private groups | 10.5 s | 0.16 s | – | same |
| 100 000 files, distinct series | 88.2 s: walk 0.5 s, read 87.6 s | 1.37 s: walk 0.57 s | 124 MB | ≤ 150 s, ≤ 10 s, ≤ 500 MB |

The peak RSS of the 100 000-file row is that of the whole benchmark process,
which also wrote the tree, so it is an upper bound for the scan. A cancel by
SIGTERM in the middle of the read, with the shipped batches of 1 000 files,
was answered with exit 130 in 0.07–0.08 s (three trials over 5 000 files),
against a budget of 2 s; the next scan read only the files of the batch
rolled back and those after it. `Worker/tests/test_index_budget.py` holds the
10 000-file budgets on every run and the 100 000-file ones under `-m slow`;
`test_index_cancel.py` holds the cancel and the resume.

### The regroup

Same machine and day, with the regroup of ADR 0022 built
(`index/group.py`). "group" is the regroup's own time as the result reports
it; "job" is the worker child from start to exit, as the app starts it.

| catalog | group | job | peak RSS | budget |
|---|---|---|---|---|
| synthetic corpus, 1 780 files: 44 studies, 130 parts, 18 split off, 61 duplicates | 0.07–0.09 s | – | – | – |
| 100 000 distinct files: 100 studies of 4 series of 250 slices, rows written straight into the catalog, mode `regroup` | 3.6–3.9 s (three trials) | 5.0–5.1 s | 246 MB | ≤ 20 s, ≤ 500 MB |
| same, every file listed by a DICOMDIR, after the corrections of ADR 0029 | 3.7–4.0 s (two trials) | 4.8–5.1 s | 283 MB | same |
| 100 000 files, 400 copies of one series of 250 slices, full scan | 1.4 s | 102.4 s: walk 0.6 s, read 100.0 s | 92 MB | ≤ 150 s, ≤ 500 MB |

The distinct-file row is the regroup's budget case: every file is an
instance of its own, so every part, check and `cat_instances` row is
written. The copy tree is the duplicate case: 99 750 losers and one part.
Its read took 100 s against the 87.6 s of the table above while other tests
ran beside it, and stays inside the budget. The peak RSS is that of the
worker child alone (`RUSAGE_CHILDREN`), which in the distinct-file row did
nothing but the regroup; the same job on a catalog without files peaks at
56 MB, so the regroup of 100 000 files adds about 190 MB. Run in-process
twice in a row, the same regroup took 3.4 s and 3.8 s, so a second
generation, which also matches every part against the previous one, costs
about as much as the first. `Worker/tests/test_index_budget.py` holds the
distinct-file budget under `-m slow`, and the full scan's budget with the
regroup in it.

The DICOMDIR row is the completeness check's worst case (ADR 0029 decision
16). Matched in SQL, it had made the regroup of 20 000 such files take 45 s
against 0.7 s without the DICOMDIR, growing with the square of the files;
matched against a set of SOP UIDs read once, it adds about 0.3 s and 30 MB
at 100 000. The same job without the DICOMDIR measured 3.6 s and 252 MB that
day, with the catalog connection's cache spill turned off (ADR 0029
decision 11), against the 246 MB above.

Reading through pydicom's `Dataset.get` cost about 1.2 s per 10 000 files,
because it resolves and converts the element again on every call; the reader
converts every element of the header once instead (`read.Flat`).

## Viewer scroll rate

Target ≥ 30 frames/s at 512×512 on an M1 (plan §9). Not measured yet (M4).
