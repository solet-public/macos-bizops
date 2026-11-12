"""Adapt the address service into immutable resolver inputs, without state access."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ananta.interfaces.address_book_service_interface import AddressBookServiceInterface

from .containment import Located
from .resolution import PathAddress


def _tag_values(tags: list[str], prefix: str) -> frozenset[str]:
    values: set[str] = set()
    for raw in tags:
        tag, separator, value = raw.strip().lower().partition(":")
        if separator and tag == prefix and value.strip():
            values.add(value.strip())
    return frozenset(values)


def _path_entry(entries: list[dict[str, Any]]) -> Path:
    paths = [entry for entry in entries if entry.get("field_type") == "path"]
    if len(paths) != 1:
        raise ValueError("a location needs exactly one path entry")
    directory = Path(paths[0]["value"])
    if not directory.is_absolute():
        raise ValueError("a location needs an absolute directory")
    return directory


def _parse_address(data: dict[str, Any]) -> PathAddress:
    directory = _path_entry(data["entries"])
    tags = data.get("tags", [])
    timestamp = data["updated_at"]
    updated = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(timestamp)
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return PathAddress(data["name"], directory, _tag_values(tags, "project"), _tag_values(tags, "file_kind"), updated)


def _rename_advisories(data: dict[str, Any], address: PathAddress) -> tuple[str, ...]:
    return tuple(f"Old export folder {address.directory} was registered as {address.name!r} because the name {tag.removeprefix('renamed_from:')!r} was already in use." for tag in data.get("tags", []) if isinstance(tag, str) and tag.startswith("renamed_from:"))


def path_addresses(address_book_service: AddressBookServiceInterface) -> Located[tuple[PathAddress, ...]]:
    """Read all location addresses and retain malformed-record advisories."""
    result = address_book_service.list_path_addresses()
    if result.get("action_status") != "completed":
        return Located((), (f"Path addresses unavailable: {result.get('error')}; resolver will use its next rung.",))
    addresses: list[PathAddress] = []
    advisories: list[str] = []
    data = cast(dict[str, Any], result.get("data") or {})
    rows = cast(list[Any], data.get("addresses", []))
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        try:
            if not isinstance(name, str) or not name:
                raise ValueError("address has no name")
            address = _parse_address(row)
            addresses.append(address)
            advisories.extend(_rename_advisories(row, address))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            advisories.append(f"Skipping malformed path address {name!r}: {exc}")
    return Located(tuple(addresses), tuple(advisories))
