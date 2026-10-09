"""The scan's budgets (ADR 0026 decisions 3 and 4): 10 000 files on every
push, 100 000 in the slow job (`pytest -m slow`).

The trees are one series written by the corpus generator and copied, so that
making 10 000 files takes about a second instead of twenty. The copies share
their UIDs, which costs the header read nothing; the regroup's budget, which
duplicates would distort, is held on 100 000 distinct file rows written
straight into the catalog instead, because the regroup reads nothing else.
"""

from __future__ import annotations

import resource
import shutil
import sys
from pathlib import Path

import pytest

import dicom_factory
from bcoa_worker.index.catalog import open_catalog
from bcoa_worker.index.read import READER_VERSION
from index_jobs import catalog_path, scan_in_process, scan_in_worker

SERIES = 250


def _tree(root: Path, files: int) -> Path:
    template = root / "template"
    dicom_factory.write_bulk(template, SERIES, per_series=SERIES)
    series = template / "bulk" / "00000"
    for copy in range(files // SERIES):
        shutil.copytree(series, root / "source" / f"{copy // 100:03d}" / f"{copy:05d}")
    shutil.rmtree(template)
    return root / "source"


def _peak_child_mb() -> float:
    # ru_maxrss is in bytes on macOS and in KiB on Linux. It is the largest
    # child this process has waited for, so with both slow tests in one run it
    # is an upper bound for either.
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return peak / 1e6 if sys.platform == "darwin" else peak * 1024 / 1e6


def test_ten_thousand_files(tmp_path: Path) -> None:
    source = _tree(tmp_path, 10_000)
    full = scan_in_process(tmp_path, {1: source})
    assert full.code == 0
    assert full.result["files"]["read"] == 10_000
    assert full.seconds < 60, full.seconds
    again = scan_in_process(tmp_path, {1: source})
    assert again.result["changed"] is False
    assert again.result["files"]["read"] == 0
    assert again.seconds < 2, again.seconds


@pytest.mark.slow
def test_a_hundred_thousand_files(tmp_path: Path) -> None:
    source = _tree(tmp_path, 100_000)
    full = scan_in_worker(tmp_path / "full", {1: source})
    assert full.code == 0, full.log[-2000:]
    assert full.result["files"]["read"] == 100_000
    assert full.seconds <= 150, full.seconds
    assert _peak_child_mb() <= 500
    shutil.copytree(full.project_dir, tmp_path / "again" / full.project_dir.name)
    again = scan_in_worker(tmp_path / "again", {1: source})
    assert again.result["changed"] is False
    assert again.seconds <= 10, again.seconds


_FILE_COLUMNS = (
    "source_id, rel_path, size, mtime_ns, reader_version, kind, sop_class_uid, sop_uid, "
    "study_uid, series_uid, for_uid, transfer_syntax_uid, pid_link, pid_state, patient_id, "
    "accession_number, sex, age_years, study_date, study_description, modality, series_number, "
    "series_description, image_type, kernel, manufacturer, slice_thickness, pixel_spacing_row, "
    "pixel_spacing_col, image_rows, image_columns, iop, ipp_x, ipp_y, ipp_z, instance_number, "
    "acquisition_number, rescale_slope, rescale_intercept, pixel_data"
)


def _distinct_rows(project_dir: Path, studies: int, series: int, *, dicomdir: bool = False) -> None:
    """The rows the reader would have written for `studies` patients with
    `series` axial CT series of SERIES slices each, every file distinct;
    with `dicomdir`, and a DICOMDIR that lists every one of them, as every
    CD export carries."""
    rows = [
        (
            1,
            f"p{study:03d}/s{number}/IM{k:04d}.dcm".encode(),
            500_000,
            1,
            READER_VERSION,
            "image",
            "1.2.840.10008.5.1.4.1.1.2",
            f"2.25.{study}.{number}.{k}",
            f"2.25.{study}",
            f"2.25.{study}.{number}",
            f"2.25.{study}.0",
            "1.2.840.10008.1.2.1",
            f"pid:{study:064x}",
            "present",
            f"P{study:03d}",
            f"A{study:03d}",
            "F",
            50.0,
            "2024-01-01",
            "CT Abdomen",
            "CT",
            number + 1,
            f"Series {number}",
            "ORIGINAL\\PRIMARY\\AXIAL",
            "B30f",
            "SIEMENS",
            3.0,
            0.7,
            0.7,
            512,
            512,
            "1.0\\0.0\\0.0\\0.0\\1.0\\0.0",
            0.0,
            0.0,
            k * 1.5,
            k + 1,
            1,
            1.0,
            -1024.0,
            "ok",
        )
        for study in range(studies)
        for number in range(series)
        for k in range(SERIES)
    ]
    marks = ", ".join("?" * len(rows[0]))
    db = open_catalog(catalog_path(project_dir))
    try:
        db.execute("BEGIN")
        # The names are the constant above, never a value.
        db.executemany(f"INSERT INTO files ({_FILE_COLUMNS}) VALUES ({marks})", rows)  # noqa: S608
        if dicomdir:
            file_id = db.execute(
                "INSERT INTO files (source_id, rel_path, size, mtime_ns, reader_version, kind) "
                "VALUES (1, CAST('DICOMDIR' AS BLOB), 1000, 1, ?, 'dicomdir')",
                (READER_VERSION,),
            ).lastrowid
            db.executemany(
                "INSERT INTO dicomdir_entries (file_id, sop_uid, series_uid) VALUES (?, ?, ?)",
                [(file_id, row[7], row[9]) for row in rows],
            )
        db.execute("COMMIT")
    finally:
        db.close()


def test_a_dicomdir_that_lists_every_file_costs_the_regroup_little(tmp_path: Path) -> None:
    # 20 000 files: matched in SQL by a correlated subquery, the DICOMDIR's
    # entries took the regroup 45 s here, against 0.7 s without them, and it
    # grew with the square of the files.
    folder = tmp_path / "regroup"
    project_dir = folder / "Study.bcoaproj"
    _distinct_rows(project_dir, 20, 4, dicomdir=True)
    source = tmp_path / "source"
    source.mkdir()
    run = scan_in_process(folder, {1: source}, mode="regroup")
    assert run.code == 0
    assert run.result["series"] == 80
    assert run.seconds < 10, run.seconds


@pytest.mark.slow
def test_the_regroup_of_a_hundred_thousand_files(tmp_path: Path) -> None:
    # 100 studies of four series: 400 parts in 100 studies, as many as a
    # large cohort brings, and every file a distinct instance, all of them
    # listed by a DICOMDIR.
    folder = tmp_path / "regroup"
    project_dir = folder / "Study.bcoaproj"
    _distinct_rows(project_dir, 100, 4, dicomdir=True)
    source = tmp_path / "source"
    source.mkdir()
    # The worker child as the app starts it, so the time includes its start
    # and the peak is the regroup's own.
    run = scan_in_worker(folder, {1: source}, mode="regroup")
    assert run.code == 0, run.log[-2000:]
    assert run.result["changed"] is True
    assert run.seconds <= 20, run.seconds
    assert _peak_child_mb() <= 500
