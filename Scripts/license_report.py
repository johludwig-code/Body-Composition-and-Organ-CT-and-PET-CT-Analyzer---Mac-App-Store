#!/usr/bin/env python3
"""THIRD_PARTY_NOTICES.md from what is actually installed in the bundle (§14).

Run with the bundled interpreter, after build_runtime.sh:

    build/runtime/python/bin/python3 Scripts/license_report.py \
        --out build/licenses/THIRD_PARTY_NOTICES.md

Whitelist principle: GPL or AGPL fails the build. LGPL or an unrecognised
licence fails unless Scripts/license_approvals.json approves that package
with a reference to the ADR that justified it. Weak copyleft such as MPL-2.0
(certifi) is allowed and named, since its source location must be stated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PERMISSIVE = {
    "MIT",
    "MIT-CMU",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSD",
    "Apache-2.0",
    "PSF-2.0",
    "Python-2.0",
    "ISC",
    "Zlib",
    "HPND",
    "Unlicense",
    "CC0-1.0",
    "0BSD",
    "MIT-0",
    "BSL-1.0",
    "Apache-2.0 OR BSD-3-Clause",
    "BSD-3-Clause OR Apache-2.0",
}
WEAK_COPYLEFT = {"MPL-2.0"}

_CLASSIFIER_MAP = {
    "MIT License": "MIT",
    "BSD License": "BSD",
    "Apache Software License": "Apache-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "ISC License (ISCL)": "ISC",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "zlib/libpng License": "Zlib",
    "Historical Permission Notice and Disclaimer (HPND)": "HPND",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0",
    "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.0-or-later",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "GNU Affero General Public License v3": "AGPL-3.0",
}

_TEXT_HINTS = [
    (re.compile(r"\bMIT\b", re.I), "MIT"),
    (re.compile(r"Apache(?: License)?,? (?:Version )?2", re.I), "Apache-2.0"),
    (re.compile(r"\bBSD[- ]3", re.I), "BSD-3-Clause"),
    (re.compile(r"\bBSD[- ]2", re.I), "BSD-2-Clause"),
    (re.compile(r"\bBSD\b", re.I), "BSD"),
    (re.compile(r"\bMPL[- ]?2", re.I), "MPL-2.0"),
    (re.compile(r"\bPSF\b|Python Software Foundation", re.I), "PSF-2.0"),
    (re.compile(r"\bLGPL", re.I), "LGPL"),
    (re.compile(r"\bAGPL", re.I), "AGPL"),
    (re.compile(r"\bGPL", re.I), "GPL"),
]

# Bundled components that are not Python distributions.
# These notices ship inside the app, where verify_bundle.py rejects the
# enterprise scheme's name anywhere; the first macOS build failed on this text.
STATIC_ENTRIES = """\
## CPython 3.12 (python-build-standalone)

Licence: PSF-2.0, with the licences of the libraries it bundles (OpenSSL
Apache-2.0, SQLite public domain, zlib, libffi MIT, XZ, bzip2, mpdecimal,
ncurses). Source: https://github.com/astral-sh/python-build-standalone

The file `urllib/parse.py` was modified: Apple's enterprise-distribution URL
scheme was removed from `uses_netloc`, as CPython's
`--with-app-store-compliance` does.

## MOOSE model weights

Copyright the MOOSE authors (ENHANCE.PET, Medical University of Vienna, LMU
Munich). Licensed under CC BY 4.0, https://creativecommons.org/licenses/by/4.0/.
Source: https://github.com/ENHANCE-PET/MOOSE (release archives named in
models/manifest.json).

Changes: optimizer state was removed from `checkpoint_final.pth`; the network
weights are unchanged. `checkpoint_best.pth`, validation predictions and
training logs were not included. SHA-256 before and after each change are in
models/manifest.json.

## Libraries inside the package wheels that carry their own terms

- FreeType (in Pillow and matplotlib): used under the FreeType License (FTL),
  which asks for this credit: portions of this software are copyright
  © The FreeType Project (www.freetype.org). All rights reserved.
- libgfortran, libquadmath, libgcc_s (in SciPy and NumPy): GPL-3.0 with the
  GCC Runtime Library Exception, which allows distributing them with
  software under any licence.
- OpenSSL (in CPython's `_ssl`): Apache-2.0. The app has no network
  entitlement and never opens a connection; see docs/OPEN_QUESTIONS.md #12.

Removed from the packages before signing (ADR 0014): TinyCC from blosc2
(LGPL-2.1), connected-components-3d (LGPL-3.0), python-gdcm, setuptools.

## GRDB.swift

Copyright Gwendal Roué. MIT licence. https://github.com/groue/GRDB.swift
"""


@dataclass
class Package:
    name: str
    version: str
    licence: str
    texts: list[tuple[str, str]]


def classify(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License :: OSI Approved :: "):
            mapped = _CLASSIFIER_MAP.get(classifier.rsplit(" :: ", 1)[-1])
            if mapped:
                return mapped
    short = (meta.get("License") or "").strip()
    # Some packages paste the whole licence text into this field
    # (batchgenerators: "Apache License\n Version 2.0 …"); its first lines
    # name it, the rest would match every hint at once.
    first = " ".join(line.strip() for line in short.splitlines()[:2] if line.strip())
    if first and len(first) < 80:
        for pattern, name in _TEXT_HINTS:
            if pattern.search(first):
                return name
    return "UNKNOWN"


def licence_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for file in dist.files or []:
        if re.search(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", file.name, re.I):
            try:
                raw = Path(str(dist.locate_file(file))).read_bytes()
                texts.append((file.name, raw.decode("utf-8", errors="replace")))
            except OSError:
                continue
    return texts


def verdict(licence: str, name: str, approvals: dict[str, str]) -> str | None:
    """None if allowed, else the reason the build must stop."""
    if name.lower() in approvals:
        return None
    upper = licence.upper()
    if "AGPL" in upper or re.search(r"(?<!L)GPL", upper):
        if " OR " in upper and any(p in licence for p in ("MIT", "BSD", "Apache")):
            return None
        return f"{name}: {licence} is not allowed in the bundle"
    if "LGPL" in upper:
        return f"{name}: {licence} needs an approval with ADR in license_approvals.json"
    if licence in PERMISSIVE or licence in WEAK_COPYLEFT:
        return None
    # "Apache-2.0 WITH LLVM-exception" (torch): an exception only grants
    # more, so the licence it modifies decides.
    plain = re.sub(r"\s+WITH\s+[A-Za-z0-9.-]+", "", licence)
    parts = re.split(r"\s+(?:OR|AND)\s+|[()]", plain)
    if all(p.strip() in PERMISSIVE | WEAK_COPYLEFT for p in parts if p.strip()):
        return None
    return f"{name}: licence '{licence}' not recognised; approve it in license_approvals.json"


def collect(approvals: dict[str, str], site: Path | None = None) -> tuple[list[Package], list[str]]:
    packages: dict[str, Package] = {}
    problems: list[str] = []
    found = metadata.distributions(path=[str(site)]) if site else metadata.distributions()
    for dist in found:
        name = dist.metadata.get("Name")
        if not name or name.lower() in packages:
            continue
        licence = classify(dist)
        reason = verdict(licence, name, approvals)
        if reason:
            problems.append(reason)
        packages[name.lower()] = Package(name, dist.version, licence, licence_texts(dist))
    return sorted(packages.values(), key=lambda p: p.name.lower()), problems


def render(packages: list[Package]) -> str:
    lines = [
        "# Third-party notices",
        "",
        "This application bundles the components listed below. Generated by",
        "Scripts/license_report.py from the installed packages; do not edit by hand.",
        "",
        STATIC_ENTRIES,
        "## Python packages",
        "",
        "| package | version | licence |",
        "|---|---|---|",
    ]
    lines += [f"| {p.name} | {p.version} | {p.licence} |" for p in packages]
    for p in packages:
        lines += ["", f"### {p.name} {p.version}", "", f"Licence: {p.licence}"]
        for filename, text in p.texts:
            lines += ["", f"#### {filename}", "", "```text", text.rstrip(), "```"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "build/licenses/THIRD_PARTY_NOTICES.md")
    parser.add_argument("--approvals", type=Path, default=ROOT / "Scripts/license_approvals.json")
    # Lets the macOS package set be audited from any machine: the wheels for
    # aarch64-apple-darwin can be unpacked anywhere with `uv pip install
    # --target`, they just cannot be imported there.
    parser.add_argument("--site", type=Path, help="site-packages to audit instead of this one")
    args = parser.parse_args()
    approvals = {
        k.lower(): v
        for k, v in (
            json.loads(args.approvals.read_text()) if args.approvals.exists() else {}
        ).items()
        if not k.startswith("_")
    }
    packages, problems = collect(approvals, args.site)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(packages), encoding="utf-8")
    print(f"[licences] {len(packages)} packages -> {args.out}")
    for problem in problems:
        print(f"[licences] FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
