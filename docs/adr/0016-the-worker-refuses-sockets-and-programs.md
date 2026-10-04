# ADR 0016: The worker refuses network sockets and new programs itself

- Status: accepted
- Date: 2026-10-02
- Plan section: §2 (no network code), §5 (worker), §16 (sandbox and offline), spike S2

## Context

The first public CT that the signed app's worker segmented inside the
sandbox on Apple Silicon (bundle.yml, real case, commit e435723) gave
correct labels, and the sandbox report failed the run anyway. The kernel had
refused the worker four kinds of operation:

| refusal | cause, found by running the same job under a Python audit hook on Linux |
|---|---|
| `network-bind local:*:0` | urllib3, imported through `requests` by `moosez.download`, binds an IPv6 socket at import to find out whether the machine has IPv6 (`urllib3/util/connection.py`, `_has_ipv6`) |
| `file-read-data /dev/fd` (three times) | CPython listing open descriptors in a child it is about to start, one per program: nnU-Net runs `hostname` at import (`nnunetv2/configuration.py`), and matplotlib, building its font cache, runs `fc-list` and, on macOS only, `system_profiler` to list the system's fonts (`matplotlib/font_manager.py`, `findSystemFonts`) |
| `ipc-posix-sem-create` | tqdm's lock, already settled by ADR 0015 |
| `file-read-data /private/tmp` | not reproduced on Linux; the next run's report lists each refusal with its time |

None of them is needed to segment a CT, none sent anything anywhere, and each
library carried on after the refusal. But every one of them is the kind of
operation plan §2 and App Store 2.4.5(iii) rule out, and each was only
stopped by the sandbox, deep inside a library, with a kernel log entry as the
only trace.

## Decision

The worker installs a Python audit hook (`bcoa_worker/guard.py`) at start,
after its own setup and before any job imports torch, MOOSE or nnU-Net. The
hook refuses, with `PermissionError`:

- any socket of the IPv4 or IPv6 family;
- starting any program (`subprocess`, `os.posix_spawn`, `os.exec*`,
  `os.spawn*`, `os.system`, `os.fork`), except those in
  `guard.ALLOWED_PROGRAMS`, which is empty until the conversion job brings
  the bundled dcm2niix (M3).

`PermissionError` is an `OSError`, which urllib3 and matplotlib already
expect from a missing network or a missing program: urllib3 then reports no
IPv6, matplotlib builds its font cache from the fonts it ships. nnU-Net's
`subprocess.getoutput` does not tolerate it, so the app and the worker set
`nnUNet_n_proc_DA=12`, the value nnU-Net picks on any host it does not know,
and the lookup is skipped. Each refusal is written once to the job log with
the frames that asked for it.

## Consequences

- The worker opens no network socket and starts no program, whatever a
  future version of MOOSE, nnU-Net or their dependencies tries. A new
  attempt shows up as a `[guard] refused …` line naming its caller, and as
  a failed job only if the library does not tolerate the refusal, which the
  CI's real case and self-test would show first.
- Measured on Linux on the real CT: the body model's labelmap with the guard
  is bit-identical to the one without it; the guard refused the urllib3
  socket, `fc-list`, and on Linux only, three programs ctypes tries while
  looking for `libdl` for the CUDA package, which is not in the macOS wheels.
- Code that only runs on macOS cannot be exercised on Linux, so it was read
  instead: of the 4 057 module files that importing the worker, MOOSE,
  nnU-Net's predictor and trainer, torch, SimpleITK and matplotlib loads,
  136 lines start a program. Those inside a macOS branch are matplotlib's
  `system_profiler` (tolerates `OSError`), numexpr's `sysctl` (not reached:
  macOS answers `os.sysconf("SC_NPROCESSORS_ONLN")` first), joblib's
  `sysctl` (inside `except Exception`, and joblib is held to one process by
  `JOBLIB_MULTIPROCESSING=0`), and code that only runs when asked
  (`pandas` locales, sympy's preview, torch's compiler). The real case on
  the Mac remains the test that counts.
- Processes started without Python's own functions (multiprocessing's spawn
  calls `_posixsubprocess` directly) raise no audit event. ADR 0015 keeps
  nnU-Net from starting them, and they need semaphores, which the sandbox
  refuses.
- AF_UNIX sockets stay allowed: `socket.socketpair()` is how some libraries
  wake a thread, and it reaches nothing outside the process.

## Rejected alternatives

- **Rely on the sandbox alone.** It stops the same operations, but only as a
  failure inside a library and a kernel log entry, and only in the signed
  app: a worker started by hand or on Linux would behave differently.
- **Remove `requests` and `urllib3` from the bundle.** `moosez.download`
  imports `requests` at module level and `moosez` imports `moosez.download`,
  so MOOSE would not import at all.
- **Point matplotlib at a prebuilt font cache.** The cache holds absolute
  paths of the build machine and is rebuilt per configuration folder; the
  refusal is simpler and leaves no path behind.
