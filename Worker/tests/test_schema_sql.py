"""The project database schema is defined once, in Swift (BCOAStore); the
worker reads the same file in export jobs. Executing the SQL here keeps a
syntax error from waiting until the first build on a Mac."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import export_project
from bcoa_worker.export import data as export_data
from bcoa_worker.index.catalog import CATALOG_DDL
from swift_sql import migration


def _project(*versions: str) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    for version in versions:
        db.executescript(migration(version))
    return db


def _columns(db: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in db.execute(
            "SELECT p.name FROM sqlite_master m, pragma_table_info(m.name) p WHERE m.type = 'table'"
        )
    ]


def test_v1_executes_and_creates_the_plans_tables() -> None:
    db = _project("v1")
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
    db = _project("v1")
    columns = _columns(db)
    assert [c for c in columns if "birth" in c] == []
    assert [c for c in columns if "name" in c] == ["label_name"]


def test_v2_adds_the_import_tables_on_top_of_v1() -> None:
    db = _project("v1", "v2")
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables >= {
        "project_meta",
        "key_counters",
        "patient_links",
        "index_checks",
        "series_pairs",
        "cohorts",
        "cohort_series",
    }
    assert dict(db.execute("SELECT key, value FROM project_meta")) == {
        "merged_generation": "0",
        "catalog_id": "",
    }
    assert dict(db.execute("SELECT kind, last FROM key_counters")) == {
        "patient": 0,
        "study": 0,
        "series": 0,
        "pseudonym": 0,
    }
    triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert triggers == {
        "series_primary_is_selected_on_insert",
        "series_primary_is_selected_on_update",
    }


def test_no_column_for_names_or_birth_dates_after_v2() -> None:
    # The import adds folder labels and PatientIDs to its catalog, never to
    # project.sqlite: patients are matched by HMAC links instead.
    columns = _columns(_project("v1", "v2"))
    assert [c for c in columns if "birth" in c] == []
    assert [c for c in columns if "name" in c] == ["label_name"]


def test_no_column_for_names_or_birth_dates_in_the_catalog() -> None:
    # The catalog holds folder labels and, until the merge, PatientIDs and
    # accession numbers; a name or a birth date it never reads.
    db = sqlite3.connect(":memory:")
    db.executescript(CATALOG_DDL)
    columns = _columns(db)
    assert [c for c in columns if "name" in c or "birth" in c] == []


def _study(db: sqlite3.Connection, study_key: str = "st_000001") -> None:
    db.execute("INSERT INTO patients (patient_key, pseudonym) VALUES ('pt_000001', 'P0001')")
    db.execute(
        "INSERT INTO studies (study_key, patient_key, study_uid) VALUES (?, 'pt_000001', ?)",
        (study_key, f"2.25.{study_key[-1]}"),
    )


def _series(db: sqlite3.Connection, key: str, selected: int, primary: int) -> None:
    db.execute(
        "INSERT INTO series (series_key, study_key, series_uid, modality, image_count, "
        "fingerprint, selected, is_primary) VALUES (?, 'st_000001', ?, 'CT', 300, ?, ?, ?)",
        (key, f"2.25.9{key[-1]}", f"fp{key}", selected, primary),
    )


def test_a_second_primary_in_a_study_is_refused() -> None:
    db = _project("v1", "v2")
    _study(db)
    _series(db, "s_000001", 1, 1)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _series(db, "s_000002", 1, 1)
    _series(db, "s_000002", 1, 0)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute("UPDATE series SET is_primary = 1 WHERE series_key = 's_000002'")


def test_a_primary_that_is_not_selected_is_refused() -> None:
    db = _project("v1", "v2")
    _study(db)
    with pytest.raises(sqlite3.IntegrityError, match="a primary series must be selected"):
        _series(db, "s_000001", 0, 1)
    _series(db, "s_000001", 1, 1)
    with pytest.raises(sqlite3.IntegrityError, match="a primary series must be selected"):
        db.execute("UPDATE series SET selected = 0 WHERE series_key = 's_000001'")
    # Clearing the primary first, then the selection, is the order every
    # statement in IndexSQL follows.
    db.execute("UPDATE series SET is_primary = 0 WHERE series_key = 's_000001'")
    db.execute("UPDATE series SET selected = 0 WHERE series_key = 's_000001'")


@pytest.mark.parametrize(
    ("results_for", "expected"),
    [
        # The export sees the selected s_000002 only: an unselected series
        # without results is not in it. A lowest key alone would hand the
        # primary to s_000001 and select a series the user never selected.
        (None, [("s_000001", 0, 0), ("s_000002", 1, 1), ("s_000003", 1, 0)]),
        # With results, s_000001 is in the export, and it is the lower key.
        ("s_000001", [("s_000001", 1, 1), ("s_000002", 1, 0), ("s_000003", 1, 0)]),
    ],
)
def test_v2_repairs_primaries_a_hand_made_v1_file_could_hold(
    results_for: str | None, expected: list[tuple[str, int, int]]
) -> None:
    # No shipped version wrote these, but the unique index would refuse to be
    # created over them, and then the project would not open at all. The
    # repair keeps the primary the export already picks.
    db = _project("v1")
    _study(db)
    _series(db, "s_000002", 1, 1)
    _series(db, "s_000001", 0, 1)
    _series(db, "s_000003", 1, 0)
    if results_for is not None:
        db.execute(
            "INSERT INTO runs (run_id, created_at, versions_json, device, models_json, "
            "settings_json) VALUES ('R1', '2026-10-08T12:00:00Z', '{}', 'mps', '[]', '{}')"
        )
        db.execute(
            "INSERT INTO results (run_id, series_key, model, label_id, label_name, voxel_count, "
            "touches_border, device) VALUES ('R1', ?, 'clin_ct_organs', 1, 'liver', 10, 0, 'mps')",
            (results_for,),
        )
    db.executescript(migration("v2"))
    assert db.execute(
        "SELECT series_key, selected, is_primary FROM series ORDER BY 1"
    ).fetchall() == (expected)


@pytest.mark.parametrize(
    ("ddl", "insert"),
    [
        ("v2", "INSERT INTO patient_links (link, patient_key) VALUES (NULL, 'pt_000001')"),
        ("v2", "INSERT INTO project_meta (key, value) VALUES (NULL, '')"),
        ("v2", "INSERT INTO key_counters (kind, last) VALUES (NULL, 0)"),
        ("catalog", "INSERT INTO cat_studies (study_uid, pid_state) VALUES (NULL, 'missing')"),
        (
            "catalog",
            "INSERT INTO pending_identifiers (study_uid, id_source, generation) "
            "VALUES (NULL, 'dicom', 1)",
        ),
        ("catalog", "INSERT INTO catalog_meta (key, value) VALUES (NULL, '')"),
    ],
)
def test_text_primary_keys_refuse_null(ddl: str, insert: str) -> None:
    # SQLite takes NULL in a primary key that is not an INTEGER one. One NULL
    # study_uid in the catalog makes the merge's `study_uid NOT IN (SELECT
    # study_uid FROM idx.cat_studies)` NULL for every study, so none would be
    # marked gone again; a NULL link does the same to new patients' links.
    if ddl == "v2":
        db = _project("v1", "v2")
        _study(db)
    else:
        db = sqlite3.connect(":memory:")
        db.executescript(CATALOG_DDL)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        db.execute(insert)


def test_key_counters_start_after_the_keys_already_used() -> None:
    db = _project("v1")
    db.execute("INSERT INTO patients (patient_key, pseudonym) VALUES ('pt_000007', 'P0012')")
    db.execute("INSERT INTO patients (patient_key, pseudonym) VALUES ('pk_legacy', 'Xyz')")
    db.execute(
        "INSERT INTO studies (study_key, patient_key, study_uid) VALUES ('st_000004', 'pt_000007', '1')"
    )
    db.execute(
        "INSERT INTO series (series_key, study_key, series_uid, modality, image_count, "
        "fingerprint) VALUES ('s_000031', 'st_000004', '1.1', 'CT', 1, 'f')"
    )
    db.executescript(migration("v2"))
    assert dict(db.execute("SELECT kind, last FROM key_counters")) == {
        "patient": 7,
        "study": 4,
        "series": 31,
        "pseudonym": 12,
    }


def test_s10_the_v1_export_fixture_migrates_and_exports_the_same(tmp_path: Path) -> None:
    path = export_project.create(tmp_path)
    before = export_data.load(path, export_project.RUN)
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(migration("v2"))
    db.commit()
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()
    after = export_data.load(path, export_project.RUN)
    assert after == before
