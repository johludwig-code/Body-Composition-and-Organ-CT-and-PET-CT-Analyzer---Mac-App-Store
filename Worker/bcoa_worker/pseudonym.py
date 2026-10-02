"""Pseudonyms, keyed UID hashes and age (plan §6, §13).

Names and birth dates are never stored. Age is derived once at indexing and
the birth date is dropped; DICOM UIDs leave the app only as HMAC-SHA-256 under
a per-project key, because a UID can be looked up in the source PACS.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import date

_AGE = re.compile(r"^(\d{3})([DWMY])$")


def pseudonym(number: int) -> str:
    if number < 1:
        raise ValueError("pseudonyms count from 1")
    return f"P{number:04d}"


def new_project_key() -> bytes:
    return secrets.token_bytes(32)


def hashed_uid(project_key: bytes, uid: str) -> str:
    if len(project_key) < 32:
        raise ValueError("project key must be at least 256 bits")
    return hmac.new(project_key, uid.strip().encode("ascii"), hashlib.sha256).hexdigest()


def age_in_years(birth: date, on: date) -> int:
    if on < birth:
        raise ValueError("study date before birth date")
    years = on.year - birth.year
    if (on.month, on.day) < (birth.month, birth.day):
        years -= 1
    return years


def parse_dicom_age(value: str) -> float | None:
    """PatientAge (AS): `045Y`, `006M`, `003W`, `010D` → years.

    Returns None for anything else; a malformed age is missing, not zero.
    """
    match = _AGE.match(value.strip().upper())
    if not match:
        return None
    number = int(match.group(1))
    per_year = {"Y": 1.0, "M": 12.0, "W": 52.1775, "D": 365.2425}[match.group(2)]
    return number / per_year
