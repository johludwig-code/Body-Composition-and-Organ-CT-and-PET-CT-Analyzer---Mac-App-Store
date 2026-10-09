from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

WORKER_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = WORKER_ROOT.parent / "Protocol"

if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))


# The corpus and its scans are made once per session, because a build takes
# 3 to 4 s and a scan 2 to 3 s, and several modules read them. They are
# imported here only when a test asks for them, so that a run of other tests
# never imports pydicom. No test may change them; a test that changes a tree
# builds its own.


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    import dicom_factory

    built = dicom_factory.build_corpus(tmp_path_factory.mktemp("index-corpus") / "corpus")
    try:
        yield built
    finally:
        # Mode 000 entries would keep pytest from removing the folder.
        built.unlock()


@pytest.fixture(scope="session")
def scanned_corpus(corpus: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The corpus indexed once in this process, with the fixtures' key."""
    from index_jobs import scan_in_process

    return scan_in_process(tmp_path_factory.mktemp("scanned-corpus"), corpus.sources)


@pytest.fixture(scope="session")
def worker_scanned_corpus(corpus: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The corpus indexed by a worker of its own, guard installed and every
    socket and program start recorded."""
    from index_jobs import scan_in_worker

    return scan_in_worker(tmp_path_factory.mktemp("worker-scanned-corpus"), corpus.sources)
