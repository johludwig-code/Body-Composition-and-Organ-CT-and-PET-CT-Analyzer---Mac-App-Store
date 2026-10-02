"""A synthetic project database for the export tests. No real patient data:
every identifier, date and number here is made up, and each one is chosen to
hit a trap of plan §11.

- P0001: two studies (t1, t2); the second has a primary and a second series.
  PatientID "00012345" loses its zeros if it is ever written as a number.
- P0002: one study, rejected in QC. PatientID "1E5" becomes 100000 in Excel.
- P0003: one study whose segmentation failed; identifiers removed; no sex
  or age.
- P0004: series description "=HYPERLINK(...)"; the organs model ran, the body
  composition model did not; the spleen label is excluded in QC; the liver
  touches the border.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from conftest import WORKER_ROOT

SWIFT = WORKER_ROOT.parent / "Packages/BCOAStore/Sources/BCOAStore/StoreMigrations.swift"

RUN = "R20261002T120000"
ORGANS = {"1": "liver", "2": "spleen", "3": "kidney_left"}
BODY = {"1": "skeletal_muscle", "2": "subcutaneous_fat", "3": "visceral_fat"}


def schema_v1() -> str:
    match = re.search(r'static let v1 = """\n(.*?)\n\s*"""', SWIFT.read_text(), re.S)
    assert match, "v1 migration not found"
    return match.group(1)


def _values(seed: int, label: int) -> tuple[object, ...]:
    """Distinct, exactly representable numbers per series and label."""
    count = 1000 * seed + 10 * label
    return (
        count,
        count / 128,  # a binary fraction, so the golden files hold it exactly
        40.25 + seed + label,
        12.5,
        40.0 + seed + label,
        20.5,
        60.75,
        -100.0,
        250.0,
    )


def create(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "project.sqlite"
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(schema_v1())

    models = [
        {"name": "clin_ct_organs", "zip_sha256": "a" * 64, "labels": ORGANS},
        {"name": "clin_ct_body_composition", "zip_sha256": "b" * 64, "labels": BODY},
    ]
    versions = {
        "app": "0.1.0",
        "moosez": "3.2.2",
        "nnunetv2": "2.8.1",
        "torch": "2.14.1",
        "python": "3.12.11",
        "chip": "Apple M1 Pro",
        "macos": "15.6",
    }
    db.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, 1)",
        (
            RUN,
            "2026-10-02T12:00:00Z",
            json.dumps(versions),
            "mps",
            json.dumps(models),
            json.dumps({"retry_on_cpu": True}),
        ),
    )

    patients = [
        ("pk1", "P0001", "F", 61.5, "00012345"),
        ("pk2", "P0002", "M", 45.0, "1E5"),
        ("pk3", "P0003", None, None, None),
        ("pk4", "P0004", "O", 70.25, "4"),
    ]
    for key, pseudonym, sex, age, patient_id in patients:
        db.execute("INSERT INTO patients VALUES (?, ?, ?, ?)", (key, pseudonym, sex, age))
        if patient_id is not None:
            db.execute(
                "INSERT INTO identifiers VALUES (?, ?, NULL)",
                (key, patient_id),
            )

    # Study keys sort against the date order on purpose: the timepoint must
    # follow the date, not the key.
    studies = [
        ("st_b", "pk1", "1.2.3.1", "2020-01-10", "CT chest abdomen"),
        ("st_a", "pk1", "1.2.3.2", "20210305", "CT chest abdomen"),
        ("st_c", "pk2", "1.2.3.3", "2019-06-01", "CT abdomen"),
        ("st_d", "pk3", "1.2.3.4", "2022-02-02", "CT abdomen"),
        ("st_e", "pk4", "1.2.3.5", "not a date", "CT"),
        # A study with no series in the run: it must not become a timepoint.
        ("st_f", "pk1", "1.2.3.6", "2019-01-01", "Scout only"),
    ]
    db.executemany("INSERT INTO studies VALUES (?, ?, ?, ?, ?)", studies)

    series = [
        # key, study, description, thickness, kernel, selected, primary
        ("se1", "st_b", "Abdomen 1.0 Br40", 1.0, "Br40", 1, 1),
        ("se2", "st_a", "Abdomen 1.0 Br40", 1.0, "Br40", 1, 1),
        ("se3", "st_a", "Abdomen 3.0 Br36", 3.0, "Br36", 1, 0),
        ("se4", "st_c", "Portal venous", 2.0, "B30f", 1, 1),
        ("se5", "st_d", "Native", 5.0, "B30f", 1, 1),
        ("se6", "st_e", '=HYPERLINK("http://example.invalid")', 1.5, "-B31", 1, 1),
        ("se7", "st_f", "Topogram", None, None, 0, 0),
    ]
    for key, study, description, thickness, kernel, selected, primary in series:
        db.execute(
            "INSERT INTO series (series_key, study_key, series_uid, modality, description, "
            "image_count, slice_thickness_mm, pixel_spacing_mm, kernel, manufacturer, kvp, "
            "contrast_agent, fingerprint, selected, is_primary) "
            "VALUES (?, ?, ?, 'CT', ?, 300, ?, 0.75, ?, 'SIEMENS', 120, NULL, ?, ?, ?)",
            (
                key,
                study,
                f"1.2.840.{key}",
                description,
                thickness,
                kernel,
                f"fp_{key}",
                selected,
                primary,
            ),
        )

    def results(series_key: str, model: str, labels: dict[str, str], seed: int) -> None:
        for label_id, name in labels.items():
            values = _values(seed, int(label_id))
            border = 1 if (series_key == "se6" and name == "liver") else 0
            flags = "truncated" if border else ""
            db.execute(
                "INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (RUN, series_key, model, int(label_id), name, *values, border, "mps", flags),
            )

    results("se1", "clin_ct_organs", ORGANS, 1)
    results("se1", "clin_ct_body_composition", BODY, 2)
    results("se2", "clin_ct_organs", ORGANS, 3)
    results("se2", "clin_ct_body_composition", BODY, 4)
    results("se3", "clin_ct_organs", ORGANS, 5)
    results("se3", "clin_ct_body_composition", BODY, 6)
    results("se4", "clin_ct_organs", ORGANS, 7)
    results("se4", "clin_ct_body_composition", BODY, 8)
    results("se6", "clin_ct_organs", ORGANS, 9)

    jobs = [
        ("j_1", "se1", "done", "2026-10-02T12:00:00", "2026-10-02T12:01:30", None),
        ("j_2", "se2", "done", "2026-10-02T12:02:00", "2026-10-02T12:03:00", None),
        ("j_3", "se3", "done", "2026-10-02T12:04:00", "2026-10-02T12:05:00", None),
        ("j_4", "se4", "done", "2026-10-02T12:06:00", "2026-10-02T12:07:00", None),
        ("j_5", "se5", "failed", "2026-10-02T12:08:00", "2026-10-02T12:08:05", "dicom_unreadable"),
        ("j_6", "se6", "done", "2026-10-02T12:09:00", "2026-10-02T12:10:00", None),
    ]
    for job_id, series_key, status, start, end, error in jobs:
        db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, 'segment', ?, ?, ?, ?, 'mps', ?, NULL)",
            (job_id, RUN, series_key, status, start, start, end, error),
        )

    qc = [
        ("se1", None, None, "accepted", "JL", "2026-10-02T13:00:00Z", None),
        ("se4", None, None, "accepted", "JL", "2026-10-02T13:01:00Z", None),
        # The later decision wins.
        ("se4", None, None, "rejected", "JL", "2026-10-02T13:02:00Z", "motion artefact"),
        (
            "se6",
            "clin_ct_organs",
            2,
            "label_excluded",
            "JL",
            "2026-10-02T13:03:00Z",
            "spleen leaks",
        ),
    ]
    for series_key, model, label_id, status, reviewer, at, comment in qc:
        db.execute(
            "INSERT INTO qc (run_id, series_key, model, label_id, status, reviewer, at, comment) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (RUN, series_key, model, label_id, status, reviewer, at, comment),
        )
    db.commit()
    db.close()
    return path
