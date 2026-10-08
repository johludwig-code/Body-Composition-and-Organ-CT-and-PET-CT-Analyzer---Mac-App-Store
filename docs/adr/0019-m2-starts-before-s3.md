# ADR 0019: The import (M2) is built before spike S3 has a Go

- Status: accepted (the owner's go-ahead of 8 October 2026; a "no" on the
  decision card in the project thread reverts it before anything merges)
- Date: 2026-10-08
- Plan section: §17, §20

## Context

Plan §17 and §20 say that features start only after all four spikes of M0
have a Go. On 8 October 2026 the spikes stood as `docs/spikes/M0.md`
records them:

- S1 is a Go on Apple Silicon in CI.
- S2 is shown in CI with a sandboxed probe. Two steps need the owner's Mac:
  a folder granted through the Open panel, and quitting the app during a
  job.
- S3 cannot run: signing with a real identity and the TestFlight upload need
  the institution's developer account (OPEN_QUESTIONS #3), the final name
  (#1) and an app icon (#10). Everything App Store Connect checks without an
  account is already in `make verify`.
- S4 has every size measured; the timing on an M1 with 16 GB is open.

M1's foundation is in the code and compiled in CI. The work that can be
done in the cloud would otherwise wait on things only the owner can supply.

## Decision

M2 (import) is built now, on its own branch and pull request, before S3 has
a Go. Nothing of it merges without the owner's word.

S3's outcome does not change the import. S3 decides where the Python
runtime sits in the bundle, which touches `BundleLayout` and the embedding
script only, and which channel ships the app. Both No-Go alternatives the
plan names keep the import as it is: a DMG-only release changes nothing in
the app, and copying data into the container happens at conversion (M3),
after the index has chosen the series.

The import does depend on one open M0 step: the worker reads the source
folders the user granted through the Open panel, which is S2 step 4 on the
owner's Mac. The CI probe showed that a worker inherits the sandbox and is
refused an ungranted folder, but not yet that it inherits a grant made in
the Open panel. That step takes about fifteen minutes, and if it fails, the
index job is the first thing to change. It is therefore the M0 step to run
first.

## Consequences

- M2 is reviewed and merged like every milestone: tests green, `make verify`
  green, ADRs and docs updated, the owner's word for the merge.
- The README's status table says that M2 started before S3.
- S3 stays open and is run as soon as the account, name and icon exist.

## Rejected alternatives

- Waiting for S3: the account, name and icon are institutional decisions
  that can take weeks, and nothing in M2 depends on them.
- Covering M3 (pipeline) as well: M3 runs MOOSE on whole CTs, and its
  defaults (the memory warning, one segmentation worker at a time) depend on
  S4's measurement on the 16 GB minimum device. Starting M3 early is a
  decision of its own.
