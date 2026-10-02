"""The worker refuses network sockets and new programs (ADR 0016).

An audit hook stays for the life of its interpreter, so every test here runs
in a child interpreter; installing the guard in pytest's own process would
refuse the subprocesses the other tests start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import WORKER_ROOT


def _child(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MPLCONFIGDIR": str(tmp_path / "mpl"), "PYTHONPATH": str(WORKER_ROOT)}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_the_entry_point_installs_the_guard(tmp_path: Path) -> None:
    # Through main(), with a handler that tries what the macOS sandbox
    # refused in the first real case.
    job = {
        "protocol_version": 1,
        "job_id": "j_guard",
        "kind": "selftest",
        "project_dir": str(tmp_path / "Study.bcoaproj"),
        "log_path": str(tmp_path / "Study.bcoaproj/logs/j_guard.log"),
        "resources_dir": str(tmp_path),
        "payload": {},
    }
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(job))
    out = _child(
        f"""
        import os, socket, subprocess, sys
        import bcoa_worker.worker as worker
        from bcoa_worker.protocol import Result

        def attempt(action):
            try:
                action()
                return "allowed"
            except PermissionError:
                return "refused"

        def probe(job, channel):
            a, b = socket.socketpair()  # AF_UNIX: no network
            a.close(); b.close()
            channel.send(Result(job.job_id, {{
                "inet": attempt(lambda: socket.socket(socket.AF_INET)),
                "inet6": attempt(lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)),
                "default": attempt(socket.socket),
                "popen": attempt(lambda: subprocess.run(["true"])),
                "shell": attempt(lambda: subprocess.getoutput("hostname")),
                "system": attempt(lambda: os.system("true")),
            }}))

        worker._handlers = lambda: {{"selftest": probe}}
        sys.exit(worker.main(["run", "--job", {str(job_file)!r}]))
        """,
        tmp_path,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    result = next(json.loads(line) for line in out.stdout.splitlines() if '"result"' in line)
    assert result["payload"] == {
        "inet": "refused",
        "inet6": "refused",
        "default": "refused",
        "popen": "refused",
        # getoutput only catches CalledProcessError, so the refusal reaches
        # the caller: why nnU-Net is told its process count (environment.py).
        "shell": "refused",
        "system": "refused",
    }
    log = (tmp_path / "Study.bcoaproj/logs/j_guard.log").read_text()
    assert "[guard] refused socket.__new__" in log
    assert "[guard] refused subprocess.Popen true from <string>" in log


def test_each_refusal_is_logged_once(tmp_path: Path) -> None:
    out = _child(
        """
        import socket
        from bcoa_worker import guard
        guard.install()
        guard.install()  # a second call adds no second hook
        for _ in range(3):
            try:
                socket.socket(socket.AF_INET)
            except PermissionError:
                pass
        """,
        tmp_path,
    )
    assert out.returncode == 0, out.stderr
    assert out.stderr.count("[guard] refused") == 1


def test_urllib3_imports_without_a_socket(tmp_path: Path) -> None:
    # MOOSE imports requests for its downloader, and urllib3 binds an IPv6
    # socket at import to see whether the machine has IPv6: the
    # `network-bind` the sandbox refused. With the guard it never gets one.
    pytest.importorskip("urllib3")
    out = _child(
        """
        from bcoa_worker import guard
        guard.install()
        import urllib3.util.connection as connection
        print(connection.HAS_IPV6)
        """,
        tmp_path,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
    assert "[guard] refused socket.__new__" in out.stderr


def test_matplotlib_builds_its_font_cache_without_a_program(tmp_path: Path) -> None:
    # matplotlib lists the system's fonts with fc-list on Linux and
    # system_profiler on macOS; refused, it uses the fonts it ships.
    pytest.importorskip("matplotlib")
    out = _child(
        """
        from bcoa_worker import guard
        guard.install()
        from matplotlib import font_manager
        print(len(font_manager.fontManager.ttflist) > 0)
        """,
        tmp_path,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True"
    assert "socket" not in out.stderr
