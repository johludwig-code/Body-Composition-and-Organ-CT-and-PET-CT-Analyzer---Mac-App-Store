# ADR 0003: One sandboxed, self-contained build for every channel

- Status: accepted; point 5 superseded by [ADR 0017](0017-per-case-pdf-report.md)
- Date: 2026-10-02
- Plan section: §2, §15

## Context

The owner's condition is that the app is App Store compliant as a whole, at
every step. The plan reaches the same place through §2 rules 1–3 and 9.
BOCARTA-MOOSE shows what happens otherwise: it installs ~10 GB of Python
environments with Homebrew and uv at first launch, needs network access, and
runs ad-hoc signed with the hardened runtime off because WeasyPrint needs
`DYLD_FALLBACK_LIBRARY_PATH`. Each of those is a rejection reason (2.5.2,
2.4.5(i), hardened runtime/notarisation).

## Decision

1. One entitlements file (`App/Resources/BCOAnalyzer.entitlements`) for DMG,
   TestFlight and Store: `app-sandbox`, `files.user-selected.read-write`,
   `files.bookmarks.app-scope`. No `network.client`, no `network.server`.
2. Helpers (the Python interpreter, `dcm2niix`) carry exactly `app-sandbox` and
   `inherit` (`App/Resources/Helper.entitlements`).
3. Hardened runtime on for everything. An exception
   (`cs.allow-unsigned-executable-memory`, `cs.disable-library-validation`) only
   if a spike proves it is needed, recorded as its own ADR. These exceptions are
   allowed in the Store but each one is a question in review.
4. Everything is in the bundle: CPython, the locked packages, the weights, and
   dcm2niix (which comes as the `dcm2niix` wheel and needs no separate build).
5. ~~No PDF output in v1. Reports are XLSX/CSV and a methods text; this removes
   the WeasyPrint/Pango dependency that blocks signing in BOCARTA-MOOSE.~~
   *Superseded by [ADR 0017](0017-per-case-pdf-report.md) on 2026-10-04: the
   owner wants an optional per-case PDF report, drawn by code that is already
   in the signed bundle. The rule behind this point stays: no WeasyPrint, Pango
   or Cairo, no `DYLD_*` variables, and a PDF only from code that is signed in
   the bundle.*
6. `PrivacyInfo.xcprivacy` declares no tracking, no collected data, and the
   required-reason APIs the app uses (file timestamps, disk space, user
   defaults, and system boot time: the interpreter's monotonic clock,
   `mach_absolute_time`, used for heartbeats and durations only). `make
   verify` fails on any category the bundle imports that the manifest misses.
7. Nothing runs after quit: the worker supervisor sends SIGTERM to every worker
   in `applicationWillTerminate`, waits up to 10 s, then SIGKILL.

## Consequences

`make verify` enforces 1, 2, 4 and the "itms-services" rule mechanically.
Spike S3 is the proof that the Store accepts an embedded Python with PyTorch;
until it passes, the DMG through Developer ID is the fallback channel and uses
the identical sandboxed build.
