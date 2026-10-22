"""The reproducibility package (plan §12): what a publication's supplement
needs to trace every number to its run, and nothing that identifies anyone.

One ZIP beside the export: the run's provenance as JSON, the metrics in the
long layout, the labels, the QC table, the data dictionary, the export
settings, the methods text and the licence notices of the models. It is
built with the export's own loader and tables, so its numbers are the
export's, with the identifiers fixed to pseudonyms and the dates to days
since the first study, whatever the export chose; it holds no images and no
plain identifiers. Entries carry the export's time, so the same export
gives the same archive.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import asdict, replace
from pathlib import Path

from bcoa_worker.export.data import ProjectData
from bcoa_worker.export.options import ExportOptions
from bcoa_worker.export.tables import (
    APP_NAME,
    BIBTEX,
    CITATIONS,
    MODEL_ATTRIBUTION,
    Export,
    Sheet,
    build,
)
from bcoa_worker.intended_use import EVERY_PAGE, STATEMENT

SHEETS = ("labels", "qc", "data_dictionary")

README = """Reproducibility package of an export of the {app}.

{notice}
{statement}

provenance.json      the run: versions, device, model checksums, settings
results_long.csv     every metric, one row per patient, series, model and structure
labels.csv           the structures of every model
qc.csv               the QC decisions and the structures excluded
data_dictionary.csv  what every column holds, with its unit
export_settings.json the options of this package and of the export it belongs to
methods.txt          the methods text to cite
LICENSES.txt         how to cite MOOSE, and the licence of its model weights

Patients are named by pseudonym and dates are days since each patient's
first study; no image and no patient ID is in this package.
"""


def _csv(sheet: Sheet) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([c.name for c in sheet.columns])
    for row in sheet.rows:
        writer.writerow(["" if v is None else v for v in row])
    return out.getvalue().encode("utf-8")


def package_options(options: ExportOptions) -> ExportOptions:
    """The export's choices, with the ones that could identify a patient or
    change the layout fixed (plan §12)."""
    return replace(
        options,
        format="csv",
        layout="long",
        identifiers="pseudonym",
        dates="days_since_first",
        methods_text=True,
        masks=False,
        reproducibility_package=False,
    )


def contents(
    data: ProjectData, export: Export, options: ExportOptions, created: str
) -> dict[str, bytes]:
    long = build(data, package_options(options), created=created)
    provenance = {row[0]: row[1] for row in long.sheet("provenance").rows if row[1] is not None}
    provenance["models"] = [
        {
            "name": m.name,
            "archive_sha256": m.zip_sha256,
            "labels": {str(k): v for k, v in m.labels.items()},
        }
        for m in data.run.models
    ]
    files = {
        "README.txt": README.format(app=APP_NAME, notice=EVERY_PAGE, statement=STATEMENT).encode(),
        "provenance.json": json.dumps(provenance, indent=1, sort_keys=True).encode(),
        "results_long.csv": _csv(long.sheet("results")),
        "export_settings.json": json.dumps(
            {"package": asdict(package_options(options)), "export": asdict(options)},
            indent=1,
            sort_keys=True,
        ).encode(),
        "methods.txt": f"{export.methods_text}\n\n{EVERY_PAGE}\n".encode(),
        "LICENSES.txt": (
            "\n\n".join(CITATIONS)
            + "\n\n"
            + BIBTEX
            + "\n\n"
            + MODEL_ATTRIBUTION
            + "\n\nThe MOOSE model weights are licensed under CC BY 4.0; nothing in this "
            "package restricts the rights that license grants.\n"
        ).encode(),
    }
    for name in SHEETS:
        files[f"{name}.csv"] = _csv(long.sheet(name))
    return files


def write(path: Path, files: dict[str, bytes], created: str) -> None:
    # The export's own time on every entry, not the clock's: the same export
    # makes the same archive, byte for byte.
    stamp = tuple(int(part) for part in created[:19].replace("T", "-").replace(":", "-").split("-"))
    partial = path.with_name(f".{path.name}.partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=stamp)  # type: ignore[arg-type]
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    partial.replace(path)
