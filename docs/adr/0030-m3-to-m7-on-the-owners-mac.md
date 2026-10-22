# ADR 0030: Building on the owner's Mac, and ad hoc builds on macOS 27

- Status: accepted (the owner asked on 22 October 2026 to finish the app as a
  Mac app: "überprüfe alles genau und ermögliche die Fertigstellung als mac
  app")
- Date: 2026-10-22
- Plan section: §15, §17, §20

## Context

Until 9 October 2026 the project was built in the cloud and in CI only.
On 22 October 2026 it ran for the first time on the owner's Mac: an Apple
M4 Pro with 24 GB, macOS 27.0.1, Xcode 27.1, the repository on the external
volume "Ludwig T7". Three things came out of that first run:

1. The worker suite and the bundle checks had never run on macOS: a ruff
   release with a new rule, a corpus folder that APFS leaves behind, and the
   bundle tests, which met a real `codesign`, failed (fixed one by one).
2. The Makefile split its absolute paths at the space in "Ludwig T7".
3. **The sandboxed app could not start its worker.** The interpreter died
   at launch with SIGABRT: dyld refused `libpython3.12.dylib` because "mapping
   process and mapped file (non-platform) have different Team IDs". Every
   Mach-O file of the bundle is signed ad hoc on its own, with the hardened
   runtime, and on macOS 27 library validation no longer lets an ad hoc
   signed process load an ad hoc signed library. Reproduced outside the app
   with two files: ad hoc with `--options runtime` fails, plain ad hoc runs.
   The same build passed on macOS 15 in CI on 4 October 2026.

A build signed with a real identity (Developer ID, App Store, or a local
Apple Development certificate) gives every file the same team and is not
affected; only the ad hoc builds that `make app` makes by default are.

## Decision

1. Ad hoc builds (`IDENTITY=-`) sign the helpers with
   `App/Resources/HelperAdHoc.entitlements`: app-sandbox, inherit and
   `com.apple.security.cs.disable-library-validation`. `sign_tree.sh` picks
   it by itself when the identity is `-`; everything else keeps the hardened
   runtime.
2. `verify_bundle.py` accepts that third key only on an ad hoc signature and
   says so in its notes; on a build signed with an identity it fails, as
   before, on any key but the two of `Helper.entitlements`.
3. The steps of the plan that need the Mac (S1, S2, S4, the merge on the
   system SQLite) are run here and recorded in `docs/spikes/M0.md` and
   `docs/benchmarks.md`; the milestones after M2 are built on branches that
   follow `m2-import`, each merged only with the owner's word, as before.

## Consequences

- An ad hoc build runs on this Mac and on macOS 15 alike. It is for
  development and for the owner's own tests, never for distribution: the
  Store and notarisation need an identity (OPEN_QUESTIONS #3), and with one
  the exception is not there.
- The CI on macos-15 signs ad hoc as well and therefore now carries the
  exception too; its sandbox probe checks the same inheritance as before.

## Rejected alternatives

- **Ad hoc without the hardened runtime.** Also loads, but then every file of
  the bundle differs from a distributed build, and `make verify` would have
  to stop checking the runtime flag.
- **Signing local builds with the owner's Apple Development certificate.**
  It is in the keychain and would avoid the exception, but using Apple
  credentials is the owner's decision; it remains an option
  (`make app IDENTITY="Apple Development: …"`).
