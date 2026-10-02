"""Entry point: ``python -I -m bcoa_worker run --job <file>``.

Life of a worker: read the job, claim stdout for the protocol, say hello, keep
a heartbeat going, run the one job, say done, exit. Exit codes: 0 ok, 1 failed,
130 cancelled (SIGTERM), 2 the job file itself was unusable.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from bcoa_worker import __version__
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.environment import apply_runtime_environment, scratch_dir
from bcoa_worker.errors import JobFailure
from bcoa_worker.protocol import (
    Done,
    Error,
    Heartbeat,
    Hello,
    Job,
    ProtocolError,
    event_to_line,
    load_job,
)


class Cancelled(Exception):
    """SIGTERM arrived; the job unwinds through its `finally` blocks."""


JobHandler = Callable[[Job, ProtocolChannel], None]


def _handlers() -> dict[str, JobHandler]:
    # Imported lazily: a selftest must not pay for importing torch twice, and
    # an index job must not import torch at all.
    from bcoa_worker.jobs import export, segment, selftest, spike_s2

    return {
        "export": export.run,
        "selftest": selftest.run,
        "spike_s2": spike_s2.run,
        "segment": segment.run,
    }


def _versions() -> dict[str, str]:
    versions = {"worker": __version__, "python": platform.python_version()}
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - stdlib since 3.8
        return versions
    for package in ("moosez", "nnunetv2", "torch", "numpy", "SimpleITK", "pydicom", "XlsxWriter"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            continue
    return versions


def _start_heartbeat(job: Job, channel: ProtocolChannel) -> threading.Event:
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(job.heartbeat_seconds):
            channel.send(Heartbeat.now(job.job_id))

    threading.Thread(target=beat, name="heartbeat", daemon=True).start()
    return stop


def _raise_cancelled(signum: int, frame: FrameType | None) -> None:
    raise Cancelled


def run_job(job: Job, channel: ProtocolChannel, handlers: dict[str, JobHandler]) -> int:
    channel.send(Hello(versions=_versions()))
    handler = handlers.get(job.kind)
    if handler is None:
        channel.send(Error("unsupported_job", f"Job kind '{job.kind}' is not built yet", False))
        channel.send(Done(job.job_id, "failed"))
        return 1
    stop_heartbeat = _start_heartbeat(job, channel)
    try:
        handler(job, channel)
    except Cancelled:
        channel.send(Done(job.job_id, "cancelled"))
        return 130
    except JobFailure as failure:
        channel.send(Error(failure.code, str(failure), failure.recoverable))
        channel.send(Done(job.job_id, "failed"))
        return 1
    except Exception as exc:
        # The message names the exception type only. Exception text from
        # pydicom or SimpleITK can quote file paths, and folder names often
        # carry patient names; the full traceback goes to the log file.
        import traceback

        traceback.print_exc()
        message = f"Unexpected {type(exc).__name__}; see job log"
        channel.send(Error("internal_error", message, False))
        channel.send(Done(job.job_id, "failed"))
        return 1
    finally:
        stop_heartbeat.set()
        # Scratch holds MOOSE's intermediate images (about 300 MiB per model)
        # and matplotlib/torch caches; on success, failure and SIGTERM alike
        # nothing of it may stay in the project folder.
        shutil.rmtree(scratch_dir(job), ignore_errors=True)
    channel.send(Done(job.job_id, "ok"))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bcoa_worker")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        job = load_job(args.job)
    except (OSError, ValueError, ProtocolError) as exc:
        # Before the channel exists stdout is still the protocol; answer there.
        failure = Error("bad_job", f"Unreadable job file ({type(exc).__name__})", False)
        print(event_to_line(failure))
        return 2

    channel = ProtocolChannel.take_over_stdout(job.log_path)
    apply_runtime_environment(job)
    signal.signal(signal.SIGTERM, _raise_cancelled)
    return run_job(job, channel, _handlers())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
