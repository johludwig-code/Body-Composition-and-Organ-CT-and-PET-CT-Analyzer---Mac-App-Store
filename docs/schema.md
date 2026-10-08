# Project database schema

`<Study>.bcoaproj/project.sqlite`, created and migrated by `BCOAStore`
(`Packages/BCOAStore/Sources/BCOAStore/StoreMigrations.swift`, migrations `v1`
and `v2`). That file is the definition; this page explains it.
`Worker/tests/test_schema_sql.py` executes the same SQL with Python's sqlite3,
so the schema is checked on every platform. Where this page describes what
ADRs 0020 to 0024 decide and the code does not do yet, it says *not built
yet*.

Migration `v2` (M2, the import) is additive: it drops and renames nothing,
and the export loader returns identical data for the v1 test project before
and after it. The import's SQL that runs on the migrated file (`IndexSQL`,
`SelectionSQL`, `IdentitySQL` in `IndexSQL.swift`) needs SQLite 3.33 for
`UPDATE … FROM` and 3.38 for the built-in JSON functions; a Swift test that
asserts `sqlite_version() >= '3.38.0'` is decided in ADR 0020, *not built
yet*.

| table | one row per | notes |
|---|---|---|
| `sources` | source folder | read-only security-scoped bookmark, which encodes the folder's absolute path in readable form and stays as long as the source does, also after identifiers are removed, because a rescan needs it; `display_path` is the folder name only, and becomes "Source n" when identifiers are removed (OPEN_QUESTIONS #25) |
| `patients` | patient | `patient_key` is internal; `pseudonym` P0001…; sex derived at indexing; `age_at_first_study` follows the selection (see "Age") |
| `identifiers` | patient | the only plain-text identifiers (PatientID, accession numbers as a JSON array); "Remove Identifiers" deletes them with `secure_delete` and writes an audit entry |
| `studies` | study | `study_uid` stays inside the project; exports carry it only as HMAC-SHA-256; `age_years` is the age at this study |
| `series` | series part | a series is split into parts by SOP class, orientation, image size, pixel spacing, a repeating acquisition, phase or echo, and stack (`part`); `selection_reason` is JSON with codes and parameters, not text (see "Checks and reasons") |
| `series_pairs` | PET part × CT part | same study, same non-empty Frame of Reference, z overlap > 0; `pet_attenuation_corrected` is 1, 0, or NULL when CorrectedImage is absent |
| `index_checks` | object × check code | checks of the project, a source, a patient, a study or a series; rebuilt at every merge |
| `patient_links` | link | a `pid:` or `folder:` HMAC and the patient it leads to |
| `cohorts` | saved selection | unique `label`, `created_at` |
| `cohort_series` | cohort × series | the series of a cohort with its `is_primary` flag; deleting a cohort deletes its rows |
| `project_meta` | key | `merged_generation`, `catalog_id`; later `link_key`, `link_key_id`, `selection_config`, `identity_config` (JSON), `identifiers_removed_at` |
| `key_counters` | key kind | the last number given out for `patient`, `study`, `series` and `pseudonym` |
| `runs` | analysis run | versions, device, models with checksums, settings; `locked` after the first export |
| `jobs` | worker job | status survives a crash: `running` becomes `interrupted` at launch and is re-queued |
| `results` | run × series × model × label | metrics of plan §10; empty metrics are NULL, never 0; `flags` comma-separated |
| `qc` | review decision | per series, or per label for a label exclusion |
| `audit_log` | event | import, selection changes, runs, QC, exports (scope, target, SHA-256); the import writes `index_merged`, `selection_changed`, `ids_confirmed`, `studies_assigned` and `patients_created` with counts only; no patient data |

No table has a column for names, birth dates or addresses (plan §6). A test in
each suite fails if one appears, and the Python test holds the index
catalog's tables to the same rule.

## Columns added in v2

- `sources`: `state` (`new`, `indexing`, `indexed`, `interrupted`,
  `unreachable`, `removed`), `volume_kind` (`local`, `network`, `removable`),
  `indexed_at`, `summary_json` (counts and codes only).
- `patients`: `id_status` (`dicom`, `file`, `unconfirmed`, `confirmed`,
  `unlinked`; see "Identity").
- `identifiers`: `id_source` (`dicom`, `file`, `folder`, `typed`).
- `studies`: `age_years`, `index_state` (`current`, `gone`), `selection_mode`
  (`auto`, `user`); index `studies_by_patient`, without which a merge of
  20 000 series took 5.31 s instead of 1.04 s.
- `series`: `index_state` (`current`, `gone`), `catalog_part`,
  `series_number`, `sop_class_uid`, `scanner_model`, `slice_count`,
  `slice_spacing_mm`, `z_extent_mm`, `orientation` (`axial`, `coronal`,
  `sagittal`, `oblique`), `image_rows`, `image_columns`,
  `transfer_syntax_uid`, `kernel_class` (`soft`, `sharp`, `unknown`),
  `auto_rank`, `auto_selected`, `selection_origin` (`auto`, `user`,
  `bulk_thin_ct`, `cohort`), `selection_cohort_id`; indexes `series_by_uid`
  and the unique `series_by_catalog_part`.

## Selection state

- `studies.selection_mode` is `auto` while the study follows the automatic
  choice and `user` once anyone changed it by hand, by a bulk action or by
  applying a cohort. A merge applies the automatic choice to auto studies
  only, and returns a user study to auto when its primary disappears or
  moves to another study (`check.primary_gone`).
- `series.selection_origin` says who selected or deselected the series:
  `auto`, `user`, `bulk_thin_ct` ("Select All CT ≤ n mm") or `cohort` (with
  `selection_cohort_id`). `auto_selected` and `auto_rank` hold the automatic
  choice whatever the study's mode; `auto_rank` is NULL for a part that does
  not qualify.
- At most one primary per study, and a primary is always selected: the
  partial unique index `series_one_primary_per_study` and the triggers
  `series_primary_is_selected_on_insert` and `…_on_update` refuse anything
  else. Statements therefore clear the primary, then set `selected`, then set
  the primary. The export takes the primary of each study.
- The studies of a patient with `id_status = 'unconfirmed'` are never
  selected.

## Gone rows

`index_state = 'gone'` marks a study or series part that is no longer in the
index catalog. A gone series has `selected = 0`, `is_primary = 0`,
`auto_selected = 0`, `auto_rank` and `catalog_part` NULL, and
`part = -rowid`, so that it can never collide with a current part on
`UNIQUE (series_uid, part)`. At each merge a gone series is deleted unless
`results`, `qc`, `jobs` or `cohort_series` refer to it; then gone studies
without series are deleted, then patients without studies.

## Keys

`patient_key` is `pt_%06d`, `study_key` `st_%06d`, `series_key` `s_%06d`, and
the pseudonym `P%04d` (wider past P9999). Each number comes from
`key_counters`, which v2 seeds from the highest key already in the file; keys
and pseudonyms are never reused, even after their row is deleted. New
patients are numbered by their first study date and then UID, new studies in
date order, new series by study key, series number, UID and part.

## Identity

- `link_key` is 32 random bytes, base64 in `project_meta`, made by the app
  when the project is created or migrated to v2 (*not built yet*: the v2
  SQL adds only `merged_generation` and `catalog_id`, and the key needs
  random bytes from Swift). `link_key_id` is the first 16 hex characters of
  HMAC-SHA256(link_key, "bcoa.link-key-id"); a catalog's links count only
  when its `link_key_id` matches.
- A `pid:` link is `"pid:" + hex(HMAC-SHA256(link_key, "pid" 0x1F
  utf8(PatientID)))`, after stripping spaces and NUL padding and normalizing
  to NFC. A `folder:` link is `"folder:" + hex(HMAC(link_key, "folder" ␟
  source_id ␟ folder components 1…k))`; it reaches `patient_links` only when
  the user confirms it.
- `id_status`: `dicom` (PatientID present), `file` (NIfTI `CT_<id>`),
  `unconfirmed` (no ID; folder candidates exist; never selected), `confirmed`
  (a folder ID confirmed or an ID typed), `unlinked` (identifiers removed or
  withheld; one patient per study).
- "Remove Identifiers" deletes `identifiers`, `patient_links`, `link_key` and
  `link_key_id` in one transaction with `secure_delete`, turns `unconfirmed`
  patients into `unlinked`, sets `identifiers_removed_at`, renames each
  `display_path` to "Source n" and writes an audit entry; in WAL mode a
  `wal_checkpoint(TRUNCATE)` follows. It then deletes the index catalog, the
  previews and the job files, and the exports holding PatientIDs that the
  user chose to delete. The bookmarks stay. ADR 0024 has the reasons. *Not
  built yet*: `ProjectStore.removeIdentifiers` still deletes `identifiers`
  only and writes the audit entry.

## Checks and reasons

- An `index_checks` row is (`object_kind`, `object_key`, `code`, `level`,
  `params_json`): the kind is `project`, `source`, `patient`, `study` or
  `series`, the level `info` or `warning`, and the parameters a JSON object,
  for example `{"count": 3, "minimum": 50}`.
- `series.selection_reason` is
  `{"v": 1, "outcome": "chosen|eligible|excluded|held", "codes": […],
  "params": {…}}`.
- Every code, with its parameters and its US-English default text, is
  registered in `Protocol/index_codes.json`. The database holds codes and
  numbers, never prose; the app renders the text through the String Catalog.

## Age

Each study stores `age_years`, from PatientAge or from the birth date, which
is read only in memory. `patients.age_at_first_study` is the age at the first
current study with a selected series, otherwise at the first dated current
study, and is recomputed after every merge and every selection change. The
export's first time point can be another study, since the export also counts
studies whose series have results and studies that are gone; OPEN_QUESTIONS
#27 asks which age the export writes.

## The index catalog

`<project>/index/catalog.sqlite` is a second database, written only by the
worker's index job (ADR 0020). Its format is 1 (`PRAGMA user_version = 1`);
it is never migrated, and a catalog of another format, or a damaged one, is
deleted and rebuilt.

- Per file: `files` (source, relative path as raw bytes, size, modification
  time, reader version, kind, header values), `frames`, `dicomdir_entries`,
  and `bad_dirs` for folders the walk could not list.
- Per scan: `scans` (state of each source) and `catalog_meta` (format,
  `catalog_id`, `generation`, `complete`, reader and worker version,
  `link_key_id`, the hash of the selection settings).
- Derived once per generation: `cat_studies`, `cat_series`, `cat_checks`,
  `cat_pairs`, `cat_id_candidates` and `cat_instances`.
- `pending_identifiers`: the PatientID and accession number of each study
  and where they came from, which the merge copies into `identifiers`; they
  stay until the catalog is deleted (OPEN_QUESTIONS #25).

It holds relative paths, plain PatientIDs and accession numbers, `pid:`
links and folder labels, so it lives inside the project folder and goes with
"Remove Identifiers". The app never writes it.
`test_the_merge_never_writes_the_catalog` holds that the merge writes
nothing to it whether it is attached read-only or not; attaching it
read-only where the system SQLite accepts URI file names, and reading folder
labels from it in one short read, belong to `IndexStore`, *not built yet*.
Deleting `index/` costs one full read of the sources.

## Journal mode

*Not built yet*: `ProjectStore.open` still keeps SQLite's default rollback
journal and GRDB's default busy mode. ADR 0021 decides that
`project.sqlite` runs in WAL mode when the project folder is on a local
volume and in DELETE mode otherwise, decided at every open, with a busy
timeout of 10 s on a local volume and 30 s on a network volume. WAL lets the
export hold its read transaction (12.5–14.7 s for 1.46 M result rows) while
the app writes; it needs shared memory, which SMB, AFP and NFS do not
provide reliably. The catalog always runs in DELETE mode; the worker's
`open_catalog` sets it.

## JSON columns of `runs`

Defined in [ADR 0013](adr/0013-export-reads-the-project-database.md), because
the export reads them:

- `models_json`: list in run order, which is the column order of the export:
  `[{"name": "clin_ct_organs", "zip_sha256": "…", "labels": {"1": "liver"},
  "colors": {"1": "#d4a373"}}]` (`colors` optional).
- `versions_json`: flat object of version strings: `app`, `moosez`,
  `nnunetv2`, `torch`, `python`, `macos`, `chip`, …
- `settings_json`: any object; copied into the export's `provenance` sheet.
