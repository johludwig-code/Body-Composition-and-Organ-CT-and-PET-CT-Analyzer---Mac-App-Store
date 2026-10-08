# ADR 0025: Previews with pydicom and SimpleITK

- Status: accepted
- Date: 2026-10-08
- Plan section: §5, §7, §13

## Context

Plan §7's series table shows a preview of the middle slice, made when it is
needed. The worker has to decode whatever transfer syntax a source holds,
without a new dependency and without starting a program.

What the bundle can decode, measured against ground truth with the locked
packages on Linux (unsigned 12-bit and signed 16-bit synthetic images):

| transfer syntax | pydicom alone | with Pillow (bundled) | SimpleITK 2.5.6 | dcm2niix 1.0.20260724 |
|---|---|---|---|---|
| Explicit, Deflated | exact | exact | exact | Deflated refused |
| RLE | exact | exact | exact | ok |
| JPEG Baseline 8-bit | fails | lossy, ok | ok | ok |
| JPEG Extended 12-bit | fails | fails | ok | refused |
| JPEG Lossless SV1 | fails | fails | exact | ok |
| JPEG-LS lossless | fails | fails | exact | ok |
| JPEG 2000 lossless | fails | exact | exact | ok |

- python-gdcm is removed from the runtime (ADR 0014), and pylibjpeg is not in
  the lock, so pydicom alone has no decoder for JPEG data.
- imagecodecs is in the bundle through nnU-Net. Registered as a pydicom
  plugin it decodes JPEG Lossless, JPEG-LS and JPEG 2000 exactly and 8-bit
  and 12-bit JPEG (the 8-bit baseline is lossy by nature), and one frame of a
  multi-frame file on its own; native, RLE and Deflated data stay with
  pydicom. Using it makes it a direct dependency.
- dcm2niix may run only from M3 (the guard of ADR 0016), and it refuses
  Deflated and 12-bit JPEG.
- SimpleITK's native library contains socket, fork and exec calls that
  belong to `sitk.Show` and `ImageViewer`; the worker's Python audit hook
  cannot see native calls. Tracing import, reading and writing found none of
  them used.
- Decoding a 512² slice with pydicom and that plugin costs 1–2 ms (native,
  Deflated), 2 ms (RLE), 2–5 ms (JPEG Lossless, JPEG-LS) and 12–31 ms
  (JPEG 2000), median of 15; the whole preview with a 256² PNG took 3–7 ms,
  or 25–38 ms for JPEG 2000. SimpleITK took 31 ms for the RLE slice.
- An image marked with burned-in annotation may show text that identifies
  the patient.

## Decision

1. **On demand.** The app asks for the visible rows that have no PNG yet,
   debounced by 300 ms, at most 64 parts per job. They are made by a light
   `index` job in mode `previews`, outside the job queue's group `catalog`
   (ADR 0021). The worker reads the catalog in one short read transaction
   (middle file and frame, geometry, transfer syntax, flags) and closes it
   before decoding.
2. **Decoders.** pydicom decodes native, RLE and Deflated pixel data.
   SimpleITK, already a direct dependency (2.5.6), decodes everything else:
   JPEG lossless, JPEG-LS, JPEG 2000 and 12-bit JPEG. For NIfTI it reads the
   middle slice. No dependency is added.
3. **The image.** CT gets its rescale and a window of W400/L40; everything
   else is scaled from its 1st to its 99th percentile. MONOCHROME1 is
   inverted, RGB is kept. The longest side is reduced to 256 px (`size_px`)
   by area averaging, respecting non-square pixels.
4. **Writing.** `sitk.WriteImage(img, "<fingerprint>.png.partial",
   imageIO="PNGImageIO")`, then `os.replace` to
   `index/previews/<fingerprint>.png`, then an `artifact` event of the new
   kind `preview` with the relative path and its sha256. The file name is a
   hash of instance keys, not an identifier.
5. **Never previewed:** Secondary Capture classes; ImageType SCREEN SAVE;
   BurnedInAnnotation = YES; non-image objects, truncated files, and data
   neither decoder reads. Each such part gets a code
   (`preview.skipped.burned_in`, `preview.skipped.screen_save`,
   `preview.skipped.secondary_capture`, `preview.skipped.not_image`,
   `preview.skipped.truncated`, `preview.failed.decode`), and the app shows
   a placeholder with its text, for example "No preview: marked as
   containing burned-in text".
6. **No viewer.** `sitk.Show` and `ImageViewer` appear nowhere in the
   worker, because either would start a program; an AST test holds this, and
   the guard would refuse a program start anyway.
7. **Lifecycle.** Previews of fingerprints that no longer exist are deleted
   after each regroup, and all previews go with the rest of `index/` at
   Remove Identifiers (ADR 0024).

## Consequences

- A scan's commit waits at most for one short read of a previews job, under
  its busy timeout; previews and scans may run at the same time.
- The budget is at most 150 ms per native 512² preview on Linux, measured
  when the previews mode is built; the decode figures above leave room for
  it.
- `test_previews.py` covers native, RLE and Deflated data through pydicom,
  JPEG lossless, JPEG-LS, JPEG 2000 and 12-bit JPEG through SimpleITK, the
  window, the size, the skip rules and the atomic write. `make test-worker`
  adds imagecodecs 2026.8.16 to encode the compressed test files only; it is
  already in the bundle, so this adds no dependency to the app.
- pydicom decodes one frame of a native multi-frame file on its own (8.9 ms
  of 300 frames, 30 ms of 1 000). SimpleITK reads a multi-frame file as a
  whole volume (1.5 s and 9.0 s for the same files), so a compressed
  Enhanced object is the slow case, and the one where the imagecodecs
  plugin would be faster.

## Rejected alternatives

- **The imagecodecs plugin for pydicom.** It decodes every compressed syntax
  in the table and one frame on its own, but it would make imagecodecs a
  direct dependency, which needs an ADR and a license check of its own;
  SimpleITK already decodes the same syntaxes. It stays the recorded
  alternative.
- **Pillow.** It refuses 12-bit JPEG Extended, which CT archives contain.
- **python-gdcm.** Removed from the runtime by ADR 0014; the bundle check
  fails if it comes back.
- **dcm2niix.** It may not run before M3, and it refuses Deflated and 12-bit
  JPEG.
- **No previews beside a scan.** One short read is enough to keep the two
  apart (ADR 0021).
