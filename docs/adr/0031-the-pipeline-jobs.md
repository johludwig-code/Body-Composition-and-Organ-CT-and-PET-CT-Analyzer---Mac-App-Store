# ADR 0031: The pipeline (M3): one job per stage, chained by the app

- Status: accepted (built on the owner's request of 22 October 2026, ADR 0030)
- Date: 2026-10-22
- Plan section: §5, §6, §8, §12

## Context

Plan §8 runs every selected series through five fixed stages: conversion,
checks, segmentation, metrics and the viewer cache; a failure stops that
series and never the batch. Plan §5 starts a fresh worker per job, names
the job kinds `convert`, `segment`, `metrics` and `viewer_cache`, and lets
light jobs run beside the one segmentation that holds the GPU. The
segmentation job existed (S1); the other three, the run, the results and the
order of the stages did not.

## Decision

1. **One job per stage and series; the app chains them.** `convert` (stages
   1 and 2) → `segment` (all chosen models, one MOOSE call each) → `metrics`
   → `viewer_cache`. The app queues the next stage when one ends `done`, so
   the light conversion of the next series runs while the current one is
   segmented, a crash costs one stage, and a failed stage leaves the stages
   before it in place. Each payload is built from the database when the job
   is queued, never stored: the convert payload holds the source roots, and
   `project.sqlite` holds no paths of source files (ADR 0020).
2. **Conversion takes the index's files.** The convert job reads the files
   of the series' catalog part (`cat_instances`) from the catalog read-only,
   refuses a catalog whose `catalog_id` is not the one the series was merged
   from (`catalog_changed`, recoverable), copies the files into its scratch
   folder under numbered names, and runs the bundled dcm2niix there with
   relative arguments (`-z n -b y -ba y -i n -f ct`): no compression, because
   dcm2niix looks for pigz to compress and the worker starts no program but
   dcm2niix; a BIDS sidecar without identifying fields; every image kept,
   because a series chosen by hand may be derived. The guard allows exactly
   the bundled dcm2niix (ADR 0016). dcm2niix's output stays out of the log
   apart from its exit code and the number of warnings; warnings about
   spacing or tilt become QC flags.
3. **The volume dcm2niix corrected is the one measured.** For a gantry tilt
   or uneven spacing dcm2niix writes a corrected copy (`_Tilt_`, `_Eq_`)
   beside the original; the job takes it and flags `gantry_tilt_corrected` or
   `spacing_equalized`. A NIfTI file has one grid, and the original of an
   unevenly spaced series is a grid the slices do not lie on. The `_Eq_`
   volume interpolates the missing slices, which plan §10 otherwise avoids
   for metrics: open question 31.
4. **A stack dcm2niix splits is read with SimpleITK.** When dcm2niix writes
   more than one volume for a part of several single-frame files, which the
   index has already found to be one stack, the job reads the numbered copies
   in the index's order with `ImageSeriesReader` and flags
   `converter_fallback`. Seen on the public Pancreas-CT collection, whose 240
   files each carry another StudyTime, which dcm2niix reads as 240
   acquisitions. A multi-frame file keeps dcm2niix as its only converter.
5. **Checks (stage 2).** Too small (under 16 × 16 × 3) and no air (the first
   percentile of the CT above −900 HU: "check rescale slope/intercept") are
   errors; slices over 3 mm are `thick_slices`. The geometry and flags go
   into `work/<series_key>/convert.json` and the result.
6. **Segmentation** keeps its job; the app sends `retry_on_cpu: false`
   (open question 15: the Mac's CPU takes hours for a whole CT).
7. **Metrics** read the CT and every labelmap, resample a labelmap that is
   not on the CT's grid by nearest neighbour (`labelmap_resampled`), and
   write one row per label of the model's manifest entry, present or not;
   series flags and `device_fallback` join each row's flags. The app inserts
   the rows into `results` in one transaction.
8. **The viewer cache** is ADR 0007's: `DICOMOrient(…, "LPS")`, raw
   little-endian `int16` and `uint8`/`uint16`, and `meta.json` with size,
   spacing, origin, direction, an `oblique` flag and the labels present,
   written to a hidden folder and renamed into place.
9. **Runs.** Starting a batch takes the newest run that is not locked and
   has the same models and settings, or makes a new one: `run_id` `R` + UTC
   time, versions from the worker's hello plus the app's version, the chip
   and macOS, the device asked for, the models with their archive SHA-256
   and labels from the bundle's manifest, and the settings (device, CPU
   retry, dcm2niix flags, pipeline version). The export locks it (plan §6).
10. **Jobs are recorded.** Every pipeline job is a row of `jobs` with run,
    series, kind, status, times, device and error code. At launch a row left
    `queued` or `running` becomes `interrupted` and its stage is queued
    again from the database (plan §8).

11. **Labelmaps and viewer caches belong to the run.** The segmentation
    writes to `work/<series_key>/runs/<run_id>/labels/` and the viewer cache
    to `viewer-cache/<run_id>/<series_key>/`; the converted CT stays one per
    series. Found when a second run of a series overwrote the labelmaps of a
    locked run: its export's masks and its viewer would have shown another
    run's segmentation beside the first run's numbers. The export takes the
    run's labelmap, and a project from before this keeps working from the
    old paths.

## Consequences

- A series that failed shows its stage and error code; "Retry Failed" queues
  that stage again.
- The cache rule of plan §6 (reuse a result when the content fingerprint,
  model checksum, pipeline version and device class agree) is not built: a
  series already measured in the run is skipped, one in another run is
  segmented again.
- The converted CT stays in `work/` until the viewer cache is written; then
  it is deleted, as plan §8 allows, because it can be made again from DICOM.

## Rejected alternatives

- **One job for all five stages.** Simpler to chain, but the GPU would be
  held through conversion and caching, and a crash would cost the whole
  series.
- **dcm2niix on the source folder.** It would convert every series in it and
  print file names that often carry patient names.
- **SimpleITK for every series.** It would avoid a program in the worker,
  but the plan pins dcm2niix, the index's rule for readable transfer
  syntaxes is dcm2niix's, and dcm2niix knows the vendors' scaling and
  multi-frame layouts.
