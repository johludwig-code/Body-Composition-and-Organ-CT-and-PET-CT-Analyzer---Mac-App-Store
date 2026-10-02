"""The protocol channel: the worker's original stdout, and nothing else on it.

MOOSE prints a figlet banner, Rich tables and progress bars; dcm2niix and
nnU-Net print to both streams; C extensions write to file descriptor 1
directly, past `sys.stdout`. Replacing `sys.stdout` would not catch the last
kind. So the descriptor itself is moved: the original stdout is duplicated to a
private descriptor for the protocol, and descriptors 1 and 2 are pointed at the
job's log file before any of those libraries is imported.
"""

from __future__ import annotations

import io
import os
import sys
import threading
from pathlib import Path
from typing import TextIO

from bcoa_worker.protocol import Event, event_to_line


class ProtocolChannel:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        # The heartbeat thread and the job write concurrently; one line must
        # never be interleaved with another.
        self._lock = threading.Lock()

    def send(self, event: Event) -> None:
        line = event_to_line(event)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    @classmethod
    def take_over_stdout(cls, log_path: Path) -> ProtocolChannel:
        """Claim fd 1 for the protocol and send everything else to `log_path`."""
        sys.stdout.flush()
        sys.stderr.flush()
        protocol_fd = os.dup(1)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
        # Python-level streams follow the descriptors, line-buffered so a crash
        # leaves the last lines in the log.
        sys.stdout = io.TextIOWrapper(os.fdopen(1, "wb", closefd=False), line_buffering=True)
        sys.stderr = io.TextIOWrapper(os.fdopen(2, "wb", closefd=False), line_buffering=True)
        stream = io.TextIOWrapper(os.fdopen(protocol_fd, "wb"), encoding="utf-8", newline="\n")
        return cls(stream)
