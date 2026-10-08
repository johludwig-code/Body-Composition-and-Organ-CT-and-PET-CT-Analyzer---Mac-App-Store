# ADR 0020: The index catalog is a worker-owned database that the app merges

- Status: accepted
- Date: 2026-10-08
- Plan section: §3, §5, §6, §7, §17

## Context

M2 indexes the source folders (plan §7): 100 000 files with progress and
cancel, incremental rescans, and duplicates across folders counted once.
Plan §6 puts the index into `project.sqlite`, which only the app writes; the
worker reads it, and only for the export (ADR 0013).

The work was measured on Linux (Xeon at 2.1 GHz, the locked pydicom 3.0.2,
synthetic files). Walking 100 000 directory entries takes 0.3–0.5 s.
Reading their headers takes 62 s with a warm cache and 80 s cold, and
100–111 s with GE-like private headers. Holding 100 000 records in memory
peaked at 556–571 MB. A scan is therefore minutes of work that a cancel, a
crash or a dropped network share can interrupt, and its records belong on
disk in batches.

Three designs were drafted on 8 October 2026, and two judges scored them:

- **A, worker-centric:** each index job writes a complete staging SQLite
  file and publishes it with `os.replace`; the app imports it into
  `project.sqlite`, the table of files included.
- **B, app-centric:** the app walks and groups in Swift; the worker only
  reads headers into staging.
- **C, risk-first:** the worker keeps a persistent catalog in the project
  folder, and the app merges finished generations of it.

Judge 1 picked C (39 points; A 37, B 24). Judge 2 picked A (40 points; C 38,
B 25). Judge 2's required fixes for A add up to C's catalog: a per-file cache
kept apart from `project.sqlite` and deletable on its own, no 100 000-row
churn in the live database, a refused or canceled scan that is not thrown
away, resume, a reader version, and protection against an empty walk. Judge
1's fatal flaws in C were all local, and each is fixed here or in ADRs 0021
to 0024. Judge 2 found no fatal flaw in C.

## Decision

1. **The catalog.** The index job records every file of every source in
   `<project>/index/catalog.sqlite`: one row per file, keyed by source and
   relative path (raw bytes), with size, modification time in nanoseconds,
   the reader version that produced the row, a kind, and the header values
   the import needs. That row is the resume state, the header cache and the
   input of grouping at once. Headers are written in batches of at most
   1 000 files or 2 s, one transaction each.
2. **Generations.** A regroup runs in one transaction. It writes the derived
   `cat_*` tables (studies, series parts, checks, PET/CT pairs, folder ID
   candidates, the members of each part), raises `catalog_meta.generation`
   by one and sets `complete = '1'`. A generation is published whole or not
   at all.
3. **One writer each.** Only the index job writes the catalog, in modes
   `scan` and `regroup`; a job in mode `previews` only reads it. Only the app
   writes `project.sqlite`. ADR 0021 keeps the two apart in time.
4. **The merge.** The app attaches the catalog as `idx` and merges one
   complete generation into `project.sqlite` in a single `BEGIN IMMEDIATE`
   transaction, followed by the recomputation of ages in the same
   transaction. The SQL is three Swift string constants,
   `IndexSQL.mergeInputs`, `IndexSQL.merge` and `IndexSQL.patientAges`. The
   Linux pytest suite extracts them by regex, as it already does for
   migration v1, and executes them against catalogs made with the worker's
   own DDL and filled with synthetic rows; synthetic DICOM files reach the
   merge only through the corpus test (ADR 0026). A guard at the start
   refuses a catalog that is incomplete, of another format, or already
   merged (the same `catalog_id` and a generation not ahead of
   `project_meta.merged_generation`); nothing changes then.
5. **Mapping.** A series part is found again by the catalog's part identity
   (`series.catalog_part = <catalog_id>:<part_ref>`), and otherwise by series
   UID plus fingerprint. A current part that maps to nothing becomes `gone`;
   it is deleted unless results, QC, jobs or a cohort refer to it. Keys come
   from `key_counters` and are never reused.
6. **No paths of source files in the project database.** `project.sqlite`
   holds no path of a source file; relative paths live only in the catalog.
   Two v1 columns are the exceptions. Each source's security-scoped bookmark
   (`sources.bookmark`) encodes the absolute path of the source folder,
   folder names included, in readable form, and it stays for as long as the
   source exists, also after Remove Identifiers, because without it the app
   cannot open the folder again for a rescan. `jobs.log_path` is the
   absolute path of a job's log inside the project folder. A source also
   keeps its folder name for display (`display_path`; ADR 0024,
   OPEN_QUESTIONS #25).
7. **A cache.** The catalog has its own format number (1, `PRAGMA
   user_version = 1`) and is never migrated: a catalog of another format, or
   a damaged one, is deleted and rebuilt. Deleting `index/` costs one full
   read and nothing else, because the merge finds every part again by its
   fingerprint.
8. **Rescans.** A file is read again only when its size, modification time
   or reader version differs, or when the last read found it changing or
   failed with an I/O error. A walk that finds no new, changed or stale row
   and no change in unreadable folders writes no generation, needs no merge,
   and its result is `{"changed": false}`.
9. **Cancel and crash.** SIGTERM rolls back the batch being written; a short
   transaction then marks the source's scan `interrupted` with its counts,
   and the job ends `cancelled`. Committed batches stay, so the next scan
   reads only the rest. A cancel during a regroup rolls the regroup back,
   and the previous generation stays complete and mergeable. A SIGKILL or a
   power cut leaves an open transaction that the rollback journal undoes at
   the next open, so a crash behaves like a cancel. If the app dies during a
   merge, nothing was committed, and at the next launch the catalog's
   generation is still ahead and the merge runs again.
10. **The job.** `index` is already a job kind and a stage of protocol
    version 1. The payload names the catalog and the previews folder
    relative to the project and lists the sources with their absolute roots;
    the worker takes paths from nowhere else and refuses a source the
    payload does not list. The result holds counts only. The index job adds
    an optional `detail` object to `progress` (`phase`, `done`, `total`), from
    which the app renders its own localized text, and an artifact kind
    `preview` (ADR 0025). Both are additive, and the protocol stays at
    version 1: app and worker ship in one bundle, and no version of either
    has been released that could meet the other.
11. **The worker's code.** A package `bcoa_worker/index/` holds the walk, the
    reader, DICOMDIR and NIfTI handling, grouping, geometry, selection,
    kernels, checks, identity, the catalog and previews. `worker._handlers()`
    imports the handler of each kind only when a job of that kind runs, so an
    index job never imports torch, MOOSE or the export.

## Consequences

- The merge is fast enough to run after every scan: 20 000 series in 5 000
  studies merge in 1.04 s the first time and in 0.40–0.49 s again. The index
  `studies_by_patient` is what makes it so; without it the first merge took
  5.31 s.
- The merge and selection SQL pass eleven scenarios on Linux: a first merge;
  a stale generation refused with nothing changed; a user's choice kept
  while a second study joins its patient; a split and renumbering that keep
  the key; gone series kept with results and deleted without; returning
  files that keep their key by fingerprint; the bulk action; the export
  reading the merged project; Remove Identifiers followed by a rebuilt
  catalog; the v1 fixture migrated with identical export data; and a cohort
  saved and applied. A scale gate of 20 000 series in at most 3 s keeps the
  merge fast.
- CI holds the index to budgets (plan §16): on Linux, 10 000 files scan in
  under 60 s, an unchanged rescan takes under 2 s and the merge under 1 s;
  a slow job holds 100 000 files to at most 150 s for a full scan and 10 s
  for an unchanged rescan. ADR 0026 records these with the rest of M2's
  budgets and what each one rests on.
- A test kills the worker at random batch boundaries and inside a regroup,
  and checks that a rerun's derived tables equal those of an uninterrupted
  run.
- `project.sqlite` no longer grows with the number of files, and the
  index's churn stays in a file that may be deleted. The project folder
  gains `index/` with `catalog.sqlite`, `catalog.lock` and `previews/`; plan
  §3 gains a second store.
- The merge SQL uses `UPDATE … FROM` (SQLite 3.33) and the built-in JSON
  functions (3.38). The deployment target is macOS 14, and a Swift test
  asserts `sqlite_version() >= '3.38.0'`.
- The worker reads the source folders through the grant the app made in the
  Open panel. That is step 4 of spike S2, still open on the owner's Mac
  (ADR 0019). If the worker cannot read a granted folder, a future ADR
  records the contingency: the app copies the bytes the header read needs
  into a spool in the project folder (`work/spool/`) in bounded chunks, and
  the worker parses the spool. The catalog, grouping, merge and everything
  after stay as decided here. Those bytes include PatientName and
  PatientBirthDate, which plan §6 says are never stored, so that ADR must
  keep them off the disk: the app blanks the identifying elements before it
  writes a chunk, deletes each chunk as soon as it is parsed, or passes the
  bytes through a pipe instead of a file.

## Rejected alternatives

- **A staging file per job, imported with its table of files (design A).**
  Each import deleted and re-inserted all 100 000 file rows in the live
  database, 40–60 MB through its journal, and the worker loaded every cached
  record into memory (about 560 MB). The generation check threw a complete
  scan away whenever something else changed the index while it ran, such as
  adding a second source, and a refused staging file was not reused as a
  cache. Records carried no reader version, so a tag the reader learns later
  would stay missing from every old file. What A did well is kept: one
  writer per database, the guarded merge in Swift-held SQL that the Python
  suite runs, and the points listed in ADRs 0021 to 0025.
- **Grouping and selection in Swift (design B).** The logic most likely to
  be wrong (grouping, splitting, duplicates, identity, selection, checks)
  could then be tested only on the macOS CI runner, because the container
  where most of the work is done has no Swift compiler. B also imported a
  partial scan after a cancel, merged in chunks per patient that showed
  half-merged states, and compared modification times that Python floored
  to microseconds with microseconds that Swift rounded from a `Date`, so
  that a rounding difference would re-read every file on every rescan. Kept
  from B: part identity by instance overlap, the reader version on every
  row, and the points in ADRs 0021 to 0024.
- **The worker writes `project.sqlite` itself.** Two writers on one file
  collide, and in rollback-journal mode without a busy timeout a write fails
  at once while another connection holds the file (ADR 0021). ADR 0013 lets
  the worker read the project, not write it, and the app's observations of
  the database would show a half-built index between the worker's commits.
- **A header cache that is dropped whole when its schema changes**, as
  BOCARTA-MOOSE's `ScanCache` is. A new reader would cost a full re-read,
  80 s per 100 000 files cold, where a version per row lets a scan re-read
  the old rows in resumable batches.
