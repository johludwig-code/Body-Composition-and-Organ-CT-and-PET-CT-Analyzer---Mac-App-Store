"""Slice geometry and its checks (ADR 0022 decision 11): every threshold just
below and just above, and the synthetic stacks of the M2 design's prototype."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence

import pytest

from bcoa_worker.index import geometry
from bcoa_worker.index.codes import Check
from bcoa_worker.index.geometry import Geometry, stack_checks, stack_geometry
from conftest import PROTOCOL_ROOT

AXIAL = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
REGISTRY = {
    entry["code"]: entry
    for entry in json.loads((PROTOCOL_ROOT / "index_codes.json").read_text())["groups"]["check"][
        "codes"
    ]
}


def _codes(checks: Sequence[Check]) -> set[str]:
    return {c.code for c in checks}


def _stack(zs: Sequence[float], shift_per_mm: tuple[float, float] = (0.0, 0.0)) -> Geometry:
    """Axial slices at the given table positions; an in-plane shift that grows
    with z is how a sheared (tilted) stack looks."""
    positions = [(-190.0 + shift_per_mm[0] * z, -190.0 + shift_per_mm[1] * z, z) for z in zs]
    result = stack_geometry(AXIAL, positions)
    assert result is not None
    return result


def _rotated(degrees: float) -> tuple[float, ...]:
    """An orientation rotated about the patient's x axis."""
    t = math.radians(degrees)
    return (1.0, 0.0, 0.0, 0.0, math.cos(t), math.sin(t))


def _along_normal(iop: Sequence[float], count: int, step: float) -> Geometry:
    """Slices stacked on their own normal: oblique, but not sheared."""
    normal = geometry.slice_normal(iop)
    assert normal is not None
    result = stack_geometry(iop, [tuple(i * step * c for c in normal) for i in range(count)])
    assert result is not None
    return result


# ----------------------------------------------------------- orientation


def _unit_row_column(row_length: float = 1.0, column_length: float = 1.0, dot: float = 0.0):
    column = (dot, math.sqrt(1.0 - dot * dot), 0.0)
    return (row_length, 0.0, 0.0, *(column_length * c for c in column))


@pytest.mark.parametrize(
    ("iop", "valid"),
    [
        (_unit_row_column(row_length=1.0099), True),
        (_unit_row_column(row_length=1.0101), False),
        (_unit_row_column(row_length=0.9901), True),
        (_unit_row_column(row_length=0.9899), False),
        (_unit_row_column(column_length=1.0099), True),
        (_unit_row_column(column_length=1.0101), False),
        (_unit_row_column(dot=0.0099), True),
        (_unit_row_column(dot=0.0101), False),
    ],
)
def test_an_orientation_is_valid_within_one_hundredth(iop: tuple[float, ...], valid: bool) -> None:
    assert (geometry.slice_normal(iop) is not None) is valid


@pytest.mark.parametrize(
    "iop",
    [
        None,
        (),
        (1, 0, 0, 0, 1),
        (1, 0, 0, 0, 1, 0, 0),
        ("1", "0", "x", 0, 1, 0),
        (1, 0, 0, 0, math.nan, 0),
    ],
    ids=["missing", "empty", "five", "seven", "text", "nan"],
)
def test_a_malformed_orientation_has_no_normal(iop: Sequence[object] | None) -> None:
    assert geometry.slice_normal(iop) is None  # type: ignore[arg-type]


def test_the_normal_is_the_unit_cross_product() -> None:
    assert geometry.slice_normal(AXIAL) == (0.0, 0.0, 1.0)
    # Rows and columns written slightly long: the normal is still a unit vector.
    normal = geometry.slice_normal((1.005, 0, 0, 0, 1.005, 0))
    assert normal is not None and math.isclose(math.hypot(*normal), 1.0)
    # Numbers as text, as pydicom's DS values can arrive.
    assert geometry.slice_normal(("1", "0", "0", "0", "1", "0")) == (0.0, 0.0, 1.0)


def test_standard_planes() -> None:
    coronal = geometry.slice_normal((1, 0, 0, 0, 0, -1))
    sagittal = geometry.slice_normal((0, 1, 0, 0, 0, -1))
    assert coronal is not None and geometry.orientation_class(coronal) == "coronal"
    assert sagittal is not None and geometry.orientation_class(sagittal) == "sagittal"
    assert geometry.orientation_class((0.0, 0.0, -1.0)) == "axial"


def _normal_with(axis: int, component: float) -> geometry.Vector:
    values = [0.0, 0.0, 0.0]
    values[axis] = component
    values[(axis + 1) % 3] = math.sqrt(1.0 - component * component)
    return (values[0], values[1], values[2])


@pytest.mark.parametrize(("axis", "plane"), [(2, "axial"), (1, "coronal"), (0, "sagittal")])
def test_a_standard_plane_needs_a_component_of_at_least_095(axis: int, plane: str) -> None:
    assert geometry.orientation_class(_normal_with(axis, 0.951)) == plane
    assert geometry.orientation_class(_normal_with(axis, -0.951)) == plane
    assert geometry.orientation_class(_normal_with(axis, 0.949)) == "oblique"


def test_the_split_angle_is_one_degree() -> None:
    axial = (0.0, 0.0, 1.0)
    assert not geometry.normals_differ(axial, _along_normal(_rotated(0.99), 2, 1).normal)
    assert geometry.normals_differ(axial, _along_normal(_rotated(1.01), 2, 1).normal)
    # Rows that run the other way give the opposite normal: another
    # orientation, which grouping must split off.
    flipped = geometry.slice_normal((-1, 0, 0, 0, 1, 0))
    assert flipped is not None
    assert math.isclose(geometry.normal_angle_deg(axial, flipped), 180.0)


# ----------------------------------------------------------- positions


def test_neighbors_closer_than_a_thousandth_share_a_position() -> None:
    assert _stack([0.0, 0.0009, 1.0, 2.0]).slice_count == 3
    assert _stack([0.0, 0.0011, 1.0, 2.0]).slice_count == 4
    # Neighbor to neighbor: a run of close positions stays one position.
    run = _stack([0.0, 0.0006, 0.0012, 1.0])
    assert run.slice_count == 2
    assert run.shared_position_count == 3


def test_count_extent_spacing_and_middle_of_a_shuffled_stack() -> None:
    zs = [i * 1.25 for i in range(81)]
    # 37 is prime to 81, so this visits every position once, out of order.
    shuffled = [zs[(i * 37) % 81] for i in range(81)]
    result = _stack(shuffled)
    assert result.slice_count == 81
    assert result.z_extent_mm == 100.0
    assert result.slice_spacing_mm == 1.25
    assert [shuffled[i] for i in result.order] == zs
    assert result.positions_mm == tuple(zs)
    # Ordinal ⌊n/2⌋ of the sorted instances.
    assert shuffled[result.middle] == zs[40]


def test_positions_are_measured_along_the_normal_whichever_way_it_points() -> None:
    # Feet first: the normal points down, positions are sorted along it.
    result = stack_geometry((1, 0, 0, 0, -1, 0), [(0, 0, -i * 2.0) for i in range(5)])
    assert result is not None
    assert result.normal == (0.0, 0.0, -1.0)
    assert result.positions_mm == (0.0, 2.0, 4.0, 6.0, 8.0)
    assert result.order == (0, 1, 2, 3, 4)
    assert result.slice_spacing_mm == 2.0


def test_equal_positions_keep_the_order_they_were_given_in() -> None:
    result = _stack([1.0, 0.0, 1.0, 0.0])
    assert result.order == (1, 3, 0, 2)


def test_a_single_slice_has_no_spacing_and_no_shear() -> None:
    result = _stack([12.5])
    assert (result.slice_count, result.z_extent_mm) == (1, 0.0)
    assert result.slice_spacing_mm is None
    assert result.shear_deg is None
    assert stack_checks(result, gantry_tilt_deg=15.0) == []


@pytest.mark.parametrize(
    ("orientation", "positions"),
    [
        (None, [(0, 0, 0), (0, 0, 1)]),
        ((1, 0, 0, 0, 1.2, 0), [(0, 0, 0), (0, 0, 1)]),
        (AXIAL, []),
        (AXIAL, [(0, 0, 0), None]),
        (AXIAL, [(0, 0, 0), (0, 0)]),
        (AXIAL, [(0, 0, 0), (0, 0, math.inf)]),
    ],
    ids=[
        "no orientation",
        "invalid orientation",
        "no instance",
        "a missing position",
        "a short position",
        "an infinite position",
    ],
)
def test_no_geometry(orientation: Sequence[float] | None, positions: list) -> None:
    assert stack_geometry(orientation, positions) is None
    assert stack_checks(None) == [Check("check.no_geometry", "warning", {})]


# ----------------------------------------------------------- spacing checks


def _with_step(step: float, spacing: float = 1.0, count: int = 20, at: int = 10) -> list[float]:
    """Evenly spaced positions with one step changed."""
    zs, z = [], 0.0
    for i in range(count):
        zs.append(z)
        z += step if i == at else spacing
    return zs


def test_a_gap_is_a_step_above_one_and_a_half_times_the_median() -> None:
    assert "check.gap" not in _codes(stack_checks(_stack(_with_step(1.49))))
    gap = [c for c in stack_checks(_stack(_with_step(1.51))) if c.code == "check.gap"]
    assert gap == [Check("check.gap", "warning", {"gaps": 1, "missing": 1, "largest_mm": 1.51})]


def test_missing_slices_are_counted_per_gap() -> None:
    one = [i * 1.0 for i in range(100) if i != 40]
    three = [i * 1.0 for i in range(100) if not 40 <= i <= 42]
    two_gaps = [i * 1.0 for i in range(100) if i not in (20, 60, 61)]
    assert stack_checks(_stack(one))[0].params == {"gaps": 1, "missing": 1, "largest_mm": 2.0}
    assert stack_checks(_stack(three))[0].params == {"gaps": 1, "missing": 3, "largest_mm": 4.0}
    assert stack_checks(_stack(two_gaps))[0].params == {"gaps": 2, "missing": 3, "largest_mm": 3.0}


@pytest.mark.parametrize(
    ("spacing", "below", "above"),
    [
        (1.0, 1.0099, 1.0101),  # 1 % of 1 mm, the same as the 0.01 mm floor
        (5.0, 5.049, 5.051),  # 1 % of 5 mm
        (0.5, 0.5099, 0.5101),  # 0.01 mm, not 1 % of 0.5 mm
    ],
)
def test_uneven_spacing_is_one_percent_of_the_median_or_a_hundredth_mm(
    spacing: float, below: float, above: float
) -> None:
    calm = stack_checks(_stack(_with_step(below, spacing)))
    assert "check.uneven_spacing" not in _codes(calm)
    uneven = [
        c
        for c in stack_checks(_stack(_with_step(above, spacing)))
        if c.code == "check.uneven_spacing"
    ]
    assert len(uneven) == 1
    assert uneven[0].params["deviation_mm"] == pytest.approx(above - spacing, abs=0.001)


def test_a_gap_is_not_also_uneven_spacing() -> None:
    assert _codes(stack_checks(_stack(_with_step(2.0)))) == {"check.gap"}


def test_shared_positions_count_every_slice_that_shares_one() -> None:
    assert stack_checks(_stack([0, 1, 2, 2, 3, 4])) == [
        Check("check.duplicate_positions", "warning", {"count": 2})
    ]
    assert stack_checks(_stack([0, 0.0009, 1, 2, 3])) == [
        Check("check.duplicate_positions", "warning", {"count": 2})
    ]
    assert "check.duplicate_positions" not in _codes(stack_checks(_stack([0, 0.0011, 1, 2, 3])))


def test_every_slice_counted_twice_is_reported_and_the_extent_stays_true() -> None:
    # BOCARTA-MOOSE's ZIP unpacked beside its archive: every slice twice.
    result = _stack([i * 1.3 for i in range(675)] * 2)
    assert result.slice_count == 675
    assert result.slice_spacing_mm == pytest.approx(1.3)
    assert result.z_extent_mm == pytest.approx(674 * 1.3)
    assert stack_checks(result) == [Check("check.duplicate_positions", "warning", {"count": 1350})]


# ----------------------------------------------------------- tilt and obliquity


def _sheared(degrees: float, count: int = 60) -> Geometry:
    return _stack([i * 1.0 for i in range(count)], (math.tan(math.radians(degrees)), 0.0))


def test_gantry_tilt_is_a_shear_above_a_tenth_of_a_degree() -> None:
    assert stack_checks(_sheared(0.09)) == []
    assert stack_checks(_sheared(0.11)) == [
        Check("check.gantry_tilt", "warning", {"degrees": 0.11})
    ]


def test_a_tilt_tag_without_shear_says_the_series_was_resampled() -> None:
    upright = _stack([i * 1.0 for i in range(30)])
    assert stack_checks(upright, gantry_tilt_deg=0.09) == []
    assert stack_checks(upright, gantry_tilt_deg=0.11) == [
        Check("check.tilt_tag_only", "info", {"degrees": 0.11})
    ]
    assert stack_checks(upright, gantry_tilt_deg=-7.0) == [
        Check("check.tilt_tag_only", "info", {"degrees": -7.0})
    ]
    # Sheared as the tag says: a real tilt, reported once.
    assert _codes(stack_checks(_sheared(7.0), gantry_tilt_deg=7.0)) == {"check.gantry_tilt"}


def test_oblique_is_more_than_one_degree_from_the_nearest_plane() -> None:
    assert stack_checks(_along_normal(_rotated(0.99), 60, 1.0)) == []
    assert stack_checks(_along_normal(_rotated(1.01), 60, 1.0)) == [
        Check("check.oblique", "info", {"degrees": 1.01})
    ]


def test_a_tilted_gantry_shears_the_stack_and_tilts_the_slices() -> None:
    # The classic tilted CT: the slice plane leans by the tilt, and the table
    # still moves along z.
    tilted = stack_geometry(_rotated(15.0), [(-190.0, -190.0, -i * 1.0) for i in range(60)])
    assert tilted is not None
    assert tilted.orientation == "axial"
    assert stack_checks(tilted, gantry_tilt_deg=15.0) == [
        Check("check.gantry_tilt", "warning", {"degrees": 15.0}),
        Check("check.oblique", "info", {"degrees": 15.0}),
    ]


def test_an_axial_stack_tilts_up_to_182_degrees() -> None:
    assert _along_normal(_rotated(18.1), 3, 1.0).orientation == "axial"
    assert _along_normal(_rotated(18.3), 3, 1.0).orientation == "oblique"


# ----------------------------------------------------------- pixels


@pytest.mark.parametrize(
    ("row", "column", "reported"),
    [
        (1.0, 1.0099, False),
        (1.0, 1.0101, True),
        (1.0101, 1.0, True),
        (0.7, 0.7, False),
        (None, 0.7, False),
        (0.0, 0.7, False),
    ],
)
def test_non_square_pixels_differ_by_more_than_one_percent(
    row: float | None, column: float | None, reported: bool
) -> None:
    checks = geometry.pixel_checks(row, column)
    assert [c.code for c in checks] == (["check.non_square_pixels"] if reported else [])
    if reported:
        assert checks[0].params == {
            "row_mm": pytest.approx(row, abs=0.001),
            "col_mm": pytest.approx(column, abs=0.001),
        }


# ----------------------------------------------------------- the prototype's stacks


def _shift(degrees: float) -> tuple[float, float]:
    return (0.0, math.tan(math.radians(degrees)))


PROTOTYPE: dict[str, tuple[Geometry, float | None, set[str]]] = {
    "regular 1 mm, shuffled": (
        _stack([((i * 37) % 100) * 1.0 for i in range(100)]),
        None,
        set(),
    ),
    "regular, descending z": (_stack([-i * 1.25 for i in range(80)]), None, set()),
    "one missing slice": (_stack([i * 1.0 for i in range(100) if i != 40]), None, {"check.gap"}),
    "three missing, one gap": (
        _stack([i * 1.0 for i in range(100) if not 40 <= i <= 42]),
        None,
        {"check.gap"},
    ),
    "uneven 1.0 and 1.5": (
        _stack([0, 1, 2, 3, 4.5, 6, 7, 8, 9, 10]),
        None,
        {"check.uneven_spacing"},
    ),
    "duplicate position": (_stack([0, 1, 2, 2, 3, 4]), None, {"check.duplicate_positions"}),
    "gantry tilt 15 degrees in the orientation": (
        stack_geometry(_rotated(15.0), [(-190.0, -190.0, -i * 1.0) for i in range(60)]),
        15.0,
        {"check.gantry_tilt", "check.oblique"},
    ),
    "tilt only as a shift, axial orientation": (
        _stack([i * 1.0 for i in range(60)], _shift(12.0)),
        None,
        {"check.gantry_tilt"},
    ),
    "oblique, stacked on its normal": (
        _along_normal(_rotated(20.0), 60, 1.0),
        None,
        {"check.oblique"},
    ),
    "tilt tag but no shear": (_stack([i * 1.0 for i in range(30)]), 7.0, {"check.tilt_tag_only"}),
    "float noise of 0.0001 mm": (
        _stack([i * 1.0 + 1e-4 * math.sin(i * 12.9898) for i in range(100)]),
        None,
        set(),
    ),
}  # type: ignore[dict-item]


@pytest.mark.parametrize(("name"), PROTOTYPE)
def test_the_prototype_stacks(name: str) -> None:
    result, tilt, expected = PROTOTYPE[name]
    assert _codes(stack_checks(result, gantry_tilt_deg=tilt)) == expected


def test_the_prototypes_mixed_stacks_are_split_before_geometry() -> None:
    # The prototype's other two cases, a localizer mixed into an axial series
    # and a slice of another size, are grouping's business (ADR 0022 decision
    # 9): a part never mixes orientations, so geometry only has to tell them
    # apart.
    localizer = geometry.slice_normal((1, 0, 0, 0, 0, -1))
    assert localizer is not None
    assert geometry.normals_differ((0.0, 0.0, 1.0), localizer)


# ----------------------------------------------------------- registry


def test_every_geometry_check_is_registered_as_written() -> None:
    emitted = [
        *stack_checks(None),
        *stack_checks(_stack(_with_step(3.0))),
        *stack_checks(_stack(_with_step(1.2))),
        *stack_checks(_stack([0, 1, 1, 2])),
        *stack_checks(_sheared(5.0)),
        *stack_checks(_stack([0, 1, 2]), gantry_tilt_deg=5.0),
        *stack_checks(_along_normal(_rotated(5.0), 3, 1.0)),
        *geometry.pixel_checks(0.5, 0.7),
    ]
    assert _codes(emitted) == {
        "check.no_geometry",
        "check.gap",
        "check.uneven_spacing",
        "check.duplicate_positions",
        "check.gantry_tilt",
        "check.tilt_tag_only",
        "check.oblique",
        "check.non_square_pixels",
    }
    for row in emitted:
        entry = REGISTRY[row.code]
        assert (row.level, entry["object"]) == (entry["level"], "series"), row.code
        assert set(row.params) == set(entry["params"]), row.code
