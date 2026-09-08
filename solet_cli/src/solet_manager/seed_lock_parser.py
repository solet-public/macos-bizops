"""Closed seed-lock syntax and immutable-identity validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import SourceError

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TREE_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROFILE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_CANONICAL_REPOSITORY = "https://github.com/solet-public/macos-bizops.git"
_CANONICAL_PROFILE = "macos-bizops"
_V1_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "repository",
        "release_tag",
        "commit",
        "tree_hash",
        "profile",
    }
)
_V2_REQUIRED_KEYS = _V1_REQUIRED_KEYS - {"release_tag"}
_OPTIONAL_KEYS = frozenset({"archive_sha256"})


@dataclass(frozen=True)
class SeedLockFields:
    repository: str
    release_tag: str | None
    commit: str
    tree_hash: str
    archive_sha256: str | None
    profile: str


def parse_seed_lock(path: Path) -> SeedLockFields:
    raw = _read_lock(path)
    schema_version = raw.get("schema_version")
    if schema_version == 2:
        _validate_key_set(raw, _V2_REQUIRED_KEYS, 2)
        release_tag = None
    else:
        _validate_key_set(raw, _V1_REQUIRED_KEYS, 1)
        if schema_version != 1:
            raise SourceError("seed lock schema_version must be exactly 1 or 2")
        release_tag = _release_tag(raw)
    repository = _repository(raw)
    commit = _pattern_value(raw, "commit", _COMMIT_PATTERN, "40 lowercase hexadecimal characters")
    tree_hash = _pattern_value(raw, "tree_hash", _TREE_PATTERN, "40 lowercase hexadecimal characters")
    archive_sha256 = _optional_pattern_value(
        raw,
        "archive_sha256",
        _SHA256_PATTERN,
        "64 lowercase hexadecimal characters",
    )
    profile = _pattern_value(raw, "profile", _PROFILE_PATTERN, "a valid identifier")
    return SeedLockFields(
        repository,
        release_tag,
        commit,
        tree_hash,
        archive_sha256,
        profile,
    )
def _read_lock(path: Path) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceError(
            f"seed lock is unreadable or invalid at {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise SourceError("seed lock must contain one JSON object")
    mapping = cast(dict[object, object], raw)
    if not all(isinstance(key, str) for key in mapping):
        raise SourceError("seed lock must contain one JSON object")
    return cast(dict[str, object], mapping)


def _validate_key_set(
    raw: dict[str, object],
    required_keys: frozenset[str],
    schema_version: int,
) -> None:
    keys = frozenset(raw)
    if required_keys <= keys <= required_keys | _OPTIONAL_KEYS:
        return
    missing = sorted(required_keys - keys)
    unknown = sorted(keys - required_keys)
    raise SourceError(
        f"seed lock fields differ from v{schema_version}; missing={missing}, unknown={unknown}"
    )


def _repository(raw: dict[str, object]) -> str:
    value = _string(raw, "repository")
    if not value.startswith("https://github.com/") or not value.endswith(".git"):
        raise SourceError("seed repository must be an explicit GitHub HTTPS .git URL")
    return value


def _release_tag(raw: dict[str, object]) -> str:
    value = _string(raw, "release_tag")
    if not _TAG_PATTERN.fullmatch(value) or value in {"main", "master", "HEAD"}:
        raise SourceError(
            "release_tag must be an exact immutable tag name, not a moving ref"
        )
    return value


def _pattern_value(
    raw: dict[str, object],
    key: str,
    pattern: re.Pattern[str],
    requirement: str,
) -> str:
    value = _string(raw, key)
    if not pattern.fullmatch(value):
        raise SourceError(f"seed {key} must be exactly {requirement}")
    return value


def _optional_pattern_value(
    raw: dict[str, object],
    key: str,
    pattern: re.Pattern[str],
    requirement: str,
) -> str | None:
    if key not in raw:
        return None
    return _pattern_value(raw, key, pattern, requirement)


def _string(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise SourceError(f"seed lock {key} must be a non-empty string")
    return value
