"""Spike S2: does a worker started by the sandboxed app inherit its access?

Proves four things, each reported separately so a No-Go says which one failed:
the worker can list and read the user-chosen source folder, write into the
project folder, whether it could write next to the source data, and whether it
sees MPS.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.jobs.selftest import accelerator_report
from bcoa_worker.protocol import Job, Result


def _probe_read(source: Path) -> dict[str, Any]:
    try:
        count = 0
        first: Path | None = None
        for entry in os.scandir(source):
            if entry.is_file():
                count += 1
                first = first or Path(entry.path)
        head = first.open("rb").read(132)[128:132] if first else b""
        # Only counts and booleans leave the worker; never names or paths.
        return {"ok": True, "files": count, "dicm_preamble": head == b"DICM"}
    except OSError as exc:
        return {"ok": False, "errno": exc.errno}


def _probe_write(directory: Path) -> dict[str, Any]:
    probe = directory / ".bcoa_spike_s2_probe"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "errno": exc.errno}


def run(job: Job, channel: ProtocolChannel) -> None:
    source = Path(str(job.payload["source_dir"]))
    work = job.project_path("work")
    work.mkdir(parents=True, exist_ok=True)
    report = {
        "read_source": _probe_read(source),
        "write_project": _probe_write(work),
        # Measured, not assumed: the open panel grants read-write for the
        # session, while bookmarks made with securityScopeAllowOnlyReadAccess
        # should resolve read-only. S2 records which one the worker inherits.
        "write_source": _probe_write(source),
        "accelerator": accelerator_report(),
        "sandboxed": "APP_SANDBOX_CONTAINER_ID" in os.environ,
    }
    channel.send(Result(job.job_id, report))
