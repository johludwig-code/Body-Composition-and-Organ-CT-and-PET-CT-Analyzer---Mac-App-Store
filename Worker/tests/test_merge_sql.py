"""The import's SQL as the app runs it: `IndexSQL`, `SelectionSQL` and
`IdentitySQL`, extracted from BCOAStore, against project databases made by
the v1 and v2 migrations and catalogs made by the worker's own DDL.

Scenarios S1 to S11 follow the M2 design (S10, the migration of the v1
export fixture, is in test_schema_sql.py). A failure here is a failure of
the app's merge on a Mac, found before the first build there."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from bcoa_worker.export import data as export_data
from bcoa_worker.index.catalog import open_catalog
from swift_sql import INDEX_SQL, constants, migration

_SQL = constants(INDEX_SQL)
INDEX = _SQL["IndexSQL"]
SELECTION = _SQL["SelectionSQL"]
IDENTITY = _SQL["IdentitySQL"]

NOW = "2026-10-08T12:00:00Z"
LINK_KEY_ID = "5f0c2a9e41d7b388"

# Links are opaque to the SQL; these stand for HMACs of synthetic IDs.
PID_A = "pid:" + "a1" * 32
PID_6 = "pid:" + "06" * 32
PID_7 = "pid:" + "07" * 32
PID_7_NEW = "pid:" + "70" * 32
PID_TYPED = "pid:" + "7e" * 32
FOLDER_SOURCE = "folder:" + "50" * 32
FOLDER_017 = "folder:" + "17" * 32
FOLDER_018 = "folder:" + "18" * 32
FOLDER_020 = "folder:" + "20" * 32

_CLEAR_DERIVED = (
    "DELETE FROM scans",
    "DELETE FROM pending_identifiers",
    "DELETE FROM cat_studies",
    "DELETE FROM cat_series",
    "DELETE FROM cat_checks",
    "DELETE FROM cat_pairs",
    "DELETE FROM cat_id_candidates",
)
_INSERT_STUDY = (
    "INSERT INTO cat_studies (study_uid, pid_link, pid_state, sex, age_years, study_date, "
    "description) VALUES (:study_uid, :pid_link, :pid_state, :sex, :age_years, :study_date, "
    ":description)"
)
_INSERT_SERIES = (
    "INSERT INTO cat_series (part_ref, series_uid, part, study_uid, modality, description, "
    "image_count, slice_thickness_mm, kernel, fingerprint, series_number, auto_rank, "
    "auto_selected, reason_json) VALUES (:part_ref, :series_uid, :part, :study_uid, :modality, "
    ":description, :image_count, :slice_thickness_mm, :kernel, :fingerprint, :series_number, "
    ":auto_rank, :auto_selected, :reason_json)"
)


def reason(outcome: str, code: str, **params: Any) -> str:
    return json.dumps({"v": 1, "outcome": outcome, "codes": [code], "params": params})


def study(uid: str, **fields: Any) -> dict[str, Any]:
    return {
        "study_uid": uid,
        "pid_link": None,
        "pid_state": "missing",
        "sex": None,
        "age_years": None,
        "study_date": None,
        "description": "CT",
    } | fields


def series(part_ref: int, uid: str, study_uid: str, **fields: Any) -> dict[str, Any]:
    return {
        "part_ref": part_ref,
        "series_uid": uid,
        "part": 0,
        "study_uid": study_uid,
        "modality": "CT",
        "description": None,
        "image_count": 300,
        "slice_thickness_mm": 1.0,
        "kernel": None,
        "fingerprint": f"fp{uid}",
        "series_number": None,
        "auto_rank": None,
        "auto_selected": 0,
        "reason_json": reason("excluded", "select.excluded.description", term="topogram"),
    } | fields


def chosen(part_ref: int, uid: str, study_uid: str, **fields: Any) -> dict[str, Any]:
    return (
        series(
            part_ref,
            uid,
            study_uid,
            auto_rank=1,
            auto_selected=1,
            reason_json=reason("chosen", "select.chosen.only_candidate"),
        )
        | fields
    )


def _transaction(db: sqlite3.Connection, *parts: str) -> None:
    # As IndexStore runs them: one transaction, rolled back as a whole when a
    # statement aborts, which also drops the temp tables it had created.
    try:
        db.executescript("BEGIN IMMEDIATE;\n" + "\n".join(parts) + "\nCOMMIT;")
    except sqlite3.DatabaseError:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


class Project:
    """project.sqlite at v2 with one source, and its catalog beside it, driven
    the way IndexStore drives them: inputs into temp tables, then one
    transaction."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / "project.sqlite"
        self.catalog = folder / "index" / "catalog.sqlite"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(migration("v1"))
            db.executescript(migration("v2"))
            db.execute(
                "INSERT INTO sources (id, bookmark, display_path, added_at) "
                "VALUES (1, x'00', 'Source 1', ?)",
                (NOW,),
            )
            db.commit()

    def connect(self) -> sqlite3.Connection:
        # A URI connection, so that the catalog can be attached read-only.
        db = sqlite3.connect(self.path.resolve().as_uri(), uri=True, isolation_level=None)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def write_catalog(
        self,
        generation: int,
        studies: Iterable[dict[str, Any]],
        parts: Iterable[dict[str, Any]],
        *,
        pending: Iterable[tuple[Any, ...]] = (),
        candidates: Iterable[tuple[Any, ...]] = (),
        checks: Iterable[tuple[Any, ...]] = (),
        pairs: Iterable[tuple[Any, ...]] = (),
        link_key_id: str | None = LINK_KEY_ID,
        complete: bool = True,
        rebuild: bool = False,
    ) -> str:
        """One generation of derived tables, written as the regroup writes
        them; returns the catalog id. `rebuild` starts a new catalog, as
        after Remove Identifiers or a catalog of another format."""
        if rebuild:
            self.catalog.unlink(missing_ok=True)
        with closing(open_catalog(self.catalog)) as db:
            db.execute("BEGIN IMMEDIATE")
            for statement in _CLEAR_DERIVED:
                db.execute(statement)
            db.execute(
                "INSERT INTO scans (source_id, state, started_at, finished_at) "
                "VALUES (1, 'complete', ?, ?)",
                (NOW, NOW),
            )
            db.executemany(_INSERT_STUDY, studies)
            db.executemany(_INSERT_SERIES, parts)
            db.executemany("INSERT INTO pending_identifiers VALUES (?, ?, ?, ?, ?)", pending)
            db.executemany("INSERT INTO cat_id_candidates VALUES (?, ?, ?, ?, ?)", candidates)
            db.executemany("INSERT INTO cat_checks VALUES (?, ?, ?, ?, ?)", checks)
            db.executemany("INSERT INTO cat_pairs VALUES (?, ?, ?, ?)", pairs)
            db.executemany(
                "INSERT OR REPLACE INTO catalog_meta (key, value) VALUES (?, ?)",
                [("generation", str(generation)), ("complete", "1" if complete else "0")],
            )
            db.execute("DELETE FROM catalog_meta WHERE key = 'link_key_id'")
            if link_key_id is not None:
                db.execute(
                    "INSERT INTO catalog_meta (key, value) VALUES ('link_key_id', ?)",
                    (link_key_id,),
                )
            db.execute("COMMIT")
            return db.execute("SELECT value FROM catalog_meta WHERE key = 'catalog_id'").fetchone()[
                0
            ]

    def merge(
        self,
        *,
        keep: int = 1,
        policy: str = "refuse",
        link_key_id: str | None = LINK_KEY_ID,
        db: sqlite3.Connection | None = None,
    ) -> None:
        connection = db or self.connect()
        try:
            connection.execute(
                "ATTACH DATABASE ? AS idx", (self.catalog.resolve().as_uri() + "?mode=ro",)
            )
            try:
                connection.executescript(INDEX["mergeInputs"])
                connection.execute(
                    "INSERT INTO temp.merge_params "
                    "(now, keep_identifiers, new_study_policy, link_key_id) VALUES (?, ?, ?, ?)",
                    (NOW, keep, policy, link_key_id),
                )
                _transaction(connection, INDEX["merge"], INDEX["patientAges"])
            finally:
                connection.execute("DETACH DATABASE idx")
        finally:
            if db is None:
                connection.close()

    def edit(
        self,
        *statements: str,
        target: Iterable[str] = (),
        scope: Iterable[str] = (),
        max_mm: float | None = None,
        label: str | None = None,
        cohort_id: int | None = None,
    ) -> None:
        with closing(self.connect()) as db:
            db.executescript(SELECTION["inputs"])
            db.executemany("INSERT INTO temp.sel_target VALUES (?)", [(k,) for k in target])
            db.executemany("INSERT INTO temp.sel_scope VALUES (?)", [(k,) for k in scope])
            db.execute(
                "INSERT INTO temp.sel_param (max_mm, label, now, cohort_id) VALUES (?, ?, ?, ?)",
                (max_mm, label, NOW, cohort_id),
            )
            _transaction(
                db,
                *(SELECTION[s] for s in statements),
                SELECTION["repairPrimary"],
                INDEX["patientAges"],
            )

    def identity(
        self,
        *statements: str,
        confirm: Iterable[tuple[str, str, str, str]] = (),
        assign: Iterable[tuple[str, str]] = (),
        typed: Iterable[tuple[str, str, str]] = (),
        level: int | None = None,
        keep: int = 1,
    ) -> int:
        """Runs identity statements as one edit; returns how many labels and
        typed IDs are still in the connection's temp tables afterwards."""
        with closing(self.connect()) as db:
            db.executescript(SELECTION["inputs"])
            db.executescript(IDENTITY["inputs"])
            db.execute(
                "INSERT INTO temp.id_param (now, actor, keep_identifiers, level) "
                "VALUES (?, 'user', ?, ?)",
                (NOW, keep, level),
            )
            db.executemany("INSERT INTO temp.id_confirm VALUES (?, ?, ?, ?)", confirm)
            db.executemany("INSERT INTO temp.id_assign VALUES (?, ?)", assign)
            db.executemany("INSERT INTO temp.id_typed VALUES (?, ?, ?)", typed)
            _transaction(
                db,
                *(IDENTITY[s] for s in statements),
                SELECTION["applyAuto"],
                SELECTION["repairPrimary"],
                INDEX["patientAges"],
            )
            return db.execute(
                "SELECT (SELECT count(*) FROM temp.id_confirm) + (SELECT count(*) FROM temp.id_typed)"
            ).fetchone()[0]

    def catalog_id(self) -> str:
        with closing(sqlite3.connect(self.catalog)) as db:
            return db.execute("SELECT value FROM catalog_meta WHERE key = 'catalog_id'").fetchone()[
                0
            ]

    def q(self, sql: str, *args: Any) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(sql, args).fetchall()

    def dump(self) -> list[str]:
        with closing(sqlite3.connect(self.path)) as db:
            return list(db.iterdump())

    def invariants(self) -> None:
        # One primary in every study with a selected series, a primary is
        # selected, nothing under an unconfirmed patient is selected, and no
        # reference dangles.
        assert (
            self.q(
                "SELECT study_key FROM series GROUP BY study_key "
                "HAVING sum(selected) > 0 AND sum(is_primary) <> 1"
            )
            == []
        )
        assert self.q("SELECT count(*) FROM series WHERE is_primary = 1 AND selected = 0") == [(0,)]
        assert self.q(
            "SELECT count(*) FROM series JOIN studies USING (study_key) "
            "JOIN patients USING (patient_key) WHERE id_status = 'unconfirmed' AND selected = 1"
        ) == [(0,)]
        # The automatic choice of an unconfirmed patient's study is held, and
        # says so, and no other is (ADR 0023).
        assert self.q(
            "SELECT count(*) FROM series AS s JOIN studies AS st USING (study_key) "
            "JOIN patients AS p USING (patient_key) "
            "WHERE s.index_state = 'current' AND s.auto_selected = 1 "
            "AND (p.id_status = 'unconfirmed') "
            "<> (coalesce(json_extract(s.selection_reason, '$.outcome'), '') = 'held')"
        ) == [(0,)]
        assert self.q("PRAGMA foreign_key_check") == []

    def primary(self, study_key: str) -> list[tuple[Any, ...]]:
        return self.q(
            "SELECT series_key FROM series WHERE is_primary = 1 AND study_key = ?", study_key
        )

    def audit(self, action: str) -> list[dict[str, Any]]:
        return [
            json.loads(r[0])
            for r in self.q(
                "SELECT details_json FROM audit_log WHERE action = ? ORDER BY id", action
            )
        ]


# ---------------------------------------------------------------- S1 to S11

A_STUDY = study(
    "1.1",
    pid_link=PID_A,
    pid_state="present",
    sex="F",
    age_years=61.0,
    study_date="2020-01-10",
    description="CT abd",
)
B_STUDY = study("2.1", sex="M", age_years=50.0, study_date="2021-02-02")
A2_STUDY = A_STUDY | {"study_uid": "1.2", "age_years": 62.0, "study_date": "2021-01-10"}
TOPO = series(1, "1.1.1", "1.1", description="Topogram", image_count=1, fingerprint="fpT")
THIN = chosen(
    2,
    "1.1.2",
    "1.1",
    description="Abd 1.0",
    fingerprint="fp1",
    reason_json=reason("chosen", "select.chosen.thinnest", thickness=1.0),
)
THICK = series(
    3,
    "1.1.3",
    "1.1",
    description="Abd 5.0",
    slice_thickness_mm=5.0,
    fingerprint="fp5",
    auto_rank=2,
    reason_json=reason("eligible", "select.eligible.thicker", thickness=5.0, chosen_thickness=1.0),
)
B_ONE = chosen(4, "2.1.1", "2.1", fingerprint="fpB")
A2_ONE = chosen(5, "1.2.1", "1.2", fingerprint="fpA2")
THINNER = chosen(
    6, "1.1.6", "1.1", description="Abd 0.6", slice_thickness_mm=0.6, fingerprint="fp06"
)
THICK_SPLIT = THICK | {"part": 2, "fingerprint": "fp5b", "auto_rank": 3}
THICK_OTHER = series(
    7, "1.1.3", "1.1", part=1, description="Abd 5.0", image_count=40, fingerprint="fp5c"
)
NEW_STUDY = study("3.1", pid_state="withheld", sex="F", age_years=40.0, study_date="2022-01-01")
NEW_ONE = chosen(20, "3.1.1", "3.1", fingerprint="fpN")


def _s1(p: Project) -> None:
    p.write_catalog(
        1,
        [A_STUDY, B_STUDY],
        [TOPO, THIN, THICK, B_ONE],
        pending=[("1.1", "00012345", "ACC1", "dicom", 1)],
        candidates=[("2.1", 1, 0, "SourceA", FOLDER_SOURCE), ("2.1", 1, 1, "CASE_017", FOLDER_017)],
        checks=[
            ("series", "2", "check.gap", "warning", '{"gaps":1,"missing":2,"largest_mm":3.0}'),
            ("source", "1", "check.archives_skipped", "info", '{"count":1}'),
        ],
    )
    p.merge()


def _s3(p: Project) -> None:
    p.edit("makePrimary", target=["s_000003"], scope=["st_000001"])
    p.write_catalog(
        2,
        [A_STUDY, B_STUDY, A2_STUDY],
        [
            TOPO,
            THIN | {"auto_rank": 2, "auto_selected": 0},
            THICK | {"auto_rank": 3},
            B_ONE,
            A2_ONE,
            THINNER,
        ],
        pending=[("1.2", "00012345", "ACC2", "dicom", 2)],
    )
    p.merge()


def _s4(p: Project) -> None:
    # The series of the user's primary is split; its parts renumber and swap.
    p.write_catalog(
        3,
        [A_STUDY, B_STUDY, A2_STUDY],
        [
            TOPO,
            THIN | {"auto_rank": 2, "auto_selected": 0},
            THICK_SPLIT,
            THICK_OTHER,
            B_ONE,
            A2_ONE,
            THINNER,
        ],
    )
    p.merge()


def _s5(p: Project) -> None:
    # Results exist for s_000002; then it and the user's primary s_000003 vanish.
    with closing(sqlite3.connect(p.path)) as db:
        db.execute(
            "INSERT INTO runs (run_id, created_at, versions_json, device, models_json, "
            "settings_json) VALUES ('R1', ?, '{}', 'mps', '[]', '{}')",
            (NOW,),
        )
        db.execute(
            "INSERT INTO results (run_id, series_key, model, label_id, label_name, voxel_count, "
            "volume_ml, hu_mean, hu_sd, hu_median, hu_p05, hu_p95, hu_min, hu_max, "
            "touches_border, device) VALUES ('R1', 's_000002', 'clin_ct_organs', 1, 'liver', "
            "10, 1.0, 40.0, 1.0, 40.0, 1.0, 1.0, 1.0, 1.0, 0, 'mps')"
        )
        db.commit()
    p.write_catalog(4, [A_STUDY, B_STUDY, A2_STUDY], [TOPO, B_ONE, A2_ONE, THINNER, THICK_OTHER])
    p.merge()


def _s6(p: Project) -> None:
    # The files of s_000002 come back unchanged, under a new part identity.
    p.write_catalog(
        5,
        [A_STUDY, B_STUDY, A2_STUDY],
        [
            TOPO,
            B_ONE,
            A2_ONE,
            THINNER,
            THICK_OTHER,
            THIN | {"part_ref": 9, "auto_rank": 2, "auto_selected": 0},
        ],
    )
    p.merge()


def _s7(p: Project) -> None:
    p.edit("bulkThinCT", scope=["st_000001", "st_000002", "st_000003"], max_mm=3.0)


def _s7_deselect(p: Project) -> None:
    p.edit("deselect", target=["s_000005"], scope=["st_000001"])


STORY: list[Callable[[Project], None]] = [_s1, _s3, _s4, _s5, _s6, _s7, _s7_deselect]


def told(tmp_path: Path, last: Callable[[Project], None]) -> Project:
    """A project that has gone through the scenarios up to and including `last`."""
    p = Project(tmp_path)
    for step in STORY[: STORY.index(last) + 1]:
        step(p)
        p.invariants()
    return p


def test_s1_first_merge(tmp_path: Path) -> None:
    p = told(tmp_path, _s1)
    catalog_id = p.catalog_id()
    assert p.q("SELECT patient_key, pseudonym, id_status FROM patients ORDER BY 1") == [
        ("pt_000001", "P0001", "dicom"),
        ("pt_000002", "P0002", "unconfirmed"),
    ]
    # The unconfirmed patient's study is proposed, never selected.
    assert p.q(
        "SELECT series_key, description, selected, is_primary, catalog_part FROM series ORDER BY 1"
    ) == [
        ("s_000001", "Topogram", 0, 0, f"{catalog_id}:1"),
        ("s_000002", "Abd 1.0", 1, 1, f"{catalog_id}:2"),
        ("s_000003", "Abd 5.0", 0, 0, f"{catalog_id}:3"),
        ("s_000004", None, 0, 0, f"{catalog_id}:4"),
    ]
    assert json.loads(
        p.q("SELECT selection_reason FROM series WHERE series_key = 's_000002'")[0][0]
    )["codes"] == ["select.chosen.thinnest"]
    # Held, and the reason says so rather than "chosen" (ADR 0023); the
    # worker's reason waits for the confirmation.
    held = json.loads(
        p.q("SELECT selection_reason FROM series WHERE series_key = 's_000004'")[0][0]
    )
    assert (held["outcome"], held["codes"]) == ("held", ["select.held.unconfirmed_patient"])
    assert held["if_confirmed"]["outcome"] == "chosen"
    assert p.q("SELECT patient_key, patient_id, accession_numbers, id_source FROM identifiers") == [
        ("pt_000001", "00012345", '["ACC1"]', "dicom")
    ]
    assert p.q("SELECT link, patient_key FROM patient_links") == [(PID_A, "pt_000001")]
    assert p.q("SELECT object_kind, object_key, code FROM index_checks ORDER BY 1") == [
        ("series", "s_000002", "check.gap"),
        ("source", "1", "check.archives_skipped"),
    ]
    assert p.q("SELECT age_at_first_study, sex FROM patients ORDER BY patient_key") == [
        (61.0, "F"),
        (50.0, "M"),
    ]
    assert p.q("SELECT state, indexed_at FROM sources") == [("indexed", NOW)]
    assert dict(p.q("SELECT key, value FROM project_meta")) == {
        "merged_generation": "1",
        "catalog_id": catalog_id,
    }
    assert dict(p.q("SELECT kind, last FROM key_counters")) == {
        "patient": 2,
        "study": 2,
        "series": 4,
        "pseudonym": 2,
    }
    assert p.audit("index_merged") == [
        {
            "generation": 1,
            "new_patients": 2,
            "new_studies": 2,
            "new_series": 4,
            "refused_studies": 0,
            "gone_series": 0,
        }
    ]
    # Folder labels stay in the catalog: not one byte of them in the project.
    assert b"CASE_017" not in p.path.read_bytes()
    assert b"SourceA" not in p.path.read_bytes()


@pytest.mark.parametrize("why", ["already_merged", "incomplete", "other_format"])
def test_s2_a_catalog_that_cannot_be_merged_changes_nothing(tmp_path: Path, why: str) -> None:
    p = told(tmp_path, _s1)
    before = p.dump()
    if why == "incomplete":
        p.write_catalog(2, [A_STUDY], [TOPO], complete=False)
    elif why == "other_format":
        p.write_catalog(2, [A_STUDY], [TOPO])
        with closing(sqlite3.connect(p.catalog)) as db:
            db.execute("UPDATE catalog_meta SET value = '2' WHERE key = 'format'")
            db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        p.merge()
    assert p.dump() == before


def test_a_refused_merge_leaves_the_connection_ready_for_the_next(tmp_path: Path) -> None:
    # IndexStore keeps one connection: the temp tables of an aborted merge
    # must go with its rollback, or the next merge fails to create them.
    p = told(tmp_path, _s1)
    with closing(p.connect()) as db:
        with pytest.raises(sqlite3.IntegrityError):
            p.merge(db=db)
        assert db.execute(
            "SELECT name FROM temp.sqlite_master WHERE type = 'table'"
        ).fetchall() == [("merge_params",)]
        p.write_catalog(2, [A_STUDY, B_STUDY], [TOPO, THIN, THICK, B_ONE])
        p.merge(db=db)
    assert p.q("SELECT value FROM project_meta WHERE key = 'merged_generation'") == [("2",)]


def test_s3_a_choice_by_hand_survives_a_rescan_and_a_second_study_joins_its_patient(
    tmp_path: Path,
) -> None:
    p = told(tmp_path, _s3)
    assert p.q("SELECT study_key, patient_key, study_uid FROM studies ORDER BY 1") == [
        ("st_000001", "pt_000001", "1.1"),
        ("st_000002", "pt_000002", "2.1"),
        ("st_000003", "pt_000001", "1.2"),
    ]
    assert p.q(
        "SELECT series_key, selected, is_primary, selection_origin FROM series "
        "WHERE study_key = 'st_000001' ORDER BY 1"
    ) == [
        ("s_000001", 0, 0, "auto"),
        ("s_000002", 1, 0, "auto"),
        ("s_000003", 1, 1, "user"),
        ("s_000005", 0, 0, "auto"),
    ]
    assert p.q("SELECT code, params_json FROM index_checks WHERE object_key = 'st_000001'") == [
        ("check.new_series_since_manual", '{"count":1,"would_be_chosen":1}')
    ]
    assert p.q("SELECT accession_numbers FROM identifiers") == [('["ACC1","ACC2"]',)]
    # The age is that of the first study with a selected series.
    assert p.q("SELECT age_at_first_study FROM patients WHERE patient_key = 'pt_000001'") == [
        (61.0,)
    ]


def test_s4_a_split_keeps_the_key_by_part_identity(tmp_path: Path) -> None:
    p = told(tmp_path, _s4)
    assert p.q(
        "SELECT series_key, part, selected, is_primary, fingerprint FROM series "
        "WHERE series_uid = '1.1.3' ORDER BY 1"
    ) == [("s_000003", 2, 1, 1, "fp5b"), ("s_000007", 1, 0, 0, "fp5c")]


def test_s5_gone_series_and_a_lost_primary(tmp_path: Path) -> None:
    p = told(tmp_path, _s5)
    # Gone with results stays, gone without anything referring to it is deleted.
    assert p.q(
        "SELECT series_key, index_state, selected, part < 0, catalog_part IS NULL FROM series "
        "WHERE series_uid IN ('1.1.2', '1.1.3') ORDER BY 1"
    ) == [("s_000002", "gone", 0, 1, 1), ("s_000007", "current", 0, 0, 0)]
    assert p.q("SELECT selection_mode FROM studies WHERE study_key = 'st_000001'") == [("auto",)]
    assert p.primary("st_000001") == [("s_000005",)]
    assert ("check.primary_gone",) in p.q(
        "SELECT code FROM index_checks WHERE object_key = 'st_000001'"
    )
    assert p.audit("index_merged")[-1]["gone_series"] == 1


def test_s6_returning_files_keep_their_key_by_fingerprint(tmp_path: Path) -> None:
    p = told(tmp_path, _s6)
    catalog_id = p.catalog_id()
    assert p.q(
        "SELECT series_key, index_state, part, catalog_part FROM series WHERE series_uid = '1.1.2'"
    ) == [("s_000002", "current", 0, f"{catalog_id}:9")]


def test_s7_bulk_thin_ct_adds_without_moving_the_primary(tmp_path: Path) -> None:
    p = told(tmp_path, _s7)
    assert p.q(
        "SELECT series_key, selected, is_primary, selection_origin FROM series "
        "WHERE study_key = 'st_000001' AND index_state = 'current' ORDER BY 1"
    ) == [
        ("s_000001", 0, 0, "auto"),
        ("s_000002", 1, 0, "bulk_thin_ct"),
        ("s_000005", 1, 1, "auto"),
        ("s_000007", 0, 0, "auto"),
    ]
    # The unconfirmed patient's thin CT is not added.
    assert p.q("SELECT selected FROM series WHERE series_key = 's_000004'") == [(0,)]


def test_s7_deselecting_the_primary_repairs_it(tmp_path: Path) -> None:
    p = told(tmp_path, _s7_deselect)
    assert p.primary("st_000001") == [("s_000002",)]
    assert p.q("SELECT selection_mode FROM studies WHERE study_key = 'st_000001'") == [("user",)]


def test_s8_the_export_loader_reads_a_merged_project(tmp_path: Path) -> None:
    p = told(tmp_path, _s7_deselect)
    data = export_data.load(p.path, "R1")
    assert {s.series_key for s in data.series} >= {"s_000002"}
    assert {x.patient_key for x in data.patients} == {"pt_000001", "pt_000002"}
    assert [x.patient_id for x in data.patients if x.patient_key == "pt_000001"] == ["00012345"]


def _remove_identifiers(p: Project) -> None:
    # What Remove Identifiers does to project.sqlite (design section 8).
    with closing(sqlite3.connect(p.path)) as db:
        db.execute("PRAGMA secure_delete = ON")
        db.execute("DELETE FROM identifiers")
        db.execute("DELETE FROM patient_links")
        db.execute("UPDATE patients SET id_status = 'unlinked' WHERE id_status = 'unconfirmed'")
        db.execute("INSERT INTO project_meta VALUES ('identifiers_removed_at', ?)", (NOW,))
        db.commit()


def _without_ids(s: dict[str, Any]) -> dict[str, Any]:
    return s | {"pid_link": None, "pid_state": "withheld"}


def _s9_catalog(p: Project) -> str:
    return p.write_catalog(
        1,
        [_without_ids(A_STUDY), B_STUDY, _without_ids(A2_STUDY), NEW_STUDY],
        [
            TOPO,
            B_ONE,
            A2_ONE,
            THINNER,
            THICK_OTHER,
            THIN | {"part_ref": 9, "auto_rank": 2, "auto_selected": 0},
            NEW_ONE,
        ],
        pending=[("1.1", None, "ACC1", "dicom", 1)],
        link_key_id=None,
        rebuild=True,
    )


def test_s9_after_remove_identifiers(tmp_path: Path) -> None:
    p = told(tmp_path, _s7_deselect)
    before = p.q("SELECT series_key FROM series WHERE index_state = 'current' ORDER BY 1")
    _remove_identifiers(p)
    catalog_id = _s9_catalog(p)
    p.merge(keep=0, link_key_id=None)
    p.invariants()
    assert p.q("SELECT count(*) FROM identifiers") == [(0,)]
    assert p.q("SELECT count(*) FROM patient_links") == [(0,)]
    # A new study cannot be matched to a patient, so it is counted, not added.
    assert p.q("SELECT count(*) FROM studies WHERE study_uid = '3.1'") == [(0,)]
    assert p.q(
        "SELECT object_kind, params_json FROM index_checks WHERE code = 'check.new_studies_not_added'"
    ) == [("project", '{"count":1}')]
    # The rebuilt catalog has new part identities; every series is found
    # again by UID and fingerprint and keeps its key.
    assert p.q("SELECT series_key FROM series WHERE index_state = 'current' ORDER BY 1") == before
    assert p.q(
        "SELECT DISTINCT substr(catalog_part, 1, 32) FROM series WHERE index_state = 'current'"
    ) == [(catalog_id,)]
    # A rebuilt catalog starts again at generation 1; its new id is what lets
    # the merge take it.
    assert dict(p.q("SELECT key, value FROM project_meta"))["catalog_id"] == catalog_id
    assert p.audit("index_merged")[-1]["refused_studies"] == 1


def test_s9_add_unlinked_policy_adds_new_studies_under_their_own_patient(tmp_path: Path) -> None:
    p = told(tmp_path, _s7_deselect)
    _remove_identifiers(p)
    _s9_catalog(p)
    p.merge(keep=0, policy="add_unlinked", link_key_id=None)
    p.invariants()
    assert p.q(
        "SELECT st.study_key, p.patient_key, p.pseudonym, p.id_status FROM studies AS st "
        "JOIN patients AS p USING (patient_key) WHERE st.study_uid = '3.1'"
    ) == [("st_000004", "pt_000003", "P0003", "unlinked")]
    assert p.q("SELECT selected, is_primary FROM series WHERE series_uid = '3.1.1'") == [(1, 1)]
    assert p.q("SELECT count(*) FROM identifiers") == [(0,)]
    assert p.q("SELECT count(*) FROM index_checks WHERE code = 'check.new_studies_not_added'") == [
        (0,)
    ]


def test_s11_a_cohort_brings_back_the_same_selection_and_primaries(tmp_path: Path) -> None:
    p = told(tmp_path, _s7_deselect)
    studies = [r[0] for r in p.q("SELECT study_key FROM studies WHERE index_state = 'current'")]

    def snapshot() -> list[tuple[Any, ...]]:
        return p.q(
            "SELECT series_key, selected, is_primary FROM series "
            "WHERE index_state = 'current' ORDER BY 1"
        )

    before = snapshot()
    selected = [r[0] for r in before if r[1]]
    p.edit("saveCohort", scope=studies, label="Baseline")
    assert p.q("SELECT cohort_id, label, created_at FROM cohorts") == [(1, "Baseline", NOW)]
    assert p.q("SELECT series_key, is_primary FROM cohort_series ORDER BY 1") == [
        (r[0], r[2]) for r in before if r[1]
    ]

    p.edit("applyAuto", scope=studies)
    p.edit("deselect", target=selected, scope=studies)
    p.invariants()
    assert snapshot() != before

    p.edit("applyCohort", scope=studies, cohort_id=1)
    p.invariants()
    assert snapshot() == before
    assert p.q(
        "SELECT DISTINCT selection_origin, selection_cohort_id FROM series "
        "WHERE index_state = 'current' AND study_key NOT IN (SELECT study_key FROM studies "
        "JOIN patients USING (patient_key) WHERE id_status = 'unconfirmed')"
    ) == [("cohort", 1)]

    # A cohort member that disappears keeps its cohort row, and the series
    # stays, marked gone, although no result refers to it.
    assert selected == ["s_000002", "s_000006"]
    p.write_catalog(
        6,
        [A_STUDY, B_STUDY, A2_STUDY],
        [TOPO, B_ONE, THINNER, THICK_OTHER, THIN | {"part_ref": 9, "auto_rank": 2}],
    )
    p.merge()
    p.invariants()
    assert p.q("SELECT count(*) FROM cohort_series WHERE series_key = 's_000006'") == [(1,)]
    assert p.q("SELECT index_state FROM series WHERE series_key = 's_000006'") == [("gone",)]


# ---------------------------------------------------------------- selection edits


def test_select_adds_series_without_changing_the_primary(tmp_path: Path) -> None:
    p = told(tmp_path, _s1)
    p.edit("select", target=["s_000003", "s_000004"], scope=["st_000001", "st_000002"])
    p.invariants()
    assert p.q(
        "SELECT series_key, selected, is_primary, selection_origin FROM series ORDER BY 1"
    ) == [
        ("s_000001", 0, 0, "auto"),
        ("s_000002", 1, 1, "auto"),
        ("s_000003", 1, 0, "user"),
        # Held: its patient's ID came from a folder name and is not confirmed.
        ("s_000004", 0, 0, "auto"),
    ]
    assert p.q("SELECT selection_mode FROM studies WHERE study_key = 'st_000001'") == [("user",)]
    # A rescan keeps a study chosen by hand as it is.
    p.write_catalog(2, [A_STUDY, B_STUDY], [TOPO, THIN, THICK, B_ONE])
    p.merge()
    p.invariants()
    assert p.q(
        "SELECT series_key, selected, is_primary FROM series WHERE study_key = 'st_000001' "
        "ORDER BY 1"
    ) == [("s_000001", 0, 0), ("s_000002", 1, 1), ("s_000003", 1, 0)]


def test_select_in_a_study_without_a_selection_makes_it_primary(tmp_path: Path) -> None:
    p = told(tmp_path, _s1)
    p.edit("deselect", target=["s_000002"], scope=["st_000001"])
    assert p.q("SELECT count(*) FROM series WHERE selected = 1") == [(0,)]
    p.edit("select", target=["s_000003"], scope=["st_000001"])
    p.invariants()
    assert p.primary("st_000001") == [("s_000003",)]


def test_select_leaves_a_gone_series_alone(tmp_path: Path) -> None:
    p = told(tmp_path, _s5)
    p.edit("select", target=["s_000002"], scope=["st_000001"])
    p.edit("makePrimary", target=["s_000002"], scope=["st_000001"])
    p.invariants()
    assert p.q("SELECT selected, is_primary FROM series WHERE series_key = 's_000002'") == [(0, 0)]
    assert p.primary("st_000001") == [("s_000005",)]


def test_pairs_are_kept_by_series_key(tmp_path: Path) -> None:
    p = Project(tmp_path)
    p.write_catalog(
        1,
        [A_STUDY],
        [
            chosen(1, "1.1.1", "1.1"),
            series(
                2,
                "1.1.2",
                "1.1",
                modality="PT",
                reason_json=reason("excluded", "select.excluded.not_ct", modality="PT"),
            ),
        ],
        pairs=[(2, 1, 1, 812.5)],
    )
    p.merge()
    p.invariants()
    assert p.q("SELECT * FROM series_pairs") == [("s_000002", "s_000001", 1, 812.5)]
    assert p.q("SELECT series_key, selected FROM series ORDER BY 1") == [
        ("s_000001", 1),
        ("s_000002", 0),
    ]


# ---------------------------------------------------------------- series that change study


@pytest.mark.parametrize("rebuild", [False, True])
def test_a_series_that_moves_into_another_study_leaves_its_primary_behind(
    tmp_path: Path, rebuild: bool
) -> None:
    # A study merge in the PACS keeps series and instance UIDs, so the same
    # part names another study. Mapped by part identity (same catalog) or by
    # fingerprint (rebuilt catalog), it must not take its primary flag into a
    # study that has one: the unique index would abort every later merge.
    p = told(tmp_path, _s1)
    p.write_catalog(2, [A_STUDY, B_STUDY, A2_STUDY], [TOPO, THIN, THICK, B_ONE, A2_ONE])
    p.merge()
    p.edit("makePrimary", target=["s_000003"], scope=["st_000001"])
    assert p.primary("st_000003") == [("s_000005",)]
    p.write_catalog(
        3,
        [A_STUDY, B_STUDY, A2_STUDY],
        [TOPO, THIN, THICK | {"study_uid": "1.2"}, B_ONE, A2_ONE],
        rebuild=rebuild,
    )
    p.merge()
    p.invariants()
    assert p.q(
        "SELECT study_key, selected, is_primary FROM series WHERE series_key = 's_000003'"
    ) == [("st_000003", 0, 0)]
    assert p.primary("st_000003") == [("s_000005",)]
    # The study it left returns to the automatic choice, and says why.
    assert p.q("SELECT selection_mode FROM studies WHERE study_key = 'st_000001'") == [("auto",)]
    assert p.primary("st_000001") == [("s_000002",)]
    assert ("check.primary_gone",) in p.q(
        "SELECT code FROM index_checks WHERE object_key = 'st_000001'"
    )


@pytest.mark.parametrize("destination", ["deselected_entirely", "with_its_own_primary"])
def test_a_series_that_moves_into_a_study_chosen_by_hand_arrives_unselected(
    tmp_path: Path, destination: str
) -> None:
    # The study it joins keeps exactly the user's choice: a moved series that
    # stayed selected would stand without a primary in a study the user had
    # emptied, or add to one the user had chosen, and no later merge repairs
    # a study in user mode.
    p = told(tmp_path, _s1)
    p.write_catalog(2, [A_STUDY, B_STUDY, A2_STUDY], [TOPO, THIN, THICK, B_ONE, A2_ONE])
    p.merge()
    if destination == "deselected_entirely":
        p.edit("deselect", target=["s_000005"], scope=["st_000003"])
        # The auto primary of 1.1 moves, and is now the best series of 1.2.
        moved, mover = "s_000002", THIN | {"study_uid": "1.2"}
        stays = A2_ONE | {"auto_rank": 2, "auto_selected": 0}
        expected = [("s_000002", 0, 0, "auto"), ("s_000005", 0, 0, "user")]
        notice = '{"count":1,"would_be_chosen":1}'
        parts = [TOPO, mover, THICK, B_ONE, stays]
    else:
        p.edit("makePrimary", target=["s_000005"], scope=["st_000003"])
        p.edit("select", target=["s_000003"], scope=["st_000001"])
        moved, mover = "s_000003", THICK | {"study_uid": "1.2"}
        expected = [("s_000003", 0, 0, "auto"), ("s_000005", 1, 1, "user")]
        notice = '{"count":1,"would_be_chosen":0}'
        parts = [TOPO, THIN, mover, B_ONE, A2_ONE]
    p.write_catalog(3, [A_STUDY, B_STUDY, A2_STUDY], parts)
    p.merge()
    p.invariants()
    assert p.q("SELECT study_key FROM series WHERE series_key = ?", moved) == [("st_000003",)]
    assert (
        p.q(
            "SELECT series_key, selected, is_primary, selection_origin FROM series "
            "WHERE study_key = 'st_000003' ORDER BY 1"
        )
        == expected
    )
    assert p.q("SELECT selection_mode FROM studies WHERE study_key = 'st_000003'") == [("user",)]
    assert p.q(
        "SELECT params_json FROM index_checks "
        "WHERE object_key = 'st_000003' AND code = 'check.new_series_since_manual'"
    ) == [(notice,)]


def test_a_known_series_under_a_refused_study_becomes_gone(tmp_path: Path) -> None:
    # After Remove Identifiers a study the project does not know is refused;
    # a known series whose files now name that study has nowhere to go.
    p = told(tmp_path, _s1)
    _remove_identifiers(p)
    p.write_catalog(
        2,
        [_without_ids(A_STUDY), B_STUDY, NEW_STUDY],
        [TOPO, THIN, THICK | {"study_uid": "3.1"}, B_ONE],
        link_key_id=None,
        rebuild=True,
    )
    p.merge(keep=0, link_key_id=None)
    p.invariants()
    assert p.q("SELECT count(*) FROM series WHERE series_key = 's_000003'") == [(0,)]
    assert p.q("SELECT count(*) FROM studies WHERE study_uid = '3.1'") == [(0,)]
    assert p.q("SELECT series_key FROM series WHERE index_state = 'current' ORDER BY 1") == [
        ("s_000001",),
        ("s_000002",),
        ("s_000004",),
    ]


def test_a_cohort_applied_after_a_study_merge_keeps_one_primary(tmp_path: Path) -> None:
    p = told(tmp_path, _s1)
    p.write_catalog(2, [A_STUDY, B_STUDY, A2_STUDY], [TOPO, THIN, THICK, B_ONE, A2_ONE])
    p.merge()
    studies = ["st_000001", "st_000002", "st_000003"]
    p.edit("saveCohort", scope=studies, label="Before the study merge")
    assert p.q("SELECT series_key FROM cohort_series WHERE is_primary = 1 ORDER BY 1") == [
        ("s_000002",),
        ("s_000005",),
    ]
    # Both primaries of the cohort end up in one study.
    p.write_catalog(
        3,
        [A_STUDY, B_STUDY, A2_STUDY],
        [
            TOPO,
            THIN | {"study_uid": "1.2", "auto_rank": 2, "auto_selected": 0},
            THICK,
            B_ONE,
            A2_ONE,
        ],
    )
    p.merge()
    p.edit("applyCohort", scope=studies, cohort_id=1)
    p.invariants()
    assert p.q(
        "SELECT series_key, selected, is_primary FROM series WHERE study_key = 'st_000003' "
        "ORDER BY 1"
    ) == [("s_000002", 1, 0), ("s_000005", 1, 1)]


# ---------------------------------------------------------------- identity edits

ANON_17A = study("5.1", sex="F", age_years=40.0, study_date="2020-03-01")
ANON_18 = study("5.3", sex="M", age_years=55.0, study_date="2020-06-01")
ANON_17B = study("5.2", sex="F", age_years=41.0, study_date="2021-03-01")
DICOM_6 = study(
    "6.1", pid_link=PID_6, pid_state="present", sex="F", age_years=70.0, study_date="2019-01-01"
)
DICOM_7 = study(
    "7.1", pid_link=PID_7, pid_state="present", sex="F", age_years=71.0, study_date="2020-09-01"
)
IDENTITY_STUDIES = [ANON_17A, ANON_18, ANON_17B, DICOM_6, DICOM_7]
IDENTITY_SERIES = [
    chosen(1, "5.1.1", "5.1"),
    chosen(2, "5.3.1", "5.3"),
    chosen(3, "5.2.1", "5.2"),
    chosen(4, "6.1.1", "6.1"),
    chosen(5, "7.1.1", "7.1"),
]
IDENTITY_PENDING = [("6.1", "00066", "ACC66", "dicom", 1), ("7.1", "00077", "ACC77", "dicom", 1)]


def _folders(uid: str, label: str, link: str) -> list[tuple[Any, ...]]:
    return [(uid, 1, 0, "SourceA", FOLDER_SOURCE), (uid, 1, 1, label, link)]


IDENTITY_CANDIDATES = [
    *_folders("5.1", "CASE_017", FOLDER_017),
    *_folders("5.3", "CASE_018", FOLDER_018),
    *_folders("5.2", "CASE_017", FOLDER_017),
]


def anonymous_folders(tmp_path: Path) -> Project:
    """Three anonymous studies in two case folders, and two patients with a
    PatientID. Keys follow the study dates:

    pt_000001 6.1 (PID 6), pt_000002 5.1 (CASE_017), pt_000003 5.3 (CASE_018),
    pt_000004 7.1 (PID 7), pt_000005 5.2 (CASE_017); study and series keys
    run in the same order."""
    p = Project(tmp_path)
    p.write_catalog(
        1,
        IDENTITY_STUDIES,
        IDENTITY_SERIES,
        pending=IDENTITY_PENDING,
        candidates=IDENTITY_CANDIDATES,
    )
    p.merge()
    p.invariants()
    assert p.q(
        "SELECT st.study_uid, p.patient_key, p.id_status, s.series_key, s.selected "
        "FROM studies AS st JOIN patients AS p USING (patient_key) "
        "JOIN series AS s USING (study_key) ORDER BY st.study_key"
    ) == [
        ("6.1", "pt_000001", "dicom", "s_000001", 1),
        ("5.1", "pt_000002", "unconfirmed", "s_000002", 0),
        ("5.3", "pt_000003", "unconfirmed", "s_000003", 0),
        ("7.1", "pt_000004", "dicom", "s_000004", 1),
        ("5.2", "pt_000005", "unconfirmed", "s_000005", 0),
    ]
    return p


def test_a_study_without_an_id_is_held_only_when_a_folder_id_is_offered(tmp_path: Path) -> None:
    # ADR 0024: unconfirmed means no ID and folder candidates to confirm.
    # With folder IDs switched off the catalog offers none, and holding the
    # study would leave it waiting for an ID typed by hand.
    p = Project(tmp_path)
    p.write_catalog(
        1,
        [
            study("8.1", study_date="2020-01-01"),
            study("8.2", pid_state="placeholder", study_date="2020-02-01"),
            study("8.3", study_date="2020-03-01"),
        ],
        [chosen(1, "8.1.1", "8.1"), chosen(2, "8.2.1", "8.2"), chosen(3, "8.3.1", "8.3")],
        candidates=_folders("8.3", "CASE_017", FOLDER_017),
    )
    p.merge()
    p.invariants()
    assert p.q(
        "SELECT st.study_uid, p.id_status, s.selected, s.is_primary FROM studies AS st "
        "JOIN patients AS p USING (patient_key) JOIN series AS s USING (study_key) ORDER BY 1"
    ) == [
        ("8.1", "unlinked", 1, 1),
        ("8.2", "unlinked", 1, 1),
        ("8.3", "unconfirmed", 0, 0),
    ]


LEVEL_1 = [
    ("st_000002", FOLDER_017, "CASE_017", "folder"),
    ("st_000005", FOLDER_017, "CASE_017", "folder"),
    ("st_000003", FOLDER_018, "CASE_018", "folder"),
]


def _patients(p: Project) -> list[tuple[Any, ...]]:
    """Each patient with its studies, joined in key order."""
    rows: dict[tuple[Any, ...], list[str]] = {}
    for *patient, study_key in p.q(
        "SELECT p.patient_key, p.pseudonym, p.id_status, p.sex, st.study_key "
        "FROM patients AS p JOIN studies AS st USING (patient_key) ORDER BY 1, 5"
    ):
        rows.setdefault(tuple(patient), []).append(study_key)
    return [(*patient, ",".join(keys)) for patient, keys in rows.items()]


def _sex_conflict(p: Project, patient_key: str) -> list[str]:
    # The merge lists the values in the order it meets the studies.
    return [
        sorted(json.loads(r[0])["values"].split(","))
        for r in p.q(
            "SELECT params_json FROM index_checks WHERE object_key = ? "
            "AND code = 'check.sex_conflict'",
            patient_key,
        )
    ]


def test_confirm_folder_level_makes_one_patient_per_folder(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    # A row whose patient is not unconfirmed (the sheet was read before
    # something else changed it) is left out, not moved.
    stale = ("st_000001", FOLDER_020, "CASE_020", "folder")
    left = p.identity("confirmFolderLevel", confirm=[*LEVEL_1, stale], level=1)
    p.invariants()
    assert left == 0
    assert _patients(p) == [
        ("pt_000001", "P0001", "dicom", "F", "st_000001"),
        ("pt_000002", "P0002", "confirmed", "F", "st_000002,st_000005"),
        ("pt_000003", "P0003", "confirmed", "M", "st_000003"),
        ("pt_000004", "P0004", "dicom", "F", "st_000004"),
    ]
    assert p.q("SELECT link, patient_key FROM patient_links ORDER BY patient_key, link") == [
        (PID_6, "pt_000001"),
        (FOLDER_017, "pt_000002"),
        (FOLDER_018, "pt_000003"),
        (PID_7, "pt_000004"),
    ]
    assert p.q(
        "SELECT patient_key, patient_id, id_source FROM identifiers ORDER BY patient_key"
    ) == [
        ("pt_000001", "00066", "dicom"),
        ("pt_000002", "CASE_017", "folder"),
        ("pt_000003", "CASE_018", "folder"),
        ("pt_000004", "00077", "dicom"),
    ]
    # The held studies take their automatic choice.
    assert p.q(
        "SELECT series_key FROM series WHERE selected = 1 AND is_primary = 1 ORDER BY 1"
    ) == [
        ("s_000001",),
        ("s_000002",),
        ("s_000003",),
        ("s_000004",),
        ("s_000005",),
    ]
    assert p.q("SELECT age_at_first_study FROM patients WHERE patient_key = 'pt_000002'") == [
        (40.0,)
    ]
    assert p.audit("ids_confirmed") == [{"patients": 2, "studies": 3, "level": 1}]
    assert dict(p.q("SELECT kind, last FROM key_counters"))["patient"] == 5


def test_confirm_without_identifiers_writes_links_but_no_identifiers(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    p.identity("confirmFolderLevel", confirm=LEVEL_1, level=1, keep=0)
    p.invariants()
    assert p.q("SELECT patient_key FROM identifiers ORDER BY 1") == [
        ("pt_000001",),
        ("pt_000004",),
    ]
    assert p.q("SELECT count(*) FROM patient_links WHERE link LIKE 'folder:%'") == [(2,)]


def test_after_confirmation_later_studies_in_the_folder_join_its_patient(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    p.identity("confirmFolderLevel", confirm=LEVEL_1, level=1)
    later = study("5.4", sex="F", age_years=42.0, study_date="2022-03-01")
    other = study("5.5", sex="M", age_years=30.0, study_date="2022-05-01")
    p.write_catalog(
        2,
        [*IDENTITY_STUDIES, later, other],
        [*IDENTITY_SERIES, chosen(6, "5.4.1", "5.4"), chosen(7, "5.5.1", "5.5")],
        pending=IDENTITY_PENDING,
        candidates=[
            *IDENTITY_CANDIDATES,
            *_folders("5.4", "CASE_017", FOLDER_017),
            *_folders("5.5", "CASE_020", FOLDER_020),
        ],
    )
    p.merge()
    p.invariants()
    assert p.q(
        "SELECT st.study_key, st.patient_key, s.selected FROM studies AS st "
        "JOIN series AS s USING (study_key) WHERE st.study_uid IN ('5.4', '5.5') ORDER BY 1"
    ) == [("st_000006", "pt_000002", 1), ("st_000007", "pt_000006", 0)]

    # The user then names CASE_017 for the study in CASE_020: it joins the
    # patient that holds that link, whose status does not change.
    p.identity(
        "confirmFolderLevel", confirm=[("st_000007", FOLDER_017, "CASE_017", "folder")], level=1
    )
    p.invariants()
    assert _patients(p)[1] == (
        "pt_000002",
        "P0002",
        "confirmed",
        None,
        "st_000002,st_000005,st_000006,st_000007",
    )
    assert p.q("SELECT count(*) FROM patients WHERE patient_key = 'pt_000006'") == [(0,)]
    # F and M: sex is left empty and the conflict is said; the ages fit no
    # single birth date.
    assert p.q(
        "SELECT code, params_json FROM index_checks WHERE object_key = 'pt_000002' ORDER BY 1"
    ) == [
        ("check.age_inconsistent", '{"studies":4}'),
        ("check.sex_conflict", '{"values":"F,M"}'),
    ]
    assert p.q("SELECT selected, is_primary FROM series WHERE series_key = 's_000007'") == [(1, 1)]
    # The next merge agrees with what the edit wrote.
    p.write_catalog(
        3,
        [*IDENTITY_STUDIES, later, other],
        [*IDENTITY_SERIES, chosen(6, "5.4.1", "5.4"), chosen(7, "5.5.1", "5.5")],
        candidates=IDENTITY_CANDIDATES,
    )
    p.merge()
    p.invariants()
    assert _patients(p)[1][3] is None
    assert _sex_conflict(p, "pt_000002") == [["F", "M"]]


def test_assign_moves_a_held_study_and_selects_it(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    p.identity("assignStudy", assign=[("st_000003", "pt_000001")])
    p.invariants()
    assert p.q("SELECT patient_key FROM studies WHERE study_key = 'st_000003'") == [("pt_000001",)]
    assert p.q("SELECT count(*) FROM patients WHERE patient_key = 'pt_000003'") == [(0,)]
    assert p.q("SELECT selected, is_primary FROM series WHERE series_key = 's_000003'") == [(1, 1)]
    assert p.q("SELECT sex FROM patients WHERE patient_key = 'pt_000001'") == [(None,)]
    assert p.q("SELECT code FROM index_checks WHERE object_key = 'pt_000001' ORDER BY 1") == [
        ("check.age_inconsistent",),
        ("check.sex_conflict",),
    ]
    assert p.audit("studies_assigned") == [{"studies": 1, "patients_removed": 1}]


def test_assign_carries_the_links_of_an_emptied_patient(tmp_path: Path) -> None:
    # Two PatientIDs for one person: once the user says so, a later study with
    # either ID joins the same patient.
    p = anonymous_folders(tmp_path)
    p.identity("assignStudy", assign=[("st_000004", "pt_000001")])
    p.invariants()
    assert p.q("SELECT link, patient_key FROM patient_links ORDER BY link") == [
        (PID_6, "pt_000001"),
        (PID_7, "pt_000001"),
    ]
    assert p.q("SELECT patient_key FROM identifiers") == [("pt_000001",)]
    assert p.q("SELECT sex FROM patients WHERE patient_key = 'pt_000001'") == [("F",)]
    again = DICOM_7 | {"study_uid": "7.2", "study_date": "2023-01-01", "age_years": 73.0}
    p.write_catalog(
        2,
        [*IDENTITY_STUDIES, again],
        [*IDENTITY_SERIES, chosen(6, "7.2.1", "7.2")],
        pending=[*IDENTITY_PENDING, ("7.2", "00077", "ACC78", "dicom", 2)],
        candidates=IDENTITY_CANDIDATES,
    )
    p.merge()
    p.invariants()
    assert p.q("SELECT patient_key FROM studies WHERE study_uid = '7.2'") == [("pt_000001",)]
    assert p.q("SELECT patient_id, accession_numbers FROM identifiers") == [
        ("00066", '["ACC66","ACC77","ACC78"]')
    ]


def test_assign_keeps_a_choice_by_hand(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    p.edit("deselect", target=["s_000004"], scope=["st_000004"])
    p.identity("assignStudy", assign=[("st_000004", "pt_000001")])
    p.invariants()
    assert p.q("SELECT selected FROM series WHERE series_key = 's_000004'") == [(0,)]


def _reason(p: Project, series_key: str) -> dict[str, Any]:
    return dict(
        json.loads(
            p.q("SELECT selection_reason FROM series WHERE series_key = ?", series_key)[0][0]
        )
    )


def test_assign_to_an_unconfirmed_patient_holds_the_study(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    before = _reason(p, "s_000004")
    p.identity("assignStudy", assign=[("st_000004", "pt_000003")])
    p.invariants()
    assert p.q("SELECT selected, is_primary FROM series WHERE series_key = 's_000004'") == [(0, 0)]
    held = _reason(p, "s_000004")
    assert (held["outcome"], held["if_confirmed"]) == ("held", before)
    # Assigned to a patient with a PatientID, it is released with the reason
    # it had (its own patient went when it was emptied).
    p.identity("assignStudy", assign=[("st_000004", "pt_000001")])
    p.invariants()
    assert _reason(p, "s_000004") == before


def test_a_confirmation_releases_the_held_reason(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    held = _reason(p, "s_000003")
    assert held["outcome"] == "held"
    p.identity("confirmFolderLevel", confirm=LEVEL_1, level=1)
    p.invariants()
    assert _reason(p, "s_000003") == held["if_confirmed"]


def test_a_typed_id_matching_nothing_creates_a_confirmed_patient(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    left = p.identity(
        "createTypedPatient", "assignStudy", typed=[("st_000003", PID_TYPED, "T-0042")]
    )
    p.invariants()
    assert left == 0
    assert _patients(p)[-1] == ("pt_000006", "P0006", "confirmed", "M", "st_000003")
    assert p.q("SELECT count(*) FROM patients WHERE patient_key = 'pt_000003'") == [(0,)]
    assert p.q("SELECT patient_key FROM patient_links WHERE link = ?", PID_TYPED) == [
        ("pt_000006",)
    ]
    assert p.q("SELECT patient_id, id_source FROM identifiers WHERE patient_key = 'pt_000006'") == [
        ("T-0042", "typed")
    ]
    assert p.q("SELECT selected, is_primary FROM series WHERE series_key = 's_000003'") == [(1, 1)]
    assert dict(p.q("SELECT kind, last FROM key_counters")) == {
        "patient": 6,
        "study": 5,
        "series": 5,
        "pseudonym": 6,
    }
    assert p.audit("patients_created") == [{"patients": 1, "id_source": "typed"}]
    assert p.audit("studies_assigned") == [{"studies": 1, "patients_removed": 1}]


def test_a_typed_id_matching_a_link_moves_the_study_to_its_patient(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    p.identity("createTypedPatient", "assignStudy", typed=[("st_000002", PID_6, "00066")])
    p.invariants()
    assert p.q("SELECT patient_key FROM studies WHERE study_key = 'st_000002'") == [("pt_000001",)]
    assert p.q("SELECT count(*) FROM patients") == [(4,)]
    assert p.audit("patients_created") == []
    assert dict(p.q("SELECT kind, last FROM key_counters"))["patient"] == 5


def test_one_typed_id_for_two_studies_makes_one_patient(tmp_path: Path) -> None:
    p = anonymous_folders(tmp_path)
    p.identity(
        "createTypedPatient",
        "assignStudy",
        typed=[("st_000002", PID_7_NEW, "B-17"), ("st_000005", PID_7_NEW, "B-17")],
        keep=0,
    )
    p.invariants()
    assert _patients(p)[-1] == ("pt_000006", "P0006", "confirmed", "F", "st_000002,st_000005")
    assert p.q("SELECT count(*) FROM identifiers WHERE patient_key = 'pt_000006'") == [(0,)]
    assert b"B-17" not in p.path.read_bytes()


# ---------------------------------------------------------------- a PatientID that changes

_LINK_CHANGED = (
    "SELECT object_key, params_json FROM index_checks "
    "WHERE object_kind = 'study' AND code = 'check.patient_link_changed' ORDER BY 1"
)


def test_a_known_study_whose_patient_id_changes_keeps_its_patient_and_says_so(
    tmp_path: Path,
) -> None:
    # ADR 0024 decision 4: a correction or a merge of patients in the PACS,
    # or files exported again with an ID where there was none. The study
    # stays where it is, a held study stays held, and the warning tells the
    # user, who decides with "Assign to Patient…".
    p = anonymous_folders(tmp_path)
    later_6 = DICOM_6 | {"study_uid": "6.2", "study_date": "2022-01-01", "age_years": 73.0}
    studies = [
        ANON_17A,
        ANON_18 | {"pid_link": PID_A, "pid_state": "present"},
        ANON_17B,
        DICOM_6 | {"pid_link": PID_7},
        DICOM_7 | {"pid_link": PID_7_NEW},
        later_6,
    ]
    parts = [*IDENTITY_SERIES, chosen(6, "6.2.1", "6.2")]
    pending = [
        ("6.1", "00077", "ACC66", "dicom", 2),
        ("7.1", "00078", "ACC77", "dicom", 2),
        ("6.2", "00066", "ACC62", "dicom", 2),
    ]
    # Only studies without a usable PatientID get folder candidates.
    candidates = [
        *_folders("5.1", "CASE_017", FOLDER_017),
        *_folders("5.2", "CASE_017", FOLDER_017),
    ]
    p.write_catalog(2, studies, parts, pending=pending, candidates=candidates)
    p.merge()
    p.invariants()
    assert p.q(
        "SELECT study_uid, patient_key FROM studies WHERE study_uid IN ('6.1', '7.1', '5.3', '6.2') "
        "ORDER BY study_key"
    ) == [("6.1", "pt_000001"), ("5.3", "pt_000003"), ("7.1", "pt_000004"), ("6.2", "pt_000001")]
    assert p.q(_LINK_CHANGED) == [
        ("st_000001", '{"other_patient":1}'),
        ("st_000003", '{"other_patient":0}'),
        ("st_000004", '{"other_patient":0}'),
    ]
    # The held study stays held, and a link the files name for a known study
    # is not given to anyone: only the user can say whose it is.
    assert p.q("SELECT selected FROM series WHERE series_key = 's_000003'") == [(0,)]
    assert p.q("SELECT link, patient_key FROM patient_links ORDER BY patient_key, link") == [
        (PID_6, "pt_000001"),
        (PID_7, "pt_000004"),
    ]
    # identifiers keeps the ID it had; a new study with that ID joins its patient.
    assert p.q("SELECT patient_key, patient_id, accession_numbers FROM identifiers ORDER BY 1") == [
        ("pt_000001", "00066", '["ACC62","ACC66"]'),
        ("pt_000004", "00077", '["ACC77"]'),
    ]

    # The user moves 6.1 to the patient its ID now names, and keeps 7.1 with
    # P0001 against its ID. The edit cannot read the files, so both warnings
    # go; the next merge raises again the one the files still contradict.
    p.identity("assignStudy", assign=[("st_000001", "pt_000004"), ("st_000004", "pt_000001")])
    p.invariants()
    assert p.q(_LINK_CHANGED) == [("st_000003", '{"other_patient":0}')]
    p.write_catalog(3, studies, parts, pending=pending, candidates=candidates)
    p.merge()
    p.invariants()
    assert p.q(_LINK_CHANGED) == [
        ("st_000003", '{"other_patient":0}'),
        ("st_000004", '{"other_patient":0}'),
    ]

    # Links made with another key say nothing about any patient.
    p.write_catalog(
        4, studies, parts, pending=pending, candidates=candidates, link_key_id="0123456789abcdef"
    )
    p.merge()
    p.invariants()
    assert p.q(_LINK_CHANGED) == []


# ---------------------------------------------------------------- the catalog stays read-only

_WRITES = frozenset(
    {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_ALTER_TABLE,
    }
)


def test_the_merge_never_writes_the_catalog(tmp_path: Path) -> None:
    # Where the system SQLite does not take URI filenames, the app attaches
    # the catalog by plain path, read-write; the worker may be writing it
    # the next moment, so the merge must not try, whatever the attach mode.
    p = Project(tmp_path)
    p.write_catalog(
        1,
        [A_STUDY, B_STUDY],
        [TOPO, THIN, THICK, B_ONE],
        pending=[("1.1", "00012345", "ACC1", "dicom", 1)],
        candidates=[("2.1", 1, 1, "CASE_017", FOLDER_017)],
        checks=[("series", "2", "check.gap", "warning", "{}")],
    )
    attempts: list[tuple[int, str | None]] = []

    def guard(action: int, arg1: str | None, arg2: str | None, db: str | None, _: Any) -> int:
        if db == "idx" and action in _WRITES:
            attempts.append((action, arg1))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with closing(p.connect()) as db:
        db.execute("ATTACH DATABASE ? AS idx", (str(p.catalog),))
        db.executescript(INDEX["mergeInputs"])
        db.execute("INSERT INTO temp.merge_params VALUES (?, 1, 'refuse', ?)", (NOW, LINK_KEY_ID))
        db.set_authorizer(guard)
        _transaction(db, INDEX["merge"], INDEX["patientAges"])
        assert attempts == []
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            db.execute("DELETE FROM idx.cat_series")
        db.set_authorizer(None)
        db.execute("DETACH DATABASE idx")
    assert p.q("SELECT count(*) FROM series") == [(4,)]


# ---------------------------------------------------------------- scale


def test_merging_20000_series_takes_at_most_3_seconds(tmp_path: Path) -> None:
    # 5 000 patients with one study of four series each: a large cohort. The
    # budget holds for the first merge and for an unchanged rescan, which
    # maps every part again.
    p = Project(tmp_path)
    count = 5000

    def write(generation: int) -> None:
        with closing(open_catalog(p.catalog)) as db:
            db.execute("BEGIN IMMEDIATE")
            for statement in _CLEAR_DERIVED:
                db.execute(statement)
            db.executemany(
                _INSERT_STUDY,
                (
                    study(
                        f"1.{i}",
                        pid_link=f"pid:{i:064x}",
                        pid_state="present",
                        sex="F",
                        age_years=60.0,
                        study_date="2020-01-01",
                    )
                    for i in range(count)
                ),
            )
            db.executemany(
                _INSERT_SERIES,
                (
                    series(
                        i * 4 + j + 1,
                        f"1.{i}.{j}",
                        f"1.{i}",
                        auto_rank=j + 1,
                        auto_selected=int(j == 0),
                    )
                    for i in range(count)
                    for j in range(4)
                ),
            )
            db.executemany(
                "INSERT INTO pending_identifiers VALUES (?, ?, ?, 'dicom', ?)",
                ((f"1.{i}", f"ID{i:05d}", f"A{i:05d}", generation) for i in range(count)),
            )
            db.executemany(
                "INSERT INTO cat_checks VALUES ('series', ?, 'check.gap', 'warning', '{}')",
                ((str(i * 4 + 1),) for i in range(count)),
            )
            db.executemany(
                "INSERT OR REPLACE INTO catalog_meta (key, value) VALUES (?, ?)",
                [("generation", str(generation)), ("complete", "1"), ("link_key_id", LINK_KEY_ID)],
            )
            db.execute("COMMIT")

    for generation in (1, 2):
        write(generation)
        started = time.perf_counter()
        p.merge()
        elapsed = time.perf_counter() - started
        assert elapsed <= 3.0, f"merge of generation {generation} took {elapsed:.2f} s"
    p.invariants()
    assert p.q("SELECT count(*), sum(selected), sum(is_primary) FROM series") == [
        (4 * count, count, count)
    ]
    assert dict(p.q("SELECT kind, last FROM key_counters"))["series"] == 4 * count
    assert p.audit("index_merged")[-1]["new_series"] == 0
