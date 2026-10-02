"""Sheets to files: XLSX through XlsxWriter, CSV through the csv module.

The rules against Excel's traps (plan §11) are enforced here, in one place,
for every sheet: text is always written as text, numbers as numbers, missing
values as empty cells, one header row, frozen, with an autofilter.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

import xlsxwriter

from bcoa_worker.export.tables import Column, Sheet
from bcoa_worker.naming import check_dimensions

CsvDialect = Literal["csv", "csv_excel"]

# Excel refuses longer strings in a cell; the methods text and BibTeX are far
# below it, a free-text comment might not be.
EXCEL_MAX_CELL_TEXT = 32_767

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _check(sheet: Sheet) -> None:
    # Header row included: Excel counts it.
    check_dimensions(len(sheet.rows) + 1, len(sheet.columns))
    for row in sheet.rows:
        if len(row) != len(sheet.columns):
            raise ValueError(
                f"sheet {sheet.name}: a row has {len(row)} of {len(sheet.columns)} cells"
            )


def _number(value: object) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        # NaN and infinity would become "#NUM!" in Excel and "nan" in CSV;
        # both are worse than the empty cell that a missing value is.
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    raise TypeError(f"not a number: {value!r}")


def write_xlsx(sheets: Sequence[Sheet], path: Path, *, created: datetime) -> None:
    for sheet in sheets:
        _check(sheet)
    workbook = xlsxwriter.Workbook(
        str(path),
        {
            # Rows are written once, in order; constant memory keeps a wide
            # export of a large cohort from holding every cell (ADR 0008).
            "constant_memory": True,
            # Never turn a series description into a formula, a URL or a
            # number: every text cell goes through write_string anyway, these
            # make sure no other path does it either.
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "strings_to_numbers": False,
        },
    )
    # A fixed creation time makes the same export produce the same file.
    workbook.set_properties({"created": created})
    header = workbook.add_format({"bold": True})
    number = workbook.add_format({"num_format": "0.00"})
    try:
        for sheet in sheets:
            worksheet = workbook.add_worksheet(sheet.name)
            for col, column in enumerate(sheet.columns):
                worksheet.write_string(0, col, column.name, header)
            for r, row in enumerate(sheet.rows, start=1):
                for col, (column, value) in enumerate(zip(sheet.columns, row, strict=True)):
                    _write_cell(worksheet, r, col, column, value, number)
            worksheet.freeze_panes(1, 0)
            if sheet.columns:
                worksheet.autofilter(0, 0, max(len(sheet.rows), 1), len(sheet.columns) - 1)
    finally:
        workbook.close()


def _write_cell(
    worksheet: xlsxwriter.worksheet.Worksheet,
    row: int,
    col: int,
    column: Column,
    value: object,
    number: xlsxwriter.format.Format,
) -> None:
    if value is None:
        return
    if column.type == "text":
        text = str(value)
        if len(text) > EXCEL_MAX_CELL_TEXT:
            raise ValueError(f"column {column.name}: text longer than Excel allows in a cell")
        worksheet.write_string(row, col, text)
    elif column.type == "boolean":
        if not isinstance(value, bool):
            raise TypeError(f"column {column.name}: not a boolean: {value!r}")
        worksheet.write_boolean(row, col, value)
    else:
        numeric = _number(value)
        if numeric is None:
            return
        if column.type == "integer":
            worksheet.write_number(row, col, numeric)
        else:
            worksheet.write_number(row, col, numeric, number)


def _csv_text(value: object, column: Column, dialect: CsvDialect) -> str:
    if value is None:
        return ""
    if column.type == "boolean":
        return "TRUE" if value else "FALSE"
    if column.type == "text":
        text = str(value)
        if dialect == "csv_excel" and text.startswith(_FORMULA_START):
            # ADR 0013: Excel evaluates a leading "=" even from CSV.
            return "'" + text
        return text
    numeric = _number(value)
    if numeric is None:
        return ""
    # repr keeps full precision (plan §10: round only in the display); a
    # fixed number of decimals would change values on the way out.
    text = repr(numeric) if isinstance(numeric, float) else str(numeric)
    if dialect == "csv_excel":
        text = text.replace(".", ",")
    return text


def write_csv(sheet: Sheet, path: Path, dialect: CsvDialect) -> None:
    for row in sheet.rows:
        if len(row) != len(sheet.columns):
            raise ValueError(
                f"sheet {sheet.name}: a row has {len(row)} of {len(sheet.columns)} cells"
            )
    excel = dialect == "csv_excel"
    # Excel reads UTF-8 without a byte order mark as the system code page.
    encoding = "utf-8-sig" if excel else "utf-8"
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, delimiter=";" if excel else ",", lineterminator="\r\n")
        writer.writerow([c.name for c in sheet.columns])
        for row in sheet.rows:
            writer.writerow(
                [_csv_text(v, c, dialect) for c, v in zip(sheet.columns, row, strict=True)]
            )


def csv_paths(stem: str, folder: Path, sheets: Sequence[Sheet]) -> dict[str, Path]:
    """`<stem>.csv` for the results, `<stem>.<sheet>.csv` for the others."""
    return {
        sheet.name: folder
        / (f"{stem}.csv" if sheet.name == "results" else f"{stem}.{sheet.name}.csv")
        for sheet in sheets
    }
