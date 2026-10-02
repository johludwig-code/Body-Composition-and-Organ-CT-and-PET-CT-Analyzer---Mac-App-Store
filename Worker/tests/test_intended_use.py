from __future__ import annotations

from bcoa_worker import intended_use
from conftest import WORKER_ROOT

PLAN = (WORKER_ROOT.parent / "docs" / "PLAN.md").read_text(encoding="utf-8")


def test_statement_is_the_plans_wording() -> None:
    assert intended_use.STATEMENT in PLAN
    assert intended_use.SHORT in PLAN
