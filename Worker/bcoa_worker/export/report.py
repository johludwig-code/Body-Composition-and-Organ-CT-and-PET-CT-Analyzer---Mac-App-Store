"""Content files of the per-case report (ADR 0017 sections 2, 8 and 11).

The worker writes one small JSON file per case; the app draws the PDF from
it. A case is one segmented series of one patient at one time point. The
values come from the export's own builder, so its QC decisions and empty
cells are the workbook's: an excluded structure is empty with its reason, a
rejected series gets no file, an unreviewed one says so. The file names the
case by pseudonym, time point and series only, and holds no path, no UID and
no patient ID; dates are days since the first study unless the export chose
the year or the full date. The same export writes the same bytes.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from bcoa_worker.export.data import ProjectData
from bcoa_worker.export.options import ExportOptions
from bcoa_worker.export.tables import Slot, _Builder, model_key
from bcoa_worker.intended_use import EVERY_PAGE, STATEMENT

FORMAT = 1
METRICS = ("voxel_count", "volume_ml", "hu_mean", "hu_sd", "hu_median")

# Plain words for the flags a reader of one case needs beside a value
# (ADR 0017 section 8); the definitions page explains the rest.
NOTES = {
    "empty_label": "not found in this CT",
    "truncated": "cut off at the image edge, volume incomplete",
    "volume_outlier": "volume unusual for this project; check the mask",
    "labelmap_resampled": "segmentation resampled to the CT grid",
}
# Structures that only some people have: their absence is anatomy, not a miss.
ACCESSORY = re.compile(r"vertebra_L6|rib_(left|right)_13")
ACCESSORY_NOTE = "not found, an accessory structure that only some people have"
CASE = re.compile(r"^P\d{4,}_t\d+_s\d+$")


def case_name(slot: Slot) -> str:
    return f"{slot.patient.pseudonym}_t{slot.timepoint}_s{slot.series_index}"


def _report_options(options: ExportOptions) -> ExportOptions:
    # Every segmented series of every time point is a case, with every
    # structure of every model; the pseudonym is the only identifier a report
    # ever shows, and an excluded structure is always empty (ADR 0017
    # section 8). The export's choice of patients still decides which cases.
    return replace(
        options,
        layout="long",
        row_level="patient",
        multiple_studies="suffix",
        multiple_series="all",
        identifiers="pseudonym",
        qc="exclude",
        models=None,
        labels=None,
    )


def _note(label: str, flags: tuple[str, ...], excluded: bool, reason: str | None) -> str | None:
    if excluded:
        return f"excluded in QC: {reason}" if reason else "excluded in QC"
    words = [
        ACCESSORY_NOTE if f == "empty_label" and ACCESSORY.fullmatch(label) else NOTES[f]
        for f in flags
        if f in NOTES
    ]
    return "; ".join(words) or None


def cases(
    data: ProjectData, options: ExportOptions, methods_text: str
) -> dict[str, dict[str, Any]]:
    """Case name → content, for every case that gets a report."""
    builder = _Builder(data, _report_options(options))
    run = data.run
    out: dict[str, dict[str, Any]] = {}
    for slot in builder.slots:
        key = slot.series.series_key
        status = builder.qc.series_status(key)
        # No report for a rejected series (ADR 0017 section 8), whatever the
        # export's QC option: a case report of a rejected series would be a
        # page that looks valid.
        if status == "rejected":
            continue
        models = []
        for model in builder.models:
            model_status = builder.model_status(key, model.name)
            structures = []
            for label_id, label in builder.labels[model.name]:
                excluded = builder.qc.label_excluded(key, model.name, label_id)
                entry = builder.qc.latest.get((key, model.name, label_id))
                values = {m: builder.metric(key, model.name, label_id, m) for m in METRICS}
                flags = builder.result_flags.get((key, model.name, label_id), ())
                structures.append(
                    {
                        "label_id": label_id,
                        "label": label,
                        **values,
                        "flags": list(flags),
                        "excluded": excluded,
                        "note": _note(
                            label, flags, excluded, entry.comment if entry and excluded else None
                        ),
                    }
                )
            models.append(
                {
                    "name": model.name,
                    "key": model_key(model.name),
                    "status": model_status,
                    "structures": structures,
                }
            )
        # A case is a segmented series; one whose every model failed or never
        # ran has nothing to report.
        if not any(m["status"] == "ok" for m in models):
            continue
        series = slot.series
        name = case_name(slot)
        if not CASE.fullmatch(name):
            raise ValueError(f"not a case name: {name!r}")
        out[name] = {
            "format": FORMAT,
            "case": name,
            "pseudonym": slot.patient.pseudonym,
            "timepoint": slot.timepoint,
            "series_index": slot.series_index,
            "date": {"kind": options.dates, "value": builder.date_value(slot)},
            "sex": slot.patient.sex,
            "age_years": (
                int(slot.patient.age_at_first_study)
                if slot.patient.age_at_first_study is not None
                else None
            ),
            "series_key": key,
            "series": {
                "description": series.description,
                "modality": series.modality,
                "slice_thickness_mm": series.slice_thickness_mm,
                "pixel_spacing_mm": series.pixel_spacing_mm,
                "kernel": series.kernel,
                "kvp": series.kvp,
                "manufacturer": series.manufacturer,
                "contrast_agent": series.contrast_agent,
                "image_count": series.image_count,
            },
            "device": builder.result_device.get(key),
            "qc": {"status": status, "reviewed": builder.qc.reviewed(key)},
            "models": models,
            "run_id": run.run_id,
            "versions": dict(sorted(run.versions.items())),
            "model_checksums": {m.name: m.zip_sha256 for m in builder.models},
            "methods_text": methods_text,
            "notice": EVERY_PAGE,
            "statement": STATEMENT,
        }
    return out


def dumps(content: dict[str, Any]) -> bytes:
    return (json.dumps(content, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
