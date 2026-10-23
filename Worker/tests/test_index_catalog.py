"""The index catalog is a cache the worker owns (ADR 0020): created on first
use, never migrated, built again when it is of another format or damaged,
and never deleted while another process holds it."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from contextlib import closing
from pathlib import Path

import pytest

from bcoa_worker.index.catalog import CATALOG_FORMAT, open_catalog
from conftest import WORKER_ROOT

TABLES = {
    "bad_dirs",
    "files",
    "frames",
    "dicomdir_entries",
    "cat_instances",
    "catalog_meta",
    "scans",
    "pending_identifiers",
    "cat_studies",
    "cat_series",
    "cat_checks",
    "cat_pairs",
    "cat_id_candidates",
}


def _meta(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as db:
        return dict(db.execute("SELECT key, value FROM catalog_meta"))


def test_a_new_catalog(tmp_path: Path) -> None:
    path = tmp_path / "index" / "catalog.sqlite"
    with closing(open_catalog(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (CATALOG_FORMAT,)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert db.isolation_level is None
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == TABLES
    meta = _meta(path)
    assert {k: v for k, v in meta.items() if k != "catalog_id"} == {
        "format": "1",
        "generation": "0",
        "complete": "0",
        "next_part_ref": "1",
    }
    assert len(meta["catalog_id"]) == 32
    int(meta["catalog_id"], 16)


def test_files_keep_what_a_regroup_needs_without_reading_again(tmp_path: Path) -> None:
    # A regroup works from catalog rows alone, and an unchanged file is never
    # read again, so every tag a check needs has a column. IssuerOfPatientID
    # (check.issuer_conflict) is kept only as an HMAC link, never in plain.
    with closing(open_catalog(tmp_path / "catalog.sqlite")) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(files)")}
    assert {"pid_link", "issuer_link"} <= columns
    assert not {c for c in columns if "issuer" in c} - {"issuer_link"}


def test_reopening_keeps_the_catalog(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    with closing(open_catalog(path)) as db:
        db.execute("INSERT INTO scans (source_id, state) VALUES (1, 'reading')")
    before = _meta(path)
    with closing(open_catalog(path)) as db:
        assert db.execute("SELECT state FROM scans").fetchall() == [("reading",)]
    assert _meta(path) == before


def _rebuilt(path: Path, old_id: str | None) -> None:
    with closing(open_catalog(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (CATALOG_FORMAT,)
        assert db.execute("SELECT count(*) FROM scans").fetchone() == (0,)
    meta = _meta(path)
    assert meta["generation"] == "0"
    assert meta["catalog_id"] != old_id


def test_a_catalog_of_another_format_is_built_again(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    with closing(open_catalog(path)) as db:
        db.execute("INSERT INTO scans (source_id, state) VALUES (1, 'complete')")
        db.execute("PRAGMA user_version = 2")
    _rebuilt(path, _meta(path)["catalog_id"])


def test_a_catalog_whose_meta_names_another_format_is_built_again(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    with closing(open_catalog(path)) as db:
        db.execute("INSERT INTO scans (source_id, state) VALUES (1, 'complete')")
        db.execute("UPDATE catalog_meta SET value = '2' WHERE key = 'format'")
    _rebuilt(path, _meta(path)["catalog_id"])


def test_a_foreign_database_at_the_path_is_built_again(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE something (x)")
        db.execute("PRAGMA user_version = 1")
        db.commit()
    _rebuilt(path, None)


def test_a_damaged_file_is_built_again(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    path.write_bytes(b"not a database, " * 512)
    _rebuilt(path, None)


def test_a_damaged_page_is_found_at_open(tmp_path: Path) -> None:
    # A damaged page found mid-job would fail the job, and the next one, and
    # every later scan; quick_check at open turns it into a rebuild instead.
    path = tmp_path / "catalog.sqlite"
    with closing(open_catalog(path)) as db:
        db.execute("BEGIN")
        db.executemany(
            "INSERT INTO files (source_id, rel_path, size, mtime_ns, reader_version, kind, "
            "series_description) VALUES (1, ?, 1, 1, 1, 'image', ?)",
            [(f"d/{n:05d}".encode(), "x" * 200) for n in range(2000)],
        )
        db.execute("COMMIT")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        root = db.execute("SELECT rootpage FROM sqlite_master WHERE name = 'files'").fetchone()[0]
    old_id = _meta(path)["catalog_id"]
    data = bytearray(path.read_bytes())
    start = (root - 1) * page_size
    data[start : start + page_size] = b"\xff" * page_size
    path.write_bytes(bytes(data))
    _rebuilt(path, old_id)


def test_an_empty_file_is_created(tmp_path: Path) -> None:
    # What a catalog interrupted while being created leaves: its creation is
    # one transaction, so nothing of it.
    path = tmp_path / "catalog.sqlite"
    path.write_bytes(b"")
    with closing(open_catalog(path)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == TABLES


def test_a_catalog_left_in_wal_mode_goes_back_to_delete(tmp_path: Path) -> None:
    # The catalog may live on a network volume, where WAL's shared memory is
    # not reliable.
    path = tmp_path / "catalog.sqlite"
    open_catalog(path).close()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    before = _meta(path)
    with closing(open_catalog(path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert _meta(path) == before
    assert not Path(f"{path}-wal").exists()


def test_a_catalog_another_process_holds_is_not_deleted(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    open_catalog(path).close()
    before = _meta(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            open_catalog(path, timeout=0.1)
        holder.execute("ROLLBACK")
    assert _meta(path) == before


def _child(code: str, tmp_path: Path, *path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(p) for p in (*path, WORKER_ROOT))}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )


@pytest.fixture
def installed_requests(tmp_path: Path) -> Path:
    # A stand-in for an installed requests, so the block is seen to hold
    # even where the real one would import.
    site = tmp_path / "site"
    (site / "requests").mkdir(parents=True)
    (site / "requests" / "__init__.py").write_text("LOADED = True\n")
    return site


def test_the_index_package_blocks_requests(tmp_path: Path, installed_requests: Path) -> None:
    out = _child(
        """
        import sys
        import bcoa_worker.index
        assert sys.modules["requests"] is None
        try:
            import requests
        except ImportError:
            print("blocked")
        """,
        tmp_path,
        installed_requests,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "blocked"


def test_a_requests_already_imported_is_left_alone(
    tmp_path: Path, installed_requests: Path
) -> None:
    out = _child(
        """
        import requests
        import bcoa_worker.index
        import requests as again
        print(again.LOADED)
        """,
        tmp_path,
        installed_requests,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True"


def _import_pydicom(tmp_path: Path, *, block: bool) -> dict[str, object]:
    out = _child(
        f"""
        import json, sys
        sockets = []
        sys.addaudithook(
            lambda event, args: sockets.append(event) if event.startswith("socket.") else None
        )
        if {block}:
            import bcoa_worker.index
        import pydicom, pydicom.pixels, pydicom.data
        print(json.dumps({{
            "urllib3": "urllib3" in sys.modules,
            "requests_blocked": sys.modules.get("requests", "absent") is None,
            "sockets": sockets,
        }}))
        """,
        tmp_path,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_block_holds_on_pydicoms_real_import_chain(tmp_path: Path) -> None:
    # The stand-in above shows the mechanism; this holds it against the pinned
    # pydicom 3.0.2, requests 2.34.2 and urllib3 2.8.0, where a plain import of
    # pydicom loads urllib3 and opens a socket to probe for IPv6. The plain
    # import is the control: without requests installed it would not load
    # urllib3, and the blocked case would pass for nothing.
    assert _import_pydicom(tmp_path, block=False)["urllib3"]
    assert _import_pydicom(tmp_path, block=True) == {
        "urllib3": False,
        "requests_blocked": True,
        "sockets": [],
    }
