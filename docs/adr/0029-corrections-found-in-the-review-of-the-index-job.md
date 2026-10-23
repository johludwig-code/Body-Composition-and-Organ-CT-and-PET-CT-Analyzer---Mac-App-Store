# ADR 0029: Corrections found in the review of the index job

- Status: accepted; corrects [ADR 0020](0020-index-catalog-owned-by-the-worker.md) decision 8, [ADR 0021](0021-journal-modes-locking-and-scheduling.md) decision 5, [ADR 0022](0022-reading-grouping-and-splitting.md) decisions 1, 4, 7, 8 and 9 and [ADR 0027](0027-corrections-found-while-building-the-scan.md) decisions 2 and 4; completes [ADR 0023](0023-auto-selection-reasons-and-selection-state.md) decision 9 and [ADR 0024](0024-patient-identity-and-privacy-in-the-import.md) decisions 3 and 8
- Date: 2026-10-09
- Plan section: §7, §13

## Context

The index job (walk, read, regroup, and the merge SQL it feeds) was
reviewed as a whole against ADRs 0020 to 0028 before M2 is accepted. Each
finding was reproduced first, with a synthetic file or tree, and each one
held. Some are places where an ADR, followed as written, gives a wrong
result; some are places where the code did not do what an ADR says; some
are points no ADR decided. They are recorded here together, so that the
earlier ADRs keep their text and point to this one.

## Decision

### Reading

1. **An encapsulated pixel element is judged by its items.** ADR 0022
   decision 4 calls such an element `truncated` when the file's last 8
   bytes are not the Sequence Delimitation Item. Data Set Trailing Padding
   (FFFC,FFFC) and the Digital Signatures Sequence (FFFA,FFFA) may legally
   follow the pixel data, so a complete file was `truncated` and its whole
   series excluded. The reader now follows the item headers from the
   element's value (FFFE,E000 and a length each) to the delimiter
   (FFFE,E0DD), reading 8 bytes per item and never a pixel. The file is
   `truncated` when an item runs past the end of the file or anything but
   an item stands where the next one belongs; an offset table with no
   fragment after it is `missing`. `READER_VERSION` becomes 2, so every row
   is read again once.
2. **A frame count the pixel element cannot hold makes the file invalid.**
   NumberOfFrames is an IS of up to 2³¹ − 1, and every frame becomes an
   instance; one 1.5 KB file that claimed 4 000 000 frames took 1.9 GB in
   the regroup, at every regroup. A native element holds at most
   length × 8 ÷ (Rows × Columns × BitsAllocated) frames (SamplesPerPixel is
   left out, because YBR_FULL_422 stores three samples in the space of
   two); an encapsulated one at most one frame per fragment; and no file
   more than 100 000 (`FRAME_CEILING`). A file over its bound, or whose
   functional groups list more frames than that, is `unreadable` with
   `read.invalid_dicom`. The regroup caps a stored frame count at the same
   ceiling.
3. **A Deflated file's pixel element is judged by its length alone.** ADR
   0027 decision 4 reads such a file a second time for its pixel element.
   That read now defers the element and looks only at its length: loading
   its value on top of pydicom's own inflated copy took 955 MB for a 300 KB
   file.
4. **A DICOMDIR is read without pydicom.** pydicom cannot filter the
   elements of nested items, so requesting DirectoryRecordSequence parsed
   every PATIENT record's PatientID and PatientName, also in a job without
   a link key, where ADR 0024 decision 9.5 says they are not even read.
   `dicomdir.py` walks the element headers itself and reads only the five
   values the entries need; every other value is skipped by its length. A
   DICOMDIR whose structure does not hold together is `read.invalid_dicom`.
5. **A file is opened only as what the walk saw.** The reader opens with
   `O_NOFOLLOW | O_NONBLOCK` and requires a regular file on the descriptor.
   A file replaced since the walk by a symbolic link (which could lead out
   of the folder the user granted), a FIFO (which blocked the scan until it
   was cancelled) or a folder is `changing`, so the next walk sees what is
   there now.
6. **A NIfTI file is read by the NIfTI reader alone.** Left to ITK's
   factory, every registered reader tried the file, and HDF5's printed its
   error stack, with the file's path, to the job log. `SetImageIO
   ("NiftiImageIO")` is set before the header is read; the file already
   passed the reader's own header test, so the factory had nothing else to
   find.

### The walk and the scan

7. **The project folder is never walked.** A project saved beside its data
   with the parent chosen as the source indexed its own catalog, which
   changed with every commit, so no rescan was ever unchanged. The walk
   skips the folder whose device and inode are the project folder's and
   counts it as skipped.
8. **An entry the walk cannot describe is kept and counted.** Rows at and
   below such an entry are kept, matched by prefix as below a folder that
   could not be listed, because the entry may be a folder. An entry without
   a row gets an `unreadable` row with size and time 0 and
   `read.permission_denied` or `read.io_error`, so that it is counted and
   can be shown, and the first walk that can describe the entry reads it;
   before, it was reported nowhere and the source said `complete`. The scan's log line
   counts these entries.
9. **A source that drops during the read deletes nothing.** File by file,
   a share that unmounts looks like files that vanished or failed with an
   I/O error: 90 of 120 rows were deleted or blanked that way, and the
   source still ended `complete`. Before a batch that holds such a file
   (one with a row) is written, the root is checked; if it can no longer be
   listed or is empty, the batch is dropped and the source ends
   `unreachable`, as ADR 0022 decision 1 says. A file with a readable row
   that fails with an I/O error keeps its values and is read again at the
   next scan; a vanished file's row is deleted only when its folder can be
   asked and says so. The walk checks the root once more at its end, for a
   share that drops while it lists.
10. **A file that stays unreadable writes nothing.** ADR 0027 decision 2
    tries such files again at every scan, and every batch set
    `regroup_due`, so one file that stayed unreadable made every rescan a
    new generation and a merge, against ADR 0020 decision 8. A read that
    finds the same kind, code, size and time as the row, for a kind whose
    row holds no values, is not written, and only a batch that changes a
    row sets `regroup_due`.
11. **A busy catalog makes the job recoverable, and a cancel is answered
    while it waits.** ADR 0021 decision 5 turns only a held `flock` into
    `catalog_busy`. A reader of the app (a label, Show Files…, a previews
    job) that held the catalog longer than the busy timeout made the next
    COMMIT fail, and the job ended `index_failed`, not recoverable; and while
    SQLite waits, Python's signal handler cannot run, so a cancel waited up
    to 10 s. The job's connection now waits 200 ms per statement, with
    `cache_spill` off so that only BEGIN IMMEDIATE and COMMIT need a lock
    another connection can hold, and retries those two for up to 10 s,
    checking for a cancel between tries. SQLITE_BUSY or SQLITE_LOCKED from
    any statement ends the job with `catalog_busy`, recoverable: every batch
    rolled back whole.

### Identity

12. **A new key strips every identifier at once.** ADR 0027 says that rows
    read under another key, or none, lose their identity columns at once.
    The identifiers the last regroup derived from them stayed until the
    next regroup: `pending_identifiers`, the folder labels and links of
    `cat_id_candidates`, and `cat_studies.pid_link`, so a job cancelled
    before its regroup left CANARY-ID-4711 in the catalog's bytes. They now
    go in the same transaction, with `secure_delete` on, and the generation
    they belonged to is marked incomplete until the next regroup.
13. **A change of the identity settings takes effect.** ADR 0024 does not
    say what a change of `placeholder_ids`, `folder_ids` or
    `folder_max_level` triggers, and an unchanged file is never read again,
    so the old candidates and PatientID states stayed. Two hashes go into
    `catalog_meta`:
    - `identity_config_sha256`, written by the regroup like the selection's
      hash (ADR 0023 decision 8). When it differs from the job's, the job
      regroups although no file changed, which recomputes the folder
      candidates.
    - `files_placeholders_sha256`, the placeholder IDs the rows were read
      with. When it differs, the next job with a key demotes the rows whose
      stored PatientID is now a placeholder, and placeholder rows, whose ID
      was never stored, are read again at the next scan of their source.
    Case and order of the placeholder list are no change. The app queues a
    scan of every source when the placeholder IDs change, as it queues a
    regroup for the selection settings.
14. **The catalog is the owner's alone.** It holds PatientIDs, accession
    numbers and folder labels, yet it was created with the umask (0644, in
    a folder of 0755), where the job files of ADR 0024 decision 8, which
    hold less, are 0600. `index/` is now 0700 and `catalog.sqlite` 0600,
    made so at every open; the rollback journal takes the database's mode.
    Volumes that keep no modes are left as they are, as for the job files.
15. **A held series says so.** ADR 0023's eligibility table has the merge
    apply `select.held.unconfirmed_patient`, and decision 9 lists the
    outcome `held`; the merge copied the worker's reason unchanged, so a
    series that was not selected read `chosen`, and corpus rule C6 had been
    written to match. The merge now writes
    `{"v": 1, "outcome": "held", "codes": ["select.held.unconfirmed_patient"],
    "params": {}, "if_confirmed": <the worker's reason>}` for the automatic
    choice of a patient whose ID is unconfirmed, and
    `IdentitySQL.confirmFolderLevel` and `assignStudy` put the worker's
    reason back when they release the study (and `assignStudy` holds a
    study it moves to an unconfirmed patient).

### Grouping

16. **The DICOMDIR check is matched in Python and reaches a lost series.**
    Matched in SQL, each listed SOP UID scanned all files of its source
    through the only index there is: the regroup of 20 000 files took 45 s
    with a DICOMDIR that listed them all, against 0.7 s without one, in one
    statement no cancel could interrupt. The regroup now matches the listed
    UIDs against a set of SOP UIDs it reads once. A series
    the DICOMDIR lists and no part holds at all, which is what a copy that
    lost a whole series looks like, had no part to carry
    `check.dicomdir_incomplete` and went unreported; the source gets
    `check.dicomdir_series_missing` (`series`, `missing`), a warning,
    beside ADR 0022 decision 7's check per part.
17. **The losing side of a UID conflict is told.** ADR 0022 decision 8
    counts a UID conflict on the winner's part only, and which file wins
    depends on the path, so the series that lost a slice to a stray file
    can be the genuine one and was told nothing. The part of the loser's
    own study and series, when it exists, gets `check.uid_conflict_lost`
    with the number of its files that lost.
18. **A missing orientation or pixel spacing splits with its own reason.**
    Steps b and d of the cascade (ADR 0022 decision 9) also set files
    without a valid orientation, or without PixelSpacing, apart from the
    rest, because they cannot be measured with them; `check.split` then said
    "changing orientation" where nothing had changed. Such a part now has
    the split reason `missing_orientation` or `missing_pixel_spacing`, and
    the rest keep the step's own reason only where their values really
    differ.

## Consequences

- ADRs 0020, 0021, 0022, 0023, 0024, 0026 and 0027 point here where their
  text is corrected or completed. `docs/schema.md` lists the new
  `catalog_meta` keys, the rows of undescribed entries, the modes, the held
  reason and the two new checks; `Protocol/index_codes.json` registers
  `check.dicomdir_series_missing`, `check.uid_conflict_lost` and the two
  split reasons. The plan's §7 and §13 say the same in German.
- `corpus_expected.json` holds rules C6, C7 and C12 to this ADR and to ADR
  0028 decision 3: a held part reads `held`, the Enhanced mixed-frames
  part's expectation is exact, and a JPEG-LS file with trailing padding
  stays `ok`.
- Tests hold each decision: `test_index_read.py` (items, padding, frames,
  Deflated, open), `test_index_walk.py` and `test_index_run.py` (project
  folder, undescribed entries, a root that goes during the read, an
  unreadable file and an unchanged rescan, a busy reader and a cancel while
  it holds the catalog, a new key cancelled before its regroup, placeholder
  and folder settings, modes), `test_index_group.py` (lost series, losing
  side, missing values, frame cap), `test_index_budget.py` (a regroup whose
  tree has a DICOMDIR listing every file), `test_merge_sql.py` (held and
  released reasons) and `test_index_privacy_read.py` (the keyless DICOMDIR,
  the NIfTI log). `test_index_privacy.py` holds the merge-side moment of
  ADR 0024: after a keyed merge no column outside `identifiers` holds a
  canary, and after a keyless merge with `keep_identifiers = 0` no file of
  the project holds one at all. The moment right after Remove Identifiers
  needs the Swift side and is still to come.
- Points that belong to the app and are not built yet: refusing a source
  that lies inside the project folder or holds it (the walk only skips the
  folder), queueing a scan of every source when the placeholder IDs change,
  and releasing held reasons in the full Remove Identifiers when it turns
  unconfirmed patients into unlinked ones.

## Rejected alternatives

- **An index on `files(sop_uid)` for the DICOMDIR check.** It would cost
  every write of the scan for one query of the regroup, and the regroup
  already reads the SOP UIDs for its duplicates.
- **Skipping DICOMDIRs in keyless jobs.** It keeps identifiers out, but at
  the cost of the completeness check exactly where a CD copy is common.
- **Keeping files without a value in their group (ADR 0022 literally).**
  A part would then mix files that can be placed along its normal, or
  measured in plane, with files that cannot, and its geometry would have to
  either leave those files out of its counts or give up for one of them.
- **Keeping the 10 s busy timeout and only mapping its failure to
  `catalog_busy`.** The job would be recoverable, but SQLite does not return
  to Python while it waits, so a cancel would still wait up to 10 s, five
  times the budget of ADR 0026.
- **Re-reading every file when the placeholder IDs change.** Rows whose ID
  is stored can be demoted in place; only rows whose ID was never stored
  need their file.
