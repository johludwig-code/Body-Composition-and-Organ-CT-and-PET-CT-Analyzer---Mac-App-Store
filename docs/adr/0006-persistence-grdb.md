# ADR 0006: SQLite through GRDB.swift

- Status: accepted
- Date: 2026-10-02
- Plan section: §3, §6

## Decision

GRDB.swift 7.x (MIT) in its own package `BCOAStore`, schema in versioned
migrations (`StoreMigrations.swift`), documented in `docs/schema.md`. The
project is a folder (`.bcoaproj`) holding `project.sqlite`, not an
`NSDocument`, because safe-save would copy gigabytes of `work/`.

Licence: MIT, compatible with the App Store; listed in THIRD_PARTY_NOTICES.

## Rejected

SwiftData (opaque schema, not readable from Python or R), Core Data (same),
NSDocument (safe-save).
