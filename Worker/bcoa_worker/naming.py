"""Names that end up as export columns, sheet names and UI labels (plan §11).

One definition for the export and the data dictionary: if they were computed in
two places they would eventually disagree, and a column that the dictionary
does not describe is a column nobody can cite.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

EXCEL_MAX_COLUMNS = 16_384
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_SHEET_NAME = 31

_NOT_ALLOWED = re.compile(r"[^a-z0-9_]+")
_REPEATED = re.compile(r"_+")


def sanitize(name: str) -> str:
    """Lowercase a–z, 0–9 and underscore; anything else becomes one underscore.

    MOOSE writes `vertebra_L3`; the export column is `vertebra_l3`.
    """
    cleaned = _NOT_ALLOWED.sub("_", name.strip().lower())
    cleaned = _REPEATED.sub("_", cleaned).strip("_")
    if not cleaned:
        raise ValueError(f"name has nothing left after sanitising: {name!r}")
    return cleaned


def model_key(model: str) -> str:
    """Short model name for columns: `clin_ct_organs` → `organs`.

    Only the clinical CT prefix is dropped, because every v1 model shares it
    and it would add eight characters to 1 440 columns. Other prefixes stay so
    that `preclin_ct_legs` and a future `clin_pt_…` model cannot collide.
    """
    key = sanitize(model)
    return key.removeprefix("clin_ct_") or key


def display_name(label: str) -> str:
    """`kidney_left` → "Kidney left"; `vertebra_L3` → "Vertebra L3".

    Tokens that MOOSE writes with capitals (vertebra levels, abbreviations)
    keep them; the export keeps the sanitised MOOSE name.
    """
    words = [w for w in label.replace("-", "_").split("_") if w]
    if not words:
        return label
    shown = [words[0][:1].upper() + words[0][1:], *words[1:]]
    return " ".join(shown)


def wide_column(
    model: str,
    label: str,
    metric: str,
    *,
    timepoint: int | None = None,
    series: int | None = None,
) -> str:
    """`<model>__<label>__<metric>[__t<n>][__s<n>]`."""
    parts = [model_key(model), sanitize(label), sanitize(metric)]
    if timepoint is not None:
        if timepoint < 1:
            raise ValueError("timepoints count from 1")
        parts.append(f"t{timepoint}")
    if series is not None:
        if series < 1:
            raise ValueError("series count from 1")
        parts.append(f"s{series}")
    return "__".join(parts)


def status_column(model: str, *, timepoint: int | None = None) -> str:
    column = f"{model_key(model)}__status"
    return f"{column}__t{timepoint}" if timepoint is not None else column


def timepoints(study_dates: Sequence[date]) -> list[int]:
    """Timepoint number of each study, by date: earliest is t1.

    Ties keep their input order, so the result is deterministic for the
    deterministic input order the export uses (study UID hash).
    """
    order = sorted(range(len(study_dates)), key=lambda i: (study_dates[i], i))
    numbers = [0] * len(study_dates)
    for rank, index in enumerate(order, start=1):
        numbers[index] = rank
    return numbers


def check_sheet_name(name: str) -> str:
    if not name or len(name) > EXCEL_MAX_SHEET_NAME or any(c in name for c in "[]:*?/\\"):
        raise ValueError(f"not a valid Excel sheet name: {name!r}")
    return name


def check_dimensions(rows: int, columns: int) -> None:
    """Fail with advice before XlsxWriter fails with a stack trace."""
    if columns > EXCEL_MAX_COLUMNS:
        raise ValueError(
            f"{columns} columns exceed Excel's {EXCEL_MAX_COLUMNS}; "
            "use the long layout, fewer metrics, or CSV"
        )
    if rows > EXCEL_MAX_ROWS:
        raise ValueError(f"{rows} rows exceed Excel's {EXCEL_MAX_ROWS}; use the wide layout or CSV")
