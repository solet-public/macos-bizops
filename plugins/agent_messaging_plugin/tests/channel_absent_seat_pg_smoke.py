#!/usr/bin/env python3
"""Private-PostgreSQL measurement of a delivery to a CHANNEL-ABSENT seat.

The half of ``prb_351c9641`` this proves is the half that was never measured
(``iss_25ca0b87``): what a sender's receipt and the seat's own merged inbox
read actually say when the recipient is a real ``managed_session`` row with
host ``operator`` and no verified pane, so ``_resolve_driver_channel`` has no
driver channel to resolve and no wake mechanism exists at all.

Everything here is the production path. One private schema, one
``PostgresStatePlugin`` pool, the declarative schema lifecycle; the
``managed_session`` row is written by ``insert_managed_session`` and moved by
``transition_lifecycle_state``; the role is claimed through
``claim_role_binding_v4`` and resolved through ``resolve_role_binding_v4``;
the send is ``peer_dispatch.dispatch_role_send`` against a real
``PeerRegistry`` over that pool and a real ``BridgeSessionManager``; the inbox
is ``AgentMessagingService.peer_inbox``. There is no fake connection, no
stubbed ``DriveOnDeliveryOutcome``, no monkeypatched ``resolve_delivery_drive``
and no assertion on a log line -- the assertions read the RETURNED receipt and
the REAL inbox page.

Run only under the shipped provisioner
(``quality_gates/provision_test_pg.py --exec``); a missing fixture exits 77
(UNMEASURED), which is never a pass.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
for _source in (
    _ROOT / "ananta/src",
    _ROOT / "plugins/agent_messaging_plugin/src",
    _ROOT / "plugins/postgres_state_management_plugin/src",
    Path(__file__).resolve().parent,
):
    sys.path.insert(0, str(_source))

import postgres_state_management_plugin  # noqa: E402
import postgres_state_management_plugin.postgres_backend.store_factory  # noqa: E402,F401 # pyright: ignore[reportUnusedImport] -- imported for its side effect: it self-registers the "postgres" Store backend, exactly as the vault plugin import does in production
from ananta.core.config.config_manager import initialize_config  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.models import PeerInboxRequest, TextPart  # noqa: E402
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    get_agent_messaging_schema,
    get_agent_role_message_schema,
    get_role_covered_mark_schema,
    get_role_read_schema,
)
from ananta.llm.agent_messaging.service import AgentMessagingConfig, AgentMessagingService  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402
from postgres_state_management_plugin.plugin import PostgresStatePlugin  # noqa: E402

import agent_messaging_plugin  # noqa: E402
import ananta  # noqa: E402
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.delivery_drive import (  # noqa: E402
    finished_drive_outcome,
    get_delivery_pickup_schema,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import (  # noqa: E402
    DELIVERY_QUEUED_FOR_REPLAY,
    DELIVERY_QUEUED_WATCHER,
    dispatch_role_send,
)
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    HolderClaim,
    claim_role_binding_v4,
    resolve_role_binding_v4,
    upsert_role_entity,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    get_agent_role_binding_schema_definition,
    get_peer_binding_schema_definition,
    get_role_model_schema_definition,
    get_session_lifecycle_schema_definition,
)
from agent_messaging_plugin.session_hosts import OPERATOR_HOST  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from quality_gates.private_test_pg import (  # noqa: E402
    MissingFixtureError,
    fixture_vault,
    load_fixture,
    skip_fixture,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_DEFINITIONS = (
    get_session_lifecycle_schema_definition,
    get_agent_role_binding_schema_definition,
    get_role_model_schema_definition,
    get_peer_binding_schema_definition,
    get_delivery_pickup_schema,
    get_agent_messaging_schema,
    get_agent_role_message_schema,
    get_role_read_schema,
    get_role_covered_mark_schema,
)


class Fixture:
    """One production-wired state service over one private fixture database.

    Built from the shipped ``quality_gates.private_test_pg`` helpers so every
    endpoint, ownership and postmaster premise is validated by the harness
    rather than re-asserted here, then started through the PUBLIC plugin
    lifecycle (``prepare_for_readiness`` + ``start_services``) with the real
    :class:`SchemaManager` installing the real declarations.
    """

    def __init__(self) -> None:
        self.plugin: PostgresStatePlugin | None = None
        self.state: StateService | None = None
        self._started = False
        self._previous_solet = os.environ.get("SOLET_NAME")

    def open(self) -> None:
        config = load_fixture("messaging_write")
        for module in (ananta, agent_messaging_plugin, postgres_state_management_plugin):
            _check(Path(str(module.__file__)).resolve().is_relative_to(_ROOT), "lane_source_provenance_" + module.__name__)
        app_home = config.root / ("app-channel-absent-" + uuid4().hex[:12])
        app_home.mkdir(mode=0o700)
        identity = "private_fixture_" + config.nonce[:12]
        os.environ["SOLET_NAME"] = identity
        manager = initialize_config(str(app_home))
        connection = dict(config.connection_config("channel_absent_seat"))
        if not manager.save_plugin_config("postgres_state_management_plugin", connection):
            raise RuntimeError("could not save the private fixture configuration")
        plugin = PostgresStatePlugin()
        self.plugin = plugin
        plugin.set_vault_service(fixture_vault(f"{identity}.postgres_state_management_plugin.db_password", []))
        plugin.prepare_for_readiness()
        started = asyncio.run(plugin.start_services())
        if started.get("action_status") != "completed":
            raise RuntimeError(f"private plugin startup failed: {started!r}")
        self._started = True
        state = StateService(_PluginManager(plugin), app_home=str(app_home), state_plugin_name=plugin.name)
        schemas = SchemaManager(state, plugin)
        schemas.initialize_schemas([factory() for factory in _DEFINITIONS])
        state.set_schema_manager(schemas)
        self.state = state

    def close(self) -> None:
        if self._previous_solet is None:
            os.environ.pop("SOLET_NAME", None)
        else:
            os.environ["SOLET_NAME"] = self._previous_solet
        if self._started and self.plugin is not None:
            stopped = asyncio.run(self.plugin.stop_services())
            if stopped.get("action_status") != "completed":
                raise RuntimeError(f"private plugin stop failed: {stopped!r}")


class _PluginManager:
    """The one lookup ``StateService`` makes; never dispatches app actions."""

    def __init__(self, plugin: object) -> None:
        self.plugin = plugin

    def get_plugin(self, plugin_name: str) -> object:
        if plugin_name != "postgres_state_management_plugin":
            raise AssertionError("unexpected fixture plugin lookup")
        return self.plugin

    def execute_action(
        self,
        plugin_name: str,
        params: dict[str, object],
        state: dict[str, object],
        APP_HOME: str,  # noqa: N803 -- the protocol's own parameter name
        plugin_config: dict[str, object],
    ) -> dict[str, object]:
        raise AssertionError("fixture must not dispatch application actions")


_ROLE = "Channel-Absent-Seat"
_SENDER_INSTANCE = "agi-" + uuid4().hex
_SENDER_BRIDGE = "br-" + uuid4().hex[:12]


def _seat_row(state: StateManagementInterface, instance: str, session: str) -> dict[str, Any]:
    """A REAL host='operator' managed_session row with NO host_ref, moved live."""
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=instance,
            lane_id="channel-absent-seat",
            assignment=None,
            work_class="operator_seat",
            budget_line="operator",
            host=OPERATOR_HOST,
            provisioning_mode="operator_existing_checkout",
            agent_runtime="claude_code",
            role_name=_ROLE,
            spawned_by_role="operator",
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id=instance,
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="channel-absent-seat-smoke",
        reason="operator-launched seat registered itself",
        recorded_fields={"agent_session_id": session},
    )
    return read_managed_session(state, instance)


def _claim_seat_role(state: StateManagementInterface, instance: str, session: str) -> Any:
    upsert_role_entity(state, name=_ROLE, description="operator seat with no pane")
    claim_role_binding_v4(
        state,
        name=_ROLE,
        claim=HolderClaim(
            holder_kind="session",
            holder_identity={"agent_id": "claude_code", "session_label": _ROLE},
            agent_instance_id=instance,
            agent_session_id=session,
            session_label=_ROLE,
        ),
    )
    return resolve_role_binding_v4(state, name=_ROLE)


def _manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda solet: f"sess-{solet}-{uuid4().hex[:8]}",
        idle_timeout_s=300,
        max_pending_events=64,
        long_poll_timeout_s=5,
    )


def _send(
    *,
    state: StateService,
    service: AgentMessagingService,
    manager: BridgeSessionManager,
    registry: PeerRegistry,
    role: Any,
    message_id: str,
) -> Any:
    return dispatch_role_send(
        bridge_manager=manager,
        peer_registry=registry,
        agent_messaging_service=service,
        state_service=cast(StateManagementInterface, state),
        role_name=_ROLE,
        role=role,
        sender_bridge_id=_SENDER_BRIDGE,
        sender_agent_id="claude_code",
        sender_agent_instance_id=_SENDER_INSTANCE,
        sender_session_label="Measuring-Sender",
        sender_parent_pid=None,
        content=[TextPart(type="text", text="IMPORTANT: measured send to a seat with no driver channel.")],
        message_id=message_id,
    )


def _inbox_ids(service: AgentMessagingService, instance: str, session: str) -> list[str]:
    """The message ids the SEAT'S OWN merged inbox read returns for itself.

    A regular (non-observer) page is the uptake-relevant read: it suppresses
    acknowledged and covered rows, so a message still listed here is one the
    seat has not taken up.
    """
    page = service.peer_inbox(
        PeerInboxRequest(
            recipient_agent_id="claude_code",
            recipient_agent_instance_id=instance,
            recipient_agent_session_id=session,
            limit=10,
        ),
    )
    print(f"        reach: role_entries={len(page.role_entries)} direct={len(page.entries)} coverage={page.role_read_coverage!r}")
    return [str(entry.message.id) for entry in page.role_entries]


def _owed_ids(service: AgentMessagingService, instance: str) -> list[str]:
    """The ids the production repair-drain reader still considers OWED.

    ``list_undelivered_for_instance`` is the exact predicate Control #5 and
    ``POST /peer/drain`` use: un-consumed, within the re-emit window and cap,
    fenced to the roles this instance holds. A message listed here is
    provably ``delivered=false`` and awaiting uptake, not merely rendered.
    """
    rows = service.list_undelivered_for_instance(agent_instance_id=instance, limit=10)
    print(f"        reach: owed rows={[(r.get('message_id'), r.get('emit_count'), r.get('consumed')) for r in rows]}")
    return [str(row.get("message_id") or "") for row in rows]


def measure_channel_absent_seat(fixture: Fixture) -> None:
    """(a)(b)(c) for a channel-less seat whose bridge binding IS live."""
    state = cast(StateService, fixture.state)
    service = AgentMessagingService(
        repository=AgentMessagingRepository(cast(StateManagementInterface, state)),
        state_service=cast(StateManagementInterface, state),
        config=AgentMessagingConfig(),
    )
    manager = _manager()
    registry = PeerRegistry(state_service=state)
    instance = "agi-" + uuid4().hex
    bridge = manager.open("channel-absent-fixture")
    session = bridge.session_id
    row = _seat_row(cast(StateManagementInterface, state), instance, session)
    _check(
        str(row["host"]) == OPERATOR_HOST and not str(row.get("host_ref") or "") and str(row["lifecycle_state"]) == LIFECYCLE_LIVE,
        "real_operator_row_is_live_with_no_host_ref",
    )
    registry.register(
        BridgeBinding(
            bridge_id=bridge.bridge_id,
            agent_id="claude_code",
            agent_instance_id=instance,
            session_label=_ROLE,
            parent_pid=None,
            agent_session_id=session,
            watcher_declared=True,
        ),
    )
    bridge.touch()
    role = _claim_seat_role(cast(StateManagementInterface, state), instance, session)
    _check(role.agent_instance_id == instance and role.agent_session_id == session, "role_resolves_to_the_channel_less_seat")
    message_id = "arm-" + uuid4().hex
    outcome = _send(state=state, service=service, manager=manager, registry=registry, role=role, message_id=message_id)
    payload = outcome.to_payload()
    print(f"        reach: delivery={outcome.delivery} drive_on_delivery={outcome.drive_on_delivery} send_status={payload.get('send_status')!r}")
    print(f"        reach: sender_notice={payload.get('sender_notice')!r}")
    _check(outcome.drive_on_delivery == "driver_unavailable", "a_receipt_carries_the_channel_absent_outcome")
    _check(outcome.drive_on_delivery != "driver_sent" and "driver_sent" not in str(payload), "a_receipt_never_claims_driver_sent")
    _check(outcome.delivery == DELIVERY_QUEUED_WATCHER, "a_delivery_label_is_the_no_wake_watcher_queue")
    _check(payload.get("send_status") == "stored_no_wake", "a_receipt_says_stored_no_wake")
    _check("will not be woken" in str(payload.get("sender_notice") or ""), "a_sender_notice_states_no_turn_was_scheduled")
    _check(payload.get("message_id") == message_id, "c_sender_holds_the_message_id_it_can_read_back_by")
    _check(message_id in _inbox_ids(service, instance, session), "b_seats_own_merged_inbox_read_returns_the_message")
    _check(message_id in _owed_ids(service, instance), "b_message_is_still_owed_so_it_is_awaiting_uptake")
    finished = finished_drive_outcome(message_id, timeout_s=0.5)
    print(f"        reach: finished_drive_outcome={finished!r}")
    _check(finished is not None, "c_finished_drive_outcome_is_readable_by_message_id")
    _check(finished is not None and finished[0] == "driver_unavailable", "c_finished_drive_outcome_holds_the_channel_absent_outcome")


def measure_unreachable_seat(fixture: Fixture) -> None:
    """(c) the send still succeeds and the row persists with no binding at all."""
    state = cast(StateService, fixture.state)
    service = AgentMessagingService(
        repository=AgentMessagingRepository(cast(StateManagementInterface, state)),
        state_service=cast(StateManagementInterface, state),
        config=AgentMessagingConfig(),
    )
    manager = _manager()
    registry = PeerRegistry(state_service=state)
    instance = "agi-" + uuid4().hex
    session = "sess-unbound-" + uuid4().hex[:8]
    _seat_row(cast(StateManagementInterface, state), instance, session)
    role = _claim_seat_role(cast(StateManagementInterface, state), instance, session)
    message_id = "arm-" + uuid4().hex
    outcome = _send(state=state, service=service, manager=manager, registry=registry, role=role, message_id=message_id)
    payload = outcome.to_payload()
    print(f"        reach: delivery={outcome.delivery} drive_on_delivery={outcome.drive_on_delivery} send_status={payload.get('send_status')!r}")
    _check(outcome.delivery == DELIVERY_QUEUED_FOR_REPLAY, "c_no_binding_is_queued_for_replay_not_a_failure")
    _check(outcome.drive_on_delivery == "driver_unavailable", "c_no_binding_receipt_carries_channel_absent_outcome")
    _check(payload.get("send_status") == "stored_no_wake", "c_no_binding_receipt_says_stored_no_wake")
    _check(message_id in _inbox_ids(service, instance, session), "c_no_message_is_dropped_the_row_persists")
    _check(message_id in _owed_ids(service, instance), "c_no_binding_row_is_still_owed_by_the_replay_path")


def main() -> int:
    fixture = Fixture()
    try:
        fixture.open()
    except MissingFixtureError:
        return skip_fixture()
    try:
        print("  channel-absent seat with a live watcher binding")
        measure_channel_absent_seat(fixture)
        print("  channel-absent seat with no binding at all")
        measure_unreachable_seat(fixture)
    finally:
        fixture.close()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
