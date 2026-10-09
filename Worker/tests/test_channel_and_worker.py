from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from bcoa_worker import worker
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.errors import JobFailure
from bcoa_worker.protocol import Job, Log, parse_event
from bcoa_worker.worker import Cancelled, run_job
from conftest import WORKER_ROOT


def _job(tmp_path: Path, kind: str = "selftest") -> Job:
    return Job("j_test", kind, tmp_path, tmp_path / "logs" / "j_test.log", tmp_path, {}, 0.05)  # type: ignore[arg-type]


def _events(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_noise_on_fd1_and_fd2_lands_in_the_log_not_the_protocol(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "job.log"
    script = f"""
import os, sys
sys.path.insert(0, {str(WORKER_ROOT)!r})
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.protocol import Log
channel = ProtocolChannel.take_over_stdout(__import__('pathlib').Path({str(log)!r}))
print('MOOSE banner')
os.write(1, b'C extension on fd 1\\n')
os.write(2, b'warning on fd 2\\n')
channel.send(Log('info', 'only this on the protocol'))
"""
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    lines = out.stdout.splitlines()
    assert len(lines) == 1
    assert parse_event(lines[0]) == Log("info", "only this on the protocol")
    noise = log.read_text()
    assert "MOOSE banner" in noise and "fd 1" in noise and "fd 2" in noise


def test_successful_job_says_hello_then_done(tmp_path: Path) -> None:
    buffer = io.StringIO()
    code = run_job(_job(tmp_path), ProtocolChannel(buffer), {"selftest": lambda job, ch: None})
    events = _events(buffer)
    assert code == 0
    assert events[0]["type"] == "hello" and events[0]["protocol_version"] == 1
    assert events[-1] == {"type": "done", "job_id": "j_test", "status": "ok"}


def test_job_failure_carries_its_code(tmp_path: Path) -> None:
    def fail(job: Job, channel: ProtocolChannel) -> None:
        raise JobFailure("model_not_in_bundle", "Model not found in app bundle: x")

    buffer = io.StringIO()
    assert run_job(_job(tmp_path), ProtocolChannel(buffer), {"selftest": fail}) == 1
    error, done = _events(buffer)[-2:]
    assert error["code"] == "model_not_in_bundle"
    assert done["status"] == "failed"


def test_unexpected_exception_does_not_leak_its_message(tmp_path: Path) -> None:
    def crash(job: Job, channel: ProtocolChannel) -> None:
        raise OSError("/Volumes/Data/Mustermann_Max/CT/IM0001: Permission denied")

    buffer = io.StringIO()
    assert run_job(_job(tmp_path), ProtocolChannel(buffer), {"selftest": crash}) == 1
    assert "Mustermann" not in buffer.getvalue()


def test_cancel_exits_130(tmp_path: Path) -> None:
    def cancelled(job: Job, channel: ProtocolChannel) -> None:
        raise Cancelled

    buffer = io.StringIO()
    assert run_job(_job(tmp_path), ProtocolChannel(buffer), {"selftest": cancelled}) == 130
    assert _events(buffer)[-1]["status"] == "cancelled"


def test_heartbeats_while_a_job_runs(tmp_path: Path) -> None:
    import time

    buffer = io.StringIO()
    run_job(_job(tmp_path), ProtocolChannel(buffer), {"selftest": lambda j, c: time.sleep(0.3)})
    assert sum(e["type"] == "heartbeat" for e in _events(buffer)) >= 2


def test_unknown_job_kind_fails_cleanly(tmp_path: Path) -> None:
    buffer = io.StringIO()
    assert run_job(_job(tmp_path, "export"), ProtocolChannel(buffer), {}) == 1
    assert _events(buffer)[-2]["code"] == "unsupported_job"


def test_spike_s2_end_to_end_through_the_real_entry_point(tmp_path: Path) -> None:
    source = tmp_path / "Source"
    source.mkdir()
    (source / "IM0001").write_bytes(b"\0" * 128 + b"DICM" + b"\0" * 16)
    project = tmp_path / "Study.bcoaproj"
    job = {
        "protocol_version": 1,
        "job_id": "j_spike_s2",
        "kind": "spike_s2",
        "project_dir": str(project),
        "log_path": str(project / "logs" / "j.log"),
        "resources_dir": str(tmp_path),
        "payload": {"source_dir": str(source)},
    }
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(job))
    # -I ignores PYTHONPATH and the working directory, which is why the build
    # installs bcoa_worker into the bundled site-packages. Here the bootstrap
    # puts the source tree on the path the way site-packages would be.
    bootstrap = (
        f"import sys, runpy; sys.path.insert(0, {str(WORKER_ROOT)!r}); "
        "sys.argv[0] = 'bcoa_worker'; runpy.run_module('bcoa_worker', run_name='__main__')"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", bootstrap, "run", "--job", str(job_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    events = [parse_event(line) for line in out.stdout.splitlines()]
    assert out.returncode == 0, out.stdout
    result = next(e for e in events if e.type == "result")
    assert result.payload["read_source"] == {"ok": True, "files": 1, "dicm_preamble": True}
    assert result.payload["write_project"] == {"ok": True}
    assert str(source) not in out.stdout


def test_scratch_is_removed_after_the_job(tmp_path: Path) -> None:
    from bcoa_worker.environment import scratch_dir

    def leave_files(job: Job, channel: ProtocolChannel) -> None:
        scratch_dir(job).mkdir(parents=True)
        (scratch_dir(job) / "intermediate.nii.gz").write_bytes(b"x")
        raise JobFailure("x", "y")

    job = _job(tmp_path)
    run_job(job, ProtocolChannel(io.StringIO()), {"selftest": leave_files})
    assert not scratch_dir(job).exists()


def test_each_job_imports_only_its_own_module(tmp_path: Path) -> None:
    # bcoa_worker.index blocks `requests` for the whole process, and real
    # moosez imports it at module level: a segment or selftest job that
    # imported the index package, as one eager import line for every kind
    # would, fails at `import moosez`. A stand-in `requests` is installed so
    # that a block shows as an ImportError.
    site = tmp_path / "site"
    (site / "requests").mkdir(parents=True)
    (site / "requests" / "__init__.py").write_text("LOADED = True\n")
    script = """
import importlib, sys
from bcoa_worker import worker
handlers = worker._handlers()
assert set(handlers) == set(worker._JOB_MODULES), handlers
assert not [m for m in sys.modules if m.startswith("bcoa_worker.jobs")], "imported eagerly"
assert not [m for m in sys.modules if m.startswith("bcoa_worker.index")], "imported eagerly"
for kind, module in worker._JOB_MODULES.items():
    if kind != "index":
        importlib.import_module(module)
assert "bcoa_worker.index" not in sys.modules
import requests
print(requests.LOADED)
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(site), str(WORKER_ROOT)])}
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=False
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True"


def test_a_job_module_that_fails_to_import_is_reported(tmp_path: Path) -> None:
    # Imported inside run_job, a broken module ends the job with an error
    # event instead of a worker that dies before saying anything.
    buffer = io.StringIO()
    handler = worker._lazy("bcoa_worker.jobs.no_such_job")
    assert run_job(_job(tmp_path), ProtocolChannel(buffer), {"selftest": handler}) == 1
    error, done = _events(buffer)[-2:]
    assert error["code"] == "internal_error"
    assert done["status"] == "failed"
