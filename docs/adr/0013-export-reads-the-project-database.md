# ADR 0013: The export job reads the project database and writes into the project

- Status: accepted
- Date: 2026-10-02
- Plan section: §11, §12

## Context

ADR 0008 put the workbook into the worker. Two questions were left: where the
worker gets the numbers from, and where it writes the file. A wide export of
1 000 patients with all clinical models is about 1.5 million values, and the
app already has them in `project.sqlite`.

## Decision

1. **Input.** The `export` job names a run; the worker opens
   `project.sqlite` read-only (`mode=ro` URI, `query_only`) and reads patients,
   studies, series, results, QC and jobs itself. The app sends only the
   options. The schema is defined once in `BCOAStore` and executed by both
   test suites (`test_schema_sql.py`), so the worker cannot drift from it
   unnoticed.
2. **Run JSON columns.** `runs.models_json` is a list in run order, which is
   the column order of the export:
   `[{"name": "clin_ct_organs", "zip_sha256": "…", "labels": {"1": "liver", …},
   "colors": {"1": "#d4a373"}}]` (`colors` optional until the viewer defines
   them in M4). `runs.versions_json` is a flat object of version strings
   (`app`, `moosez`, `nnunetv2`, `torch`, `python`, `macos`, `chip`, …).
   `runs.settings_json` is any object; it is copied into `provenance` as
   given.
3. **Output.** The worker writes only below `<project>/exports/`; the job
   names a file stem, never a path. Writing to a folder the user chooses
   outside the project depends on what the sandboxed worker inherits, which
   is spike S2; the app copies the finished file instead, which works either
   way.
4. **CSV.** The `results` sheet becomes `<stem>.csv`, every other sheet
   `<stem>.<sheet>.csv`, so that choosing CSV loses neither the data
   dictionary nor the provenance. CSV for R/Python is UTF-8 without BOM, comma,
   decimal point. CSV for Excel is UTF-8 **with** BOM (Excel reads UTF-8
   without it as the system code page), semicolon, decimal comma.
5. **Formula injection.** Series descriptions, kernels and comments come from
   DICOM headers and people. In the workbook every text cell is written with
   `write_string`, so `=…` stays text. In CSV for Excel a text value starting
   with `=`, `+`, `-`, `@`, tab or carriage return gets a leading apostrophe;
   CSV for R/Python stays verbatim, because those readers do not evaluate
   anything and a changed value would be a wrong value there. CSV cannot keep
   Excel from stripping the leading zeros of a PatientID; that is what the
   workbook's text cells are for, and the default identifier is the
   pseudonym, which has no such problem.
6. **Not in this step.** The reproducibility package (M6) is refused with a
   clear error rather than silently left out. Hashed study UIDs are not
   exported yet, because the per-project key has no column; rows are traced
   through pseudonym, timepoint and run ID instead.

## Consequences

The worker depends on the database schema; a migration that renames a column
the export reads fails the worker suite. The export can run while the app
keeps writing, because SQLite readers see a consistent snapshot.
