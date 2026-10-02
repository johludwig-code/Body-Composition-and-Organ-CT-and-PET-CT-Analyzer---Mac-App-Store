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
| M0 Spikes | **S1 done on CPU**: MOOSE source read, five offline/sandbox traps handled in the adapter, then a real `clin_ct_organs` run through the worker with no network at all — weights from the bundle layout, no write into site-packages, output on the CT grid, slimmed weights (118 MB instead of 475 MB) bit-identical. The MPS run, S2, S3 and S4 need a Mac: [`docs/spikes/M0.md`](docs/spikes/M0.md) is the runbook. |
| M1 Foundation | Repository layout, XcodeGen project, entitlements, privacy manifest, IPC v1 with schemas and fixtures, worker skeleton, database migrations, build/sign/verify scripts. **Written, not yet compiled** — this was produced in a Linux container without Xcode. |
| M2–M8 | Not started. Metrics (section 10) and the export column naming (section 11) exist already because the plan asks for tests first in those places. |

What has actually been run, in a Linux container: the worker suite (62 tests:
protocol and fixtures, metrics, naming, pseudonymisation, the MOOSE adapter's
offline guards, the database schema SQL), the script suite (6 tests for
`verify_bundle.py`), `build_runtime.sh` against the Linux build of the same
CPython, `license_report.py`, `fetch_models.py` and the S1 run above. The
Swift code has **not** been compiled yet — there is no Swift toolchain in the
container; the first `make test-swift` and `make app` on a Mac are part of S2.

## Layout

```text
App/          SwiftUI app: sources, resources, entitlements, tests, UI tests
Packages/     BCOAKit (protocol, worker process, queue, viewer geometry)
              BCOAStore (GRDB schema and migrations)
Worker/       Python package bcoa_worker with tests, requirements.in/.lock
Protocol/     JSON schemas and fixtures shared by both test suites
Scripts/      build_runtime.sh, fetch_models.py, slim_checkpoints.py,
              license_report.py, sign.sh, verify_bundle.sh
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
