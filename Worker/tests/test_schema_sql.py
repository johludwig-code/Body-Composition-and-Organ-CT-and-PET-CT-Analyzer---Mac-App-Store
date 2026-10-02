"""The project database schema is defined once, in Swift (BCOAStore); the
worker reads the same file in export jobs. Executing the SQL here keeps a
syntax error from waiting until the first build on a Mac."""

from __future__ import annotations

import re
import sqlite3

from conftest import WORKER_ROOT

SWIFT = WORKER_ROOT.parent / "Packages/BCOAStore/Sources/BCOAStore/StoreMigrations.swift"


def _v1() -> str:
    match = re.search(r'static let v1 = """\n(.*?)\n\s*"""', SWIFT.read_text(), re.S)
    assert match, "v1 migration not found"
    return match.group(1)


def test_v1_executes_and_creates_the_plans_tables() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(_v1())
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables >= {
        "sources",
        "patients",
        "identifiers",
        "studies",
        "series",
        "runs",
        "jobs",
        "results",
        "qc",
        "audit_log",
    }


def test_no_column_for_names_or_birth_dates() -> None:
    db = sqlite3.connect(":memory:")
    db.executescript(_v1())
    columns = [
        r[0]
        for r in db.execute(
            "SELECT p.name FROM sqlite_master m, pragma_table_info(m.name) p WHERE m.type = 'table'"
        )
    ]
    assert [c for c in columns if "birth" in c] == []
    assert [c for c in columns if "name" in c] == ["label_name"]
