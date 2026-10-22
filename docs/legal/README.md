# Intended use, rights and licences

One place for everything the app says about what it is for and whose work
it contains, and where each statement appears. This is not legal advice
(plan §14); the institution's legal and regulatory offices check it before a
release (OPEN_QUESTIONS #21, #36).

## Intended use

| text | where it appears |
|---|---|
| "For research use only. Not for clinical use." (plan §2 rule 5) | every window's footer, start screen, About, every export (provenance sheet, methods file), DMG Read Me |
| "Research software for the automated segmentation and quantification of anatomical structures in CT data. Not intended for diagnosis, treatment planning, or any other clinical decision-making." (plan §13) | start screen, About, provenance sheet, footer's accessibility label, DMG Read Me, user guide |
| "Not a medical device; not for diagnosis or treatment decisions." (plan §13, the case report's line on every page) | every window's footer, start screen, About, provenance sheet (`regulatory_status`), methods file, DMG Read Me |
| What the user is responsible for: checking every result, not entering results into patient records, ethics approval and data protection (`IntendedUse.responsibility`) | About, DMG Read Me, user guide |

The texts live in `IntendedUse` (BCOAKit) and `bcoa_worker.intended_use`;
tests hold both to each other and to the plan's wording. None of them is a
screen to accept: App Review 2.4.5(vi) forbids licence or consent screens at
launch, and `LaunchTests` checks that none appears.

**Regulatory status.** With this intended use the app is typically not a
medical device under the MDR (plan §13); any clinical use would change that.
The app shows no reference ranges, percentiles against norms, traffic lights
or findings. The robust z-score in the cohort view compares series of one
study with each other to find failed segmentations; it is a QC aid, not a
normative value. Before results of real patients are reported case by case
(the planned PDF report), the institution classifies the report in writing
(OPEN_QUESTIONS #21).

## Rights

| component | licence | what the app does about it |
|---|---|---|
| MOOSE (`moosez` 3.2.2), code | Apache-2.0 | licence text in the notices; adaptations live in the app's adapter, not in MOOSE's files (ADR 0011, 0018); "MOOSE" is not in the app's name or icon (Apple 4.1(c), Apache-2.0 §6); About says the app is based on MOOSE and neither made nor endorsed by its authors |
| MOOSE model weights | CC BY 4.0 | attribution, licence link and the changes made (optimizer state removed) in the notices and About; "nothing in this app's license restricts the rights that license grants" (OPEN_QUESTIONS #14) |
| nnU-Net v2, SimpleITK, PyTorch, NumPy, SciPy, pandas, pydicom, XlsxWriter, … | Apache-2.0, BSD, MIT and others | `Scripts/license_report.py` writes `THIRD_PARTY_NOTICES.md` from the installed packages with their licence texts; GPL or AGPL fails the build; LGPL parts were removed (ADR 0014) |
| dcm2niix | BSD-2-Clause with public-domain and MIT parts | approved in `Scripts/license_approvals.json`, text in the notices |
| CPython 3.12 | PSF-2.0 and its bundled libraries | in the notices, with the one change made (`urllib/parse.py`) |
| GRDB.swift | MIT | full licence text in the notices |
| App icon | the owner's artwork (`App/Icon/AppIcon-source.png`) | rights to be confirmed by the owner (OPEN_QUESTIONS #37) |
| The app itself | not decided | the repository has no licence file; on GitHub that means all rights reserved (OPEN_QUESTIONS #35) |

The notices ship in the bundle (`Contents/Resources/licenses/`) and are shown
under About and Licenses; `make verify` fails a bundle without them.

## Privacy

No network entitlement, no telemetry, no crash upload; the privacy manifest
declares no tracking and no collected data, with the reasons for the APIs
the bundle uses. Patient names and birth dates are never stored; plain IDs
live only in `identifiers` and go with Remove Identifiers; the export is
pseudonymised by default (plan §13, ADR 0024).

## For the App Store

- App Privacy: "Data Not Collected". Category: Medical.
- Description: may say "Based on MOOSE" and must say "For research use only.
  Not for clinical use."; no claims of accuracy without the institution's own
  validation (plan §13).
- Review notes: the intended use, the offline design, and public test data
  (TCIA) to try the app with.
- Health apps are expected from a legal entity (5.1.1(ix)): the
  institution's developer account (OPEN_QUESTIONS #3).
