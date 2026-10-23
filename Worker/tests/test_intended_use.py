from __future__ import annotations

from bcoa_worker import intended_use
from conftest import WORKER_ROOT

PLAN = (WORKER_ROOT.parent / "docs" / "PLAN.md").read_text(encoding="utf-8")


def test_statement_is_the_plans_wording() -> None:
    assert intended_use.STATEMENT in PLAN
    assert intended_use.SHORT in PLAN


def test_the_every_page_line_is_the_plans_wording() -> None:
    assert intended_use.EVERY_PAGE in PLAN


def _swift_literals() -> str:
    swift = (WORKER_ROOT.parent / "Packages/BCOAKit/Sources/BCOAKit/IntendedUse.swift").read_text(
        encoding="utf-8"
    )
    # Multi-line literals end their lines with a backslash; joined, they read
    # as the one line Swift makes of them.
    return " ".join(line.strip().rstrip("\\").strip() for line in swift.splitlines())


def test_regulatory_and_warranty_lines_are_the_apps() -> None:
    swift = _swift_literals()
    assert intended_use.REGULATORY in swift
    assert intended_use.WARRANTY in swift


def test_the_package_readme_carries_every_notice(tmp_path) -> None:
    from bcoa_worker.export import package

    readme = package.README.format(
        app="App",
        notice=intended_use.EVERY_PAGE,
        statement=intended_use.STATEMENT,
        regulatory=intended_use.REGULATORY,
        warranty=intended_use.WARRANTY,
    )
    for text in (intended_use.EVERY_PAGE, intended_use.REGULATORY, intended_use.WARRANTY):
        assert text in readme
