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
| site-packages from `requirements.lock`, macOS arm64 wheels | 1 170 MB | 297 MB | 2026-10-02, `uv pip install --target`, wheels for `macosx_14_0_arm64` |
| of which torch | 529 MB | | |
| of which SimpleITK | 170 MB | | |

**Estimate for the whole app with every clinical model: about 2.6 GB
installed, about 1.6 GB to download.** The Swift app itself is a few MB. To be
confirmed by the real bundle on a Mac (spike S4) and against App Store
Connect's limits in S3. Candidates for trimming before then: packages pulled
in only for training or downloads (`httpx`, `huggingface_hub`, the `nnUNetv2_*`
training entry points), `gdcm` if pydicom does not need it for the
compressions seen in practice.

## Runtime per model

| model | volume | device | wall time | peak RSS | measured on |
|---|---|---|---|---|---|
| clin_ct_organs | 512×512×200, 1.17×1.17×2.0 mm | MPS | 91 s | – | BOCARTA-MOOSE, docs/moose-phase0.md, 2026-10-01, Apple Silicon |
| clin_ct_organs | 192×192×100 phantom, 1.5×1.5×2.5 mm | CPU, 4 cores | 79 s | 3.8 GB | 2026-10-02, Linux build container, torch 2.14.1, offline, slimmed weights |

## Viewer scroll rate

Target ≥ 30 frames/s at 512×512 on an M1 (plan §9). Not measured yet (M4).
