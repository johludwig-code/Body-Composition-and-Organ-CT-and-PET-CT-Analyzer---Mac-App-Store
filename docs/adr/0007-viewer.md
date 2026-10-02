# ADR 0007: Own slice renderer on canonical LPS volumes

- Status: accepted
- Date: 2026-10-02
- Plan section: §9

## Decision

The worker reorients every volume to LPS with `SimpleITK.DICOMOrient` and
writes raw `int16`/`uint8`/`uint16` plus `meta.json`. Swift memory-maps the raw
files and never parses NIfTI. `ViewerGeometry` in BCOAKit holds the mapping
from volume axes to screen axes and the orientation letters, and is unit-tested
against the table in §9 with a marker phantom.

## Why not reuse the BOCARTA-MOOSE viewer

Its viewer reads NIfTI in Swift (`DicomCore/NIfTI`) and resolves orientation
there; the plan deliberately moves that ambiguity out of Swift. Its overlay
rendering ideas (nearest-neighbour overlay layer, per-label alpha) carry over.
