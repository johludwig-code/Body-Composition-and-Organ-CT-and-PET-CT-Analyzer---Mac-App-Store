"""Kernel classes: soft, sharp or unknown (ADR 0023 decision 7).

Body composition needs a soft-tissue reconstruction: a sharp kernel amplifies
noise in fat and muscle until the segmentation drifts. Kernel codes mean
different things from vendor to vendor, so there is one table per vendor, and
the lists are BOCARTA-MOOSE's `KernelTable.swift`, verbatim and in its order
(ADR 0012).

Matching follows BOCARTA-MOOSE's code, which learned it the hard way: its
first version matched by prefix, Philips' one-letter code "b" matched "BONE",
and a bone kernel counted as soft tissue. Three differences from it, each
recorded in ADR 0023:

- Philips codes match exactly only. BOCARTA-MOOSE's comment says so, but its
  code let Philips codes of three or more letters take a suffix letter; the
  suffix letters are the ones Siemens appends, and Philips appends none.
- Without a ConvolutionKernel the class is unknown. BOCARTA-MOOSE then
  guessed from words of the series description; a description names the
  body region as often as the filter, and unknown already ranks between soft
  and sharp.
- GE must be a word of the manufacturer, not its first two letters, which
  other names begin with too.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

KernelClass = Literal["soft", "sharp", "unknown"]
Vendor = Literal["siemens", "ge", "philips", "canon"]


@dataclass(frozen=True, slots=True)
class KernelTable:
    soft: tuple[str, ...]
    sharp: tuple[str, ...]
    # Codes match only as written, without a suffix letter.
    exact: bool = False


# The lists are laid out as BOCARTA-MOOSE lays them out, so that the two can be
# compared line by line.
# fmt: off
DEFAULT_KERNELS: dict[str, KernelTable] = {
    "siemens": KernelTable(
        soft=(
            "b08", "b10", "b19", "b20", "b26", "b30", "b31", "b35", "b40", "b41",
            "br32", "br34", "br36", "br38", "br40", "br44",
            "i26", "i30", "i31", "i36", "i40", "i41",
            "qr36", "qr40", "sa36", "sa40", "bf37", "bf40", "bv36", "bv40",
        ),
        sharp=(
            "b45", "b46", "b50", "b60", "b70", "b75", "b80",
            "bl57", "bl64", "br49", "br54", "br59", "br64", "br69",
            "i50", "i70", "bv49", "bv59",
            "hr40", "hr49", "hr59", "hr64", "hr68",
            "qr49", "qr59", "qr69", "u70", "u90", "s80", "y80",
        ),
    ),
    # BODY and ABDOMEN were once in this soft list; they are body parts, and
    # matched descriptions that had nothing to do with the filter.
    "ge": KernelTable(
        soft=("standard", "stnd", "std", "soft"),
        sharp=("bone", "boneplus", "bonesplus", "lung", "detail", "edge", "chest", "chst"),
    ),
    "philips": KernelTable(
        soft=("a", "b", "c", "d", "smooth", "standard"),
        sharp=(
            "l", "ya", "yb", "yc", "yd", "e", "ea", "eb", "ec",
            "ub", "uc", "ud", "sharp", "detail", "bone", "lung",
        ),
        exact=True,
    ),
    # Canon (formerly Toshiba) FC kernels run from soft to sharp as the
    # number rises.
    "canon": KernelTable(
        soft=(
            "fc01", "fc02", "fc03", "fc07", "fc08", "fc11", "fc12",
            "fc13", "fc14", "fc17", "fc18", "fc21", "fc22", "fc26",
        ),
        sharp=(
            "fc30", "fc31", "fc35", "fc50", "fc51", "fc52", "fc53",
            "fc55", "fc56", "fc80", "fc81", "fc82", "fc86",
        ),
    ),
}
# fmt: on

# The letters Siemens appends to a code (B30f, B31s, I30d, Br40d). Codes of
# fewer than three characters never take one, because the prefix of a one- or
# two-letter code says nothing.
SUFFIX_LETTERS = frozenset("fsdhqr")
_MIN_SUFFIXED_LENGTH = 3

_TOKEN = re.compile(r"[^\W_]+")


def vendor(manufacturer: str | None) -> Vendor | None:
    """The vendor whose table decides its own codes, from Manufacturer
    upper-cased: SIEMENS; GE as a word, or GENERAL ELECTRIC; PHILIPS; CANON
    or TOSHIBA. Checked in BOCARTA-MOOSE's order."""
    if not manufacturer:
        return None
    name = unicodedata.normalize("NFC", manufacturer).upper()
    if "SIEMENS" in name:
        return "siemens"
    if "GE" in _TOKEN.findall(name) or "GENERAL ELECTRIC" in name:
        return "ge"
    if "PHILIPS" in name:
        return "philips"
    if "CANON" in name or "TOSHIBA" in name:
        return "canon"
    return None


def first_value(kernel: str | None) -> str | None:
    """The first value of ConvolutionKernel as written, stripped, or None.

    Siemens writes the strength of its iterative reconstruction as a second
    value (`I30f\\3`); the filter is the first.
    """
    if kernel is None:
        return None
    first = kernel.split("\\", 1)[0].strip()
    return first or None


def _code(value: str) -> str:
    return value.strip().lower()


def _matches(code: str, entries: Iterable[str], exact: bool) -> bool:
    for entry in entries:
        entry = _code(entry)
        if code == entry:
            return True
        if (
            not exact
            and len(entry) >= _MIN_SUFFIXED_LENGTH
            and len(code) == len(entry) + 1
            and code.startswith(entry)
            and code[-1] in SUFFIX_LETTERS
        ):
            return True
    return False


def classify(
    kernel: str | None,
    manufacturer: str | None,
    tables: Mapping[str, KernelTable] = DEFAULT_KERNELS,
) -> KernelClass:
    """The class of a part's kernel.

    The vendor's own sharp list first, then its own soft list, then every
    table's sharp list, then every soft list, as in BOCARTA-MOOSE's code. The
    vendor's table decides its own codes, because codes mean different things
    from vendor to vendor; for every other code the sharp lists come first,
    because a bone kernel taken for soft tissue corrupts the measurements
    without a sign, while unknown only costs a rank.
    """
    value = first_value(kernel)
    if value is None:
        return "unknown"
    code = _code(value)
    own = tables.get(vendor(manufacturer) or "")
    if own is not None:
        if _matches(code, own.sharp, own.exact):
            return "sharp"
        if _matches(code, own.soft, own.exact):
            return "soft"
    if any(_matches(code, table.sharp, table.exact) for table in tables.values()):
        return "sharp"
    if any(_matches(code, table.soft, table.exact) for table in tables.values()):
        return "soft"
    return "unknown"


def tables_from_json(value: object) -> dict[str, KernelTable]:
    """The `kernels` object of a SelectionConfig, decoded leniently.

    A vendor that is missing takes its default table, and a table that lacks
    a key takes that key from the vendor's default, so Philips stays exact
    unless `"exact": false` is written. A list that is present is taken as it
    is, empty included: the user may empty a list on purpose. A vendor the
    defaults do not know is kept; it cannot be detected from Manufacturer, so
    it only takes part in the lists consulted for every vendor.
    """
    tables = dict(DEFAULT_KERNELS)
    if not isinstance(value, Mapping):
        return tables
    for name, raw in value.items():
        if not isinstance(name, str) or not isinstance(raw, Mapping):
            continue
        default = DEFAULT_KERNELS.get(name, KernelTable(soft=(), sharp=()))
        exact = raw.get("exact")
        tables[name] = KernelTable(
            soft=_strings(raw.get("soft"), default.soft),
            sharp=_strings(raw.get("sharp"), default.sharp),
            exact=exact if isinstance(exact, bool) else default.exact,
        )
    return tables


def tables_to_json(tables: Mapping[str, KernelTable]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, table in tables.items():
        entry: dict[str, Any] = {}
        # Written where it is true or where it is not the vendor's default, so
        # the default tables encode as the protocol fixtures write them and a
        # Philips table made inexact by the user stays inexact when decoded.
        default = DEFAULT_KERNELS.get(name)
        if table.exact or table.exact != (default.exact if default else False):
            entry["exact"] = table.exact
        entry["soft"] = list(table.soft)
        entry["sharp"] = list(table.sharp)
        result[name] = entry
    return result


def _strings(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        return default
    return tuple(item for item in value if isinstance(item, str))
