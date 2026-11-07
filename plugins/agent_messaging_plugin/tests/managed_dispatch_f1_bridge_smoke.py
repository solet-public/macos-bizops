#!/usr/bin/env python3
"""F1 legacy compatibility and F2 logical lifetime-removal regressions."""

from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from _register_unit_double import fixture_assignment  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
)
from managed_dispatch_smoke import (  # noqa: E402
    T0,
    _ack,
    _coordinator_actor,
    _dispatch_events,
    _first_turn,
    _prepare,
    _state,
    _worker_actor,
)

from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DISPATCH_ACTIVE,
    DISPATCH_BLOCKED_INTERNAL,
    DISPATCH_COMPLETED,
    DISPATCH_EXPIRED,
    DispatchError,
    _canonical_sha256,
    _replay_event,
    _write_event,
    managed_dispatch_events,
    managed_dispatch_inventory,
    managed_dispatch_status,
    read_managed_dispatch,
    record_first_turn_evidence,
    report_managed_dispatch,
    resolve_managed_dispatch,
    supervise_managed_dispatches,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_DISPATCH,
    TABLE_MANAGED_DISPATCH_EVENT,
    get_managed_dispatch_schema,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    list_sessions,
    report_alive,
    session_status,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def test_schema_keeps_nullable_columns_but_writers_omit_values() -> None:
    schema = get_managed_dispatch_schema()
    legacy_columns = ("ttl_seconds", "expires_at", "expires_at_window_seconds")
    _check(
        all(not schema.columns[name].not_null for name in legacy_columns),
        "F1 schema declares all three legacy managed-dispatch TTL columns nullable",
    )
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _prepare(state, Path(raw))
        persisted = cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0]
        _check(
            all(name not in row and name not in persisted for name in legacy_columns),
            "F2 prepare writer omits every legacy TTL value",
        )


def test_null_ttl_row_is_readable_and_never_expires() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        active = _ack(state)
        persisted = cast(RealShapeState, state).rows(
            AGENT_ROLE_BINDING_NAMESPACE,
            TABLE_MANAGED_DISPATCH,
        )[0]
        for name in ("ttl_seconds", "expires_at", "expires_at_window_seconds"):
            persisted[name] = None

        milestone = report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-null-ttl-milestone",
            event_kind="milestone",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=int(active["version"]),
            payload={
                "completed_work": ["nullable read path"],
                "current_evidence": {"legacy_ttl": None},
                "next_action": "exercise blocker path",
                "next_report_deadline": (T0 + timedelta(hours=5, minutes=15)).isoformat(),
            },
            observed_at=T0 + timedelta(hours=5),
        )
        blocked = report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-null-ttl-blocker",
            event_kind="blocked",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=int(milestone["version"]),
            payload={
                "blocker_class": "internal",
                "blocker_owner": "Coordinator-Main",
                "question": "Confirm the bridge row remains readable?",
                "safe_options": ["resolve"],
                "evidence": {"legacy_ttl": None},
                "decision_due_at": (T0 + timedelta(hours=5, minutes=10)).isoformat(),
            },
            observed_at=T0 + timedelta(hours=5, minutes=1),
        )
        supervised = supervise_managed_dispatches(state, now=T0 + timedelta(days=30))
        status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(days=30))
        _check(
            blocked["state"] == DISPATCH_BLOCKED_INTERNAL,
            "F1 milestone and blocker validators accept a missing legacy expiry",
        )
        _check(
            not any(item["condition"] == "ttl_expired" for item in supervised["conditions"]),
            "F1 supervisor never treats a missing legacy expiry as elapsed",
        )
        _check(
            "expires_at" not in status and "expires_at" not in status["deadline_overdue"] and status["state"] != DISPATCH_EXPIRED,
            "F2 status omits legacy expiry without inventing overdue or expiry",
        )


def test_dispatch_inventory_and_events_are_tie_safe_pages() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        for index in range(3):
            dispatch_id = f"mdp-page-{index}"
            _prepare(state, Path(raw), dispatch_id=dispatch_id)
            _write_event(
                state,
                dispatch_id=dispatch_id,
                event_id=f"evt-page-{index}",
                event_kind="fixture",
                attempt_agent_instance_id="",
                actor_role="fixture",
                actor_instance_id="",
                prior_version=0,
                observed_at=T0,
                payload={"index": index},
                accepted=True,
            )

        first_inventory = managed_dispatch_inventory(state, limit=2)
        inventory_cursor = cast(dict[str, str], first_inventory["next_cursor"])
        second_inventory = managed_dispatch_inventory(
            state,
            limit=2,
            after_created_at=inventory_cursor["created_at"],
            after_id=inventory_cursor["id"],
        )
        inventory_ids = [str(row["dispatch_id"]) for row in first_inventory["dispatches"] + second_inventory["dispatches"]]
        _check(
            len(inventory_ids) == 3 and len(set(inventory_ids)) == 3,
            "F1 inventory cursor enumerates same-timestamp rows without gaps or repeats",
        )
        _check(
            first_inventory["truncated"] is True and second_inventory["truncated"] is False and second_inventory["next_cursor"] is None,
            "F1 inventory paging exposes an explicit terminal page",
        )

        first_events = managed_dispatch_events(state, limit=2)
        event_cursor = cast(dict[str, str], first_events["next_cursor"])
        second_events = managed_dispatch_events(
            state,
            limit=2,
            after_event_at=event_cursor["event_at"],
            after_id=event_cursor["id"],
        )
        event_ids = [str(row["event_id"]) for row in first_events["events"] + second_events["events"]]
        _check(
            len(event_ids) == 3 and len(set(event_ids)) == 3,
            "F1 event cursor enumerates same-timestamp rows without gaps or repeats",
        )


def _legacy_cancel_payload() -> dict[str, object]:
    return {
        "reason": "legacy expiry has no remaining work",
        "evidence": {
            "attempt_disposition": "absent",
            "work_disposition": "none_to_preserve",
            "evidence_refs": ["fixture:no-current-attempt"],
        },
    }


def _rejected_legacy_correction_code(
    state: StateManagementInterface,
    *,
    event_id: str,
    actor: object,
    prior_version: int,
    payload: dict[str, object],
) -> str:
    try:
        resolve_managed_dispatch(  # type: ignore[arg-type]
            state,
            dispatch_id="mdp-fixture",
            event_id=event_id,
            action="cancel_legacy_expiry",
            actor=actor,  # type: ignore[arg-type]
            prior_version=prior_version,
            payload=payload,
            observed_at=T0 + timedelta(hours=5),
        )
    except DispatchError as exc:
        return exc.code
    return ""


def test_legacy_corrections_are_causal_authorized_and_evidence_bound() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        cast(RealShapeState, state).rows(
            AGENT_ROLE_BINDING_NAMESPACE,
            TABLE_MANAGED_DISPATCH,
        )[0]["state"] = DISPATCH_EXPIRED
        payload = _legacy_cancel_payload()
        wrong_role = _rejected_legacy_correction_code(
            state,
            event_id="evt-legacy-cancel-wrong-role",
            actor=_worker_actor("agi-attacker"),
            prior_version=0,
            payload=payload,
        )
        stale = _rejected_legacy_correction_code(
            state,
            event_id="evt-legacy-cancel-stale",
            actor=_coordinator_actor(),
            prior_version=99,
            payload=payload,
        )
        missing_evidence = _rejected_legacy_correction_code(
            state,
            event_id="evt-legacy-cancel-missing-evidence",
            actor=_coordinator_actor(),
            prior_version=0,
            payload={"reason": "assertion alone is insufficient"},
        )
        cancelled = resolve_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-legacy-cancel-valid",
            action="cancel_legacy_expiry",
            actor=_coordinator_actor(),
            prior_version=0,
            payload=payload,
            observed_at=T0 + timedelta(hours=5),
        )
        _check(
            wrong_role == "coordinator_authority_denied" and stale == "stale_dispatch_version" and missing_evidence == "legacy_expiry_evidence_required",
            "F1 legacy correction rejects wrong-role, stale-version, and assertion-only calls",
        )
        _check(
            cancelled["state"] == "cancelled" and str(cancelled["terminal_reason"]).startswith("legacy_expiry_cancelled:"),
            "F1 evidence-bearing legacy cancellation reaches an explicit terminal state",
        )

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _prepare(state, Path(raw))
        cast(RealShapeState, state).rows(
            AGENT_ROLE_BINDING_NAMESPACE,
            TABLE_MANAGED_DISPATCH,
        )[0]["state"] = DISPATCH_EXPIRED
        artifact = Path(str(row["expected_path"]))
        artifact.write_text("verified legacy result\n", encoding="utf-8")
        worker_evidence = {
            "focused": {"status": "pass"},
            "registered": {"status": "skipped", "reason": "legacy receipt"},
        }
        completed = resolve_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="evt-legacy-completion-valid",
            action="accept_verified_legacy_completion",
            actor=_coordinator_actor(),
            prior_version=0,
            payload={
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "completion_contract_sha256": row["completion_contract_sha256"],
                "worker_completion": {
                    "evidence": worker_evidence,
                    "verdict": "READY-FOR-REVIEW",
                },
                "acceptance_evidence": {
                    "evidence": worker_evidence,
                    "verdict": "READY-FOR-REVIEW",
                    "worker_evidence_sha256": _canonical_sha256(worker_evidence),
                },
            },
            observed_at=T0 + timedelta(hours=5),
        )
        _check(
            completed["state"] == DISPATCH_COMPLETED and completed["terminal_reason"] == "verified_legacy_completion",
            "F1 verified legacy completion binds artifact, contract, worker, and acceptance evidence",
        )


def test_paged_reads_are_discoverable_processes() -> None:
    for name in ("managed_dispatch_inventory", "managed_dispatch_events"):
        metadata = getattr(AgentMessagingPlugin, name)._platform_process_metadata  # type: ignore[attr-defined]  # noqa: SLF001
        _check(
            "limit" in metadata.parameters and "after_id" in metadata.parameters,
            f"F1 {name} publishes bounded cursor metadata",
        )


def test_retired_arguments_are_absent_and_direct_calls_fail_before_side_effects() -> None:
    plugin = object.__new__(AgentMessagingPlugin)
    for name in ("spawn_session", "dispatch_managed_work", "provision_role_session"):
        method = getattr(plugin, name)
        metadata = getattr(plugin, f"{name}_action")._platform_process_metadata  # noqa: SLF001
        _check(
            not {"ttl_seconds", "expires_at"}.intersection(metadata.parameters),
            f"F2 {name} publishes no lifetime parameters",
        )
        for field in ("ttl_seconds", "expires_at"):
            for value in (None, 0, "", "invalid"):
                result = method({field: value}, {})
                _check(
                    result["error"]["code"] == "retired_session_ttl_argument",
                    f"F2 direct {name} rejects {field}={value!r} before looking up state",
                )


def test_populated_legacy_values_cannot_expire_work_or_constrain_reports() -> None:
    for legacy in (None, "", "not-a-timestamp", (T0 - timedelta(days=1)).isoformat()):
        with tempfile.TemporaryDirectory() as raw:
            state = _state()
            _prepare(state, Path(raw))
            _first_turn(state, delivered=True)
            active = _ack(state)
            persisted = cast(RealShapeState, state).rows(
                AGENT_ROLE_BINDING_NAMESPACE,
                TABLE_MANAGED_DISPATCH,
            )[0]
            persisted.update(ttl_seconds=1, expires_at=legacy, expires_at_window_seconds=1)
            milestone = report_managed_dispatch(
                state,
                dispatch_id="mdp-fixture",
                event_id="f2-late-milestone",
                event_kind="milestone",
                attempt_agent_instance_id="agi-worker-1",
                actor=_worker_actor(),
                prior_version=int(active["version"]),
                payload={"completed_work": ["lifetime removal"], "current_evidence": {"legacy": legacy}, "next_action": "review", "next_report_deadline": (T0 + timedelta(days=10000)).isoformat()},
                observed_at=T0 + timedelta(days=9999),
            )
            supervised = supervise_managed_dispatches(state, now=T0 + timedelta(days=10001))
            status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(days=10001))
            _check(milestone["state"] == status["state"] == DISPATCH_ACTIVE, f"F2 legacy expiry {legacy!r} never terminates work or caps reporting")
            _check(not {"ttl_seconds", "expires_at", "expires_at_window_seconds"}.intersection(status), "F2 current projection contains no legacy lifetime fields")
            _check(all(item["condition"] != "ttl_expired" for item in supervised["conditions"]), "F2 lifetime never generates a supervisor condition")
            inventory = managed_dispatch_inventory(state)["dispatches"][0]
            _check(inventory["expires_at"] == legacy, "F2 legacy inventory preserves reconciliation evidence")


def test_session_and_dispatch_report_clocks_remain_independent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _prepare(state, Path(raw))
        _first_turn(state, delivered=True)
        active = _ack(state)
        session = insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-worker-1",
                lane_id="clock-proof",
                assignment=fixture_assignment(),
                work_class="production_mutation",
                budget_line="clock-proof",
                host="operator",
                dispatch_id="mdp-fixture",
                report_by_seconds=600,
            ),
        )
        _check("expires_at" not in session, "F2 new session insert omits legacy expiry")
        report_alive(state, agent_instance_id="agi-worker-1", status="working", directed_by="fixture")
        session_deadline = read_managed_session(state, "agi-worker-1")["report_by"]
        dispatch = read_managed_dispatch(state, "mdp-fixture")
        _check(dispatch["report_by"] == active["report_by"] and dispatch["version"] == active["version"], "B2 session report_alive does not rearm or version the dispatch")
        status = managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(hours=5))
        _check(status["deadline_overdue"]["report_by"], "B2 session heartbeat leaves dispatch overdue")
        report_managed_dispatch(
            state,
            dispatch_id="mdp-fixture",
            event_id="f2-clock-milestone",
            event_kind="milestone",
            attempt_agent_instance_id="agi-worker-1",
            actor=_worker_actor(),
            prior_version=int(dispatch["version"]),
            payload={"completed_work": ["clock proof"], "current_evidence": {"independent": True}, "next_action": "review", "next_report_deadline": (T0 + timedelta(hours=6)).isoformat()},
            observed_at=T0 + timedelta(hours=5),
        )
        _check(read_managed_session(state, "agi-worker-1")["report_by"] == session_deadline, "B2 causal milestone does not rearm the session report clock")
        _check(not managed_dispatch_status(state, "mdp-fixture", now=T0 + timedelta(hours=5))["deadline_overdue"]["report_by"], "B2 causal milestone independently clears dispatch report debt")
        persisted = cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, "managed_session")[0]
        persisted["expires_at"] = "legacy-evidence"
        _check("expires_at" not in session_status(state, "agi-worker-1"), "F2 session status omits legacy expiry")
        _check("expires_at" not in list_sessions(state, live_only=True)["sessions"][0], "F2 current fleet roster omits legacy expiry")


def test_legacy_event_replay_hides_lifetime_without_rewriting_receipt() -> None:
    from agent_messaging_plugin.managed_dispatch import _ACCEPTED_OUTCOME_PAYLOAD_KEY  # noqa: PLC0415

    outcome = {"state": DISPATCH_ACTIVE, "ttl_seconds": 1, "expires_at": "legacy", "expires_at_window_seconds": 1, "version": 2}
    receipt = {"accepted": True, "payload": {_ACCEPTED_OUTCOME_PAYLOAD_KEY: outcome}}
    replayed = _replay_event(receipt)
    _check(replayed == {"state": DISPATCH_ACTIVE, "version": 2}, "F2 historical replay omits retired lifetime from the current response")
    _check(outcome["expires_at"] == "legacy" and outcome["ttl_seconds"] == 1, "F2 historical accepted-event receipt remains unchanged")


def _uptake(state: StateManagementInterface, **kwargs: Any) -> dict[str, Any]:
    """Invoke the registered bridge method against the same faithful ordered-query fake."""
    plugin = object.__new__(AgentMessagingPlugin)
    params = {"since": T0.isoformat(), "as_of": (T0 + timedelta(seconds=400)).isoformat(), **kwargs}
    with patch.object(plugin, "_get_state_service", return_value=state):
        result = plugin.managed_dispatch_uptake_rate(params, {})
    assert result["action_status"] == "completed", result
    return cast(dict[str, Any], result["data"])


def _uptake_contract(
    state: StateManagementInterface,
    tmp: Path,
    index: int = 0,
    *,
    delivered: bool = True,
) -> dict[str, Any]:
    """Prepare a real immutable assignment and append the native initial submission receipt."""
    row = _prepare(state, tmp, dispatch_id=f"mdp-uptake-{index:03}", host="tmux")
    record_first_turn_evidence(state, dispatch_id=str(row["dispatch_id"]), agent_instance_id="agi-worker-1", source="charter", delivered=delivered, error="" if delivered else "driver_failure", host="tmux", host_ref="fixture", agent_runtime="codex", observed_at=T0 + timedelta(seconds=1), register=None, actor=None)
    return row


def _uptake_ack(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    event_id: str = "uptake-ack",
    seconds: int = 2,
    accepted: bool = True,
    attempt: str = "agi-worker-1",
) -> None:
    """Insert controlled receipt variants without bypassing the query under test."""
    _write_event(
        state,
        dispatch_id=str(row["dispatch_id"]),
        event_id=event_id,
        event_kind="ack",
        attempt_agent_instance_id=attempt,
        actor_role=str(row["role_name"]),
        actor_instance_id=attempt,
        prior_version=1,
        observed_at=T0 + timedelta(seconds=seconds),
        payload={"assignment_sha256": row["assignment_sha256"], "revision_id": row["assignment"]["revision"]["revision_id"], "role_binding": row["role_name"], "scope_readback_sha256": "a" * 64, "plan_sha256": "b" * 64},
        accepted=accepted,
    )


def _uptake_events(state: StateManagementInterface) -> list[dict[str, Any]]:
    """Expose durable fixture rows only to build malformed source evidence."""
    return cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH_EVENT)


def test_uptake_submission_only() -> None:
    """A submitted first turn is observable transport evidence, never an uptake ACK.

    The callable lookup deliberately survives base imports so the red leg fails
    on the named missing-capability assertion. The same test then rejects a
    submission-only conversion after the implementation is present.
    """
    uptake = getattr(AgentMessagingPlugin, "managed_dispatch_uptake_rate", None)
    assert callable(uptake), "uptake query missing"
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _uptake_contract(state, Path(raw))
        result = _uptake(state)
        row = result["dispatches"][0]
        assert row["converted"] is False, "submission alone is not uptake"
        assert row["uptake_event_id"] is None, "submission has no uptake event"
        assert result["selected_count"] == 1 and result["slo_status"] == "PARTIAL"


def test_uptake_ack_causality() -> None:
    """Rejected, wrong-attempt and foreign-dispatch receipts cannot manufacture uptake.

    Build each causal loser independently before adding a valid worker receipt.
    Duplicate accepted receipts and native idempotent replay must preserve one
    conversion for each selected contract and the earliest valid event identity.
    """
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _uptake_contract(state, Path(raw))
        _uptake_ack(state, row, accepted=False)
        assert _uptake(state)["successes"] == 0, "rejected ACK is not uptake"
        _uptake_ack(state, row, event_id="wrong-attempt", attempt="agi-worker-2")
        assert _uptake(state)["successes"] == 0, "wrong attempt is not uptake"
        other = _uptake_contract(state, Path(raw), 1)
        _uptake_ack(state, other, event_id="other-dispatch")
        assert _uptake(state)["dispatches"][0]["converted"] is False, "wrong dispatch is not uptake"
        _uptake_ack(state, row, event_id="valid")
        _uptake_ack(state, row, event_id="duplicate", seconds=3)
        result = _uptake(state)
        assert result["successes"] == 2 and result["selected_count"] == 2
        assert result["dispatches"][0]["uptake_event_id"] == "valid", "earliest ACK wins once"
        assert result == _uptake(state), "read replay is deterministic"
        # Native idempotent replay must leave one ACK receipt and one conversion.
        replay_state = _state()
        _prepare(replay_state, Path(raw))
        _first_turn(replay_state, delivered=True)
        _ack(replay_state)
        _ack(replay_state)
        replay = _uptake(replay_state)
        assert replay["successes"] == 1 and len(replay["dispatches"][0]["source_event_ids"]) == 2


def test_uptake_timing_and_retry() -> None:
    """Backdated and late ACKs fail; a successful retry cannot rewrite attempt one.

    The later retry deliberately names a different host and runtime. Historical
    labels and the latency origin must continue to name the original first turn
    even when another worker eventually succeeds.
    """
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _uptake_contract(state, Path(raw))
        _uptake_ack(state, row, event_id="early", seconds=0)
        _uptake_ack(state, row, event_id="late", seconds=302)
        result = _uptake(state)["dispatches"][0]
        assert result["converted"] is False, "early and late ACKs cannot convert"
        assert result["latency_seconds"] == 301 and result["uptake_event_id"] == "late"
        _write_event(
            state,
            dispatch_id=str(row["dispatch_id"]),
            event_id="first-turn:2",
            event_kind="first_turn",
            attempt_agent_instance_id="agi-worker-2",
            actor_role="Coordinator-Main",
            actor_instance_id="agi-coordinator",
            prior_version=2,
            observed_at=T0 + timedelta(seconds=310),
            payload={"source": "retry", "delivered": True, "host": "headless", "agent_runtime": "claude_code"},
            accepted=True,
        )
        _uptake_ack(state, row, event_id="retry-ack", seconds=311, attempt="agi-worker-2")
        result = _uptake(state)["dispatches"][0]
        assert result["converted"] is False and result["transport"] == "tmux", "retry cannot repair initial uptake"
        assert result["runtime"] == "codex" and result["vendor"] == "openai"


def test_uptake_asof_cutoff() -> None:
    """The inclusive observation cutoff excludes a later worker ACK from history.

    Observe the same receipts at two fixed instants. A later ACK must neither
    convert the earlier cohort nor appear among that observation's source IDs;
    at the boundary it becomes visible but the maturity window remains open.
    """
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _uptake_contract(state, Path(raw))
        _uptake_ack(state, row)
        before = _uptake(state, as_of=(T0 + timedelta(seconds=1)).isoformat())
        assert before["successes"] == 0 and before["dispatches"][0]["uptake_event_id"] is None
        assert "uptake-ack" not in before["dispatches"][0]["source_event_ids"], "as_of excludes future ACK"
        after = _uptake(state, as_of=(T0 + timedelta(seconds=2)).isoformat())
        assert after["successes"] == 1 and after["pending"] == 1


def test_uptake_maturity() -> None:
    """Even timely successes must finish the entire observation window to qualify.

    Fifty early successful ACKs cannot qualify one second before maturity.
    Moving only the observation cutoff to the exact full-window boundary proves
    the pass path with the same consecutive denominator and causal evidence.
    """
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        for index in range(50):
            _uptake_ack(state, _uptake_contract(state, Path(raw), index))
        pending = _uptake(state, runtime="codex", transport="tmux", as_of=(T0 + timedelta(seconds=300)).isoformat())
        assert pending["pending"] == 50 and pending["slo_status"] == "PARTIAL", "maturity waits the full window"
        mature = _uptake(state, runtime="codex", transport="tmux", as_of=(T0 + timedelta(seconds=301)).isoformat())
        assert mature["pending"] == 0 and mature["slo_status"] == "PASS", "exact maturity boundary qualifies"


def test_uptake_missing_initial_attempt() -> None:
    """Attempt two cannot stand in for a missing first-turn:1 receipt.

    Keep a successful submission and worker ACK but remove the attempt-one key.
    Missing initial observation time must remain null rather than borrowing
    the replacement attempt's timestamp or declaring a mature success.
    """
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _uptake_contract(state, Path(raw))
        _uptake_events(state)[0]["event_id"] = "first-turn:2"
        _uptake_ack(state, row)
        result = _uptake(state)["dispatches"][0]
        assert result["converted"] is None and result["mature"] is None, "missing initial submission is unknown"
        assert result["reason"] == "initial_submission_missing"


def test_uptake_malformed_evidence() -> None:
    """Partial and malformed records remain visible with unknown attribution.

    Corrupt one required source field per fixture so complete-row successes
    cannot stand in for partial-row coverage. A failed start with no original
    transport receipt must remain in the selected filtered denominator.
    """
    with tempfile.TemporaryDirectory() as raw:
        for field, value in (("payload", None), ("attempt_agent_instance_id", None), ("event_at", "bad-time"), ("id", None), ("event_at", T0.replace(tzinfo=None))):
            state = _state()
            row = _uptake_contract(state, Path(raw))
            _uptake_ack(state, row)
            _uptake_events(state)[0][field] = value
            result = _uptake(state)
            assert result["selected_count"] == 1 and result["unknown"] == 1, "malformed evidence stays visible"
            assert result["dispatches"][0]["converted"] is None
        state = _state()
        _prepare(state, Path(raw), dispatch_id="failed-start")
        dispatch = cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0]
        dispatch["state"] = "failed_start"
        result = _uptake(state, runtime="codex", transport="tmux")
        assert result["selected_ids"] == ["failed-start"] and result["unknown"] == 1
        assert result["dispatches"][0]["filter_eligibility"] == "unknown", "unknown transport remains in denominator"


def test_uptake_denominator_and_threshold() -> None:
    """Consecutive failures stay in the denominator and the 48-of-50 threshold is exact.

    The failed submission contracts are selected before outcomes are measured.
    Add exactly one ACK to move from 47 to 48 successes without changing IDs.
    Unknown original transport must subsequently prevent a qualified pass.
    """
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        for index in range(50):
            row = _uptake_contract(state, Path(raw), index, delivered=index < 48)
            if index < 47:
                _uptake_ack(state, row)
        result = _uptake(state, runtime="codex", transport="tmux")
        assert result["selected_count"] == 50 and result["failed"] == 3, "50 includes failed submissions"
        assert result["successes"] == 47 and result["slo_status"] == "FAIL", "47 of 50 fails SLO"
        row = read_managed_dispatch(state, "mdp-uptake-047")
        _uptake_ack(state, row)
        result = _uptake(state, runtime="codex", transport="tmux")
        assert result["successes"] == 48 and result["slo_status"] == "PASS", "48 of 50 passes SLO"
        assert result["conversion_rate"] == 0.96
        _uptake_events(state)[0]["payload"].pop("host")
        uncertain = _uptake(state, runtime="codex", transport="tmux")
        assert uncertain["selected_count"] == 50 and uncertain["slo_status"] == "PARTIAL"


def test_uptake_tie_safe_cohort() -> None:
    """Both contract and event scans cross the 100-row timestamp tie without loss.

    More than one page shares the same leading timestamp in each table.
    This exercises the real composite-cursor matcher through RealShapeState,
    including exhaustion and explicit known-filter exclusions.
    """
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        for index in range(105):
            _uptake_contract(state, Path(raw), index)
        result = _uptake(state, sample_size=105)
        assert result["selected_count"] == 105, "tie boundary keeps all consecutive contracts"
        assert len(set(result["selected_ids"])) == 105 and result["reads_complete"] is True
        row = read_managed_dispatch(state, "mdp-uptake-000")
        for index in range(105):
            _uptake_ack(state, row, event_id=f"ack-{index:03}")
        result = _uptake(state)
        assert len(result["dispatches"][0]["source_event_ids"]) == 106, "event ties exhaust every page"
        filtered = _uptake(state, runtime="claude_code")
        assert filtered["selected_count"] == 0 and len(filtered["exclusions"]) == 105


def test_uptake_partial_read_and_identity() -> None:
    """Missing telemetry never claims a complete cohort or mutates source history.

    Preserve a raw before/after receipt comparison, then independently break
    assignment binding, payload shape and provider availability. An unreadable
    inventory is advisory and cannot masquerade as a complete empty cohort.
    """
    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        row = _uptake_contract(state, Path(raw))
        _uptake_ack(state, row)
        events = _uptake_events(state)
        before = repr(events)
        _uptake(state)
        assert repr(events) == before, "read-only query preserves source receipts"
        # Valid shape alone is insufficient: bind the immutable assignment and author.
        events[1]["payload"]["assignment_sha256"] = "wrong"
        assert _uptake(state)["successes"] == 0, "wrong assignment is not uptake"
        events[1]["payload"] = None
        result = _uptake(state)
        assert result["unknown"] == 1 and result["dispatches"][0]["converted"] is None
        cast(RealShapeState, state).fail_next("query")
        result = _uptake(state)
        assert result["reads_complete"] is False and result["slo_status"] == "UNMEASURABLE"
        assert result["advisories"], "provider failure remains an explicit advisory"


def test_uptake_bridge_parameters() -> None:
    """Public parameter metadata and the EDGE registry expose the exact read contract.

    Every bridge parameter must be declared, not merely accepted by Python.
    Invalid caller syntax remains an ordinary process error, separate from
    telemetry uncertainty in an otherwise executable read.
    """
    method = AgentMessagingPlugin.managed_dispatch_uptake_rate
    metadata = method._platform_process_metadata  # noqa: SLF001
    assert set(metadata.parameters) == {"since", "as_of", "window_seconds", "sample_size", "runtime", "transport"}
    plugin = object.__new__(AgentMessagingPlugin)
    assert "managed_dispatch_uptake_rate" in plugin.get_edge_process_definitions()
    with patch.object(plugin, "_get_state_service", return_value=_state()):
        for params in ({"since": "bad"}, {"since": T0.isoformat(), "sample_size": 0}):
            result = method(plugin, params, {})
            assert result["action_status"] == "failed", "caller syntax validation is explicit"


def _claim_fixture(tmp: Path) -> tuple[StateManagementInterface, dict[str, Any]]:
    state = _state()
    _prepare(state, tmp)
    _first_turn(state, delivered=True)
    _ack(state)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_DISPATCH, "filters": {"dispatch_id": "mdp-fixture"}},
        {"version": 51, "report_by": (T0 + timedelta(minutes=3)).isoformat(), "last_milestone_at": (T0 + timedelta(minutes=1)).isoformat(), "watchdog_fired_at": (T0 + timedelta(minutes=3)).isoformat()},
    )
    baseline = read_managed_dispatch(state, "mdp-fixture")
    claim = {
        "baseline": {
            key: baseline[key]
            for key in (
                "version",
                "report_by",
                "last_milestone_at",
                "current_agent_instance_id",
                "dispatch_id",
                "attempt_number",
            )
        },
        "requested_at": (T0 + timedelta(minutes=4)).isoformat(),
        "event_id": "evt-claim-milestone",
    }
    _claim_report(state, "evt-claim-milestone", 51, 5, 10)
    return state, claim


def _claim_report(
    state: StateManagementInterface,
    event_id: str,
    version: int,
    at: int,
    deadline: int,
) -> dict[str, Any]:
    return report_managed_dispatch(
        state,
        dispatch_id="mdp-fixture",
        event_id=event_id,
        event_kind="milestone",
        attempt_agent_instance_id="agi-worker-1",
        actor=_worker_actor(),
        prior_version=version,
        observed_at=T0 + timedelta(minutes=at),
        payload={"completed_work": "measured progress", "current_evidence": {"test": "pass"}, "next_action": "continue", "next_report_deadline": (T0 + timedelta(minutes=deadline)).isoformat()},
    )


def _claim_verdict(state: StateManagementInterface, claim: Any) -> dict[str, Any]:
    before = copy.deepcopy(cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH))
    events = copy.deepcopy(_dispatch_events(state))
    status = managed_dispatch_status(
        state,
        "mdp-fixture",
        now=T0 + timedelta(minutes=6),
        milestone_claim=claim,
    )
    assert before == cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)
    assert events == _dispatch_events(state), "CLAIM_VERIFIER_HAS_NO_WRITES"
    return status["milestone_claim_verification"]


def _claim_event_update(state: StateManagementInterface, updates: dict[str, Any]) -> None:
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_DISPATCH_EVENT, "filters": {"event_id": "evt-claim-milestone"}},
        updates,
    )


def test_claim_fresh_replay_and_correction() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state, claim = _claim_fixture(Path(raw))
        verified = _claim_verdict(state, claim)
        assert verified["status"] == "satisfied", verified
        assert verified["post_transition"]["version"] == 52
        assert verified["post_transition"]["report_by"] == (T0 + timedelta(minutes=10)).isoformat()
        before = copy.deepcopy(_dispatch_events(state))
        replay = _claim_report(state, "evt-claim-milestone", 51, 5, 10)
        assert replay["version"] == 52 and before == _dispatch_events(state)
        assert _claim_verdict(state, claim) == verified, "EXACT_RECEIPT_REPLAY"
        assert "milestone_claim_verification" not in managed_dispatch_status(state, "mdp-fixture", now=T0)


def test_claim_correction_and_stale_report() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state, claim = _claim_fixture(Path(raw))
        replay = read_managed_dispatch(state, "mdp-fixture")
        bad = {**claim, "event_id": "missing"}
        assert _claim_verdict(state, bad)["status"] == "mismatch"
        assert _claim_verdict(state, claim)["status"] == "satisfied", "CLAIM_CORRECTION"
        try:
            _claim_report(state, "evt-stale-claim", 51, 6, 12)
        except DispatchError as exc:
            assert exc.code == "stale_dispatch_version"
        else:
            raise AssertionError("STALE_REPORT_STILL_REFUSED")
        assert _claim_verdict(state, {**claim, "event_id": "evt-stale-claim"})["status"] == "mismatch"
        new_claim = {**claim, "baseline": {**claim["baseline"], **{key: replay[key] for key in ("version", "report_by", "last_milestone_at")}}, "requested_at": (T0 + timedelta(minutes=6)).isoformat(), "event_id": "evt-correction"}
        _claim_report(state, "evt-correction", 52, 7, 15)
        assert _claim_verdict(state, new_claim)["status"] == "satisfied", "NEW_SEMANTIC_EVENT"
        equivalent = copy.deepcopy(new_claim)
        equivalent["requested_at"] = "2026-08-22T14:06:00+02:00"
        equivalent["baseline"]["report_by"] = "2026-08-22T14:10:00+02:00"
        assert _claim_verdict(state, equivalent)["status"] == "satisfied", "UTC_EQUIVALENCE"


def test_claim_event_identity() -> None:
    for updates, reason in (({"accepted": False}, "event_not_accepted"), ({"event_kind": "supervision_notice"}, "event_not_milestone")):
        with tempfile.TemporaryDirectory() as raw:
            state, claim = _claim_fixture(Path(raw))
            _claim_event_update(state, updates)
            verdict = _claim_verdict(state, claim)
            assert verdict["status"] == "mismatch", ("EVENT_IDENTITY_REQUIRED", updates, verdict)
            assert reason in verdict["reasons"]


def test_claim_pre_request_receipt() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state, claim = _claim_fixture(Path(raw))
        claim["requested_at"] = (T0 + timedelta(minutes=6)).isoformat()
        verdict = _claim_verdict(state, claim)
        assert verdict["status"] == "mismatch", ("POST_REQUEST_REQUIRED", verdict)
        assert verdict["reasons"] == ["event_before_request"]


def test_claim_outcome_advancement() -> None:
    cases = (
        ("version", 51, "outcome_version_not_advanced"),
        ("report_by", (T0 + timedelta(minutes=3)).isoformat(), "report_deadline_not_advanced"),
        ("report_by", (T0 + timedelta(minutes=2)).isoformat(), "report_deadline_not_advanced"),
        ("last_milestone_at", (T0 + timedelta(minutes=1)).isoformat(), "milestone_time_not_advanced"),
    )
    for field, value, reason in cases:
        with tempfile.TemporaryDirectory() as raw:
            state, claim = _claim_fixture(Path(raw))
            event = _dispatch_events(state, event_ids={claim["event_id"]})[0]
            payload = copy.deepcopy(event["payload"])
            payload["_managed_dispatch_accepted_outcome"][field] = value
            updates: dict[str, Any] = {"payload": payload}
            _claim_event_update(state, updates)
            verdict = _claim_verdict(state, claim)
            assert verdict["status"] == "mismatch", ("OUTCOME_ADVANCEMENT_REQUIRED", field, verdict)
            assert reason in verdict["reasons"]


def test_claim_attempt_binding() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state, claim = _claim_fixture(Path(raw))
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_DISPATCH, "filters": {"dispatch_id": "mdp-fixture"}},
            {"current_agent_instance_id": "agi-replacement", "attempt_number": 2},
        )
        verdict = _claim_verdict(state, claim)
        assert verdict["status"] == "mismatch", ("CURRENT_ATTEMPT_REQUIRED", verdict)
        assert "baseline_attempt_superseded" in verdict["reasons"]


def test_claim_later_overdue() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state, claim = _claim_fixture(Path(raw))
        status = managed_dispatch_status(
            state,
            "mdp-fixture",
            now=T0 + timedelta(minutes=11),
            milestone_claim=claim,
        )
        assert status["milestone_claim_verification"]["status"] == "satisfied"
        assert status["next_required_action"] == "request_worker_milestone", "CURRENT_OVERDUE_SURVIVES"
        assert status["overdue_condition"] == "milestone_overdue"


def test_claim_uncertainty_and_history() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state, claim = _claim_fixture(Path(raw))
        for malformed in ([], {}, {**claim, "baseline": {}}, {**claim, "baseline": {**claim["baseline"], "version": True}}, {**claim, "requested_at": "2026-08-22T12:04:00"}):
            verdict = _claim_verdict(state, malformed)
            assert verdict["status"] == "unverifiable" and verdict["advisory"]
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_DISPATCH, "filters": {"dispatch_id": "mdp-fixture"}},
            {"state": DISPATCH_COMPLETED},
        )
        assert _claim_verdict(state, claim)["status"] == "satisfied", "TERMINAL_RECEIPT_READABLE"
        _claim_event_update(state, {"payload": {}})
        assert _claim_verdict(state, claim)["reasons"] == ["accepted_outcome_missing"]
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_DISPATCH, "filters": {"dispatch_id": "mdp-fixture"}},
            {"assignment": None},
        )
        status = managed_dispatch_status(state, "mdp-fixture", now=T0, milestone_claim=claim)
        assert status["assignment_status"] == "historical"
        assert status["milestone_claim_verification"]["reasons"] == ["historical_assignment"]


def test_claim_wrong_receipt_and_baseline() -> None:
    cases = (("actor_instance_id", "agi-forged", "event_actor_mismatch"), ("attempt_agent_instance_id", "agi-old", "event_attempt_mismatch"), ("prior_version", 50, "event_prior_version_before_baseline"))
    for field, value, reason in cases:
        with tempfile.TemporaryDirectory() as raw:
            state, claim = _claim_fixture(Path(raw))
            _claim_event_update(state, {field: value})
            verdict = _claim_verdict(state, claim)
            assert verdict["status"] == "mismatch" and reason in verdict["reasons"], verdict
    with tempfile.TemporaryDirectory() as raw:
        state, claim = _claim_fixture(Path(raw))
        wrong = {**claim, "baseline": {**claim["baseline"], "dispatch_id": "mdp-other"}}
        assert "baseline_dispatch_mismatch" in _claim_verdict(state, wrong)["reasons"]
        event = _dispatch_events(state, event_ids={claim["event_id"]})[0]
        payload = copy.deepcopy(event["payload"])
        payload["_managed_dispatch_accepted_outcome"]["version"] = True
        _claim_event_update(state, {"payload": payload})
        assert _claim_verdict(state, claim)["status"] == "unverifiable", "BOOL_IS_NOT_VERSION"


def test_milestone_claim_bridge_contract() -> None:
    metadata = AgentMessagingPlugin.managed_dispatch_status._platform_process_metadata  # noqa: SLF001
    schema = InvocationSchemaGenerator().generate(
        "plugin::agent_messaging_plugin::managed_dispatch_status",
        {name: parameter.to_dict() for name, parameter in metadata.parameters.items()},
    )
    arguments = cast(dict[str, Any], cast(dict[str, Any], schema["properties"])["arguments"])
    assert arguments["properties"]["milestone_claim"]["type"] == "object"
    assert "milestone_claim" not in arguments.get("required", [])
    assert arguments["additionalProperties"] is False
    with tempfile.TemporaryDirectory() as raw:
        state, claim = _claim_fixture(Path(raw))
        plugin = object.__new__(AgentMessagingPlugin)
        with patch.object(plugin, "_get_state_service", return_value=state):
            result = plugin.managed_dispatch_status(
                {"parameters": {"dispatch_id": "mdp-fixture", "milestone_claim": claim}},
                {},
            )
            assert result["action_status"] == "completed", result
            verdict = result["data"]["milestone_claim_verification"]
            assert verdict["status"] == "satisfied", "DECLARED_BRIDGE_CLAIM_REACHES_VERIFIER"
            assert verdict["post_transition"]["version"] == 52
            ordinary = plugin.managed_dispatch_status({"dispatch_id": "mdp-fixture"}, {})
            assert "milestone_claim_verification" not in ordinary["data"]
        _check(True, "Milestone claim optional schema and forwarding fixture reached")


def main() -> int:
    test_claim_fresh_replay_and_correction()
    test_claim_correction_and_stale_report()
    test_claim_event_identity()
    test_claim_pre_request_receipt()
    test_claim_outcome_advancement()
    test_claim_attempt_binding()
    test_claim_later_overdue()
    test_claim_uncertainty_and_history()
    test_claim_wrong_receipt_and_baseline()
    test_milestone_claim_bridge_contract()
    test_uptake_submission_only()
    test_uptake_ack_causality()
    test_uptake_timing_and_retry()
    test_uptake_asof_cutoff()
    test_uptake_maturity()
    test_uptake_missing_initial_attempt()
    test_uptake_malformed_evidence()
    test_uptake_denominator_and_threshold()
    test_uptake_tie_safe_cohort()
    test_uptake_partial_read_and_identity()
    test_uptake_bridge_parameters()
    _check(True, "Uptake causal, cohort, maturity, malformed evidence and bridge assertions reached")
    test_schema_keeps_nullable_columns_but_writers_omit_values()
    test_null_ttl_row_is_readable_and_never_expires()
    test_dispatch_inventory_and_events_are_tie_safe_pages()
    test_legacy_corrections_are_causal_authorized_and_evidence_bound()
    test_paged_reads_are_discoverable_processes()
    test_retired_arguments_are_absent_and_direct_calls_fail_before_side_effects()
    test_populated_legacy_values_cannot_expire_work_or_constrain_reports()
    test_session_and_dispatch_report_clocks_remain_independent()
    test_legacy_event_replay_hides_lifetime_without_rewriting_receipt()
    print(f"\nmanaged dispatch F1 bridge smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
