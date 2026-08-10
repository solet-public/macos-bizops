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
``report_by`` — that stays ``drive_session``'s own edge):

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

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/drive_on_delivery_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.models import RoleMessagePersisted, TextPart  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from click.testing import CliRunner  # noqa: E402

import agent_messaging_plugin.local_cli.wake as wake_mod  # noqa: E402
import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.bridge_sessions import (  # noqa: E402
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
)
from agent_messaging_plugin.local_cli.spool import (  # noqa: E402
    default_spool_path,
    spool_offset_path,
    watch_instance_digest,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import (  # noqa: E402
    DELIVERY_QUEUED_NOTIFICATION,
    DELIVERY_QUEUED_WATCHER,
    dispatch_peer_send,
    dispatch_role_send,
)
from agent_messaging_plugin.peer_registry import PeerUnreachableError  # noqa: E402
from agent_messaging_plugin.peer_role_management import ResolvedRole  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_DEADLINE,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_SPAWNING,
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
    EVENT_SESSION_OVERDUE_NOTICE,
    sweep_deadline_dependencies,
    sweep_overdue_sessions,
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
    return cast("StateManagementInterface", RealShapeState())


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


class _FakeDriverWithChannel:
    def __init__(self, *, raise_on_send: bool = False) -> None:
        self.channel = _FakeChannel(raise_on_send=raise_on_send)

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
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _install_fake_host(*, raise_on_send: bool = False) -> _FakeDriverWithChannel:
    driver = _FakeDriverWithChannel(raise_on_send=raise_on_send)
    session_hosts._REGISTRY[_TEST_HOST] = driver  # noqa: SLF001 -- test-only monkeypatch
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


def _binding() -> BridgeBinding:
    return BridgeBinding(
        bridge_id="agc-recipient",
        agent_id="claude_code",
        agent_instance_id=_RECIPIENT_AGI,
        session_label="Recipient",
        parent_pid=4242,
    )


class _FakePeerRegistry:
    """No native adapter registered — forces the append_event (channel-event)
    notify branch, orthogonal to the drive-on-delivery seam under test."""

    def resolve(self, agent_id: str, agent_instance_id: str | None) -> BridgeBinding:
        del agent_id, agent_instance_id
        return _binding()

    def wake_adapter_for(self, agent_id: str) -> None:
        del agent_id
        return None

    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        del agent_instance_id
        return ""

    def touch_binding(self, agent_instance_id: str) -> None:
        del agent_instance_id


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

    def __init__(self) -> None:
        self._by_instance: dict[str, BridgeBinding] = {}

    def register(self, agent_instance_id: str, *, bridge_id: str) -> None:
        self._by_instance[agent_instance_id] = _bound_binding(
            agent_instance_id, bridge_id=bridge_id,
        )

    def resolve(self, agent_id: str, agent_instance_id: str | None) -> BridgeBinding:
        del agent_id
        binding = self._by_instance.get(agent_instance_id or "")
        if binding is None:
            raise PeerUnreachableError(f"no binding registered for {agent_instance_id!r}")
        return binding

    def resolve_by_agent_instance_id(self, agent_instance_id: str) -> BridgeBinding | None:
        return self._by_instance.get(agent_instance_id)


# --- wake_capable spool-tee fixtures (codex-watch-migration, 2026-08-06) ---
# Sibling coverage to drive_on_delivery above, through the SAME dispatch_peer_send
# seam -- see peer_dispatch.py's _tee_spool_if_wake_incapable.

_WC_SESSION_ID = "ases-wc-test-101-22222"
_WC_HOMUNCULUS_NAME = "wc-testling"


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
    peer_id/instance hint -- these legs test the tee's OWN guard, not
    resolution, which the existing _FakePeerRegistry already covers."""

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
    state: StateManagementInterface, binding: BridgeBinding, *, content: str,
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
    return default_spool_path(_WC_HOMUNCULUS_NAME, instance_id)


def _wc_invoke_wake(spool: Path, *, max_wait: float = 0.2) -> Any:
    with patch.object(wake_mod, "resolve_homunculus_name", lambda: _WC_HOMUNCULUS_NAME):
        return CliRunner().invoke(
            wake_mod.wake,
            ["--spool", str(spool), "--max-wait", str(max_wait)],
            env={"AGENT_SESSION_LABEL": "WC-Recipient", "AGENT_SESSION_ID": _WC_SESSION_ID},
            obj={},
        )


def test_wake_capable_recipient_never_tees() -> None:
    """Leg 1 -- claude-office no-op. Must NOT touch the existing native-wake/
    append_event assertions above (it doesn't -- this is a new, independent
    test using its own fixtures). Named failing mutation: removing the
    ``if recipient.wake_capable: return`` guard at the top of
    ``_tee_spool_if_wake_incapable`` -- must red (a spool file appears where
    none should, since this binding's wake_capable=True is the ONLY thing
    stopping the write)."""
    spool = _wc_expected_spool_path()
    spool.unlink(missing_ok=True)
    with patch.dict(os.environ, {"HOMUNCULUS_NAME": _WC_HOMUNCULUS_NAME}):
        state = _state()
        outcome, manager = _send_wc(
            state, _wc_binding(wake_capable=True), content="IMPORTANT: wc capable ping",
        )
    _check(not spool.exists(), "wake_capable=true recipient: no spool file created")
    _check(
        len(manager.events) == 1,
        "wake_capable=true: the existing channel-event notify still fires unchanged",
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
        "wake_capable=true: delivery_kind untouched by the (no-op) tee",
    )


def test_watcher_recipient_no_second_tee() -> None:
    """Leg 2 -- bridge-less-worker unchanged. A watch-registered binding
    (``is_watcher`` True via the ``agi-watch-`` prefix; ``wake_capable``
    defaults True since nothing sets it False for a real watcher
    registration) must get NO write from this new mechanism -- its own live
    watch process is already its spool feeder, and a tee here would be a
    SECOND, colliding writer. Named failing mutation: same guard removal as
    leg 1 -- proves this recipient class is protected by the same guard, not
    by some other coincidental gate."""
    watcher_instance_id = f"agi-watch-{watch_instance_digest('ases-watcher-session')}"
    binding = BridgeBinding(
        bridge_id="agc-wc-watcher",
        agent_id="claude_code",
        agent_instance_id=watcher_instance_id,
        session_label="Watcher",
        parent_pid=4242,
        agent_session_id="ases-watcher-session",
    )
    _check(binding.is_watcher, "fixture sanity: this binding IS a watcher binding")
    _check(binding.wake_capable, "fixture sanity: wake_capable defaults True, unset by this binding")
    spool = default_spool_path(_WC_HOMUNCULUS_NAME, watcher_instance_id)
    spool.unlink(missing_ok=True)
    with patch.dict(os.environ, {"HOMUNCULUS_NAME": _WC_HOMUNCULUS_NAME}):
        state = _state()
        outcome, manager = _send_wc(state, binding, content="IMPORTANT: wc watcher ping")
    _check(
        not spool.exists(),
        "watcher recipient: this dispatch-side mechanism writes NOTHING (a real live "
        "watch process would tee this delivery itself -- not simulated here, and this "
        "mechanism must never duplicate it)",
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_WATCHER,
        "watcher recipient: existing delivery_kind (queued_watcher) untouched",
    )
    _check(len(manager.events) == 1, "watcher recipient: existing notify still fires unchanged")


def test_wake_incapable_recipient_tees_and_real_wake_consumes() -> None:
    """Leg 3 -- codex-class tee. Proves the write and read sides actually
    COMPOSE against the REAL ``wake.py`` entry point, not a re-implemented
    reader. Named failing mutation: removing the
    ``_tee_spool_if_wake_incapable(recipient, delivered_prose)`` call site in
    ``dispatch_peer_send`` -- must red on both the spool-file assertion and
    the real ``wake()`` invocation's exit code."""
    spool = _wc_expected_spool_path()
    spool.unlink(missing_ok=True)
    spool_offset_path(spool).unlink(missing_ok=True)
    with patch.dict(os.environ, {"HOMUNCULUS_NAME": _WC_HOMUNCULUS_NAME}):
        state = _state()
        outcome, manager = _send_wc(
            state, _wc_binding(wake_capable=False), content="IMPORTANT: codex wc ping",
        )
    _check(
        spool.exists(),
        "wake_capable=false: a spool file appears at the DERIVED default path -- proving "
        "dispatch and wake.py agree on the path with zero handshake",
    )
    if spool.exists():
        line = spool.read_text(encoding="utf-8").strip()
        _check(bool(line), "the spool line is non-empty")
        try:
            parsed: Any = json.loads(line)
        except json.JSONDecodeError:
            parsed = None
        _check(
            isinstance(parsed, dict) and "codex wc ping" in json.dumps(parsed),
            f"the tee'd line carries the delivered prose (got {line!r})",
        )
    _check(
        outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
        "wake_capable=false: existing delivery_kind untouched by the tee",
    )
    _check(
        len(manager.events) == 1,
        "wake_capable=false: existing channel-event notify STILL fires ALONGSIDE the tee",
    )
    result = _wc_invoke_wake(spool)
    _check(
        result.exit_code == wake_mod.WAKE_EXIT_SIGNAL,
        f"the REAL wake.py entry point unblocks on the dispatch-written spool "
        f"(exit {result.exit_code}, output {result.output!r})",
    )
    _check("codex wc ping" in result.stderr, "the real wake packet surfaces the tee'd content")


class _FakeRoleMessagingService:
    """Minimal persist_role_message stand-in, same shape as role_dispatch_smoke.py's
    own _FakeService -- dispatch_role_send's persist/notify logic is not under
    test here, only the wake_capable tee riding after it."""

    def persist_role_message(self, **kwargs: Any) -> RoleMessagePersisted:
        return RoleMessagePersisted(message_id=str(kwargs["message_id"]), created_at="")


def test_wake_incapable_role_send_also_tees() -> None:
    """dispatch_role_send parity leg. Role-addressed sends
    (``peer_send_by_name``) are the PRIMARY way an office is reached -- this
    proves the tee fires on that call site independently, not merely by
    resemblance to the dispatch_peer_send legs above. Named failing
    mutation: removing the ``_tee_spool_if_wake_incapable(recipient,
    delivered_prose)`` call site INSIDE ``dispatch_role_send`` (not the
    ``dispatch_peer_send`` one already proven by legs 3-4) must red EXACTLY
    this leg and nothing else -- that exactness is what proves the two call
    sites are independently covered rather than one shadowing the other."""
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
    with patch.dict(os.environ, {"HOMUNCULUS_NAME": _WC_HOMUNCULUS_NAME}):
        state = _state()
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
        spool.exists(),
        "role-send to wake_capable=false recipient: spool file appears at the "
        "derived default path",
    )
    if spool.exists():
        line = spool.read_text(encoding="utf-8").strip()
        _check(
            "role wc ping" in line,
            f"the tee'd line carries the role-send prose (got {line!r})",
        )
    _check(
        outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
        "role-send: existing delivery_kind untouched by the tee",
    )
    _check(
        len(manager.events) == 1,
        "role-send: the existing channel-event notify still fires ALONGSIDE the tee",
    )
    result = _wc_invoke_wake(spool)
    _check(
        result.exit_code == wake_mod.WAKE_EXIT_SIGNAL,
        f"the REAL wake.py entry point unblocks on the role-send-written spool "
        f"(exit {result.exit_code}, output {result.output!r})",
    )
    _check("role wc ping" in result.stderr, "the real wake packet surfaces the role-send content")


def test_wake_incapable_recipient_second_delivery_also_consumable() -> None:
    """Leg 4 -- lifecycle-advancing leg
    ([[feedback_a_lifecycle_claim_needs_a_fixture_that_advances]]). A SECOND
    delivery to the same wake_capable=false recipient, after the FIRST has
    already been consumed by a real ``wake()`` call, must also tee and be
    independently consumable -- proves this is a live, repeatable mechanism,
    not a one-shot fixture artifact that happens to pass once. Named failing
    mutation: an emit/commit ordering bug on the reader side that only
    manifests against a SECOND producer write would pass leg 3 alone but
    fail here."""
    spool = _wc_expected_spool_path()
    spool.unlink(missing_ok=True)
    spool_offset_path(spool).unlink(missing_ok=True)
    with patch.dict(os.environ, {"HOMUNCULUS_NAME": _WC_HOMUNCULUS_NAME}):
        state = _state()
        _send_wc(state, _wc_binding(wake_capable=False), content="IMPORTANT: first wc ping")
        first = _wc_invoke_wake(spool)
        _check(
            first.exit_code == wake_mod.WAKE_EXIT_SIGNAL,
            "leg 4 setup: the first delivery's real wake() consumes cleanly",
        )
        _send_wc(state, _wc_binding(wake_capable=False), content="IMPORTANT: second wc ping")
    second = _wc_invoke_wake(spool)
    _check(
        second.exit_code == wake_mod.WAKE_EXIT_SIGNAL,
        f"a SECOND independent delivery also tees and wakes (exit {second.exit_code})",
    )
    _check("second wc ping" in second.stderr, "the second wake surfaces the NEW content")
    _check(
        "first wc ping" not in second.stderr,
        "the second wake does not re-surface already-consumed content",
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
        self, bridge_id: str, event: str, prose: str, meta: dict[str, object],
    ) -> None:
        self.events.append((bridge_id, event, prose, meta))


def _send(state: StateManagementInterface) -> tuple[Any, _FakeBridgeManager]:
    manager = _FakeBridgeManager()
    registry = _FakePeerRegistry()
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


def _insert(
    state: StateManagementInterface, *, host: str = _TEST_HOST,
    agent_instance_id: str = _RECIPIENT_AGI,
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id="lane-dod", brief_ref="",
            work_class="read_only", budget_line="b1", host=host,
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
                state, agent_instance_id=_RECIPIENT_AGI, from_state=LIFECYCLE_SPAWNING,
                to_state=LIFECYCLE_LIVE, directed_by="test:none",
            )
            if eligible_state != LIFECYCLE_LIVE:
                transition_lifecycle_state(
                    state, agent_instance_id=_RECIPIENT_AGI, from_state=LIFECYCLE_LIVE,
                    to_state=eligible_state, directed_by="test:none",
                )
            outcome, manager = _send(state)
            _check(
                len(driver.channel.sent) == 1 and "drain peer_inbox" in driver.channel.sent[0],
                f"({eligible_state}) drive-on-delivery notice reaches the channel",
            )
            _check(
                "IMPORTANT: ping" not in driver.channel.sent[0]
                and "ping" not in driver.channel.sent[0],
                f"({eligible_state}) the channel notice is NOT the message body",
            )
            _check(
                len(manager.events) == 1,
                f"({eligible_state}) the existing channel-event notify STILL fires "
                "(ALONGSIDE, not replaced)",
            )
            _check(
                outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
                f"({eligible_state}) delivery_kind is untouched by the drive nudge",
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
                    state, agent_instance_id=_RECIPIENT_AGI, from_state=LIFECYCLE_SPAWNING,
                    to_state=LIFECYCLE_LIVE, directed_by="test:none",
                )
                transition_lifecycle_state(
                    state, agent_instance_id=_RECIPIENT_AGI, from_state=LIFECYCLE_LIVE,
                    to_state=ineligible_state, directed_by="test:none",
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
            state, agent_instance_id=_RECIPIENT_AGI, from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="test:none",
        )
        raised = False
        outcome = None
        try:
            outcome, manager = _send(state)
        except Exception:  # noqa: BLE001 — the whole point is it must NOT raise
            raised = True
        _check(not raised, "a raising driver channel does not propagate out of dispatch_peer_send")
        _check(
            outcome is not None and outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
            "the caller's own delivery outcome is untouched by the channel fault",
        )
        _check(driver.channel.sent == [], "nothing recorded as sent (the raise happened inside send)")
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
            state, agent_instance_id=_RECIPIENT_AGI, from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="test:none",
        )
        report_by_before = read_managed_session(state, _RECIPIENT_AGI).get("report_by")
        _send(state)
        report_by_after = read_managed_session(state, _RECIPIENT_AGI).get("report_by")
        _check(
            report_by_before == report_by_after,
            "report_by is byte-identical before/after a drive-on-delivery notice "
            f"(before={report_by_before!r}, after={report_by_after!r})",
        )
    finally:
        _remove_fake_host()


def test_state_service_none_never_raises() -> None:
    """Optional-collaborator degrade (mirrors sweep_overdue_sessions's own
    convention): state_service=None must not crash dispatch_peer_send — the
    drive is best-effort and simply has nothing to drive without a state
    handle."""
    manager = _FakeBridgeManager()
    registry = _FakePeerRegistry()
    raised = False
    try:
        dispatch_peer_send(
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
            state, agent_instance_id=waiter, agent_id="claude_code",
            agent_session_id="ases-sweep-waiter01",
        )
        past_deadline = "2020-01-01T00:00:00+00:00"
        armed = verb_arm_session_dependency(
            state,
            ArmSessionDependencyRequest(
                waiter_instance_id=waiter, condition_kind=CONDITION_DEADLINE,
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


def test_sweep_overdue_steward_notice_unmanaged_steward_byte_unchanged() -> None:
    """Ruling 2 (2026-08-04 sign-off): the unmanaged-steward path (an
    operator-launched steward with no managed_session row of its own — the
    common case, e.g. the seat) must be byte-unchanged by this lane:
    drive_on_delivery's SessionNotFoundError no-op covers it silently, the
    existing steward notice still fires exactly as before."""
    overdue_worker = "agi-sweep-overdue-worker01"
    unmanaged_steward = "agi-sweep-unmanaged-steward01"
    state = _state()
    _insert(state, host="operator", agent_instance_id=overdue_worker)
    transition_lifecycle_state(
        state, agent_instance_id=overdue_worker, from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="test:none",
    )
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": overdue_worker}},
        {
            "spawned_by_instance_id": unmanaged_steward,
            "report_by": "2020-01-01T00:00:00+00:00",
        },
    )
    registry = _FakeMultiPeerRegistry()
    registry.register(unmanaged_steward, bridge_id="agc-unmanaged-steward")
    manager = _FakeBridgeManager()
    marked = sweep_overdue_sessions(state, peer_registry=registry, bridge_manager=manager)  # type: ignore[arg-type]
    _check(marked == 1, f"sweep_overdue_sessions marked exactly 1 row overdue (got {marked})")
    _check(
        len(manager.events) == 1 and manager.events[0][1] == EVENT_SESSION_OVERDUE_NOTICE,
        "the existing overdue-steward notice STILL fires — byte-unchanged for an unmanaged steward",
    )


def test_sweep_overdue_steward_notice_drives_a_managed_steward() -> None:
    """Companion to the byte-unchanged leg: a MANAGED steward (itself a
    spawned session with its own managed_session row) gets the extra
    drive-on-delivery nudge on the same steward-notice path."""
    overdue_worker = "agi-sweep-overdue-worker02"
    managed_steward = "agi-sweep-managed-steward01"
    driver = _install_fake_host()
    try:
        state = _state()
        _insert(state, agent_instance_id=managed_steward)
        transition_lifecycle_state(
            state, agent_instance_id=managed_steward, from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="test:none",
        )
        _insert(state, host="operator", agent_instance_id=overdue_worker)
        transition_lifecycle_state(
            state, agent_instance_id=overdue_worker, from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="test:none",
        )
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": "managed_session", "filters": {"agent_instance_id": overdue_worker}},
            {
                "spawned_by_instance_id": managed_steward,
                "report_by": "2020-01-01T00:00:00+00:00",
            },
        )
        registry = _FakeMultiPeerRegistry()
        registry.register(managed_steward, bridge_id="agc-managed-steward")
        manager = _FakeBridgeManager()
        marked = sweep_overdue_sessions(state, peer_registry=registry, bridge_manager=manager)  # type: ignore[arg-type]
        _check(marked == 1, f"sweep_overdue_sessions marked exactly 1 row overdue (got {marked})")
        _check(
            len(manager.events) == 1 and manager.events[0][1] == EVENT_SESSION_OVERDUE_NOTICE,
            "the existing overdue-steward notice still fires",
        )
        _check(
            len(driver.channel.sent) == 1,
            "a MANAGED steward's driver channel also receives a drive-on-delivery notice",
        )
    finally:
        _remove_fake_host()


def main() -> int:
    print("=== drive-on-delivery lane smoke (slices 1-2) ===")
    test_eligible_recipient_channel_receives_notice_alongside_notify()
    test_ineligible_recipient_channel_receives_nothing()
    test_raising_channel_never_fails_the_send()
    test_non_managed_recipient_unaffected()
    test_report_by_unchanged_after_drive_on_delivery()
    test_state_service_none_never_raises()
    test_sweep_deadline_edge_drives_a_live_managed_waiter()
    test_sweep_overdue_steward_notice_unmanaged_steward_byte_unchanged()
    test_sweep_overdue_steward_notice_drives_a_managed_steward()
    test_wake_capable_recipient_never_tees()
    test_watcher_recipient_no_second_tee()
    test_wake_incapable_recipient_tees_and_real_wake_consumes()
    test_wake_incapable_role_send_also_tees()
    test_wake_incapable_recipient_second_delivery_also_consumable()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
