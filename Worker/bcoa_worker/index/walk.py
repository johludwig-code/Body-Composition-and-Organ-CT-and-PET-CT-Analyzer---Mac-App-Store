"""Discovery: every file below a source root, as bytes (ADR 0022 decision 1).

The walk lists folders and stats files; it opens none of them. What it finds
is compared with the catalog by size, modification time and reader version,
and only what differs is read. Measured on Linux, listing 100 000 directory
entries takes 0.3-0.5 s, so an unchanged rescan costs little more than this.

Paths stay bytes from the root down. The relative path is the catalog's key
and what opens the file again, so a name that is not valid UTF-8, or one
written in NFD on one volume and NFC on another, never turns into a second
file or a missing one. NFC is for matching names and showing labels only.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field

# What macOS and Windows leave beside the files a user copied. Skipped without
# a row and counted: a resource fork `._IM0001.dcm` read as a file would at
# best be junk and at worst a second copy of nothing.
SKIPPED_FILES = frozenset({b".DS_Store", b"Thumbs.db", b"desktop.ini"})
SKIPPED_FILE_PREFIX = b"._"
# Folders of the system's own bookkeeping, found at the root of a volume. A
# Trash can hold a deleted study, which must not come back by being indexed.
SKIPPED_DIRS = frozenset(
    {
        b".Spotlight-V100",
        b".Trashes",
        b".fseventsd",
        b".TemporaryItems",
        b".DocumentRevisions-V100",
    }
)

BadDirCode = str  # 'permission_denied' or 'io_error', as bad_dirs.code takes them


@dataclass(frozen=True, slots=True)
class Entry:
    """One file the walk found: a regular file or a symbolic link."""

    rel_path: bytes
    size: int
    mtime_ns: int
    symlink: bool = False


@dataclass(slots=True)
class Walk:
    """What a walk of one source found."""

    reachable: bool
    entries: list[Entry] = field(default_factory=list)
    # Folders that could not be listed, by relative path. Rows below one are
    # never treated as stale: the walk did not see what is there now.
    bad_dirs: dict[bytes, BadDirCode] = field(default_factory=dict)
    # Files and folders of SKIPPED_FILES and SKIPPED_DIRS (a folder counts once,
    # whatever it holds).
    skipped: int = 0
    # FIFOs, sockets and devices: opening a FIFO blocks until something writes
    # to it, so these are never opened and get no row.
    special: int = 0
    # Entries the listing named and the system would not describe (a stat
    # that failed with something other than "not found"), with the code of
    # the failure. An entry may be a folder, so the old rows at and below it
    # are kept, as below a folder that could not be listed, and the scan gives
    # an entry without a row an `unreadable` one, so that it is counted and
    # can be shown (ADR 0029).
    unknown: dict[bytes, BadDirCode] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """Nothing at all below the root, which is how a dropped network share
        often looks (ADR 0022 decision 1)."""
        return not self.entries and not self.bad_dirs and not self.unknown


def bad_dir_code(error: OSError) -> BadDirCode:
    if error.errno in (errno.EACCES, errno.EPERM):
        return "permission_denied"
    return "io_error"


def is_skipped_file(name: bytes) -> bool:
    return name in SKIPPED_FILES or name.startswith(SKIPPED_FILE_PREFIX)


def kept_unseen(rel_path: bytes, walked: Walk) -> bool:
    """Whether a catalog row the walk did not see must be kept anyway: it lies
    below a folder that could not be listed, or at or below an entry that
    could not be described. The walk does not know what is there now, so it
    is not stale."""
    if rel_path in walked.unknown:
        return True
    parts = rel_path.split(b"/")
    for depth in range(1, len(parts)):
        above = b"/".join(parts[:depth])
        if above in walked.bad_dirs or above in walked.unknown:
            return True
    return False


def root_present(root: bytes) -> bool:
    """Whether the root can still be listed and holds anything. A share that
    dropped shows as an error or as an empty mount point, and either must
    make the source unreachable rather than its files gone (ADR 0022
    decision 1)."""
    try:
        with os.scandir(root) as iterator:
            return any(True for _ in iterator)
    except OSError:
        return False


def identity(path: bytes) -> tuple[int, int] | None:
    """The device and inode of a folder, which is what `walk` compares to
    skip one, so that a path through a link or another spelling is the same
    folder."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def walk(
    root: bytes,
    *,
    progress: Callable[[int], None] | None = None,
    skip: tuple[int, int] | None = None,
) -> Walk:
    """Every regular file and symbolic link below `root`, in a fixed order.

    The walk is iterative, so a deep tree cannot exhaust the stack, and
    follows no symbolic link: a link is recorded as a file of its own, because
    links make loops and can leave the folder the user granted. Each folder's
    entries are sorted by their bytes, so two walks of the same tree give the
    same order and the reads and batches that follow are reproducible.

    `skip` is the (device, inode) of a folder that is never entered: the
    project folder, when the user keeps it beside the data. Its catalog
    changes with every commit, so every scan read it again and wrote a new
    generation, and an unchanged rescan never happened (measured: four scans,
    four generations).

    `progress` is called after each folder with the number of files found so
    far.
    """
    result = Walk(reachable=True)
    listed_root = False
    stack: list[bytes] = [b""]
    while stack:
        rel_dir = stack.pop()
        directory = os.path.join(root, rel_dir) if rel_dir else root
        try:
            with os.scandir(directory) as iterator:
                listed = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            if not rel_dir:
                # A root that cannot be listed cannot be reached: the share is
                # gone, or the grant is. Nothing of the source is deleted.
                return Walk(reachable=False)
            result.bad_dirs[rel_dir] = bad_dir_code(error)
            continue
        if not rel_dir:
            listed_root = bool(listed)
        folders: list[bytes] = []
        for entry in listed:
            name = entry.name
            rel_path = os.path.join(rel_dir, name) if rel_dir else name
            try:
                if entry.is_symlink():
                    info = entry.stat(follow_symlinks=False)
                    result.entries.append(Entry(rel_path, info.st_size, info.st_mtime_ns, True))
                elif entry.is_dir(follow_symlinks=False):
                    if name in SKIPPED_DIRS or (skip is not None and _same(entry, skip)):
                        result.skipped += 1
                    else:
                        folders.append(rel_path)
                elif entry.is_file(follow_symlinks=False):
                    if is_skipped_file(name):
                        result.skipped += 1
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        result.special += 1
                        continue
                    result.entries.append(Entry(rel_path, info.st_size, info.st_mtime_ns))
                else:
                    result.special += 1
            except FileNotFoundError:
                # Deleted between the listing and the stat: it is not there,
                # unless the whole share went, which the end of the walk asks.
                continue
            except OSError as error:
                result.unknown[rel_path] = bad_dir_code(error)
        # Reversed, so that the stack hands them out in sorted order.
        stack.extend(reversed(folders))
        if progress is not None:
            progress(len(result.entries))
    if listed_root and not root_present(root):
        # The share dropped during the walk: its last folders failed to list
        # and their files to stat, which would otherwise read as deleted. A
        # root that was empty from the start is the empty walk the scan
        # judges itself.
        return Walk(reachable=False)
    return result


def _same(entry: os.DirEntry[bytes], folder: tuple[int, int]) -> bool:
    info = entry.stat(follow_symlinks=False)
    return (info.st_dev, info.st_ino) == folder
