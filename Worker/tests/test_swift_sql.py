"""The extraction behind every SQL test: it must read each constant exactly as
Swift does, or refuse, because a constant it reads differently is tested in a
form the app never runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from swift_sql import INDEX_SQL, MIGRATIONS, constants


def test_every_constant_of_the_store_is_read() -> None:
    # 16 in IndexSQL.swift and v1, v2 in StoreMigrations.swift when this was
    # written; the count only guards against a regression to zero.
    assert sum(len(c) for c in constants(INDEX_SQL).values()) >= 16
    assert set(constants(MIGRATIONS)["StoreMigrations"]) >= {"v1", "v2"}


def _swift(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "Example.swift"
    path.write_text(f"public enum Example {{\n{body}\n}}\n")
    return path


@pytest.mark.parametrize(
    "body",
    [
        # A LIKE escape, which Swift reads as an escape sequence.
        "    static let like = \"\"\"\n        SELECT 1 WHERE 'a' LIKE 'a' ESCAPE '\\\\'\n        \"\"\"",
        # An interpolation hidden in a comment.
        '    static let comment = """\n        -- see \\(note)\n        SELECT 1\n        """',
        # A raw literal, which the extraction does not read at all.
        '    static let raw = #"""\n        SELECT 1\n        """#',
    ],
    ids=["backslash", "interpolation", "raw_literal"],
)
def test_text_that_swift_reads_differently_is_refused(tmp_path: Path, body: str) -> None:
    with pytest.raises(AssertionError):
        constants(_swift(tmp_path, body))


def test_plain_constants_are_read_as_written(tmp_path: Path) -> None:
    path = _swift(tmp_path, '    static let one = """\n        SELECT 1;\n        """')
    assert constants(path) == {"Example": {"one": "        SELECT 1;"}}
