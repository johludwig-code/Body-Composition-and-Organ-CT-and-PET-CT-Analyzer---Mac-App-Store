# ADR 0018: The adapter corrects two faults of moosez 3.2.2

- Status: accepted
- Date: 2026-10-03
- Plan section: §4 "MOOSE-Adapter", §12 (QC and reproducibility), §16 (validation), §19 (MOOSE upstream)

## Context

Building the sample PDF report from the public PET/CT (`docs/benchmarks.md`,
"The real case") showed two faults in what moosez 3.2.2 returns. Neither
raises an error, and both reach the export as plausible numbers.

**The lungs model sees the image mirrored.** moosez turns every input to RAS
before inference. Every model with left and right labels comes as a 2025
`_ras_` weight set, except `clin_ct_lungs`, which moosez 3.2.2 still fetches
with its 2023 weights. On the public CT its two "left" lobes lay in the right
lung (Dice 0.00 against the organ model's lobes of the same name), the left
lung had three lobes, and the "middle lobe" of 155 mL lay on the left. The
structure is mirrored, not only the names, so renaming cannot repair it.

**Block edges of the resampled image are water.** moosez resamples the CT to
the model's spacing in blocks (`ImageResampler.resample_image_SimpleITK_DASK_array`):
each axis longer than 150 voxels is cut into equal blocks, and each block is
resampled on its own. When the block's last output slice falls beyond its
last input slice, SimpleITK fills it with 0. The public CT has 356 slices of
2.5 mm, two blocks of 178; at 1.5 mm each block yields 297 slices, the last
1.5 mm beyond the input, so resampled slices 296 and 593 were 0 HU
everywhere, a plane of water across the body. The first plane lies in the
chest: in CT slices 178 and 179 the lungs model found 1 304 and 1 407 lung
voxels instead of about 23 600, the organ model 1 506 and 6 299. The plane
reaches further than its own slice: nnU-Net predicts in overlapping
windows, and every window that holds the plane predicts differently. The
organ model's labelmap changed wherever a window could reach the plane, up
to 80 CT slices (200 mm) from it, and nowhere else. The number of such
planes and where they fall depends on each model's spacing and each CT's
size.

Neither fault is visible in the export: the volumes are the size of real
organs, and a lobe on the wrong side keeps its name.

## Decision

The adapter (`Worker/bcoa_worker/moose_adapter.py`, the only place where
MOOSE is adapted) corrects both for moosez 3.2.2, the only version it
accepts:

1. For the models in `MIRRORED_MODELS`, which is `clin_ct_lungs` alone, the
   CT is mirrored along the image axis closest to the patient's left-right
   axis (read from the direction matrix) before MOOSE sees it, and the
   labelmap is mirrored back. The labelmap must be on the CT's grid, or the
   job fails rather than mirror something else.
2. `harden()` replaces `ImageResampler.resample_chunk_SimpleITK` with the
   same call to `SimpleITK.Resample` and `useNearestNeighborExtrapolator`
   set: an output slice beyond the block takes the block's nearest slice
   instead of 0. `harden()` refuses a moosez whose block resampler has
   another signature.

What stays as moosez does it: the blocks themselves, and with them a shift
of a third of a voxel at each block edge (the second block's first slice
lies at 445 mm of the input and is placed at 445.5 mm); the inference
chunking; and every other model's orientation.

## Consequences

- On the public CT (`docs/benchmarks.md`, "Two faults of moosez 3.2.2") the
  lungs model's lobes now match the organ model's (Dice 0.956 to 0.986), the
  organ model's five lobes grew from 5 070 to 5 262 mL (the collection's
  independent segmentation: 5 303 mL), and the organs within a window of
  the chest plane moved as well: liver 819.7 to 809.1 mL, kidneys 84.9 to
  80.1 and 65.2 to 62.1 mL, stomach 87.6 to 82.6 mL. The urinary bladder,
  out of every window's reach, kept every voxel.
- The app's numbers for a CT differ from MOOSE's own for the same CT: the
  lungs model's everywhere, every other model's within a window of a block
  edge. The
  methods text of the export must say so; it is written by the export job
  (PR #2), which gets that sentence when both changes are on main.
- The corrections are tied to moosez 3.2.2 and to the pinned weights
  (`manifest.json`, by checksum). A new moosez version, or new lungs
  weights, means measuring both faults again and dropping a correction that
  is no longer needed; the version pin and the signature check make that a
  failed build, not a silent double correction.
- Both faults are worth reporting to MOOSE's maintainers (OPEN_QUESTIONS
  #18 and #19); that is the owner's call.

## Rejected alternatives

- **Swap the lungs model's left and right label names.** The left lung got
  three lobes and the right two; the anatomy is mirrored, not the names.
- **A wider overlap between inference chunks.** The first suspicion, since
  moosez also cuts long images into chunks for inference with 20 slices of
  overlap. A run with 112 slices of overlap (half a patch, peak memory
  6.50 GB) still missed the lung in CT slices 178 and 179; the 0 HU plane is
  already in the resampled image.
- **Resample the whole image in one piece.** It removes the plane and the
  third-of-a-voxel shift, and moves the organs a little further than the
  plane alone does: liver 801.3 mL instead of 809.1, kidneys 78.2 and 61.7
  instead of 80.1 and 62.1, peak memory 7.72 GB. That remainder is what
  nnU-Net gives for an image shifted by a fraction of a voxel; the plane of
  water is a fault. Keeping the blocks keeps the app's numbers as close to
  MOOSE's as the fault allows.
- **Leave both and flag them in QC.** A flag cannot give back 190 mL of lung
  or put a lobe on the right side.
- **Patch MOOSE's sources.** The plan allows that only in an emergency
  (§4, §20); a runtime patch in the adapter does the same and leaves the
  sources as they are.
