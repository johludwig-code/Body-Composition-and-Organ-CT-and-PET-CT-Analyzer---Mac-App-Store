#!/usr/bin/env python3
"""Crash test of the pipeline in the real, sandboxed app (Debug build).

    python3 Scripts/crash_test.py --project <folder>.bcoaproj [--models clin_ct_organs]

Starts a batch with `-BCOAAutoAnalyze`, kills the first segmentation's worker
20 s into it (the batch must go on and that stage fail), kills the app itself
during the next segmentation (no worker may outlive it by more than 5 s),
relaunches with `-BCOARetryFailed` and waits until every series of the new
run has its viewer cache. Then it checks the run in the database: no job left
running, results for every series and model with every label of the model,
no partial folder and no scratch left. Exit code 0 only when all of that
holds.

Run it on a project of public data with at least two selected CT series
(the ACRIN case on the T7 has three); it takes about two minutes per series
and model. It reads `project.sqlite` read-only and changes nothing itself:
the app writes the run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "build/Build/Products/Debug/BCOAnalyzer.app"
T0 = time.time()


def say(text: str) -> None:
    print(f"[{time.time() - T0:7.1f} s] {text}", flush=True)


def workers() -> list[tuple[int, int, str, str]]:
    """(pid, parent pid, job kind, run id) of every worker process."""
    out = subprocess.run(["ps", "-axo", "pid,ppid,command"], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3 and "bcoa_worker run --job" in parts[2]:
            job = Path(parts[2].split("--job", 1)[1].strip())
            try:
                spec = json.loads(job.read_text())
                kind = spec["kind"]
                # The run is in the labels' folder (ADR 0031 decision 11).
                labels = str(spec["payload"].get("labels_dir", ""))
                match = re.search(r"runs/(R[0-9T]+)/", labels)
                run = match.group(1) if match else ""
            except (OSError, ValueError, KeyError, AttributeError):
                kind, run = "?", ""
            found.append((int(parts[0]), int(parts[1]), kind, run))
    return found


def app_pids() -> list[int]:
    out = subprocess.run(
        ["pgrep", "-f", f"{APP}/Contents/MacOS/BCOAnalyzer"], capture_output=True, text=True
    ).stdout
    return [int(p) for p in out.split()]


def wait_for(predicate, timeout: float, step: float = 1.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--models", default="clin_ct_organs")
    args = parser.parse_args()
    project: Path = args.project
    if project.suffix != ".bcoaproj" or not (project / "project.sqlite").is_file():
        print("not a project folder", file=sys.stderr)
        return 2

    def rows(sql: str, *params: object) -> list[tuple]:
        db = sqlite3.connect(f"file:{project / 'project.sqlite'}?mode=ro", uri=True, timeout=10)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    selected = rows("SELECT series_key FROM series WHERE selected = 1 AND modality = 'CT'")
    if len(selected) < 2:
        print("the project needs at least two selected CT series", file=sys.stderr)
        return 2
    if app_pids() or workers():
        print("quit the app first", file=sys.stderr)
        return 2
    problems: list[str] = []

    def launch(*flags: str) -> None:
        subprocess.run(["open", "-n", str(APP), "--args", *flags], check=True)
        say(f"app started: {wait_for(app_pids, 30)}")

    launch("-BCOAAutoAnalyze", args.models)
    first = wait_for(lambda: [w for w in workers() if w[2] == "segment"], 900)
    if not first:
        say("no segmentation started: have the series results for these models already?")
        return 1
    run = first[0][3]
    say(f"run {run}; segment worker {first[0][0]}")
    time.sleep(20)
    os.kill(first[0][0], signal.SIGKILL)
    say("killed the first segmentation's worker")

    second = wait_for(
        lambda: [w for w in workers() if w[2] == "segment" and w[0] != first[0][0]], 900
    )
    if not second:
        problems.append("the batch did not go on after a worker was killed")
    else:
        time.sleep(20)
        for pid in app_pids():
            os.kill(pid, signal.SIGKILL)
        say("killed the app")
        if not wait_for(lambda: not workers(), 5, step=0.25):
            problems.append(f"workers outlived the app by 5 s: {workers()}")
            for pid, *_ in workers():
                os.kill(pid, signal.SIGKILL)
        else:
            say("no worker outlived the app")

    launch("-BCOARetryFailed", "YES")
    series = {r[0] for r in selected}

    def finished():
        if workers():
            return None
        latest: dict[str, dict[str, str]] = {}
        for key, kind, status in rows(
            "SELECT series_key, kind, status FROM jobs WHERE run_id = ? ORDER BY queued_at", run
        ):
            latest.setdefault(key, {})[kind] = status
        done = all(latest.get(key, {}).get("viewer_cache") == "done" for key in series)
        return latest if done else None

    if wait_for(finished, 3600, step=5) is None:
        problems.append("the run did not finish within an hour")
    subprocess.run(["osascript", "-e", 'tell application id "de.ludwig.bcoanalyzer" to quit'])
    wait_for(lambda: not app_pids(), 30)
    if workers():
        problems.append(f"workers after quit: {workers()}")

    running = rows(
        "SELECT job_id FROM jobs WHERE run_id = ? AND status IN ('running', 'queued')", run
    )
    if running:
        problems.append(f"jobs left running or queued: {running}")
    models = {
        m["name"]: len(m["labels"])
        for m in json.loads(rows("SELECT models_json FROM runs WHERE run_id = ?", run)[0][0])
    }
    grouped = rows(
        "SELECT series_key, model, count(*) FROM results WHERE run_id = ? "
        "GROUP BY series_key, model",
        run,
    )
    counts = {(k, m): n for k, m, n in grouped}
    for key in sorted(series):
        for model, labels in models.items():
            if counts.get((key, model)) != labels:
                problems.append(
                    f"{key} {model}: {counts.get((key, model))} results, {labels} labels"
                )
    leftovers = [str(p.relative_to(project)) for p in project.rglob("*.partial")]
    leftovers += [str(p.relative_to(project)) for p in (project / "work" / ".scratch").glob("*")]
    if leftovers:
        problems.append(f"left behind: {leftovers}")
    jobs = rows(
        "SELECT series_key, kind, status, error_code FROM jobs WHERE run_id = ? ORDER BY queued_at",
        run,
    )
    say(f"jobs: {jobs}")
    for problem in problems:
        say(f"FAIL {problem}")
    say("PASS" if not problems else f"{len(problems)} problems")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
