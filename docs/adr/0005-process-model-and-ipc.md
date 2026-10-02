# ADR 0005: A fresh worker process per job, JSON Lines over stdout

- Status: accepted (sandbox inheritance verified in spike S2)
- Date: 2026-10-02
- Plan section: §3, §5

## Decision

- The app launches `python -I -m bcoa_worker run --job <path>` for each job.
  `-I` plus `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1` because the signed
  bundle is read-only and must stay byte-identical.
- The worker duplicates fd 1 as the protocol channel, then points fd 1 and 2 at
  the job's log file with `os.dup2`. MOOSE prints a figlet banner, Rich tables
  and progress bars, and `dcm2niix` writes to stdout; none of that may reach
  the protocol.
- Protocol version 1, eight event types, schemas in `Protocol/schemas`, fixtures
  in `Protocol/fixtures`; both test suites parse every fixture.
- Cancel: SIGTERM, worker cleans up and exits 130; SIGKILL after 10 s.
  Watchdog: no heartbeat for 5 minutes ends the job.
- One segmentation worker at a time (GPU memory); index and export may run in
  parallel.

## Why stdout and not XPC

An XPC service would have to be a bundle target of its own with its own
sandbox, and Python cannot be the XPC service binary without a Swift shim.
JSON Lines needs nothing but a pipe and is testable from a shell. XPC remains
possible later behind the same `WorkerChannel` protocol in BCOAKit.
