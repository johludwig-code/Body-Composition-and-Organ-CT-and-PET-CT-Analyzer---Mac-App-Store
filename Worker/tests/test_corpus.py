"""The synthetic corpus and what the index must make of it (ADR 0026 decision 1).

Two builds are the same bytes, every scenario of `corpus_expected.json` is in
the tree, the facts of the generator the expectation relies on still hold,
every code it names is registered, and `corpus_check.compare` reads a merged
project the way the expectation means it. Then the index job scans the
corpus, `IndexSQL.merge` merges its catalog into a new project, and the
project must be exactly what the expectation says, with the default
selection settings and with `accept_derived_primary`.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import sqlite3
import stat
import struct
import unicodedata
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import dicom_factory
from bcoa_worker.index.catalog import open_catalog
from bcoa_worker.index.identity import decode_link_key, link_key_id, pid_link
from conftest import PROTOCOL_ROOT
from corpus_check import (
    compare,
    load_expected,
    merge,
    new_project,
    registry,
    resolve,
    totals_key,
)
from index_jobs import run_index

EXPECTED = load_expected()
CODES = registry()


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Any, Any]]:
    """Two builds of the corpus, made once for the module: a build takes a
    few seconds."""
    built = [
        dicom_factory.build_corpus(tmp_path_factory.mktemp("corpus") / name)
        for name in ("first", "second")
    ]
    try:
        yield built[0], built[1]
    finally:
        # Mode 000 entries would keep pytest from removing the folders.
        for corpus in built:
            corpus.unlock()


def _scenario(name: str, data: dict[str, Any] = EXPECTED) -> dict[str, Any]:
    return next(s for s in data["scenarios"] if s["name"] == name)


def _all(data: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    for scenario in data["scenarios"]:
        for patient in scenario["patients"]:
            for study in patient["studies"]:
                yield scenario, patient, study


# ------------------------------------------------------------------ the generator


def test_two_builds_are_the_same_bytes(corpora: tuple[Any, Any]) -> None:
    first, second = corpora
    assert first.features == second.features
    modes = [
        sorted((p.relative_to(c.root), stat.S_IMODE(os.lstat(p).st_mode)) for p in c.locked)
        for c in (first, second)
    ]
    assert modes[0] == modes[1]
    first.unlock()
    second.unlock()
    digest = dicom_factory.tree_digest(first.root)
    assert digest == dicom_factory.tree_digest(second.root)
    # The tree is the corpus, not an empty folder that compares equal.
    assert len(digest) > 1900


def test_another_seed_gives_other_uids() -> None:
    a = dicom_factory._Writer(Path(), dicom_factory.SEED)
    b = dicom_factory._Writer(Path(), "another seed")
    assert a.uid("study") != b.uid("study")
    assert a.uid("study") == dicom_factory._Writer(Path(), dicom_factory.SEED).uid("study")
    # 2.25 and a 128-bit integer: at most 44 characters, within the 64 of a UI.
    assert a.uid("study").startswith("2.25.") and len(a.uid("study")) <= 44


def _headers(root: Path) -> tuple[set[tuple[str, str]], set[str]]:
    """(StudyDescription, SeriesDescription) and normalized PatientID of every
    DICOM file below `root` that pydicom reads."""
    series: set[tuple[str, str]] = set()
    patients: set[str] = set()
    for directory, _, files in os.walk(os.fsencode(root)):
        for name in files:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                continue
            try:
                ds = dicom_factory.read_header(os.fsdecode(path))
            except Exception:  # noqa: S112 - junk is part of the corpus
                continue
            if "SeriesDescription" in ds:
                series.add((str(ds.get("StudyDescription", "")), str(ds.SeriesDescription)))
            if ds.get("PatientID"):
                patients.add(unicodedata.normalize("NFC", str(ds.PatientID).strip(" \x00")))
    return series, patients


def _nifti_files(folder: Path) -> set[tuple[str, int]]:
    """(modality by name, first dimension) of every NIfTI-1 file in `folder`."""
    found = set()
    for path in folder.iterdir():
        data = path.read_bytes()
        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                continue
        if len(data) < 348 or data[344:347] != b"n+1":
            continue
        rows = struct.unpack_from("<8h", data, 40)[1]
        prefix = path.name[:3]
        found.add(({"CT_": "CT", "PT_": "PT"}.get(prefix, "OT"), rows))
    return found


def test_every_scenario_is_in_the_tree(corpora: tuple[Any, Any]) -> None:
    corpus = corpora[0]
    want = resolve(EXPECTED, features=corpus.features)
    series, patient_ids = _headers(corpus.root)
    nifti = _nifti_files(corpus.root / "source1" / "nifti")
    for scenario in want["scenarios"]:
        for folder in scenario["folders"]:
            assert (corpus.root / folder).is_dir(), (scenario["name"], folder)
        for patient in scenario["patients"]:
            if "patient_id" in patient["match"]:
                pid = patient["match"]["patient_id"]
                assert pid in patient_ids or any(
                    pid in name for name in os.listdir(corpus.root / "source1" / "nifti")
                ), pid
        for _, _, study in _all({"scenarios": [scenario]}):
            for part in study["parts"]:
                description = study["match"].get("description")
                if description is None:
                    # A NIfTI study: named by its matrix.
                    assert (part["match"]["modality"], part["match"]["image_rows"]) in nifti
                else:
                    assert (description, part["match"]["description"]) in series, (
                        scenario["name"],
                        description,
                        part["match"],
                    )


def test_the_features_follow_the_platform(corpora: tuple[Any, Any]) -> None:
    corpus = corpora[0]
    assert corpus.features <= {dicom_factory.PERMISSIONS, dicom_factory.NON_UTF8_NAMES}
    # Root reads a file of mode 000, so the permission cases are left out there
    # instead of being expected to fail.
    if os.geteuid() == 0:
        assert dicom_factory.PERMISSIONS not in corpus.features
    if dicom_factory.PERMISSIONS in corpus.features:
        assert len(corpus.locked) == 2
    latin1 = corpus.root / "source1" / "nfd" / "latin1"
    assert latin1.exists() == (dicom_factory.NON_UTF8_NAMES in corpus.features)


def test_a_bulk_tree_has_the_files_asked_for(tmp_path: Path) -> None:
    written = dicom_factory.write_bulk(tmp_path, 12, per_series=5, private_groups=True)
    files = sorted(p for p in tmp_path.rglob("*.dcm"))
    assert written == len(files) == 12
    ds = dicom_factory.read_header(files[0])
    assert ds[0x00431028].VR == "OB" and len(ds[0x00431028].value) == 8192


# ------------------------------------------------------------------ the expectation


def test_the_expectation_follows_the_generator() -> None:
    writer = dicom_factory._Writer(Path(), dicom_factory.SEED)
    assert EXPECTED["generator"]["seed"] == dicom_factory.SEED
    private = writer.uid("private-syntax")
    assert EXPECTED["generator"]["private_syntax"] == private
    syntaxes = _scenario("transfer_syntaxes")["patients"][0]["studies"][0]["parts"]
    assert [p["fields"]["transfer_syntax_uid"] for p in syntaxes][-1] == private
    # The tie is decided by the series UID as text; the expectation names the
    # winner by description, so it must be the one whose UID sorts first.
    uids = {key: writer.uid("selection.tie", key, "series") for key in ("a", "b")}
    winner = min(uids, key=uids.__getitem__)
    tie = _scenario("selection.tie")["patients"][0]["studies"][0]["parts"]
    chosen = [p["match"]["description"] for p in tie if p["selection"]["auto_rank"] == 1]
    assert chosen == [f"Thorax {winner.upper()} 3.0 I30f"]
    job = EXPECTED["job"]
    assert link_key_id(decode_link_key(job["link_key"])) == job["link_key_id"]


def _checks_with_kind(data: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for source in data["sources"].values():
        for check in source["checks"]:
            yield "source", check
    for check in data["project_checks"]:
        yield "project", check
    for _, patient, study in _all(data):
        for check in patient["checks"]:
            yield "patient", check
        for check in study["checks"]:
            yield "study", check
        for part in study["parts"]:
            for check in part.get("checks", []) + part.get("checks_include", []):
                yield "series", check


def _reasons(data: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for _, _, study in _all(data):
        for part in study["parts"]:
            if "reason" in part:
                yield part["reason"]
    for variant in data["variants"].values():
        for override in variant["parts"]:
            if "reason" in override:
                yield override["reason"]


def test_every_expected_code_is_registered() -> None:
    for kind, check in _checks_with_kind(EXPECTED):
        entry = CODES.get(check["code"])
        assert entry is not None and entry["group"] == "check", check["code"]
        assert entry["object"] == kind, (check["code"], kind)
        if "params" in check:
            assert set(check["params"]) == set(entry["params"]), check
        if check["code"] == "check.split":
            assert CODES[check["params"]["reason"]]["group"] == "split_reason"
    for reason in _reasons(EXPECTED):
        assert reason["outcome"] in {"chosen", "eligible", "excluded", "held"}
        codes = reason.get("codes", reason.get("codes_include"))
        for code in codes:
            entry = CODES.get(code)
            assert entry is not None and entry["group"] == "select", code
            assert code.split(".")[1] == reason["outcome"], (code, reason["outcome"])
        if "codes" in reason:
            names = {name for code in codes for name in CODES[code]["params"]}
            assert set(reason["params"]) == names, reason
        if "orientation" in reason.get("params", {}):
            assert CODES[reason["params"]["orientation"]]["group"] == "orientation"


def test_names_in_the_expectation_are_unique() -> None:
    descriptions = [
        study["match"]["description"]
        for _, _, study in _all(EXPECTED)
        if study["match"].get("description") is not None
    ]
    assert len(descriptions) == len(set(descriptions))
    matches = [
        json.dumps(p["match"], sort_keys=True) for s in EXPECTED["scenarios"] for p in s["patients"]
    ]
    assert len(matches) == len(set(matches))
    for _, _, study in _all(EXPECTED):
        parts = [json.dumps(p["match"], sort_keys=True) for p in study["parts"]]
        assert len(parts) == len(set(parts)), study["match"]
    names = [s["name"] for s in EXPECTED["scenarios"]]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    "features", [frozenset(), frozenset({"non_utf8_names"}), frozenset({"permissions"})]
)
def test_the_totals_count_what_is_listed(features: frozenset[str]) -> None:
    want = resolve(EXPECTED, features=features)
    patients = sum(len(s["patients"]) for s in want["scenarios"])
    studies = sum(1 for _ in _all(want))
    parts = sum(len(study["parts"]) for _, _, study in _all(want))
    assert want["totals"][totals_key(features)] == {
        "patients": patients,
        "studies": studies,
        "series": parts,
        "pairs": len(want["pairs"]),
    }


def test_resolve_applies_features_and_variants() -> None:
    plain = resolve(EXPECTED)
    unreadable = [
        c for c in plain["sources"]["1"]["checks"] if c["code"] == "check.unreadable_files"
    ]
    assert [c["params"]["count"] for c in unreadable] == [2]
    assert "check.bad_dirs" not in {c["code"] for c in plain["sources"]["1"]["checks"]}
    locked = resolve(EXPECTED, features={"permissions"})
    unreadable = [
        c for c in locked["sources"]["1"]["checks"] if c["code"] == "check.unreadable_files"
    ]
    assert [c["params"]["count"] for c in unreadable] == [3]

    derived = resolve(EXPECTED, variant="accept_derived_primary")
    pet = next(
        s for _, _, s in _all(derived) if s["match"].get("description") == "PET-CT Whole Body"
    )
    reasons = {p["match"]["description"]: p["reason"]["codes"] for p in pet["parts"]}
    assert reasons["CT WB 5.0 Br38f"] == ["select.chosen.original"]
    assert reasons["CT WB 5.0 B30f LowDose"] == ["select.eligible.derived"]
    # The expectation itself is left as it was.
    assert resolve(EXPECTED) == plain


# ------------------------------------------------------------------ compare


def _catalog_from(want: dict[str, Any], path: Path, link_key: str) -> None:
    """A catalog generation that says exactly what `want` expects: the input
    the regroup must produce, written by hand."""
    key = decode_link_key(link_key)
    db = open_catalog(path)
    studies, parts, checks, pending, candidates = [], [], [], [], []
    part_ref = 0
    for number, (_, patient, study) in enumerate(_all(want), start=1):
        uid = f"2.25.{number}"
        facts = study.get("catalog", {})
        pid = patient["match"].get("patient_id")
        studies.append(
            (
                uid,
                pid_link(key, pid) if pid else None,
                facts.get("pid_state", "present"),
                patient["fields"]["sex"],
                study["fields"]["age_years"],
                study["fields"]["study_date"],
                study["match"].get("description"),
            )
        )
        if patient["identifiers"]:
            ids = patient["identifiers"]
            accession = ids["accession_numbers"][0] if ids["accession_numbers"] else None
            pending.append((uid, ids["patient_id"], accession, ids["id_source"], 1))
        for level in facts.get("candidate_levels", []):
            label = facts.get("candidate_labels", {}).get(str(level), "source1")
            link = "folder:" + hashlib.sha256(f"{uid}/{level}".encode()).hexdigest()
            candidates.append((uid, 1, level, label, link))
        for check in study["checks"]:
            checks.append(
                (
                    "study",
                    uid,
                    check["code"],
                    CODES[check["code"]]["level"],
                    json.dumps(check.get("params", {})),
                )
            )
        for part in study["parts"]:
            part_ref += 1
            reason = {"v": 1, **part["reason"]}
            row = {
                "part_ref": part_ref,
                "series_uid": f"2.25.{number}.{part['match']['description']}",
                "part": part["match"].get("part", 0),
                "study_uid": uid,
                "description": part["match"]["description"],
                "fingerprint": f"fp{part_ref}",
                "reason_json": json.dumps(reason),
                **part["fields"],
                **{
                    k: v
                    for k, v in part["selection"].items()
                    if k in ("auto_rank", "auto_selected")
                },
            }
            parts.append(row)
            for check in part.get("checks", []):
                checks.append(
                    (
                        "series",
                        str(part_ref),
                        check["code"],
                        CODES[check["code"]]["level"],
                        json.dumps(check.get("params", {})),
                    )
                )
    for source_id, source in want["sources"].items():
        for check in source["checks"]:
            checks.append(
                (
                    "source",
                    source_id,
                    check["code"],
                    CODES[check["code"]]["level"],
                    json.dumps(check["params"]),
                )
            )
    with closing(db):
        db.execute("BEGIN IMMEDIATE")
        db.executemany("INSERT INTO cat_studies VALUES (?, ?, ?, ?, ?, ?, ?)", studies)
        for row in parts:
            columns = ", ".join(row)
            marks = ", ".join(f":{name}" for name in row)
            # The column names are the expectation's own field names, and a
            # name that is no column fails here rather than being dropped.
            db.execute(f"INSERT INTO cat_series ({columns}) VALUES ({marks})", row)  # noqa: S608
        db.executemany("INSERT INTO cat_checks VALUES (?, ?, ?, ?, ?)", checks)
        db.executemany("INSERT INTO pending_identifiers VALUES (?, ?, ?, ?, ?)", pending)
        db.executemany("INSERT INTO cat_id_candidates VALUES (?, ?, ?, ?, ?)", candidates)
        db.executemany(
            "INSERT INTO scans (source_id, state, finished_at) VALUES (?, 'complete', ?)",
            [(int(source_id), "2026-10-09T12:00:00Z") for source_id in want["sources"]],
        )
        db.executemany(
            "INSERT OR REPLACE INTO catalog_meta (key, value) VALUES (?, ?)",
            [("generation", "1"), ("complete", "1"), ("link_key_id", link_key_id(key))],
        )
        db.execute("COMMIT")


def _fragment() -> dict[str, Any]:
    """Two scenarios of the real expectation: a ranking decided by a warning,
    and an anonymous study held for its unconfirmed patient."""
    anonymous = copy.deepcopy(_scenario("anonymous"))
    anonymous["patients"] = anonymous["patients"][:1]
    return {
        "scenarios": [copy.deepcopy(_scenario("selection.warnings")), anonymous],
        "pairs": [],
        "sources": {
            "1": {"checks": [{"code": "check.not_dicom", "params": {"count": 7}}]},
            "2": {"checks": []},
        },
        "project_checks": [],
        "catalog": {"files_by_kind": {}, "pixel_data": {}},
        "variants": {},
        "totals": {"without_features": {"patients": 2, "studies": 2, "series": 3, "pairs": 0}},
    }


@pytest.fixture
def merged(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    want = _fragment()
    catalog = tmp_path / "index" / "catalog.sqlite"
    _catalog_from(want, catalog, EXPECTED["job"]["link_key"])
    project = new_project(tmp_path / "project.sqlite", {1: "source1", 2: "source2"})
    merge(project, catalog, link_key_id=EXPECTED["job"]["link_key_id"])
    return project, catalog, want


def test_compare_accepts_the_project_the_merge_makes_of_the_expectation(
    merged: tuple[Path, Path, dict[str, Any]],
) -> None:
    project, catalog, want = merged
    assert compare(project, want, catalog_path=catalog) == []
    # The held study is what the merge makes of an eligible part of an
    # unconfirmed patient: ranked first, not selected (C6).
    with closing(sqlite3.connect(project)) as db:
        held = db.execute(
            "SELECT auto_rank, auto_selected, selected, is_primary FROM series "
            "WHERE description = 'Anon 017a 3.0 Br40'"
        ).fetchall()
    assert held == [(1, 1, 0, 0)]


def test_compare_names_every_kind_of_difference(merged: tuple[Path, Path, dict[str, Any]]) -> None:
    project, catalog, want = merged
    with closing(sqlite3.connect(project)) as db:
        db.execute(
            "UPDATE series SET selection_reason = json_set(selection_reason, '$.params.count', 2) "
            "WHERE description = 'Abdomen A 3.0 B30f'"
        )
        db.execute(
            "UPDATE series SET kernel_class = 'sharp' WHERE description = 'Abdomen B 3.0 B30f'"
        )
        b = db.execute(
            "SELECT series_key FROM series WHERE description = 'Abdomen B 3.0 B30f'"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO index_checks VALUES ('series', ?, 'check.gap', 'warning', "
            '\'{"gaps": 1, "missing": 1, "largest_mm": 6.0}\')',
            (b,),
        )
        db.execute("UPDATE index_checks SET level = 'info' WHERE code = 'check.no_rescale'")
        db.execute("DELETE FROM index_checks WHERE code = 'check.not_dicom'")
        db.execute("UPDATE identifiers SET accession_numbers = '[]'")
        db.execute("INSERT INTO patients (patient_key, pseudonym) VALUES ('pt_999999', 'P9999')")
        db.execute(
            "INSERT INTO studies (study_key, patient_key, study_uid, description) "
            "VALUES ('st_999999', 'pt_999999', '2.25.999', 'CT Hidden')"
        )
        db.commit()
    differences = compare(project, want, catalog_path=catalog)
    expected_fragments = [
        "params are",  # the reason's count
        'kernel_class is "sharp", expected "soft"',
        "unexpected check.gap",
        "check.no_rescale has level 'info'",
        "check.not_dicom missing",
        "accession_numbers are []",
        "unexpected patient with studies ['CT Hidden']",
        "totals: 3 patients, expected 2",
        "totals: 3 studies, expected 2",
    ]
    for fragment in expected_fragments:
        assert any(fragment in line for line in differences), (fragment, differences)
    assert len(differences) == len(expected_fragments), differences


def test_compare_finds_a_missing_part_and_an_unexpected_one(
    merged: tuple[Path, Path, dict[str, Any]],
) -> None:
    project, _, want = merged
    with closing(sqlite3.connect(project)) as db:
        db.execute(
            "UPDATE series SET description = 'Abdomen C' WHERE description = 'Abdomen A 3.0 B30f'"
        )
        db.commit()
    differences = compare(project, want)
    assert any("0 series match" in line for line in differences), differences
    assert any("unexpected series 'Abdomen C'" in line for line in differences), differences


# ------------------------------------------------------------------ the index


def _run_index_job(job: dict[str, Any]) -> None:
    """A scan of every source in the job followed by its regroup, as the
    worker runs them (ADR 0020 decision 10)."""
    code, events = run_index(job)
    assert code == 0, events[-2:]


def _index_and_merge(
    corpus: Any, folder: Path, *, selection: dict[str, Any] | None = None
) -> tuple[Path, Path]:
    """Index both sources of `corpus` into a new project and merge the
    catalog it writes; returns the project database and the catalog."""
    project_dir = folder / "Study.bcoaproj"
    project_dir.mkdir(parents=True)
    sources = {int(source_id): name for source_id, name in EXPECTED["job"]["sources"].items()}
    project = new_project(project_dir / "project.sqlite", sources)
    job = json.loads((PROTOCOL_ROOT / "fixtures" / "jobs" / "index_scan.json").read_text())
    job["project_dir"] = str(project_dir)
    job["log_path"] = str(project_dir / "logs" / "j_corpus.log")
    payload = job["payload"]
    payload["sources"] = [
        {"source_id": source_id, "root": str(root), "status": "active", "volume_kind": "local"}
        for source_id, root in sorted(corpus.sources.items())
    ]
    payload["scan_sources"] = sorted(corpus.sources)
    payload["merged_generation"] = 0
    payload["link_key"] = EXPECTED["job"]["link_key"]
    payload["link_key_id"] = EXPECTED["job"]["link_key_id"]
    payload["selection"].update(selection or {})
    _run_index_job(job)
    catalog = project_dir / payload["catalog"]
    merge(project, catalog, link_key_id=payload["link_key_id"])
    return project, catalog


def test_the_index_makes_the_expected_project(tmp_path: Path) -> None:
    corpus = dicom_factory.build_corpus(tmp_path / "corpus")
    try:
        project, catalog = _index_and_merge(corpus, tmp_path)
        differences = compare(project, EXPECTED, features=corpus.features, catalog_path=catalog)
    finally:
        corpus.unlock()
    assert differences == []


def test_accept_derived_primary_takes_the_original_ct_of_the_pet_ct(tmp_path: Path) -> None:
    variant = "accept_derived_primary"
    corpus = dicom_factory.build_corpus(tmp_path / "corpus")
    try:
        project, catalog = _index_and_merge(
            corpus, tmp_path, selection=EXPECTED["variants"][variant]["selection"]
        )
        differences = compare(
            project, EXPECTED, features=corpus.features, variant=variant, catalog_path=catalog
        )
    finally:
        corpus.unlock()
    assert differences == []
