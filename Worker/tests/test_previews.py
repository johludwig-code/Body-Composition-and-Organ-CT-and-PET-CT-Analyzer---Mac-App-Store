"""Index mode previews (ADR 0025): the middle slice of each part asked for,
windowed, at most 256 px, written atomically; skipped parts get a code."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import SimpleITK as sitk
from test_pipeline_jobs import ct_volume, write_series

from bcoa_worker import worker
from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.index import previews
from bcoa_worker.protocol import parse_job
from index_jobs import index_job, run_index


def _previews_job(project: Path, source: Path, catalog_id: str, refs: list[int]) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "job_id": "j_previews",
        "kind": "index",
        "project_dir": str(project),
        "log_path": str(project / "logs" / "j_previews.log"),
        "resources_dir": str(project),
        "payload": {
            "mode": "previews",
            "catalog": "index/catalog.sqlite",
            "previews_dir": "index/previews",
            "sources": [
                {"source_id": 1, "root": str(source), "status": "active", "volume_kind": "local"}
            ],
            "parts": [{"catalog_id": catalog_id, "part_ref": r} for r in refs],
            "preview": {"size_px": 32},
        },
    }


def _run(raw: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    buffer = io.StringIO()
    code = worker.run_job(parse_job(raw), ProtocolChannel(buffer), worker._handlers())
    return code, [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_a_ct_part_gets_a_windowed_preview(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_series(source / "CT", ct_volume())
    project = tmp_path / "Study.bcoaproj"
    project.mkdir()
    assert run_index(index_job(project, {1: source}))[0] == 0
    with sqlite3.connect(project / "index" / "catalog.sqlite") as db:
        catalog_id = db.execute(
            "SELECT value FROM catalog_meta WHERE key = 'catalog_id'"
        ).fetchone()[0]
        ref, fingerprint = db.execute("SELECT part_ref, fingerprint FROM cat_series").fetchone()
    code, events = _run(_previews_job(project, source, catalog_id, [ref, 999]))
    assert code == 0, events
    result = next(e["payload"] for e in events if e["type"] == "result")
    assert result["parts"][0] == {"part_ref": ref, "written": True}
    assert result["parts"][1]["written"] is False
    png = project / "index" / "previews" / f"{fingerprint}.png"
    image = sitk.GetArrayFromImage(sitk.ReadImage(str(png)))
    assert image.shape == (32, 32)
    # Air is black and soft tissue at W400/L40 mid-gray.
    assert image[0, 0] == 0 and 120 <= image[16, 16] <= 150
    assert not list((project / "index" / "previews").glob("*.partial"))
    assert any(e["type"] == "artifact" and e["kind"] == "preview" for e in events)


def test_burned_in_and_non_image_parts_are_skipped() -> None:
    def part(**fields: Any) -> previews._Part:
        base = dict(
            part_ref=1,
            fingerprint="f",
            modality="CT",
            sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
            image_type="ORIGINAL\\PRIMARY\\AXIAL",
            syntax="1.2.840.10008.1.2.1",
            source_id=1,
            rel_path=b"a",
            file_kind="image",
            frame=0,
            burned_in=None,
            pixel_data="ok",
            photometric="MONOCHROME2",
            slope=1.0,
            intercept=-1024.0,
            row_mm=1.0,
            col_mm=1.0,
        )
        return previews._Part(**(base | fields))

    assert previews.skip_code(part()) is None
    assert previews.skip_code(part(burned_in="YES")) == "preview.skipped.burned_in"
    assert (
        previews.skip_code(part(image_type="DERIVED\\SECONDARY\\SCREEN SAVE"))
        == "preview.skipped.screen_save"
    )
    assert (
        previews.skip_code(part(sop_class_uid="1.2.840.10008.5.1.4.1.1.7"))
        == "preview.skipped.secondary_capture"
    )
    assert previews.skip_code(part(file_kind="non_image")) == "preview.skipped.not_image"
    assert previews.skip_code(part(pixel_data="truncated")) == "preview.skipped.truncated"


def test_resizing_keeps_the_physical_aspect() -> None:
    image = np.full((100, 200), 200, dtype=np.uint8)
    out = previews.resize(image, 50, row_mm=2.0, col_mm=1.0)
    # 100 rows of 2 mm and 200 columns of 1 mm are a square.
    assert out.shape == (50, 50) and int(out.mean()) == 200


def test_a_job_with_too_many_parts_is_refused() -> None:
    with pytest.raises(previews.ProtocolError):
        previews.parse_parts({"parts": [{"catalog_id": "a", "part_ref": n} for n in range(1, 66)]})
