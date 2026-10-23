"""Robustness: what the worker does with payloads and files the app should
never send, and with a disk that fails under it.

Every job must end with a `done` event, a failure must come with an error
code, nothing may be written outside the project folder, and no message may
carry the project's absolute path (folder names often carry patient names,
plan §13). The payloads are deterministic mutations, so a failure here can
be replayed.
"""

from __future__ import annotations

import io
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import export_project
from bcoa_worker import worker
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.jobs import export as export_job
from bcoa_worker.protocol import Job

# selftest and spike_s2 ignore their payload; segment is left out because it
# imports torch and MOOSE, which the Linux test environment does not have.
KINDS = ("convert", "metrics", "viewer_cache", "export", "index")

HOSTILE_STRINGS = [
    "",
    "../outside",
    "../../outside/x.nii.gz",
    "/etc/passwd",
    "a" * 5000,
    "nul\x00byte",
    "ümlaut ✓ 🫀",
    '=HYPERLINK("http://example.invalid")',
    "work/../../outside",
    ".",
]
HOSTILE_VALUES: list[object] = [
    None,
    0,
    -1,
    2**63,
    1.5,
    float("inf"),
    True,
    [],
    {},
    ["a"],
    {"a": "b"},
    *HOSTILE_STRINGS,
]
KEYS = [
    "series_key",
    "ct",
    "labelmaps",
    "labels_dir",
    "cache_dir",
    "run_id",
    "stem",
    "options",
    "mode",
    "sources",
    "files",
    "devices",
    "flags",
    "scan_sources",
    "catalog_id",
    "models",
    "link_key",
    "part_refs",
]


def _payloads() -> list[dict[str, object]]:
    rng = random.Random(20261023)  # noqa: S311 - a replayable fuzz, not a secret
    fixed: list[dict[str, object]] = [
        {},
        {"series_key": None},
        {
            "series_key": "../escape",
            "ct": "../../outside.nii.gz",
            "labelmaps": {"m": "../../x"},
            "cache_dir": "../../evil",
            "run_id": "R1",
            "stem": "../evil",
        },
        {"series_key": "s", "ct": "missing.nii.gz", "labelmaps": "not a dict"},
        {"run_id": 5, "stem": "ok", "options": "not a dict"},
        {"run_id": export_project.RUN, "stem": "ok", "options": {"format": "pdf"}},
        {"mode": "scan", "sources": "x"},
        {"mode": "nonsense"},
    ]
    for _ in range(60):
        payload = {}
        for key in rng.sample(KEYS, rng.randint(1, 6)):
            payload[key] = rng.choice(HOSTILE_VALUES)
        fixed.append(payload)
    return fixed


def _tree(folder: Path) -> set[str]:
    return {str(p.relative_to(folder)) for p in folder.rglob("*")}


def _run(kind: str, project: Path, payload: dict[str, object]) -> list[dict[str, object]]:
    buffer = io.StringIO()
    job = Job(f"j_{kind}", kind, project, project / "logs" / f"j_{kind}.log", project, payload,
              heartbeat_seconds=60)  # fmt: skip
    worker.run_job(job, ProtocolChannel(buffer), worker._handlers())
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("robust")
    project = root / "project"
    outside = root / "outside"
    project.mkdir()
    outside.mkdir()
    export_project.create(project)
    (project / "logs").mkdir(exist_ok=True)
    return project, outside


@pytest.mark.parametrize("kind", KINDS)
def test_every_job_ends_cleanly_whatever_its_payload(kind: str, sandbox) -> None:
    project, outside = sandbox
    before_outside = _tree(outside)
    before_root = _tree(project.parent) - _tree(project) - _tree(outside)
    for number, payload in enumerate(_payloads()):
        events = _run(kind, project, payload)
        assert events, (kind, number)
        last = events[-1]
        assert last["type"] == "done", (kind, number, payload, events[-3:])
        if last["status"] == "failed":
            errors = [e for e in events if e["type"] == "error"]
            assert errors, (kind, number)
            assert errors[-1]["code"], (kind, number)
        for event in events:
            text = json.dumps(event)
            assert str(project.parent) not in text, (kind, number, event)
    assert _tree(outside) == before_outside
    # Nothing next to the project either (the folder holds project/ and outside/).
    after_root = _tree(project.parent) - _tree(project) - _tree(outside)
    assert after_root == before_root


def test_a_symlink_out_of_the_project_is_refused(sandbox) -> None:
    project, outside = sandbox
    (outside / "secret.nii.gz").write_bytes(b"not for the worker")
    link = project / "work" / "linked"
    link.parent.mkdir(exist_ok=True)
    if not link.exists():
        link.symlink_to(outside)
    events = _run(
        "viewer_cache",
        project,
        {"series_key": "s", "ct": "work/linked/secret.nii.gz", "cache_dir": "work/linked/cache"},
    )
    assert events[-1] == {"type": "done", "job_id": "j_viewer_cache", "status": "failed"}
    assert not (outside / "cache").exists()


# -- a disk that fails under the export --------------------------------------------


def _export(project: Path, options: dict[str, object]) -> list[dict[str, object]]:
    return _run(
        "export", project, {"run_id": export_project.RUN, "stem": "cohort", "options": options}
    )


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"format": "csv"},
        {"case_reports": True},
        {"reproducibility_package": True},
    ],
)
def test_a_full_disk_leaves_the_exports_as_they_were(tmp_path: Path, monkeypatch, options) -> None:
    export_project.create(tmp_path)
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "cohort.xlsx").write_bytes(b"the export of yesterday")
    real_open = io.open
    written = {"bytes": 0}

    # After 4 kB into exports/ the disk is full, for every way of opening.
    class Full(io.FileIO):
        def write(self, data):  # type: ignore[override]
            written["bytes"] += len(data)
            if written["bytes"] > 4096:
                raise OSError(28, "No space left on device")
            return super().write(data)

    def guarded_open(
        file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, *rest, **kw
    ):
        if "exports" in str(file) and any(m in mode for m in "wax"):
            raw = Full(file, "w" if "w" in mode else mode.replace("b", "").replace("t", ""))
            buffered = io.BufferedWriter(raw)
            if "b" in mode:
                return buffered
            return io.TextIOWrapper(
                buffered, encoding=encoding or "utf-8", errors=errors, newline=newline
            )
        return real_open(file, mode, buffering, encoding, errors, newline, *rest, **kw)

    monkeypatch.setattr("builtins.open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    events = _export(tmp_path, options)
    monkeypatch.undo()
    assert events[-1]["status"] == "failed", events[-3:]
    error = next(e for e in events if e["type"] == "error")
    assert error["code"] == "disk_full", error
    # Yesterday's export untouched; nothing of today's, not even hidden.
    assert sorted(p.name for p in exports.iterdir()) == ["cohort.xlsx"]
    assert (exports / "cohort.xlsx").read_bytes() == b"the export of yesterday"


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("hdiutil"), reason="macOS disk images"
)
def test_a_really_full_disk_image(tmp_path: Path) -> None:
    image = tmp_path / "tiny.dmg"
    mount = tmp_path / "mount"
    mount.mkdir()
    subprocess.run(
        ["hdiutil", "create", "-size", "3m", "-fs", "HFS+", "-volname", "bcoafull", "-layout", "NONE", str(image)],
        check=True, capture_output=True,
    )  # fmt: skip
    subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), str(image)],
        check=True,
        capture_output=True,
    )
    try:
        project = mount / "project"
        project.mkdir()
        export_project.create(project)
        # Fill the volume but for a few kilobytes.
        free = shutil.disk_usage(mount).free
        with open(mount / "filler", "wb") as filler:
            filler.write(b"\0" * max(free - 16 * 1024, 0))
        events = _export(project, {"case_reports": True, "format": "csv"})
        assert events[-1]["status"] == "failed", events[-3:]
        error = next(e for e in events if e["type"] == "error")
        assert error["code"] == "disk_full", error
        assert not [p for p in (project / "exports").iterdir()]
        # The same export goes through once there is space again.
        (mount / "filler").unlink()
        assert _export(project, {"case_reports": True, "format": "csv"})[-1]["status"] == "ok"
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)


def test_an_export_folder_that_cannot_be_written_says_so(tmp_path: Path) -> None:
    export_project.create(tmp_path)
    exports = tmp_path / "exports"
    exports.mkdir()
    exports.chmod(0o500)
    try:
        events = _export(tmp_path, {})
    finally:
        exports.chmod(0o700)
    assert events[-1]["status"] == "failed"
    error = next(e for e in events if e["type"] == "error")
    assert error["code"] == "export_not_writable", error
    assert str(tmp_path) not in error["message"]


def test_two_exports_of_one_run_do_not_disturb_each_other(tmp_path: Path) -> None:
    export_project.create(tmp_path)
    first = _export(tmp_path, {"case_reports": True})
    second = _run(
        "export",
        tmp_path,
        {"run_id": export_project.RUN, "stem": "again", "options": {"case_reports": True}},
    )
    assert first[-1]["status"] == second[-1]["status"] == "ok"
    names = sorted(
        p.relative_to(tmp_path / "exports").as_posix()
        for p in (tmp_path / "exports").rglob("*.json")
    )
    assert len([n for n in names if n.startswith("cohort_reports/")]) == 4
    assert len([n for n in names if n.startswith("again_reports/")]) == 4


def test_export_job_module_is_the_one_under_test() -> None:
    # Guards the tests above against testing a stale copy of the worker.
    assert (
        Path(export_job.__file__).parent.parent.parent.name == "Worker"
        or "site-packages" not in export_job.__file__
    )


# -- the app goes away under a running job -------------------------------------------


def test_a_worker_whose_app_is_gone_leaves_at_once(tmp_path: Path) -> None:
    from bcoa_worker.environment import scratch_dir

    job = Job("j_orphan", "segment", tmp_path, tmp_path / "log", tmp_path, {})
    scratch = scratch_dir(job)
    scratch.mkdir(parents=True)
    (scratch / "intermediate.nii.gz").write_bytes(b"300 MiB of MOOSE")
    parents = iter([100, 100, 1])
    codes: list[int] = []
    thread = worker._watch_parent(
        job, 100, interval=0.01, getppid=lambda: next(parents, 1), exit=codes.append
    )
    thread.join(timeout=5)
    assert codes == [worker.EXIT_ORPHANED]
    assert not scratch.exists()


def test_a_real_orphan_ends_within_seconds(tmp_path: Path) -> None:
    # A parent starts a worker-like child that only watches, then dies at
    # once without a word, as a crashed app does.
    child = f"""
import sys, time
sys.path.insert(0, {str(Path(worker.__file__).parent.parent)!r})
from pathlib import Path
from bcoa_worker import worker
from bcoa_worker.protocol import Job
import os
job = Job("j_child", "segment", Path({str(tmp_path)!r}), Path({str(tmp_path)!r}) / "log", Path({str(tmp_path)!r}), {{}})
worker._watch_parent(job, os.getppid(), interval=0.1)
time.sleep(60)
"""
    parent = f"""
import subprocess, sys, os
proc = subprocess.Popen([sys.executable, "-c", {child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(proc.pid, flush=True)
os._exit(0)
"""
    out = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=30)
    pid = int(out.stdout.strip())
    deadline = time.time() + 10
    alive = True
    while alive and time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            alive = False
    if alive:
        os.kill(pid, 9)
    assert not alive, "the child outlived its parent by 10 s"
