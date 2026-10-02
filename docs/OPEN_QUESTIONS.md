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
| 7 | Contact the MOOSE authors | yes, before the pilot | open |
| 8 | Phase 2 priority: PET SUV or body composition | the working title names both; PET/CT pairs are already indexed in v1 | open |

## New

| # | question | context | recommendation |
|---|---|---|---|
| 9 | Own repository | the owner chose an own repository on 2 October 2026 | move with `git subtree split` once it exists (ADR 0002) |
| 10 | App icon | the Store needs one; no "MOOSE" in name or icon (Apple 4.1(c)) | commission or design one; `AppIcon.appiconset` is empty |
| 11 | `tiny_component` auto-QC flag | BOCARTA-MOOSE saw MOOSE report a 0.1 mL spleen in a pelvic CT — a fragment at the field-of-view edge, not an organ | add to §12: flag labels whose volume is below a per-label minimum, export as empty with a QC note |
| 12 | Export compliance (`ITSAppUsesNonExemptEncryption = NO`) | the bundled CPython carries OpenSSL, which the app never uses for transport (no network entitlement) | keep NO; confirm in S3 that App Store Connect accepts it; alternatively drop `_ssl` from the runtime if MOOSE still imports without it |
| 13 | Drop the PDF report entirely in v1? | BOCARTA-MOOSE's WeasyPrint/Pango PDF is what keeps it from notarisation | yes; XLSX, CSV and the methods text cover §11 (ADR 0003) |
