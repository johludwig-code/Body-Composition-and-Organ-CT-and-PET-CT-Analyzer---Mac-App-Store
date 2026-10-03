"""What the worker may not do, refused inside the interpreter. See ADR 0016.

The App Sandbox already keeps the worker off the network and out of folders
nobody granted, but it does so by logging a violation and failing the call
somewhere deep inside a library. The first real CT segmented by the sandboxed
worker on Apple Silicon left four such violations in the kernel log: urllib3,
imported by MOOSE for its downloader, binds an IPv6 socket at import to learn
whether the machine has IPv6; nnU-Net runs `hostname` at import; matplotlib
runs `fc-list` and `system_profiler` to list the Mac's fonts when it builds
its font cache. None of it is needed to segment a CT.

An audit hook sees each of those before the operating system does and refuses
it with PermissionError, which every one of these callers already tolerates:
urllib3 then reports no IPv6, matplotlib uses the fonts it ships. The worker
thereby opens no network socket and starts no program at all, which is what
"no network code" (plan §2) and "no processes after quit" (App Store
2.4.5(iii)) ask, now enforced rather than assumed.
"""

from __future__ import annotations

import errno
import os
import socket
import sys
import traceback
from typing import Any

# Programs the worker may start, by absolute path. None yet: dcm2niix joins
# with the conversion job (milestone M2).
ALLOWED_PROGRAMS: frozenset[str] = frozenset()

_NETWORK_FAMILIES = {socket.AF_INET, socket.AF_INET6}
_PROGRAM_EVENTS = {
    "subprocess.Popen",
    "os.posix_spawn",
    "os.exec",
    "os.spawn",
    "os.system",
    "os.fork",
    "os.forkpty",
}

_installed = False
_reported: set[tuple[str, str]] = set()


# Frames that only say how the call was made, not who made it.
_PLUMBING = ("guard.py", "subprocess.py", "socket.py", "os.py")


def _caller() -> str:
    """The innermost frames that asked, for the job log.

    Six, because an import-time attempt is several frames deep in the library
    that makes it before the frame that imported the library.
    """
    frames = [
        f"{frame.filename.rsplit('site-packages/', 1)[-1]}:{frame.lineno}"
        for frame in traceback.extract_stack(limit=30)
        if not frame.filename.endswith(_PLUMBING) and not frame.filename.startswith("<frozen")
    ]
    return " <- ".join(reversed(frames[-6:]))


def _refuse(event: str, what: str) -> None:
    # Once per event and target: urllib3 or matplotlib may retry, and the log
    # is read by people looking for the one line that matters.
    if (event, what) not in _reported:
        _reported.add((event, what))
        print(f"[guard] refused {event} {what} from {_caller()}", file=sys.stderr, flush=True)
    raise PermissionError(errno.EPERM, f"the worker may not do this (ADR 0016): {event} {what}")


def _program(event: str, args: tuple[Any, ...]) -> str:
    """The executable an event would start, as the caller named it."""
    if event in ("os.fork", "os.forkpty"):
        return "fork"
    if event == "os.system":
        return os.fsdecode(args[0])
    # subprocess.Popen passes (executable, args, ...); the os events (path, argv, ...).
    executable = args[0] if args else None
    if executable is None and len(args) > 1:
        executable = args[1]
    if isinstance(executable, (list, tuple)):
        executable = executable[0] if executable else None
    return "?" if executable is None else os.fsdecode(executable)


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event == "socket.__new__":
        family = args[1]
        # -1 is socket.socket()'s default, which becomes AF_INET.
        if family in _NETWORK_FAMILIES or family == -1:
            _refuse(event, f"family={family}")
    elif event in _PROGRAM_EVENTS:
        program = _program(event, args)
        if program not in ALLOWED_PROGRAMS:
            _refuse(event, program)


def install() -> None:
    """Refuse network sockets and new programs for the rest of this process.

    Audit hooks cannot be removed, so this is called once, by the worker's
    entry point, after its own setup and before torch, MOOSE or nnU-Net is
    imported. Tests call it in a child interpreter.
    """
    global _installed
    if _installed:
        return
    sys.addaudithook(_hook)
    _installed = True
