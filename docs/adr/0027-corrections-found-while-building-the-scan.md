# ADR 0027: Four corrections found while building the scan

- Status: accepted; corrects [ADR 0020](0020-index-catalog-owned-by-the-worker.md) decision 8, [ADR 0022](0022-reading-grouping-and-splitting.md) decision 4 and [ADR 0024](0024-patient-identity-and-privacy-in-the-import.md) decision 6
- Date: 2026-10-09
- Plan section: §7, §13

## Context

The scan side of the index job (walk, header read, DICOMDIR, NIfTI, the job
with its batches, cancel and resume) was built against ADRs 0020, 0022 and
0024 and tested on the synthetic corpus of ADR 0026. In four places the text
of those ADRs, followed as written, gives a wrong result. Each was found by a
test or a measurement, and each is corrected here rather than in the code
alone.

## Decision

1. **Identifiers are kept per file as well.** ADR 0024 decision 6 keeps the
   plain PatientID and accession number in the catalog only per study, in
   `pending_identifiers`. A study's ID is that of its most frequent link
   (ADR 0022 decision 12), which only the counts over all its files decide,
   and an unchanged file is never read again (ADR 0020 decision 8), so a
   regroup has nothing to count from; and a file read again with a corrected
   ID must replace what its old read left. `files` therefore gains
   `patient_id` (stripped and NFC, as linked; for a NIfTI file the `<id>` of
   its name) and `accession_number`. They are NULL, and the tags are not even
   requested, when the job carries no link key, and a placeholder never
   reaches them. The regroup derives `pending_identifiers` from them. Remove
   Identifiers deletes the whole catalog, as before.
2. **A file that could not be opened for want of permission is read again
   at every scan.** ADR 0020 decision 8 reads again only files that were
   changing or failed with an I/O error. Making a file readable with `chmod`
   changes the time of its inode, not the modification time the walk
   compares, so such a file would stay `unreadable` for good. Trying it again
   costs one failed open.

   *Corrected by [ADR 0029](0029-corrections-found-in-the-review-of-the-index-job.md)
   on 2026-10-09: it cost a regroup and a merge as well, because every
   batch set `regroup_due`; a read that fails again in the same way now
   writes nothing.*
3. **A Pixel Data element of length 0 is `missing`.** ADR 0022 decision 4
   calls every pixel element that is neither cut nor absent `ok`. An element
   that holds no bytes holds no image; the converter cannot make a slice of
   it, and calling it `ok` would hide the gap from `check.missing_pixel_data`.
4. **A Deflated file's pixel element is found by a second read.** ADR 0022
   decision 4 reads the element header at the file position where
   `stop_before_pixels` stopped. pydicom inflates a Deflated dataset into
   memory first, so that position is the end of the file (measured with
   pydicom 3.0.2) and says nothing. The reader reads such a file a second time
   with only the pixel tags and judges presence and length from that; a cut
   Deflated stream already fails the header read and is `unreadable`.

   *Corrected by [ADR 0029](0029-corrections-found-in-the-review-of-the-index-job.md)
   on 2026-10-09: the element is deferred and only its length is read,
   because its value loaded beside pydicom's inflated copy took 955 MB for
   a 300 KB file.*

## Consequences

- `docs/schema.md` lists the two columns, and the plan's §13 names them
  beside `pending_identifiers`. `Worker/tests/test_index_privacy_read.py`
  holds that without a key no identifier tag is requested and no identifier
  column is filled.
- Two additions that the ADRs leave open are recorded in the catalog's
  `catalog_meta`: `regroup_due`, set by every write to `files` or `bad_dirs`
  and cleared by the regroup, so that a scan cancelled after its last batch
  still regroups next time; and `files_link_key_id`, the key the rows were
  read under. Rows read under another key, or none, lose their identity
  columns at once and are read again, so that a catalog that survived a
  Remove Identifiers it should not have survived holds nothing linkable.
  [ADR 0029](0029-corrections-found-in-the-review-of-the-index-job.md) makes that true for what the
  last regroup derived from them as well.
- `test_index_run.py`, `test_index_read.py` and `test_index_crash.py` hold
  the four corrections.

## Rejected alternatives

- **Counting the IDs per study during the scan.** A scan reads only what
  changed, so the counts of a study whose other files were read by an
  earlier scan would be wrong.
- **Leaving permission-denied files to a forced re-read.** The user who fixed
  a permission has no reason to know that a forced re-read exists.
- **Parsing the inflated stream to find the pixel element.** It is what the
  second read does, through pydicom, without a second inflater.
