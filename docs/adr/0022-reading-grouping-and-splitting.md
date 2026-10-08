# ADR 0022: Reading, grouping and splitting

- Status: accepted
- Date: 2026-10-08
- Plan section: §7

## Context

Plan §7 asks the index job to read headers only (pydicom with
`stop_before_pixels` and the tags it needs), to recognize DICOM by the
"DICM" preamble, to use DICOMDIR, to group by patient, study and series, to
split a series whose orientation or image size changes into marked parts, to
count duplicates across folders once, to rescan incrementally, and to cope
with Enhanced CT, 100 000 files, unreadable files and NIfTI files named
`CT_<id>.nii.gz`.

BOCARTA-MOOSE (BTM) shows what goes wrong:

- Unpacking a ZIP beside the archive put every instance into the scan
  twice. 675 slices became 1 350, the slice gaps collapsed to 0, and an
  88 cm patient was reconstructed 2.9 m long.
- BTM never splits a series; orientation, size and spacing come from its
  first file.
- Its duplicates go to the first file seen, and its crawler appends files
  in the order its threads finish, so the winner can change between scans.
- Its header cache is dropped whole when its schema version changes,
  because a stale cache silently lacks new fields.

Measured with the locked pydicom 3.0.2 on Linux (Xeon at 2.1 GHz, synthetic
files):

- `dcmread(force=True)` never fails: it returned a dataset for an empty
  file, a ZIP, a NIfTI file and random bytes.
- Reading 100 000 headers through one 64 KB buffer per file takes 62 s warm
  and 80 s cold (76.6 s and 96.4 s straight from the file), and 100–111 s
  with GE-like private headers. Threads make it slower: 0.70 ms per file on
  one thread, 3.0 ms on two, 5.7 ms on four. Process pools fail in the
  sandbox (ADR 0015).
- Reading the per-frame geometry of an Enhanced CT costs 21–115 ms per file.
- `pydicom.fileset.FileSet` silently drops DICOMDIR records whose file is
  missing (14 of 15 loaded with one file deleted; 0 of 15 with lower-case
  file names on a case-sensitive file system) and takes 1.37 s for 2 000
  records, where walking the records directly takes 174 ms.
- `import pydicom` loads its downloader. In an environment with requests
  2.34.2 and urllib3 2.8.0, the import loads urllib3, which makes one IPv6
  socket attempt; the guard of ADR 0016 refuses and logs it.
- Validation warnings quote raw values, for example
  `Invalid value for VR UI: '1.2.x'`.
- A file cut inside its pixel data reads its header without an error.

Designs A and C (ADR 0020) carried a part's key over by its number within
the series. Both judges found that a changed split then re-binds a key, with
its selection, QC and results, to different images.

## Decision

1. **Discovery.** Each source is walked with an iterative `os.scandir` over
   bytes paths and `follow_symlinks=False`. The relative path is stored as
   raw bytes; it is the key and what opens the file. NFC is used only to
   match names (NIfTI prefixes, description terms) and to display labels.
   - Symbolic links are counted (kind `symlink`) and not followed, because
     of loops and of links that leave the folder the user granted.
   - `.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini` and the folders
     `.Spotlight-V100`, `.Trashes`, `.fseventsd`, `.TemporaryItems` and
     `.DocumentRevisions-V100` are skipped without a row, and counted.
   - A folder that cannot be listed gets a `bad_dirs` row
     (`permission_denied` for EACCES or EPERM, `io_error` otherwise), and
     the walk goes on. Rows below a bad folder are never treated as stale.
   - A root that cannot be reached marks the source `unreachable`, and
     nothing is deleted. A walk that finds nothing under a root for which the
     catalog holds files counts as unreachable too (summary code
     `source.empty_walk`), because a dropped network share often looks like
     an empty folder. To drop a source's series, the user removes the
     source.
2. **Recognition by content.**

   | content | kind |
   |---|---|
   | bytes 128–131 are `DICM` | DICOM Part 10 |
   | `.nii` or `.nii.gz` (case-insensitive, NFC) with a valid NIfTI-1 header (`sizeof_hdr` 348, magic `n+1` or `ni1`) | `nifti` |
   | ZIP, gzip that is not NIfTI, tar (`ustar` at 257), 7z, RAR | `archive`: counted, not opened |
   | anything else, including raw datasets without a preamble | `not_dicom` |

   Raw datasets count as `not_dicom` because the plan names the preamble as
   the test, and the alternative is parsing arbitrary files.
3. **Header read.** Each file is opened by its bytes path and read through a
   64 KB buffer with `dcmread(f, stop_before_pixels=True,
   specific_tags=TAGS)`, `config.settings.reading_validation_mode =
   config.IGNORE`, warnings suppressed and the pydicom logger at CRITICAL,
   because validation warnings quote raw values.
   - `bcoa_worker/index/__init__.py` sets `sys.modules["requests"] = None`
     (if `requests` is not already imported) before the first pydicom
     import. Measured: urllib3 is then not loaded, no socket is attempted,
     and pydicom still reads and decodes.
   - PatientBirthDate is read only to compute the age in memory and is never
     stored. PatientID, IssuerOfPatientID and AccessionNumber are requested
     only when the job carries a link key (ADR 0024).
   - Enhanced and Legacy Converted multi-frame objects are read a second
     time with SharedFunctionalGroupsSequence and
     PerFrameFunctionalGroupsSequence; each frame gets a row with position,
     orientation and StackID.
4. **Pixel data without reading pixels.** From the file position after
   `stop_before_pixels`, the worker reads the next element header. Pixel
   Data (7FE0,0010) with a defined length that runs past the end of the file
   is `truncated`; with an undefined (encapsulated) length it is `truncated`
   when the file's last 8 bytes are not the Sequence Delimitation Item
   (FFFE,E0DD); an image SOP class without a pixel element is `missing`;
   anything else is `ok`.
5. **Changing and unreadable files.** A file is stat'ed before it is opened
   and after it is read. If its size or modification time changed, its kind
   is `changing`, it is read again at the next scan, and the source gets
   `check.files_changing`. Every exception makes the file `unreadable` with
   a code from a fixed table (`read.permission_denied`, `read.io_error`,
   `read.invalid_dicom`, `read.unsupported`); the exception's message is
   never stored. Which files these are, and which archives and folders,
   the app shows only on request, from the catalog (ADR 0024).
6. **A reader version on every row.** Each file row carries the
   `reader_version` that produced it. A worker with a newer `READER_VERSION`
   gets a scan that re-reads the older rows, in batches and resumably like
   any scan (ADR 0021).
7. **DICOMDIR is a completeness check.** A file with MediaStorageSOPClassUID
   1.2.840.10008.1.3.10 is kind `dicomdir`. Its DirectoryRecordSequence is
   walked directly: each IMAGE record's ReferencedSOPInstanceUIDInFile, with
   the SeriesInstanceUID of its parent SERIES record, becomes a row. Per
   series, SOP UIDs that the DICOMDIR lists and that were not found in the
   same source give `check.dicomdir_incomplete`. The DICOMDIR does not drive
   discovery: the walk already finds every file, and a DICOMDIR can be
   stale. Matching by SOP UID makes the case and Unicode normalization of
   ReferencedFileID irrelevant.
8. **Instances and duplicates.** Each `image` or `non_image` file is one
   instance, and so is each frame of a multi-frame file. The instance key
   (`sop_key`) is the SOP Instance UID, followed by `#<frame>` for a frame of
   a multi-frame file. Of several files with the same key one wins, in this
   order: pixel data `ok` over truncated or missing; a transfer syntax the
   converter can read over one it cannot (ADR 0023); the lowest source and
   relative path. The losers are counted in `check.duplicates`. The same SOP
   UID under another study or series UID gives `check.uid_conflict`, and the
   winner's grouping is used.
9. **Grouping and the split cascade.** Parts are formed per series UID
   within the study UID and split in this order, each step within the groups
   of the step before:

   | step | split by | rule |
   |---|---|---|
   | a | SOP class | image vs. non-image |
   | b | orientation | slice normals differ by more than 1° |
   | c | matrix | rows × columns |
   | d | pixel spacing | differs by more than 1% |
   | e | AcquisitionNumber, TemporalPositionIdentifier or EchoNumbers | only when positions repeat and that tag separates the repeats (several phases in one series) |
   | f | StackID | Enhanced objects |

   A multi-frame file is never split; all its frames stay with their file. A
   file whose frames differ in orientation or size gets
   `check.enhanced_mixed_frames` and is not selected automatically. Gantry
   tilt is not a split reason: a tilted series is one acquisition, and it
   gets a warning. Parts are numbered 0…n−1 by their lowest position along
   the slice normal, then by InstanceNumber, and each part of a split series
   gets `check.split` with its reason.
10. **Part identity across regroups.** Each new part is compared by instance
    key with the parts of the previous generation. Matching is greedy, by
    descending overlap: a new part takes an old `part_ref` when the overlap
    is more than half of the old part's instances and that `part_ref` is
    still free. Otherwise it gets a fresh `part_ref` from
    `catalog_meta.next_part_ref`, which is never reused. The merge maps by
    this identity first (ADR 0020), so a key, a manual choice and results
    stay with the same images when a split renumbers the parts.
11. **Geometry per part.** With r and c the row and column direction cosines
    of ImageOrientationPatient, the orientation is valid when both lengths
    are within 0.01 of 1 and |r·c| ≤ 0.01; otherwise the part has no
    geometry. The slice normal is n = (r × c) / ‖r × c‖, and each
    instance's position along it is d = p · n (Enhanced: per frame). On the
    sorted positions, neighbors closer than 0.001 mm share a position;
    `slice_count` is the number of distinct positions, `z_extent_mm` the
    distance from the first to the last, and `slice_spacing_mm` the median
    step s. A part is axial when |n_z| ≥ 0.95, coronal when |n_y| ≥ 0.95,
    sagittal when |n_x| ≥ 0.95, and oblique otherwise.
    - The middle instance, for the preview, is ordinal ⌊n/2⌋ of the sorted
      positions.
    - `slice_thickness_mm` is the median SliceThickness (Enhanced:
      PixelMeasuresSequence), falling back to the spacing;
      `pixel_spacing_mm` is the row spacing; `kernel` is the most frequent
      value.
    - The fingerprint is the hex sha256 of the sorted instance keys joined
      by `\n`. It is the part's identity and nothing more: the merge maps
      parts by it when the catalog was rebuilt (ADR 0020), and a file size
      in it would change it whenever another duplicate wins, so the part
      would lose its key. It is therefore not the content fingerprint of
      plan §6's cache rule (instance UIDs plus file sizes); M3's result
      cache defines that one in its own ADR and must not reuse
      `series.fingerprint` for it, because a series exported again with the
      same UIDs and other pixel data keeps this fingerprint.
    - These feed the checks, among them a gap (a step above 1.5 s), uneven
      spacing (a step that is no gap differs from s by more than
      max(0.01 s, 0.01 mm)), gantry tilt (a shear above 0.1°), a tilt tag
      without shear, an oblique stack (more than 1° from the nearest
      standard plane; still eligible up to 18.2°) and non-square pixels
      (more than 1%). These formulas pass 13 of 13 synthetic geometry
      cases. Every check code, with its level and parameters, is registered
      in `Protocol/index_codes.json`.
12. **Studies and PET/CT pairs.** A study takes the most frequent patient
    link of its files (ties to the smallest; more than one distinct link
    gives `check.study_patient_conflict`), the most frequent of F, M and O
    for sex, the most frequent age, and its date and description. Within one
    study, every PT image part is paired with every CT image part that has
    the same non-empty FrameOfReferenceUID and a z overlap greater than 0.
    Each pair stores the overlap and `pet_attenuation_corrected`: 1 when
    CorrectedImage contains `ATTN`, 0 when the tag is present without it,
    NULL when it is absent. Series number and description never pair.
13. **NIfTI.** The header is read with SimpleITK
    `ImageFileReader.ReadImageInformation()`. The file's sha256 is computed
    streaming and cached by size and modification time.
    - A file named `CT_<id>.nii[.gz]` or `PT_<id>.nii[.gz]` (NFC,
      case-sensitive prefix) gets modality CT or PT and its patient from
      `<id>` (ADR 0024).
    - Any other name gets modality `OT` and `check.nifti_unnamed`, and is
      never selected automatically, because a label mask lying next to a CT
      must not be chosen as a CT. Such a file is assigned to a patient by
      hand.
    - Each file is one study. Its UIDs are synthetic,
      `study_uid = "2.25." + int(sha256(b"bcoa.nifti.study\x1f" +
      content_sha256)[:15])` and the same with `series` for the series UID,
      and its fingerprint is the content sha256. A UID built from the folder
      name could be brute-forced back to that name, so the folder is not
      used.
    - NIfTI files are not paired for PET/CT, since they have no Frame of
      Reference; that is left to Phase 2.
14. **Batches.** Rows are upserted with `INSERT … ON CONFLICT(source_id,
    rel_path) DO UPDATE`, and a re-read file's frame and DICOMDIR rows are
    replaced in the same transaction.

## Consequences

- A rescan of an unchanged folder costs a walk and a stat per file: about
  0.3–0.5 s per 100 000 entries for the walk on Linux, with a budget of
  10 s per 100 000 files on a local disk. Stat on APFS is measured in the
  sandboxed probe on the macOS runner.
- The index job makes no network attempt at all, and
  `test_index_no_network.py` asserts that an audit hook records no socket and
  no program start during a full index job. `test_index_ast.py` holds that
  the `requests` block precedes every pydicom import.
- Archives in a source are reported, not indexed: `check.archives_skipped`
  asks the user to extract them, and "Show Files…" names them (ADR 0024).
- A NIfTI file keeps its study across renames and moves, because its UIDs
  come from its content.
- The tests are `test_index_walk.py`, `test_index_read.py`,
  `test_geometry.py`, `test_checks.py`, `test_index_group.py`,
  `test_dicomdir.py` and `test_nifti.py`, each on synthetic files.

## Rejected alternatives

- **`dcmread(force=True)`, or a sniff for raw datasets.** `force=True`
  returned a dataset for every kind of junk tried. A sniff for raw datasets
  can be written, but the plan names the preamble as the test.
- **DICOMDIR as the source of files, or read through `FileSet`.** A DICOMDIR
  can be stale. `FileSet` drops the records whose file is missing, which is
  exactly what is worth reporting, and is about eight times slower than
  walking the records.
- **Opening archives.** BTM unpacked ZIPs beside the archive: every image
  was counted twice, 3.4 GB of unpacked copies built up unseen, and a
  half-finished extraction read as a study. Source folders are only read
  (plan §13).
- **Reading headers in threads.** Measured slower, as above.
- **Keys carried by part number (designs A and C).** A changed split
  re-binds a key, with its selection, QC and results, to different images.
  Identity by overlap, from design B, keeps them with their images.
- **Splitting on gantry tilt.** A tilted series is one acquisition; it gets
  `check.gantry_tilt` instead.
- **First file seen wins (BTM).** The winner depends on the order of the
  walk; the rule above is deterministic and prefers a file the converter can
  read.
- **Tolerating the `requests` import.** The guard would refuse one socket in
  every index job and log it; with the block, a test can assert none.
