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

## Viewer scroll rate

Target ≥ 30 frames/s at 512×512 on an M1 (plan §9). Not measured yet (M4).
