"""The regroup: one catalog generation from the rows of `files` (ADR 0020
decision 2, ADR 0022 decisions 7 to 13, ADR 0023, ADR 0024 decision 3).

Everything happens in one `BEGIN IMMEDIATE` transaction, in this order:

1. Instances. Every image, non-image and NIfTI file is one instance, and
   every frame of a multi-frame file is one; the instance key is the SOP
   Instance UID, with `#<frame>` for a frame.
2. Duplicates. Of the files sharing an instance key one wins: pixel data
   `ok`, then a transfer syntax the converter reads, then the lowest source
   and relative path. A file that wins nothing is a loser, counted on the
   part of the file it lost to.
3. Parts. The winners of each series (series UID within study UID) are
   split by the cascade of ADR 0022 decision 9, a multi-frame file always
   whole, and numbered by their lowest position, then InstanceNumber.
4. Geometry per part, from `geometry.stack_geometry`.
5. Part identity. A part keeps the `part_ref` of a part of the previous
   generation that it shares more than half of that part's instances with;
   otherwise it gets a new one, and a `part_ref` is never used twice.
6. Aggregates and checks per part, then selection and checks per study, the
   patient link, sex, age, date and description of each study, its pending
   identifiers and its folder candidates.
7. PET/CT pairs and the checks of each source.
8. The `cat_*` tables, `cat_instances` and `pending_identifiers` are
   replaced and `catalog_meta` moves to the next complete generation.

After COMMIT the previews of fingerprints that no longer exist are deleted.
A cancel or an error anywhere before COMMIT rolls all of it back, and the
previous generation stays complete and mergeable (ADR 0020 decision 9).

Removed sources and stale files are not deleted here: the scan deletes
their rows in its own transactions before the regroup (`run._drop_removed`
and `Scanner._forget`), so that a cancelled scan does not leave them for a
regroup that may never run, and those transactions set `regroup_due`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, fields
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

from bcoa_worker import __version__
from bcoa_worker.index import checks, geometry, image_type, kernels
from bcoa_worker.index.catalog import execute_waiting
from bcoa_worker.index.codes import Check, check, rounded
from bcoa_worker.index.geometry import Geometry, Vector
from bcoa_worker.index.identity import IdentityConfig, folder_candidates
from bcoa_worker.index.read import FRAME_CEILING, READER_VERSION, decode_numbers
from bcoa_worker.index.select import (
    PartFacts,
    Selection,
    SelectionConfig,
    is_convertible,
    select_study,
    thickness_mm,
)

mode = checks.mode


@dataclass(frozen=True, slots=True)
class RegroupContext:
    """What a regroup needs besides the catalog."""

    selection: SelectionConfig
    identity: IdentityConfig
    # None after Remove Identifiers: no link, candidate or pending identifier
    # is written then (ADR 0024 decision 9.5).
    key: bytes | None
    # The key's id as the job carries it; '' without a key.
    link_key_id: str
    # Raises worker.Cancelled when the job was asked to stop. Called between
    # steps, so that a cancel the signal handler raised inside a library
    # that swallowed it still ends the regroup.
    check_cancelled: Callable[[], None]
    # The label of level 0 of the folder candidates, by source: the name of
    # the source folder, for display only (ADR 0024 decision 3).
    source_labels: Mapping[int, str] = field(default_factory=dict)
    # Where the previews live; those of fingerprints that no longer exist
    # are deleted after COMMIT (ADR 0025 decision 7). None deletes none.
    previews_dir: Path | None = None
    # Held around the rollback, so that a second SIGTERM cannot break it off
    # (`run._sigterm_deferred`).
    rollback_guard: Callable[[], AbstractContextManager[Any]] = nullcontext


@dataclass(frozen=True, slots=True)
class GroupOutcome:
    """The counts the result reports of a regroup."""

    studies: int = 0
    series: int = 0
    parts_split: int = 0
    duplicates: int = 0


# ------------------------------------------------------------------ the rows


@dataclass(slots=True)
class _File:
    """The columns of a winning file that the regroup reads. The names are
    those of `files`, and the SELECT is built from them."""

    file_id: int
    source_id: int
    rel_path: bytes
    kind: str
    sop_class_uid: str | None
    sop_uid: str
    study_uid: str
    series_uid: str
    for_uid: str | None
    transfer_syntax_uid: str | None
    frames: int | None
    pid_link: str | None
    issuer_link: str | None
    pid_state: str | None
    patient_id: str | None
    accession_number: str | None
    sex: str | None
    age_years: float | None
    age_conflict: int | None
    study_date: str | None
    study_description: str | None
    modality: str | None
    series_number: int | None
    series_description: str | None
    image_type: str | None
    kernel: str | None
    manufacturer: str | None
    scanner_model: str | None
    kvp: float | None
    contrast_agent: str | None
    slice_thickness: float | None
    pixel_spacing_row: float | None
    pixel_spacing_col: float | None
    image_rows: int | None
    image_columns: int | None
    iop: str | None
    ipp_x: float | None
    ipp_y: float | None
    ipp_z: float | None
    instance_number: int | None
    acquisition_number: int | None
    temporal_position: int | None
    echo_number: int | None
    gantry_tilt: float | None
    rescale_slope: float | None
    rescale_intercept: float | None
    burned_in: str | None
    pixel_data: str | None
    pet_json: str | None
    nifti_json: str | None


_FILE_COLUMNS = tuple(f.name for f in fields(_File))
# Text that repeats from file to file of a series. Each value is kept once,
# which is most of what 100 000 rows in memory would otherwise cost: the
# rows of one series share every one of these.
_SHARED_COLUMNS = frozenset(
    {
        "kind",
        "sop_class_uid",
        "study_uid",
        "series_uid",
        "for_uid",
        "transfer_syntax_uid",
        "pid_link",
        "issuer_link",
        "pid_state",
        "patient_id",
        "accession_number",
        "sex",
        "study_date",
        "study_description",
        "modality",
        "series_description",
        "image_type",
        "kernel",
        "manufacturer",
        "scanner_model",
        "contrast_agent",
        "iop",
        "burned_in",
        "pixel_data",
        "pet_json",
    }
)
_GROUPED_KINDS = "('image', 'non_image', 'nifti')"


@dataclass(slots=True)
class _Instance:
    file: _File
    # 0 for a single-frame file, 1… for the frames of a multi-frame one.
    frame: int
    sop_key: str
    position: tuple[float, float, float] | None
    iop: str | None
    stack_id: str | None


@dataclass(slots=True)
class _Unit:
    """One file with the instances it won: what the cascade moves, because
    a multi-frame file is never split (ADR 0022 decision 9)."""

    file: _File
    instances: list[_Instance]
    image: bool
    normal: Vector | None
    # Frames whose normals differ by more than the split angle, or of which
    # only some have a valid orientation: check.enhanced_mixed_frames.
    mixed: bool
    stacks: tuple[str, ...]


@dataclass(slots=True)
class _Part:
    study_uid: str
    series_uid: str
    units: list[_Unit]
    # The cascade step that last divided this part's lineage; None when the
    # series was not split.
    reason: str | None
    instances: list[_Instance] = field(default_factory=list)
    geometry: Geometry | None = None
    nifti: dict[str, Any] | None = None
    part: int = 0
    part_ref: int = 0
    split: bool = False
    fingerprint: str = ""
    losers: int = 0
    conflicts: int = 0
    # Files of this part's own series that lost their instance to a file of
    # another study or series (ADR 0029).
    lost: int = 0
    facts: PartFacts | None = None
    row: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    selection: Selection | None = None

    @property
    def files(self) -> list[_File]:
        return [unit.file for unit in self.units]

    @property
    def image(self) -> bool:
        return self.units[0].image

    @property
    def mixed(self) -> bool:
        return any(unit.mixed for unit in self.units)


# ------------------------------------------------------------------ the regroup


def regroup(db: sqlite3.Connection, context: RegroupContext) -> GroupOutcome:
    """Write the next generation in one transaction; a cancel or an error
    rolls all of it back, and the previous generation stays as it was."""
    context.check_cancelled()
    # Both wait for another program's lock in short steps, so that a cancel
    # is answered while they wait (`catalog.execute_waiting`).
    execute_waiting(db, "BEGIN IMMEDIATE", context.check_cancelled)
    try:
        built = _Build(db, context)
        outcome = built.run()
        context.check_cancelled()
        execute_waiting(db, "COMMIT", context.check_cancelled)
    except BaseException:
        with context.rollback_guard():
            if db.in_transaction:
                db.execute("ROLLBACK")
        raise
    if context.previews_dir is not None:
        _forget_previews(context.previews_dir, built.fingerprints)
    return outcome


class _Build:
    """One regroup's state between its steps."""

    def __init__(self, db: sqlite3.Connection, context: RegroupContext) -> None:
        self.db = db
        self.context = context
        self.config = context.selection
        self.fingerprints: set[str] = set()
        self._normals: dict[str | None, Vector | None] = {}

    def run(self) -> GroupOutcome:
        check_cancelled = self.context.check_cancelled
        generation = int(_meta(self.db, "generation") or "0") + 1
        next_part_ref = int(_meta(self.db, "next_part_ref") or "1")
        won, lost_to, conflicts = self._duplicates()
        check_cancelled()
        files = self._winning_files(won)
        instances = self._instances(files, won)
        check_cancelled()
        parts = self._parts(files, instances)
        check_cancelled()
        for part in parts:
            self._geometry(part)
        _number(parts)
        next_part_ref = self._identify(parts, next_part_ref)
        check_cancelled()
        self._count_losers(parts, lost_to, conflicts)
        missing = self._dicomdir_missing()
        by_series: Counter[str] = Counter()
        for (_, series_uid), count in missing.items():
            by_series[series_uid] += count
        for part in parts:
            self._describe(part, by_series.get(part.series_uid, 0))
        check_cancelled()
        studies = self._studies(parts)
        pairs = _pairs(parts)
        sources = self._source_checks(missing, {part.series_uid for part in parts})
        check_cancelled()
        self._write(parts, studies, pairs, sources, generation, next_part_ref)
        self.fingerprints = {part.fingerprint for part in parts}
        return GroupOutcome(
            studies=len(studies),
            series=len(parts),
            parts_split=sum(part.split for part in parts),
            duplicates=len(lost_to),
        )

    # -------------------------------------------------------------- duplicates

    def _duplicates(
        self,
    ) -> tuple[dict[int, set[str]], dict[int, int], dict[int, tuple[str, str]]]:
        """The instance keys each winning file won; for every file that won
        none, the file it lost its first instance to; and those losers that
        came from another study or series than their winner, with their own
        (study UID, series UID).

        Only the columns the order needs are read here, so that the full
        rows are held for winners only: 100 000 copies of one series would
        otherwise all be held for nothing.
        """
        frame_rows = dict(self.db.execute("SELECT file_id, count(*) FROM frames GROUP BY file_id"))
        best: dict[str, tuple[tuple[bool, bool, int, bytes], int]] = {}
        first_key: dict[int, str] = {}
        grouping: dict[int, tuple[str, str]] = {}
        shared: dict[str, str] = {}
        for (
            file_id,
            source_id,
            rel_path,
            kind,
            sop_uid,
            study_uid,
            series_uid,
            frames,
            pixel,
            syntax,
        ) in self.db.execute(
            "SELECT file_id, source_id, rel_path, kind, sop_uid, study_uid, series_uid, frames, "  # noqa: S608 - the kinds are a constant
            f"pixel_data, transfer_syntax_uid FROM files WHERE kind IN {_GROUPED_KINDS} "
            "AND sop_uid IS NOT NULL AND study_uid IS NOT NULL AND series_uid IS NOT NULL"
        ):
            count = 1 if kind == "nifti" else _frame_count(frame_rows.get(file_id), frames)
            rank = (
                pixel != "ok",
                not (syntax is not None and is_convertible(syntax)),
                source_id,
                bytes(rel_path),
            )
            keys = _sop_keys(sop_uid, count)
            first_key[file_id] = keys[0]
            grouping[file_id] = (
                shared.setdefault(study_uid, study_uid),
                shared.setdefault(series_uid, series_uid),
            )
            for key in keys:
                current = best.get(key)
                if current is None or rank < current[0]:
                    best[key] = (rank, file_id)
        won: dict[int, set[str]] = defaultdict(set)
        for key, (_, file_id) in best.items():
            won[file_id].add(key)
        lost_to: dict[int, int] = {}
        conflicts: dict[int, tuple[str, str]] = {}
        for file_id, key in first_key.items():
            if file_id in won:
                continue
            winner = best[key][1]
            lost_to[file_id] = winner
            if grouping[file_id] != grouping[winner]:
                conflicts[file_id] = grouping[file_id]
        return dict(won), lost_to, conflicts

    def _winning_files(self, won: Mapping[int, set[str]]) -> dict[int, _File]:
        shared: dict[Any, Any] = {}
        indices = [i for i, name in enumerate(_FILE_COLUMNS) if name in _SHARED_COLUMNS]
        files: dict[int, _File] = {}
        for row in self.db.execute(
            f"SELECT {', '.join(_FILE_COLUMNS)} FROM files "  # noqa: S608
            f"WHERE kind IN {_GROUPED_KINDS} ORDER BY file_id"
        ):
            if row[0] not in won:
                continue
            values = list(row)
            for i in indices:
                value = values[i]
                if value is not None:
                    values[i] = shared.setdefault(value, value)
            values[2] = bytes(values[2])
            files[row[0]] = _File(*values)
        return files

    def _instances(
        self, files: Mapping[int, _File], won: Mapping[int, set[str]]
    ) -> dict[int, list[_Instance]]:
        frames: dict[int, dict[int, tuple[Any, ...]]] = defaultdict(dict)
        for file_id, frame, x, y, z, iop, stack_id in self.db.execute(
            "SELECT file_id, frame, ipp_x, ipp_y, ipp_z, iop, stack_id FROM frames"
        ):
            if file_id in files:
                frames[file_id][frame] = (_position(x, y, z), iop, stack_id)
        instances: dict[int, list[_Instance]] = {}
        for file_id, file in files.items():
            keys = won[file_id]
            own = frames.get(file_id, {})
            count = 1 if file.kind == "nifti" else _frame_count(len(own) or None, file.frames)
            position = _position(file.ipp_x, file.ipp_y, file.ipp_z)
            if count == 1:
                stack = own[1][2] if 1 in own else None
                instances[file_id] = [_Instance(file, 0, file.sop_uid, position, file.iop, stack)]
                continue
            listed = []
            for frame in range(1, count + 1):
                key = f"{file.sop_uid}#{frame}"
                if key not in keys:
                    continue
                if frame in own:
                    frame_position, frame_iop, stack = own[frame]
                    listed.append(
                        _Instance(file, frame, key, frame_position, frame_iop or file.iop, stack)
                    )
                else:
                    # A multi-frame object without per-frame geometry (a
                    # classic multi-frame class): its frames have no
                    # position of their own.
                    listed.append(_Instance(file, frame, key, None, file.iop, None))
            instances[file_id] = listed
        return instances

    # -------------------------------------------------------------- the cascade

    def _normal(self, iop: str | None) -> Vector | None:
        if iop not in self._normals:
            self._normals[iop] = geometry.slice_normal(decode_numbers(iop))
        return self._normals[iop]

    def _unit(self, file: _File, instances: list[_Instance]) -> _Unit:
        normals = [self._normal(instance.iop) for instance in instances]
        valid = [normal for normal in normals if normal is not None]
        mixed = len(instances) > 1 and (
            (0 < len(valid) < len(normals))
            or any(geometry.normals_differ(valid[0], other) for other in valid[1:])
        )
        stacks = tuple(sorted({i.stack_id for i in instances if i.stack_id is not None}))
        return _Unit(
            file=file,
            instances=instances,
            image=file.kind != "non_image",
            normal=None if mixed or not valid else valid[0],
            mixed=mixed,
            stacks=stacks,
        )

    def _parts(
        self, files: Mapping[int, _File], instances: Mapping[int, list[_Instance]]
    ) -> list[_Part]:
        series: dict[tuple[str, str], list[_Unit]] = defaultdict(list)
        for file_id, file in files.items():
            if instances[file_id]:
                series[(file.study_uid, file.series_uid)].append(
                    self._unit(file, instances[file_id])
                )
        parts: list[_Part] = []
        for (study_uid, series_uid), units in sorted(series.items()):
            units.sort(key=_unit_order)
            for members, reason in _cascade(units):
                parts.append(_Part(study_uid, series_uid, members, reason))
        counts = Counter((part.study_uid, part.series_uid) for part in parts)
        for part in parts:
            part.split = counts[(part.study_uid, part.series_uid)] > 1
            part.instances = [i for unit in part.units for i in unit.instances]
        return parts

    # -------------------------------------------------------------- geometry

    def _geometry(self, part: _Part) -> None:
        first = part.units[0].file
        if first.kind == "nifti":
            part.nifti = _nifti_meta(first.nifti_json)
            part.geometry = _nifti_geometry(first, part.nifti)
            part.fingerprint = part.nifti.get("content_sha256") or _fingerprint(part.instances)
            return
        part.fingerprint = _fingerprint(part.instances)
        if not part.image or part.mixed:
            # A file whose frames point several ways has no one orientation
            # to measure positions along.
            return
        orientation = mode(instance.iop for instance in part.instances)
        part.geometry = geometry.stack_geometry(
            decode_numbers(orientation), [instance.position for instance in part.instances]
        )

    # -------------------------------------------------------------- identity

    def _identify(self, parts: Sequence[_Part], next_part_ref: int) -> int:
        """Carry part_refs over from the previous generation by instance
        overlap (ADR 0022 decision 10) and number the rest from
        `next_part_ref`; return the new `next_part_ref`."""
        old_of: dict[str, int] = {}
        old_size: Counter[int] = Counter()
        for part_ref, sop_key in self.db.execute("SELECT part_ref, sop_key FROM cat_instances"):
            old_of[sop_key] = part_ref
            old_size[part_ref] += 1
        candidates: list[tuple[int, int, int]] = []
        for index, part in enumerate(parts):
            overlaps = Counter(old_of[i.sop_key] for i in part.instances if i.sop_key in old_of)
            for part_ref, overlap in overlaps.items():
                if 2 * overlap > old_size[part_ref]:
                    candidates.append((overlap, part_ref, index))
        taken: set[int] = set()
        for _, part_ref, index in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
            if part_ref in taken or parts[index].part_ref:
                continue
            parts[index].part_ref = part_ref
            taken.add(part_ref)
        for part in sorted(parts, key=lambda p: (p.study_uid, p.series_uid, p.part)):
            if not part.part_ref:
                part.part_ref = next_part_ref
                next_part_ref += 1
        return next_part_ref

    @staticmethod
    def _count_losers(
        parts: Sequence[_Part],
        lost_to: Mapping[int, int],
        conflicts: Mapping[int, tuple[str, str]],
    ) -> None:
        """Count each loser on the part of the file it lost to, and a UID
        conflict's loser also on every part of its own series. Which file wins
        depends on nothing but the path order, so the series that lost a
        slice can as well be the genuine one; without a check of its own it
        lost the slice in silence (measured: 49 of 50 slices, and excluded as
        too short)."""
        part_of = {unit.file.file_id: part for part in parts for unit in part.units}
        by_series: dict[tuple[str, str], list[_Part]] = defaultdict(list)
        for part in parts:
            by_series[(part.study_uid, part.series_uid)].append(part)
        for loser, winner in lost_to.items():
            part = part_of.get(winner)
            if part is not None:
                part.losers += 1
                if loser in conflicts:
                    part.conflicts += 1
            for own in by_series.get(conflicts[loser], []) if loser in conflicts else []:
                own.lost += 1

    def _dicomdir_missing(self) -> dict[tuple[int, str], int]:
        """Per source and series UID, the SOP UIDs a DICOMDIR of that source
        lists that no image or non-image file of the same source has (ADR
        0022 decision 7).

        The files are matched in Python, against the SOP UIDs the DICOMDIRs
        list. Matched in SQL, every entry scanned all files of its source
        through the only index there is: 45 s for a DICOMDIR that listed
        20 000 files, against 0.7 s without it, in one statement that no
        cancel could interrupt.
        """
        entries = self.db.execute(
            "SELECT d.source_id, e.sop_uid, e.series_uid FROM dicomdir_entries e "
            "JOIN files d ON d.file_id = e.file_id"
        ).fetchall()
        if not entries:
            return {}
        listed = {sop_uid for _, sop_uid, _ in entries}
        found = {
            (source_id, sop_uid)
            for source_id, sop_uid in self.db.execute(
                "SELECT source_id, sop_uid FROM files "
                "WHERE kind IN ('image', 'non_image') AND sop_uid IS NOT NULL"
            )
            if sop_uid in listed
        }
        missing: dict[tuple[int, str], set[str]] = defaultdict(set)
        for source_id, sop_uid, series_uid in entries:
            if (source_id, sop_uid) not in found:
                missing[(source_id, series_uid)].add(sop_uid)
        return {key: len(sops) for key, sops in missing.items()}

    # -------------------------------------------------------------- per part

    def _describe(self, part: _Part, dicomdir_missing: int) -> None:
        """The part's `cat_series` values, its checks and what the selection
        needs to know of it."""
        files = part.files
        nifti = part.nifti is not None
        geom = part.geometry
        modality = mode(f.modality for f in files) or "OT"
        kernel = mode(f.kernel for f in files)
        manufacturer = mode(f.manufacturer for f in files)
        raw_image_type = mode(f.image_type for f in files)
        thicknesses = [
            t for f in files if (t := f.slice_thickness) is not None and math.isfinite(t) and t > 0
        ]
        row_spacing = mode(f.pixel_spacing_row for f in files)
        col_spacing = mode(f.pixel_spacing_col for f in files)
        syntaxes = [f.transfer_syntax_uid for f in files]
        states = Counter(f.pixel_data for f in files)
        found: list[Check] = []
        if part.image:
            found += geometry.stack_checks(geom, gantry_tilt_deg=mode(f.gantry_tilt for f in files))
            found += checks.too_few_slices(
                geom.slice_count if geom else None, self.config.min_slices
            )
            found += geometry.pixel_checks(row_spacing, col_spacing)
            if part.mixed:
                found.append(check("check.enhanced_mixed_frames"))
            if nifti:
                if modality == "OT":
                    found.append(check("check.nifti_unnamed"))
            else:
                found += checks.burned_in(f.burned_in for f in files)
                found += checks.transfer_syntax(syntaxes)
                found += checks.pixel_data(f.pixel_data for f in files)
                found += checks.rescale(
                    modality, ((f.rescale_slope, f.rescale_intercept) for f in files)
                )
                if modality.strip().upper() == "PT":
                    found += checks.pet(f.pet_json for f in files)
        if part.split:
            found.append(check("check.split", reason=part.reason))
        found += checks.duplicates(part.losers, part.conflicts, part.lost)
        if dicomdir_missing:
            found.append(check("check.dicomdir_incomplete", missing=dicomdir_missing))
        part.checks = found

        dims = (part.nifti or {}).get("dims") or []
        part.facts = PartFacts(
            series_uid=part.series_uid,
            part=part.part,
            modality=modality,
            is_image=part.image,
            sop_class_uid=mode(f.sop_class_uid for f in files),
            image_type=image_type.image_type_values(raw_image_type),
            description=mode(f.series_description for f in files),
            normal=geom.normal if geom else None,
            z_extent_mm=geom.z_extent_mm if geom else None,
            slice_count=geom.slice_count if geom else None,
            mixed_frames=part.mixed,
            truncated_files=states["truncated"],
            missing_pixel_files=states["missing"],
            transfer_syntaxes=frozenset(s for s in syntaxes if s is not None),
            nifti=nifti,
            nifti_3d=not nifti or _single_volume(part.nifti or {}),
            nifti_named=not nifti or modality != "OT",
            slice_thickness_mm=median(thicknesses) if thicknesses else None,
            slice_spacing_mm=geom.slice_spacing_mm if geom else None,
            kernel=kernel,
            kernel_class=kernels.classify(kernel, manufacturer, self.config.kernels),
            warnings=sum(c.level == "warning" for c in found),
            series_number=mode(f.series_number for f in files),
        )
        facts = part.facts
        middle = _middle(part)
        part.row = {
            "part_ref": part.part_ref,
            "series_uid": part.series_uid,
            "part": part.part,
            "study_uid": part.study_uid,
            "modality": modality,
            "description": facts.description,
            "image_count": _image_count(dims) if nifti else len(part.instances),
            "slice_thickness_mm": thickness_mm(facts),
            "pixel_spacing_mm": row_spacing,
            "kernel": kernel,
            "kernel_class": facts.kernel_class,
            "manufacturer": manufacturer,
            "kvp": mode(f.kvp for f in files),
            "contrast_agent": mode(f.contrast_agent for f in files),
            "image_type": raw_image_type,
            "frame_of_reference_uid": mode(f.for_uid for f in files),
            "fingerprint": part.fingerprint,
            "series_number": facts.series_number,
            "sop_class_uid": facts.sop_class_uid,
            "scanner_model": mode(f.scanner_model for f in files),
            "slice_count": facts.slice_count,
            "slice_spacing_mm": facts.slice_spacing_mm,
            "z_extent_mm": facts.z_extent_mm,
            "orientation": geom.orientation if geom else None,
            "image_rows": mode(f.image_rows for f in files),
            "image_columns": mode(f.image_columns for f in files),
            "transfer_syntax_uid": mode(syntaxes),
            "middle_file_id": middle.file.file_id,
            "middle_frame": middle.frame,
        }

    # -------------------------------------------------------------- per study

    def _studies(self, parts: Sequence[_Part]) -> list[dict[str, Any]]:
        by_study: dict[str, list[_Part]] = defaultdict(list)
        for part in parts:
            by_study[part.study_uid].append(part)
        studies = []
        for study_uid, members in sorted(by_study.items()):
            members.sort(key=lambda p: (p.series_uid, p.part))
            selections = select_study([_facts(p) for p in members], self.config)
            for part, selection in zip(members, selections, strict=True):
                part.selection = selection
            files = [f for p in members for f in p.files]
            studies.append(self._study(study_uid, files, members))
        return studies

    def _study(self, study_uid: str, files: list[_File], parts: list[_Part]) -> dict[str, Any]:
        link = mode(f.pid_link for f in files)
        linked = [f for f in files if f.pid_link == link] if link is not None else files
        state = mode(f.pid_state for f in linked) or mode(f.pid_state for f in files) or "missing"
        eligible = any(p.selection and p.selection.auto_rank is not None for p in parts)
        study: dict[str, Any] = {
            "row": (
                study_uid,
                link,
                state,
                mode(f.sex for f in files if f.sex in ("F", "M", "O")),
                mode(f.age_years for f in files),
                mode(f.study_date for f in files),
                mode(f.study_description for f in files),
            ),
            "checks": checks.study(
                eligible=eligible,
                links=(f.pid_link for f in files),
                issuers=(f.issuer_link for f in linked) if link is not None else (),
                age_conflict=any(f.age_conflict for f in files),
            ),
            "pending": None,
            "candidates": [],
        }
        key = self.context.key
        if key is None:
            return study
        patient_id = mode(f.patient_id for f in linked) if link is not None else None
        accession = mode(f.accession_number for f in files)
        if patient_id is not None or accession is not None:
            source = "file" if state == "file" else "dicom"
            study["pending"] = (study_uid, patient_id, accession, source)
        identity = self.context.identity
        if identity.folder_ids and state in ("missing", "placeholder"):
            study["candidates"] = self._candidates(key, study_uid, files)
        return study

    def _candidates(self, key: bytes, study_uid: str, files: list[_File]) -> list[tuple[Any, ...]]:
        """Folder candidates in the source that holds most of the study's
        files (the lowest source on a tie), from the deepest folder common
        to them there (ADR 0024 decision 3)."""
        per_source = Counter(f.source_id for f in files)
        source_id = min(per_source, key=lambda s: (-per_source[s], s))
        folders = [
            [os.fsdecode(part) for part in f.rel_path.split(b"/")[:-1]]
            for f in files
            if f.source_id == source_id
        ]
        common = os.path.commonprefix(folders) if folders else []
        label = self.context.source_labels.get(source_id, "")
        return [
            (study_uid, source_id, candidate.level, candidate.label, candidate.link)
            for candidate in folder_candidates(
                key, source_id, label, common, self.context.identity.folder_max_level
            )
        ]

    # -------------------------------------------------------------- per source

    def _source_checks(
        self, dicomdir_missing: Mapping[tuple[int, str], int], series_with_parts: set[str]
    ) -> dict[int, list[Check]]:
        """The checks of each source, among them the series a DICOMDIR of the
        source lists and no part holds at all: a copy that lost a whole
        series has no part to carry check.dicomdir_incomplete, and the loss
        went unreported (ADR 0029)."""
        lost_series: dict[int, list[int]] = defaultdict(list)
        for (source_id, series_uid), count in dicomdir_missing.items():
            if series_uid not in series_with_parts:
                lost_series[source_id].append(count)
        kinds: dict[int, dict[str, int]] = defaultdict(dict)
        unreadable: dict[int, dict[str, int]] = defaultdict(dict)
        bad_dirs: dict[int, int] = {}
        for source_id, kind, code, count in self.db.execute(
            "SELECT source_id, kind, CASE kind WHEN 'unreadable' THEN code END, count(*) "
            "FROM files GROUP BY 1, 2, 3"
        ):
            kinds[source_id][kind] = kinds[source_id].get(kind, 0) + count
            if kind == "unreadable" and code is not None:
                unreadable[source_id][code] = count
        for source_id, count in self.db.execute(
            "SELECT source_id, count(*) FROM bad_dirs GROUP BY source_id"
        ):
            bad_dirs[source_id] = count
        return {
            source_id: checks.source(
                kinds.get(source_id, {}),
                unreadable.get(source_id, {}),
                bad_dirs.get(source_id, 0),
                lost_series.get(source_id, []),
            )
            for source_id in sorted(set(kinds) | set(bad_dirs))
        }

    # -------------------------------------------------------------- writing

    def _write(
        self,
        parts: Sequence[_Part],
        studies: Sequence[dict[str, Any]],
        pairs: Sequence[tuple[int, int, int | None, float]],
        sources: Mapping[int, list[Check]],
        generation: int,
        next_part_ref: int,
    ) -> None:
        db = self.db
        for table in (
            "cat_studies",
            "cat_series",
            "cat_checks",
            "cat_pairs",
            "cat_id_candidates",
            "cat_instances",
            "pending_identifiers",
        ):
            db.execute(f"DELETE FROM {table}")  # noqa: S608 - the names are the ones above
        db.executemany(
            "INSERT INTO cat_studies (study_uid, pid_link, pid_state, sex, age_years, study_date, "
            "description) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [study["row"] for study in studies],
        )
        series_rows = []
        for part in parts:
            assert part.selection is not None
            series_rows.append(
                {
                    **part.row,
                    "auto_rank": part.selection.auto_rank,
                    "auto_selected": int(part.selection.auto_selected),
                    "reason_json": part.selection.reason_json,
                }
            )
        if series_rows:
            names = list(series_rows[0])
            db.executemany(
                f"INSERT INTO cat_series ({', '.join(names)}) "  # noqa: S608 - names are ours
                f"VALUES ({', '.join(':' + name for name in names)})",
                series_rows,
            )
        check_rows = [
            ("series", str(part.part_ref), c.code, c.level, checks.params_json(c))
            for part in parts
            for c in part.checks
        ]
        check_rows += [
            ("study", study["row"][0], c.code, c.level, checks.params_json(c))
            for study in studies
            for c in study["checks"]
        ]
        check_rows += [
            ("source", str(source_id), c.code, c.level, checks.params_json(c))
            for source_id, found in sources.items()
            for c in found
        ]
        db.executemany(
            "INSERT INTO cat_checks (object_kind, object_ref, code, level, params_json) "
            "VALUES (?, ?, ?, ?, ?)",
            check_rows,
        )
        db.executemany(
            "INSERT INTO cat_pairs (pet_part_ref, ct_part_ref, pet_attenuation_corrected, "
            "z_overlap_mm) VALUES (?, ?, ?, ?)",
            pairs,
        )
        db.executemany(
            "INSERT INTO cat_id_candidates (study_uid, source_id, level, label, link) "
            "VALUES (?, ?, ?, ?, ?)",
            [row for study in studies for row in study["candidates"]],
        )
        db.executemany(
            "INSERT INTO pending_identifiers (study_uid, patient_id, accession_number, id_source, "
            "generation) VALUES (?, ?, ?, ?, ?)",
            [(*study["pending"], generation) for study in studies if study["pending"]],
        )
        db.executemany(
            "INSERT INTO cat_instances (part_ref, ordinal, file_id, frame, sop_key, position_mm) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row for part in parts for row in _instance_rows(part)),
        )
        db.executemany(
            "INSERT OR REPLACE INTO catalog_meta (key, value) VALUES (?, ?)",
            [
                ("generation", str(generation)),
                ("complete", "1"),
                ("next_part_ref", str(next_part_ref)),
                ("reader_version", str(READER_VERSION)),
                ("worker_version", __version__),
                ("link_key_id", self.context.link_key_id),
                ("selection_config_sha256", self.config.sha256()),
                ("identity_config_sha256", self.context.identity.sha256()),
                ("regroup_due", "0"),
            ],
        )


# ------------------------------------------------------------------ helpers


def _meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM catalog_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _frame_count(frame_rows: int | None, frames: int | None) -> int:
    """The frames of a file: its frame rows when the reader made them (they
    are what its functional groups hold), else NumberOfFrames, never more
    than the reader believes of any file. Reader 2 refuses a count its pixel
    data cannot hold; a row of reader 1, which a regroup can meet before the
    scan that reads it again, may still claim 2^31 - 1."""
    if frame_rows:
        return frame_rows
    return min(frames, FRAME_CEILING) if frames is not None and frames > 1 else 1


def _sop_keys(sop_uid: str, count: int) -> list[str]:
    if count == 1:
        return [sop_uid]
    return [f"{sop_uid}#{frame}" for frame in range(1, count + 1)]


def _position(
    x: float | None, y: float | None, z: float | None
) -> tuple[float, float, float] | None:
    if x is None or y is None or z is None:
        return None
    return (x, y, z)


def _unit_order(unit: _Unit) -> tuple[bool, int, str]:
    # InstanceNumber first, missing last, then the instance key: the order
    # in which instances at one position keep their places in the stack.
    number = unit.file.instance_number
    return (number is None, number or 0, unit.instances[0].sop_key)


# ------------------------------------------------------------------ the cascade

_Group = list[_Unit]
# A step's groups, each with the split reason it gives its members; None is
# the step's own reason.
_Pieces = list[tuple[_Group, str | None]]
_Splitter = Callable[[_Group], _Pieces]


def _partition(members: _Group, key: Callable[[_Unit], Any]) -> _Pieces:
    groups: dict[Any, _Group] = {}
    for unit in members:
        groups.setdefault(key(unit), []).append(unit)
    return [(group, None) for group in groups.values()]


def _by_sop_class(members: _Group) -> _Pieces:
    return _partition(members, lambda unit: unit.image)


def _missing_apart(groups: list[_Group], clusters: int, without: _Group, missing: str) -> _Pieces:
    """Steps b and d set files without the value apart, with a reason that
    says so (ADR 0029): "changing orientation" was the text of a part that
    had lost one file without ImageOrientationPatient, where nothing had
    changed. The rest keep the step's reason only where the value really
    differs between them; otherwise it was the missing value that split
    them too."""
    rest = None if clusters > 1 or not without else missing
    return [(group, missing if group is without else rest) for group in groups]


def _by_orientation(members: _Group) -> _Pieces:
    """Units whose normals are within 1° of a group's first join it. Units
    without a valid orientation form one group, and a file with mixed frames
    one of its own: it cannot be measured with anything."""
    clusters: list[tuple[Vector | None, _Group]] = []
    without: _Group = []
    groups: list[_Group] = []
    for unit in members:
        if unit.mixed:
            groups.append([unit])
            continue
        if unit.normal is None:
            if not without:
                groups.append(without)
            without.append(unit)
            continue
        for normal, cluster in clusters:
            assert normal is not None
            if not geometry.normals_differ(normal, unit.normal):
                cluster.append(unit)
                break
        else:
            cluster = [unit]
            clusters.append((unit.normal, cluster))
            groups.append(cluster)
    return _missing_apart(groups, len(clusters), without, "missing_orientation")


def _by_matrix(members: _Group) -> _Pieces:
    return _partition(members, lambda unit: (unit.file.image_rows, unit.file.image_columns))


# Pixel spacings further apart than this split (ADR 0022 decision 9, step d).
SPACING_SPLIT_RELATIVE = 0.01


def _spacing_differs(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return any(abs(x - y) > SPACING_SPLIT_RELATIVE * min(x, y) for x, y in zip(a, b, strict=True))


def _by_pixel_spacing(members: _Group) -> _Pieces:
    clusters: list[tuple[tuple[float, float], _Group]] = []
    without: _Group = []
    groups: list[_Group] = []
    for unit in members:
        row, col = unit.file.pixel_spacing_row, unit.file.pixel_spacing_col
        if row is None or col is None or row <= 0 or col <= 0:
            if not without:
                groups.append(without)
            without.append(unit)
            continue
        for spacing, cluster in clusters:
            if not _spacing_differs(spacing, (row, col)):
                cluster.append(unit)
                break
        else:
            cluster = [unit]
            clusters.append(((row, col), cluster))
            groups.append(cluster)
    return _missing_apart(groups, len(clusters), without, "missing_pixel_spacing")


_REPEAT_TAGS: tuple[Callable[[_File], int | None], ...] = (
    lambda f: f.acquisition_number,
    lambda f: f.temporal_position,
    lambda f: f.echo_number,
)


def _crowd(positions: Iterable[float]) -> int:
    """The largest number of instances at one position: neighbors closer
    than POSITION_MERGE_MM along the normal are one position."""
    ordered = sorted(positions)
    largest = run = 1 if ordered else 0
    for previous, current in pairwise(ordered):
        run = run + 1 if current - previous < geometry.POSITION_MERGE_MM else 1
        largest = max(largest, run)
    return largest


def _by_repeat(members: _Group) -> _Pieces:
    """Step e: AcquisitionNumber, TemporalPositionIdentifier or EchoNumbers
    split a group whose positions repeat, when that tag separates the
    repeats: as many values as instances share the most crowded position,
    and no position repeated within a value.

    The count is not in ADR 0022's wording. Without it, a sequential scan
    that numbers every slice as an acquisition of its own, with one slice
    written twice, would split into one part per slice; with it, only a
    tag with one value per phase splits.
    """
    whole: _Pieces = [(members, None)]
    normal = members[0].normal
    if normal is None or any(unit.normal is None for unit in members):
        return whole
    along: dict[int, list[float]] = {}
    for index, unit in enumerate(members):
        values = []
        for instance in unit.instances:
            if instance.position is None:
                return whole
            p = instance.position
            values.append(p[0] * normal[0] + p[1] * normal[1] + p[2] * normal[2])
        along[index] = values
    crowd = _crowd(d for values in along.values() for d in values)
    if crowd < 2:
        return whole
    for tag in _REPEAT_TAGS:
        by_value: dict[int | None, list[int]] = {}
        for index, unit in enumerate(members):
            by_value.setdefault(tag(unit.file), []).append(index)
        if len(by_value) != crowd:
            continue
        if all(_crowd(d for i in indices for d in along[i]) == 1 for indices in by_value.values()):
            return [([members[i] for i in indices], None) for indices in by_value.values()]
    return whole


def _by_stack(members: _Group) -> _Pieces:
    return _partition(members, lambda unit: unit.stacks)


_STEPS: tuple[tuple[str, _Splitter], ...] = (
    ("sop_class", _by_sop_class),
    ("orientation", _by_orientation),
    ("size", _by_matrix),
    ("pixel_spacing", _by_pixel_spacing),
    ("acquisition", _by_repeat),
    ("stack", _by_stack),
)


def _cascade(units: _Group) -> list[tuple[_Group, str | None]]:
    """The parts of one series, each with the step that last divided it
    (ADR 0022 decision 9), each step within the groups of the one before."""
    groups: list[tuple[_Group, str | None]] = [(units, None)]
    for reason, split in _STEPS:
        following: list[tuple[_Group, str | None]] = []
        for members, previous in groups:
            pieces = split(members)
            if len(pieces) > 1:
                following.extend((piece, own or reason) for piece, own in pieces)
            else:
                following.append((members, previous))
        groups = following
    return groups


# ------------------------------------------------------------------ parts


def _lowest_position(part: _Part) -> float:
    if part.geometry is None:
        return math.inf
    # To the merge tolerance: two phases that start at one position must
    # tie here and be ordered by InstanceNumber.
    return round(part.geometry.distinct_mm[0], 3)


def _number(parts: Sequence[_Part]) -> None:
    """`part` 0…n−1 per series UID: by lowest position along the part's
    normal, then lowest InstanceNumber, then instance key. Numbered across
    studies, because `(series_uid, part)` is unique in the catalog and in
    the project even where one series UID turns up in two studies."""
    by_series: dict[str, list[_Part]] = defaultdict(list)
    for part in parts:
        by_series[part.series_uid].append(part)
    for members in by_series.values():
        members.sort(
            key=lambda p: (
                p.study_uid,
                _lowest_position(p),
                min(
                    (u.file.instance_number for u in p.units if u.file.instance_number is not None),
                    default=math.inf,
                ),
                min(i.sop_key for i in p.instances),
            )
        )
        for number, part in enumerate(members):
            part.part = number


def _facts(part: _Part) -> PartFacts:
    assert part.facts is not None, "every part is described before the selection"
    return part.facts


def _fingerprint(instances: Iterable[_Instance]) -> str:
    keys = sorted(instance.sop_key for instance in instances)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _middle(part: _Part) -> _Instance:
    if part.geometry is not None and part.nifti is None:
        return part.instances[part.geometry.middle]
    return part.instances[len(part.instances) // 2]


def _instance_rows(part: _Part) -> Iterable[tuple[Any, ...]]:
    geom = part.geometry
    if geom is not None and part.nifti is None:
        for ordinal, (index, position) in enumerate(
            zip(geom.order, geom.positions_mm, strict=True)
        ):
            instance = part.instances[index]
            yield (
                part.part_ref,
                ordinal,
                instance.file.file_id,
                instance.frame,
                instance.sop_key,
                position,
            )
        return
    for ordinal, instance in enumerate(part.instances):
        yield (
            part.part_ref,
            ordinal,
            instance.file.file_id,
            instance.frame,
            instance.sop_key,
            None,
        )


# ------------------------------------------------------------------ NIfTI


def _nifti_meta(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _numbers(value: Any, count: int) -> list[float] | None:
    if not isinstance(value, list) or len(value) < count:
        return None
    if not all(isinstance(v, int | float) and math.isfinite(v) for v in value):
        return None
    return [float(v) for v in value]


def _nifti_geometry(file: _File, meta: Mapping[str, Any]) -> Geometry | None:
    """A NIfTI volume's slices: the origin moved by the slice spacing along
    the third image axis, one position per slice, measured as a DICOM stack
    would be."""
    dims = meta.get("dims")
    if not isinstance(dims, list) or len(dims) < 3 or not isinstance(dims[2], int):
        return None
    dimension = len(dims)
    spacing = _numbers(meta.get("spacing"), 3)
    origin = _numbers(meta.get("origin"), 3)
    direction = _numbers(meta.get("direction"), dimension * dimension)
    if spacing is None or origin is None or direction is None or dims[2] < 1:
        return None
    axis = [direction[i * dimension + 2] for i in range(3)]
    positions = [
        tuple(origin[i] + k * spacing[2] * axis[i] for i in range(3)) for k in range(dims[2])
    ]
    return geometry.stack_geometry(decode_numbers(file.iop), positions)


def _single_volume(meta: Mapping[str, Any]) -> bool:
    """One 3D volume of scalars: three dimensions, any beyond them of size
    1, one component per voxel."""
    dims = meta.get("dims")
    if not isinstance(dims, list) or len(dims) < 3:
        return False
    return all(d == 1 for d in dims[3:]) and meta.get("components", 1) == 1


def _image_count(dims: Sequence[Any]) -> int:
    # The 2D images of the volume: slices times every further dimension.
    count = 1
    for d in dims[2:]:
        count *= d if isinstance(d, int) and d > 0 else 1
    return count


# ------------------------------------------------------------------ pairs


def _z_range(part: _Part) -> tuple[float, float] | None:
    zs = [i.position[2] for i in part.instances if i.position is not None]
    if not zs or len(zs) != len(part.instances):
        return None
    return min(zs), max(zs)


def _pairs(parts: Sequence[_Part]) -> list[tuple[int, int, int | None, float]]:
    """Every PT image part with every CT image part of the same study and
    non-empty Frame of Reference whose z ranges overlap (ADR 0022 decision
    12). NIfTI files have no Frame of Reference and pair with nothing."""
    by_study: dict[str, list[_Part]] = defaultdict(list)
    for part in parts:
        if part.image and part.nifti is None and part.row.get("frame_of_reference_uid"):
            by_study[part.study_uid].append(part)
    pairs = []
    for members in by_study.values():
        pets = [p for p in members if p.row["modality"].strip().upper() == "PT"]
        cts = [p for p in members if p.row["modality"].strip().upper() == "CT"]
        for pet in pets:
            pet_range = _z_range(pet)
            if pet_range is None:
                continue
            attenuation = checks.pet_attenuation(f.pet_json for f in pet.files)
            for ct in cts:
                if ct.row["frame_of_reference_uid"] != pet.row["frame_of_reference_uid"]:
                    continue
                ct_range = _z_range(ct)
                if ct_range is None:
                    continue
                overlap = min(pet_range[1], ct_range[1]) - max(pet_range[0], ct_range[0])
                if overlap > 0:
                    pairs.append((pet.part_ref, ct.part_ref, attenuation, rounded(overlap)))
    return sorted(pairs)


# ------------------------------------------------------------------ previews


def _forget_previews(folder: Path, fingerprints: set[str]) -> None:
    """Delete the previews of parts that no longer exist. A preview is a
    cache; one that cannot be deleted now goes at the next regroup."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if name.endswith(".png") and name[: -len(".png")] not in fingerprints:
            try:
                os.unlink(entry.path)
            except OSError:
                continue
