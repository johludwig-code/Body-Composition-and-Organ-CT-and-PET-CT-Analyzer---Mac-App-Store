"""What an export reads from the project database, as plain values (ADR 0013).

The tables are built from these dataclasses, not from SQL rows, so that the
layout logic can be tested with a handful of objects and the loader with a
real SQLite file, each on its own.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from bcoa_worker.metrics import METRIC_COLUMNS


@dataclass(frozen=True)
class Patient:
    patient_key: str
    pseudonym: str
    sex: str | None
    age_at_first_study: float | None
    patient_id: str | None = None


@dataclass(frozen=True)
class Study:
    study_key: str
    patient_key: str
    study_date: date | None
    description: str | None


@dataclass(frozen=True)
class Series:
    series_key: str
    study_key: str
    part: int
    modality: str
    description: str | None
    image_count: int
    slice_thickness_mm: float | None
    pixel_spacing_mm: float | None
    kernel: str | None
    manufacturer: str | None
    kvp: float | None
    contrast_agent: str | None
    selected: bool
    is_primary: bool


@dataclass(frozen=True)
class ModelInfo:
    name: str
    zip_sha256: str | None
    labels: dict[int, str]
    colors: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Run:
    run_id: str
    created_at: str
    device: str
    versions: dict[str, str]
    models: tuple[ModelInfo, ...]
    settings: dict[str, Any]


@dataclass(frozen=True)
class ResultRow:
    series_key: str
    model: str
    label_id: int
    label_name: str
    values: dict[str, object]
    device: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class QCEntry:
    series_key: str
    model: str | None
    label_id: int | None
    status: str
    reviewer: str | None
    at: str
    comment: str | None


@dataclass(frozen=True)
class JobRecord:
    series_key: str
    kind: str
    status: str
    started_at: str | None
    ended_at: str | None
    error_code: str | None

    @property
    def seconds(self) -> float | None:
        if not self.started_at or not self.ended_at:
            return None
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.ended_at)
        return (end - start).total_seconds()


@dataclass(frozen=True)
class ProjectData:
    run: Run
    patients: tuple[Patient, ...]
    studies: tuple[Study, ...]
    series: tuple[Series, ...]
    results: tuple[ResultRow, ...]
    qc: tuple[QCEntry, ...] = ()
    jobs: tuple[JobRecord, ...] = ()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    # DICOM writes YYYYMMDD; the app may store ISO. A malformed date is
    # missing, not a guess: a wrong date moves a study to another timepoint.
    for parse in (date.fromisoformat, lambda s: datetime.strptime(s, "%Y%m%d").date()):
        try:
            return parse(text)
        except ValueError:
            continue
    return None


def _models(raw: str) -> tuple[ModelInfo, ...]:
    models = []
    for entry in json.loads(raw):
        models.append(
            ModelInfo(
                name=str(entry["name"]),
                zip_sha256=entry.get("zip_sha256"),
                labels={int(k): str(v) for k, v in entry.get("labels", {}).items()},
                colors={int(k): str(v) for k, v in entry.get("colors", {}).items()},
            )
        )
    return tuple(models)


def load(database: Path, run_id: str) -> ProjectData:
    """Everything one run's export needs, read in one read-only transaction."""
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        return _read(connection, run_id)
    finally:
        connection.close()


def _read(db: sqlite3.Connection, run_id: str) -> ProjectData:
    row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise LookupError(f"run {run_id} is not in this project")
    run = Run(
        run_id=row["run_id"],
        created_at=row["created_at"],
        device=row["device"],
        versions={str(k): str(v) for k, v in json.loads(row["versions_json"]).items()},
        models=_models(row["models_json"]),
        settings=json.loads(row["settings_json"]),
    )

    patients = tuple(
        Patient(
            r["patient_key"], r["pseudonym"], r["sex"], r["age_at_first_study"], r["patient_id"]
        )
        for r in db.execute(
            "SELECT p.*, i.patient_id FROM patients p "
            "LEFT JOIN identifiers i USING (patient_key) ORDER BY p.pseudonym"
        )
    )
    studies = tuple(
        Study(r["study_key"], r["patient_key"], _parse_date(r["study_date"]), r["description"])
        for r in db.execute("SELECT * FROM studies ORDER BY study_key")
    )
    series = tuple(
        Series(
            series_key=r["series_key"],
            study_key=r["study_key"],
            part=r["part"],
            modality=r["modality"],
            description=r["description"],
            image_count=r["image_count"],
            slice_thickness_mm=r["slice_thickness_mm"],
            pixel_spacing_mm=r["pixel_spacing_mm"],
            kernel=r["kernel"],
            manufacturer=r["manufacturer"],
            kvp=r["kvp"],
            contrast_agent=r["contrast_agent"],
            selected=bool(r["selected"]),
            is_primary=bool(r["is_primary"]),
        )
        for r in db.execute("SELECT * FROM series ORDER BY series_key")
    )
    results = tuple(
        ResultRow(
            series_key=r["series_key"],
            model=r["model"],
            label_id=r["label_id"],
            label_name=r["label_name"],
            values={m: (bool(r[m]) if m == "touches_border" else r[m]) for m in METRIC_COLUMNS},
            device=r["device"],
            flags=tuple(f for f in r["flags"].split(",") if f),
        )
        for r in db.execute(
            "SELECT * FROM results WHERE run_id = ? ORDER BY series_key, model, label_id", (run_id,)
        )
    )
    qc = tuple(
        QCEntry(
            r["series_key"],
            r["model"],
            r["label_id"],
            r["status"],
            r["reviewer"],
            r["at"],
            r["comment"],
        )
        for r in db.execute("SELECT * FROM qc WHERE run_id = ? ORDER BY at, id", (run_id,))
    )
    jobs = tuple(
        JobRecord(
            r["series_key"], r["kind"], r["status"], r["started_at"], r["ended_at"], r["error_code"]
        )
        for r in db.execute(
            "SELECT * FROM jobs WHERE run_id = ? AND series_key IS NOT NULL "
            "ORDER BY queued_at, job_id",
            (run_id,),
        )
    )
    return ProjectData(run, patients, studies, series, results, qc, jobs)
