"""Formula-installed immutable seed lock value object."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import JsonValue
from .seed_lock_parser import parse_seed_lock


@dataclass(frozen=True)
class SeedLock:
    """Reviewed seed identity shipped by the formula."""

    repository: str
    release_tag: str | None
    commit: str
    tree_hash: str
    archive_sha256: str | None
    profile: str

    def source_ref(self) -> str:
        """Return the immutable source reference carried into the setup flow."""

        return self.release_tag if self.release_tag is not None else self.commit

    def identity_dict(self) -> dict[str, JsonValue]:
        return {
            "seed_repository": self.repository,
            "seed_tag": self.release_tag,
            "seed_commit": self.commit,
            "seed_tree_hash": self.tree_hash,
            "seed_archive_sha256": self.archive_sha256,
            "profile": self.profile,
        }


def load_seed_lock(path: Path) -> SeedLock:
    """Parse one closed seed-lock schema and reject moving references."""

    fields = parse_seed_lock(path)
    return SeedLock(
        fields.repository,
        fields.release_tag,
        fields.commit,
        fields.tree_hash,
        fields.archive_sha256,
        fields.profile,
    )


def seed_lock_from_identity(value: dict[str, JsonValue]) -> SeedLock:
    """Reconstitute the transaction-recorded lock used for resume."""

    return SeedLock(
        repository=str(value["seed_repository"]),
        release_tag=_optional_release_tag(value["seed_tag"]),
        commit=str(value["seed_commit"]),
        tree_hash=str(value["seed_tree_hash"]),
        archive_sha256=_optional_archive_sha256(value["seed_archive_sha256"]),
        profile=str(value["profile"]),
    )


def _optional_archive_sha256(value: JsonValue) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("seed_archive_sha256 must be a string or null")


def _optional_release_tag(value: JsonValue) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("seed_tag must be a string or null")
