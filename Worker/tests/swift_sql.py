"""The SQL the app runs is written once, as string constants in BCOAStore; the
worker suite extracts the same text by regex and executes it, so a syntax or
logic error shows up on Linux before the first build on a Mac."""

from __future__ import annotations

import re
from pathlib import Path

from conftest import WORKER_ROOT

STORE = WORKER_ROOT.parent / "Packages/BCOAStore/Sources/BCOAStore"
MIGRATIONS = STORE / "StoreMigrations.swift"
INDEX_SQL = STORE / "IndexSQL.swift"

_CONSTANT = re.compile(r'static let (\w+) = """\n(.*?)\n\s*"""', re.S)
# Every multi-line constant, in whatever form: a raw `#"""` literal, or another
# layout, would escape _CONSTANT, and its SQL would never run here.
_OPENING = re.compile(r'static let \w+\s*=\s*#*"""')
_ENUM = re.compile(r"^public enum (\w+)", re.M)


def constants(path: Path) -> dict[str, dict[str, str]]:
    """Every `static let name = \"\"\"…\"\"\"` of a Swift file, by enclosing
    enum: two enums may both have an `inputs`."""
    text = path.read_text()
    enums = [(m.start(), m.group(1)) for m in _ENUM.finditer(text)]
    found: dict[str, dict[str, str]] = {}
    for match in _CONSTANT.finditer(text):
        owner = [name for start, name in enums if start < match.start()][-1]
        names = found.setdefault(owner, {})
        name, sql = match.group(1), match.group(2)
        assert name not in names, f"{owner}.{name} is defined twice"
        # Swift reads a backslash as an escape, an interpolation or a joined
        # line, and this suite reads it as a character: the SQL would run one
        # way here and another, or not compile, in the app.
        assert "\\" not in sql, f"{owner}.{name} contains a backslash"
        assert '"""' not in sql, f"{owner}.{name} contains a triple quote"
        names[name] = sql
    extracted = sum(len(names) for names in found.values())
    assert len(_OPENING.findall(text)) == extracted, f"{path.name}: a constant escaped the regex"
    return found


def migration(name: str) -> str:
    sql = constants(MIGRATIONS)["StoreMigrations"].get(name)
    assert sql, f"migration {name} not found"
    return sql
