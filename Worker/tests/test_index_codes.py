"""Protocol/index_codes.json is the one list of the codes the import writes:
the app's String Catalog keys and enums are made from it, so a code written
anywhere must be in it, with the object, level and parameters it is written
with."""

from __future__ import annotations

import json
import re
import string
from typing import Any

import pytest

from bcoa_worker.index.catalog import CATALOG_DDL
from bcoa_worker.index.codes import CHECK_LEVELS
from conftest import PROTOCOL_ROOT
from swift_sql import INDEX_SQL, constants, migration

REGISTRY = json.loads((PROTOCOL_ROOT / "index_codes.json").read_text())
GROUPS: dict[str, list[dict[str, Any]]] = {
    name: group["codes"] for name, group in REGISTRY["groups"].items()
}
SQL = constants(INDEX_SQL)
PREFIXES = {
    "read": r"read\.[a-z_]+",
    "bad_dir": r"[a-z_]+",
    "source": r"source\.[a-z_]+",
    "select": r"select\.(chosen|eligible|excluded|held)\.[a-z0-9_]+",
    "status": r"(status\.[a-z_]+(\.[a-z_]+)?|badge\.primary)",
    "check": r"check\.[a-z_]+",
    "preview": r"preview\.(skipped|failed)\.[a-z_]+",
    "error": r"[a-z][a-z0-9_]*",
    "split_reason": r"[a-z_]+",
    "orientation": r"orientation\.(coronal|sagittal|oblique)",
    "transfer_syntax": r"[0-9]+(\.[0-9]+)+",
    "source_state": r"[a-z]+",
}


def _codes(group: str) -> set[str]:
    return {c["code"] for c in GROUPS[group]}


def _in_check(sql: str, column: str) -> set[str]:
    """The values of `column … CHECK (column IN (…))` in a schema."""
    match = re.search(rf"\b{column}\b[^,]*?CHECK \({column} IN \(([^)]*)\)", sql, re.S)
    assert match, column
    return set(re.findall(r"'([^']*)'", match.group(1)))


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_group_is_checked() -> None:
    assert set(GROUPS) == set(PREFIXES)


@pytest.mark.parametrize("group", PREFIXES)
def test_codes_are_unique_and_shaped_like_their_group(group: str) -> None:
    codes = [c["code"] for c in GROUPS[group]]
    assert len(codes) == len(set(codes))
    assert [c for c in codes if not re.fullmatch(PREFIXES[group], c)] == []


def test_dotted_codes_are_unique_across_groups() -> None:
    dotted = [c["code"] for codes in GROUPS.values() for c in codes if "." in c["code"]]
    assert len(dotted) == len(set(dotted))


@pytest.mark.parametrize(
    "entry",
    [c for codes in GROUPS.values() for c in codes if "text" in c],
    ids=lambda c: c["code"],
)
def test_texts_name_only_their_parameters(entry: dict[str, Any]) -> None:
    # A placeholder without a parameter renders as "{name}" in the app.
    params = set(entry.get("params", []))
    assert entry["text"].strip()
    assert len(params) == len(entry.get("params", []))
    assert _placeholders(entry["text"]) <= params
    for when, text in entry.get("variants", {}).items():
        assert when in params
        assert _placeholders(text) <= params


def test_checks_name_an_object_and_level_the_project_database_takes() -> None:
    v2 = migration("v2")
    objects = _in_check(v2, "object_kind")
    levels = _in_check(v2, "level")
    for entry in GROUPS["check"]:
        assert entry["object"] in objects, entry["code"]
        assert entry["level"] in levels, entry["code"]
        assert isinstance(entry["params"], list), entry["code"]


# Every literal check row the app's SQL inserts: object kind, check code,
# level, and the keys of its params_json.
_CHECK_WRITE = re.compile(
    r"SELECT '(\w+)', [\w.']+, '(check\.\w+)', '(\w+)',\s+('\{\}'|json_object\([^\n]*)"
)


def _sql_check_writes() -> list[tuple[str, str, str, set[str]]]:
    writes = []
    for owner in ("IndexSQL", "SelectionSQL", "IdentitySQL"):
        for sql in SQL[owner].values():
            for kind, code, level, params in _CHECK_WRITE.findall(sql):
                writes.append((kind, code, level, set(re.findall(r"'(\w+)', ", params))))
    return writes


def test_every_check_the_sql_writes_is_registered_as_written() -> None:
    registered = {c["code"]: c for c in GROUPS["check"]}
    writes = _sql_check_writes()
    assert {code for _, code, _, _ in writes} == {
        "check.sex_conflict",
        "check.new_series_since_manual",
        "check.primary_gone",
        "check.new_studies_not_added",
        "check.patient_link_changed",
        "check.age_inconsistent",
    }
    for kind, code, level, params in writes:
        entry = registered[code]
        assert (kind, level, params) == (entry["object"], entry["level"], set(entry["params"]))


def test_no_check_code_in_the_sql_escapes_the_pattern() -> None:
    # A code the pattern above does not see would go unchecked.
    seen = {code for _, code, _, _ in _sql_check_writes()}
    for owner in ("IndexSQL", "SelectionSQL", "IdentitySQL"):
        for sql in SQL[owner].values():
            assert set(re.findall(r"'(check\.\w+)'", sql)) <= seen


def test_preview_codes_are_the_result_schema_codes() -> None:
    schema = json.loads((PROTOCOL_ROOT / "schemas" / "index_result.schema.json").read_text())
    part = schema["$defs"]["previews"]["properties"]["parts"]["items"]
    assert set(part["properties"]["code"]["enum"]) == _codes("preview")


def test_source_codes_are_the_result_schema_codes() -> None:
    schema = json.loads((PROTOCOL_ROOT / "schemas" / "index_result.schema.json").read_text())
    source = schema["$defs"]["sources"]["additionalProperties"]
    assert set(source["properties"]["code"]["enum"]) == _codes("source")


def test_error_codes_say_whether_they_can_be_retried() -> None:
    for entry in GROUPS["error"]:
        assert isinstance(entry["recoverable"], bool), entry["code"]


def test_source_states_are_those_of_the_sources_table() -> None:
    assert _codes("source_state") == _in_check(migration("v2"), "state")


def test_bad_dir_codes_are_those_of_the_catalog() -> None:
    assert _codes("bad_dir") == _in_check(CATALOG_DDL.split("CREATE TABLE files")[0], "code")


def test_held_is_the_reason_of_an_unconfirmed_patients_series() -> None:
    # The merge writes this code as the reason of such a series and keeps
    # the worker's own beside it (ADR 0029), so it must exist exactly once.
    assert [c for c in _codes("select") if c.startswith("select.held.")] == [
        "select.held.unconfirmed_patient"
    ]


# The checks the app's SQL writes after a merge; the worker never computes them.
_MERGE_CHECKS = {
    "check.sex_conflict",
    "check.new_series_since_manual",
    "check.primary_gone",
    "check.new_studies_not_added",
    "check.patient_link_changed",
    "check.age_inconsistent",
}


def test_the_workers_check_levels_are_the_registrys() -> None:
    registered = {c["code"]: c for c in GROUPS["check"]}
    assert set(CHECK_LEVELS) == set(registered) - _MERGE_CHECKS
    for code, level in CHECK_LEVELS.items():
        assert level == registered[code]["level"], code
        # cat_checks takes these objects only; patients and the project are
        # the merge's.
        assert registered[code]["object"] in {"source", "study", "series"}, code


def test_no_two_excluded_codes_share_a_parameter() -> None:
    # A reason holds one params object for all its codes, and an excluded
    # part usually fails several conditions: a shared name would make one
    # code's number the other's.
    seen: dict[str, str] = {}
    for entry in GROUPS["select"]:
        if not entry["code"].startswith("select.excluded."):
            continue
        for name in entry["params"]:
            assert name not in seen, (name, seen.get(name), entry["code"])
            seen[name] = entry["code"]
