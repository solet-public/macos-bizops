#!/usr/bin/env python3
"""Behavioral proof for managed-dispatch first-turn routing."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _recorded_lane_worktree_fixture import RecordedLaneWorktreeFixture  # noqa: E402
from _register_unit_double import (  # noqa: E402
    FIXTURE_UNIT_ID,
    RegisterUnitDouble,  # noqa: E402
    fixture_assignment,
)
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from managed_dispatch_smoke import (  # noqa: E402
    T0,
    _assignment_identity,
    _coordinator_actor,
    _dispatch_events,
    _first_turn,
    _prepare,
    _register,
    _ReplacementDriver,
    _seed_binding,
    _spec,
    _state,
    _worker_actor,
)

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DISPATCH_ACTIVE,
    DISPATCH_CANCELLED,
    DISPATCH_FAILED_START,
    DISPATCH_UPTAKE_PENDING,
    DispatchActor,
    DispatchError,
    DispatchSpec,
    dispatch_managed_work,
    managed_dispatch_status,
    mark_dispatch_worker_lost,
    prepare_managed_dispatch,
    read_managed_dispatch,
    report_managed_dispatch,
    resolve_managed_dispatch,
)
from agent_messaging_plugin.register_unit_client import RegisterActor, VerbError  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MANAGED_DISPATCH  # noqa: E402
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    FIRST_TURN_SOURCE_CHARTER,
    CaptureLaneCharterRequest,
    capture_lane_charter,
    spawn_dispatch_attempt,
)

_passed = 0


def _check(condition: object, label: str) -> None:
    global _passed
    if not condition:
        raise AssertionError(label)
    _passed += 1
    print(f"PASS {label}")


def _install_driver(host: str) -> tuple[tuple[str, str], object, _ReplacementDriver]:
    key = (session_hosts.AGENT_RUNTIME_CODEX, host)
    prior = session_hosts._REGISTRY.get(key)  # noqa: SLF001
    driver = _ReplacementDriver(fail_start=False)
    session_hosts._REGISTRY[key] = driver  # noqa: SLF001
    return key, prior, driver


def _restore_driver(key: tuple[str, str], prior: object) -> None:
    if prior is None:
        session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
    else:
        session_hosts._REGISTRY[key] = prior  # type: ignore[assignment]  # noqa: SLF001


def test_bootstrap_ack_protocol() -> None:
    first_turn = _render_ack_protocol_first_turn()
    _assert_ack_status_text(first_turn)
    _assert_ack_hash_text(first_turn)
    _assert_ack_report_text(first_turn)
    _assert_ack_stale_and_authority_text(first_turn)
    _assert_ack_peer_progress_text(first_turn)
    _assert_ack_milestone_text(first_turn)
    _assert_ack_completion_text(first_turn)
    _assert_ack_acceptance_and_event_readback()
    _assert_silent_worker_overdue()
    _assert_terminal_cancel_rejects_late_ack()
    _assert_retry_rejects_old_attempt_ack()


def _render_ack_protocol_first_turn() -> str:
    host = "managed-dispatch-ack-protocol-fixture"
    key, prior, driver = _install_driver(host)
    try:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            state = _state()
            spec = _spec(
                root,
                host=host,
                allowed_hosts=[host],
                local_name="Codex-Fixture-Builder",
                repository_root=raw,
            )
            register = _register(spec)
            dispatch_managed_work(
                state,
                spec,
                actor=_coordinator_actor(),
                spawn_attempt=lambda row: spawn_dispatch_attempt(
                    state,
                    row,
                    register=register,
                    actor=RegisterActor(
                        "agi-coordinator",
                        "ases-agi-coordinator",
                        "Coordinator-Main",
                        "fixture-lane",
                    ),
                ),
                register=register,
                now=T0,
            )
            return driver.channel.sent[0] if driver.channel.sent else ""
    finally:
        _restore_driver(key, prior)


def _assert_ack_status_text(first_turn: str) -> None:
    _check("managed_dispatch_status" in first_turn, "ACK_STATUS_ACQUISITION")
    _check(
        all(("dispatch_id" in first_turn, "version" in first_turn, "role_name" in first_turn)),
        "ACK_STATUS_VERSION_AND_ROLE",
    )
    _check(
        all(
            (
                "current_agent_instance_id" in first_turn,
                "assignment_sha256" in first_turn,
                "assignment.revision.revision_id" in first_turn,
            )
        ),
        "ACK_STATUS_ATTEMPT_AND_ASSIGNMENT",
    )


def _assert_ack_hash_text(first_turn: str) -> None:
    _check(
        all(
            (
                "SHA-256" in first_turn,
                "UTF-8" in first_turn,
                "scope_readback_sha256" in first_turn,
                "plan_sha256" in first_turn,
                "do not use supplied or default hashes" in first_turn,
            )
        ),
        "ACK_WORKER_TEXT_HASHES",
    )


def _assert_ack_report_text(first_turn: str) -> None:
    _check(
        all(
            name in first_turn
            for name in (
                "event_id",
                "unique `event_id`",
                "event_kind=ack",
                "attempt_agent_instance_id",
                "prior_version",
                "payload",
            )
        ),
        "ACK_REPORT_ARGUMENTS",
    )


def _assert_ack_stale_and_authority_text(first_turn: str) -> None:
    _check(
        all(
            (
                "stale_dispatch_version" in first_turn,
                "retry at most once" in first_turn,
                "fresh `event_id`" in first_turn,
            )
        ),
        "ACK_STALE_VERSION_RETRY_BOUND",
    )
    _check(
        all(
            (
                "preparing" in first_turn,
                "unknown" in first_turn,
                "uptake_uncertain" in first_turn,
                "authority mismatch" in first_turn,
                "cancel, retry, or spawn" in first_turn,
            )
        ),
        "ACK_STATE_AND_AUTHORITY_MISMATCH_REPORTING",
    )


def _assert_ack_peer_progress_text(first_turn: str) -> None:
    _check(
        all(
            (
                "informal peer" in first_turn.lower(),
                "do not substitute" in first_turn.lower(),
                "report_alive" in first_turn,
                "first_turn_delivered" in first_turn,
            )
        ),
        "ACK_PEER_PROGRESS_IS_NOT_UPTAKE",
    )


def _assert_ack_milestone_text(first_turn: str) -> None:
    _check(
        all(
            name in first_turn
            for name in (
                "completed_work",
                "current_evidence",
                "next_action",
                "next_report_deadline",
                "fresh status",
            )
        ),
        "ACK_MILESTONE_ARGUMENTS",
    )


def _assert_ack_completion_text(first_turn: str) -> None:
    _check(
        all(
            name in first_turn
            for name in (
                "completion_contract_sha256",
                "completion_contract",
                "expected_path",
                "artifact_path",
                "artifact_sha256",
                "evidence",
                "verdict",
                "prior_version",
                "completion_reported",
                "coordinator acceptance",
            )
        ),
        "ACK_COMPLETION_READBACK_AND_ACCEPTANCE",
    )


def _assert_ack_acceptance_and_event_readback() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        before = managed_dispatch_status(
            state,
            "mdp-fixture",
            now=T0 + timedelta(seconds=1),
        )
        scope_readback = "I will update the assigned driver text and existing smoke only."
        execution_plan = "Read the brief, implement the two fenced files, and run the named proof."
        attempt = str(before["current_agent_instance_id"])
        role = str(before["role_name"])
        scope_digest = hashlib.sha256(scope_readback.encode("utf-8")).hexdigest()
        plan_digest = hashlib.sha256(execution_plan.encode("utf-8")).hexdigest()
        try:
            accepted = report_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id="evt-bootstrap-ack",
                event_kind="ack",
                attempt_agent_instance_id=attempt,
                actor=_worker_actor(),
                prior_version=int(before["version"]),
                payload={
                    **_assignment_identity(before),
                    "role_binding": role,
                    "scope_readback_sha256": scope_digest,
                    "plan_sha256": plan_digest,
                },
                observed_at=T0 + timedelta(seconds=2),
            )
        except DispatchError as exc:
            if exc.code != "stale_dispatch_version":
                raise
            accepted = {"state": before["state"], "last_ack_at": None}
        ack_status = managed_dispatch_status(
            state,
            "mdp-fixture",
            now=T0 + timedelta(minutes=3),
        )
        ack_events = _dispatch_events(state, event_ids={"evt-bootstrap-ack"})
        _assert_ack_projection(accepted, ack_status)
        _assert_ack_event_identity(ack_events, attempt, role)
        _assert_ack_event_payload(ack_events, before, role, scope_digest, plan_digest)


def _assert_ack_projection(accepted: Mapping[str, Any], ack_status: Mapping[str, Any]) -> None:
    _check(
        all(
            (
                accepted["state"] == DISPATCH_ACTIVE,
                accepted.get("last_ack_at") is not None,
                ack_status["state"] == DISPATCH_ACTIVE,
                ack_status.get("last_ack_at") is not None,
                ack_status.get("overdue_condition") != "uptake_overdue",
            )
        ),
        "ACK_ACCEPTED_STATE_CLEARS_UPTAKE",
    )


def _assert_ack_event_identity(ack_events: list[dict[str, Any]], attempt: str, role: str) -> None:
    event = ack_events[0] if ack_events else {}
    _check(
        all(
            (
                len(ack_events) == 1,
                event.get("accepted") is True,
                event.get("event_id") == "evt-bootstrap-ack",
                event.get("event_kind") == "ack",
                event.get("attempt_agent_instance_id") == attempt,
                event.get("actor_role") == role,
                event.get("actor_instance_id") == attempt,
            )
        ),
        "ACK_EVENT_IDENTITY_READBACK",
    )


def _assert_ack_event_payload(
    ack_events: list[dict[str, Any]],
    before: Mapping[str, Any],
    role: str,
    scope_digest: str,
    plan_digest: str,
) -> None:
    event = ack_events[0] if ack_events else {}
    payload = event.get("payload", {})
    _check(
        all(
            (
                payload.get("assignment_sha256") == before["assignment_sha256"],
                payload.get("revision_id") == _assignment_identity(before)["revision_id"],
                payload.get("role_binding") == role,
                payload.get("scope_readback_sha256") == scope_digest,
                payload.get("plan_sha256") == plan_digest,
            )
        ),
        "ACK_EVENT_PAYLOAD_READBACK_MATCHES_WORKER_TEXT",
    )


def _assert_silent_worker_overdue() -> None:
    with tempfile.TemporaryDirectory() as raw:
        silent_state = _state()
        _prepare(silent_state, Path(raw))
        _first_turn(silent_state, delivered=True)
        silent = managed_dispatch_status(
            silent_state,
            "mdp-fixture",
            now=T0 + timedelta(minutes=3),
        )
        _check(
            all(
                (
                    silent["state"] == DISPATCH_UPTAKE_PENDING,
                    silent.get("overdue_condition") == "uptake_overdue",
                    silent.get("last_ack_at") is None,
                )
            ),
            "ACK_SILENT_WORKER_STAYS_OVERDUE",
        )


def _assert_terminal_cancel_rejects_late_ack() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        spec = _spec(Path(raw))
        register = _register(spec)
        prepare_managed_dispatch(state, spec, actor=_coordinator_actor(), register=register, now=T0)
        _first_turn(state, delivered=True)
        pending = read_managed_dispatch(state, spec.dispatch_id)
        cancelled = resolve_managed_dispatch(
            state,
            dispatch_id=spec.dispatch_id,
            event_id="evt-cancel-before-late-ack",
            action="cancel",
            actor=_coordinator_actor(),
            prior_version=int(pending["version"]),
            payload={"reason": "terminal late ACK fixture"},
            observed_at=T0 + timedelta(seconds=2),
            register=register,
        )
        rejected = ""
        try:
            report_managed_dispatch(
                state,
                dispatch_id=spec.dispatch_id,
                event_id="evt-late-cancelled-ack",
                event_kind="ack",
                attempt_agent_instance_id=str(cancelled["current_agent_instance_id"]),
                actor=_worker_actor(),
                prior_version=int(cancelled["version"]),
                payload={
                    **_assignment_identity(cancelled),
                    "role_binding": str(cancelled["role_name"]),
                    "scope_readback_sha256": "a" * 64,
                    "plan_sha256": "b" * 64,
                },
                observed_at=T0 + timedelta(seconds=3),
            )
        except DispatchError as exc:
            rejected = exc.code
        after = managed_dispatch_status(
            state,
            spec.dispatch_id,
            now=T0 + timedelta(seconds=4),
        )
        events = _dispatch_events(state, event_ids={"evt-late-cancelled-ack"})
        event = events[0] if events else {}
        _check(
            all(
                (
                    rejected == "ack_not_allowed",
                    after["state"] == DISPATCH_CANCELLED,
                    after["version"] == cancelled["version"],
                    after.get("last_ack_at") is None,
                )
            ),
            "ACK_TERMINAL_CANCEL_REJECTS_LATE_ACK",
        )
        _check(
            all(
                (
                    len(events) == 1,
                    event.get("accepted") is False,
                    event.get("rejection_code") == "ack_not_allowed",
                )
            ),
            "ACK_TERMINAL_LATE_ACK_REMAINS_AUDITED",
        )


def _assert_retry_rejects_old_attempt_ack() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        spec = _spec(Path(raw))
        register = _register(spec)
        prepare_managed_dispatch(state, spec, actor=_coordinator_actor(), register=register, now=T0)
        _first_turn(state, delivered=True)
        first_attempt = _worker_actor()
        original = _ack_first_attempt(state, spec, first_attempt)
        _check(
            all(
                (
                    original["state"] == DISPATCH_ACTIVE,
                    original["current_agent_instance_id"] == first_attempt.agent_instance_id,
                )
            ),
            "ACK_ORIGINAL_ATTEMPT_STARTS_ACTIVE",
        )
        lost, retried = _start_replacement_attempt(state, spec, register, first_attempt)
        replacement_ack = _ack_replacement_attempt(state, spec, retried)
        rejected = _reject_old_attempt_ack(state, spec, first_attempt, replacement_ack)
        after = managed_dispatch_status(
            state,
            spec.dispatch_id,
            now=T0 + timedelta(seconds=7),
        )
        events = _dispatch_events(
            state,
            event_ids={
                "evt-original-attempt-ack",
                "evt-replacement-attempt-ack",
                "evt-old-attempt-late-ack",
            },
        )
        _assert_replacement_projection(lost, retried, replacement_ack, rejected, after)
        _assert_retry_ack_history(events, first_attempt.agent_instance_id)


def _ack_first_attempt(
    state: StateManagementInterface,
    spec: DispatchSpec,
    actor: DispatchActor,
) -> dict[str, Any]:
    row = read_managed_dispatch(state, spec.dispatch_id)
    return report_managed_dispatch(
        state,
        dispatch_id=spec.dispatch_id,
        event_id="evt-original-attempt-ack",
        event_kind="ack",
        attempt_agent_instance_id=actor.agent_instance_id,
        actor=actor,
        prior_version=int(row["version"]),
        payload={
            **_assignment_identity(row),
            "role_binding": spec.role_name,
            "scope_readback_sha256": "a" * 64,
            "plan_sha256": "b" * 64,
        },
        observed_at=T0 + timedelta(seconds=2),
    )


def _start_replacement_attempt(
    state: StateManagementInterface,
    spec: DispatchSpec,
    register: RegisterUnitDouble,
    first_attempt: DispatchActor,
) -> tuple[dict[str, Any], dict[str, Any]]:
    lost = mark_dispatch_worker_lost(state, dispatch_id=spec.dispatch_id, agent_instance_id=first_attempt.agent_instance_id, observed_at=T0 + timedelta(seconds=3), detail="fixture transition before replacement attempt", register=None)

    def replacement(_row: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"agent_instance_id": "agi-replacement"}

    retried = resolve_managed_dispatch(
        state,
        dispatch_id=spec.dispatch_id,
        event_id="evt-retry-replacement-attempt",
        action="request_retry",
        actor=_coordinator_actor(),
        prior_version=int(lost["version"]),
        payload={"reason": "fixture replacement attempt"},
        observed_at=T0 + timedelta(seconds=4),
        register=register,
        spawn_attempt=replacement,
    )
    _seed_binding(
        state,
        role=spec.role_name,
        instance="agi-replacement",
        session="ases-replacement",
    )
    return lost, retried


def _ack_replacement_attempt(
    state: StateManagementInterface,
    spec: DispatchSpec,
    retried: Mapping[str, Any],
) -> dict[str, Any]:
    replacement_actor = DispatchActor(
        "agi-replacement",
        "ases-replacement",
        "live_peer_binding",
    )
    return report_managed_dispatch(
        state,
        dispatch_id=spec.dispatch_id,
        event_id="evt-replacement-attempt-ack",
        event_kind="ack",
        attempt_agent_instance_id="agi-replacement",
        actor=replacement_actor,
        prior_version=int(retried["version"]),
        payload={
            **_assignment_identity(retried),
            "role_binding": spec.role_name,
            "scope_readback_sha256": "c" * 64,
            "plan_sha256": "d" * 64,
        },
        observed_at=T0 + timedelta(seconds=5),
    )


def _reject_old_attempt_ack(
    state: StateManagementInterface,
    spec: DispatchSpec,
    first_attempt: DispatchActor,
    replacement_ack: Mapping[str, Any],
) -> str:
    try:
        report_managed_dispatch(
            state,
            dispatch_id=spec.dispatch_id,
            event_id="evt-old-attempt-late-ack",
            event_kind="ack",
            attempt_agent_instance_id=first_attempt.agent_instance_id,
            actor=first_attempt,
            prior_version=int(replacement_ack["version"]),
            payload={
                **_assignment_identity(replacement_ack),
                "role_binding": spec.role_name,
                "scope_readback_sha256": "e" * 64,
                "plan_sha256": "f" * 64,
            },
            observed_at=T0 + timedelta(seconds=6),
        )
    except DispatchError as exc:
        return exc.code
    return ""


def _assert_replacement_projection(
    lost: Mapping[str, Any],
    retried: Mapping[str, Any],
    replacement_ack: Mapping[str, Any],
    rejected: str,
    after: Mapping[str, Any],
) -> None:
    _check(
        all(
            (
                retried["attempt_number"] == lost["attempt_number"] + 1,
                retried["current_agent_instance_id"] == "agi-replacement",
                replacement_ack["state"] == DISPATCH_ACTIVE,
                rejected == "worker_authority_denied",
                after["state"] == DISPATCH_ACTIVE,
                after["current_agent_instance_id"] == "agi-replacement",
                after["version"] == replacement_ack["version"],
            )
        ),
        "ACK_OLD_ATTEMPT_CANNOT_CHANGE_REPLACEMENT",
    )


def _assert_retry_ack_history(events: list[dict[str, Any]], old_attempt: str) -> None:
    event_by_id = {str(event["event_id"]): event for event in events}
    original = event_by_id.get("evt-original-attempt-ack", {})
    replacement = event_by_id.get("evt-replacement-attempt-ack", {})
    late = event_by_id.get("evt-old-attempt-late-ack", {})
    _check(
        all(
            (
                len(events) == 3,
                original.get("accepted") is True,
                original.get("attempt_agent_instance_id") == old_attempt,
                replacement.get("accepted") is True,
                replacement.get("attempt_agent_instance_id") == "agi-replacement",
                late.get("accepted") is False,
                late.get("attempt_agent_instance_id") == old_attempt,
                late.get("rejection_code") == "worker_authority_denied",
            )
        ),
        "ACK_OLD_ATTEMPT_HISTORY_REMAINS_APPEND_ONLY",
    )


def test_bound_dispatch_first_turn_starts_work() -> None:
    host = "managed-dispatch-first-turn-fixture"
    key, prior, driver = _install_driver(host)
    try:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            state = _state()
            spec = _spec(
                root,
                host=host,
                allowed_hosts=[host],
                local_name="Codex-Fixture-Builder",
                repository_root=raw,
            )
            register = _register(spec)
            prepare_managed_dispatch(state, spec, actor=_coordinator_actor(), register=register, now=T0)
            register.rebind(
                spec.unit_id,
                replace(
                    fixture_assignment(text="new plan revision must not replace the assignment").revision,
                    revision_id="drv_new-plan-revision",
                ),
            )
            result = dispatch_managed_work(
                state,
                spec,
                actor=_coordinator_actor(),
                spawn_attempt=lambda row: spawn_dispatch_attempt(
                    state,
                    row,
                    register=register,
                    actor=RegisterActor(
                        "agi-coordinator",
                        "ases-agi-coordinator",
                        "Coordinator-Main",
                        "fixture-lane",
                    ),
                ),
                register=register,
                now=T0,
            )
            duplicate = dispatch_managed_work(
                state,
                spec,
                actor=_coordinator_actor(),
                register=register,
                now=T0,
                spawn_attempt=lambda row: spawn_dispatch_attempt(
                    state,
                    row,
                    register=register,
                    actor=RegisterActor(
                        "agi-coordinator",
                        "ases-agi-coordinator",
                        "Coordinator-Main",
                        "fixture-lane",
                    ),
                ),
            )
            _check(
                duplicate["attempt"] == result["attempt"] and len(driver.channel.sent) == 1,
                "duplicate request retains one host attempt after a plan edit",
            )
            _check(not any(detail["event"] == "uptake_uncertain" for _unit, detail in register.events), "ANNOTATION_DELIVERED_ZERO_UNCERTAIN")
            first_turn = driver.channel.sent[0] if driver.channel.sent else ""
            _check(
                "exact immutable brief\n" in first_turn and "new plan revision" not in first_turn,
                "host first turn retains the pinned registered bytes after plan edits",
            )
            _check(
                result["attempt"]["first_turn_source"] == FIRST_TURN_SOURCE_CHARTER,
                "managed dispatch selects the charter first turn",
            )
            _check(
                all(
                    item in first_turn
                    for item in (
                        spec.dispatch_id,
                        str(result["dispatch"]["unit_id"]),
                        "drv_fixture-revision",
                        str(result["dispatch"]["assignment_sha256"]),
                    )
                ),
                "first turn carries dispatch, ensured Unit, brief reference, and digest",
            )
            _check(
                "CLAIM YOUR ROLE BINDING FIRST" in first_turn and "report_managed_dispatch" in first_turn and "event_kind=ack" in first_turn,
                "first turn claims the role and instructs native managed-dispatch ACK",
            )
            _check(
                "After accepted ACK readback, begin the assigned work." in first_turn and "awaiting dispatch" not in first_turn,
                "first turn starts the assigned brief without a separate driving message",
            )
            _check(
                result["dispatch"]["first_turn_source"] == FIRST_TURN_SOURCE_CHARTER,
                "managed dispatch persists charter as the first-turn source",
            )
    finally:
        _restore_driver(key, prior)


def test_dispatch_row_overrides_unrelated_lane_charter() -> None:
    host = "managed-dispatch-row-only-fixture"
    key, prior, driver = _install_driver(host)
    try:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            state = _state()
            spec = _spec(
                root,
                host=host,
                allowed_hosts=[host],
                local_name="Codex-Fixture-Builder",
                repository_root=raw,
            )
            capture_lane_charter(
                state,
                CaptureLaneCharterRequest(
                    lane_id=spec.lane_id,
                    charter_text="UNBOUND_LANE_CHARTER_MUST_NOT_DRIVE_MANAGED_WORK",
                    captured_at=T0.isoformat(),
                    unit_id=FIXTURE_UNIT_ID,
                    revision_id="drv_fixture-revision",
                    directed_by="operator:none",
                ),
            )
            register = _register(spec)
            dispatch_managed_work(
                state,
                spec,
                actor=_coordinator_actor(),
                spawn_attempt=lambda row: spawn_dispatch_attempt(
                    state,
                    row,
                    register=register,
                    actor=RegisterActor(
                        "agi-coordinator",
                        "ases-agi-coordinator",
                        "Coordinator-Main",
                        "fixture-lane",
                    ),
                ),
                register=register,
                now=T0,
            )
            first_turn = driver.channel.sent[0] if driver.channel.sent else ""
            _check(
                "UNBOUND_LANE_CHARTER_MUST_NOT_DRIVE_MANAGED_WORK" not in first_turn,
                "managed first turn ignores lane charter text outside its dispatch row",
            )
            _check(
                "exact immutable brief\n" in first_turn and "drv_fixture-revision" in first_turn,
                "managed first turn retains the row-bound brief and digest",
            )
    finally:
        _restore_driver(key, prior)


def test_missing_dispatch_brief_fails_before_spawn() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        spec = _spec(root)
        state = _state()
        _prepare(state, root)
        register = _register(spec)
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_DISPATCH, "filters": {"dispatch_id": spec.dispatch_id}},
            {"assignment": None, "next_required_action": "spawn_worker"},
        )
        error_code = ""
        try:
            spawn_dispatch_attempt(
                state,
                read_managed_dispatch(state, spec.dispatch_id),
                register=register,
                actor=RegisterActor("agi-coordinator", "ases-agi-coordinator", "Coordinator-Main", "fixture-lane"),
            )
        except DispatchError as exc:
            error_code = exc.code
        _check(
            error_code == "unit_assignment_historical",
            "malformed managed brief is refused before any worker can receive fallback",
        )


def test_native_managed_same_host_owner_recovery() -> None:
    from unittest.mock import patch

    import agent_messaging_plugin.session_hosts as hosts
    import agent_messaging_plugin.session_lifecycle_verbs as verbs
    from agent_messaging_plugin.lane_worktrees import LaneWorktree

    for mode in ("exact_request", "request_retry"):
        with tempfile.TemporaryDirectory() as raw:
            state = _state()
            root = Path(raw)
            spec = _spec(root)
            register = _register(spec)
            register.owners[spec.unit_id] = "act-main"
            register.transfer_error = "unit_register_unavailable"
            driver = _ReplacementDriver(fail_start=False)
            actor = RegisterActor("agi-coordinator", "ases-coordinator", "Coordinator-Main", spec.lane_id)
            with (
                patch.dict(hosts._REGISTRY, {("codex", "headless"): driver}),  # noqa: SLF001
                patch.object(
                    verbs,
                    "_provision_worktree_for_request",
                    return_value=LaneWorktree(root, root, root, "fixture"),
                ),
            ):

                def launch(
                    row: Mapping[str, Any],
                    state: StateManagementInterface = state,
                    register: RegisterUnitDouble = register,
                    actor: RegisterActor = actor,
                ) -> dict[str, Any]:
                    return verbs.spawn_dispatch_attempt(state, row, register=register, actor=actor)

                try:
                    dispatch_managed_work(
                        state,
                        spec,
                        register=register,
                        actor=_coordinator_actor(),
                        spawn_attempt=launch,
                        now=T0,
                    )
                except DispatchError as exc:
                    _check(
                        exc.code == "spawn_owner_transfer_failed",
                        f"NATIVE_MANAGED_PARTIAL_OWNER_FAILURE {mode}",
                    )
                else:
                    raise AssertionError("NATIVE_MANAGED_PARTIAL_OWNER_FAILURE did not refuse")
                failed = read_managed_dispatch(state, spec.dispatch_id)
                _check(
                    failed["state"] == DISPATCH_FAILED_START and not driver.channel.sent and register.owners[spec.unit_id] == "act-main",
                    f"NATIVE_MANAGED_PARTIAL_NO_FIRSTTURN {mode}",
                )
                register.transfer_error = ""
                recovered = _recover_native_managed_owner(state, spec, register, launch, mode, failed)
                instance = driver.spawned_instances[0]
                _assert_native_same_host_recovery(recovered, failed, driver, register, spec, instance, mode)
                replay = dispatch_managed_work(
                    state,
                    spec,
                    register=register,
                    actor=_coordinator_actor(),
                    spawn_attempt=launch,
                    now=T0,
                )
                _check(
                    replay["attempt"]["agent_instance_id"] == instance and len(driver.spawned_instances) == 1,
                    f"NATIVE_MANAGED_RECOVERY_REPLAY {mode}",
                )


def _recover_native_managed_owner(
    state: StateManagementInterface,
    spec: DispatchSpec,
    register: RegisterUnitDouble,
    launch: Any,
    mode: str,
    failed: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        if mode == "exact_request":
            return dispatch_managed_work(
                state,
                spec,
                register=register,
                actor=_coordinator_actor(),
                spawn_attempt=launch,
                now=T0,
            )["dispatch"]
        kwargs: dict[str, Any] = {
            "dispatch_id": spec.dispatch_id,
            "event_id": "evt-native-owner-recovery",
            "action": "request_retry",
            "actor": _coordinator_actor(),
            "prior_version": int(failed["version"]),
            "payload": {"reason": "retry retained host ownership"},
            "observed_at": T0 + timedelta(minutes=1),
            "register": register,
            "spawn_attempt": launch,
        }
        recovered = resolve_managed_dispatch(state, **kwargs)
        duplicate = resolve_managed_dispatch(state, **kwargs)
        _check(duplicate == recovered, "NATIVE_MANAGED_SAME_HOST_RECOVERY duplicate native retry event")
        return recovered
    except (DispatchError, VerbError) as exc:
        raise AssertionError(f"NATIVE_MANAGED_SAME_HOST_RECOVERY {mode}: {exc.code}") from exc


def _assert_native_same_host_recovery(
    recovered: Mapping[str, Any],
    failed: Mapping[str, Any],
    driver: _ReplacementDriver,
    register: RegisterUnitDouble,
    spec: DispatchSpec,
    instance: str,
    mode: str,
) -> None:
    _check(
        all(
            (
                recovered["state"] == DISPATCH_UPTAKE_PENDING,
                recovered["current_agent_instance_id"] == instance,
                recovered["attempt_number"] == failed["attempt_number"],
                len(driver.spawned_instances) == len(driver.channel.sent) == len(register.owner_changes) == 1,
                register.owners[spec.unit_id] == f"act-{instance}",
                recovered["assignment_sha256"] == failed["assignment_sha256"],
            )
        ),
        f"NATIVE_MANAGED_SAME_HOST_RECOVERY {mode}: same actor/host/attempt, owner before firstturn, pinned Unit",
    )


def test_causal_uncertain_annotation_fresh_and_retained() -> None:
    """Use the original outer request, real owner recovery and first-turn writer."""
    from unittest.mock import patch

    import agent_messaging_plugin.session_lifecycle_verbs as verbs
    from agent_messaging_plugin.lane_worktrees import LaneWorktree

    for retained in (False, True):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = _state()
            spec = _spec(root)
            register = _register(spec)
            driver = _ReplacementDriver(fail_start=False)
            actor = RegisterActor("agi-coordinator", "ases-coordinator", "Coordinator-Main", spec.lane_id)
            register.transfer_error = "unit_register_unavailable" if retained else ""
            with (
                patch.dict(session_hosts._REGISTRY, {("codex", "headless"): driver}),  # noqa: SLF001
                patch.object(verbs, "_provision_worktree_for_request", return_value=LaneWorktree(root, root, root, "fixture")),
                patch.object(driver.channel, "send", side_effect=RuntimeError("exact submission fault")),
            ):

                def launch(row: Mapping[str, Any], state: StateManagementInterface = state, register: RegisterUnitDouble = register, actor: RegisterActor = actor) -> Mapping[str, Any]:
                    return spawn_dispatch_attempt(state, row, register=register, actor=actor)

                if retained:
                    try:
                        dispatch_managed_work(state, spec, register=register, actor=_coordinator_actor(), spawn_attempt=launch, now=T0)
                    except DispatchError as exc:
                        _check(exc.code == "spawn_owner_transfer_failed", "ANNOTATION_RETAINED_OWNER_FAILURE_REACHED")
                    else:
                        raise AssertionError("expected owner-transfer failure")
                    failed = read_managed_dispatch(state, spec.dispatch_id)
                    _check(len(driver.spawned_instances) == 1, "ANNOTATION_RETAINED_ONE_HOST_BEFORE_RECOVERY")
                    register.transfer_error = ""
                result = dispatch_managed_work(state, spec, register=register, actor=_coordinator_actor(), spawn_attempt=launch, now=T0)
                observed = [(detail, register.event_actors[index]) for index, (_unit, detail) in enumerate(register.events) if detail["event"] == "uptake_uncertain"]
                _check(len(observed) == 1, "ANNOTATION_UNCERTAIN_EXACTLY_ONE")
                row = result["dispatch"]
                _assert_uncertain_annotation(observed[0], row, actor, driver)
                if retained:
                    _check(row["attempt_receipt"]["annotation_source"] == failed["attempt_receipt"]["annotation_source"], "ANNOTATION_RETAINED_IMMUTABLE_SOURCE")
                before = len(register.events)
                dispatch_managed_work(state, spec, register=register, actor=_coordinator_actor(), spawn_attempt=launch, now=T0)
                _check(len(register.events) == before, "ANNOTATION_DUPLICATE_ZERO_OBSERVATIONS")


def _assert_uncertain_annotation(
    observed: tuple[dict[str, Any], RegisterActor],
    row: Mapping[str, Any],
    actor: RegisterActor,
    driver: _ReplacementDriver,
) -> None:
    detail, recorded_actor = observed
    _check(row["state"] == "uptake_uncertain" and row["first_turn_error"] == "exact submission fault" and not row.get("last_ack_at"), "ANNOTATION_ORIGINAL_OUTCOME")
    _check(recorded_actor == actor and detail["agent_instance_id"] == driver.spawned_instances[0], "ANNOTATION_EXACT_SOURCE_ACTOR_WORKER")
    _check(detail["assignment_sha256"] == row["assignment_sha256"] and detail["attempt_number"] == 1, "ANNOTATION_EXACT_ASSIGNMENT_ATTEMPT")
    _check(detail["causal_event_id"] == "first-turn:1" and detail["transition_version"] == detail["prior_version"] + 1, "ANNOTATION_CAUSAL_VERSIONS")
    _check(len(driver.spawned_instances) == 1, "ANNOTATION_ONE_HOST_AFTER_RECOVERY")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        with RecordedLaneWorktreeFixture(Path(raw)):
            test_causal_uncertain_annotation_fresh_and_retained()
            test_bootstrap_ack_protocol()
            test_native_managed_same_host_owner_recovery()
            test_bound_dispatch_first_turn_starts_work()
            test_dispatch_row_overrides_unrelated_lane_charter()
            test_missing_dispatch_brief_fails_before_spawn()
    print(f"managed dispatch first-turn smoke: {_passed} passed, 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
