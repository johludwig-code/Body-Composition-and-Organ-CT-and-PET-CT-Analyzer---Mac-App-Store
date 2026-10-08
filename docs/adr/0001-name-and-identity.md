# ADR 0001: Working title, code name and bundle identity

- Status: accepted (name and bundle ID still open, see OPEN_QUESTIONS.md)
- Date: 2026-10-02
- Plan section: §1, §14, §19

## Context

The plan calls the app "AtlasQuant". The project owner replaced that with the
working title **"Body Composition and Organ CT and PET-CT Analyzer"** (49
characters). App Store Connect limits the app name to 30 characters, and the
menu bar shows `CFBundleName`, which macOS truncates well before that. The
plan's licence section forbids "MOOSE" in the name and icon (Apple 4.1(c),
Apache-2.0 §6); the working title complies.

## Decision

- `CFBundleDisplayName` carries the full working title; it is what Finder and
  the About window show.
- The code name is **BCOA**: app target and executable `BCOAnalyzer`, Swift
  packages `BCOAKit`/`BCOAStore`, Python package `bcoa_worker`,
  `CFBundleName` "BCO Analyzer".
- Bundle identifier `de.ludwig.bcoanalyzer` as a placeholder. It must change
  to the institution's reverse domain if the institution's developer account
  submits the app (§13: Apple expects health apps from a legal entity).
- The plan's `aq_backend` becomes `bcoa_worker`, `AtlasQuant` becomes BCOA in
  every path of §4 and §20; the project folder extension `.aqproj` becomes
  `.bcoaproj`.

## Consequences

The Store name must be chosen before M8. Candidates within 30 characters:
"Body & Organ CT Analyzer" (24), "BodyComp & Organ CT/PET" (23). Renaming
later touches `Info.plist` and the worker's `APP_NAME` in
`Worker/bcoa_worker/export/tables.py`, which writes the name into exported
methods texts and cannot read the bundle; a test holds the two equal. The UI
reads the name from the bundle.

The title promises PET-CT and body composition. In the plan both are phase 2
(§18). The PET/CT pairing is already indexed in v1 (§7), so the promise is kept
by the data model; see OPEN_QUESTIONS.md for the priority question.
