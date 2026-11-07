"""Passively verify that readable router status publishes an active identity.

The recorded router port range describes the public listen port, which status
does not publish. Colour and streamable ports are child listeners and remain
informational here; this advisory grades identity only and never blocks.
"""

from __future__ import annotations

import json
import socket as socket_mod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .doctor_advisory_result import advisory_unknown, advisory_verified
from .models import InstanceRecord, JsonValue

_IDENTITY_CHECK_ID = "doctor::router_identity_matches_record_v1"
_STATUS_TIMEOUT_S = 2.0
StatusReader = Callable[[Path], dict[str, Any]]


def collect_router_identity_advisories(
    record: InstanceRecord,
    *,
    status_reader: StatusReader | None = None,
) -> list[JsonValue]:
    """Return the report-only check comparing the live router to the record."""

    reader = _read_router_status if status_reader is None else status_reader
    return [_router_identity_advisory(record, reader)]


def _read_router_status(socket_path: Path) -> dict[str, Any]:
    """Ask the router management socket for its status payload."""

    with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as sock:
        sock.settimeout(_STATUS_TIMEOUT_S)
        sock.connect(str(socket_path))
        sock.sendall(json.dumps({"verb": "status", "args": {}}).encode() + b"\n")
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
    value = json.loads(bytes(buf).decode())
    if not isinstance(value, dict):
        raise ValueError(f"router status is {type(value).__name__}, not an object")
    return value


def _ports(status: dict[str, Any], key: str) -> list[JsonValue]:
    """Return the requested listener ports reported for colour bindings."""

    colors = status.get("colors")
    if not isinstance(colors, list):
        return []
    ports: list[JsonValue] = []
    for entry in colors:
        if isinstance(entry, dict) and isinstance(entry.get(key), int):
            ports.append(entry[key])
    return ports


def _router_identity_advisory(record: InstanceRecord, reader: StatusReader) -> dict[str, JsonValue]:
    expected: dict[str, JsonValue] = {
        "router_socket": record.expected_router_socket,
        "router_port_range": record.expected_router_port_range,
        "router_name": record.expected_router_name,
    }
    observed: dict[str, JsonValue] = {
        "published_ports": [],
        "streamable_ports": [],
        "active_instance_id": None,
    }
    source = str(record.expected_router_socket)

    if record.expected_router_socket is None or record.expected_router_port_range is None:
        return advisory_unknown(
            _IDENTITY_CHECK_ID,
            "This record carries no router expectation, so the live router cannot be compared.",
            expected,
            observed,
            source,
            "router_expectation_unrecorded",
            "The record predates expected_router_socket/expected_router_port_range; an ungradable record is not a passing one.",
        )

    try:
        status = reader(Path(record.expected_router_socket))
    except (OSError, ValueError) as exc:
        return advisory_unknown(
            _IDENTITY_CHECK_ID,
            "The router management socket could not be read, so its identity is unknown.",
            expected,
            observed,
            source,
            "router_status_unreadable",
            str(exc),
        )

    active_instance = status.get("active_instance_id")
    observed["active_instance_id"] = active_instance if isinstance(active_instance, str) else None
    return _grade_published_ports(status, expected, observed, source)


def _grade_published_ports(
    status: dict[str, Any],
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
) -> dict[str, JsonValue]:
    """Grade active identity only; child listener ports are informational."""

    observed["published_ports"] = _ports(status, "port")
    observed["streamable_ports"] = _ports(status, "streamable_port")
    active_instance = observed["active_instance_id"]
    if not isinstance(active_instance, str) or not active_instance:
        return advisory_unknown(
            _IDENTITY_CHECK_ID,
            "The router status is readable but does not publish an active instance identity.",
            expected,
            observed,
            source,
            "router_active_identity_missing",
            "Colour and streamable ports are informational; this status cannot verify identity.",
        )

    return advisory_verified(
        _IDENTITY_CHECK_ID,
        "The router status publishes an active instance identity. Colour and streamable ports are informational; status does not publish the public listen port, so the recorded router port range is not graded here.",
        expected,
        observed,
        source,
    )
