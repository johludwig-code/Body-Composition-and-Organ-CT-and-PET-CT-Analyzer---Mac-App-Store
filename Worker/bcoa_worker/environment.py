"""Environment the worker needs inside a read-only, sandboxed bundle.

The app sets these when it launches the worker. They are set again here,
before MOOSE, torch or matplotlib is imported, so that a worker started by hand
for debugging behaves the same as one started by the app.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from bcoa_worker.protocol import Job


def scratch_dir(job: Job) -> Path:
    """Per-job scratch inside the project folder.

    Not the system temp directory: inside the sandbox that is the app
    container, which the user cannot see or clean, and MOOSE leaves about
    300 MiB per model there (measured in BOCARTA-MOOSE, docs/moose-phase0.md).
    """
    return job.project_dir / "work" / ".scratch" / job.job_id


def runtime_environment(job: Job) -> dict[str, str]:
    scratch = scratch_dir(job)
    return {
        # Some ops have no MPS kernel yet; without the fallback nnU-Net stops
        # with NotImplementedError instead of running that op on the CPU.
        "PYTORCH_ENABLE_MPS_FALLBACK": "1",
        # matplotlib (imported by MOOSE) writes a font cache on first import
        # and would try the read-only bundle or ~/.matplotlib.
        "MPLCONFIGDIR": str(scratch / "mpl"),
        "TMPDIR": str(scratch / "tmp"),
        "TORCH_HOME": str(scratch / "torch"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
        # The signed bundle must stay byte-identical; .pyc are precompiled.
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        # huggingface-hub is installed as a dependency of timm/nnU-Net. It has
        # no reason to run, and if it ever did it must not try the network.
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        # joblib (through scikit-learn) creates a POSIX semaphore at import to
        # see whether it may use processes. The sandbox refuses it, which
        # joblib survives with a warning and the kernel logs as a violation
        # (ADR 0015); this tells it to stay serial without trying.
        "JOBLIB_MULTIPROCESSING": "0",
    }


def apply_runtime_environment(job: Job) -> None:
    for key, value in runtime_environment(job).items():
        os.environ[key] = value
    # The environment variable only takes effect at interpreter start; a
    # worker started without it (by hand, for debugging) must not write .pyc
    # files into the signed bundle either.
    sys.dont_write_bytecode = True
    for key in ("MPLCONFIGDIR", "TMPDIR", "TORCH_HOME", "XDG_CACHE_HOME"):
        Path(os.environ[key]).mkdir(parents=True, exist_ok=True)
