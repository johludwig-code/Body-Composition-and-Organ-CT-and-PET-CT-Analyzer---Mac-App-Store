"""Holds a merged project to `corpus_expected.json` (ADR 0026 decision 1).

`compare` reads a `project.sqlite` that a scan of the synthetic corpus and
`IndexSQL.merge` produced, and returns every way it differs from the
expectation, one line each; an empty list means it is exactly what the
expectation says. It reads what the app shows: patients, identifiers,
studies, series parts with their selection fields and reason codes,
`index_checks` and `series_pairs`. Given the catalog too, it also holds the
walk's counts of files by kind and the identity facts that never reach the
project (pid_state and the folder candidates).

The expectation names series by scenario and description, never by UID, so
that its owner can review it (ADR 0026 decision 2). Everything that would
otherwise go unnoticed counts as a difference: a patient, study or series the
expectation does not list, a check it does not list where it lists the
checks, and any code or level that `Protocol/index_codes.json` does not
register.

`new_project` and `merge` build the project the way `IndexStore` does, with
the migrations and the merge SQL extracted from BCOAStore, so the comparison
runs against what the app itself would hold.
"""

from __future__ import annotations

import copy
import json
import math
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from conftest import PROTOCOL_ROOT
from swift_sql import INDEX_SQL, constants, migration

EXPECTED_PATH = Path(__file__).with_name("corpus_expected.json")
REGISTRY_PATH = PROTOCOL_ROOT / "index_codes.json"

# Lengths and angles are stored as computed and their parameters rounded to a
# thousandth (codes.rounded); a thousandth is also finer than any difference
# the corpus is built to show.
TOLERANCE = 0.001

NOW = "2026-10-09T12:00:00Z"

_ENTRY_KEYS_NOT_FIELDS = ("study", "match", "why")


def load_expected() -> dict[str, Any]:
    return json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))


def registry() -> dict[str, dict[str, Any]]:
    """Every registered code with its group, and for checks its level."""
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    codes: dict[str, dict[str, Any]] = {}
    for group, body in raw["groups"].items():
        for entry in body["codes"]:
            codes[entry["code"]] = {"group": group, **entry}
    return codes


# ------------------------------------------------------------------ the expectation


def resolve(
    expected: Mapping[str, Any],
    *,
    features: Iterable[str] = (),
    variant: str | None = None,
) -> dict[str, Any]:
    """The expectation for one build of the corpus.

    Entries that need a feature this platform could not hold (`requires`)
    are dropped, and so are those that a present feature replaces (`unless`);
    a variant's overrides replace the keys they name in the parts they match.
    """
    present = frozenset(features)
    data = copy.deepcopy(dict(expected))
    if variant is not None:
        _apply_variant(data, data["variants"][variant])
    return _filtered(data, present)


def _filtered(value: Any, features: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {key: _filtered(item, features) for key, item in value.items()}
    if isinstance(value, list):
        return [_filtered(item, features) for item in value if _applies(item, features)]
    return value


def _applies(item: Any, features: frozenset[str]) -> bool:
    if not isinstance(item, dict):
        return True
    if "requires" in item and item["requires"] not in features:
        return False
    return not ("unless" in item and item["unless"] in features)


def _apply_variant(data: dict[str, Any], spec: Mapping[str, Any]) -> None:
    for override in spec.get("parts", []):
        found = [
            part
            for study in _studies(data)
            if study["match"].get("description") == override["study"]
            for part in study["parts"]
            if part["match"] == override["match"]
        ]
        if len(found) != 1:
            raise ValueError(f"variant override matches {len(found)} parts: {override['match']}")
        for key, value in override.items():
            if key not in _ENTRY_KEYS_NOT_FIELDS:
                found[0][key] = copy.deepcopy(value)


def _studies(data: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for scenario in data["scenarios"]:
        for patient in scenario["patients"]:
            yield from patient["studies"]


def totals_key(features: Iterable[str]) -> str:
    return "with_non_utf8_names" if "non_utf8_names" in set(features) else "without_features"


# ------------------------------------------------------------------ values


def same(actual: Any, wanted: Any) -> bool:
    """Equality as the expectation means it: numbers to within TOLERANCE,
    `{"set": [...]}` for a comma-joined text whose order is not defined
    (group_concat), and containers element by element."""
    if isinstance(wanted, dict) and set(wanted) == {"set"}:
        return isinstance(actual, str) and sorted(actual.split(",")) == sorted(wanted["set"])
    if isinstance(wanted, bool) or isinstance(actual, bool):
        return actual is wanted
    if isinstance(wanted, int | float) and isinstance(actual, int | float):
        return math.isclose(actual, wanted, rel_tol=0.0, abs_tol=TOLERANCE)
    if isinstance(wanted, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == wanted.keys()
            and all(same(actual[key], wanted[key]) for key in wanted)
        )
    if isinstance(wanted, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(wanted)
            and all(same(a, w) for a, w in zip(actual, wanted, strict=True))
        )
    return actual == wanted


def _nfc(value: str | None) -> str | None:
    return None if value is None else unicodedata.normalize("NFC", value)


def _show(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ------------------------------------------------------------------ the project


class _Project:
    """The current rows of a merged project, read once."""

    def __init__(self, path: Path) -> None:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            self.patients = _rows(db, "SELECT * FROM patients", "patient_key")
            self.identifiers = _rows(db, "SELECT * FROM identifiers", "patient_key")
            self.studies = _rows(
                db, "SELECT * FROM studies WHERE index_state = 'current'", "study_key"
            )
            self.series = _rows(
                db, "SELECT * FROM series WHERE index_state = 'current'", "series_key"
            )
            self.checks: dict[tuple[str, str], dict[str, tuple[str, Any]]] = {}
            for row in db.execute("SELECT * FROM index_checks"):
                params = json.loads(row["params_json"])
                self.checks.setdefault((row["object_kind"], row["object_key"]), {})[row["code"]] = (
                    row["level"],
                    params,
                )
            self.pairs = [dict(row) for row in db.execute("SELECT * FROM series_pairs")]

    def studies_of(self, patient_key: str) -> list[str]:
        return [k for k, s in self.studies.items() if s["patient_key"] == patient_key]

    def series_of(self, study_key: str) -> list[str]:
        return [k for k, s in self.series.items() if s["study_key"] == study_key]

    def matching_patients(self, match: Mapping[str, Any]) -> list[str]:
        keys = set(self.patients)
        for name, value in match.items():
            if name == "patient_id":
                keys &= {
                    k for k, i in self.identifiers.items() if _nfc(i["patient_id"]) == _nfc(value)
                }
            elif name == "study_description":
                keys &= {
                    s["patient_key"] for s in self.studies.values() if s["description"] == value
                }
            elif name == "series":
                keys &= {
                    self.studies[s["study_key"]]["patient_key"]
                    for s in self.series.values()
                    if s["study_key"] in self.studies and _fits(s, value)
                }
            else:
                raise ValueError(f"unknown patient selector {name!r}")
        return sorted(keys)

    def matching_studies(self, patient_key: str, match: Mapping[str, Any]) -> list[str]:
        keys = self.studies_of(patient_key)
        for name, value in match.items():
            if name == "description":
                keys = [k for k in keys if self.studies[k]["description"] == value]
            elif name == "series":
                keys = [
                    k for k in keys if any(_fits(self.series[s], value) for s in self.series_of(k))
                ]
            else:
                raise ValueError(f"unknown study selector {name!r}")
        return keys


def _rows(db: sqlite3.Connection, sql: str, key: str) -> dict[str, dict[str, Any]]:
    return {row[key]: dict(row) for row in db.execute(sql)}


def _fits(row: Mapping[str, Any], match: Mapping[str, Any]) -> bool:
    for column, value in match.items():
        if column not in row:
            raise ValueError(f"unknown series column {column!r}")
        if not same(row[column], value):
            return False
    return True


# ------------------------------------------------------------------ compare


def compare(
    index_db_path: Path | str,
    expected: Mapping[str, Any],
    *,
    features: Iterable[str] = (),
    variant: str | None = None,
    catalog_path: Path | str | None = None,
) -> list[str]:
    """Every difference between a merged project and the expectation.

    `features` are those `build_corpus` reported for the tree that was
    indexed, `variant` names an entry of the expectation's `variants` (the
    scan then ran with that variant's selection settings), and
    `catalog_path`, when given, adds the catalog's own facts.
    """
    present = frozenset(features)
    want = resolve(expected, features=present, variant=variant)
    project = _Project(Path(index_db_path))
    codes = registry()
    out: list[str] = []
    found: list[tuple[str, dict[str, Any]]] = []
    used_patients: set[str] = set()

    for scenario in want["scenarios"]:
        for patient in scenario["patients"]:
            label = f"{scenario['name']} › patient {_show(patient['match'])}"
            keys = [
                k for k in project.matching_patients(patient["match"]) if k not in used_patients
            ]
            if len(keys) != 1:
                out.append(f"{label}: {len(keys)} patients match, expected 1")
                continue
            used_patients.add(keys[0])
            _patient(project, keys[0], patient, label, out, found)

    for key in sorted(set(project.patients) - used_patients):
        descriptions = sorted(
            str(project.studies[s]["description"]) for s in project.studies_of(key)
        )
        out.append(f"unexpected patient with studies {descriptions}")

    _pairs(project, want["pairs"], out)
    for source_id, entry in want["sources"].items():
        _checks(project, "source", source_id, entry, f"source {source_id}", out)
    _checks(project, "project", "project", {"checks": want["project_checks"]}, "project", out)
    for kind, key in sorted(project.checks):
        if kind == "source" and key not in want["sources"]:
            out.append(f"source {key}: checks of a source the expectation does not list")
    _registered(project, codes, out)
    _totals(project, want["totals"][totals_key(present)], out)
    if catalog_path is not None:
        _catalog(Path(catalog_path), project, want, present, found, out)
    return out


def _patient(
    project: _Project,
    key: str,
    patient: Mapping[str, Any],
    label: str,
    out: list[str],
    found: list[tuple[str, dict[str, Any]]],
) -> None:
    _fields(project.patients[key], patient["fields"], label, out)
    _identifiers(project.identifiers.get(key), patient["identifiers"], label, out)
    _checks(project, "patient", key, patient, label, out)
    used: set[str] = set()
    for study in patient["studies"]:
        study_label = f"{label} › study {_show(study['match'])}"
        keys = [k for k in project.matching_studies(key, study["match"]) if k not in used]
        if len(keys) != 1:
            out.append(f"{study_label}: {len(keys)} studies match, expected 1")
            continue
        used.add(keys[0])
        found.append((keys[0], study))
        _study(project, keys[0], study, study_label, out)
    for extra in sorted(set(project.studies_of(key)) - used):
        out.append(f"{label}: unexpected study {project.studies[extra]['description']!r}")


def _study(
    project: _Project, key: str, study: Mapping[str, Any], label: str, out: list[str]
) -> None:
    _fields(project.studies[key], study["fields"], label, out)
    _checks(project, "study", key, study, label, out)
    used: set[str] = set()
    for part in study["parts"]:
        part_label = f"{label} › {_show(part['match'])}"
        keys = [
            k
            for k in project.series_of(key)
            if k not in used and _fits(project.series[k], part["match"])
        ]
        if len(keys) != 1:
            out.append(f"{part_label}: {len(keys)} series match, expected 1")
            continue
        used.add(keys[0])
        _part(project, keys[0], part, part_label, out)
    for extra in sorted(set(project.series_of(key)) - used):
        row = project.series[extra]
        out.append(
            f"{label}: unexpected series {row['description']!r} part {row['part']} "
            f"({row['modality']}, {row['image_count']} images)"
        )


def _part(project: _Project, key: str, part: Mapping[str, Any], label: str, out: list[str]) -> None:
    row = project.series[key]
    _fields(row, part.get("fields", {}), label, out)
    _fields(row, part.get("selection", {}), label, out)
    if "reason" in part:
        _reason(row["selection_reason"], part["reason"], label, out)
    _checks(project, "series", key, part, label, out)


def _fields(row: Mapping[str, Any], wanted: Mapping[str, Any], label: str, out: list[str]) -> None:
    for column, value in wanted.items():
        if column not in row:
            out.append(f"{label}: no column {column!r}")
        elif not same(row[column], value):
            out.append(f"{label}: {column} is {_show(row[column])}, expected {_show(value)}")


def _identifiers(
    row: Mapping[str, Any] | None, wanted: Mapping[str, Any] | None, label: str, out: list[str]
) -> None:
    if wanted is None:
        if row is not None:
            out.append(f"{label}: has an identifiers row, expected none")
        return
    if row is None:
        out.append(f"{label}: no identifiers row")
        return
    if _nfc(row["patient_id"]) != _nfc(wanted["patient_id"]):
        out.append(
            f"{label}: patient_id is {row['patient_id']!r}, expected {wanted['patient_id']!r}"
        )
    accessions = json.loads(row["accession_numbers"]) if row["accession_numbers"] else None
    if accessions != wanted["accession_numbers"]:
        out.append(
            f"{label}: accession_numbers are {_show(accessions)}, "
            f"expected {_show(wanted['accession_numbers'])}"
        )
    if row["id_source"] != wanted["id_source"]:
        out.append(f"{label}: id_source is {row['id_source']!r}, expected {wanted['id_source']!r}")


def _reason(raw: str | None, wanted: Mapping[str, Any], label: str, out: list[str]) -> None:
    try:
        reason = json.loads(raw) if raw is not None else None
    except json.JSONDecodeError:
        reason = None
    if not isinstance(reason, dict) or reason.get("v") != 1:
        out.append(f"{label}: selection_reason {raw!r} is not a version 1 reason")
        return
    if reason.get("outcome") != wanted["outcome"]:
        out.append(f"{label}: outcome is {reason.get('outcome')!r}, expected {wanted['outcome']!r}")
    actual_codes = reason.get("codes", [])
    if "codes" in wanted and actual_codes != wanted["codes"]:
        out.append(f"{label}: codes are {_show(actual_codes)}, expected {_show(wanted['codes'])}")
    for code in wanted.get("codes_include", []):
        if code not in actual_codes:
            out.append(f"{label}: codes {_show(actual_codes)} lack {code}")
    if "params" in wanted and not same(reason.get("params"), wanted["params"]):
        out.append(
            f"{label}: params are {_show(reason.get('params'))}, expected {_show(wanted['params'])}"
        )


def _checks(
    project: _Project, kind: str, key: str, entry: Mapping[str, Any], label: str, out: list[str]
) -> None:
    """`checks` is the exact list of an object's checks; `checks_include`
    lists some of them and leaves the rest unasserted; with neither, the
    object's checks are not asserted at all."""
    if "checks" in entry:
        wanted, exact = entry["checks"], True
    elif "checks_include" in entry:
        wanted, exact = entry["checks_include"], False
    else:
        return
    actual = project.checks.get((kind, key), {})
    listed = set()
    for check in wanted:
        code = check["code"]
        listed.add(code)
        if code not in actual:
            out.append(f"{label}: {code} missing")
        elif "params" in check and not same(actual[code][1], check["params"]):
            out.append(
                f"{label}: {code} params are {_show(actual[code][1])}, "
                f"expected {_show(check['params'])}"
            )
    if exact:
        for code in sorted(set(actual) - listed):
            out.append(f"{label}: unexpected {code} {_show(actual[code][1])}")


def _pairs(project: _Project, wanted: list[Mapping[str, Any]], out: list[str]) -> None:
    def name(series_key: str) -> tuple[str, str]:
        row = project.series.get(series_key)
        if row is None:
            return ("?", "?")
        return (str(project.studies[row["study_key"]]["description"]), str(row["description"]))

    actual: dict[tuple[str, str, str], dict[str, Any]] = {}
    for pair in project.pairs:
        study, pet = name(pair["pet_series_key"])
        _, ct = name(pair["ct_series_key"])
        actual[(study, pet, ct)] = pair
    listed = set()
    for pair in wanted:
        key = (pair["study"], pair["pet"], pair["ct"])
        listed.add(key)
        label = f"pair {pair['pet']!r} × {pair['ct']!r}"
        if key not in actual:
            out.append(f"{label}: missing")
            continue
        for column in ("pet_attenuation_corrected", "z_overlap_mm"):
            if not same(actual[key][column], pair[column]):
                out.append(
                    f"{label}: {column} is {_show(actual[key][column])}, "
                    f"expected {_show(pair[column])}"
                )
    for key in sorted(set(actual) - listed):
        out.append(f"unexpected pair {key[1]!r} × {key[2]!r} in {key[0]!r}")


def _registered(project: _Project, codes: Mapping[str, Mapping[str, Any]], out: list[str]) -> None:
    """Every check and reason code the project holds is registered, and every
    check has the level the registry gives it."""
    for (kind, key), checks in sorted(project.checks.items()):
        for code, (level, _) in sorted(checks.items()):
            entry = codes.get(code)
            if entry is None or entry["group"] != "check":
                out.append(f"{kind} {key}: {code} is not a registered check")
            elif entry["level"] != level or entry["object"] != kind:
                out.append(
                    f"{kind} {key}: {code} has level {level!r} on a {kind}; the registry says "
                    f"{entry['level']!r} on a {entry['object']}"
                )
    for row in project.series.values():
        try:
            reason = json.loads(row["selection_reason"] or "null")
        except json.JSONDecodeError:
            continue
        for code in reason.get("codes", []) if isinstance(reason, dict) else []:
            if codes.get(code, {}).get("group") != "select":
                out.append(f"series {row['description']!r}: {code} is not a registered reason")


def _totals(project: _Project, wanted: Mapping[str, int], out: list[str]) -> None:
    actual = {
        "patients": len(project.patients),
        "studies": len(project.studies),
        "series": len(project.series),
        "pairs": len(project.pairs),
    }
    for name, value in wanted.items():
        if actual[name] != value:
            out.append(f"totals: {actual[name]} {name}, expected {value}")


def _catalog(
    path: Path,
    project: _Project,
    want: Mapping[str, Any],
    features: frozenset[str],
    found: list[tuple[str, dict[str, Any]]],
    out: list[str],
) -> None:
    expected = want["catalog"]
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        kinds: dict[str, dict[str, int]] = {}
        for source_id, kind, count in db.execute(
            "SELECT source_id, kind, count(*) FROM files GROUP BY source_id, kind"
        ):
            kinds.setdefault(str(source_id), {})[kind] = count
        pixels: dict[str, dict[str, int]] = {}
        for source_id, state, count in db.execute(
            "SELECT source_id, pixel_data, count(*) FROM files "
            "WHERE pixel_data IN ('truncated', 'missing') GROUP BY source_id, pixel_data"
        ):
            pixels.setdefault(str(source_id), {})[state] = count
        studies = {
            row[0]: row[1] for row in db.execute("SELECT study_uid, pid_state FROM cat_studies")
        }
        candidates: dict[str, dict[int, str]] = {}
        for study_uid, level, label in db.execute(
            "SELECT study_uid, level, label FROM cat_id_candidates"
        ):
            candidates.setdefault(study_uid, {})[level] = label

    wanted_kinds = copy.deepcopy(expected["files_by_kind"])
    for feature, deltas in expected.get("files_by_kind_with", {}).items():
        if feature in features:
            for source_id, delta in deltas.items():
                for kind, count in delta.items():
                    counts = wanted_kinds.setdefault(source_id, {})
                    counts[kind] = counts.get(kind, 0) + count
    for source_id in sorted(set(wanted_kinds) | set(kinds)):
        have, need = kinds.get(source_id, {}), wanted_kinds.get(source_id, {})
        for kind in sorted(set(have) | set(need)):
            if have.get(kind, 0) != need.get(kind, 0):
                out.append(
                    f"catalog source {source_id}: {have.get(kind, 0)} files of kind {kind}, "
                    f"expected {need.get(kind, 0)}"
                )
    for source_id, need in expected.get("pixel_data", {}).items():
        have = pixels.get(source_id, {})
        for state in ("truncated", "missing"):
            if have.get(state, 0) != need.get(state, 0):
                out.append(
                    f"catalog source {source_id}: {have.get(state, 0)} files with pixel data "
                    f"{state}, expected {need.get(state, 0)}"
                )
    for study_key, study in found:
        if "catalog" not in study:
            continue
        uid = project.studies[study_key]["study_uid"]
        label = f"catalog study {_show(study['match'])}"
        facts = study["catalog"]
        if "pid_state" in facts and studies.get(uid) != facts["pid_state"]:
            out.append(
                f"{label}: pid_state is {studies.get(uid)!r}, expected {facts['pid_state']!r}"
            )
        have = candidates.get(uid, {})
        if "candidate_levels" in facts and sorted(have) != facts["candidate_levels"]:
            out.append(
                f"{label}: candidate levels {sorted(have)}, expected {facts['candidate_levels']}"
            )
        for level, wanted_label in facts.get("candidate_labels", {}).items():
            if have.get(int(level)) != wanted_label:
                out.append(
                    f"{label}: candidate {level} is {have.get(int(level))!r}, "
                    f"expected {wanted_label!r}"
                )


# ------------------------------------------------------------------ building the project

_SQL = constants(INDEX_SQL)


def new_project(path: Path, sources: Mapping[int, str]) -> Path:
    """A project.sqlite at v2 with the given sources, as the app creates it."""
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(migration("v1"))
        db.executescript(migration("v2"))
        db.executemany(
            "INSERT INTO sources (id, bookmark, display_path, added_at) VALUES (?, x'00', ?, ?)",
            [(source_id, name, NOW) for source_id, name in sorted(sources.items())],
        )
        db.commit()
    return path


def merge(
    project: Path,
    catalog: Path,
    *,
    link_key_id: str | None,
    keep_identifiers: int = 1,
    new_study_policy: str = "refuse",
) -> None:
    """One merge as IndexStore runs it: the catalog attached read-only, the
    inputs in temp tables, then `merge` and `patientAges` in one
    transaction."""
    index = _SQL["IndexSQL"]
    db = sqlite3.connect(project.resolve().as_uri(), uri=True, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("ATTACH DATABASE ? AS idx", (catalog.resolve().as_uri() + "?mode=ro",))
        db.executescript(index["mergeInputs"])
        db.execute(
            "INSERT INTO temp.merge_params (now, keep_identifiers, new_study_policy, link_key_id) "
            "VALUES (?, ?, ?, ?)",
            (NOW, keep_identifiers, new_study_policy, link_key_id),
        )
        try:
            db.executescript(
                "BEGIN IMMEDIATE;\n" + index["merge"] + "\n" + index["patientAges"] + "\nCOMMIT;"
            )
        except sqlite3.DatabaseError:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        db.execute("DETACH DATABASE idx")
    finally:
        db.close()
