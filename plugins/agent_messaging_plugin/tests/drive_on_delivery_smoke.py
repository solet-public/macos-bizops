#!/usr/bin/env python3
"""Unit smoke for the drive-on-delivery lane (2026-08-04), slice 1 —
``session_lifecycle_verbs.drive_on_delivery`` as exercised through the real
``dispatch_peer_send`` seam (``peer_dispatch.py``).

Against a trivial FAKE ``HostDriver``/``DriverChannel`` pair, monkeypatched
into ``session_hosts._REGISTRY`` under a test-only host name — same isolation
pattern ``session_lifecycle_verbs_smoke.py`` uses for
``clear_session``/``compact_session``/``drive_session``, so this smoke proves
the NEW seam's own logic, not the real ``tmux``/``headless`` driver's
subprocess machinery.

Covers the brief's slice-1 legs (a)-(d) plus (e), a regression guard added
during seat sign-off (position 2: drive-on-delivery must never re-arm
``report_by`` — that stays ``drive_session``'s own edge), and the watcher
identity reconciliation added for managed Codex delivery:

  (a) an eligible managed recipient's channel receives the notice, ALONGSIDE
      the existing notify (never instead of it — ruling 1 on the append_event
      question).
  (b) a PARKED managed recipient's channel receives NOTHING — CORRECTED
      2026-08-04 sign-off: ``_resolve_driver_channel`` performs NO lifecycle
      check of its own (a parked row's channel is perfectly live), so this
      must red against a mutant that deletes drive_on_delivery's OWN explicit
      eligibility gate, not against ``unsupported_on_host`` or any other
      guard. Also covers SPAWNING (legal for drive_session, illegal here).
  (c) a raising driver channel never fails the outer send.
  (d) a non-managed recipient (no managed_session row at all) is byte-
      identical to pre-lane behaviour — no driver lookup even attempted.
  (e) report_by is unchanged after a drive-on-delivery notice.
  (f) a watcher registration resolves its managed spawn row by exact stable
      ``agent_session_id``; ambiguous stable ids are never guessed.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/drive_on_delivery_smoke.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import current_thread
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT = REPO_ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.models import RoleMessagePersisted, TextPart  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from codex_eventual_pickup_smoke import _State  # noqa: E402 -- production-shaped transaction fixture

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.bridge_sessions import (  # noqa: E402
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
)
from agent_messaging_plugin.channel_support import channel_carries_message  # noqa: E402
from agent_messaging_plugin.delivery_drive import (  # noqa: E402
    CodexPickupStore,
    drive_queue_snapshot,
    finished_drive_outcome,
)
from agent_messaging_plugin.local_cli.spool import (  # noqa: E402
    default_spool_path,
    spool_offset_path,
    watch_instance_digest,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import (  # noqa: E402
    DELIVERY_QUEUED_NOTIFICATION,
    DELIVERY_QUEUED_WAKE,
    DELIVERY_QUEUED_WATCHER,
    dispatch_peer_send,
    dispatch_role_send,
)
from agent_messaging_plugin.peer_registry import PeerUnreachableError  # noqa: E402
from agent_messaging_plugin.peer_role_management import ResolvedRole  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_DEADLINE,
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_SPAWNING,
)
from agent_messaging_plugin.session_lifecycle_control import (  # noqa: E402
    _fire_session_terminal_dependencies,  # noqa: PLC2701 -- the terminal-edge fire is a private helper; its notice is the behaviour under test
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    backfill_registration,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    ArmSessionDependencyRequest,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    arm_session_dependency as verb_arm_session_dependency,
)
from agent_messaging_plugin.session_sweep import (  # noqa: E402
    EVENT_SESSION_DEPENDENCY_WAKE,
    sweep_deadline_dependencies,
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


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", _State())


_TEST_HOST = "test-drive-on-delivery-host"
_RECIPIENT_AGI = "agi-recipient"


class _FakeChannel:
    def __init__(self, *, raise_on_send: bool = False) -> None:
        self.sent: list[str] = []
        self._raise_on_send = raise_on_send

    def send(self, text: str) -> None:
        if self._raise_on_send:
            raise RuntimeError("driver channel exploded")
        self.sent.append(text)


class _FakeParkInterruptingChannel(_FakeChannel):
    def __init__(self) -> None:
        super().__init__()
        self.interrupts = 0

    def interrupt_park(self) -> str:
        self.interrupts += 1
        return "interrupted_park"


class _FakeDriverWithChannel:
    def __init__(
        self,
        *,
        raise_on_send: bool = False,
        park_interrupt: bool = False,
        raise_on_delivery_drive_thread: bool = False,
    ) -> None:
        self.channel = _FakeParkInterruptingChannel() if park_interrupt else _FakeChannel(raise_on_send=raise_on_send)
        self._raise_on_delivery_drive_thread = raise_on_delivery_drive_thread

    def spawn(self, spec: object) -> str:
        del spec
        return "fake-host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _FakeChannel:
        del host_ref
        if self._raise_on_delivery_drive_thread and current_thread().name.startswith("peer-delivery-drive"):
            raise RuntimeError("delivery-drive executor channel lookup exploded")
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _install_fake_host(
    *,
    raise_on_send: bool = False,
    park_interrupt: bool = False,
    raise_on_delivery_drive_thread: bool = False,
) -> _FakeDriverWithChannel:
    driver = _FakeDriverWithChannel(
        raise_on_send=raise_on_send,
        park_interrupt=park_interrupt,
        raise_on_delivery_drive_thread=raise_on_delivery_drive_thread,
    )
    session_hosts._REGISTRY[_TEST_HOST] = driver  # noqa: SLF001 -- test-only monkeypatch  # pyright: ignore[reportArgumentType]
    return driver


def _remove_fake_host() -> None:
    session_hosts._REGISTRY.pop(_TEST_HOST, None)  # noqa: SLF001


class _FakeMessagingService:
    """Minimal peer_send stand-in — dispatch_peer_send's own persist/notify
    logic is not under test here, only the drive-on-delivery seam riding
    after it."""

    def peer_send(self, request: Any) -> Any:  # noqa: ANN401, ARG002
        class _Result:
            thread_id = "agt-dod"
            message_id = "agm-dod"
            cursor = 1

        return _Result()


def _binding(
    *,
    agent_instance_id: str = _RECIPIENT_AGI,
    agent_session_id: str = "",
) -> BridgeBinding:
    return BridgeBinding(
        bridge_id="agc-recipient",
        agent_id="claude_code",
        agent_instance_id=agent_instance_id,
        session_label="Recipient",
        parent_pid=4242,
        agent_session_id=agent_session_id,
    )


class _FakePeerRegistry:
    """No native adapter registered — forces the append_event (channel-event)
    notify branch, orthogonal to the drive-on-delivery seam under test."""

    def __init__(self, binding: BridgeBinding | None = None) -> None:
        self._binding = binding or _binding()

    def resolve(self, agent_id: str, agent_instance_id: str | None) -> BridgeBinding:
        del agent_id, agent_instance_id
        return self._binding

    def wake_adapter_for(self, agent_id: str) -> None:
        del agent_id
        return None

    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        del agent_instance_id
        return ""

    def touch_binding(self, agent_instance_id: str) -> None:
        del agent_instance_id


class _FakeWakeAdapter:
    """The native wake adapter a live Claude bridge registers: ``wake`` queues
    the channel event on the recipient's bridge and returns that bridge's id."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def wake(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "agc-recipient"


class _AdapterPeerRegistry(_FakePeerRegistry):
    """A registry with a native wake adapter registered, so a send to a
    non-watcher recipient reports ``queued_wake`` (the existing fakes return
    no adapter and stay on ``queued_notification``)."""

    def __init__(self, binding: BridgeBinding | None = None) -> None:
        super().__init__(binding)
        self.adapter = _FakeWakeAdapter()

    def wake_adapter_for(self, agent_id: str) -> _FakeWakeAdapter:  # type: ignore[override]
        del agent_id
        return self.adapter


class _FaultNthQueryState(RealShapeState):
    """Return a real-shape provider failure on one selected read call."""

    def __init__(
        self,
        *,
        fault_query_number: int | None = None,
        fault_ordered_query_number: int | None = None,
    ) -> None:
        super().__init__()
        self._fault_query_number = fault_query_number
        self._fault_ordered_query_number = fault_ordered_query_number
        self._query_number = 0
        self._ordered_query_number = 0

    @staticmethod
    def _fault_result(label: str) -> dict[str, Any]:
        return {
            "action_status": "failed",
            "error": {"message": f"injected {label} fault"},
            "data": {},
        }

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self._query_number += 1
        if self._query_number == self._fault_query_number:
            return self._fault_result(f"query #{self._query_number}")
        return super().query_state(namespace, query)

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self._ordered_query_number += 1
        if self._ordered_query_number == self._fault_ordered_query_number:
            return self._fault_result(f"ordered query #{self._ordered_query_number}")
        return super().query_ordered(namespace, query)


def _bound_binding(agent_instance_id: str, *, bridge_id: str) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id="claude_code",
        agent_instance_id=agent_instance_id,
        session_label=agent_instance_id,
        parent_pid=4242,
    )


class _FakeMultiPeerRegistry:
    """Sweep-reroute tests (smoke f + steward leg) need MULTIPLE distinct,
    independently-registered waiters/stewards, unlike the fixed single-
    recipient ``_FakePeerRegistry`` above — a real mini-registry, not a
    hardcoded stub."""

    def __init__(self, *, adapter: _FakeWakeAdapter | None = None) -> None:
        self._by_instance: dict[str, BridgeBinding] = {}
        self._adapter = adapter

    def register(
        self,
        agent_instance_id: str,
        *,
        bridge_id: str,
        wake_capable: bool = True,
        watcher_declared: bool = False,
    ) -> None:
        self._by_instance[agent_instance_id] = replace(
            _bound_binding(agent_instance_id, bridge_id=bridge_id),
            wake_capable=wake_capable,
            watcher_declared=watcher_declared,
        )

    def wake_adapter_for(self, agent_id: str) -> _FakeWakeAdapter | None:
        """The native wake adapter, when this registry was built with one."""
        del agent_id
        return self._adapter

    def resolve(self, agent_id: str, agent_instance_id: str | None) -> BridgeBinding:
        del agent_id
        binding = self._by_instance.get(agent_instance_id or "")
        if binding is None:
            raise PeerUnreachableError(f"no binding registered for {agent_instance_id!r}")
        return binding

    def resolve_by_agent_instance_id(self, agent_instance_id: str) -> BridgeBinding | None:
        return self._by_instance.get(agent_instance_id)


# --- dead-spool retirement fixtures (codex-0147, 2026-08-13) ---
# peer_dispatch.py's dispatch-side spool tee (formerly named
# _tee_spool_if_wake_incapable) is retired: stock Codex's Stop hook cannot
# consume a spool file (async command hooks do not execute on stock Codex --
# codex-0147-async-hook-regression, 2026-08-13), so the tee was writing an
# unconsumed file on every wake_capable=false delivery. These legs prove
# dispatch_peer_send / dispatch_role_send now write NOTHING to the path the
# retired tee used to derive, through the SAME dispatch_peer_send /
# dispatch_role_send seam drive_on_delivery above is proven through.

_WC_SESSION_ID = "ases-wc-test-101-22222"
_WC_SOLET_NAME = "wc-testling"


def _wc_binding(*, wake_capable: bool) -> BridgeBinding:
    return BridgeBinding(
        bridge_id="agc-wc-recipient",
        agent_id="claude_code" if wake_capable else "codex",
        agent_instance_id="agi-wc-recipient",
        session_label="WC-Recipient",
        parent_pid=4242,
        agent_session_id=_WC_SESSION_ID,
        wake_capable=wake_capable,
    )


class _FakeSingleBindingRegistry:
    """Resolves to exactly one pre-built binding, ignoring the
    peer_id/instance hint -- these legs prove the retired tee stays gone
    (no spool file at the path it used to derive), not resolution, which
    the existing _FakePeerRegistry already covers."""

    def __init__(self, binding: BridgeBinding) -> None:
        self._binding = binding

    def resolve(self, agent_id: str, agent_instance_id: str | None) -> BridgeBinding:
        del agent_id, agent_instance_id
        return self._binding

    def wake_adapter_for(self, agent_id: str) -> None:
        del agent_id
        return None

    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        del agent_instance_id
        return ""

    def touch_binding(self, agent_instance_id: str) -> None:
        del agent_instance_id


def _send_wc(
    state: StateManagementInterface,
    binding: BridgeBinding,
    *,
    content: str,
) -> tuple[Any, _FakeBridgeManager]:
    manager = _FakeBridgeManager()
    registry = _FakeSingleBindingRegistry(binding)
    outcome = dispatch_peer_send(
        bridge_manager=manager,  # type: ignore[arg-type]
        peer_registry=registry,  # type: ignore[arg-type]
        agent_messaging_service=_FakeMessagingService(),
        state_service=state,
        sender_bridge_id="agc-sender",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        sender_parent_pid=None,
        peer_id=binding.agent_id,
        peer_agent_instance_id=binding.agent_instance_id,
        content=[TextPart(type="text", text=content)],
    )
    return outcome, manager


def _wc_expected_spool_path() -> Path:
    instance_id = f"agi-watch-{watch_instance_digest(_WC_SESSION_ID)}"
    return default_spool_path(_WC_SOLET_NAME, instance_id)


class _FakeRoleMessagingService:
    """Minimal persist_role_message stand-in, same shape as role_dispatch_smoke.py's
    own _FakeService -- dispatch_role_send's persist/notify logic is not under
    test here, only the (now-absent) spool tee that used to ride after it."""

    def persist_role_message(self, **kwargs: Any) -> RoleMessagePersisted:
        return RoleMessagePersisted(message_id=str(kwargs["message_id"]), created_at="")


def test_wake_capable_recipient_writes_no_spool() -> None:
    """wake_capable=True recipient: no spool file. Unchanged behaviour from
    before the retirement (this class never triggered the old tee's guard
    either), kept as a regression guard against a future re-introduction
    that gates on the wrong condition."""
    spool = _wc_expected_spool_path()
    spool.unlink(missing_ok=True)
    with patch.dict(os.environ, {"SOLET_NAME": _WC_SOLET_NAME}):
        state = _state()
        outcome, manager = _send_wc(
            state,
            _wc_binding(wake_capable=True),
            content="IMPORTANT: wc capable ping",
        )
    _check(not spool.exists(), "wake_capable=true recipient: no spool file created")
    _check(
        len(manager.events) == 1,
        "wake_capable=true: the existing channel-event notify still fires unchanged",
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
        "wake_capable=true: delivery_kind untouched",
    )


def test_wake_incapable_dispatch_writes_no_spool() -> None:
    """The central dead-spool-retirement leg (codex-0147, 2026-08-13). Before
    this lane, a wake_capable=False (Codex) recipient got a dispatch-side
    spool tee here that stock Codex's Stop hook could never consume; now it
    must get NOTHING. Named failing mutation: restoring a
    ``_tee_spool_if_wake_incapable``-shaped call site inside
    ``dispatch_peer_send`` must red this (a spool file would appear where
    none should, at the exact path the retired tee used to derive)."""
    spool = _wc_expected_spool_path()
    spool.unlink(missing_ok=True)
    spool_offset_path(spool).unlink(missing_ok=True)
    with patch.dict(os.environ, {"SOLET_NAME": _WC_SOLET_NAME}):
        state = _state()
        outcome, manager = _send_wc(
            state,
            _wc_binding(wake_capable=False),
            content="IMPORTANT: codex wc ping",
        )
    _check(
        not spool.exists(),
        "wake_capable=false: dispatch writes nothing at the path the retired tee used to derive -- no dead spool left behind",
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
        "wake_capable=false: existing delivery_kind untouched by the retirement",
    )
    _check(
        len(manager.events) == 1,
        "wake_capable=false: existing channel-event notify still fires unchanged",
    )


def test_wake_incapable_role_send_writes_no_spool() -> None:
    """dispatch_role_send parity leg -- proves the tee's role-addressed call
    site is ALSO gone, independent of the dispatch_peer_send leg above
    (role-addressed sends are the primary way a Codex office is reached).
    Named failing mutation: restoring a ``_tee_spool_if_wake_incapable``-
    shaped call site INSIDE ``dispatch_role_send`` must red EXACTLY this
    leg and nothing else."""
    spool = _wc_expected_spool_path()
    spool.unlink(missing_ok=True)
    spool_offset_path(spool).unlink(missing_ok=True)
    binding = _wc_binding(wake_capable=False)
    role = ResolvedRole(
        name="Codex-Reviewer",
        agent_id=binding.agent_id,
        agent_instance_id=binding.agent_instance_id,
        session_label=binding.session_label,
    )
    with patch.dict(os.environ, {"SOLET_NAME": _WC_SOLET_NAME}):
        state = _state()
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": "role_binding",
                "conflict_columns": ["external_id"],
                "record": {
                    "external_id": "role:Codex-Reviewer",
                    "role": "Codex-Reviewer",
                    "holder_kind": "session",
                    "agent_instance_id": binding.agent_instance_id,
                    "agent_session_id": binding.agent_session_id,
                    "holder_identity": {"agent_id": "codex", "session_label": "Codex-Reviewer"},
                    "claim_epoch": 1,
                },
            },
        )
        manager = _FakeBridgeManager()
        registry = _FakeSingleBindingRegistry(binding)
        outcome = dispatch_role_send(
            bridge_manager=manager,  # type: ignore[arg-type]
            peer_registry=registry,  # type: ignore[arg-type]
            agent_messaging_service=_FakeRoleMessagingService(),
            state_service=state,
            role_name="Codex-Reviewer",
            role=role,
            sender_bridge_id="agc-sender",
            sender_agent_id="claude_code",
            sender_agent_instance_id="agi-sender",
            sender_session_label="Sender",
            sender_parent_pid=None,
            content=[TextPart(type="text", text="IMPORTANT: role wc ping")],
            message_id="arm-wc-role-test",
        )
    _check(
        not spool.exists(),
        "role-send to wake_capable=false recipient: no spool file appears at the path the retired tee used to derive",
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
        "role-send: existing delivery_kind untouched by the retirement",
    )
    _check(
        len(manager.events) == 1,
        "role-send: the existing channel-event notify still fires unchanged",
    )
    _check(
        outcome.to_payload().get("drive_on_delivery") == "driver_scheduled",
        "Codex role-send schedules the retained pickup lookup alongside delivery",
    )
    finished = _await_drive(outcome)
    _check(
        finished is not None and finished[0] == "not_managed",
        "finished Codex role lookup preserves not_managed without touching any channel",
    )
    pickup = CodexPickupStore(state).read("arm-wc-role-test")
    _check(
        pickup is not None and pickup["pending"] and pickup["phase"] == "pending_resolution" and pickup["reason"] == "not_managed",
        "unmanaged Codex role recipient retains a truthful pending pickup projection",
    )
    history: list[dict[str, Any]] = CodexPickupStore(state).history("arm-wc-role-test")
    _check(
        [event["version"] for event in history] == [1, 2] and history[0]["evidence"]["phase"] == "queued" and history[1]["evidence"]["phase"] == "pending_resolution" and history[1]["evidence"]["reason"] == "not_managed",
        "unmanaged role lookup appends evidence without claiming pane submission or inbox pickup",
    )


class _LiveBridge:
    closed = False
    last_seen_at = datetime.now(UTC).isoformat()

    def touch(self) -> None:
        return None


class _FakeBridgeManager:
    @property
    def binding_liveness_window_s(self) -> int:
        return DEFAULT_BINDING_LIVENESS_WINDOW_S

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def touch(self) -> None:
        return None

    def get(self, bridge_id: str) -> _LiveBridge:
        del bridge_id
        return _LiveBridge()

    def touch_binding(self, agent_instance_id: str) -> None:
        del agent_instance_id

    def append_event(
        self,
        bridge_id: str,
        event: str,
        prose: str,
        meta: dict[str, object],
    ) -> None:
        self.events.append((bridge_id, event, prose, meta))


def _send(
    state: StateManagementInterface,
    *,
    binding: BridgeBinding | None = None,
    registry: _FakePeerRegistry | None = None,
) -> tuple[Any, _FakeBridgeManager]:
    manager = _FakeBridgeManager()
    registry = registry or _FakePeerRegistry(binding)
    outcome = dispatch_peer_send(
        bridge_manager=manager,  # type: ignore[arg-type]
        peer_registry=registry,  # type: ignore[arg-type]
        agent_messaging_service=_FakeMessagingService(),
        state_service=state,
        sender_bridge_id="agc-sender",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        sender_parent_pid=None,
        peer_id="claude_code",
        peer_agent_instance_id=_RECIPIENT_AGI,
        content=[TextPart(type="text", text="IMPORTANT: ping")],
    )
    return outcome, manager


def _await_drive(outcome: Any) -> tuple[str, str | None] | None:
    """iss_ab680764: a send reports ``driver_scheduled``; the channel half runs
    on the delivery-drive executor. Wait for that drive's finished record."""
    return finished_drive_outcome(outcome.message_id, timeout_s=5.0)


def _insert(
    state: StateManagementInterface,
    *,
    host: str = _TEST_HOST,
    agent_instance_id: str = _RECIPIENT_AGI,
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            label="delivery-target",
            session_origin="spawned",
            host=host,
        ),
    )


def test_eligible_recipient_channel_receives_notice_alongside_notify() -> None:
    """(a) + ruling 1 (ALONGSIDE, not instead of)."""
    for eligible_state in (LIFECYCLE_LIVE, LIFECYCLE_IDLE, LIFECYCLE_OVERDUE):
        driver = _install_fake_host()
        try:
            state = _state()
            _insert(state)
            transition_lifecycle_state(
                state,
                agent_instance_id=_RECIPIENT_AGI,
                from_state=LIFECYCLE_SPAWNING,
                to_state=LIFECYCLE_LIVE,
                directed_by="test:none",
            )
            if eligible_state != LIFECYCLE_LIVE:
                transition_lifecycle_state(
                    state,
                    agent_instance_id=_RECIPIENT_AGI,
                    from_state=LIFECYCLE_LIVE,
                    to_state=eligible_state,
                    directed_by="test:none",
                )
            outcome, manager = _send(state)
            _check(
                outcome.to_payload().get("drive_on_delivery") == "driver_scheduled",
                f"({eligible_state}) the send reports driver_scheduled, not a drive it has not run",
            )
            finished = _await_drive(outcome)
            _check(
                len(driver.channel.sent) == 1 and "drain peer_inbox" in driver.channel.sent[0],
                f"({eligible_state}) drive-on-delivery notice reaches the channel",
            )
            _check(
                "IMPORTANT: ping" not in driver.channel.sent[0] and "ping" not in driver.channel.sent[0],
                f"({eligible_state}) the channel notice is NOT the message body",
            )
            _check(
                len(manager.events) == 1,
                f"({eligible_state}) the existing channel-event notify STILL fires (ALONGSIDE, not replaced)",
            )
            _check(
                outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
                f"({eligible_state}) delivery_kind is untouched by the drive nudge",
            )
            _check(
                finished is not None and finished[0] == "driver_sent",
                f"({eligible_state}) the finished drive records driver_sent alongside delivery",
            )
        finally:
            _remove_fake_host()


def test_ineligible_recipient_channel_receives_nothing() -> None:
    """(b), CORRECTED per 2026-08-04 sign-off: parked AND spawning must both
    be excluded by drive_on_delivery's OWN explicit gate — neither state is
    excluded by _resolve_driver_channel (verified: the fake driver's channel
    is unconditionally live for both). Named failing mutation (hand-verified
    2026-08-04): deleting the
    ``if str(row.get("lifecycle_state") or "") not in
    _DRIVE_ON_DELIVERY_ELIGIBLE_STATES: return`` gate in
    session_lifecycle_verbs.drive_on_delivery reds BOTH checks below (the
    channel receives the notice on a parked/spawning row instead of nothing)
    — no other guard in the call chain catches either state."""
    for ineligible_state, needs_transition in (
        (LIFECYCLE_SPAWNING, False),
        (LIFECYCLE_PARKED, True),
    ):
        driver = _install_fake_host()
        try:
            state = _state()
            _insert(state)
            if needs_transition:
                transition_lifecycle_state(
                    state,
                    agent_instance_id=_RECIPIENT_AGI,
                    from_state=LIFECYCLE_SPAWNING,
                    to_state=LIFECYCLE_LIVE,
                    directed_by="test:none",
                )
                transition_lifecycle_state(
                    state,
                    agent_instance_id=_RECIPIENT_AGI,
                    from_state=LIFECYCLE_LIVE,
                    to_state=ineligible_state,
                    directed_by="test:none",
                )
            outcome, manager = _send(state)
            _check(
                driver.channel.sent == [],
                f"({ineligible_state}) drive-on-delivery does NOT touch the channel",
            )
            _check(
                len(manager.events) == 1,
                f"({ineligible_state}) the existing notify still fires (untouched by the gate)",
            )
            _check(
                outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
                f"({ineligible_state}) delivery_kind is untouched",
            )
            _check(
                outcome.to_payload().get("drive_on_delivery") == "ineligible_state",
                f"({ineligible_state}) sender sees ineligible_state",
            )
        finally:
            _remove_fake_host()


def test_parked_capable_recipient_interrupts_and_reports_detail() -> None:
    """A parked Codex-capable pane is the narrow exception to the old gate.

    Failing mutation: remove the parked capability check or the detail sink.
    A generic parked driver remains ineligible in the preceding test, so this
    does not broaden recovery to an unmeasured host.
    """
    driver = _install_fake_host(park_interrupt=True)
    try:
        state = _state()
        _insert(state)
        transition_lifecycle_state(
            state,
            agent_instance_id=_RECIPIENT_AGI,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="test:none",
        )
        transition_lifecycle_state(
            state,
            agent_instance_id=_RECIPIENT_AGI,
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_PARKED,
            directed_by="test:none",
        )
        outcome, manager = _send(state)
        _check(
            outcome.to_payload().get("drive_on_delivery") == "driver_scheduled",
            "parked capable delivery is scheduled at send time",
        )
        finished = _await_drive(outcome)
        _check(driver.channel.interrupts == 1, "parked capable channel receives one interrupt")
        _check(
            driver.channel.sent == ["delivery waiting from Sender — drain peer_inbox"],
            "parked capable channel receives the ordinary fixed notice after interrupt",
        )
        _check(
            finished is not None and finished[0] == "driver_sent",
            "parked capable delivery records driver_sent without changing durable delivery",
        )
        _check(
            finished is not None and finished[1] == "interrupted_park",
            "parked capable delivery records interrupted_park truthfully",
        )
        _check(len(manager.events) == 1, "park recovery stays alongside the durable notify")
    finally:
        _remove_fake_host()


def test_raising_channel_never_fails_the_send() -> None:
    """(c) — containment: the send's own result is untouched by a raising
    driver channel. Named failing mutation: removing drive_on_delivery's
    ``try/except Exception`` around ``channel.send(notice)`` reds this leg
    (the RuntimeError propagates out of dispatch_peer_send instead of being
    swallowed)."""
    driver = _install_fake_host(raise_on_send=True)
    try:
        state = _state()
        _insert(state)
        transition_lifecycle_state(
            state,
            agent_instance_id=_RECIPIENT_AGI,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="test:none",
        )
        raised = False
        outcome = None
        try:
            outcome, _manager = _send(state)
        except Exception:  # noqa: BLE001 — the whole point is it must NOT raise
            raised = True
        _check(not raised, "a raising driver channel does not propagate out of dispatch_peer_send")
        finished = _await_drive(outcome) if outcome is not None else None
        _check(
            outcome is not None and outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
            "the caller's own delivery outcome is untouched by the channel fault",
        )
        _check(
            driver.channel.sent == [],
            "nothing recorded as sent (the raise happened inside send)",
        )
        _check(
            outcome is not None and outcome.to_payload().get("drive_on_delivery") == "driver_scheduled",
            "the send itself reports driver_scheduled; the fault happens on the executor",
        )
        _check(
            finished is not None and finished[0] == "driver_error",
            "a contained channel exception is recorded as driver_error",
        )
    finally:
        _remove_fake_host()


def test_drive_fault_releases_the_queue_slot() -> None:
    """The executor-thread lookup fault reaches _run_and_log's except path."""
    _install_fake_host(raise_on_delivery_drive_thread=True)
    try:
        state = _state()
        _insert(state)
        transition_lifecycle_state(
            state,
            agent_instance_id=_RECIPIENT_AGI,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="test:none",
        )
        with patch("agent_messaging_plugin.delivery_drive.logger.exception") as logged_fault:
            pending_before = drive_queue_snapshot()["pending"]
            outcome, _manager = _send(state)
            finished = _await_drive(outcome)
            pending_after = drive_queue_snapshot()["pending"]
        fault_log = logged_fault.call_args
        _check(
            logged_fault.call_count == 1 and fault_log is not None and "faulted after" in str(fault_log.args[0]),
            "drive fault releases the queue slot: _run_and_log exception branch was entered",
        )
        _check(
            outcome.to_payload().get("drive_on_delivery") == "driver_scheduled" and finished is not None and finished[0] == "driver_error",
            "drive fault releases the queue slot: executor driver fault was recorded",
        )
        _check(
            pending_after == pending_before,
            f"drive fault releases the queue slot: pending counter returns to its prior value (before={pending_before}, after={pending_after})",
        )
    finally:
        _remove_fake_host()


def test_successful_drive_releases_the_queue_slot() -> None:
    """The queue pending counter returns to its prior value after a normal drive."""
    _install_fake_host()
    try:
        state = _state()
        _insert(state)
        transition_lifecycle_state(
            state,
            agent_instance_id=_RECIPIENT_AGI,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="test:none",
        )
        pending_before = drive_queue_snapshot()["pending"]
        outcome, _manager = _send(state)
        finished = _await_drive(outcome)
        pending_after = drive_queue_snapshot()["pending"]
        _check(
            outcome.to_payload().get("drive_on_delivery") == "driver_scheduled" and finished is not None and finished[0] == "driver_sent",
            "successful drive releases the queue slot: scheduled drive completed",
        )
        _check(
            pending_after == pending_before,
            f"successful drive releases the queue slot: pending counter returns to its prior value (before={pending_before}, after={pending_after})",
        )
    finally:
        _remove_fake_host()


def test_non_managed_recipient_unaffected() -> None:
    """(d) — a recipient with NO managed_session row at all (never spawned
    via spawn_session — an ordinary operator-launched session) is byte-
    identical to pre-lane dispatch_peer_send: no driver lookup even
    attempted (no fake host installed for this test at all)."""
    state = _state()
    outcome, manager = _send(state)
    _check(
        outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
        "non-managed recipient: delivery_kind unchanged from pre-lane behaviour",
    )
    _check(len(manager.events) == 1, "non-managed recipient: the existing notify still fires")
    _check(
        outcome.to_payload().get("drive_on_delivery") == "not_managed",
        "non-managed recipient: sender sees not_managed",
    )


def test_exact_query_empty_is_not_managed() -> None:
    """A completed exact lookup with zero rows is genuine absence."""
    outcome, _manager = _send(_state())
    _check(
        outcome.to_payload().get("drive_on_delivery") == "not_managed",
        "exact query completed empty -> not_managed",
    )


def test_exact_query_fault_is_driver_unavailable() -> None:
    """An exact-lookup provider fault is unknown, never absence."""
    state = cast(
        "StateManagementInterface",
        _FaultNthQueryState(fault_query_number=1),
    )
    outcome, _manager = _send(state)
    _check(
        outcome.to_payload().get("drive_on_delivery") == "driver_unavailable",
        "exact query fault -> driver_unavailable (must not collapse to not_managed)",
    )


def test_fallback_query_empty_is_not_managed() -> None:
    """After an exact miss, a completed empty stable-id lookup is absence."""
    outcome, _manager = _send(
        _state(),
        binding=_binding(agent_session_id="ases-missing-managed-session"),
    )
    _check(
        outcome.to_payload().get("drive_on_delivery") == "not_managed",
        "fallback query completed empty -> not_managed",
    )


def test_fallback_query_fault_is_driver_unavailable() -> None:
    """After an exact miss, a stable-id query fault remains unknown."""
    state = cast(
        "StateManagementInterface",
        _FaultNthQueryState(fault_ordered_query_number=1),
    )
    outcome, _manager = _send(
        state,
        binding=_binding(agent_session_id="ases-faulted-managed-session"),
    )
    _check(
        outcome.to_payload().get("drive_on_delivery") == "driver_unavailable",
        "fallback query fault -> driver_unavailable (must not collapse to not_managed)",
    )


def test_report_by_unchanged_after_drive_on_delivery() -> None:
    """(e), regression guard for seat position 2: an inbound delivery notice
    must never re-arm report_by (that stays drive_session's own edge — an
    inbound message keeping a silent worker 'alive' would blind the sweep).
    Named failing mutation: adding a ``_rearm_report_by`` call inside
    drive_on_delivery would red this leg."""
    _install_fake_host()
    try:
        state = _state()
        _insert(state)
        transition_lifecycle_state(
            state,
            agent_instance_id=_RECIPIENT_AGI,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="test:none",
        )
        report_by_before = read_managed_session(state, _RECIPIENT_AGI).get("report_by")
        outcome, _manager = _send(state)
        finished = _await_drive(outcome)
        _check(
            finished is not None and finished[0] == "driver_sent",
            "the scheduled drive actually ran before report_by is re-read",
        )
        report_by_after = read_managed_session(state, _RECIPIENT_AGI).get("report_by")
        _check(
            report_by_before == report_by_after,
            f"report_by is byte-identical before/after a drive-on-delivery notice (before={report_by_before!r}, after={report_by_after!r})",
        )
    finally:
        _remove_fake_host()


def test_watcher_identity_resolves_exact_managed_session() -> None:
    """(f) A managed watch worker registers under ``agi-watch-*`` while its
    managed row retains the spawn-time instance id. The stable session id is
    the exact, backfilled join key; dispatch must carry it into the driver
    lookup so a delivery still starts the managed worker's next turn."""
    driver = _install_fake_host()
    try:
        state = _state()
        spawned_instance_id = "agi-spawned-codex-worker"
        stable_session_id = "ases-agi-spawned-codex-worker"
        watcher_instance_id = f"agi-watch-{watch_instance_digest(stable_session_id)}"
        _insert(state, agent_instance_id=spawned_instance_id)
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": "managed_session",
                "filters": {"agent_instance_id": spawned_instance_id, "is_deleted": 0},
            },
            {"agent_session_id": stable_session_id},
        )
        transition_lifecycle_state(
            state,
            agent_instance_id=spawned_instance_id,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="test:none",
        )
        outcome, manager = _send(
            state,
            binding=_binding(
                agent_instance_id=watcher_instance_id,
                agent_session_id=stable_session_id,
            ),
        )
        _await_drive(outcome)
        _check(
            len(driver.channel.sent) == 1 and "drain peer_inbox" in driver.channel.sent[0],
            "watcher stable session id resolves the managed spawn driver's channel",
        )
        _check(len(manager.events) == 1, "watcher delivery still queues its ordinary event")
        _check(
            outcome.delivery == DELIVERY_QUEUED_WATCHER,
            "watcher identity reconciliation does not relabel the delivery outcome",
        )
    finally:
        _remove_fake_host()


def test_watcher_identity_ambiguity_never_drives() -> None:
    """A corrupt duplicate stable id must fail closed: no arbitrary managed
    row receives the notice, while the ordinary durable delivery proceeds."""
    driver = _install_fake_host()
    try:
        state = _state()
        stable_session_id = "ases-duplicate-managed-lineage"
        watcher_instance_id = f"agi-watch-{watch_instance_digest(stable_session_id)}"
        for index in (1, 2):
            spawned_instance_id = f"agi-duplicate-{index}"
            _insert(state, agent_instance_id=spawned_instance_id)
            state.update_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": "managed_session",
                    "filters": {
                        "agent_instance_id": spawned_instance_id,
                        "is_deleted": 0,
                    },
                },
                {"agent_session_id": stable_session_id},
            )
            transition_lifecycle_state(
                state,
                agent_instance_id=spawned_instance_id,
                from_state=LIFECYCLE_SPAWNING,
                to_state=LIFECYCLE_LIVE,
                directed_by="test:none",
            )
        outcome, manager = _send(
            state,
            binding=_binding(
                agent_instance_id=watcher_instance_id,
                agent_session_id=stable_session_id,
            ),
        )
        _check(driver.channel.sent == [], "ambiguous stable session id drives no managed row")
        _check(len(manager.events) == 1, "ambiguous mapping does not suppress ordinary delivery")
        _check(
            outcome.delivery == DELIVERY_QUEUED_WATCHER,
            "ambiguous mapping does not change the delivery outcome",
        )
        _check(
            outcome.to_payload().get("drive_on_delivery") == "driver_unavailable",
            "ambiguous stable identity is visible as driver_unavailable",
        )
    finally:
        _remove_fake_host()


def test_state_service_none_never_raises() -> None:
    """Optional-collaborator degrade (mirrors the sweeps' own
    convention): state_service=None must not crash dispatch_peer_send — the
    drive is best-effort and simply has nothing to drive without a state
    handle."""
    manager = _FakeBridgeManager()
    registry = _FakePeerRegistry()
    raised = False
    outcome = None
    try:
        outcome = dispatch_peer_send(
            bridge_manager=manager,  # type: ignore[arg-type]
            peer_registry=registry,  # type: ignore[arg-type]
            agent_messaging_service=_FakeMessagingService(),
            state_service=None,
            sender_bridge_id="agc-sender",
            sender_agent_id="claude_code",
            sender_agent_instance_id="agi-sender",
            sender_session_label="Sender",
            sender_parent_pid=None,
            peer_id="claude_code",
            peer_agent_instance_id=_RECIPIENT_AGI,
            content=[TextPart(type="text", text="IMPORTANT: ping")],
        )
    except Exception:  # noqa: BLE001 — the whole point is it must NOT raise
        raised = True
    _check(not raised, "state_service=None degrades silently, never raises")
    _check(
        outcome is not None and outcome.to_payload().get("drive_on_delivery") == "driver_unavailable",
        "state_service=None is visible as driver_unavailable",
    )


def test_managed_recipient_without_driver_is_visible() -> None:
    """An eligible managed row whose host has no driver channel keeps the
    durable delivery successful while reporting driver_unavailable."""
    state = _state()
    _insert(state, host="operator")
    transition_lifecycle_state(
        state,
        agent_instance_id=_RECIPIENT_AGI,
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="test:none",
    )
    outcome, manager = _send(state)
    _check(len(manager.events) == 1, "driver-unavailable recipient still receives notify")
    _check(
        outcome.to_payload().get("drive_on_delivery") == "driver_unavailable",
        "eligible managed recipient without a driver reports driver_unavailable",
    )


def test_sweep_deadline_edge_drives_a_live_managed_waiter() -> None:
    """(f) — sweep_deadline_dependencies fires an armed edge and drives the
    managed waiter's channel ALONGSIDE the existing append_event (ruling 1,
    2026-08-04 sign-off: never instead of it). Also proves the two lanes
    compose: the row this test arms is written by the REAL
    arm_session_dependency verb, not hand-inserted, then fired by the REAL
    sweep."""
    waiter = "agi-sweep-waiter01"
    driver = _install_fake_host()
    try:
        state = _state()
        _insert(state, agent_instance_id=waiter)
        # _deliver_dependency_wake resolves via the row's OWN agent_id column
        # (a pre-existing quirk of that function, distinct from
        # drive_on_delivery's read-by-instance-id) -- a fresh
        # insert_managed_session row carries no agent_id until the real
        # spawned process registers; backfill_registration is what a real
        # registration does, so this mirrors a genuinely LIVE managed
        # session, not a half-spawned one.
        # backfill_registration ALSO fires the spawning -> live edge (the
        # registration hook's own job) -- no separate manual transition
        # needed, calling both would race the row's own state.
        backfill_registration(
            state,
            agent_instance_id=waiter,
            agent_id="claude_code",
            agent_session_id="ases-sweep-waiter01",
        )
        past_deadline = "2020-01-01T00:00:00+00:00"
        armed = verb_arm_session_dependency(
            state,
            ArmSessionDependencyRequest(
                waiter_instance_id=waiter,
                condition_kind=CONDITION_DEADLINE,
                condition_ref=past_deadline,
            ),
        )
        _check(armed.get("armed") is True, "the deadline edge armed cleanly via the real verb")

        registry = _FakeMultiPeerRegistry()
        registry.register(waiter, bridge_id="agc-sweep-waiter")
        manager = _FakeBridgeManager()
        fired = sweep_deadline_dependencies(state, peer_registry=registry, bridge_manager=manager)  # type: ignore[arg-type]
        _check(fired == 1, f"sweep_deadline_dependencies fired exactly 1 edge (got {fired})")
        _check(
            len(manager.events) == 1 and manager.events[0][1] == EVENT_SESSION_DEPENDENCY_WAKE,
            "the existing session_dependency_wake append_event STILL fires (ALONGSIDE, not replaced)",
        )
        _check(
            len(driver.channel.sent) == 1 and "drain peer_inbox" in driver.channel.sent[0],
            "the managed waiter's driver channel ALSO receives a drive-on-delivery notice",
        )
    finally:
        _remove_fake_host()


# ---------------------------------------------------------------------------
# prb_57c118ad (r74 K3-B1): the send seam skips the "delivery waiting" pane
# notice when the recipient's live channel carries the message. The notice
# exists to wake a session with no other way to learn of the message; a Claude
# session on a live MCP bridge already has the queued channel event. Legs
# (a)/(d)/(e) are the new behaviour; (b)/(c) pin the sessions that keep the
# notice (no surfacing channel; pull-consumer watcher).
# ---------------------------------------------------------------------------


def _managed_live(state: StateManagementInterface, *, agent_instance_id: str = _RECIPIENT_AGI) -> None:
    _insert(state, agent_instance_id=agent_instance_id)
    transition_lifecycle_state(
        state,
        agent_instance_id=agent_instance_id,
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="test:none",
    )


def test_channel_carried_message_gets_no_pane_notice() -> None:
    """(a) A managed, live, non-watcher Claude recipient whose bridge is
    wake_capable and has a native wake adapter: the message is queued on its
    channel, so nothing is typed into its pane and nothing is scheduled."""
    driver = _install_fake_host()
    try:
        state = _state()
        _managed_live(state)
        registry = _AdapterPeerRegistry(_binding())
        outcome, manager = _send(state, registry=registry)
        payload = outcome.to_payload()
        _check(outcome.delivery == DELIVERY_QUEUED_WAKE, "(a) the send reports queued_wake: the channel event is queued")
        _check(len(registry.adapter.calls) == 1, "(a) the native wake adapter queued the channel event exactly once")
        _check(
            payload.get("drive_on_delivery") == "driver_not_needed",
            "(a) a channel-carried message reports driver_not_needed, not a drive it never ran",
        )
        # Wait for whatever drive was scheduled (none after the fix), so "nothing
        # typed" is read after a drive could have typed, never before.
        finished = _await_drive(outcome)
        _check(driver.channel.sent == [], "(a) no 'delivery waiting' notice reaches the recipient's pane")
        _check(
            CodexPickupStore(state).read(outcome.message_id) is None,
            "(a) no pickup row is written for a message the channel carries",
        )
        _check(
            finished is not None and finished[0] == "driver_not_needed" and finished[1] is not None and "live channel, which carries it" in finished[1],
            "(a) the outcome is recorded by message id with its own detail text",
        )
        _check("send_status" not in payload, "(a) the sender-visible payload carries no NOT DELIVERED annotation")
        _check(not manager.events, "(a) the adapter, not append_event, carried the message")
    finally:
        _remove_fake_host()


def test_channel_without_surfacing_still_gets_pane_notice() -> None:
    """(b) wake_capable=False (a Claude bridge on a provider that ignores
    development channels): the queued event is never surfaced, so the notice
    is that session's only wake and is typed exactly as before."""
    driver = _install_fake_host()
    try:
        state = _state()
        _managed_live(state)
        registry = _AdapterPeerRegistry(replace(_binding(), wake_capable=False))
        outcome, _manager = _send(state, registry=registry)
        _check(outcome.delivery == DELIVERY_QUEUED_WAKE, "(b) the send still reports queued_wake")
        _check(
            outcome.to_payload().get("drive_on_delivery") == "driver_scheduled",
            "(b) wake_capable=false: the drive is scheduled as before",
        )
        finished = _await_drive(outcome)
        _check(finished is not None and finished[0] == "driver_sent", "(b) the finished drive records driver_sent")
        _check(
            len(driver.channel.sent) == 1 and "drain peer_inbox" in driver.channel.sent[0],
            "(b) wake_capable=false: exactly one pane notice reaches the channel",
        )
    finally:
        _remove_fake_host()


def test_watcher_with_adapter_still_gets_pane_notice() -> None:
    """(c) A watcher is a pull consumer: its channel surfaces nothing, so the
    notice stays, adapter registered or not."""
    driver = _install_fake_host()
    try:
        state = _state()
        spawned_instance_id = "agi-spawned-watch-worker"
        stable_session_id = "ases-agi-spawned-watch-worker"
        watcher_instance_id = f"agi-watch-{watch_instance_digest(stable_session_id)}"
        _insert(state, agent_instance_id=spawned_instance_id)
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": "managed_session", "filters": {"agent_instance_id": spawned_instance_id, "is_deleted": 0}},
            {"agent_session_id": stable_session_id},
        )
        transition_lifecycle_state(
            state,
            agent_instance_id=spawned_instance_id,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="test:none",
        )
        registry = _AdapterPeerRegistry(_binding(agent_instance_id=watcher_instance_id, agent_session_id=stable_session_id))
        outcome, _manager = _send(state, registry=registry)
        _await_drive(outcome)
        _check(outcome.delivery == DELIVERY_QUEUED_WATCHER, "(c) a watcher recipient is labelled queued_watcher")
        _check(
            len(driver.channel.sent) == 1 and "drain peer_inbox" in driver.channel.sent[0],
            "(c) the watcher still gets its pane notice: a pull consumer has no channel that surfaces the message",
        )
    finally:
        _remove_fake_host()


def test_role_send_channel_carried_gets_no_pane_notice() -> None:
    """(d) The role-send twin of (a), through dispatch_role_send's own call."""
    driver = _install_fake_host()
    try:
        state = _state()
        _managed_live(state)
        binding = _binding(agent_session_id="ases-role-twin")
        registry = _AdapterPeerRegistry(binding)
        role = ResolvedRole(
            name="Channel-Role",
            agent_id=binding.agent_id,
            agent_instance_id=binding.agent_instance_id,
            session_label=binding.session_label,
        )
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": "role_binding",
                "conflict_columns": ["external_id"],
                "record": {
                    "external_id": "role:Channel-Role",
                    "role": "Channel-Role",
                    "holder_kind": "session",
                    "agent_instance_id": binding.agent_instance_id,
                    "agent_session_id": binding.agent_session_id,
                    "holder_identity": {"agent_id": "claude_code", "session_label": "Channel-Role"},
                    "claim_epoch": 1,
                },
            },
        )
        manager = _FakeBridgeManager()
        outcome = dispatch_role_send(
            bridge_manager=manager,  # type: ignore[arg-type]
            peer_registry=registry,  # type: ignore[arg-type]
            agent_messaging_service=_FakeRoleMessagingService(),
            state_service=state,
            role_name="Channel-Role",
            role=role,
            sender_bridge_id="agc-sender",
            sender_agent_id="claude_code",
            sender_agent_instance_id="agi-sender",
            sender_session_label="Sender",
            sender_parent_pid=None,
            content=[TextPart(type="text", text="IMPORTANT: role channel ping")],
            message_id="arm-channel-role-test",
        )
        _check(outcome.delivery == DELIVERY_QUEUED_WAKE, "(d) the role send reports queued_wake")
        _check(
            outcome.to_payload().get("drive_on_delivery") == "driver_not_needed",
            "(d) a channel-carried role message reports driver_not_needed",
        )
        _await_drive(outcome)
        _check(driver.channel.sent == [], "(d) no pane notice reaches the role holder's pane")
        _check(
            CodexPickupStore(state).read("arm-channel-role-test") is None,
            "(d) no pickup row is written for a role message the channel carries",
        )
    finally:
        _remove_fake_host()


def test_channel_carries_message_truth_table() -> None:
    """(e) The one definition of 'the live channel carries the message'."""
    base = _binding()
    watcher = replace(base, agent_instance_id="agi-watch-0123456789ab")
    _check(channel_carries_message(base, native_wake_adapter=True), "(e) wake_capable, adapter, not a watcher: the channel carries it")
    _check(not channel_carries_message(base, native_wake_adapter=False), "(e) no native wake adapter: the message went to the notification path, not carried")
    _check(not channel_carries_message(replace(base, wake_capable=False), native_wake_adapter=True), "(e) wake_capable false: the channel never surfaces it")
    _check(not channel_carries_message(watcher, native_wake_adapter=True), "(e) a watcher is a pull consumer: no channel surfaces it")


# ---------------------------------------------------------------------------
# prb_57c118ad (r74 K3-D1): the callers that remain after the clock sweeps
# were removed. The dependency-wake sweep appends a bridge event and then
# drives its waiter; the session_terminal fire drives its waiter and appends
# nothing. The pane notice follows the sweep's event only when that event did
# not reach a live channel (same rule as the send seam), and always follows
# the terminal fire, which has no event to rely on.
# ---------------------------------------------------------------------------

_WAKE_WAITER = "agi-dep-wake-waiter"


class _RaisingBridgeManager(_FakeBridgeManager):
    """A bridge whose append_event fails: the event reached no channel."""

    def append_event(self, bridge_id: str, event: str, prose: str, meta: dict[str, object]) -> None:
        del bridge_id, event, prose, meta
        raise RuntimeError("bridge append refused")


def _arm_live_waiter(
    state: StateManagementInterface,
    *,
    condition_kind: str,
    condition_ref: str,
) -> None:
    """A live managed waiter (registered the way a spawned session registers)
    with one armed dependency edge, written through the real arm verb."""
    _insert(state, agent_instance_id=_WAKE_WAITER)
    backfill_registration(
        state,
        agent_instance_id=_WAKE_WAITER,
        agent_id="claude_code",
        agent_session_id="ases-dep-wake-waiter",
    )
    armed = verb_arm_session_dependency(
        state,
        ArmSessionDependencyRequest(
            waiter_instance_id=_WAKE_WAITER,
            condition_kind=condition_kind,
            condition_ref=condition_ref,
        ),
    )
    _check(armed.get("armed") is True, f"{condition_kind} edge armed cleanly via the real verb")


def _sweep_wake(registry: _FakeMultiPeerRegistry, manager: _FakeBridgeManager) -> tuple[int, list[str]]:
    """Fire one past-deadline edge through the real sweep; return how many fired
    and the pane notices the waiter's driver channel received."""
    driver = _install_fake_host()
    try:
        state = _state()
        _arm_live_waiter(state, condition_kind=CONDITION_DEADLINE, condition_ref="2020-01-01T00:00:00+00:00")
        fired = sweep_deadline_dependencies(state, peer_registry=registry, bridge_manager=manager)  # type: ignore[arg-type]
        return fired, list(driver.channel.sent)
    finally:
        _remove_fake_host()


def test_dependency_wake_to_channel_carried_waiter_gets_no_pane_notice() -> None:
    """(f) The wake event reached a live channel (adapter registered,
    wake_capable, not a watcher): typing 'delivery waiting' would only repeat it."""
    registry = _FakeMultiPeerRegistry(adapter=_FakeWakeAdapter())
    registry.register(_WAKE_WAITER, bridge_id="agc-dep-wake")
    manager = _FakeBridgeManager()
    fired, notices = _sweep_wake(registry, manager)
    _check(fired == 1, f"(f) the sweep fired exactly 1 edge (got {fired})")
    _check(
        len(manager.events) == 1 and manager.events[0][1] == EVENT_SESSION_DEPENDENCY_WAKE,
        "(f) the wake event is still appended to the waiter's bridge",
    )
    _check(notices == [], f"(f) a channel-carried waiter gets no pane notice (got {notices})")


def test_dependency_wake_without_a_surfacing_channel_still_gets_pane_notice() -> None:
    """(g) wake_capable=False, a watcher, and no adapter: the event surfaces
    nothing, so the notice is that waiter's only wake and is typed as before."""
    cases = (
        ("wake_capable=false", {"wake_capable": False}, True),
        ("watcher", {"watcher_declared": True}, True),
        ("no adapter registered", {}, False),
    )
    for label, flags, with_adapter in cases:
        registry = _FakeMultiPeerRegistry(adapter=_FakeWakeAdapter() if with_adapter else None)
        registry.register(_WAKE_WAITER, bridge_id="agc-dep-wake", **flags)  # type: ignore[arg-type]
        manager = _FakeBridgeManager()
        fired, notices = _sweep_wake(registry, manager)
        _check(fired == 1 and len(manager.events) == 1, f"(g) {label}: the edge fired and the event was appended")
        _check(
            len(notices) == 1 and "drain peer_inbox" in notices[0],
            f"(g) {label}: exactly one pane notice reaches the waiter (got {notices})",
        )


def test_dependency_wake_whose_event_append_failed_still_gets_pane_notice() -> None:
    """(h) A channel-carried waiter whose event append raised got nothing on
    its channel, so the notice must still follow."""
    registry = _FakeMultiPeerRegistry(adapter=_FakeWakeAdapter())
    registry.register(_WAKE_WAITER, bridge_id="agc-dep-wake")
    fired, notices = _sweep_wake(registry, _RaisingBridgeManager())
    _check(fired == 1, "(h) the edge fired although the wake append failed")
    _check(
        len(notices) == 1 and "drain peer_inbox" in notices[0],
        f"(h) a failed append leaves the pane notice as the only wake (got {notices})",
    )


def test_terminal_edge_waiter_always_gets_pane_notice() -> None:
    """(i) The session_terminal fire appends no event, so nothing reached any
    channel and the notice is the only wake, for every kind of waiter."""
    driver = _install_fake_host()
    try:
        state = _state()
        _arm_live_waiter(state, condition_kind=CONDITION_SESSION_TERMINAL, condition_ref="agi-awaited-session")
        fired = _fire_session_terminal_dependencies(
            state,
            agent_instance_id="agi-awaited-session",
            fired_at=datetime.now(UTC).isoformat(),
        )
        _check(fired == 1, f"(i) the session_terminal edge fired (got {fired})")
        _check(
            len(driver.channel.sent) == 1 and "drain peer_inbox" in driver.channel.sent[0],
            f"(i) the waiter gets its pane notice, no event reached a channel (got {driver.channel.sent})",
        )
    finally:
        _remove_fake_host()


def main() -> int:
    print("=== drive-on-delivery lane smoke (slices 1-2) ===")
    test_eligible_recipient_channel_receives_notice_alongside_notify()
    test_ineligible_recipient_channel_receives_nothing()
    test_parked_capable_recipient_interrupts_and_reports_detail()
    test_raising_channel_never_fails_the_send()
    test_drive_fault_releases_the_queue_slot()
    test_successful_drive_releases_the_queue_slot()
    test_non_managed_recipient_unaffected()
    test_exact_query_empty_is_not_managed()
    test_exact_query_fault_is_driver_unavailable()
    test_fallback_query_empty_is_not_managed()
    test_fallback_query_fault_is_driver_unavailable()
    test_report_by_unchanged_after_drive_on_delivery()
    test_watcher_identity_resolves_exact_managed_session()
    test_watcher_identity_ambiguity_never_drives()
    test_state_service_none_never_raises()
    test_managed_recipient_without_driver_is_visible()
    test_sweep_deadline_edge_drives_a_live_managed_waiter()
    test_wake_capable_recipient_writes_no_spool()
    test_wake_incapable_dispatch_writes_no_spool()
    test_wake_incapable_role_send_writes_no_spool()
    test_channel_carried_message_gets_no_pane_notice()
    test_channel_without_surfacing_still_gets_pane_notice()
    test_watcher_with_adapter_still_gets_pane_notice()
    test_role_send_channel_carried_gets_no_pane_notice()
    test_channel_carries_message_truth_table()
    test_dependency_wake_to_channel_carried_waiter_gets_no_pane_notice()
    test_dependency_wake_without_a_surfacing_channel_still_gets_pane_notice()
    test_dependency_wake_whose_event_append_failed_still_gets_pane_notice()
    test_terminal_edge_waiter_always_gets_pane_notice()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
