# Working on BCOA with Claude Code

This repository was started fresh on 2 October 2026. BOCARTA-MOOSE (the
Bocarta/BTM app) is a source of knowledge only and is never modified from here
(ADR 0012).

Read `docs/PLAN.md` completely before changing anything. The plan is the source
of truth; a deviation is recorded as an ADR in `docs/adr/` and the plan is
updated to match.

## Rules (from section 20 of the plan)

- M0 first. No features before the spikes have a Go. The exceptions the plan
  itself makes: tests first for metrics, export and orientation.
- Every architecture decision is an ADR in `docs/adr/` before the code exists.
- Code, comments, commits and all UI text in US English. UI text goes through
  the String Catalog (`App/Resources/Localizable.xcstrings`) so that localising
  later needs no rework.
- Swift 6 with strict concurrency checking, SwiftUI and Observation. A new
  dependency needs an ADR and a licence check.
- Python 3.12 with type hints, ruff and pytest. No network code in the worker;
  every path comes from the job JSON.
- Every change comes with tests; CI is green before a merge.
- Never real patient data in the repository, tests, logs, commits or prompts.
- Unclear points go to `docs/OPEN_QUESTIONS.md` and are asked, not guessed.
- MOOSE sources are never modified; adaptations live in
  `Worker/bcoa_worker/moose_adapter.py` only.
- No network entitlement, no download logic, no update check — in the app and
  in the worker.
- One branch and pull request per milestone: summary, test evidence,
  screenshots, updated `docs/benchmarks.md`.
- `make verify` runs before every merge.

## App Store compliance is checked at every step

The app must stay acceptable to the Mac App Store as a whole, not only the
Swift part. Before calling anything done, check it against these:

| rule | what it means here |
|---|---|
| App Sandbox always (2.4.5(i)) | also for the DMG build; one entitlements file for every channel |
| no code download (2.5.2) | Python, packages, weights and dcm2niix are in the bundle; nothing is installed after launch |
| no processes after quit (2.4.5(iii)) | `WorkerSupervisor` terminates every worker in `applicationWillTerminate` |
| updates only via the Store (2.4.5(vii)) | no update check, no Sparkle |
| no licence/consent screen at launch (2.4.5(vi)) | the research notice is shown, never acknowledged |
| no "itms-services" anywhere | `build_runtime.sh` patches `urllib/parse.py`, `verify_bundle.sh` greps the whole bundle |
| code only where Apple expects it | every Mach-O is signed inside out with hardened runtime; placement confirmed in spike S3 |
| privacy | `PrivacyInfo.xcprivacy` declares no tracking and no collected data |

## Commands

```bash
make test-worker     # pytest + ruff for Worker/
make test-swift      # swift test for Packages/BCOAKit and Packages/BCOAStore
make all             # runtime → models → app → sign → verify
```

## Comments

Comments say why, not what: the reason, the measurement or the failure that
produced the line. Full sentences.
