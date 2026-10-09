"""The regroup (ADR 0020 decision 2, ADR 0022 decisions 8 to 10) on catalogs
written row by row: the split cascade, duplicates, part identity across
generations, and generations themselves, published whole or not at all.

The rows are what the reader would have written for synthetic files, so
each test states exactly the headers it is about; the corpus test runs the
same code on real files, and `test_index_crash.py` kills it.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import dicom_factory
from bcoa_worker import __version__
from bcoa_worker.index import group
from bcoa_worker.index.catalog import open_catalog
from bcoa_worker.index.identity import IdentityConfig, folder_link
from bcoa_worker.index.read import READER_VERSION
from bcoa_worker.index.select import SelectionConfig
from bcoa_worker.worker import Cancelled
from index_jobs import derived, index_job, meta, result_of, run_index

AXIAL = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
# Rows along x, columns down -z: the normal is +y.
CORONAL = (1.0, 0.0, 0.0, 0.0, 0.0, -1.0)
CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
ENHANCED_CT = "1.2.840.10008.5.1.4.1.1.2.1"
COMPREHENSIVE_SR = "1.2.840.10008.5.1.4.1.1.88.33"
EXPLICIT = "1.2.840.10008.1.2.1"
DEFLATED = "1.2.840.10008.1.2.1.99"
KEY = bytes(range(32))


def _iop(values: tuple[float, ...]) -> str:
    return "\\".join(repr(v) for v in values)


def _tilted(degrees: float) -> tuple[float, ...]:
    """Axial, with the columns turned about x: the normal leaves z by that
    angle."""
    angle = math.radians(degrees)
    return (1.0, 0.0, 0.0, 0.0, math.cos(angle), -math.sin(angle))


class Catalog:
    """A catalog filled with file rows as the reader writes them."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.db = open_catalog(path)
        self.count = 0

    def add(
        self,
        series: str = "s1",
        *,
        z: float = 0.0,
        position: tuple[float, float, float] | None = None,
        **values: Any,
    ) -> int:
        """One file of `series`; every column not given has the value of an
        ordinary axial CT slice at `z`."""
        self.count += 1
        x, y, z = position if position is not None else (0.0, 0.0, z)
        row: dict[str, Any] = {
            "source_id": 1,
            "rel_path": f"{series}/IM{self.count:04d}.dcm".encode(),
            "size": 1000,
            "mtime_ns": 1,
            "reader_version": READER_VERSION,
            "kind": "image",
            "sop_class_uid": CT_IMAGE,
            "sop_uid": f"{series}.{self.count}",
            "study_uid": "st1",
            "series_uid": series,
            "for_uid": "for1",
            "transfer_syntax_uid": EXPLICIT,
            "pid_link": "pid:one",
            "pid_state": "present",
            "patient_id": "ONE",
            "accession_number": "ACC1",
            "sex": "F",
            "age_years": 50.0,
            "study_date": "2024-01-02",
            "study_description": "CT Study",
            "modality": "CT",
            "series_number": 2,
            "series_description": f"Series {series}",
            "image_type": "ORIGINAL\\PRIMARY\\AXIAL",
            "kernel": "B30f",
            "manufacturer": "SIEMENS",
            "slice_thickness": 3.0,
            "pixel_spacing_row": 0.7,
            "pixel_spacing_col": 0.7,
            "image_rows": 16,
            "image_columns": 16,
            "iop": _iop(AXIAL),
            "ipp_x": x,
            "ipp_y": y,
            "ipp_z": z,
            "instance_number": self.count,
            "acquisition_number": 1,
            "rescale_slope": 1.0,
            "rescale_intercept": -1024.0,
            "pixel_data": "ok",
        }
        row.update(values)
        names = ", ".join(row)
        marks = ", ".join(f":{name}" for name in row)
        cursor = self.db.execute(f"INSERT INTO files ({names}) VALUES ({marks})", row)  # noqa: S608
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def stack(self, series: str, zs: list[float], **values: Any) -> list[int]:
        return [self.add(series, z=z, **values) for z in zs]

    def frames(
        self, file_id: int, frames: list[tuple[float, tuple[float, ...], str | None]]
    ) -> None:
        """Per-frame rows of a multi-frame file: (z, orientation, StackID)."""
        self.db.executemany(
            "INSERT INTO frames (file_id, frame, ipp_x, ipp_y, ipp_z, iop, stack_id) "
            "VALUES (?, ?, 0.0, 0.0, ?, ?, ?)",
            [
                (file_id, number, z, _iop(iop), stack)
                for number, (z, iop, stack) in enumerate(frames, start=1)
            ],
        )
        self.db.execute(
            "UPDATE files SET frames = ?, sop_class_uid = ?, ipp_z = ?, iop = ? WHERE file_id = ?",
            (len(frames), ENHANCED_CT, frames[0][0], _iop(frames[0][1]), file_id),
        )

    def remove(self, file_ids: list[int]) -> None:
        self.db.executemany("DELETE FROM files WHERE file_id = ?", [(i,) for i in file_ids])

    def regroup(
        self,
        *,
        min_slices: int = 3,
        key: bytes | None = KEY,
        identity: IdentityConfig | None = None,
        check_cancelled: Callable[[], None] = lambda: None,
        **context: Any,
    ) -> group.GroupOutcome:
        return group.regroup(
            self.db,
            group.RegroupContext(
                selection=SelectionConfig(min_slices=min_slices),
                identity=identity or IdentityConfig(),
                key=key,
                link_key_id="0123456789abcdef" if key is not None else "",
                check_cancelled=check_cancelled,
                **context,
            ),
        )

    def parts(self, series: str | None = None) -> list[dict[str, Any]]:
        """The cat_series rows, of one series or all, in series and part
        order, each with its checks and its instance keys in stack order."""
        rows = []
        self.db.row_factory = _dict_row
        try:
            for row in self.db.execute(
                "SELECT * FROM cat_series WHERE ? IS NULL OR series_uid = ? "
                "ORDER BY series_uid, part",
                (series, series),
            ):
                ref = str(row["part_ref"])
                row["checks"] = {
                    r["code"]: json.loads(r["params_json"])
                    for r in self.db.execute(
                        "SELECT code, params_json FROM cat_checks "
                        "WHERE object_kind = 'series' AND object_ref = ?",
                        (ref,),
                    )
                }
                row["keys"] = [
                    r["sop_key"]
                    for r in self.db.execute(
                        "SELECT sop_key FROM cat_instances WHERE part_ref = ? ORDER BY ordinal",
                        (row["part_ref"],),
                    )
                ]
                row["reason"] = json.loads(row["reason_json"])
                rows.append(row)
        finally:
            self.db.row_factory = None
        return rows

    def meta(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT key, value FROM catalog_meta"))

    def table(self, name: str) -> list[tuple[Any, ...]]:
        return sorted(self.db.execute(f"SELECT * FROM {name}"), key=repr)  # noqa: S608


def _dict_row(cursor: Any, row: tuple[Any, ...]) -> dict[str, Any]:
    return {column[0]: value for column, value in zip(cursor.description, row, strict=True)}


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    made = Catalog(tmp_path / "index" / "catalog.sqlite")
    try:
        yield made
    finally:
        made.db.close()


# ------------------------------------------------------------------ one part


def test_a_series_with_nothing_to_split_is_one_part(catalog: Catalog) -> None:
    ids = catalog.stack("s1", [12.0, 0.0, 6.0, 3.0, 9.0])
    outcome = catalog.regroup()
    assert (outcome.studies, outcome.series, outcome.parts_split, outcome.duplicates) == (
        1,
        1,
        0,
        0,
    )
    [part] = catalog.parts()
    keys = [f"s1.{n}" for n in (2, 4, 3, 5, 1)]
    assert part["keys"] == keys
    assert part["fingerprint"] == hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()
    assert (part["part"], part["image_count"], part["slice_count"]) == (0, 5, 5)
    assert (part["z_extent_mm"], part["slice_spacing_mm"], part["orientation"]) == (
        12.0,
        3.0,
        "axial",
    )
    # The middle of the stack, z = 6, is the third file written.
    assert (part["middle_file_id"], part["middle_frame"]) == (ids[2], 0)
    assert part["checks"] == {}
    assert part["reason"]["outcome"] == "chosen"


# ------------------------------------------------------------------ the cascade


def test_step_a_splits_images_from_non_images(catalog: Catalog) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    catalog.add(
        "s1",
        kind="non_image",
        sop_class_uid=COMPREHENSIVE_SR,
        modality="SR",
        iop=None,
        ipp_x=None,
        ipp_y=None,
        ipp_z=None,
        image_type=None,
        pixel_data=None,
    )
    assert catalog.regroup().parts_split == 2
    parts = {p["modality"]: p for p in catalog.parts()}
    assert {m: p["checks"].get("check.split") for m, p in parts.items()} == {
        "CT": {"reason": "sop_class"},
        "SR": {"reason": "sop_class"},
    }
    # A dose report has no geometry to report on (C2 of the corpus).
    assert set(parts["SR"]["checks"]) == {"check.split"}
    assert parts["SR"]["reason"]["codes"][0] == "select.excluded.not_image"


def test_step_b_splits_by_orientation_and_not_within_a_degree(catalog: Catalog) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    for y in (0.0, 3.0, 6.0):
        catalog.add("s1", position=(0.0, y, 0.0), iop=_iop(CORONAL))
    catalog.stack("s2", [0.0, 3.0, 6.0])
    catalog.stack("s2", [9.0, 12.0], iop=_iop(_tilted(0.5)))
    catalog.regroup()
    split = catalog.parts("s1")
    assert [p["orientation"] for p in split] == ["axial", "coronal"]
    assert all(p["checks"]["check.split"] == {"reason": "orientation"} for p in split)
    [whole] = catalog.parts("s2")
    assert whole["image_count"] == 5 and "check.split" not in whole["checks"]


def test_step_c_splits_by_matrix(catalog: Catalog) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    catalog.stack("s1", [0.0, 3.0, 6.0], image_rows=20, image_columns=20)
    catalog.regroup()
    parts = catalog.parts("s1")
    assert sorted(p["image_rows"] for p in parts) == [16, 20]
    assert all(p["checks"]["check.split"] == {"reason": "size"} for p in parts)


def test_step_d_splits_spacings_more_than_one_percent_apart(catalog: Catalog) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    catalog.stack("s1", [0.0, 3.0, 6.0], pixel_spacing_row=0.75, pixel_spacing_col=0.75)
    catalog.stack("s2", [0.0, 3.0, 6.0])
    catalog.stack("s2", [9.0, 12.0, 15.0], pixel_spacing_row=0.704, pixel_spacing_col=0.704)
    catalog.regroup()
    split = catalog.parts("s1")
    assert sorted(p["pixel_spacing_mm"] for p in split) == [0.7, 0.75]
    assert all(p["checks"]["check.split"] == {"reason": "pixel_spacing"} for p in split)
    assert len(catalog.parts("s2")) == 1


def test_files_without_an_orientation_are_set_apart_with_a_reason_that_says_so(
    catalog: Catalog,
) -> None:
    # Nothing changes in s1: one file has no ImageOrientationPatient.
    catalog.stack("s1", [0.0, 3.0, 6.0, 9.0])
    catalog.add("s1", z=12.0, iop=None)
    # In s2 the orientation does change, and one file has none as well.
    catalog.stack("s2", [0.0, 3.0, 6.0])
    for y in (0.0, 3.0, 6.0):
        catalog.add("s2", position=(0.0, y, 0.0), iop=_iop(CORONAL))
    catalog.add("s2", z=12.0, iop=None)
    catalog.regroup()
    s1 = catalog.parts("s1")
    assert sorted(p["image_count"] for p in s1) == [1, 4]
    assert all(p["checks"]["check.split"] == {"reason": "missing_orientation"} for p in s1)
    reasons = {p["orientation"]: p["checks"]["check.split"]["reason"] for p in catalog.parts("s2")}
    assert reasons == {
        "axial": "orientation",
        "coronal": "orientation",
        None: "missing_orientation",
    }


def test_files_without_a_pixel_spacing_are_set_apart_with_a_reason_that_says_so(
    catalog: Catalog,
) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0, 9.0])
    catalog.add("s1", z=12.0, pixel_spacing_row=None, pixel_spacing_col=None)
    catalog.regroup()
    s1 = catalog.parts("s1")
    assert sorted(p["image_count"] for p in s1) == [1, 4]
    assert all(p["checks"]["check.split"] == {"reason": "missing_pixel_spacing"} for p in s1)


@pytest.mark.parametrize("tag", ["acquisition_number", "temporal_position", "echo_number"])
def test_step_e_splits_phases_at_repeated_positions(catalog: Catalog, tag: str) -> None:
    zs = [0.0, 3.0, 6.0, 9.0]
    # The second phase is written first, with the lower InstanceNumbers.
    second = catalog.stack("s1", zs, **{tag: 2})
    first = catalog.stack("s1", zs, **{tag: 1})
    catalog.regroup()
    parts = catalog.parts("s1")
    assert [p["image_count"] for p in parts] == [4, 4]
    assert all(p["checks"]["check.split"] == {"reason": "acquisition"} for p in parts)
    # Both start at z = 0, so the lower InstanceNumbers come first.
    assert parts[0]["middle_file_id"] in second and parts[1]["middle_file_id"] in first
    assert all("check.duplicate_positions" not in p["checks"] for p in parts)


def test_step_e_needs_a_tag_with_one_value_per_phase(catalog: Catalog) -> None:
    # A sequential scan numbers every slice as an acquisition of its own;
    # one slice written twice must not split it into a part per slice.
    for number, z in enumerate([0.0, 3.0, 6.0, 9.0, 9.0, 12.0], start=1):
        catalog.add("s1", z=z, acquisition_number=number)
    # Acquisition numbers without a repeated position say nothing either.
    for number, z in enumerate([0.0, 3.0, 6.0, 9.0], start=1):
        catalog.add("s2", z=z, acquisition_number=number)
    catalog.regroup()
    [repeated] = catalog.parts("s1")
    assert (repeated["image_count"], repeated["slice_count"]) == (6, 5)
    assert repeated["checks"]["check.duplicate_positions"] == {"count": 2}
    [plain] = catalog.parts("s2")
    assert "check.split" not in plain["checks"]


def test_step_f_splits_stacks_and_never_a_multi_frame_file(catalog: Catalog) -> None:
    one = catalog.add("s1", instance_number=2)
    two = catalog.add("s1", instance_number=1)
    catalog.frames(one, [(z, AXIAL, "1") for z in (0.0, 3.0, 6.0)])
    catalog.frames(two, [(z, AXIAL, "2") for z in (0.0, 3.0, 6.0)])
    catalog.regroup()
    parts = catalog.parts("s1")
    assert all(p["checks"]["check.split"] == {"reason": "stack"} for p in parts)
    # One file each, every frame an instance; part 0 holds InstanceNumber 1.
    assert parts[0]["keys"] == ["s1.2#1", "s1.2#2", "s1.2#3"]
    assert parts[1]["keys"] == ["s1.1#1", "s1.1#2", "s1.1#3"]
    assert parts[0]["middle_frame"] == 2


def test_a_file_with_mixed_frames_stays_whole_without_geometry(catalog: Catalog) -> None:
    mixed = catalog.add("s1")
    catalog.frames(mixed, [(0.0, AXIAL, None), (3.0, AXIAL, None), (6.0, CORONAL, None)])
    catalog.regroup()
    [part] = catalog.parts("s1")
    assert part["image_count"] == 3 and part["slice_count"] is None
    assert {"check.enhanced_mixed_frames", "check.no_geometry"} <= set(part["checks"])
    assert "select.excluded.mixed_frames" in part["reason"]["codes"]


def test_gantry_tilt_is_not_a_split(catalog: Catalog) -> None:
    # The table moves along z while the slices stay upright in y: a shear.
    shift = math.tan(math.radians(15.0))
    for z in (0.0, 3.0, 6.0, 9.0):
        catalog.add("s1", position=(0.0, z * shift, z))
    catalog.regroup()
    [part] = catalog.parts("s1")
    assert part["checks"]["check.gantry_tilt"] == {"degrees": 15.0}


def test_splits_nest_and_each_part_names_its_own_reason(catalog: Catalog) -> None:
    zs = [0.0, 3.0, 6.0]
    catalog.stack("s1", zs, acquisition_number=1)
    catalog.stack("s1", zs, acquisition_number=2)
    for y in zs:
        catalog.add("s1", position=(0.0, y, 0.0), iop=_iop(CORONAL))
    catalog.regroup()
    reasons = sorted(
        (p["orientation"], p["checks"]["check.split"]["reason"]) for p in catalog.parts("s1")
    )
    assert reasons == [
        ("axial", "acquisition"),
        ("axial", "acquisition"),
        ("coronal", "orientation"),
    ]


def test_parts_are_numbered_by_lowest_position_first(catalog: Catalog) -> None:
    # The larger matrix lies lower but was written later.
    catalog.stack("s1", [0.0, 3.0, 6.0])
    catalog.stack("s1", [-30.0, -27.0, -24.0], image_rows=20, image_columns=20)
    catalog.regroup()
    assert [p["image_rows"] for p in catalog.parts("s1")] == [20, 16]


def test_one_series_uid_in_two_studies_is_numbered_across_them(catalog: Catalog) -> None:
    # The series exported again under a new study UID, with new SOP UIDs, so
    # that no duplicate rule joins them (ADR 0028 decision 2). Numbered per
    # study, both would be part 0 and break UNIQUE (series_uid, part).
    catalog.stack("s1", [0.0, 3.0, 6.0], study_uid="st2")
    catalog.stack("s1", [0.0, 3.0, 6.0], study_uid="st1")
    catalog.regroup()
    parts = catalog.parts("s1")
    assert [(p["study_uid"], p["part"]) for p in parts] == [("st1", 0), ("st2", 1)]
    assert all("check.split" not in p["checks"] for p in parts)


# ------------------------------------------------------------------ duplicates


def _copy(catalog: Catalog, series: str, z: float, sop: str, path: str, **values: Any) -> int:
    return catalog.add(series, z=z, sop_uid=sop, rel_path=path.encode(), **values)


@pytest.mark.parametrize(
    ("loser", "winner"),
    [
        # Complete pixel data beats a path that sorts first.
        ({"rel_path": b"a/1.dcm", "pixel_data": "truncated"}, {"rel_path": b"b/1.dcm"}),
        # A syntax the converter reads beats one it does not.
        ({"rel_path": b"a/1.dcm", "transfer_syntax_uid": DEFLATED}, {"rel_path": b"b/1.dcm"}),
        # Then the lower source, whatever its path.
        ({"source_id": 2, "rel_path": b"a/1.dcm"}, {"source_id": 1, "rel_path": b"z/1.dcm"}),
        # Then the lower path, compared as bytes.
        ({"rel_path": b"b/1.dcm"}, {"rel_path": b"a/1.dcm"}),
    ],
)
def test_the_winner_of_a_duplicate(
    catalog: Catalog, loser: dict[str, Any], winner: dict[str, Any]
) -> None:
    catalog.add("s1", z=3.0)
    lost = catalog.add("s1", z=0.0, sop_uid="dup", **loser)
    won = catalog.add("s1", z=0.0, sop_uid="dup", **winner)
    outcome = catalog.regroup(min_slices=2)
    assert outcome.duplicates == 1
    [part] = catalog.parts("s1")
    assert part["image_count"] == 2
    assert part["checks"] == {"check.duplicates": {"count": 1}}
    files = {row[0] for row in catalog.db.execute("SELECT file_id FROM cat_instances")}
    assert won in files and lost not in files


def test_a_uid_conflict_keeps_the_winners_grouping(catalog: Catalog) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    # A stray file under another series reuses slice 2's instance UID and
    # loses by path: its series has no part, and s1 keeps the instance.
    catalog.add("stray", z=3.0, sop_uid="s1.2", rel_path=b"z/stray.dcm")
    # Another one wins by path, and its instance goes with its own series.
    catalog.add("s1", z=9.0, sop_uid="s3.1", rel_path=b"y/late.dcm")
    catalog.add("s3", z=9.0, sop_uid="s3.1", rel_path=b"a/early.dcm")
    assert catalog.regroup().duplicates == 2
    [s1] = catalog.parts("s1")
    assert s1["image_count"] == 3
    assert s1["checks"] == {
        "check.duplicates": {"count": 1},
        "check.uid_conflict": {"count": 1},
        # And s1's own late.dcm lost its instance to s3's file: the series
        # that loses a slice is told so as well (ADR 0029).
        "check.uid_conflict_lost": {"count": 1},
    }
    assert catalog.parts("stray") == []
    [s3] = catalog.parts("s3")
    assert s3["checks"]["check.uid_conflict"] == {"count": 1}
    assert "check.uid_conflict_lost" not in s3["checks"]


def _dicomdir(catalog: Catalog, entries: list[tuple[str, str]]) -> None:
    file_id = catalog.add(
        "dir", kind="dicomdir", sop_uid=None, study_uid=None, series_uid=None, rel_path=b"DICOMDIR"
    )
    catalog.db.executemany(
        "INSERT INTO dicomdir_entries (file_id, sop_uid, series_uid) VALUES (?, ?, ?)",
        [(file_id, sop_uid, series_uid) for sop_uid, series_uid in entries],
    )


def test_a_series_a_dicomdir_lists_and_no_part_holds_is_reported_on_the_source(
    catalog: Catalog,
) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    listed = [("s1.1", "s1"), ("s1.2", "s1"), ("s1.3", "s1"), ("s1.lost", "s1")]
    # The copy lost all of s2, which therefore has no part to carry a check.
    listed += [(f"s2.{k}", "s2") for k in range(5)] + [(f"s3.{k}", "s3") for k in range(2)]
    _dicomdir(catalog, listed)
    catalog.regroup()
    [s1] = catalog.parts("s1")
    assert s1["checks"]["check.dicomdir_incomplete"] == {"missing": 1}
    source = dict(
        catalog.db.execute(
            "SELECT code, params_json FROM cat_checks WHERE object_kind = 'source' "
            "AND object_ref = '1'"
        )
    )
    assert json.loads(source["check.dicomdir_series_missing"]) == {"series": 2, "missing": 7}


def test_a_frame_count_of_an_older_reader_is_bounded() -> None:
    # Reader 2 refuses such a file; a row of reader 1 may still claim it
    # until the scan that reads it again.
    assert group._frame_count(None, 2**31 - 1) == group.FRAME_CEILING
    assert group._frame_count(None, 3) == 3
    assert group._frame_count(7, 2**31 - 1) == 7


def test_copies_of_a_multi_frame_file_count_once(catalog: Catalog) -> None:
    frames = [(z, AXIAL, None) for z in (0.0, 3.0, 6.0)]
    for path in (b"a/enh.dcm", b"b/enh.dcm"):
        catalog.frames(catalog.add("s1", sop_uid="enh", rel_path=path), frames)
    assert catalog.regroup().duplicates == 1
    [part] = catalog.parts("s1")
    assert part["keys"] == ["enh#1", "enh#2", "enh#3"]
    assert part["checks"] == {"check.duplicates": {"count": 1}}


# ------------------------------------------------------------------ part identity


def _ref(catalog: Catalog, series: str = "s1") -> list[int]:
    return [p["part_ref"] for p in catalog.parts(series)]


def test_more_than_half_of_the_old_instances_keeps_the_part_ref(catalog: Catalog) -> None:
    ids = catalog.stack("s1", [float(z) for z in range(0, 30, 3)])
    catalog.regroup()
    [first] = _ref(catalog)
    catalog.remove(ids[:4])
    catalog.regroup()
    assert _ref(catalog) == [first]
    assert catalog.meta()["next_part_ref"] == str(first + 1)


def test_half_or_less_takes_a_new_part_ref_and_the_old_one_is_never_reused(
    catalog: Catalog,
) -> None:
    zs = [float(z) for z in range(0, 30, 3)]
    ids = catalog.stack("s1", zs)
    catalog.regroup()
    [first] = _ref(catalog)
    removed = [
        catalog.db.execute("SELECT sop_uid FROM files WHERE file_id = ?", (i,)).fetchone()[0]
        for i in ids[:5]
    ]
    catalog.remove(ids[:5])
    catalog.regroup()
    [second] = _ref(catalog)
    assert second == first + 1
    # The five come back from another folder: the part now holds every
    # instance of the first generation's part, but that part_ref is retired,
    # and the part keeps the second.
    for sop_uid, z in zip(removed, zs[:5], strict=True):
        catalog.add("s1", z=z, sop_uid=sop_uid, rel_path=f"back/{sop_uid}.dcm".encode())
    catalog.regroup()
    assert _ref(catalog) == [second]
    assert catalog.meta()["next_part_ref"] == str(second + 1)


def test_a_split_that_renumbers_keeps_each_part_ref_with_its_images(catalog: Catalog) -> None:
    zs = [0.0, 3.0, 6.0, 9.0]
    catalog.stack("s1", zs, acquisition_number=2, instance_number=None)
    catalog.regroup()
    [phase_two] = _ref(catalog)
    # A first phase arrives with lower InstanceNumbers: it becomes part 0,
    # and the images that were part 0 become part 1 with their part_ref.
    for number, z in enumerate(zs, start=1):
        catalog.add("s1", z=z, acquisition_number=1, instance_number=number)
    catalog.regroup()
    parts = catalog.parts("s1")
    assert [p["part"] for p in parts] == [0, 1]
    assert parts[1]["part_ref"] == phase_two
    assert parts[0]["part_ref"] == phase_two + 1


def test_a_part_that_is_gone_frees_no_part_ref(catalog: Catalog) -> None:
    gone = catalog.stack("s1", [0.0, 3.0, 6.0])
    catalog.stack("s2", [0.0, 3.0, 6.0])
    catalog.regroup()
    refs = {p["series_uid"]: p["part_ref"] for p in catalog.parts()}
    catalog.remove(gone)
    catalog.stack("s3", [0.0, 3.0, 6.0])
    catalog.regroup()
    after = {p["series_uid"]: p["part_ref"] for p in catalog.parts()}
    assert after == {"s2": refs["s2"], "s3": max(refs.values()) + 1}


def test_the_largest_overlap_takes_the_part_ref(catalog: Catalog) -> None:
    zs = [float(z) for z in range(0, 30, 3)]
    ids = catalog.stack("s1", zs)
    catalog.regroup()
    [first] = _ref(catalog)
    # Seven slices stay axial and three turn coronal: the seven keep it.
    catalog.db.executemany(
        "UPDATE files SET iop = ?, ipp_y = ipp_z, ipp_z = 0 WHERE file_id = ?",
        [(_iop(CORONAL), file_id) for file_id in ids[7:]],
    )
    catalog.regroup()
    by_orientation = {p["orientation"]: p["part_ref"] for p in catalog.parts("s1")}
    assert by_orientation == {"axial": first, "coronal": first + 1}


# ------------------------------------------------------------------ generations


def test_each_regroup_publishes_a_complete_generation(catalog: Catalog) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    catalog.db.execute("INSERT OR REPLACE INTO catalog_meta VALUES ('regroup_due', '1')")
    catalog.regroup()
    values = catalog.meta()
    assert (values["generation"], values["complete"], values["regroup_due"]) == ("1", "1", "0")
    assert values["reader_version"] == str(READER_VERSION)
    assert values["worker_version"] == __version__
    assert values["link_key_id"] == "0123456789abcdef"
    assert values["selection_config_sha256"] == SelectionConfig(min_slices=3).sha256()
    assert catalog.table("pending_identifiers") == [("st1", "ONE", "ACC1", "dicom", 1)]
    catalog.regroup()
    assert catalog.meta()["generation"] == "2"
    assert catalog.table("pending_identifiers") == [("st1", "ONE", "ACC1", "dicom", 2)]


def _derived(catalog: Catalog) -> dict[str, Any]:
    tables = (
        "cat_studies",
        "cat_series",
        "cat_checks",
        "cat_pairs",
        "cat_id_candidates",
        "cat_instances",
        "pending_identifiers",
        "catalog_meta",
    )
    return {name: catalog.table(name) for name in tables}


# check_cancelled runs before BEGIN (1), after each of the regroup's six
# steps (2 to 7) and before COMMIT (8).
@pytest.mark.parametrize("nth", [1, 2, 4, 6, 8])
def test_a_cancel_inside_the_regroup_leaves_the_previous_generation(
    catalog: Catalog, nth: int
) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    catalog.regroup()
    before = _derived(catalog)
    catalog.stack("s2", [0.0, 3.0, 6.0])
    calls = [0]
    guarded = []

    def cancel_at_nth() -> None:
        calls[0] += 1
        if calls[0] == nth:
            raise Cancelled

    @contextmanager
    def guard() -> Iterator[None]:
        guarded.append(catalog.db.in_transaction)
        yield

    with pytest.raises(Cancelled):
        catalog.regroup(check_cancelled=cancel_at_nth, rollback_guard=guard)
    assert not catalog.db.in_transaction
    assert _derived(catalog) == before
    # The rollback ran under the guard; a cancel before BEGIN has nothing
    # to roll back.
    assert guarded == ([] if nth == 1 else [True])


def test_an_error_inside_the_regroup_leaves_the_previous_generation(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    catalog.regroup()
    before = _derived(catalog)
    catalog.stack("s1", [9.0])

    def broken(*args: Any) -> None:
        raise RuntimeError

    monkeypatch.setattr(group, "_pairs", broken)
    with pytest.raises(RuntimeError):
        catalog.regroup()
    assert _derived(catalog) == before


def test_previews_of_fingerprints_that_are_gone_are_deleted(
    catalog: Catalog, tmp_path: Path
) -> None:
    catalog.stack("s1", [0.0, 3.0, 6.0])
    previews = tmp_path / "index" / "previews"
    previews.mkdir()
    catalog.regroup(previews_dir=previews)
    [part] = catalog.parts()
    for name in (f"{part['fingerprint']}.png", f"{'0' * 64}.png", f"{'1' * 64}.png.partial"):
        (previews / name).write_bytes(b"png")
    catalog.regroup(previews_dir=previews)
    assert sorted(p.name for p in previews.iterdir()) == [
        f"{'1' * 64}.png.partial",
        f"{part['fingerprint']}.png",
    ]


# ------------------------------------------------------------------ studies


def test_a_study_takes_its_most_frequent_link(catalog: Catalog) -> None:
    catalog.stack("s1", [0.0, 3.0], pid_link="pid:b", patient_id="B")
    catalog.stack("s1", [6.0, 9.0], pid_link="pid:a", patient_id="A", issuer_link="issuer:1")
    catalog.add("s1", z=12.0, pid_link="pid:a", patient_id="A", issuer_link="issuer:2")
    catalog.regroup()
    [study] = catalog.table("cat_studies")
    assert study[:3] == ("st1", "pid:a", "present")
    checks = dict(
        catalog.db.execute(
            "SELECT code, params_json FROM cat_checks WHERE object_kind = 'study'"
        ).fetchall()
    )
    # Two issuers among the files of the study's own link.
    assert checks == {
        "check.study_patient_conflict": '{"count":2}',
        "check.issuer_conflict": '{"count":2}',
    }
    assert catalog.table("pending_identifiers") == [("st1", "A", "ACC1", "dicom", 1)]


def test_folder_candidates_only_for_studies_without_a_usable_id(catalog: Catalog) -> None:
    anonymous = {"pid_link": None, "pid_state": "placeholder", "patient_id": None}
    for z in (0.0, 3.0):
        catalog.add(
            "s1", z=z, study_uid="anon", rel_path=f"CASE/2019/CT/{z}.dcm".encode(), **anonymous
        )
    catalog.add("s1", z=6.0, study_uid="anon", rel_path=b"CASE/2019/6.dcm", **anonymous)
    catalog.stack("s2", [0.0, 3.0])
    catalog.regroup(source_labels={1: "Cohort"})
    candidates = catalog.table("cat_id_candidates")
    assert [(row[0], row[2], row[3]) for row in candidates] == [
        ("anon", 0, "Cohort"),
        ("anon", 1, "CASE"),
        ("anon", 2, "2019"),
    ]
    assert candidates[2][4] == folder_link(KEY, 1, ["CASE", "2019"], 2)
    catalog.regroup(identity=IdentityConfig(folder_ids=False))
    assert catalog.table("cat_id_candidates") == []
    catalog.regroup(key=None)
    assert catalog.table("cat_id_candidates") == []
    assert catalog.table("pending_identifiers") == []


def test_pet_pairs_need_one_frame_of_reference_and_overlap(catalog: Catalog) -> None:
    pet = {"modality": "PT", "pet_json": '{"attenuation_corrected":1}'}
    catalog.stack("ct", [-100.0, -50.0, 0.0])
    catalog.stack("pet", [-80.0, -40.0], **pet)
    catalog.stack("far", [100.0, 140.0], **pet)
    catalog.stack("other", [-80.0, -40.0], for_uid="for2", **pet)
    catalog.regroup()
    refs = {p["series_uid"]: p["part_ref"] for p in catalog.parts()}
    assert catalog.table("cat_pairs") == [(refs["pet"], refs["ct"], 1, 40.0)]


# ------------------------------------------------------------------ through the job


def test_only_a_scan_that_changed_something_publishes_a_generation(tmp_path: Path) -> None:
    root = tmp_path / "source"
    dicom_factory.write_bulk(root, 6, per_series=6)
    project = tmp_path / "Study.bcoaproj"
    first = result_of(run_index(index_job(project, {1: root}))[1])
    assert (first["changed"], first["generation"], first["series"]) == (True, 1, 1)
    published = derived(project)
    again = result_of(run_index(index_job(project, {1: root}))[1])
    assert again["changed"] is False and "generation" not in again
    assert meta(project)["generation"] == "1"
    assert derived(project) == published
    series = root / "bulk" / "00000"
    shutil.copyfile(series / "IM0001.dcm", tmp_path / "IM0001.dcm")
    (series / "IM0001.dcm").unlink()
    third = result_of(run_index(index_job(project, {1: root}))[1])
    assert (third["changed"], third["generation"]) == (True, 2)
    # Five of six instances remain: the part keeps its part_ref.
    [before] = published["cat_series"]
    [after] = derived(project)["cat_series"]
    assert dict(after)["part_ref"] == dict(before)["part_ref"]
    assert dict(after)["image_count"] == 5
