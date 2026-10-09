"""Patient identity as keyed links (ADR 0024).

A later study must still find its patient, so a link to the PatientID has to
outlive the scan, and a plain PatientID must not be what carries it: it would
sit in every row that ties a study to a patient. A link is an HMAC under the
project's `link_key` instead, and Remove Identifiers deletes the key, which
makes every link worthless at once.

`Protocol/fixtures/link_vectors.json` holds the rules and their results; the
app's LinkHasher computes `pid:` links for typed IDs and is held to the same
file in swift test, so a byte that differs here breaks the patient of every
typed ID.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

LINK_KEY_PREFIX = "base64:"
LINK_KEY_BYTES = 32
SEPARATOR = "\x1f"

# IDs that anonymizers and scanners write where there is none. Compared
# case-folded, so "anonymized" is as missing as "ANONYMIZED" (ADR 0024).
DEFAULT_PLACEHOLDER_IDS: tuple[str, ...] = (
    "ANONYMOUS",
    "ANONYMIZED",
    "ANONYMISED",
    "ANON",
    "UNKNOWN",
    "NONE",
    "NULL",
    "N/A",
    "NA",
    "0",
    "-",
)
MAX_FOLDER_LEVEL = 3

PidState = Literal["present", "missing", "placeholder"]


class LinkKeyMismatch(ValueError):
    """The job's link key is malformed or does not match its `link_key_id`
    (error `link_key_mismatch`). The message never holds key material."""


def decode_link_key(value: str) -> bytes:
    """The 32 key bytes of `"base64:" + standard base64 with padding`."""
    if not value.startswith(LINK_KEY_PREFIX):
        raise LinkKeyMismatch("link key without its prefix")
    try:
        key = base64.b64decode(value.removeprefix(LINK_KEY_PREFIX), validate=True)
    except (binascii.Error, ValueError):
        raise LinkKeyMismatch("link key is not base64") from None
    if len(key) != LINK_KEY_BYTES:
        raise LinkKeyMismatch("link key is not 32 bytes")
    return key


def link_key_id(key: bytes) -> str:
    """The first 16 hex characters of HMAC-SHA256(key, "bcoa.link-key-id")."""
    return hmac.new(key, b"bcoa.link-key-id", hashlib.sha256).hexdigest()[:16]


def verify_link_key(link_key: str, expected_id: str) -> bytes:
    """The key bytes, after checking them against the job's `link_key_id`.

    This catches a key or an ID that was damaged or edited by hand, and
    nothing more: a stale job file carries the old key with its own ID, which
    is why the app fills both when the job starts.
    """
    key = decode_link_key(link_key)
    if not hmac.compare_digest(link_key_id(key), expected_id):
        raise LinkKeyMismatch("link key does not match its id")
    return key


def _utf8(text: str) -> bytes:
    # surrogateescape gives back the original bytes of a folder name that is
    # not valid UTF-8 (os.fsdecode decodes it that way), so its link is the
    # link of the name on disk; for every valid string it is plain UTF-8.
    return text.encode("utf-8", "surrogateescape")


def _hmac(key: bytes, *parts: str) -> str:
    return hmac.new(key, _utf8(SEPARATOR.join(parts)), hashlib.sha256).hexdigest()


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def normalize_id(value: str | None) -> str:
    """Spaces and NUL stripped from both ends, then NFC.

    DICOM pads LO values with spaces, and some writers pad with NUL; NFD and
    NFC spellings of one name are one ID. Case is kept: two IDs that differ
    only in case are two IDs.
    """
    if value is None:
        return ""
    return _nfc(value.strip(" \x00"))


@dataclass(frozen=True, slots=True)
class PatientLink:
    normalized: str
    state: PidState
    # "pid:" + hex HMAC; None when the ID is missing or a placeholder.
    link: str | None


def pid_link(key: bytes, normalized: str) -> str:
    """`"pid:" + hex(HMAC-SHA256(key, "pid" ␟ id))` of an ID that
    `normalize_id` already normalized."""
    return "pid:" + _hmac(key, "pid", normalized)


def patient_link(
    key: bytes, patient_id: str | None, placeholders: Iterable[str] = DEFAULT_PLACEHOLDER_IDS
) -> PatientLink:
    """The state and link of a file's PatientID."""
    normalized = normalize_id(patient_id)
    if not normalized:
        return PatientLink(normalized, "missing", None)
    if normalized.casefold() in folded_placeholders(placeholders):
        return PatientLink(normalized, "placeholder", None)
    return PatientLink(normalized, "present", pid_link(key, normalized))


def folded_placeholders(placeholders: Iterable[str]) -> set[str]:
    """The placeholder IDs as a normalized PatientID is compared with them."""
    return {normalize_id(p).casefold() for p in placeholders}


def issuer_link(key: bytes, issuer: str | None) -> str | None:
    """`"issuer:" + hex(HMAC-SHA256(key, "issuer" ␟ value))` of an
    IssuerOfPatientID, after the same stripping and NFC, or None when empty.

    Only whether two issuers differ matters (check.issuer_conflict), so the
    issuer is kept as a link like the ID, and is not part of the ID's link.
    """
    normalized = normalize_id(issuer)
    if not normalized:
        return None
    return "issuer:" + _hmac(key, "issuer", normalized)


def folder_link(key: bytes, source_id: int, components: Sequence[str], level: int) -> str:
    """`"folder:" + hex(HMAC(key, "folder" ␟ source_id ␟ components 1…level))`,
    each component NFC. Level 0 is the source folder itself, so its link
    depends on the source alone."""
    if not 0 <= level <= len(components):
        raise ValueError("folder level outside the path")
    parts = [_nfc(component) for component in components[:level]]
    return "folder:" + _hmac(key, "folder", str(source_id), *parts)


@dataclass(frozen=True, slots=True)
class FolderCandidate:
    level: int
    # For display only, NFC; it lives in the catalog and is never written to
    # project.sqlite (ADR 0024 decision 3).
    label: str
    link: str


def _label(text: str) -> str:
    # A name that is not valid UTF-8 is shown with replacement characters; the
    # link above still uses its bytes.
    return _nfc(text).encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def folder_candidates(
    key: bytes,
    source_id: int,
    source_label: str,
    components: Sequence[str],
    max_level: int = MAX_FOLDER_LEVEL,
) -> list[FolderCandidate]:
    """The folder-ID candidates of a study without a usable PatientID.

    `components` are those of the deepest folder common to the study's files,
    relative to the source root; level k is the k-th of them, up to
    `max_level` and the depth of that folder, and level 0 is the source
    folder, labeled `source_label`.
    """
    deepest = min(max(max_level, 0), MAX_FOLDER_LEVEL, len(components))
    candidates = [FolderCandidate(0, _label(source_label), folder_link(key, source_id, (), 0))]
    for level in range(1, deepest + 1):
        candidates.append(
            FolderCandidate(
                level,
                _label(components[level - 1]),
                folder_link(key, source_id, components, level),
            )
        )
    return candidates


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    """`project_meta.identity_config` and the payload's `identity`, decoded
    leniently like the SelectionConfig: a missing or malformed key takes its
    default."""

    placeholder_ids: tuple[str, ...] = DEFAULT_PLACEHOLDER_IDS
    folder_ids: bool = True
    folder_max_level: int = MAX_FOLDER_LEVEL

    @classmethod
    def from_json(cls, value: object) -> IdentityConfig:
        raw: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
        default = cls()
        placeholders = raw.get("placeholder_ids")
        folder_ids = raw.get("folder_ids")
        level = raw.get("folder_max_level")
        return cls(
            placeholder_ids=(
                tuple(p for p in placeholders if isinstance(p, str))
                if isinstance(placeholders, list)
                else default.placeholder_ids
            ),
            folder_ids=folder_ids if isinstance(folder_ids, bool) else default.folder_ids,
            folder_max_level=(
                level
                if isinstance(level, int)
                and not isinstance(level, bool)
                and 0 <= level <= MAX_FOLDER_LEVEL
                else default.folder_max_level
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "placeholder_ids": list(self.placeholder_ids),
            "folder_ids": self.folder_ids,
            "folder_max_level": self.folder_max_level,
        }

    def placeholders_sha256(self) -> str:
        """`catalog_meta.files_placeholders_sha256`: the placeholder IDs as the
        reader compares them, so that another order or case is no change and
        re-reads nothing."""
        return _sha256(sorted(folded_placeholders(self.placeholder_ids)))

    def sha256(self) -> str:
        """`catalog_meta.identity_config_sha256`, which the regroup records
        like the selection's hash (ADR 0029): a change of folder IDs or of
        their deepest level makes the generation again."""
        return _sha256(
            {
                "placeholder_ids": sorted(folded_placeholders(self.placeholder_ids)),
                "folder_ids": self.folder_ids,
                "folder_max_level": self.folder_max_level,
            }
        )


def _sha256(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
