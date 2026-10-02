# ADR 0012: What was taken from BOCARTA-MOOSE, and what was not

- Status: accepted
- Date: 2026-10-02

## Context

The owner asked for a fresh start with BOCARTA-MOOSE as the only source of
code and implementation ideas. That app (BTM, a fork of Bocarta) drives BOA,
TotalSegmentator and MOOSE through three Python environments installed into
`~/boa-m0` at first launch. Its architecture is the opposite of what the App
Store allows, so its code is a reference, not a base.

## Taken (as knowledge, re-implemented here)

- **MOOSE facts measured on a real Mac** (`docs/moose-phase0.md` there): masks lie
  on the image grid; MOOSE's own statistics are in mm³, not mL; label prefixes
  have the modality in capitals; `organ_indices` carry SNOMED codes; weights are
  ~475 MB (organs), ~672 MB (body composition), ~459 MB (vertebrae); one model on
  a 512×512×200 CT took 91 s on MPS; `clin_mr_FVM` ships without `dataset.json`
  and cannot run. The size and runtime estimates in spike S4 start from these.
- **Small fragments at the edge of the field of view**: MOOSE reported a 0.1 mL
  spleen in a pelvic CT. This becomes the auto-QC flag `tiny_component`
  (proposed in OPEN_QUESTIONS.md as an addition to §12).
- **Rules that cost BOCARTA-MOOSE a bug each**: lenient decoders for saved
  settings (a synthesised `Codable` decoder throws on a new key), "a number that
  matters gets a test", warnings in the build output are read.
- **PET SUV arithmetic** with a per-slice rescale slope (`boa_pet.py` and its
  phantom test) — for phase 2, not copied yet.

## Not taken

- The Homebrew/uv first-run installer, `~/boa-m0`, BOA and TotalSegmentator,
  WeasyPrint PDF reports, the CUDA→MPS shim (MOOSE supports MPS natively), the
  DICOM network code (no PACS in v1), and the NIfTI handling in Swift.
- No source file was copied verbatim. Where a later milestone ports code (PET
  SUV in phase 2), the file names its origin and the Apache-2.0 notice of
  BOCARTA-MOOSE is carried into THIRD_PARTY_NOTICES.
