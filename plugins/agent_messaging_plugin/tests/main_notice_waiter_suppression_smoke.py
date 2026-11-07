#!/usr/bin/env python3
"""Main notice appends stay durable without waking its local waiter."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest import TestCase
from unittest.mock import patch

import session_sweep_smoke as fixture_smoke
from session_sweep_smoke import (
    _bind_notice_main,
    _check,
    _gauge,
    _NoticeChannel,
    _register_live_binding,
    _ticking,
    _wired,
)

from agent_messaging_plugin.gauge_notice_record_store import read_gauge_notice_records
from agent_messaging_plugin.schema import NOTICE_DELIVERY_APPENDED
from agent_messaging_plugin.session_sweep import _notify_rotation_due, sweep_gauge_staleness

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from agent_messaging_plugin.bridge_sessions import BridgeSessionManager
    from agent_messaging_plugin.peer_registry import PeerRegistry


def _uncertain_main(
    mode: str,
) -> tuple[StateManagementInterface, PeerRegistry, BridgeSessionManager, str]:
    state, registry, manager, bridge_id = _wired("ases-steward")
    if mode != "vacant":
        _bind_notice_main(
            state,
            "agi-other" if mode == "stale" else "agi-steward",
            "ases-old" if mode == "stale" else "ases-steward",
        )
    if mode == "ambiguous":
        _register_live_binding(registry, manager, agent_instance_id="agi-duplicate", agent_session_id="ases-steward")
    return state, registry, manager, bridge_id


@patch.dict(os.environ, {"SOLET_NAME": "fixture"})
def test_main_rotation_stores_without_driver_and_binding_reversal_restores_it() -> None:
    state, registry, manager, bridge_id = _wired("ases-steward")
    _bind_notice_main(state, "agi-steward", "ases-steward")
    channel = _NoticeChannel()
    with patch("agent_messaging_plugin.session_lifecycle_control._resolve_driver_channel", return_value=channel):
        with patch.object(manager, "_signal_wakeup", wraps=manager._signal_wakeup) as signal:
            delivered = _notify_rotation_due(
                state=state,
                peer_registry=registry,
                bridge_manager=manager,
                row={"current_tokens": 900_000},
                agent_instance_id="agi-worker",
                spawner_instance_id="agi-steward",
            )
            _check(signal.call_count == 0, "confirmed Main rotation does not wake its local waiter")
        _check(not channel.sent, "confirmed Main rotation has no native driver send")
        _, events = manager.get(bridge_id).events_after(-1)
        _check((delivered, [event.event_type for event in events]) == (True, ["rotation_due_notice"]), "Main rotation event remains readable")
        other_bridge = _register_live_binding(
            registry,
            manager,
            agent_instance_id="agi-other",
            agent_session_id="ases-other",
        )
        _bind_notice_main(state, "agi-other", "ases-other")
        with patch.object(manager, "_signal_wakeup", wraps=manager._signal_wakeup) as later_signal:
            _notify_rotation_due(
                state=state,
                peer_registry=registry,
                bridge_manager=manager,
                row={"current_tokens": 900_000},
                agent_instance_id="agi-worker",
                spawner_instance_id="agi-steward",
            )
            _check(later_signal.call_count == 1, "binding reversal restores the non-Main waiter signal")
        _, later = manager.get(bridge_id).events_after(-1)
        _check(channel.sent == ["delivery waiting from rotation_due_notice — drain peer_inbox"], "binding reversal restores the existing non-Main rotation driver")
        _check((len(later), later[0], manager.get(other_bridge).events_after(-1)[1]) == (2, events[0], []), "binding reversal preserves prior events and recipient")


@patch.dict(os.environ, {"SOLET_NAME": "fixture"})
def test_gauge_notice_main_stores_without_local_wake_and_non_main_preserves_route() -> None:
    for is_main in (True, False):
        state, registry, manager, bridge_id = _wired("ases-steward")
        if is_main:
            _bind_notice_main(state, "agi-steward", "ases-steward")
        now = datetime.now(UTC)
        _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
        _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
        channel = _NoticeChannel()
        with patch("agent_messaging_plugin.session_lifecycle_control._resolve_driver_channel", return_value=channel):
            with patch.object(manager, "_signal_wakeup", wraps=manager._signal_wakeup) as signal:
                delivered = sweep_gauge_staleness(
                    state,
                    now=now,
                    peer_registry=registry,
                    bridge_manager=manager,
                )
        _, events = manager.get(bridge_id).events_after(-1)
        records, truncated = read_gauge_notice_records(state, notice_type="gauge_stale_notice")
        _check((delivered, [event.event_type for event in events]) == (1, ["gauge_stale_notice"]), f"Main={is_main}: gauge event remains readable")
        _check((len(records), truncated, records[0]["delivery_outcome"]) == (1, False, NOTICE_DELIVERY_APPENDED), f"Main={is_main}: gauge delivery record remains")
        _check(signal.call_count == (0 if is_main else 1), f"Main={is_main}: local wake follows the confirmed recipient binding")
        _check(not channel.sent, f"Main={is_main}: gauge notice has no native driver send")


@patch.dict(os.environ, {"SOLET_NAME": "fixture"})
def test_uncertain_gauge_main_bindings_advise_and_keep_wake() -> None:
    for mode in ("vacant", "stale", "ambiguous"):
        state, registry, manager, bridge_id = _uncertain_main(mode)
        now = datetime.now(UTC)
        _gauge(state, "agi-worker", measured_at=(now - timedelta(seconds=5400)).isoformat())
        _ticking(state, "agi-worker", last_alive=now - timedelta(seconds=30))
        with patch.object(manager, "_signal_wakeup", wraps=manager._signal_wakeup) as signal:
            with TestCase().assertLogs("agent_messaging_plugin.session_sweep", level="WARNING") as logs:
                delivered = sweep_gauge_staleness(
                    state,
                    now=now,
                    peer_registry=registry,
                    bridge_manager=manager,
                )
        _, events = manager.get(bridge_id).events_after(-1)
        records, truncated = read_gauge_notice_records(state, notice_type="gauge_stale_notice")
        _check((delivered, [event.event_type for event in events], signal.call_count) == (1, ["gauge_stale_notice"], 1), f"{mode} Main binding keeps the gauge event and wake")
        _check((len(records), truncated, records[0]["delivery_outcome"]) == (1, False, NOTICE_DELIVERY_APPENDED), f"{mode} Main binding keeps the delivery record")
        _check(any("ADVISORY" in line for line in logs.output), f"{mode} Main binding emits visible ADVISORY")


def main() -> int:
    test_main_rotation_stores_without_driver_and_binding_reversal_restores_it()
    test_gauge_notice_main_stores_without_local_wake_and_non_main_preserves_route()
    test_uncertain_gauge_main_bindings_advise_and_keep_wake()
    print(f"PASSED: {fixture_smoke._passed}")
    print(f"FAILED: {len(fixture_smoke._failed)}")
    for label in fixture_smoke._failed:
        print(f"  - {label}")
    return 1 if fixture_smoke._failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
