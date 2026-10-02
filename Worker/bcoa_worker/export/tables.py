"""The sheets of an export as plain tables (plan §11).

Everything about layout lives here and nothing about files: a sheet is a list
of typed columns and rows of Python values, `None` for an empty cell. The
writers turn the same sheets into XLSX or CSV, so both formats always hold the
same numbers in the same order.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Literal

from bcoa_worker import __version__
from bcoa_worker.export.data import ModelInfo, Patient, ProjectData, QCEntry, Series, Study
from bcoa_worker.export.options import ExportOptions
from bcoa_worker.intended_use import SHORT, STATEMENT
from bcoa_worker.naming import (
    check_sheet_name,
    display_name,
    model_key,
    sanitize,
    status_column,
    timepoints,
    wide_column,
)

ColumnType = Literal["text", "integer", "number", "boolean"]

APP_NAME = "Body Composition and Organ CT and PET-CT Analyzer"

SHEET_ORDER = ("results", "series", "qc", "labels", "data_dictionary", "provenance", "skipped")

_METRIC_TYPES: dict[str, tuple[ColumnType, str, str]] = {
    "voxel_count": ("integer", "", "number of voxels with this label"),
    "volume_ml": ("number", "mL", "voxel count times voxel volume"),
    "hu_mean": ("number", "HU", "mean CT value inside the label"),
    "hu_sd": ("number", "HU", "sample standard deviation (n - 1) of the CT values"),
    "hu_median": ("number", "HU", "median CT value"),
    "hu_p05": ("number", "HU", "5th percentile of the CT values (linear interpolation)"),
    "hu_p95": ("number", "HU", "95th percentile of the CT values (linear interpolation)"),
    "hu_min": ("number", "HU", "lowest CT value"),
    "hu_max": ("number", "HU", "highest CT value"),
    "touches_border": (
        "boolean",
        "",
        "label touches the first or last slice or the in-plane image edge",
    ),
}

# Where in the scan a model's labels can lie. MOOSE runs body composition on
# the L1-L5 range found by clin_ct_fast_vertebrae and keeps only the z-range
# of L3 (moosez.workflows, crop_label 22). On a real whole-body CT that was a
# 50 mm slab: "skeletal_muscle 362 mL" is the muscle at L3, not in the body,
# and nothing in the numbers says so.
WHOLE_FIELD = "whole field of view"
MODEL_REGIONS = {
    "clin_ct_body_composition": "z-range of the L3 vertebra only (MOOSE workflow)",
}

CITATIONS = (
    "Shiyam Sundar LK, Yu J, Muzik O, et al. Fully automated, semantic segmentation of "
    "whole-body 18F-FDG PET/CT images based on data-centric artificial intelligence. "
    "J Nucl Med. 2022;63(12):1941-1948. doi:10.2967/jnumed.122.264063",
    "Isensee F, Jaeger PF, Kohl SAA, Petersen J, Maier-Hein KH. nnU-Net: a self-configuring "
    "method for deep learning-based biomedical image segmentation. Nat Methods. "
    "2021;18(2):203-211. doi:10.1038/s41592-020-01008-z",
)

BIBTEX = """@article{shiyamsundar2022moose,
  author = {Shiyam Sundar, Lalith Kumar and Yu, Josef and Muzik, Otto and others},
  title = {Fully Automated, Semantic Segmentation of Whole-Body {18F-FDG} {PET/CT}
           Images Based on Data-Centric Artificial Intelligence},
  journal = {Journal of Nuclear Medicine},
  year = {2022},
  volume = {63},
  number = {12},
  pages = {1941--1948},
  doi = {10.2967/jnumed.122.264063}
}
@article{isensee2021nnunet,
  author = {Isensee, Fabian and Jaeger, Paul F. and Kohl, Simon A. A. and
            Petersen, Jens and Maier-Hein, Klaus H.},
  title = {{nnU-Net}: a self-configuring method for deep learning-based
           biomedical image segmentation},
  journal = {Nature Methods},
  year = {2021},
  volume = {18},
  number = {2},
  pages = {203--211},
  doi = {10.1038/s41592-020-01008-z}
}"""

MODEL_ATTRIBUTION = (
    "Segmentation model weights: MOOSE (ENHANCE.PET), licensed under CC BY 4.0, "
    "https://creativecommons.org/licenses/by/4.0/. Changes: optimizer state and training "
    "artefacts were removed from the checkpoints; the network weights are unchanged. "
    "MOOSE software: Apache-2.0."
)


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    unit: str = ""
    description: str = ""


@dataclass
class Sheet:
    name: str
    columns: list[Column]
    rows: list[list[object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        check_sheet_name(self.name)


@dataclass
class Export:
    sheets: list[Sheet]
    warnings: list[str]
    # (series_key, model, file stem) of every labelmap the masks extra copies.
    masks: list[tuple[str, str, str]]
    methods_text: str
    excluded_series: int

    def sheet(self, name: str) -> Sheet:
        return next(s for s in self.sheets if s.name == name)


@dataclass(frozen=True)
class Slot:
    """One series as it appears in the export: where it sits and how it is named."""

    patient: Patient
    study: Study
    series: Series
    timepoint: int
    series_index: int
    day: int | None


class _QC:
    """The latest QC decision per series, model and label.

    Entries arrive ordered by time; a later decision replaces an earlier one,
    because a reviewer who changes "rejected" to "accepted" means it.
    """

    def __init__(self, entries: Iterable[QCEntry]) -> None:
        self.latest: dict[tuple[str, str | None, int | None], QCEntry] = {}
        for entry in entries:
            self.latest[(entry.series_key, entry.model, entry.label_id)] = entry

    def series_status(self, series_key: str) -> str:
        entry = self.latest.get((series_key, None, None))
        return entry.status if entry else "unreviewed"

    def model_rejected(self, series_key: str, model: str) -> bool:
        entry = self.latest.get((series_key, model, None))
        return entry is not None and entry.status == "rejected"

    def label_excluded(self, series_key: str, model: str, label_id: int) -> bool:
        entry = self.latest.get((series_key, model, label_id))
        return entry is not None and entry.status == "label_excluded"

    def reviewed(self, series_key: str) -> bool:
        # A decision on one label is a look at the series as well.
        return any(key[0] == series_key for key in self.latest)


class _Builder:
    def __init__(self, data: ProjectData, options: ExportOptions) -> None:
        self.data = data
        self.options = options
        self.qc = _QC(data.qc)
        self.warnings: list[str] = []

        self.models: list[ModelInfo] = [
            m for m in data.run.models if options.models is None or m.name in options.models
        ]
        if options.models is not None:
            missing = sorted(set(options.models) - {m.name for m in data.run.models})
            if missing:
                raise ValueError(f"models not in run {data.run.run_id}: {', '.join(missing)}")
        self.labels: dict[str, list[tuple[int, str]]] = {
            m.name: [
                (label_id, sanitize(name))
                for label_id, name in sorted(m.labels.items())
                if options.labels is None or sanitize(name) in options.labels
            ]
            for m in self.models
        }

        self.results: dict[tuple[str, str], dict[int, dict[str, object]]] = defaultdict(dict)
        self.result_flags: dict[tuple[str, str, int], tuple[str, ...]] = {}
        self.result_device: dict[str, str] = {}
        for r in data.results:
            self.results[(r.series_key, r.model)][r.label_id] = r.values
            self.result_flags[(r.series_key, r.model, r.label_id)] = r.flags
            self.result_device.setdefault(r.series_key, r.device)
        self.failed_jobs: dict[str, str] = {}
        self.runtime: dict[str, float] = defaultdict(float)
        for job in data.jobs:
            if job.status == "failed":
                self.failed_jobs[job.series_key] = job.error_code or "failed"
            elif job.status == "done" and job.seconds is not None:
                self.runtime[job.series_key] += job.seconds

        self.n_studies: dict[str, int] = {}
        self.slots = self._slots()

    # -- which series appear, and where ---------------------------------------

    def _slots(self) -> list[Slot]:
        options = self.options
        studies_of: dict[str, list[Study]] = defaultdict(list)
        for study in self.data.studies:
            studies_of[study.patient_key].append(study)
        series_of: dict[str, list[Series]] = defaultdict(list)
        with_results = {key for key, _ in self.results}
        for series in self.data.series:
            if series.selected or series.series_key in with_results:
                series_of[series.study_key].append(series)

        patients = sorted(self.data.patients, key=lambda p: p.pseudonym)
        if options.patients is not None:
            wanted = set(options.patients)
            patients = [p for p in patients if p.pseudonym in wanted]

        slots: list[Slot] = []
        for patient in patients:
            # Studies without any series in the run do not count as timepoints:
            # a scout-only study would otherwise shift t2 to t3.
            studies = [s for s in studies_of[patient.patient_key] if series_of[s.study_key]]
            if not studies:
                continue
            studies.sort(key=lambda s: (s.study_date or date.max, s.study_key))
            numbers = timepoints([s.study_date or date.max for s in studies])
            first = min((s.study_date for s in studies if s.study_date), default=None)
            # Counted before "first" or "last" picks one: the column says how
            # many studies the patient has, not how many rows were exported.
            self.n_studies[patient.patient_key] = len(studies)
            chosen = list(zip(numbers, studies, strict=True))
            if options.row_level == "patient" and options.multiple_studies in ("first", "last"):
                chosen = [chosen[0]] if options.multiple_studies == "first" else [chosen[-1]]
            for number, study in chosen:
                candidates = sorted(
                    series_of[study.study_key], key=lambda s: (not s.is_primary, s.series_key)
                )
                if options.multiple_series == "primary":
                    candidates = candidates[:1]
                day = (
                    (study.study_date - first).days
                    if study.study_date is not None and first is not None
                    else None
                )
                for index, series in enumerate(candidates, start=1):
                    slots.append(Slot(patient, study, series, number, index, day))
        return slots

    # -- values -----------------------------------------------------------------

    def series_excluded(self, series_key: str) -> bool:
        return self.options.qc == "exclude" and self.qc.series_status(series_key) == "rejected"

    def model_status(self, series_key: str, model: str) -> str:
        if self.options.qc == "exclude" and (
            self.series_excluded(series_key) or self.qc.model_rejected(series_key, model)
        ):
            return "excluded"
        if (series_key, model) in self.results:
            return "ok"
        if series_key in self.failed_jobs:
            return "failed"
        return "not_run"

    def metric(self, series_key: str, model: str, label_id: int, metric: str) -> object:
        if self.model_status(series_key, model) != "ok":
            return None
        if self.options.qc == "exclude" and self.qc.label_excluded(series_key, model, label_id):
            return None
        values = self.results[(series_key, model)].get(label_id)
        return None if values is None else values.get(metric)

    def date_value(self, slot: Slot) -> object:
        if self.options.dates == "days_since_first":
            return slot.day
        if slot.study.study_date is None:
            return None
        if self.options.dates == "year":
            return slot.study.study_date.year
        return slot.study.study_date.isoformat()

    def date_column(self) -> Column:
        if self.options.dates == "days_since_first":
            return Column("day", "integer", "days", "days since the patient's first study")
        if self.options.dates == "year":
            return Column("year", "integer", "", "year of the study")
        return Column("study_date", "text", "ISO 8601", "date of the study")

    def identity_columns(self) -> list[Column]:
        columns = []
        if self.options.identifiers in ("pseudonym", "both"):
            columns.append(Column("pseudonym", "text", "", "project pseudonym of the patient"))
        if self.options.identifiers in ("patient_id", "both"):
            columns.append(Column("patient_id", "text", "", "DICOM PatientID (identifying)"))
        return columns

    def identity(self, patient: Patient) -> list[object]:
        values: list[object] = []
        if self.options.identifiers in ("pseudonym", "both"):
            values.append(patient.pseudonym)
        if self.options.identifiers in ("patient_id", "both"):
            values.append(patient.patient_id)
        return values

    # -- results ----------------------------------------------------------------

    def results_sheet(self) -> Sheet:
        if self.options.layout == "long":
            return self._long()
        return self._wide()

    def _metric_column(self, name: str, metric: str, what: str) -> Column:
        kind, unit, description = _METRIC_TYPES[metric]
        return Column(name, kind, unit, f"{what}: {description}")

    def _wide_block(
        self, timepoint: int | None, series_index: int | None
    ) -> list[tuple[Column, object]]:
        """Columns of one series position, with a placeholder for the values."""
        where = ""
        if timepoint is not None:
            where += f", timepoint {timepoint}"
        if series_index is not None:
            where += f", series {series_index}"

        def suffix(name: str) -> str:
            parts = [name]
            if timepoint is not None:
                parts.append(f"t{timepoint}")
            if series_index is not None:
                parts.append(f"s{series_index}")
            return "__".join(parts)

        block: list[tuple[Column, object]] = []
        fixed = [
            self.date_column(),
            Column("series_description", "text", "", "DICOM SeriesDescription"),
            Column("slice_thickness_mm", "number", "mm", "DICOM SliceThickness"),
            Column("kernel", "text", "", "reconstruction kernel"),
            Column("device", "text", "", "compute device the segmentation ran on"),
            Column("qc_status", "text", "", "QC status of the series"),
            Column("run_id", "text", "", "analysis run"),
        ]
        for column in fixed:
            block.append(
                (
                    Column(
                        suffix(column.name), column.type, column.unit, column.description + where
                    ),
                    column.name,
                )
            )
        for model in self.models:
            key = model_key(model.name)
            status = (
                status_column(model.name, timepoint=timepoint)
                if series_index is None
                else suffix(f"{key}__status")
            )
            block.append(
                (
                    Column(
                        status,
                        "text",
                        "",
                        f"{key}: ok, failed, excluded or not_run{where}",
                    ),
                    ("status", model.name),
                )
            )
            for label_id, label in self.labels[model.name]:
                for metric in self.options.metrics:
                    name = wide_column(
                        model.name, label, metric, timepoint=timepoint, series=series_index
                    )
                    region = MODEL_REGIONS.get(model.name)
                    what = f"{key} / {label}{where}" + (f", {region}" if region else "")
                    column = self._metric_column(name, metric, what)
                    block.append((column, ("metric", model.name, label_id, metric)))
        return block

    def _wide_value(self, slot: Slot | None, what: object) -> object:
        if slot is None:
            return None
        series = slot.series
        if isinstance(what, tuple):
            if what[0] == "status":
                return self.model_status(series.series_key, what[1])
            _, model, label_id, metric = what
            return self.metric(series.series_key, model, label_id, metric)
        if what in ("day", "year", "study_date"):
            return self.date_value(slot)
        return {
            "series_description": series.description,
            "slice_thickness_mm": series.slice_thickness_mm,
            "kernel": series.kernel,
            "device": self.result_device.get(series.series_key),
            "qc_status": self.qc.series_status(series.series_key),
            "run_id": self.data.run.run_id,
        }[str(what)]

    def _wide(self) -> Sheet:
        options = self.options
        per_series = options.row_level == "series"
        per_study = options.one_row_per_study
        all_series = options.multiple_series == "all" and not per_series

        # Rows: the slots that share a row. Positions: (timepoint, series)
        # pairs that need their own block of columns, or None when the row
        # holds exactly one position and needs no suffix.
        groups: dict[tuple[object, ...], list[Slot]] = defaultdict(list)
        for slot in self.slots:
            if per_series:
                key: tuple[object, ...] = (
                    slot.patient.pseudonym,
                    slot.timepoint,
                    slot.series_index,
                )
            elif per_study:
                key = (slot.patient.pseudonym, slot.timepoint)
            else:
                key = (slot.patient.pseudonym,)
            groups[key].append(slot)

        suffix_timepoints = options.row_level == "patient" and options.multiple_studies == "suffix"
        positions: list[tuple[int | None, int | None]] = []
        seen: set[tuple[int | None, int | None]] = set()
        for slots in groups.values():
            for slot in slots:
                position = (
                    slot.timepoint if suffix_timepoints else None,
                    slot.series_index if all_series else None,
                )
                if position not in seen:
                    seen.add(position)
                    positions.append(position)
        positions.sort(key=lambda p: (p[0] or 0, p[1] or 0))
        if not positions:
            positions = [(1 if suffix_timepoints else None, 1 if all_series else None)]

        columns = [
            *self.identity_columns(),
            Column("n_studies", "integer", "", "number of studies of the patient in this run"),
            Column("sex", "text", "", "DICOM PatientSex: F, M or O"),
            Column("age_first_study", "number", "years", "age at the first study"),
        ]
        if per_study or per_series:
            columns.append(Column("timepoint", "integer", "", "study number by date, 1 = first"))
        if per_series:
            columns.append(Column("series", "integer", "", "series number within the study"))
        blocks = [(position, self._wide_block(*position)) for position in positions]
        for _, block in blocks:
            columns.extend(column for column, _ in block)

        sheet = Sheet("results", columns)
        for slots in groups.values():
            patient = slots[0].patient
            row: list[object] = [
                *self.identity(patient),
                self.n_studies[patient.patient_key],
                patient.sex,
                patient.age_at_first_study,
            ]
            if per_study or per_series:
                row.append(slots[0].timepoint)
            if per_series:
                row.append(slots[0].series_index)
            by_position = {
                (
                    s.timepoint if suffix_timepoints else None,
                    s.series_index if all_series else None,
                ): s
                for s in slots
            }
            for position, block in blocks:
                slot = by_position.get(position)
                row.extend(self._wide_value(slot, what) for _, what in block)
            sheet.rows.append(row)
        return sheet

    def _long(self) -> Sheet:
        columns = [
            *self.identity_columns(),
            Column("timepoint", "integer", "", "study number by date, 1 = first"),
            self.date_column(),
            Column("series", "integer", "", "series number within the study"),
            Column("series_description", "text", "", "DICOM SeriesDescription"),
            Column("model", "text", "", "segmentation model"),
            Column("label_id", "integer", "", "label value in the labelmap"),
            Column("label", "text", "", "label name, sanitised"),
        ]
        for metric in self.options.metrics:
            columns.append(self._metric_column(metric, metric, "label"))
        columns += [
            Column("flags", "text", "", "automatic QC flags, comma-separated"),
            Column("qc_status", "text", "", "QC status of the series, model or label"),
            Column("device", "text", "", "compute device the segmentation ran on"),
            Column("run_id", "text", "", "analysis run"),
        ]
        sheet = Sheet("results", columns)
        for slot in self.slots:
            key = slot.series.series_key
            for model in self.models:
                # Failed and not-run models are listed once in `skipped`
                # rather than as 144 rows of empty cells here.
                if self.model_status(key, model.name) != "ok":
                    continue
                for label_id, label in self.labels[model.name]:
                    excluded = self.qc.label_excluded(key, model.name, label_id)
                    if excluded and self.options.qc == "exclude":
                        continue
                    status = self.qc.series_status(key)
                    if self.qc.model_rejected(key, model.name):
                        status = "rejected"
                    if excluded:
                        status = "label_excluded"
                    values = self.results[(key, model.name)].get(label_id, {})
                    sheet.rows.append(
                        [
                            *self.identity(slot.patient),
                            slot.timepoint,
                            self.date_value(slot),
                            slot.series_index,
                            slot.series.description,
                            model_key(model.name),
                            label_id,
                            label,
                            *(values.get(m) for m in self.options.metrics),
                            ",".join(self.result_flags.get((key, model.name, label_id), ())),
                            status,
                            self.result_device.get(key),
                            self.data.run.run_id,
                        ]
                    )
        return sheet

    # -- the other sheets ---------------------------------------------------------

    def series_sheet(self) -> Sheet:
        sheet = Sheet(
            "series",
            [
                *self.identity_columns(),
                Column("timepoint", "integer", "", "study number by date, 1 = first"),
                self.date_column(),
                Column("series", "integer", "", "series number within the study"),
                Column("primary", "boolean", "", "series chosen as primary for the study"),
                Column("modality", "text", "", "DICOM Modality"),
                Column("series_description", "text", "", "DICOM SeriesDescription"),
                Column("manufacturer", "text", "", "scanner manufacturer"),
                Column("kernel", "text", "", "reconstruction kernel"),
                Column("slice_thickness_mm", "number", "mm", "DICOM SliceThickness"),
                Column("pixel_spacing_mm", "number", "mm", "in-plane pixel spacing"),
                Column("kvp", "number", "kV", "tube voltage"),
                Column("contrast_agent", "text", "", "DICOM ContrastBolusAgent"),
                Column("image_count", "integer", "", "number of images"),
                Column("qc_status", "text", "", "QC status of the series"),
                Column("device", "text", "", "compute device the segmentation ran on"),
                Column("runtime_s", "number", "s", "time of the finished jobs of this series"),
            ],
        )
        for slot in self.slots:
            s = slot.series
            runtime = self.runtime.get(s.series_key)
            sheet.rows.append(
                [
                    *self.identity(slot.patient),
                    slot.timepoint,
                    self.date_value(slot),
                    slot.series_index,
                    s.is_primary,
                    s.modality,
                    s.description,
                    s.manufacturer,
                    s.kernel,
                    s.slice_thickness_mm,
                    s.pixel_spacing_mm,
                    s.kvp,
                    s.contrast_agent,
                    s.image_count,
                    self.qc.series_status(s.series_key),
                    self.result_device.get(s.series_key),
                    runtime,
                ]
            )
        return sheet

    def qc_sheet(self) -> Sheet:
        sheet = Sheet(
            "qc",
            [
                *self.identity_columns(),
                Column("timepoint", "integer", "", "study number by date, 1 = first"),
                Column("series", "integer", "", "series number within the study"),
                Column("model", "text", "", "segmentation model; empty for the whole series"),
                Column("label_id", "integer", "", "label; empty for the whole series or model"),
                Column("label", "text", "", "label name"),
                Column("status", "text", "", "QC decision"),
                Column("flags", "text", "", "automatic QC flags, comma-separated"),
                Column("reviewer", "text", "", "reviewer initials"),
                Column("at", "text", "ISO 8601", "time of the decision"),
                Column("comment", "text", "", "reviewer comment"),
            ],
        )
        names = {
            (m.name, label_id): label
            for m in self.models
            for label_id, label in self.labels[m.name]
        }
        exported = {m.name for m in self.models}
        for slot in self.slots:
            key = slot.series.series_key
            head = [*self.identity(slot.patient), slot.timepoint, slot.series_index]
            series_entry = self.qc.latest.get((key, None, None))
            sheet.rows.append(
                [
                    *head,
                    None,
                    None,
                    None,
                    self.qc.series_status(key),
                    None,
                    series_entry.reviewer if series_entry else None,
                    series_entry.at if series_entry else None,
                    series_entry.comment if series_entry else None,
                ]
            )
            for (series_key, model, label_id), entry in sorted(
                self.qc.latest.items(), key=lambda item: (str(item[0][1]), item[0][2] or -1)
            ):
                if series_key != key or model is None or model not in exported:
                    continue
                sheet.rows.append(
                    [
                        *head,
                        model_key(model),
                        label_id,
                        names.get((model, label_id)) if label_id is not None else None,
                        entry.status,
                        None,
                        entry.reviewer,
                        entry.at,
                        entry.comment,
                    ]
                )
            for model in self.models:
                for label_id, label in self.labels[model.name]:
                    flags = self.result_flags.get((key, model.name, label_id), ())
                    if flags:
                        sheet.rows.append(
                            [
                                *head,
                                model_key(model.name),
                                label_id,
                                label,
                                None,
                                ",".join(flags),
                                None,
                                None,
                                None,
                            ]
                        )
        return sheet

    def labels_sheet(self) -> Sheet:
        sheet = Sheet(
            "labels",
            [
                Column("model", "text", "", "segmentation model"),
                Column("model_key", "text", "", "model name as used in column names"),
                Column("label_id", "integer", "", "label value in the labelmap"),
                Column("label", "text", "", "label name, sanitised"),
                Column("display_name", "text", "", "label name as shown in the app"),
                Column("region", "text", "", "part of the scan the label is measured in"),
                Column("color", "text", "", "overlay colour in the app"),
                Column("model_sha256", "text", "", "SHA-256 of the model archive"),
            ],
        )
        for model in self.models:
            for label_id, label in self.labels[model.name]:
                sheet.rows.append(
                    [
                        model.name,
                        model_key(model.name),
                        label_id,
                        label,
                        display_name(model.labels[label_id]),
                        MODEL_REGIONS.get(model.name, WHOLE_FIELD),
                        model.colors.get(label_id),
                        model.zip_sha256,
                    ]
                )
        return sheet

    def skipped_sheet(self) -> Sheet:
        sheet = Sheet(
            "skipped",
            [
                *self.identity_columns(),
                Column("timepoint", "integer", "", "study number by date, 1 = first"),
                Column("series", "integer", "", "series number within the study"),
                Column("series_description", "text", "", "DICOM SeriesDescription"),
                Column("modality", "text", "", "DICOM Modality"),
                Column("model", "text", "", "segmentation model; empty for all"),
                Column("reason", "text", "", "why there are no values"),
            ],
        )
        for slot in self.slots:
            key = slot.series.series_key
            statuses = {m.name: self.model_status(key, m.name) for m in self.models}
            head = [
                *self.identity(slot.patient),
                slot.timepoint,
                slot.series_index,
                slot.series.description,
                slot.series.modality,
            ]
            if statuses and all(s != "ok" for s in statuses.values()):
                if len(set(statuses.values())) == 1:
                    sheet.rows.append(
                        [*head, None, self._reason(key, next(iter(statuses.values())))]
                    )
                    continue
            for model, status in statuses.items():
                if status != "ok":
                    sheet.rows.append([*head, model_key(model), self._reason(key, status)])
        return sheet

    def _reason(self, series_key: str, status: str) -> str:
        if status == "failed":
            return f"failed: {self.failed_jobs.get(series_key, 'unknown')}"
        if status == "excluded":
            return "excluded by QC"
        return "not run"

    def methods_text(self, excluded: int) -> str:
        versions = self.data.run.versions
        chip = versions.get("chip", "unknown hardware")
        segmented = {
            slot.series.series_key
            for slot in self.slots
            if any((slot.series.series_key, m.name) in self.results for m in self.models)
        }
        reviewed = sum(1 for key in segmented if self.qc.reviewed(key))
        # The paragraph is pasted into papers, so it claims a review only as
        # far as one was recorded; plan §11's sentence is the complete case.
        if segmented and reviewed == len(segmented):
            qc = f"Results underwent visual quality control; {excluded} series were excluded."
        elif reviewed:
            qc = (
                f"{reviewed} of {len(segmented)} series underwent visual quality control; "
                f"{excluded} series were excluded."
            )
        else:
            qc = "No visual quality control was recorded for these results."
        return (
            f"Segmentations were generated with MOOSE v{versions.get('moosez', 'unknown')} "
            "(Shiyam Sundar et al., J Nucl Med 2022), based on nnU-Net (Isensee et al., "
            f"Nat Methods 2021), using {APP_NAME} v{versions.get('app', __version__)} on "
            f"{chip} (PyTorch {self.data.run.device}). {qc}"
        )

    def provenance_sheet(self, created: str, methods: str) -> Sheet:
        run = self.data.run
        sheet = Sheet(
            "provenance",
            [Column("key", "text", "", "what is described"), Column("value", "text", "", "value")],
        )
        rows: list[tuple[str, object]] = [
            ("intended_use", SHORT),
            ("intended_use_statement", STATEMENT),
            ("application", APP_NAME),
            ("export_created_at", created),
            ("run_id", run.run_id),
            ("run_created_at", run.created_at),
            ("device", run.device),
        ]
        rows += [(f"version.{k}", v) for k, v in sorted(run.versions.items())]
        rows += [(f"model.{m.name}.sha256", m.zip_sha256) for m in self.models]
        rows += [
            ("run_settings", json.dumps(run.settings, sort_keys=True)),
            ("export_options", json.dumps(asdict(self.options), sort_keys=True)),
            ("methods_text", methods if self.options.methods_text else None),
            *[(f"citation.{i}", c) for i, c in enumerate(CITATIONS, start=1)],
            ("bibtex", BIBTEX),
            ("model_attribution", MODEL_ATTRIBUTION),
        ]
        sheet.rows = [[key, value] for key, value in rows]
        return sheet


def _dictionary(sheets: Sequence[Sheet]) -> Sheet:
    dictionary = Sheet(
        "data_dictionary",
        [
            Column("sheet", "text", "", "sheet the column is in"),
            Column("column", "text", "", "column name"),
            Column("description", "text", "", "what the column holds"),
            Column("unit", "text", "", "unit of the values"),
            Column("type", "text", "", "text, integer, number or boolean"),
        ],
    )
    for sheet in sheets:
        for column in sheet.columns:
            dictionary.rows.append(
                [sheet.name, column.name, column.description, column.unit or None, column.type]
            )
    return dictionary


def build(data: ProjectData, options: ExportOptions, *, created: str) -> Export:
    """All sheets of one export, in the plan's order.

    `created` is passed in rather than read from the clock so that the same
    project and options give the same file, which the golden files rely on.
    """
    builder = _Builder(data, options)
    if options.identifiers != "pseudonym":
        builder.warnings.append(
            "This export contains DICOM PatientIDs. Treat the file as identifying data."
        )
        if any(p.patient_id is None for p in data.patients):
            builder.warnings.append(
                "Some patients have no stored PatientID (identifiers were removed); "
                "their cells are empty."
            )

    results = builder.results_sheet()
    excluded = sum(
        1 for s in builder.slots if builder.qc.series_status(s.series.series_key) == "rejected"
    )
    methods = builder.methods_text(excluded)
    sheets = [
        results,
        builder.series_sheet(),
        builder.qc_sheet(),
        builder.labels_sheet(),
    ]
    provenance = builder.provenance_sheet(created, methods)
    skipped = builder.skipped_sheet()
    dictionary = _dictionary([*sheets, provenance, skipped])
    sheets += [dictionary, provenance, skipped]
    assert tuple(s.name for s in sheets) == SHEET_ORDER

    masks = [
        (
            slot.series.series_key,
            model.name,
            "_".join(
                [
                    slot.patient.pseudonym,
                    f"t{slot.timepoint}",
                    f"s{slot.series_index}",
                    model_key(model.name),
                ]
            ),
        )
        for slot in builder.slots
        for model in builder.models
        if builder.model_status(slot.series.series_key, model.name) == "ok"
    ]
    return Export(sheets, builder.warnings, masks, methods, excluded)
