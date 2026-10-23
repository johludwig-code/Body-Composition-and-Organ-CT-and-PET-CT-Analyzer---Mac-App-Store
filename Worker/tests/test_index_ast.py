"""What the code of bcoa_worker/index may not contain, held by its syntax
tree (ADR 0015, ADR 0016, ADR 0022 decision 3, ADR 0024 decision 7).

The guard refuses sockets and program starts at run time; this refuses the
code that would attempt them, before it runs. A window SimpleITK opens to
show an image starts a program, and a process pool fails in the sandbox.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bcoa_worker import worker
from conftest import WORKER_ROOT

PACKAGE = WORKER_ROOT / "bcoa_worker"
INDEX = PACKAGE / "index"
MODULES = sorted(INDEX.glob("*.py"))

FORBIDDEN_IMPORTS = (
    "subprocess",
    "socket",
    "ssl",
    "requests",
    "urllib",
    "urllib3",
    "http",
    "ftplib",
    "smtplib",
    "asyncio",
    "multiprocessing",
    "concurrent",
    "pydicom.fileset",
)
FORBIDDEN_ATTRIBUTES = {
    "Show",
    "ImageViewer",
    "system",
    "popen",
    "fork",
    "forkpty",
    "posix_spawn",
    "posix_spawnp",
    "startfile",
    "execv",
    "execve",
    "execvp",
    "execl",
    "spawnv",
    "spawnl",
}


def _imports(tree: ast.AST) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.append((node.module, node.lineno))
            found += [(f"{node.module}.{alias.name}", node.lineno) for alias in node.names]
    return found


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_package_has_the_modules_this_test_reads() -> None:
    names = {path.stem for path in MODULES}
    assert {"__init__", "walk", "read", "dicomdir", "nifti", "run", "group"} <= names


@pytest.mark.parametrize("path", MODULES, ids=lambda path: path.name)
def test_no_network_no_program_and_no_pool(path: Path) -> None:
    tree = _tree(path)
    for name, line in _imports(tree):
        for forbidden in FORBIDDEN_IMPORTS:
            assert name != forbidden and not name.startswith(forbidden + "."), (path.name, line)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_ATTRIBUTES, (path.name, node.lineno, node.attr)
        if isinstance(node, ast.Name):
            assert node.id not in ("ThreadPoolExecutor", "ProcessPoolExecutor"), path.name


def test_the_requests_block_precedes_every_pydicom_import() -> None:
    init = INDEX / "__init__.py"
    tree = _tree(init)
    blocks = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and ast.unparse(target) == "sys.modules['requests']"
            and isinstance(node.value, ast.Constant)
            and node.value.value is None
            for target in node.targets
        )
    ]
    assert len(blocks) == 1
    early = [line for name, line in _imports(tree) if name.split(".")[0] == "pydicom"]
    assert all(line > blocks[0] for line in early)
    # Every other module that imports pydicom is inside the package, whose
    # __init__ runs first; no module of the worker outside it imports pydicom.
    for path in sorted(PACKAGE.rglob("*.py")):
        if INDEX in path.parents:
            continue
        importing = [name for name, _ in _imports(_tree(path)) if name.split(".")[0] == "pydicom"]
        assert not importing, path


def test_only_the_failure_log_prints() -> None:
    # Everything else leaves the job as events, which hold counts and codes.
    printing = [
        (path.name, node.lineno)
        for path in MODULES
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert [name for name, _ in printing] == ["run.py"]


def test_the_index_handler_is_imported_only_when_an_index_job_runs() -> None:
    assert worker._JOB_MODULES["index"] == "bcoa_worker.index.run"
    names = [name for name, _ in _imports(_tree(PACKAGE / "worker.py"))]
    assert not [name for name in names if name.startswith("bcoa_worker.index")]
