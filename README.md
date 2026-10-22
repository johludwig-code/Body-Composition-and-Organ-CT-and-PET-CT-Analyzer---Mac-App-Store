# Body Composition and Organ CT and PET-CT Analyzer

*Working title. The code name used in targets, modules and paths is **BCOA**
(`BCOAnalyzer`, `BCOAKit`, `bcoa_worker`), because the title is longer than the
30 characters the App Store allows for a name — see
[ADR 0001](docs/adr/0001-name-and-identity.md).*

A native macOS app for research in radiology and nuclear medicine: index DICOM
folders, choose series, segment them with MOOSE fully offline, review the
segmentations in a radiological viewer, assign a QC status and export the
numbers to Excel with one row per patient.

**For research use only. Not for clinical use. Not a medical device; not for
diagnosis or treatment decisions.** What the app says about its intended use,
whose work it contains and under which licences: [`docs/legal/README.md`](docs/legal/README.md).

## Status

Started fresh on 2 October 2026 from [`docs/PLAN.md`](docs/PLAN.md), which is
the source of truth. BOCARTA-MOOSE served only as a source of knowledge and
code ([ADR 0012](docs/adr/0012-what-came-from-bocarta-moose.md)).

| Milestone | State |
|---|---|
| M0 Spikes | **S1 Go** on Apple Silicon in CI and on the owner's Mac (M4 Pro, 22 October 2026): the sandboxed worker ran `clin_ct_organs` on MPS from the bundle's weights, a whole-body CT in 105 s; all ten clinical models ran offline on a real public PET/CT on Linux, with two MOOSE faults corrected in the adapter ([ADR 0018](docs/adr/0018-two-moose-faults-corrected-in-the-adapter.md)). **S2 Go** on the owner's Mac: a folder granted in the Open panel reaches the worker, and quitting during a job leaves no worker; ad hoc builds need library validation off for the helpers on macOS 27 ([ADR 0030](docs/adr/0030-m3-to-m7-on-the-owners-mac.md)). **S3**: what App Store Connect checks without an account is in `make verify`; signing and the upload need the developer account, the name and an icon. **S4**: sizes measured, and the M4 Pro's times (`docs/benchmarks.md`); the M1 with 16 GB is open. Runbook: [`docs/spikes/M0.md`](docs/spikes/M0.md). |
| M1 Foundation | Repository layout, XcodeGen project, entitlements, privacy manifest, IPC v1 with schemas and fixtures, worker skeleton, database migrations, build/sign/verify scripts. Compiled and tested on a macOS runner in CI. |
| M2 Import | Built, on branch `m2-import` and completed on the branches after it; started before S3 by [ADR 0019](docs/adr/0019-m2-starts-before-s3.md). Designed in ADRs [0020](docs/adr/0020-index-catalog-owned-by-the-worker.md) to [0029](docs/adr/0029-corrections-found-in-the-review-of-the-index-job.md). The worker scans and regroups; the app adds sources through the Open panel, queues scans with the link key added when the job starts, merges in the queue's finalize hook (`IndexStore`, on the Mac's SQLite with the catalog attached read-only), and shows the tree with selection, reasons, checks, filters, bulk actions, cohorts, Selection Settings…, Confirm Patient IDs… and Remove Identifiers…. Not built: the previews mode, Show Files…, typed IDs and Assign to Patient…. Acceptance still needs the owner's review of `corpus_expected.json` and a run with a real folder (ADR 0026). |
| M3 Pipeline | Built ([ADR 0031](docs/adr/0031-the-pipeline-jobs.md)): one job per stage and series, chained by the app: convert (the catalog's files, the bundled dcm2niix, a SimpleITK fallback for stacks dcm2niix splits, checks), segment, metrics, viewer cache; runs, recorded jobs, results; an interrupted stage is queued again at launch. The cache rule of plan §6 and the equivalence test are not built. |
| M4 Viewer | Built: three planes in radiological convention from the LPS cache, window presets, overlays per model and label, fill or contour, opacity, crosshair, neurological convention, the cursor's position, HU and labels. The 30 frames/s goal is not measured yet. |
| M5 Export | Engine with golden files (ADR 0013); the dialog in the app with every option; the first export locks its run and is audited. |
| M6 QC | QC per series (accept, accept with comment, reject) and labels excluded from the export, by buttons and keys, audited. Not built: the QC queue mode, the cohort overview with its chart, the volume outlier flag, the reproducibility package. |
| M6b–M8 | The per-case PDF report is decided in [ADR 0017](docs/adr/0017-per-case-pdf-report.md), not built; the handbook, the DMG and TestFlight (M7) and the Store (M8) need the owner's account, name and icon. |

What runs in CI on every push: the worker and script test suites (`make
test-worker`) on Linux, and `swift test` for both Swift packages and a Debug
build of the UI on a macos-15 arm64 runner. On pull requests that touch the
worker, the scripts, the models or the build, `bundle.yml` also builds the ad
hoc signed Release app with its Python backend, runs `make verify`, and has
the sandboxed worker run the S2 probe and a self-test with inference. Outside
CI, in Linux containers: all ten clinical models on a real public PET/CT,
whose numbers are in [`docs/benchmarks.md`](docs/benchmarks.md). Not done
yet: a run of the app on the owner's Mac.

## Layout

```text
App/          SwiftUI app: sources, resources, entitlements, tests, UI tests
Packages/     BCOAKit (protocol, worker process, queue, viewer geometry)
              BCOAStore (GRDB schema and migrations)
Worker/       Python package bcoa_worker with tests, requirements.in/.lock
Protocol/     JSON schemas and fixtures shared by both test suites
Scripts/      build_runtime.sh, fetch_models.py, slim_checkpoints.py,
              license_report.py, sign_tree.sh, embed_backend.sh,
              verify_bundle.py, run_selftest.sh
Models/       not versioned; only manifest.lock.json is
docs/         PLAN.md, adr/, spikes/, OPEN_QUESTIONS.md, schema.md, benchmarks.md
project.yml   XcodeGen definition
Makefile
```

## On a Mac (Apple Silicon, macOS 14+, Xcode 16+)

```bash
brew install xcodegen uv          # build tools only; nothing of this ships
make test-worker                  # Python tests
make runtime                      # pinned CPython 3.12 + locked packages into build/runtime
make models MODELS="clin_ct_organs"   # weights at build time, checked against the lock
make app                          # xcodegen + xcodebuild
make sign IDENTITY="Developer ID Application: …"
make verify                       # no itms-services, all Mach-O signed, no network entitlement …
```

`make all` runs the chain. Spike S2 starts with the unsigned `make app`.
