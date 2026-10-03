# Open questions

Decisions the plan leaves to the owner (§19), and questions that came up
while building. Each one names a recommendation; nothing here is decided until
the owner says so.

## From the plan (§19)

| # | question | recommendation | status |
|---|---|---|---|
| 1 | Final app name and bundle ID | Store name within 30 characters, e.g. "Body & Organ CT Analyzer"; bundle ID under the institution's domain | open — working title in use (ADR 0001) |
| 2 | Distribution channel for v1 | notarised DMG for the pilot, TestFlight for testers, Store after S3 proves it | open |
| 3 | Apple Developer account: person or institution | institution (Apple 5.1.1(ix) expects health apps from a legal entity) | open |
| 4 | Models in the bundle: all or clinical only | the ten clinical CT models plus their dependencies; measured size decides (S4) | open |
| 5 | Minimum hardware 16 GB RAM | yes, as MOOSE recommends; S4 measures the peak | open |
| 6 | Pseudonymisation and storage with data protection | as in §13; to be agreed with the data protection officer | open |
| 7 | Contact the MOOSE authors | yes, before the pilot. Since checked: moosez 3.2.2's own README (PyPI source archive) states code Apache-2.0, models CC BY 4.0, and advertises a commercial version through Zenta. CC BY 4.0 allows this app, but a Store release of their weights is worth telling them about first | open |
| 8 | Phase 2 priority: PET SUV or body composition | the working title names both; PET/CT pairs are already indexed in v1 | open |

## New

| # | question | context | recommendation |
|---|---|---|---|
| 9 | Own repository | chosen by the owner on 2 October 2026 | done (ADR 0002) |
| 10 | App icon | the Store needs one; no "MOOSE" in name or icon (Apple 4.1(c)) | commission or design one; `AppIcon.appiconset` is empty |
| 11 | `tiny_component` auto-QC flag | BOCARTA-MOOSE saw MOOSE report a 0.1 mL spleen in a pelvic CT — a fragment at the field-of-view edge, not an organ | add to §12: flag labels whose volume is below a per-label minimum, export as empty with a QC note |
| 12 | Export compliance (`ITSAppUsesNonExemptEncryption = NO`) | the bundled CPython carries OpenSSL, which the app never uses for transport (no network entitlement). Checked on 2 October 2026: the worker, MOOSE, nnU-Net's predictor and its trainer lookup all import with `_ssl` and `_hashlib` blocked, so nothing on the inference path needs OpenSSL. Dropping OpenSSL is still not possible: python-build-standalone links `_ssl`, `_hashlib` and OpenSSL into `libpython3.12.dylib` on macOS. python-gdcm's second OpenSSL is gone (ADR 0014) | keep NO; confirm in S3 that App Store Connect accepts it |
| 13 | Drop the PDF report entirely in v1? | BOCARTA-MOOSE's WeasyPrint/Pango PDF is what keeps it from notarisation | yes; XLSX, CSV and the methods text cover §11 (ADR 0003) |
| 14 | CC BY 4.0 and the App Store licence | CC BY 4.0 §2(a)(5)(C) forbids adding terms that restrict what a recipient may do with the weights; Apple's standard licence (EULA) restricts copying the app. The weights are plain files in the bundle and unencrypted, so no technical measure applies, but the EULA's wording is the open point | add one sentence to the notices and the Store description: "The MOOSE model weights in this app are licensed under CC BY 4.0; nothing in this app's licence restricts the rights that licence grants." Let the institution's legal office confirm |
| 15 | CPU fallback when MPS fails | the segment job retries a model on the CPU when MPS raises (`retry_on_cpu`, on by default). On the GitHub M2 Pro runner the organ model on a whole-body CT took 395 s on MPS and had not finished after 68 minutes on the CPU; the same job takes 12.5 minutes on four Linux Xeon cores, so the Mac's CPU path is unusually slow there (cause not known yet; the real case's crop comparison measures it per slice) | keep the retry for a single job but say before it starts that it may take an hour or more per model; in a batch, mark the series failed and let the user re-run it on the CPU |
