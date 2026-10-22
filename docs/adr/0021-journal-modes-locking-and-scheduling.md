# ADR 0021: Journal modes, locking and scheduling

- Status: accepted; corrects the last sentence of [ADR 0013](0013-export-reads-the-project-database.md)
- Date: 2026-10-08
- Plan section: §3, §5, §6

## Context

ADR 0020 gives a project two SQLite files with one writer each: the app
writes `project.sqlite`, the index job writes `index/catalog.sqlite`, and the
app merges the catalog by attaching it. Three readers come with them: the
export job reads the project (ADR 0013), a previews job reads the catalog,
and the app reads folder labels from the catalog.

`ProjectStore.open` keeps SQLite's default rollback journal (DELETE) and
GRDB's default busy mode, which fails a blocked write at once. In that mode
a reader's shared lock blocks every commit until the read ends. The export's
`load()` holds one read transaction across its whole read; measured on Linux
with 1 000 series × 1 461 labels (1.46 M result rows, 117 MB), that is
12.5–14.7 s. Every write of the app in that time would wait or fail: a click
on a selection, a merge, a job status. ADR 0013 ends with "The export can run
while the app keeps writing, because SQLite readers see a consistent
snapshot." That is true only in WAL mode.

WAL needs memory shared between connections (the `-shm` file), which SMB,
AFP and NFS do not provide reliably. The project folder is chosen by the user
(plan §6), may sit on a network volume, and can move to one. `flock` on SMB
is advisory.

Measured with both files in DELETE mode: a writer with a busy timeout waited
0.33 s behind a reader that held a 0.3 s read, then committed. While the
merge transaction holds the attached catalog, another connection that writes
the catalog gets "database is locked". A scan that commits during a merge
would fail, so locks cannot be what keeps them apart.

GRDB 7.0.0 opens its connections without `SQLITE_OPEN_URI` (its
`Configuration.SQLiteOpenFlags`). Whether `ATTACH 'file:…?mode=ro'` then
works depends on the system SQLite having been built with `SQLITE_USE_URI`,
which is unknown.

## Decision

1. **`project.sqlite`: WAL on a local volume, DELETE otherwise.** The mode is
   decided at every open from the project folder's
   `URLResourceValues.volumeIsLocal`, because a project folder can move.
   GRDB sets WAL with `config.journalMode = .wal`, which exists in its v7.0.0
   and v6.29.3 tags (Package.swift requires `from: "7.0.0"`); DELETE is set
   with `PRAGMA journal_mode = DELETE` in `prepareDatabase`, which also turns
   a file left in WAL mode back. The busy timeout is 10 s on a local volume
   and 30 s on a network volume.
2. **`catalog.sqlite`: always DELETE.** It has one writer at a time, may live
   on a network volume with the project, and is read only in short
   transactions.
3. **One writer per database** (ADR 0020). The worker never writes
   `project.sqlite` during an M2 job, and the app never writes the catalog.
4. **Scheduling is the guarantee.** The job queue gets an exclusive group
   `catalog`: index jobs in modes `scan` and `regroup` run one at a time per
   project. A `previews` job is a light job outside the group; it reads the
   catalog in one short transaction and closes it before decoding (ADR
   0025). The merge runs in a new `finalize` hook of the job that wrote the
   catalog, after its worker has exited and before the queue starts the next
   job of the group; at launch it runs before anything is scheduled. No
   worker writes the catalog while it is merged. A merge that fails is tried
   again (decision 11). A scan whose result says `changed: false` wrote no
   generation, so nothing brings its sources' state into the project but
   the result itself: its finalize hook applies each source's state,
   `indexed_at` and summary from the result's `sources` in one short write
   (ADR 0020, decision 8), and merges only a generation that is still
   pending (decision 11).
5. **`flock` is a second guard.** The worker takes `fcntl.flock` on
   `index/catalog.lock`; if the lock is taken, the job ends with the
   recoverable error `catalog_busy`. On SMB this is best effort.

   *Corrected by [ADR 0029](0029-corrections-found-in-the-review-of-the-index-job.md)
   on 2026-10-09: SQLITE_BUSY or SQLITE_LOCKED from any statement of the
   job is `catalog_busy` as well. The job's connection waits 200 ms per
   statement, with `cache_spill` off, and retries BEGIN IMMEDIATE and COMMIT
   for up to 10 s, checking for a cancel between tries, so that a cancel is
   answered while another connection holds the catalog.*
6. **Attaching the catalog.** `IndexStore` reads `PRAGMA compile_options`
   once. With `USE_URI` it attaches `file:<path>?mode=ro`; measured in
   Python, a write through it is refused with "attempt to write a readonly
   database", and the merge commits. Without it, it attaches the plain path.
   Either way the guarantee is a test:
   `test_the_merge_never_writes_the_catalog` in
   `Worker/tests/test_merge_sql.py` runs the whole merge with the catalog
   attached read-write under an SQLite authorizer that denies every write
   to the schema `idx`. The merge attempts none, and a deliberate write is
   denied.
7. **A hot journal.** If the worker crashed and left a hot journal, a
   read-only attach fails with `SQLITE_READONLY_ROLLBACK`. The launch merge
   then skips; the scan queued for the interrupted source opens the catalog,
   rolls the journal back, and merges in its finalize hook.
8. **At launch, in this order:**
   1. delete leftover job files in `jobs/`;
   2. mark sources in state `indexing` as `interrupted`;
   3. merge if the catalog is complete and either its generation is ahead of
      `merged_generation` or its `catalog_id` differs;
   4. queue a scan for interrupted sources, and for unreachable sources that
      are reachable again;
   5. queue a scan with re-read if the worker's `READER_VERSION` is newer
      than the catalog's.
9. **Sources know their volume.** `sources.volume_kind` (local, network,
   removable) is set from `volumeIsLocal` and `volumeIsRemovable` or
   `volumeIsEjectable`. A network source shows "Indexing a network folder
   takes longer than a local one."
10. **ADR 0013 is corrected.** The export runs while the app writes only in
    WAL mode. On a network project an edit during an export waits under the
    30 s timeout, and the app says "Waiting for the export to finish
    reading…"; an edit that waits longer is rolled back and reported as not
    saved. The export's read-only connection needs a writable `-shm` in
    WAL mode; the project folder is granted read-write, so it has one.
11. **A merge that fails is tried again.** On a network project the merge
    waits for an export's read like any other write, and the 12.5–14.7 s
    measured above were on a local Linux disk; over SMB the read can outlast
    the 30 s timeout, and the merge then rolls back with nothing changed.
    Because a rescan without changes writes no generation (ADR 0020), the
    next index job would not bring the pending one in by itself. So the
    `finalize` hook of every index job, and of every export while no job of
    the group `catalog` runs, merges whenever the catalog is complete and
    its generation is ahead of `merged_generation` or its `catalog_id`
    differs, whichever job wrote it; the guard of ADR 0020 makes a repeated
    attempt harmless. While a generation waits, the source list shows "Index
    not yet applied: waiting for the export to finish." The launch merge
    (decision 8) is the last retry.

## Consequences

- On a local volume a selection, a merge or a job status no longer waits for
  an export. `test_export_wal.py` holds that the export loader reads a WAL
  project while a writer commits, and that the writer is not blocked.
- A project in WAL mode has `-wal` and `-shm` files beside `project.sqlite`.
  Remove Identifiers therefore checkpoints with
  `PRAGMA wal_checkpoint(TRUNCATE)`, so that the deleted rows do not survive
  in the `-wal` file (ADR 0024).
- Two index jobs of one project no longer run side by side; until now the
  queue let any two light jobs run at once.
- `JobQueue` gains the `finalize` hook and the exclusive group, and deletes
  job files when a job finishes and at launch. `JobQueueFinalizeTests` holds
  that the hook runs before the next job of the group, and that a merge
  refused while an export reads runs again when the export ends;
  `JournalModeTests`
  holds WAL on a local folder, DELETE when the volume counts as a network
  one, and the timeouts; `AttachTests` holds that `compile_options` decides
  the URI form and that no stray file named `file:…` appears.

## Rejected alternatives

- **WAL for both files everywhere, guarded by `flock` (design C as
  drafted).** Judge 1 counted it a fatal flaw: SMB and NFS make neither WAL's
  shared memory nor `flock` reliable, and WAL mode persists in the file, so
  a project made on a local disk and opened later from a share would still
  use it.
- **DELETE everywhere, with a busy timeout.** Judge 2 suggested it unless a
  measurement showed exports blocking the app's writes. The measurement
  does: 12.5–14.7 s per export read at 1.46 M rows, longer than a click
  should wait and longer than the 10 s local timeout.
- **Locks as the only guard.** A scan that commits while the merge holds the
  attached catalog fails with "database is locked", and `flock` on SMB is
  advisory. The queue knows which job wrote the catalog and when it ended,
  so it orders them instead.
- **No previews beside a scan** (judge 1's fix for DELETE mode). A previews
  job holds its read for one short transaction, so a scan's commit waits
  only for that moment under its busy timeout.
- **Relying on `mode=ro` alone.** GRDB 7 opens without URI support, so the
  read-only form may not be available; the authorizer test holds the merge
  to reading either way.
