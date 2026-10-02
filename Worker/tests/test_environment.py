"""The worker's environment, which the app sets and the worker sets again."""

from __future__ import annotations

import re
from pathlib import Path

from bcoa_worker.environment import runtime_environment
from bcoa_worker.protocol import Job, parse_job

SWIFT = Path(__file__).resolve().parents[2] / "Packages/BCOAKit/Sources/BCOAKit/BundleLayout.swift"


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


def test_the_app_sets_the_same_variables_as_the_worker(tmp_path: Path) -> None:
    # Both sides set the environment; a variable added on one side only works
    # for a worker started by hand or only for one started by the app.
    source = SWIFT.read_text(encoding="utf-8")
    body = source.split("func workerEnvironment", 1)[1].split("return environment", 1)[0]
    swift = set(re.findall(r'"([A-Z][A-Z0-9_]+)":', body))
    # LANG only matters for how the app reads the worker's output.
    assert swift - {"LANG"} == set(runtime_environment(_job(tmp_path)))
