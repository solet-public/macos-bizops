#!/usr/bin/env python3
"""Deterministic smoke for the operator-hosted seat wake advice (prb_351c9641).

Re-measured on 2026-11-04: an operator-hosted ``managed_session`` row (host
``operator``, null ``host_ref``) still has no driver channel, so ``clear``,
``compact`` and ``drive`` raise ``unsupported_on_host`` and the delivery lookup
reports ``driver_unavailable``. That stays by design -- an attended seat is
never typed into automatically -- but both texts used to say only "use the
manual equivalent" without naming it. This smoke pins that they now name it.

Failing path (operator row, no channel): the refusal text and the
lookup-recorded detail both carry the manual wake instruction, the error code
and the outcome are unchanged, and the lookup records but never refuses.
Passing path (a driver that does return a channel): the channel is resolved
and no advice or error appears.

No database, no subprocess, no live session. Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/operator_seat_wake_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

# session_lifecycle_verbs must load before the control module: the two import
# each other, and only the verbs-first order completes.
from agent_messaging_plugin import session_lifecycle_verbs  # noqa: E402

# isort: split
from agent_messaging_plugin import session_lifecycle_control  # noqa: E402
from agent_messaging_plugin.delivery_drive import (  # noqa: E402
    OPERATOR_SEAT_MANUAL_WAKE,
    _record_lookup_outcome,  # pyright: ignore[reportPrivateUsage]
    finished_drive_outcome,
)

_passed = 0
_failed: list[str] = []

_MANUAL_WAKE_TOKEN = "drain peer_inbox"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeChannel:
    """A minimal DriverChannel: only identity matters to this smoke."""

    def insert(self, text: str) -> None:
        del text

    def submit(self) -> None:
        return None

    def send(self, text: str) -> None:
        del text


class _ChannelDriver:
    def driver_channel(self, host_ref: str) -> _FakeChannel | None:
        del host_ref
        return _CHANNEL


_CHANNEL = _FakeChannel()


def _operator_row() -> dict[str, Any]:
    return {
        "agent_instance_id": "agi-operator-seat",
        "host": "operator",
        "host_ref": None,
        "agent_runtime": "claude_code",
        "lifecycle_state": "live",
    }


def _check_operator_refusal_names_manual_wake() -> None:
    verb_error: session_lifecycle_verbs.VerbError | None = None
    try:
        session_lifecycle_control._resolve_driver_channel(_operator_row())  # pyright: ignore[reportPrivateUsage]
    except session_lifecycle_verbs.VerbError as exc:
        verb_error = exc
    _check(verb_error is not None, "an operator row still has no driver channel (unsupported_on_host is raised)")
    if verb_error is None:
        return
    text = str(verb_error)
    _check("unsupported_on_host" in text, "the error code is unchanged: unsupported_on_host")
    _check(_MANUAL_WAKE_TOKEN in text, "the refusal names the manual wake instruction")
    _check(OPERATOR_SEAT_MANUAL_WAKE in text, "the refusal carries the shared manual wake text verbatim")


def _check_lookup_detail_names_manual_wake() -> None:
    returned = _record_lookup_outcome("msg-operator-seat-1", "agi-operator-seat", "driver_unavailable")
    _check(returned == "driver_unavailable", "the lookup still returns driver_unavailable (a report, not a gate)")
    finished = finished_drive_outcome("msg-operator-seat-1", timeout_s=0.0)
    _check(finished is not None, "the lookup outcome is readable by message id")
    if finished is None:
        return
    outcome, recorded_detail = finished
    detail = recorded_detail or ""
    _check(outcome == "driver_unavailable", "the finished outcome is driver_unavailable")
    _check(_MANUAL_WAKE_TOKEN in detail, "the recorded detail names the manual wake instruction")
    _check("durable mail is retained" in detail, "the detail still says the durable mail is retained")


def _check_channel_present_resolves_without_advice() -> None:
    row = _operator_row() | {"host": "tmux", "host_ref": "%1"}
    original = session_lifecycle_control.resolve_host_driver
    session_lifecycle_control.resolve_host_driver = lambda host, runtime=None: (_ChannelDriver(), host)  # type: ignore[assignment]
    try:
        resolved = session_lifecycle_control._resolve_driver_channel(row)  # pyright: ignore[reportPrivateUsage]
    except session_lifecycle_verbs.VerbError as exc:
        _check(False, f"a host with a channel resolves it (raised {exc})")
        return
    finally:
        session_lifecycle_control.resolve_host_driver = original  # type: ignore[assignment]
    _check(resolved is _CHANNEL, "a host that returns a channel resolves it with no error and no advice")


def main() -> int:
    _check_operator_refusal_names_manual_wake()
    _check_lookup_detail_names_manual_wake()
    _check_channel_present_resolves_without_advice()
    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
