# Body Composition and Organ CT and PET-CT Analyzer

*Working title. The code name used in targets, modules and paths is **BCOA**
(`BCOAnalyzer`, `BCOAKit`, `bcoa_worker`), because the title is longer than the
30 characters the App Store allows for a name — see
[ADR 0001](docs/adr/0001-name-and-identity.md).*

A native macOS app for research in radiology and nuclear medicine: index DICOM
folders, choose series, segment them with MOOSE fully offline, review the
segmentations in a radiological viewer, assign a QC status and export the
numbers to Excel with one row per patient.

**For research use only. Not for clinical use.**

## Status

Started fresh on 2 October 2026 from [`docs/PLAN.md`](docs/PLAN.md), which is
the source of truth. BOCARTA-MOOSE served only as a source of knowledge and
code ([ADR 0012](docs/adr/0012-what-came-from-bocarta-moose.md)).

| Milestone | State |
|---|---|
| M0 Spikes | **S1 Go** on Apple Silicon in CI: the sandboxed Release app's worker ran `clin_ct_organs` on MPS from the bundle's weights, and MPS matched the CPU label for label; all ten clinical models ran offline on a real public PET/CT on Linux, with two MOOSE faults corrected in the adapter ([ADR 0018](docs/adr/0018-two-moose-faults-corrected-in-the-adapter.md)). **S2** shown in CI with a probe; the Open panel and quitting during a job are left for a Mac. **S3**: what App Store Connect checks without an account is in `make verify`; signing and the upload need the developer account and an icon. **S4**: sizes of every model and of the Release app measured; the M1 with 16 GB is open. Runbook and results: [`docs/spikes/M0.md`](docs/spikes/M0.md). |
| M1 Foundation | Repository layout, XcodeGen project, entitlements, privacy manifest, IPC v1 with schemas and fixtures, worker skeleton, database migrations, build/sign/verify scripts. Compiled and tested on a macOS runner in CI. |
| M2 Import | In progress on branch `m2-import`, started before S3 by [ADR 0019](docs/adr/0019-m2-starts-before-s3.md), the owner's decision of 8 October 2026. Designed in ADRs [0020](docs/adr/0020-index-catalog-owned-by-the-worker.md) to [0026](docs/adr/0026-how-the-import-is-tested-and-accepted.md). Built so far: the protocol additions (the index payload and result schemas, the progress `detail`, the artifact kind `preview`, their fixtures, the code registry `Protocol/index_codes.json` and the link vectors), migration v2, the index catalog's schema in the worker, and the merge, selection and identity SQL as Swift constants. The SQL and the migration are tested on Linux through pytest (scenarios S1 to S11, confirming and assigning patients, the test that the merge never writes the catalog, and a 20 000-series scale gate); the Swift code and its new tests have not been compiled yet, which the macOS runner does first. Not built yet: the worker's walk, read and regroup, the previews, `IndexStore` with the journal modes and Remove Identifiers, the job queue's finalize hook, and the UI. |
| M3–M8 | Not started, apart from what the plan asks to have tests first: the metrics (section 10) and the export engine (section 11, M5): XLSX and both CSV variants, long and wide, every row level and timepoint rule, all seven sheets, methods text and masks, held by golden files ([ADR 0013](docs/adr/0013-export-reads-the-project-database.md)). The export dialog in the app is not built yet. The per-case PDF report (M6b) is decided in [ADR 0017](docs/adr/0017-per-case-pdf-report.md), not built. |

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
