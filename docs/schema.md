# Project database schema

`<Study>.bcoaproj/project.sqlite`, created and migrated by `BCOAStore`
(`Packages/BCOAStore/Sources/BCOAStore/StoreMigrations.swift`, migration `v1`).
That file is the definition; this page explains it. `Worker/tests/test_schema_sql.py`
executes the same SQL with Python's sqlite3, so the schema is checked on every
platform.

| table | one row per | notes |
|---|---|---|
| `sources` | source folder | read-only security-scoped bookmark; `display_path` is the folder name only |
| `patients` | patient | `patient_key` is internal; `pseudonym` P0001…; sex and age derived at indexing |
| `identifiers` | patient | the only plain-text identifiers (PatientID, accession numbers); "Remove Identifiers" deletes them with `secure_delete` and writes an audit entry |
| `studies` | study | `study_uid` stays inside the project; exports carry it only as HMAC-SHA-256 |
| `series` | series part | a series with changing orientation or image size is split into parts (`part`); `selection_reason` is the tooltip text of the auto-selection |
| `runs` | analysis run | versions, device, models with checksums, settings; `locked` after the first export |
| `jobs` | worker job | status survives a crash: `running` becomes `interrupted` at launch and is re-queued |
| `results` | run × series × model × label | metrics of plan §10; empty metrics are NULL, never 0; `flags` comma-separated |
| `qc` | review decision | per series, or per label for a label exclusion |
| `audit_log` | event | import, selection changes, runs, QC, exports (scope, target, SHA-256); no patient data |

No table has a column for names, birth dates or addresses (plan §6). A test in
each suite fails if one appears.
