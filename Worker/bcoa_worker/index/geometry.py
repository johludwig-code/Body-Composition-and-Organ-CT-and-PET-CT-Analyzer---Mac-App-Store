"""Slice geometry of a series part, and the checks that come from it
(ADR 0022 decision 11).

Everything here works on ImageOrientationPatient and ImagePositionPatient
(Enhanced objects: the same per frame) and nothing else. BOCARTA-MOOSE took
orientation, size and spacing from the first file of a series; when a ZIP
was unpacked beside its archive every slice was there twice, the steps
collapsed to 0, and an 88 cm patient was reconstructed 2.9 m long. Positions
along the slice normal, merged when they coincide, are what the coverage,
the spacing and every check below are measured on.

Plain floats and `math`, no NumPy: a part has at most a few thousand
positions, and the index job does not need to load NumPy for them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from statistics import median
from typing import Literal

from bcoa_worker.index.codes import Check, check, rounded

Vector = tuple[float, float, float]
Orientation = Literal["axial", "coronal", "sagittal", "oblique"]

# A direction cosine vector whose length is off by more than 1 %, or a row and
# column further from perpendicular than that, is not an orientation that was
# rounded: it is damaged or made up, and a normal computed from it would put
# every position in the wrong place.
ORIENTATION_TOLERANCE = 0.01
# Neighbors closer than this along the normal are one position. A copy of a
# slice sits 0 mm from it (the twice-counted slices of BOCARTA-MOOSE made steps
# of exactly 0), and no reconstruction spaces slices a thousandth of a
# millimeter apart.
POSITION_MERGE_MM = 0.001
# A normal component at or above this puts the slices in that axis's standard
# plane, at most arccos(0.95) = 18.2° from it. Tilted further, a part is
# oblique, and ADR 0023 does not select it automatically.
AXIS_MIN_ABS = 0.95
# Grouping splits a series where slice normals differ by more than this
# (ADR 0022 decision 9, step b).
SPLIT_ANGLE_DEG = 1.0
# A step above 1.5 times the median step is a gap: at least one slice is
# missing, since one missing slice makes a step of 2. Below it the step is a
# spacing that varies, which is the uneven-spacing check's business.
GAP_FACTOR = 1.5
# Steps that differ from the median by more than 1 % of it are uneven. Below
# 1 mm spacing the bound stays at 0.01 mm: positions written to two decimals
# make steps that differ by up to 0.01 mm in a perfectly even series.
UNEVEN_RELATIVE = 0.01
UNEVEN_ABSOLUTE_MM = 0.01
# Shear between the slice normal and the line through the first and last
# position. A tenth of a degree over a 400 mm stack moves the last slice
# 0.7 mm in plane, less than a pixel of a body CT.
TILT_DEG = 0.1
# Slices more than 1° from the nearest standard plane are reported as
# oblique; they stay eligible while they count as axial (up to 18.2°).
OBLIQUE_DEG = 1.0
# Row and column spacing differing by more than 1 % of the smaller one.
NON_SQUARE_RELATIVE = 0.01


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Sequence[float], b: Sequence[float]) -> Vector:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(a: Sequence[float]) -> float:
    return math.sqrt(_dot(a, a))


def _finite(values: Sequence[object], count: int) -> list[float] | None:
    """`count` finite floats, or None for anything else (a missing tag, a
    wrong multiplicity, text that is not a number, NaN)."""
    if values is None or len(values) != count:
        return None
    try:
        numbers = [float(v) for v in values]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in numbers):
        return None
    return numbers


def slice_normal(orientation: Sequence[float] | None) -> Vector | None:
    """The unit slice normal n = (r × c) / ‖r × c‖ of an ImageOrientationPatient,
    or None when the orientation is missing or invalid.

    Valid means both direction cosines have a length within 0.01 of 1 and
    |r · c| ≤ 0.01. The normal is not flipped to point anywhere in particular:
    a series whose rows run the other way is another orientation, and
    grouping must see it as one (`normal_angle_deg` is then 180°).
    """
    values = _finite(orientation, 6) if orientation is not None else None
    if values is None:
        return None
    row, column = values[:3], values[3:]
    if (
        abs(_norm(row) - 1.0) > ORIENTATION_TOLERANCE
        or abs(_norm(column) - 1.0) > ORIENTATION_TOLERANCE
        or abs(_dot(row, column)) > ORIENTATION_TOLERANCE
    ):
        return None
    normal = _cross(row, column)
    # Within the tolerances above ‖r × c‖ is at least 0.97, never 0.
    length = _norm(normal)
    return (normal[0] / length, normal[1] / length, normal[2] / length)


def orientation_class(normal: Vector) -> Orientation:
    """Axial when |n_z| ≥ 0.95, coronal when |n_y| ≥ 0.95, sagittal when
    |n_x| ≥ 0.95, oblique otherwise. A unit vector has at most one component
    that large, so the order of the tests does not matter."""
    if abs(normal[2]) >= AXIS_MIN_ABS:
        return "axial"
    if abs(normal[1]) >= AXIS_MIN_ABS:
        return "coronal"
    if abs(normal[0]) >= AXIS_MIN_ABS:
        return "sagittal"
    return "oblique"


def obliquity_deg(normal: Vector) -> float:
    """φ = arccos(max(|n_x|, |n_y|, |n_z|)): the angle between the slices and
    the nearest standard plane."""
    largest = max(abs(normal[0]), abs(normal[1]), abs(normal[2]))
    return math.degrees(math.acos(min(1.0, largest)))


def normal_angle_deg(a: Vector, b: Vector) -> float:
    """The angle between two unit normals, 0° to 180°.

    atan2 of the sine and cosine rather than arccos of the dot product:
    arccos loses half its digits near 0°, which is exactly where the 1°
    split threshold is decided.
    """
    return math.degrees(math.atan2(_norm(_cross(a, b)), _dot(a, b)))


def normals_differ(a: Vector, b: Vector) -> bool:
    """Whether grouping splits two instances by orientation."""
    return normal_angle_deg(a, b) > SPLIT_ANGLE_DEG


@dataclass(frozen=True, slots=True)
class Geometry:
    """A part's stack along its slice normal.

    `order` lists the instances (indices into the positions given) sorted
    along the normal; instances at the same position keep the order they were
    given in, so the caller decides ties (by InstanceNumber, say).
    """

    normal: Vector
    order: tuple[int, ...]
    # Position along the normal (d = p · n) of each instance, in `order`.
    positions_mm: tuple[float, ...]
    # The distinct positions (the lowest of each run of neighbors closer than
    # POSITION_MERGE_MM) and the steps between them.
    distinct_mm: tuple[float, ...]
    steps_mm: tuple[float, ...]
    # Instances that share their position with at least one other instance.
    shared_position_count: int
    orientation: Orientation
    obliquity_deg: float
    # θ between the normal and the line through the first and last position;
    # None for a single position, where there is no line.
    shear_deg: float | None

    @property
    def slice_count(self) -> int:
        return len(self.distinct_mm)

    @property
    def z_extent_mm(self) -> float:
        return self.distinct_mm[-1] - self.distinct_mm[0]

    @property
    def slice_spacing_mm(self) -> float | None:
        """The median step s, or None for a single position."""
        return median(self.steps_mm) if self.steps_mm else None

    @property
    def middle(self) -> int:
        """The instance for the preview: ordinal ⌊n/2⌋ of the sorted instances."""
        return self.order[len(self.order) // 2]


def stack_geometry(
    orientation: Sequence[float] | None, positions: Sequence[Sequence[float] | None]
) -> Geometry | None:
    """The geometry of a part from its orientation and one position per
    instance, or None when the part has no geometry.

    A part has no geometry when the orientation is missing or invalid, when it
    has no instance, or when any instance lacks a valid position: a coverage
    measured on the instances that happen to have one would be a number about
    other images than the ones the converter will read.
    """
    normal = slice_normal(orientation)
    if normal is None or not positions:
        return None
    points: list[list[float]] = []
    for position in positions:
        point = _finite(position, 3) if position is not None else None
        if point is None:
            return None
        points.append(point)
    along = [_dot(point, normal) for point in points]
    order = sorted(range(len(points)), key=along.__getitem__)
    sorted_mm = [along[i] for i in order]
    distinct = [sorted_mm[0]]
    sizes = [1]
    for previous, current in pairwise(sorted_mm):
        # Neighbor to neighbor, as ADR 0022 says, so that a run of positions
        # each 0.0005 mm from the next stays one position.
        if current - previous < POSITION_MERGE_MM:
            sizes[-1] += 1
        else:
            distinct.append(current)
            sizes.append(1)
    steps = tuple(b - a for a, b in pairwise(distinct))
    shear = _shear_deg(points[order[0]], points[order[-1]], normal) if steps else None
    return Geometry(
        normal=normal,
        order=tuple(order),
        positions_mm=tuple(sorted_mm),
        distinct_mm=tuple(distinct),
        steps_mm=steps,
        shared_position_count=sum(size for size in sizes if size > 1),
        orientation=orientation_class(normal),
        obliquity_deg=obliquity_deg(normal),
        shear_deg=shear,
    )


def _shear_deg(first: Sequence[float], last: Sequence[float], normal: Vector) -> float:
    """θ = arccos(|u · n|) with u the unit vector from the first to the last
    position, computed as atan2 for the same reason as `normal_angle_deg`:
    the 0.1° tilt threshold sits where arccos is least precise."""
    u = (last[0] - first[0], last[1] - first[1], last[2] - first[2])
    return math.degrees(math.atan2(_norm(_cross(u, normal)), abs(_dot(u, normal))))


def stack_checks(geometry: Geometry | None, *, gantry_tilt_deg: float | None = None) -> list[Check]:
    """The geometry checks of an image part: no_geometry, gap, uneven_spacing,
    duplicate_positions, gantry_tilt, tilt_tag_only and oblique.

    `gantry_tilt_deg` is the part's GantryDetectorTilt, if any file has one.
    Callers do not pass non-image parts (a dose report has no geometry, and
    saying so would be a warning about nothing).
    """
    if geometry is None:
        return [check("check.no_geometry")]
    checks: list[Check] = []
    spacing = geometry.slice_spacing_mm
    if spacing is not None:
        gaps = [step for step in geometry.steps_mm if step > GAP_FACTOR * spacing]
        if gaps:
            # Half up, not Python's half to even: a step of 2.5 s means two
            # slices are missing as much as it means one.
            missing = sum(math.floor(step / spacing + 0.5) - 1 for step in gaps)
            checks.append(
                check("check.gap", gaps=len(gaps), missing=missing, largest_mm=rounded(max(gaps)))
            )
        # The median is one of the steps or lies between two of them, so at
        # least one step is at most s and this list is never empty.
        regular = [step for step in geometry.steps_mm if step <= GAP_FACTOR * spacing]
        deviation = max(abs(step - spacing) for step in regular)
        if deviation > max(UNEVEN_RELATIVE * spacing, UNEVEN_ABSOLUTE_MM):
            checks.append(check("check.uneven_spacing", deviation_mm=rounded(deviation)))
    if geometry.shared_position_count:
        checks.append(check("check.duplicate_positions", count=geometry.shared_position_count))
    shear = geometry.shear_deg
    if shear is not None and shear > TILT_DEG:
        checks.append(check("check.gantry_tilt", degrees=rounded(shear)))
    elif (
        shear is not None
        and gantry_tilt_deg is not None
        and math.isfinite(gantry_tilt_deg)
        and abs(gantry_tilt_deg) > TILT_DEG
    ):
        # The scanner says it tilted, and the positions show no shear: the
        # series was resampled onto an upright grid already.
        checks.append(check("check.tilt_tag_only", degrees=rounded(gantry_tilt_deg)))
    if geometry.obliquity_deg > OBLIQUE_DEG:
        checks.append(check("check.oblique", degrees=rounded(geometry.obliquity_deg)))
    return checks


def pixel_checks(row_mm: float | None, col_mm: float | None) -> list[Check]:
    """non_square_pixels: |row − col| > 0.01 · min(row, col).

    `row_mm` and `col_mm` are the two values of PixelSpacing in the order the
    standard gives them (between rows, then between columns). Missing or
    non-positive spacings say nothing about the shape of a pixel.
    """
    if row_mm is None or col_mm is None:
        return []
    if not (math.isfinite(row_mm) and math.isfinite(col_mm)) or row_mm <= 0 or col_mm <= 0:
        return []
    if abs(row_mm - col_mm) > NON_SQUARE_RELATIVE * min(row_mm, col_mm):
        return [check("check.non_square_pixels", row_mm=rounded(row_mm), col_mm=rounded(col_mm))]
    return []
