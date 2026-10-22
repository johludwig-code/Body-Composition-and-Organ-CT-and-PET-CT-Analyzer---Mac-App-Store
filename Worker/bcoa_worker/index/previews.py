"""Index mode `previews`: a small picture of the middle slice of each part
asked for (ADR 0025).

The catalog is read in one short transaction and closed before anything is
decoded, so a scan that commits meanwhile waits for one read at most (ADR
0021). Native, RLE and Deflated pixel data are decoded by pydicom, everything
else by SimpleITK; neither starts a program. A part whose picture could show
identifying text, or that is not an acquired image, gets a code instead.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.errors import JobFailure
from bcoa_worker.index.catalog import private_folder
from bcoa_worker.index.read import is_image_class
from bcoa_worker.index.select import SECONDARY_CAPTURE_SOP_CLASSES
from bcoa_worker.moose_adapter import sha256_file
from bcoa_worker.protocol import Artifact, Job, Progress, ProtocolError

MAX_PARTS = 64
# pydicom's own decoders; the rest go to SimpleITK (ADR 0025 decision 2).
_PYDICOM_SYNTAXES = frozenset(
    {
        "1.2.840.10008.1.2",  # Implicit VR Little Endian
        "1.2.840.10008.1.2.1",  # Explicit VR Little Endian
        "1.2.840.10008.1.2.2",  # Explicit VR Big Endian
        "1.2.840.10008.1.2.1.99",  # Deflated
        "1.2.840.10008.1.2.5",  # RLE Lossless
    }
)
CT_WINDOW = (400.0, 40.0)


@dataclass(frozen=True)
class _Part:
    part_ref: int
    fingerprint: str
    modality: str
    sop_class_uid: str | None
    image_type: str | None
    syntax: str | None
    source_id: int | None
    rel_path: bytes | None
    file_kind: str | None
    frame: int
    burned_in: str | None
    pixel_data: str | None
    photometric: str | None
    slope: float | None
    intercept: float | None
    row_mm: float | None
    col_mm: float | None


def parse_parts(raw: dict[str, Any]) -> tuple[str, list[int], int]:
    parts = raw.get("parts")
    if not isinstance(parts, list) or not parts or len(parts) > MAX_PARTS:
        raise ProtocolError(f"a previews job names 1 to {MAX_PARTS} parts")
    ids = {p.get("catalog_id") for p in parts if isinstance(p, dict)}
    if len(ids) != 1 or not isinstance(next(iter(ids)), str):
        raise ProtocolError("the parts of a previews job belong to one catalog")
    refs = [p.get("part_ref") for p in parts]
    if not all(isinstance(r, int) and not isinstance(r, bool) and r > 0 for r in refs):
        raise ProtocolError("part_ref is not a positive integer")
    size = (raw.get("preview") or {}).get("size_px", 256)
    if not isinstance(size, int) or not 16 <= size <= 1024:
        raise ProtocolError("preview.size_px is out of range")
    return str(next(iter(ids))), [int(r) for r in refs], size


def _read_parts(catalog: Path, catalog_id: str, refs: list[int]) -> list[_Part] | None:
    """None when the catalog is another one than the parts belong to."""
    uri = f"{catalog.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10.0) as db:
        meta = dict(db.execute("SELECT key, value FROM catalog_meta").fetchall())
        if meta.get("catalog_id") != catalog_id:
            return None
        marks = ",".join("?" for _ in refs)
        rows = db.execute(
            f"""
            SELECT s.part_ref, s.fingerprint, s.modality, s.sop_class_uid, s.image_type,
                   coalesce(f.transfer_syntax_uid, s.transfer_syntax_uid), f.source_id, f.rel_path,
                   f.kind, coalesce(s.middle_frame, 0), f.burned_in, f.pixel_data, f.photometric,
                   f.rescale_slope, f.rescale_intercept, f.pixel_spacing_row, f.pixel_spacing_col
            FROM cat_series AS s LEFT JOIN files AS f ON f.file_id = s.middle_file_id
            WHERE s.part_ref IN ({marks})
            """,  # noqa: S608 - placeholders only
            refs,
        ).fetchall()
    return [_Part(*row) for row in rows]


def skip_code(part: _Part) -> str | None:
    """ADR 0025 decision 5: what is never previewed, by its code."""
    if part.sop_class_uid in SECONDARY_CAPTURE_SOP_CLASSES:
        return "preview.skipped.secondary_capture"
    if part.file_kind not in ("image", "nifti") or not is_image_class(part.sop_class_uid):
        if part.file_kind != "nifti":
            return "preview.skipped.not_image"
    if part.image_type and "SCREEN SAVE" in part.image_type.upper():
        return "preview.skipped.screen_save"
    if (part.burned_in or "").strip().upper() == "YES":
        return "preview.skipped.burned_in"
    if part.pixel_data in ("truncated", "missing"):
        return "preview.skipped.truncated"
    return None


def _decode(path: Path, part: _Part) -> tuple[Any, bool]:
    """The frame as a float array (rows, columns) or (rows, columns, 3), and
    whether its values are already rescaled."""
    import numpy as np

    if part.file_kind == "nifti":
        import SimpleITK as sitk

        image = sitk.ReadImage(str(path), imageIO="NiftiImageIO")
        array = sitk.GetArrayViewFromImage(image)
        middle = array[array.shape[0] // 2] if array.ndim == 3 else array
        return np.asarray(middle, dtype=np.float64), True
    if part.syntax in _PYDICOM_SYNTAXES:
        from pydicom import dcmread

        ds = dcmread(path)
        frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
        if frames > 1:
            from pydicom.pixels import pixel_array

            data = pixel_array(ds, index=min(part.frame, frames - 1))
        else:
            data = ds.pixel_array
        return np.asarray(data, dtype=np.float64), False
    import SimpleITK as sitk

    image = sitk.ReadImage(str(path))
    array = sitk.GetArrayFromImage(image)
    if array.ndim == 4:  # frames, rows, columns, samples
        array = array[min(part.frame, array.shape[0] - 1)]
    elif array.ndim == 3 and image.GetNumberOfComponentsPerPixel() == 1:
        array = array[min(part.frame, array.shape[0] - 1)]
    # SimpleITK applies the rescale itself; mark it so it is not applied twice.
    return np.asarray(array, dtype=np.float64), True


def _gray(values: Any, part: _Part, rescaled: bool) -> Any:
    import numpy as np

    if values.ndim == 3:  # RGB is kept as it is
        return np.clip(values, 0, 255).astype(np.uint8)
    if part.modality == "CT":
        hu = values if rescaled else values * (part.slope or 1.0) + (part.intercept or 0.0)
        width, center = CT_WINDOW
        out = (hu - (center - width / 2)) / width * 255.0
    else:
        low, high = np.percentile(values, [1.0, 99.0])
        out = (values - low) / max(high - low, 1e-9) * 255.0
    out = np.clip(out, 0, 255)
    if (part.photometric or "").upper() == "MONOCHROME1":
        out = 255 - out
    return out.astype(np.uint8)


def _area_weights(source: int, target: int) -> Any:
    """Rows of the overlap of each output pixel with the input pixels, each
    summing to 1: area averaging for any factor."""
    import numpy as np

    scale = source / target
    weights = np.zeros((target, source))
    for out in range(target):
        start, end = out * scale, (out + 1) * scale
        for index in range(int(start), min(source, int(np.ceil(end)))):
            weights[out, index] = min(end, index + 1) - max(start, index)
        weights[out] /= weights[out].sum()
    return weights


def resize(image: Any, size_px: int, row_mm: float | None, col_mm: float | None) -> Any:
    """The longest side to `size_px`, respecting non-square pixels."""
    import numpy as np

    rows, columns = image.shape[:2]
    height, width = rows * (row_mm or 1.0), columns * (col_mm or 1.0)
    scale = size_px / max(height, width)
    out_rows, out_cols = max(1, round(height * scale)), max(1, round(width * scale))
    if (out_rows, out_cols) == (rows, columns):
        return image
    rw, cw = _area_weights(rows, out_rows), _area_weights(columns, out_cols)
    data = image.astype(np.float64)
    if data.ndim == 3:
        result = np.stack([rw @ data[..., c] @ cw.T for c in range(data.shape[2])], axis=-1)
    else:
        result = rw @ data @ cw.T
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)


def _write(image: Any, folder: Path, fingerprint: str) -> Path:
    import SimpleITK as sitk

    target = folder / f"{fingerprint}.png"
    partial = folder / f"{fingerprint}.png.partial"
    sitk.WriteImage(
        sitk.GetImageFromArray(image, isVector=image.ndim == 3), str(partial), imageIO="PNGImageIO"
    )
    os.replace(partial, target)
    return target


def run_previews(job: Job, channel: ProtocolChannel) -> dict[str, Any]:
    raw = job.payload
    catalog_id, refs, size = parse_parts(raw)
    catalog = job.project_path(str(raw.get("catalog") or ""))
    folder = job.project_path(str(raw.get("previews_dir") or ""))
    private_folder(folder)
    if not catalog.is_file():
        raise JobFailure("catalog_missing", "There is no index yet; scan first", recoverable=True)
    parts = _read_parts(catalog, catalog_id, refs)
    if parts is None:
        raise JobFailure("catalog_changed", "The index changed; ask again", recoverable=True)
    by_ref = {p.part_ref: p for p in parts}
    results: list[dict[str, Any]] = []
    root = job.project_dir.resolve()
    for done, ref in enumerate(refs, start=1):
        channel.send(Progress(job.job_id, "index", done / len(refs), "Making previews"))
        part = by_ref.get(ref)
        if part is None:
            results.append({"part_ref": ref, "written": False, "code": "preview.skipped.not_image"})
            continue
        code = skip_code(part)
        if code is None:
            try:
                path = Path(
                    os.fsdecode(
                        os.path.join(
                            os.fsencode(job.source_root(part.source_id or 0)), part.rel_path or b""
                        )
                    )
                )
                values, rescaled = _decode(path, part)
                image = resize(_gray(values, part, rescaled), size, part.row_mm, part.col_mm)
                target = _write(image, folder, part.fingerprint)
            except (OSError, ValueError, RuntimeError, AttributeError, KeyError, ProtocolError):
                # Which decoder failed and why can quote the file; the code says enough.
                code = "preview.failed.decode"
            else:
                channel.send(
                    Artifact("preview", target.relative_to(root).as_posix(), sha256_file(target))
                )
                results.append({"part_ref": ref, "written": True})
                continue
        results.append({"part_ref": ref, "written": False, "code": code})
    return {"catalog_id": catalog_id, "parts": results}
