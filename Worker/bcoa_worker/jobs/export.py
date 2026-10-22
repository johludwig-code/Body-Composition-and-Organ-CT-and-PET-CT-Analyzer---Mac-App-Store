"""Export one run to XLSX or CSV (plan §11, ADR 0008, ADR 0013).

Payload: `run_id`, `stem` (file name without extension) and `options` as in
the export dialog. Everything is written below `<project>/exports/`.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from bcoa_worker.channel import ProtocolChannel
from bcoa_worker.errors import JobFailure
from bcoa_worker.export import data as export_data
from bcoa_worker.export.options import ExportOptionError, parse_options
from bcoa_worker.export.tables import build
from bcoa_worker.export.writers import csv_paths, write_csv, write_xlsx
from bcoa_worker.intended_use import EVERY_PAGE
from bcoa_worker.moose_adapter import sha256_file
from bcoa_worker.protocol import Artifact, Job, Log, Progress, Result

DATABASE = "project.sqlite"
_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,99}$")


def _publish(partial: Path, final: Path) -> None:
    # The app may watch the folder; it must never see half a workbook.
    os.replace(partial, final)


def run(job: Job, channel: ProtocolChannel) -> None:
    payload = job.payload
    run_id = str(payload.get("run_id", ""))
    stem = str(payload.get("stem", ""))
    if not _STEM.match(stem) or stem.endswith("."):
        raise JobFailure("bad_export_name", f"not a usable file name: {stem!r}")
    try:
        options = parse_options(payload.get("options", {}))
    except ExportOptionError as exc:
        raise JobFailure("bad_export_options", str(exc)) from exc

    database = job.project_path(DATABASE)
    if not database.is_file():
        raise JobFailure("no_project_database", "project.sqlite is missing")
    channel.send(Progress(job.job_id, "export", None, "Reading results"))
    try:
        project = export_data.load(database, run_id)
    except LookupError as exc:
        raise JobFailure("unknown_run", str(exc)) from exc

    now = datetime.now(UTC).replace(microsecond=0)
    created = now.isoformat().replace("+00:00", "Z")
    try:
        export = build(project, options, created=created)
    except ValueError as exc:
        raise JobFailure("bad_export_options", str(exc)) from exc
    for warning in export.warnings:
        channel.send(Log("warning", warning))

    folder = job.project_path("exports")
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    # Announced as kind "report", which the v1 schema lists (ADR 0017 section 10).
    reported: list[Path] = []
    channel.send(Progress(job.job_id, "export", 0.5, "Writing files"))
    try:
        if not options.tables:
            pass
        elif options.format == "xlsx":
            target = folder / f"{stem}.xlsx"
            partial = folder / f".{stem}.xlsx.partial"
            write_xlsx(export.sheets, partial, created=now.replace(tzinfo=None))
            _publish(partial, target)
            written.append(target)
        else:
            for sheet_name, target in csv_paths(stem, folder, export.sheets).items():
                partial = target.with_name(f".{target.name}.partial")
                write_csv(export.sheet(sheet_name), partial, options.format)
                _publish(partial, target)
                written.append(target)
    except ValueError as exc:
        # The size limits of plan §11 arrive here, with advice in the message.
        raise JobFailure("export_too_large", str(exc)) from exc

    if options.methods_text:
        target = folder / f"{stem}.methods.txt"
        # Every export carries the notice (plan §2 rule 5); the paragraph above
        # it is the one to paste into a paper, word for word.
        target.write_text(f"{export.methods_text}\n\n{EVERY_PAGE}\n", encoding="utf-8")
        written.append(target)

    if options.reproducibility_package:
        from bcoa_worker.export import package

        target = folder / f"{stem}_reproducibility.zip"
        package.write(target, package.contents(project, export, options, created), created)
        written.append(target)

    if options.case_reports:
        from bcoa_worker.export import report

        channel.send(Progress(job.job_id, "export", 0.8, "Writing case reports"))
        reports = folder / f"{stem}_reports"
        reports.mkdir(exist_ok=True)
        cases = report.cases(project, options, export.methods_text)
        if not cases:
            channel.send(Log("warning", "No case to report: no segmented series that QC kept"))
        for name, content in cases.items():
            target = reports / f"{name}.report.json"
            partial = reports / f".{name}.report.json.partial"
            partial.write_bytes(report.dumps(content))
            _publish(partial, target)
            reported.append(target)

    if options.masks:
        masks = folder / f"{stem}_masks"
        masks.mkdir(exist_ok=True)
        for series_key, model, name in export.masks:
            # The run's own labelmap (ADR 0031 decision 11); projects from
            # before it kept one per series.
            source = job.project_path(f"work/{series_key}/runs/{run_id}/labels/{model}.nii.gz")
            if not source.is_file():
                source = job.project_path(f"work/{series_key}/labels/{model}.nii.gz")
            if not source.is_file():
                channel.send(Log("warning", f"Mask {name} is missing in the project folder"))
                continue
            target = masks / f"{name}.nii.gz"
            shutil.copyfile(source, target)
            written.append(target)

    root = job.project_dir.resolve()
    for kind, paths in (("export", written), ("report", reported)):
        for path in paths:
            channel.send(
                Artifact(kind, path.resolve().relative_to(root).as_posix(), sha256_file(path))
            )
    results = export.sheet("results")
    channel.send(
        Result(
            job.job_id,
            {
                "run_id": run_id,
                "files": [p.resolve().relative_to(root).as_posix() for p in written],
                "reports": [p.resolve().relative_to(root).as_posix() for p in reported],
                "rows": len(results.rows),
                "columns": len(results.columns),
                "excluded_series": export.excluded_series,
                "warnings": export.warnings,
            },
        )
    )
