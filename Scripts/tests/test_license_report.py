"""How license_report.py reads licences, on the forms the locked packages
actually use (found when the macOS package set was first audited)."""

from __future__ import annotations

import sys
from email.message import Message
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import license_report
import verify_bundle


class _Dist:
    def __init__(self, **fields: str | list[str]) -> None:
        self.metadata = Message()
        for key, value in fields.items():
            for item in value if isinstance(value, list) else [value]:
                self.metadata[key.replace("_", "-")] = item


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"License_Expression": "BSD-3-Clause"}, "BSD-3-Clause"),
        ({"Classifier": ["License :: OSI Approved :: MIT License"]}, "MIT"),
        # batchgenerators and dynamic_network_architectures paste the text.
        (
            {"License": "Apache License\n        Version 2.0, January 2004\n  TERMS AND"},
            "Apache-2.0",
        ),
        ({"License": "The following files are from different authors"}, "UNKNOWN"),
    ],
)
def test_classify(fields: dict[str, str], expected: str) -> None:
    assert license_report.classify(_Dist(**fields)) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "licence",
    [
        # torch 2.14.1, verbatim.
        "Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause "
        "AND BSL-1.0 AND MIT",
        "MPL-2.0",
        "Apache-2.0 OR BSD-2-Clause",
    ],
)
def test_permissive_expressions_pass(licence: str) -> None:
    assert license_report.verdict(licence, "pkg", {}) is None


@pytest.mark.parametrize(
    ("licence", "word"),
    [
        ("GPL-3.0-only", "not allowed"),
        ("AGPL-3.0", "not allowed"),
        # connected-components-3d before ADR 0014 removed it.
        ("LGPL-3.0-or-later", "approval"),
        ("UNKNOWN", "not recognised"),
        ("Apache-2.0 AND SSPL-1.0", "not recognised"),
    ],
)
def test_copyleft_and_unknown_stop_the_build(licence: str, word: str) -> None:
    reason = license_report.verdict(licence, "pkg", {})
    assert reason is not None
    assert word in reason


def test_an_approval_lets_a_package_through() -> None:
    assert license_report.verdict("UNKNOWN", "dcm2niix", {"dcm2niix": "ADR 0014"}) is None


def test_the_approvals_file_names_an_adr_for_every_entry() -> None:
    import json

    approvals = json.loads(
        (Path(license_report.ROOT) / "Scripts/license_approvals.json").read_text()
    )
    for name, reason in approvals.items():
        if not name.startswith("_"):
            assert "ADR" in reason, name


def test_the_static_notices_pass_the_bundle_check() -> None:
    # The notices are copied into the app, and the first macOS build failed
    # verify_bundle.py because they named the scheme the patch removes.
    assert verify_bundle.FORBIDDEN_STRING not in license_report.STATIC_ENTRIES.encode()
