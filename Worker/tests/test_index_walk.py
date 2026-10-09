"""Discovery (ADR 0022 decision 1): what the walk lists, skips and records,
on small trees made for each test."""

from __future__ import annotations

import errno
import inspect
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from bcoa_worker.index import walk as walk_module
from bcoa_worker.index.walk import Walk, kept_unseen, walk


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def _paths(result: Walk) -> list[bytes]:
    return [entry.rel_path for entry in result.entries]


def test_files_come_as_relative_bytes_in_a_fixed_order(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"b/2": b"2", "a/10": b"10", "a/1": b"1", "c": b"c"})
    (tmp_path / "z").mkdir()
    result = walk(os.fsencode(root))
    # A folder's own files first, then its folders, each sorted by bytes:
    # two walks of one tree read and batch in the same order.
    assert _paths(result) == [b"c", b"a/1", b"a/10", b"b/2"]
    assert result.reachable and not result.empty
    entry = result.entries[1]
    stat = (root / "a" / "1").stat()
    assert (entry.size, entry.mtime_ns, entry.symlink) == (1, stat.st_mtime_ns, False)


def test_names_stay_the_bytes_the_file_system_holds(tmp_path: Path) -> None:
    nfd = "Schädel.dcm".encode()
    (tmp_path / os.fsdecode(nfd)).write_bytes(b"x")
    names = [nfd]
    try:
        (tmp_path / os.fsdecode(b"Bild_\xe9.dcm")).write_bytes(b"x")
        names.append(b"Bild_\xe9.dcm")
    except (OSError, UnicodeError):
        pass  # APFS refuses a name that is not UTF-8; NFD alone is tested then.
    assert sorted(_paths(walk(os.fsencode(tmp_path)))) == sorted(names)


def test_system_files_and_folders_are_skipped_and_counted(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "IM0001.dcm": b"x",
            ".DS_Store": b"x",
            "._IM0001.dcm": b"x",
            "series/Thumbs.db": b"x",
            "series/desktop.ini": b"x",
            "series/IM0002.dcm": b"x",
            ".Spotlight-V100/Store-V2/a": b"x",
            ".Trashes/501/IM0003.dcm": b"x",
            ".fseventsd/0001": b"x",
            ".TemporaryItems/folders.501/b": b"x",
            ".DocumentRevisions-V100/PerUID/c": b"x",
        },
    )
    result = walk(os.fsencode(root))
    assert _paths(result) == [b"IM0001.dcm", b"series/IM0002.dcm"]
    # Five folders, each counted once whatever it holds, and four files.
    assert result.skipped == 9


def test_symbolic_links_are_recorded_and_never_followed(tmp_path: Path) -> None:
    _tree(tmp_path, {"data/IM0001.dcm": b"slice"})
    os.symlink("IM0001.dcm", tmp_path / "data" / "link.dcm")
    os.symlink("..", tmp_path / "data" / "loop")
    os.symlink("/nonexistent/target", tmp_path / "data" / "dangling")
    result = walk(os.fsencode(tmp_path))
    links = {entry.rel_path: entry for entry in result.entries if entry.symlink}
    assert set(links) == {b"data/link.dcm", b"data/loop", b"data/dangling"}
    # The loop is a file of its own, so the walk ends, and nothing below it
    # is listed twice.
    assert _paths(result).count(b"data/IM0001.dcm") == 1
    assert len(result.entries) == 4
    assert links[b"data/link.dcm"].size == os.lstat(tmp_path / "data" / "link.dcm").st_size


class _Listing:
    """os.scandir with chosen failures: a folder that cannot be listed, or
    entries whose stat fails."""

    def __init__(
        self, real: Any, stat_errors: dict[bytes, OSError], kind_errors: dict[bytes, OSError]
    ) -> None:
        self._real = real
        self._errors = stat_errors
        self._kind_errors = kind_errors

    def __enter__(self) -> _Listing:
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def __iter__(self) -> Any:
        for entry in self._real:
            yield _Entry(entry, self._errors.get(entry.name), self._kind_errors.get(entry.name))


class _Entry:
    def __init__(self, real: Any, error: OSError | None, kind_error: OSError | None) -> None:
        self._real = real
        self._error = error
        self._kind_error = kind_error
        self.name = real.name

    def is_symlink(self) -> bool:
        return self._real.is_symlink()

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        if self._kind_error is not None:
            raise self._kind_error
        return self._real.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return self._real.is_file(follow_symlinks=follow_symlinks)

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        if self._error is not None:
            raise self._error
        return self._real.stat(follow_symlinks=follow_symlinks)


def _failing_scandir(
    monkeypatch: pytest.MonkeyPatch,
    *,
    folders: dict[bytes, OSError] | None = None,
    stats: dict[bytes, OSError] | None = None,
    kinds: dict[bytes, OSError] | None = None,
) -> None:
    real = os.scandir

    def scandir(path: bytes) -> Any:
        for suffix, error in (folders or {}).items():
            if path.endswith(suffix):
                raise error
        return _Listing(real(path), stats or {}, kinds or {})

    monkeypatch.setattr(walk_module.os, "scandir", scandir)


def test_a_folder_that_cannot_be_listed_is_recorded_and_the_walk_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path, {"locked/IM1.dcm": b"x", "broken/IM2.dcm": b"x", "fine/IM3.dcm": b"x"})
    _failing_scandir(
        monkeypatch,
        folders={
            b"/locked": PermissionError(errno.EACCES, "denied"),
            b"/broken": OSError(errno.EIO, "I/O error"),
        },
    )
    result = walk(os.fsencode(tmp_path))
    assert _paths(result) == [b"fine/IM3.dcm"]
    assert result.bad_dirs == {b"locked": "permission_denied", b"broken": "io_error"}
    assert not result.empty


def test_eperm_counts_as_a_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path, {"sandboxed/IM1.dcm": b"x"})
    _failing_scandir(monkeypatch, folders={b"/sandboxed": PermissionError(errno.EPERM, "no")})
    assert walk(os.fsencode(tmp_path)).bad_dirs == {b"sandboxed": "permission_denied"}


def test_a_real_folder_without_permission(tmp_path: Path) -> None:
    import dicom_factory

    if not dicom_factory.permissions_enforced(tmp_path):
        pytest.skip("permissions are not enforced here (running as root)")
    _tree(tmp_path, {"locked/IM1.dcm": b"x", "IM2.dcm": b"x"})
    (tmp_path / "locked").chmod(0)
    try:
        result = walk(os.fsencode(tmp_path))
    finally:
        (tmp_path / "locked").chmod(0o700)
    assert result.bad_dirs == {b"locked": "permission_denied"}
    assert _paths(result) == [b"IM2.dcm"]


def test_an_entry_that_cannot_be_described_is_unknown_and_a_vanished_one_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path, {"a.dcm": b"x", "b.dcm": b"x", "c.dcm": b"x"})
    _failing_scandir(
        monkeypatch,
        stats={
            b"a.dcm": FileNotFoundError(errno.ENOENT, "gone"),
            b"b.dcm": OSError(errno.EIO, "I/O error"),
        },
    )
    result = walk(os.fsencode(tmp_path))
    assert _paths(result) == [b"c.dcm"]
    assert result.unknown == {b"b.dcm": "io_error"}
    assert not result.empty


def test_a_folder_that_cannot_be_described_keeps_the_rows_below_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path, {"bulk/00000/IM1.dcm": b"x", "bulk/00001/IM1.dcm": b"x"})
    # On a file system that reports DT_UNKNOWN, is_dir() falls back to an
    # lstat, which a failing share answers with EIO.
    _failing_scandir(monkeypatch, kinds={b"00001": OSError(errno.EIO, "I/O error")})
    result = walk(os.fsencode(tmp_path))
    assert _paths(result) == [b"bulk/00000/IM1.dcm"]
    assert result.unknown == {b"bulk/00001": "io_error"}
    assert kept_unseen(b"bulk/00001/IM1.dcm", result)
    assert not kept_unseen(b"bulk/00002/IM1.dcm", result)


def test_a_root_that_goes_away_during_the_walk_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "share"
    _tree(root, {"a/IM1.dcm": b"x", "b/IM2.dcm": b"x", "c/IM3.dcm": b"x"})
    real = os.scandir
    moved = tmp_path / "unmounted"

    def scandir(path: bytes) -> Any:
        if path.endswith(b"/b"):
            # The share drops after the first folders were listed: what is
            # left fails to list, and would otherwise read as deleted.
            root.rename(moved)
        return real(path)

    monkeypatch.setattr(walk_module.os, "scandir", scandir)
    assert not walk(os.fsencode(root)).reachable
    monkeypatch.undo()
    moved.rename(root)
    # An empty mount point left behind counts as gone as well.
    assert walk_module.root_present(os.fsencode(root))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not walk_module.root_present(os.fsencode(empty))


def test_the_project_folder_inside_a_source_is_skipped(tmp_path: Path) -> None:
    root = _tree(
        tmp_path / "Data",
        {"IM1.dcm": b"x", "Study.bcoaproj/index/catalog.sqlite": b"x", "Study.bcoaproj/x": b"x"},
    )
    project = walk_module.identity(os.fsencode(root / "Study.bcoaproj"))
    assert project is not None
    result = walk(os.fsencode(root), skip=project)
    assert _paths(result) == [b"IM1.dcm"]
    assert result.skipped == 1
    # Reached by another spelling, through a link, it is the same folder.
    os.symlink(root / "Study.bcoaproj", tmp_path / "alias")
    assert walk_module.identity(os.fsencode(tmp_path / "alias")) == project


def test_a_root_that_cannot_be_listed_is_unreachable(tmp_path: Path) -> None:
    assert not walk(os.fsencode(tmp_path / "missing")).reachable
    (tmp_path / "file").write_bytes(b"x")
    assert not walk(os.fsencode(tmp_path / "file")).reachable


def test_an_empty_walk_is_flagged(tmp_path: Path) -> None:
    result = walk(os.fsencode(tmp_path))
    assert result.reachable and result.empty
    # Only what is skipped counts as nothing: a share whose files went away
    # can leave a .DS_Store behind.
    (tmp_path / ".DS_Store").write_bytes(b"x")
    (tmp_path / "empty").mkdir()
    assert walk(os.fsencode(tmp_path)).empty


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_a_fifo_is_never_listed(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "IM1.dcm").write_bytes(b"x")
    result = walk(os.fsencode(tmp_path))
    # Opening a FIFO would block the scan until something writes to it.
    assert _paths(result) == [b"IM1.dcm"]
    assert result.special == 1


def test_rows_below_a_bad_folder_or_unknown_entry_are_kept() -> None:
    walked = Walk(
        reachable=True,
        bad_dirs={b"a/locked": "io_error"},
        unknown={b"b/odd.dcm": "io_error", b"c/odd": "permission_denied"},
    )
    assert kept_unseen(b"a/locked/IM1.dcm", walked)
    assert kept_unseen(b"c/odd/IM1.dcm", walked)
    assert kept_unseen(b"c/odd", walked)
    assert not kept_unseen(b"c/odd2/IM1.dcm", walked)
    assert kept_unseen(b"a/locked/deeper/IM1.dcm", walked)
    assert kept_unseen(b"b/odd.dcm", walked)
    # A sibling whose name only starts like the bad folder is not below it.
    assert not kept_unseen(b"a/locked2/IM1.dcm", walked)
    assert not kept_unseen(b"a/IM1.dcm", walked)
    assert not kept_unseen(b"a/locked", walked)


def test_progress_counts_the_files_found_after_each_folder(tmp_path: Path) -> None:
    _tree(tmp_path, {"a/1": b"x", "a/2": b"x", "b/3": b"x"})
    seen: list[int] = []
    walk(os.fsencode(tmp_path), progress=seen.append)
    assert seen == [0, 2, 3]


def test_a_deep_tree_does_not_need_the_stack(tmp_path: Path) -> None:
    # 200 levels below a recursion limit only 60 frames above this one: a
    # walk that recursed per folder would fail; the path stays short enough
    # for the 1 024 bytes of macOS.
    deep = tmp_path.joinpath(*["d"] * 200)
    deep.mkdir(parents=True)
    (deep / "IM1.dcm").write_bytes(b"x")
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack(0)) + 60)
    try:
        result = walk(os.fsencode(tmp_path))
    finally:
        sys.setrecursionlimit(limit)
    assert _paths(result) == [b"/".join([b"d"] * 200) + b"/IM1.dcm"]
