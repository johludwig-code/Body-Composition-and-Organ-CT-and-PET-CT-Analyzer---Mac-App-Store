"""The options of the export dialog (plan §11), parsed from the job payload.

Every default here is the plan's default. An unknown value is refused rather
than mapped to a default: an export that silently used another layout than
the one chosen would be a wrong table with the right file name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from bcoa_worker.metrics import METRIC_COLUMNS
from bcoa_worker.naming import sanitize

Format = Literal["xlsx", "csv", "csv_excel"]
Layout = Literal["wide", "long"]
RowLevel = Literal["patient", "study", "series"]
MultipleStudies = Literal["suffix", "first", "last", "per_study"]
MultipleSeries = Literal["primary", "all"]
QCMode = Literal["exclude", "include"]
Identifiers = Literal["pseudonym", "patient_id", "both"]
Dates = Literal["days_since_first", "year", "iso"]


class ExportOptionError(ValueError):
    """An option the export dialog cannot have produced."""


@dataclass(frozen=True)
class ExportOptions:
    format: Format = "xlsx"
    layout: Layout = "wide"
    row_level: RowLevel = "patient"
    multiple_studies: MultipleStudies = "suffix"
    multiple_series: MultipleSeries = "primary"
    qc: QCMode = "exclude"
    identifiers: Identifiers = "pseudonym"
    dates: Dates = "days_since_first"
    # None means everything in the run; a tuple is the chosen subset.
    patients: tuple[str, ...] | None = None
    models: tuple[str, ...] | None = None
    labels: tuple[str, ...] | None = None
    metrics: tuple[str, ...] = METRIC_COLUMNS
    methods_text: bool = True
    masks: bool = False
    reproducibility_package: bool = False

    @property
    def one_row_per_study(self) -> bool:
        return self.row_level == "study" or (
            self.row_level == "patient" and self.multiple_studies == "per_study"
        )


def _choice(raw: Mapping[str, Any], key: str, allowed: Any, default: str) -> Any:
    value = raw.get(key, default)
    options = get_args(allowed)
    if value not in options:
        raise ExportOptionError(f"{key} must be one of {', '.join(options)}, not {value!r}")
    return value


def _subset(raw: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ExportOptionError(f"{key} must be a list of names")
    if not value:
        raise ExportOptionError(f"{key} is empty; leave it out to export everything")
    return tuple(value)


def parse_options(raw: Mapping[str, Any]) -> ExportOptions:
    known = set(ExportOptions.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ExportOptionError(f"unknown export options: {', '.join(unknown)}")

    metrics = _subset(raw, "metrics") or METRIC_COLUMNS
    wrong = [m for m in metrics if m not in METRIC_COLUMNS]
    if wrong:
        raise ExportOptionError(f"unknown metrics: {', '.join(wrong)}")
    # Plan order, whatever order the dialog sent: column order is part of the
    # golden files.
    metrics = tuple(m for m in METRIC_COLUMNS if m in metrics)

    labels = _subset(raw, "labels")
    if labels is not None:
        labels = tuple(sanitize(label) for label in labels)

    options = ExportOptions(
        format=_choice(raw, "format", Format, "xlsx"),
        layout=_choice(raw, "layout", Layout, "wide"),
        row_level=_choice(raw, "row_level", RowLevel, "patient"),
        multiple_studies=_choice(raw, "multiple_studies", MultipleStudies, "suffix"),
        multiple_series=_choice(raw, "multiple_series", MultipleSeries, "primary"),
        qc=_choice(raw, "qc", QCMode, "exclude"),
        identifiers=_choice(raw, "identifiers", Identifiers, "pseudonym"),
        dates=_choice(raw, "dates", Dates, "days_since_first"),
        patients=_subset(raw, "patients"),
        models=_subset(raw, "models"),
        labels=labels,
        metrics=metrics,
        methods_text=bool(raw.get("methods_text", True)),
        masks=bool(raw.get("masks", False)),
        reproducibility_package=bool(raw.get("reproducibility_package", False)),
    )
    if options.reproducibility_package:
        # ADR 0013: refused until M6 builds it, never silently left out.
        raise ExportOptionError("the reproducibility package is not built yet (milestone M6)")
    return options
