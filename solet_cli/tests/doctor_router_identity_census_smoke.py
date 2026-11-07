"""Offline smoke for router identity grading and informational child ports.

The recorded range describes the router's public listen port, which status does
not publish. Colour and streamable ports may be OS-assigned child listeners and
must not cause a warning. Readable active identity verifies; missing identity or
an unreadable status remains unknown. No real socket or router is used.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_router_identity_census import (  # noqa: E402
    collect_router_identity_advisories,
)

_ID = "doctor::router_identity_matches_record_v1"
_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Record:
    """A registry record carrying the router expectations create time records."""

    name = "census"
    target = "/nonexistent/census-target"
    expected_router_name = "census"
    expected_router_socket = "/nonexistent/census.router.sock"
    expected_router_port_range = "8800-8999"


def _status(*ports: int, active: str | None = "census-blue") -> dict[str, Any]:
    return {
        "router_started_at": 1.0,
        "active_color": "blue",
        "active_instance_id": active,
        "colors": [
            {
                "color": "blue",
                "port": port,
                "streamable_port": port + 100,
                "instance_id": active,
                "status": "live",
            }
            for port in ports
        ],
    }


def _advisory(record: object, payload: dict[str, Any] | Exception) -> dict[str, object]:
    def _reader(_socket_path: Path) -> dict[str, Any]:
        if isinstance(payload, Exception):
            raise payload
        return payload

    results = collect_router_identity_advisories(record, status_reader=_reader)
    _check(len(results) == 1, f"the census stopped emitting exactly one check: {len(results)}")
    entry = results[0]
    assert isinstance(entry, dict)
    _check(str(entry["check_id"]) == _ID, f"unexpected check id: {entry['check_id']}")
    return entry


def _assert_ports_in_range_are_green() -> None:
    advisory = _advisory(_Record(), _status(9101, 9102))
    _check(
        advisory["status"] == "verified",
        f"HEALTHY_COLOR_PORTS_OUTSIDE_BAND_ARE_INFORMATIONAL status={advisory['status']}",
    )
    _check(advisory["blocking"] is False, "a router identity advisory must never block")
    _check(
        advisory["expected"]["router_port_range"] == "8800-8999" and "status does not publish the public listen port" in advisory["summary"],
        "the public-port range must remain recorded and its status limitation must be stated",
    )
    observed = advisory["observed"]
    _check(
        isinstance(observed, dict) and observed["published_ports"] == [9101, 9102] and observed["streamable_ports"] == [9201, 9202],
        "colour and streamable ports must be reported as informational observations",
    )


def _assert_foreign_port_is_named() -> None:
    """An out-of-band child port remains visible without affecting identity grade."""

    advisory = _advisory(_Record(), _status(9101))
    _check(
        advisory["status"] == "verified",
        f"a readable active identity was not verified: {advisory['status']}",
    )
    observed = advisory["observed"]
    assert isinstance(observed, dict)
    _check(observed["published_ports"] == [9101], "the child port was not identified")


def _assert_one_foreign_among_valid_is_still_named() -> None:
    """Mixed child ports stay informational rather than producing a range warning."""

    advisory = _advisory(_Record(), _status(8801, 9101))
    _check(
        advisory["status"] == "verified",
        f"a mixed binding set affected identity grade: {advisory['status']}",
    )
    observed = advisory["observed"]
    assert isinstance(observed, dict)
    _check(observed["published_ports"] == [8801, 9101], "child ports were not identified")


def _assert_boundary_ports_are_inclusive() -> None:
    """Boundary child ports do not affect the active identity grade."""

    advisory = _advisory(_Record(), _status(8800, 8999))
    _check(
        advisory["status"] == "verified",
        f"the active identity was not verified: {advisory['reason_code']}",
    )


def _assert_no_colors_is_not_a_divergence() -> None:
    advisory = _advisory(_Record(), _status())
    _check(
        advisory["status"] == "verified",
        f"a readable active identity was not verified: {advisory['reason_code']}",
    )


def _assert_missing_active_identity_is_not_verified() -> None:
    advisory = _advisory(_Record(), _status(9101, active=None))
    _check(
        advisory["status"] == "unknown" and advisory["reason_code"] == "router_active_identity_missing",
        f"missing active identity must remain unknown: {advisory['status']} {advisory['reason_code']}",
    )


def _assert_unrecorded_expectation_is_unknown_not_green() -> None:
    """An older record cannot be graded, and ungradable is not passing."""

    class _Old:
        name = "census"
        target = "/nonexistent/census-target"
        expected_router_name = None
        expected_router_socket = None
        expected_router_port_range = None

    advisory = _advisory(_Old(), _status(8801))
    _check(
        advisory["status"] == "unknown",
        f"an unrecorded expectation did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "router_expectation_unrecorded",
        f"an unrecorded expectation was not named: {advisory['reason_code']}",
    )


def _assert_unparseable_range_does_not_block_identity() -> None:
    class _Bad:
        name = "census"
        target = "/nonexistent/census-target"
        expected_router_name = "census"
        expected_router_socket = "/nonexistent/census.router.sock"
        expected_router_port_range = "not-a-range"

    advisory = _advisory(_Bad(), _status(8801))
    _check(
        advisory["status"] == "verified",
        f"the unused public-port range affected identity grade: {advisory['status']}",
    )


def _assert_unreadable_socket_is_unknown_not_green() -> None:
    advisory = _advisory(_Record(), OSError("connection refused"))
    _check(
        advisory["status"] == "unknown",
        f"an unreadable socket did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "router_status_unreadable",
        f"an unreadable socket was not named: {advisory['reason_code']}",
    )


def main() -> int:
    _assert_ports_in_range_are_green()
    _assert_foreign_port_is_named()
    _assert_one_foreign_among_valid_is_still_named()
    _assert_boundary_ports_are_inclusive()
    _assert_no_colors_is_not_a_divergence()
    _assert_missing_active_identity_is_not_verified()
    _assert_unrecorded_expectation_is_unknown_not_green()
    _assert_unparseable_range_does_not_block_identity()
    _assert_unreadable_socket_is_unknown_not_green()
    print(f"doctor_router_identity_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
