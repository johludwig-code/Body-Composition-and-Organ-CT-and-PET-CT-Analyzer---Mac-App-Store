"""Per-label metrics on the CT's original grid (plan §10).

MOOSE's own statistics never reach the export: they are in mm³ while the
export is in mL (BOCARTA-MOOSE found a bladder of "241 622" that is 241.6 mL),
and they list only labels that are present. Everything here is computed from
the labelmap and the CT, in float64, without interpolation.

Arrays are in SimpleITK's array order (z, y, x); spacing is in SimpleITK's
image order (x, y, z) in millimetres. Mixing the two silently swaps a 1 mm and
a 3 mm axis, which changes every volume — the tests hold that.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

METRIC_COLUMNS: tuple[str, ...] = (
    "voxel_count",
    "volume_ml",
    "hu_mean",
    "hu_sd",
    "hu_median",
    "hu_p05",
    "hu_p95",
    "hu_min",
    "hu_max",
    "touches_border",
)


@dataclass(frozen=True)
class LabelMetrics:
    label_id: int
    voxel_count: int
    volume_ml: float
    hu_mean: float | None
    hu_sd: float | None
    hu_median: float | None
    hu_p05: float | None
    hu_p95: float | None
    hu_min: float | None
    hu_max: float | None
    touches_border: bool
    flags: tuple[str, ...] = field(default=())

    def as_row(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in ("label_id", *METRIC_COLUMNS)} | {
            "flags": ",".join(self.flags)
        }


def voxel_volume_ml(spacing_xyz: Sequence[float]) -> float:
    if len(spacing_xyz) != 3 or any(s <= 0 for s in spacing_xyz):
        raise ValueError(f"spacing must be three positive millimetre values, got {spacing_xyz}")
    return float(np.prod(np.asarray(spacing_xyz, dtype=np.float64))) / 1000.0


def border_labels(labels: npt.NDArray[np.integer]) -> set[int]:
    """Labels touching the first or last slice or the in-plane image edge.

    A structure cut by the field of view has a truncated volume; this is the
    `truncated` auto-QC flag.
    """
    if labels.ndim != 3:
        raise ValueError("labelmap must be three-dimensional (z, y, x)")
    faces = (
        labels[0],
        labels[-1],
        labels[:, 0, :],
        labels[:, -1, :],
        labels[:, :, 0],
        labels[:, :, -1],
    )
    found: set[int] = set()
    for face in faces:
        found.update(int(v) for v in np.unique(face))
    found.discard(0)
    return found


def compute_label_metrics(
    ct_hu: npt.NDArray[np.number],
    labels: npt.NDArray[np.integer],
    spacing_xyz: Sequence[float],
    label_ids: Iterable[int],
) -> list[LabelMetrics]:
    """Metrics for every requested label, in the order given.

    `ct_hu` must already be in Hounsfield units (rescale slope and intercept
    applied — dcm2niix does this when it writes the NIfTI). A requested label
    that is absent gets voxel_count 0, empty metrics and the `empty_label`
    flag, never zeros: a zero volume averaged into a cohort is a wrong number,
    an empty cell is a missing one.
    """
    if ct_hu.shape != labels.shape:
        raise ValueError(f"CT {ct_hu.shape} and labelmap {labels.shape} are not on one grid")
    unit_ml = voxel_volume_ml(spacing_xyz)
    touching = border_labels(labels)

    flat_labels = labels.ravel()
    flat_ct = ct_hu.ravel().astype(np.float64, copy=False)
    # One sort instead of one boolean mask per label: 144 labels on a
    # 512×512×600 CT would otherwise scan 157 M voxels 144 times.
    order = np.argsort(flat_labels, kind="stable")
    sorted_labels = flat_labels[order]
    present, starts, counts = np.unique(sorted_labels, return_index=True, return_counts=True)
    spans = {int(v): (int(s), int(c)) for v, s, c in zip(present, starts, counts, strict=True)}

    results: list[LabelMetrics] = []
    for label_id in label_ids:
        start, count = spans.get(int(label_id), (0, 0))
        if count == 0:
            results.append(
                LabelMetrics(
                    int(label_id),
                    0,
                    0.0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    False,
                    ("empty_label",),
                )
            )
            continue
        values = flat_ct[order[start : start + count]]
        p05, median, p95 = np.percentile(values, [5.0, 50.0, 95.0])
        border = int(label_id) in touching
        results.append(
            LabelMetrics(
                label_id=int(label_id),
                voxel_count=count,
                volume_ml=count * unit_ml,
                hu_mean=float(values.mean()),
                # Sample SD (n − 1). With a single voxel it is undefined, and
                # undefined is an empty cell, not 0.
                hu_sd=float(values.std(ddof=1)) if count > 1 else None,
                hu_median=float(median),
                hu_p05=float(p05),
                hu_p95=float(p95),
                hu_min=float(values.min()),
                hu_max=float(values.max()),
                touches_border=border,
                flags=("truncated",) if border else (),
            )
        )
    return results
