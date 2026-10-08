# ADR 0024: Patient identity and privacy in the import

- Status: accepted
- Date: 2026-10-08
- Plan section: §6, §7, §12, §13

## Context

Plan §7 groups studies into patients by PatientID and, for anonymized data
without one, takes an ID from the folder name once the user confirms it.
Plan §13 keeps plain identifiers (PatientID, accession number) only in the
table `identifiers`, says "Remove Identifiers" deletes them irreversibly,
and keeps paths out of logs and messages, because folder names often contain
patient names. Plan §6 stores no names and no birth dates and derives the
age at indexing.

What exists: v1 stores each source's folder name as `display_path`, and
its security-scoped bookmark, which encodes the folder's absolute path;
`removeIdentifiers` empties `identifiers` with `secure_delete` and an audit
row, and nothing calls it yet; the project key that ADR 0013 wants for
hashed UIDs has no column. Job files in `jobs/` keep the absolute source
paths. The worker's generic error handler writes the full traceback to the
job log, and pydicom's messages quote paths and values. The privacy manifest
declares file timestamps with reason C617.1 only, which covers files in the
app's own container.

A later study must still find its patient, so a link to the PatientID has to
outlive the scan. The three designs (ADR 0020) failed here in different
ways, as the judges found:

- Design B kept `match_key = HMAC(project_key, PatientID)` and the key in the
  same `project.sqlite` after Remove Identifiers. Anyone with the file and a
  list of candidate IDs recovers every PatientID by enumeration, 10^8 to
  10^10 HMACs; both judges called it fatal.
- Design C did not stop the worker from writing plain PatientIDs and
  accession numbers into the rebuilt catalog after the removal.
- Design A could never index the project again after Remove Identifiers,
  contradicted itself about new studies, and wrote folder-name candidates
  into `project.sqlite` before the user confirmed them.

## Decision

1. **Identity is a keyed link.** The app creates `link_key`, 32 random bytes,
   when a project is created or migrated to v2, and stores it base64-encoded
   in `project_meta` with `link_key_id`, the first 16 hex characters of
   HMAC-SHA256(link_key, "bcoa.link-key-id"). A PatientID is stripped of
   spaces and NUL padding and NFC-normalized; its link is
   `"pid:" + hex(HMAC-SHA256(link_key, b"pid\x1f" + utf8(id)))`.
   - Empty IDs, and IDs in the placeholder list compared case-folded
     (ANONYMOUS, ANONYMIZED, ANONYMISED, ANON, UNKNOWN, NONE, NULL, N/A, NA,
     0, -, adjustable in `identity_config`), count as missing.
   - IssuerOfPatientID is not part of the link; it only feeds
     `check.issuer_conflict`.
   - The worker recomputes `link_key_id` from the key in the job and refuses
     a mismatch with `link_key_mismatch`, which catches a stale job file. The
     merge uses links only when the catalog's `link_key_id` equals the
     project's: links made with a key that is no longer the project's are
     worth nothing.
2. **Patients.** A new study finds its patient through `patient_links`, and
   otherwise through a confirmed folder link. New patients are created one
   per link (or one per study without a link), with keys `pt_%06d` and
   pseudonyms `P%04d` from `key_counters`; neither is ever reused. Each
   patient has an `id_status`:

   | `id_status` | meaning | selected automatically |
   |---|---|---|
   | dicom | PatientID present | yes |
   | file | NIfTI `CT_<id>` | yes |
   | unconfirmed | no ID; folder candidates exist | never |
   | confirmed | the user confirmed a folder ID or typed an ID | yes |
   | unlinked | identifiers removed or withheld; one patient per study | yes |

3. **Folder IDs are confirmed, never assumed.** Only with a link key and
   with folder IDs switched on (`folder_ids`, up to `folder_max_level` 3),
   and only for studies whose PatientID is missing or a placeholder, the
   worker records candidates: level 0 is the source folder, level k (1–3)
   the k-th component of the deepest folder common to the study's files.
   Each has a display label in NFC and a link
   `"folder:" + hex(HMAC(link_key, "folder" ␟ source_id ␟ components 1…k))`.
   The labels live only in the catalog. "Confirm Patient IDs…" reads them in
   one short read and shows them, never writing them to `project.sqlite`.
   The default level is 1 if every anonymous study has a level-1 component
   and there are at least 2 distinct values, otherwise 0. On confirm
   (`IdentitySQL.confirmFolderLevel`, one transaction), each chosen link's
   studies move to the patient already holding that link, or else to the
   patient with the lowest `patient_key` (key order, because `P10000` sorts
   before `P9999` as text); emptied patients are deleted; the link is
   stored, and a target that was `unconfirmed` becomes `confirmed` (one
   found through its link keeps `dicom`, `file` or `confirmed`); automatic
   selection and ages are recomputed for the affected studies; and an audit
   row `ids_confirmed` holds the counts of patients and studies and the
   level. Later anonymous studies in the same folder join through the
   merge.
4. **Typed IDs and assignment.** The app computes `pid:` links for typed IDs
   with CryptoKit's HMAC, a system framework, against a vector file
   `Protocol/fixtures/link_vectors.json` that both test suites check. A typed
   ID that matches a link moves the study to that patient; one that matches
   nothing creates a `confirmed` patient. "Assign to Patient…" moves a study
   to an existing patient, which is also how a NIfTI file without a
   `CT_<id>` name gets one. Each of these edits writes an audit row,
   `patients_created` or `studies_assigned`, with counts only. All of this
   lasts across rescans, because the merge never changes the patient of a
   known study.

   The other side of that rule: when the PatientID in a known study's files
   changes (a correction or a merge of patients in the PACS, or files
   exported again with an ID where there was none), the study keeps its
   patient, `identifiers` keeps the old ID, and a study held under an
   unconfirmed patient stays held. So the merge raises
   `check.patient_link_changed` (study, warning) when the catalog's links
   are valid and hold a `pid:` link for a known study that its patient does
   not hold in `patient_links`, because it leads to another patient or to
   none. The user resolves it with "Assign to Patient…" or a typed ID; the
   merge does not release a held study by itself, because which patient a
   study belongs to is the user's decision once the files and the project
   disagree. A study the user assigned against its PatientID keeps the
   warning, which then says exactly that.
5. **Sex and age.** PatientSex is normalized to F, M or O, otherwise NULL. A
   patient takes the value only if every study that records one agrees;
   otherwise it stays NULL with `check.sex_conflict`. Each study stores its
   `age_years`, from PatientAge (nnnD, nnnW, nnnM or nnnY) or else as
   completed years from PatientBirthDate to StudyDate, with 29 February
   handled; the birth date is never stored. When both exist and differ by
   more than a year, the study gets `check.age_conflict`.
   `age_at_first_study` is the age at the first study with a selected
   series, otherwise at the first dated study, recomputed after every merge
   and every selection change. That is not always the export's first time
   point: the export counts a study that has a selected series or a series
   with results in the run, gone studies included, and this rule counts
   current studies with a selected series. OPEN_QUESTIONS #27 asks which
   age the export writes.
   `check.age_inconsistent` fires when the birth dates the studies imply
   (date − age × 365.2425 days) span more than 1.5 years.
6. **Where identifiers live.** In `project.sqlite` only in `identifiers`:
   the PatientID, the accession numbers as a JSON array, and `id_source`
   (dicom, file, folder or typed). In the catalog: `pending_identifiers`,
   the plain PatientID and accession number of each study, which the merge
   copies into `identifiers` and which stay until the catalog is deleted;
   the `pid:` links; the folder labels; and the relative paths. Folder
   labels and relative paths hold patient names wherever the folder names
   do. `project.sqlite` has no column for names or birth dates, holds links
   only as HMACs, and holds no path of a source file. Two of its columns
   still hold a source folder's name: `display_path`, and the bookmark,
   which encodes the folder's absolute path; a source folder named after a
   patient therefore puts that name into `project.sqlite` (OPEN_QUESTIONS
   #25). `jobs.log_path` holds the path of a job's log inside the project
   folder. The tree shows pseudonyms; a PatientID column, read from
   `identifiers`, is off by default and can be switched on (#25).
7. **What leaves the worker.** Events, results, logs and audit rows hold
   counts and codes only; a progress message never holds a path. The index
   job catches every exception itself, sends `index_failed`, and logs the
   traceback's frames only (file, line, function), never the message or
   locals, so the generic handler's full traceback is never reached.
8. **Job files.** They hold absolute roots and the link key, so they are
   written with mode 0600 and deleted when the job ends and at launch.
9. **Remove Identifiers.**
   1. It is refused while a scan, regroup or export runs: "Remove
      Identifiers is available when indexing and exporting have finished."
      Before it starts, it lists the exports below `exports/` that hold
      PatientIDs or the key table of plan §13, as their audit rows record
      (each export's audit row records its `identifiers` option), and
      offers to delete them in step 4. It deletes no export unasked, and
      copies outside the project folder are beyond its reach.
   2. One transaction with `PRAGMA secure_delete = ON` deletes
      `identifiers`, `patient_links`, and `link_key` and `link_key_id` from
      `project_meta`. In the same transaction, `unconfirmed` patients become
      `unlinked`, `identifiers_removed_at` is set, `display_path` becomes
      "Source {n}" (OPEN_QUESTIONS #25), and an audit row is written. The
      bookmarks stay: without them the app cannot open the source folders
      again for the scan in step 5, so each source's absolute path stays
      in `project.sqlite` until the source is removed (#25).
   3. In WAL mode, `PRAGMA wal_checkpoint(TRUNCATE)` follows (ADR 0021).
   4. `index/catalog.sqlite*`, `index/catalog.lock`, `index/previews/` and
      `jobs/*` are deleted, and the exports the user chose in step 1.
   5. A full scan is queued with `link_key: null`. The worker then requests
      neither PatientID (0010,0020), IssuerOfPatientID (0010,0021),
      OtherPatientIDsSequence nor AccessionNumber (0008,0050), and writes
      `pid_state = 'withheld'`, no `pid:` link, no pending identifiers and
      no folder candidates. The merge finds every known series again by
      fingerprint.
   6. New studies are refused from then on (`new_study_policy = 'refuse'`,
      OPEN_QUESTIONS #26) and counted in `check.new_studies_not_added`; known
      study UIDs still update.
10. **The privacy manifest** gets reason 3B52.1 under
    `NSPrivacyAccessedAPICategoryFileTimestamp`, beside C617.1, because the
    index reads the modification times of files in folders the user
    granted.
11. **Files the index could not use are shown on request.** The plan's old
    rule was to skip unreadable files and log them; the log now holds counts
    and codes only (decision 7), so a user would learn how many files were
    unreadable, which archives were not opened and which folders could not
    be listed, but not where they are. "Show Files…" beside these checks in
    the source's inspector reads the relative paths of the source's
    `unreadable`, `archive` and `symlink` files (with their read codes) and
    its `bad_dirs` from the catalog in one short read, as "Confirm Patient
    IDs…" reads folder labels (decision 3), and shows them in a sheet. They
    are never written to `project.sqlite`, a log, an event or the audit
    log, and they go with the catalog.

## Consequences

- After Remove Identifiers neither `project.sqlite`, the catalog, the
  previews nor the job files can link a study back to a PatientID: the key
  that made the links is gone, and so is every link. Known studies keep
  their patients and are still updated by rescans. Exports written with
  PatientIDs, and the key table, keep them unless the user deletes them in
  step 9.1, and so do copies made outside the project folder. Each source's
  bookmark keeps the folder's path, which names a patient only if the user
  chose a folder named after one as a source.
- Deleting a file on APFS is not a secure erase: the catalog, the previews
  and the job files are removed, not overwritten. FileVault, which plan §13
  recommends, is what protects whatever remains on the disk.
- `test_index_privacy.py` builds a canary corpus (PatientName
  `CANARY^NAME`, PatientID `CANARY-ID-4711`, accession `CANARYACC`, birth
  date 19010101, folder `CANARY_FOLDER` below the source root, because the
  root's own path stays in the bookmark by design). Logs, events, results,
  preview file names and `project.sqlite` outside `identifiers` hold no
  canary. Before the removal it writes an export with `identifiers =
  "both"`. After Remove Identifiers with that export chosen for deletion,
  no file in the project folder holds a canary, `-wal`, `-journal` and
  `jobs/` included; with it kept, only that export does.
- `test_schema_sql.py` keeps its rule (the only column containing "name" is
  `label_name`, none contains "birth") and applies it to the catalog's DDL
  too.
- Adding data after Remove Identifiers needs a new project, unless the owner
  chooses to add new studies as unlinked patients (#26), which is one
  parameter of the merge.

## Rejected alternatives

- **The key and the links kept after removal (design B).** Removal would be
  reversible by enumeration; plan §13 says irreversible.
- **No indexing after Remove Identifiers (design A).** Known studies would
  no longer be updated, a limitation the plan does not ask for.
- **New studies as unlinked patients by default.** Their files can no longer
  be told apart by patient, so every new study would become a patient of its
  own. It is offered as the alternative in #26.
- **Folder candidates in `project.sqlite` before confirmation (design A).**
  Folder names often contain patient names; only a confirmed link, never the
  label, reaches the project database.
- **The plain PatientID as the patient key.** It would sit in every row that
  ties a study to a patient. A keyed hash becomes worthless at once when the
  key is deleted.
