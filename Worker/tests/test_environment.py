"""The worker's environment, which the app sets and the worker sets again."""

from __future__ import annotations

import re
from pathlib import Path

from bcoa_worker.environment import runtime_environment
from bcoa_worker.protocol import Job, parse_job

ROOT = Path(__file__).resolve().parents[2]
SWIFT = ROOT / "Packages/BCOAKit/Sources/BCOAKit/BundleLayout.swift"
# The CI's stand-in for the app, which starts the worker in the sandbox.
PROBE = ROOT / "Scripts/ci/WorkerProbe.swift"
KEY = r'"([A-Za-z][A-Za-z0-9_]+)":'


def _job(tmp_path: Path) -> Job:
    return parse_job(
        {
            "protocol_version": 1,
            "job_id": "j_env",
            "kind": "selftest",
            "project_dir": str(tmp_path),
            "log_path": str(tmp_path / "logs/j_env.log"),
            "resources_dir": str(tmp_path / "Resources"),
            "heartbeat_seconds": 30,
            "payload": {},
        }
    )


def test_joblib_is_told_not_to_probe_for_semaphores(tmp_path: Path) -> None:
    # The sandbox refuses the semaphore joblib creates at import (ADR 0015).
    assert runtime_environment(_job(tmp_path))["JOBLIB_MULTIPROCESSING"] == "0"


def test_nnunet_is_told_its_process_count(tmp_path: Path) -> None:
    # Without it nnU-Net runs `hostname` in a shell at import (ADR 0016).
    assert runtime_environment(_job(tmp_path))["nnUNet_n_proc_DA"] == "12"


def test_the_app_sets_the_same_variables_as_the_worker(tmp_path: Path) -> None:
    # Both sides set the environment; a variable added on one side only works
    # for a worker started by hand or only for one started by the app.
    source = SWIFT.read_text(encoding="utf-8")
    body = source.split("func workerEnvironment", 1)[1].split("return environment", 1)[0]
    swift = set(re.findall(KEY, body))
    # LANG only matters for how the app reads the worker's output.
    assert swift - {"LANG"} == set(runtime_environment(_job(tmp_path)))


def test_the_ci_probe_sets_what_the_app_sets(tmp_path: Path) -> None:
    # What the sandbox report finds is only evidence for the app if the
    # probe starts the worker the way the app does.
    body = PROBE.read_text(encoding="utf-8").split("var environment", 1)[1].split("]\n", 1)[0]
    assert set(re.findall(KEY, body)) - {"LANG"} == set(runtime_environment(_job(tmp_path)))
