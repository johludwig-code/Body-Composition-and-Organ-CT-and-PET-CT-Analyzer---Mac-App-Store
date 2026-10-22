"""The case report's content files (ADR 0017 section 11, worker part), on the
synthetic project of the export tests (tests/export_project.py)."""

from __future__ import annotations

import dataclasses
import io
import json
import re
from pathlib import Path

import pytest

import export_project
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.errors import JobFailure
from bcoa_worker.export import data as export_data
from bcoa_worker.export import report
from bcoa_worker.export.options import ExportOptionError, parse_options
from bcoa_worker.export.tables import build
from bcoa_worker.intended_use import EVERY_PAGE, STATEMENT
from bcoa_worker.jobs import export as export_job
from bcoa_worker.protocol import Job

CREATED = "2026-10-02T14:00:00Z"


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> export_data.ProjectData:
    folder = tmp_path_factory.mktemp("project")
    return export_data.load(export_project.create(folder), export_project.RUN)


def _cases(project: export_data.ProjectData, **options: object) -> dict[str, dict]:
    parsed = parse_options({"case_reports": True, **options})
    export = build(project, parsed, created=CREATED)
    return report.cases(project, parsed, export.methods_text)


def _structure(case: dict, model: str, label: str) -> dict:
    entry = next(m for m in case["models"] if m["name"] == model)
    return next(s for s in entry["structures"] if s["label"] == label)


def test_one_case_per_segmented_series_that_qc_kept(project) -> None:
    # P0001: t1 with one series, t2 with two; P0002 rejected; P0003 failed;
    # P0004 with the organs model only.
    assert sorted(_cases(project)) == ["P0001_t1_s1", "P0001_t2_s1", "P0001_t2_s2", "P0004_t1_s1"]


def test_rejected_series_gets_no_report_even_when_the_export_includes_it(project) -> None:
    assert "P0002_t1_s1" not in _cases(project, qc="include")


def test_same_numbers_as_the_long_export(project) -> None:
    long = build(
        project, parse_options({"layout": "long", "multiple_series": "all"}), created=CREATED
    )
    names = [c.name for c in long.sheet("results").columns]
    rows = [dict(zip(names, r, strict=True)) for r in long.sheet("results").rows]
    cases = _cases(project)
    checked = 0
    for row in rows:
        case = cases[f"{row['pseudonym']}_t{row['timepoint']}_s{row['series']}"]
        model = next(m for m in case["models"] if m["key"] == row["model"])
        structure = next(s for s in model["structures"] if s["label_id"] == row["label_id"])
        for metric in report.METRICS:
            assert structure[metric] == row[metric], (case["case"], row["label"], metric)
            checked += 1
    # Every row of the export is in a report: all three series of P0001 and
    # P0004's two organs, five metrics each.
    assert len(rows) == 20
    assert checked == len(rows) * len(report.METRICS)


def test_excluded_structure_is_empty_with_its_reason(project) -> None:
    case = _cases(project, qc="include")["P0004_t1_s1"]
    spleen = _structure(case, "clin_ct_organs", "spleen")
    assert spleen["excluded"] is True
    assert all(spleen[m] is None for m in report.METRICS)
    assert spleen["note"] == "excluded in QC: spleen leaks"
    liver = _structure(case, "clin_ct_organs", "liver")
    assert liver["volume_ml"] is not None


def test_qc_status_and_review_are_stated(project) -> None:
    cases = _cases(project)
    assert cases["P0001_t1_s1"]["qc"] == {"status": "accepted", "reviewed": True}
    assert cases["P0001_t2_s1"]["qc"] == {"status": "unreviewed", "reviewed": False}
    # A structure excluded is a look at the series, but not its acceptance.
    assert cases["P0004_t1_s1"]["qc"] == {"status": "unreviewed", "reviewed": True}


def test_a_model_that_did_not_run_says_so(project) -> None:
    case = _cases(project)["P0004_t1_s1"]
    statuses = {m["name"]: m["status"] for m in case["models"]}
    assert statuses == {"clin_ct_organs": "ok", "clin_ct_body_composition": "not_run"}


def test_notes_in_plain_words(project) -> None:
    liver = _structure(_cases(project)["P0004_t1_s1"], "clin_ct_organs", "liver")
    assert liver["flags"] == ["truncated"]
    assert liver["note"] == "cut off at the image edge, volume incomplete"

    results = tuple(
        dataclasses.replace(r, flags=("empty_label",))
        if r.series_key == "se1" and r.label_name == "kidney_left"
        else r
        for r in project.results
    )
    kidney = _structure(
        _cases(dataclasses.replace(project, results=results))["P0001_t1_s1"],
        "clin_ct_organs",
        "kidney_left",
    )
    assert kidney["note"] == "not found in this CT"


def test_an_accessory_structure_is_named_as_one() -> None:
    assert report._note("vertebra_l6", ("empty_label",), False, None) == report.ACCESSORY_NOTE
    assert report._note("rib_left_13", ("empty_label",), False, None) == report.ACCESSORY_NOTE
    assert report._note("rib_left_12", ("empty_label",), False, None) == "not found in this CT"


def test_volume_outlier_is_a_note_on_the_mask_not_a_judgment() -> None:
    note = report._note("liver", ("volume_outlier",), False, None)
    assert note == "volume unusual for this project; check the mask"
    assert "abnormal" not in note


def test_no_path_uid_patient_id_or_date(project) -> None:
    cases = _cases(project)
    text = "".join(report.dumps(c).decode() for c in cases.values())
    # The fixture's patient IDs, UIDs and study dates, and the pieces every
    # project path has.
    for forbidden in (
        "00012345", "1E5", "1.2.840.", "1.2.3.", "2020-01-10", "20210305",
        "work/", "viewer-cache", ".nii", "exports/", ".sqlite", "/Users", "/Volumes",
    ):  # fmt: skip
        assert forbidden not in text, forbidden
    assert "patient_id" not in text
    assert {c["date"]["kind"] for c in cases.values()} == {"days_since_first"}
    assert cases["P0001_t2_s1"]["date"]["value"] == 420


def test_dates_follow_the_export_when_it_chose_year_or_full_date(project) -> None:
    assert _cases(project, dates="year")["P0001_t2_s1"]["date"] == {"kind": "year", "value": 2021}
    assert _cases(project, dates="iso")["P0001_t1_s1"]["date"]["value"] == "2020-01-10"


def test_identifiers_are_always_the_pseudonym(project) -> None:
    text = "".join(report.dumps(c).decode() for c in _cases(project, identifiers="both").values())
    assert "00012345" not in text


def test_notice_statement_and_methods_text_come_along(project) -> None:
    case = _cases(project)["P0001_t1_s1"]
    assert case["notice"] == EVERY_PAGE
    assert case["statement"] == STATEMENT
    assert case["methods_text"].startswith("Segmentations were")
    assert case["versions"]
    assert case["run_id"] == export_project.RUN


def test_same_input_same_bytes(project) -> None:
    first = {k: report.dumps(v) for k, v in _cases(project).items()}
    second = {k: report.dumps(v) for k, v in _cases(project).items()}
    assert first == second


def test_case_names_follow_the_fixed_pattern(project) -> None:
    assert all(re.fullmatch(r"P\d{4}_t\d+_s\d+", name) for name in _cases(project))


# -- the job -----------------------------------------------------------------------


def _run(project_dir: Path, options: dict[str, object]) -> list[dict[str, object]]:
    buffer = io.StringIO()
    job = Job(
        "j_export",
        "export",
        project_dir,
        project_dir / "logs" / "j_export.log",
        project_dir,
        {"run_id": export_project.RUN, "stem": "cohort", "options": options},
    )
    export_job.run(job, ProtocolChannel(buffer))
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_job_writes_reports_as_report_artifacts(tmp_path: Path) -> None:
    export_project.create(tmp_path)
    events = _run(tmp_path, {"case_reports": True})
    reports = sorted(e["path"] for e in events if e["type"] == "artifact" and e["kind"] == "report")
    assert reports == [
        f"exports/cohort_reports/{name}.report.json"
        for name in ("P0001_t1_s1", "P0001_t2_s1", "P0001_t2_s2", "P0004_t1_s1")
    ]
    assert (tmp_path / "exports" / "cohort.xlsx").is_file()
    result = next(e for e in events if e["type"] == "result")["payload"]
    assert result["reports"] == reports
    assert not list((tmp_path / "exports").rglob(".*partial"))


def test_job_writes_reports_alone_when_tables_are_off(tmp_path: Path) -> None:
    export_project.create(tmp_path)
    events = _run(
        tmp_path,
        {"case_reports": True, "tables": False, "methods_text": False, "patients": ["P0004"]},
    )
    paths = [e["path"] for e in events if e["type"] == "artifact"]
    assert paths == ["exports/cohort_reports/P0004_t1_s1.report.json"]
    assert not (tmp_path / "exports" / "cohort.xlsx").exists()


def test_an_export_of_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExportOptionError):
        parse_options({"tables": False})
    export_project.create(tmp_path)
    with pytest.raises(JobFailure) as failure:
        _run(tmp_path, {"tables": False})
    assert failure.value.code == "bad_export_options"


def test_package_never_holds_a_report(project) -> None:
    from bcoa_worker.export import package

    options = parse_options({"case_reports": True, "reproducibility_package": True})
    export = build(project, options, created=CREATED)
    files = package.contents(project, export, options, CREATED)
    assert not any("report" in name for name in files if name != "README.txt")
    assert package.package_options(options).case_reports is False
