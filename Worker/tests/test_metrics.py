from __future__ import annotations

import numpy as np
import pytest

from bcoa_worker.metrics import border_labels, compute_label_metrics, voxel_volume_ml


def _empty(shape: tuple[int, int, int] = (20, 30, 40)) -> tuple[np.ndarray, np.ndarray]:
    return np.full(shape, -1000.0), np.zeros(shape, dtype=np.uint8)


def test_ten_cubed_at_one_by_one_by_two_is_two_millilitres() -> None:
    ct, labels = _empty()
    labels[5:15, 5:15, 5:15] = 1
    (m,) = compute_label_metrics(ct, labels, (1.0, 1.0, 2.0), [1])
    assert m.voxel_count == 1000
    assert m.volume_ml == pytest.approx(2.0, abs=1e-12)


def test_spacing_is_x_y_z_while_arrays_are_z_y_x() -> None:
    # A label one voxel thick in z and ten wide in x. With 5 mm slices and
    # 0.5 mm pixels a swapped spacing would give a different volume.
    ct, labels = _empty()
    labels[3, 0:10, 0:10] = 2
    (m,) = compute_label_metrics(ct, labels, (0.5, 0.5, 5.0), [2])
    assert m.volume_ml == pytest.approx(100 * 0.5 * 0.5 * 5.0 / 1000)


def test_hounsfield_statistics_on_known_values() -> None:
    ct, labels = _empty()
    values = np.arange(1, 101, dtype=np.float64)  # 1 … 100
    ct[10, 10:20, 10:20] = values.reshape(10, 10)
    labels[10, 10:20, 10:20] = 3
    (m,) = compute_label_metrics(ct, labels, (1, 1, 1), [3])
    assert m.hu_mean == pytest.approx(50.5)
    assert m.hu_sd == pytest.approx(np.std(values, ddof=1))
    assert m.hu_median == pytest.approx(50.5)
    assert m.hu_p05 == pytest.approx(np.percentile(values, 5))
    assert m.hu_p95 == pytest.approx(np.percentile(values, 95))
    assert (m.hu_min, m.hu_max) == (1.0, 100.0)


def test_absent_label_is_empty_not_zero() -> None:
    ct, labels = _empty()
    (m,) = compute_label_metrics(ct, labels, (1, 1, 1), [7])
    assert m.voxel_count == 0
    assert m.volume_ml == 0.0
    assert m.hu_mean is None and m.hu_sd is None and m.hu_median is None
    assert m.flags == ("empty_label",)


def test_single_voxel_has_no_standard_deviation() -> None:
    ct, labels = _empty()
    labels[10, 10, 10] = 4
    ct[10, 10, 10] = 42
    (m,) = compute_label_metrics(ct, labels, (1, 1, 1), [4])
    assert m.hu_mean == 42 and m.hu_sd is None


def test_border_contact_in_every_direction() -> None:
    _, labels = _empty()
    labels[0, 10, 10] = 1  # first slice
    labels[-1, 10, 10] = 2  # last slice
    labels[10, 0, 10] = 3  # anterior/posterior edge
    labels[10, 10, -1] = 4  # left/right edge
    labels[10, 10, 10] = 5  # inside
    assert border_labels(labels) == {1, 2, 3, 4}
    ct = np.zeros(labels.shape)
    flags = {m.label_id: m.flags for m in compute_label_metrics(ct, labels, (1, 1, 1), range(1, 6))}
    assert flags[1] == ("truncated",) and flags[5] == ()


def test_results_follow_the_requested_order() -> None:
    ct, labels = _empty()
    labels[5, 5, 5] = 9
    labels[6, 6, 6] = 1
    ids = [m.label_id for m in compute_label_metrics(ct, labels, (1, 1, 1), [9, 1, 5])]
    assert ids == [9, 1, 5]


def test_grids_must_match() -> None:
    with pytest.raises(ValueError, match="one grid"):
        compute_label_metrics(
            np.zeros((2, 2, 2)), np.zeros((2, 2, 3), dtype=np.uint8), (1, 1, 1), [1]
        )


def test_spacing_must_be_positive() -> None:
    with pytest.raises(ValueError):
        voxel_volume_ml((1.0, 0.0, 1.0))
