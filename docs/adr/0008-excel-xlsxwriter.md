# ADR 0008: XLSX through XlsxWriter in the worker

- Status: accepted
- Date: 2026-10-02
- Plan section: §11

XlsxWriter (BSD-2-Clause) writes the workbook in the worker; the app only
assembles the export options into the job JSON. Constant-memory mode for wide
exports. Column naming lives in `bcoa_worker/naming.py` and is the single
definition both the export and the data dictionary use.
