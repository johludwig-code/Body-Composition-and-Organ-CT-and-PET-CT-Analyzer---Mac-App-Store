import importlib.util
from pathlib import Path

import pytest

STUB = Path(__file__).resolve().parents[1] / "stubs" / "cc3d" / "__init__.py"


def _load_stub():
    spec = importlib.util.spec_from_file_location("cc3d_stub", STUB)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_stub_imports_because_the_trainer_lookup_imports_cc3d() -> None:
    # nnU-Net imports every trainer module to find one by name, and that chain
    # reaches `import cc3d` at module level; the stub has to import cleanly.
    assert "ADR 0014" in (_load_stub().__doc__ or "")


def test_the_stub_refuses_every_use() -> None:
    with pytest.raises(RuntimeError, match="ADR 0014"):
        _load_stub().connected_components([[1]])


def test_the_stub_answers_interpreter_probes_the_usual_way() -> None:
    # pickle and inspect walk every loaded module with getattr(..., default);
    # a RuntimeError there would break unrelated code in the worker.
    assert getattr(_load_stub(), "__wrapped__", None) is None
