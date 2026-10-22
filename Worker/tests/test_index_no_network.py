"""The index job makes no network attempt and starts no program (ADR 0016,
ADR 0022 Consequences), and imports nothing of the other jobs (ADR 0020
decision 11).

The corpus is scanned by a worker of its own, started through the worker's
main with the guard installed, as the app starts it; an audit hook in that
process records every socket event and every program start, including the
ones the guard would let through or refuse.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from index_jobs import AUDIT_PRELUDE


def test_a_full_scan_opens_no_socket_and_starts_no_program(worker_scanned_corpus: Any) -> None:
    scan = worker_scanned_corpus
    assert scan.code == 0, scan.log[-2000:]
    assert scan.result["changed"] is True
    assert scan.audit == ()
    # The guard would have logged a refusal; with the `requests` block of
    # bcoa_worker.index there is nothing to refuse.
    assert "[guard]" not in scan.log


def test_an_index_job_imports_no_model_no_export_and_no_downloader(
    worker_scanned_corpus: Any,
) -> None:
    modules = set(worker_scanned_corpus.modules)
    assert "bcoa_worker.index.run" in modules
    roots = {name.split(".")[0] for name in modules}
    for package in ("torch", "moosez", "nnunetv2", "urllib3", "matplotlib", "xlsxwriter"):
        assert package not in roots, package
    assert not [m for m in modules if m.startswith(("bcoa_worker.jobs", "bcoa_worker.export"))]
    assert "bcoa_worker.moose_adapter" not in modules
    # Present only as the block itself: None in sys.modules.
    assert not [m for m in modules if m.startswith("requests.")]


def test_the_audit_hook_would_see_one() -> None:
    # The control: the same hook in a process that does open a socket and
    # start a program records both, so an empty record above means none.
    script = "\n".join(
        [
            "import sys",
            AUDIT_PRELUDE,
            "import socket, subprocess",
            "socket.socket(socket.AF_UNIX).close()",
            "subprocess.run([sys.executable, '-c', 'pass'], check=True)",
            "print(json.dumps(_audited))",
        ]
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    recorded = json.loads(out.stdout)
    assert "socket.__new__" in recorded
    assert "subprocess.Popen" in recorded
