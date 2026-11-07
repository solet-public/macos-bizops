#!/usr/bin/env python3
"""Native registered-assignment regressions (historical gate filename retained).

No live launcher or register write runs here. The recording CLI exercises the
real byte/provenance protocol; state tests use explicitly seeded register rows
and a recording attempt launcher at the package-2 boundary.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import chdir
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from _register_unit_double import FIXTURE_UNIT_ID, RegisterUnitDouble  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE, HOLDER_KIND_INFERENCE_PROVIDER  # noqa: E402
from codex_managed_session_smoke import CausalAnnotationTests as CausalAnnotationTests  # noqa: E402
from managed_dispatch_cli_actor_smoke import (  # noqa: E402
    _SUCCESSION_KEY,
    _successor_cancel,
    _successor_denied,
    _successor_events,
    _successor_fixture,
)
from managed_dispatch_smoke import (  # noqa: E402
    T0,
    _assignment_identity,
    _coordinator_actor,
    _seed_binding,
    _spec,
    _state,
)

from agent_messaging_plugin.lane_worktree_evidence import PsoletRetirementEvidenceReader  # noqa: E402
from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DISPATCH_NONTERMINAL_STATES,
    DispatchActor,
    DispatchError,
    DispatchSpec,
    _cancel_updates,
    _update_dispatch,
    dispatch_managed_work,
    managed_dispatch_status,
    mark_dispatch_worker_lost,
    prepare_managed_dispatch,
    read_managed_dispatch,
    record_first_turn_evidence,
    report_managed_dispatch,
    resolve_managed_dispatch,
    validate_resolution_admission,
)
from agent_messaging_plugin.managed_dispatch_unit import fail_start_updates  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.register_unit_client import (  # noqa: E402
    PsoletRegisterUnitClient,
    RegisteredRevision,
    RegisterUnitError,
    UnitReadback,
    assignment_from_record,
    parse_unit_show,
    render_assignment,
)
from agent_messaging_plugin.role_binding_store import ResolvedRole, RoleBindingVacantError  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_DISPATCH,
    TABLE_MANAGED_DISPATCH_EVENT,
    TABLE_MANAGED_SESSION,
    get_managed_dispatch_schema,
)
from agent_messaging_plugin.session_launch_adapters import _resolution_preflight  # noqa: E402


class CoordinatorSuccessionTests(unittest.TestCase):
    """Resolution follows the live role holder while retaining spawning provenance.

    These fixtures use real-shaped state rows and a recording register double.
    They never launch a host or repair a role from historical session data.
    """

    def test_successor_uses_binding_without_historical_session(self) -> None:
        """Historical absence cannot strand the holder; caller identity remains required."""
        for session in (None, "", "__unclaimed__"):
            with self.subTest(original_session=session), tempfile.TemporaryDirectory() as tmp:
                state, row, actor, register = _successor_fixture(
                    Path(tmp),
                    "ases-different",
                    original_session=session,
                )
                with patch.object(state, "query_state", wraps=state.query_state) as queries:
                    self.assertEqual(_successor_cancel(state, row, actor, register)["state"], "cancelled")
                self.assertFalse(any(call.args[1]["table"] == TABLE_MANAGED_SESSION for call in queries.call_args_list))
        for session in ("", "__unclaimed__"):
            with self.subTest(caller_session=session):
                self.assertTrue(_successor_denied(session=session, original_session=session))
        with tempfile.TemporaryDirectory() as tmp:
            state, row, _, register = _successor_fixture(Path(tmp), original_session=None)
            _seed_binding(state, role="Coordinator-Main", instance="agi-coordinator", session="ases-coordinator")
            with patch.object(state, "query_state", wraps=state.query_state) as queries:
                self.assertEqual(_successor_cancel(state, row, _coordinator_actor(), register)["state"], "cancelled")
            self.assertFalse(any(call.args[1]["table"] == TABLE_MANAGED_SESSION for call in queries.call_args_list))

    def test_successor_stale_version_and_revocation(self) -> None:
        """CAS rejection is attributed, and losing the role revokes replay authority."""
        with tempfile.TemporaryDirectory() as tmp:
            state, row, actor, register = _successor_fixture(Path(tmp))
            with self.assertRaises(DispatchError) as stale:
                _successor_cancel(state, row, actor, register, event="stale", prior_version=row["version"] - 1)
            self.assertEqual(stale.exception.code, "stale_dispatch_version")
            self.assertEqual(read_managed_dispatch(state, row["dispatch_id"]), row)
            rejection = next(event for event in _successor_events(state) if event["event_id"] == "stale")
            self.assertFalse(rejection["accepted"])
            self.assertEqual(rejection["rejection_code"], "stale_dispatch_version")
            self.assertEqual(rejection["actor_instance_id"], actor.agent_instance_id)
            cancelled = _successor_cancel(state, row, actor, register)
            events = list(_successor_events(state))
            _seed_binding(state, role="Coordinator-Main", instance="agi-revoked", session=actor.agent_session_id)
            for event in ("successor-cancel", "after-revocation"):
                with self.subTest(event=event), self.assertRaises(DispatchError) as denied:
                    _successor_cancel(state, row, actor, register, event=event)
                self.assertEqual(denied.exception.code, "coordinator_authority_denied")
            self.assertEqual(read_managed_dispatch(state, row["dispatch_id"]), cancelled)
            self.assertEqual(_successor_events(state)[: len(events)], events)

    def test_successor_evidence_is_server_owned(self) -> None:
        """Callers cannot forge the reserved proof or persist it on the projection."""
        with tempfile.TemporaryDirectory() as tmp:
            state, row, actor, register = _successor_fixture(Path(tmp))
            with self.assertRaises(DispatchError) as injected:
                _successor_cancel(state, row, actor, register, event="injected", payload={_SUCCESSION_KEY: {"forged": True}})
            self.assertEqual(injected.exception.code, "event_payload_reserved")
            self.assertEqual(read_managed_dispatch(state, row["dispatch_id"]), row)
            _successor_cancel(state, row, actor, register)
            accepted = next(event for event in _successor_events(state) if event["event_id"] == "successor-cancel")
            proof = accepted["payload"][_SUCCESSION_KEY]
            self.assertEqual(
                proof,
                {
                    "proof_kind": "current_role_holder",
                    "coordinator_role": row["spawned_by_role"],
                    "caller_instance_id": actor.agent_instance_id,
                    "caller_session_id": actor.agent_session_id,
                    "original_instance_id": row["spawned_by_instance_id"],
                },
            )
            self.assertNotIn("forged", proof)
            self.assertNotIn(_SUCCESSION_KEY, read_managed_dispatch(state, row["dispatch_id"]))
        with tempfile.TemporaryDirectory() as tmp:
            state, row, actor, register = _successor_fixture(Path(tmp))
            cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0].pop("assignment")
            cancelled = _successor_cancel(state, row, actor, register, historical_cancel_role="Coordinator-Main")
            self.assertEqual(cancelled["cancelled_by_instance_id"], actor.agent_instance_id)
            self.assertNotIn(_SUCCESSION_KEY, _successor_events(state)[-1]["payload"])

    def test_successor_binding_identity_negatives(self) -> None:
        """Both authenticated identity slots must match the current binding exactly.

        A matching session with another instance is insufficient, as is a matching
        instance with another session. Admission and mutation share this boundary.
        """
        for instance, session in (
            ("agi-other", "ases-different"),
            ("agi-successor", "ases-wrong"),
            ("", "ases-different"),
            ("agi-successor", ""),
            ("agi-successor", "__unclaimed__"),
        ):
            with self.subTest(instance=instance, session=session), tempfile.TemporaryDirectory() as tmp:
                state, row, actor, register = _successor_fixture(Path(tmp), "ases-different")
                wrong = replace(actor, agent_instance_id=instance, agent_session_id=session)
                with self.assertRaises(DispatchError) as denied:
                    _successor_cancel(state, row, wrong, register)
                self.assertEqual(denied.exception.code, "coordinator_authority_denied", "EXACT_BINDING_REQUIRED")
                with self.assertRaises(DispatchError) as admission:
                    validate_resolution_admission(state, dispatch_id=row["dispatch_id"], event_id="admit-wrong", action="cancel", actor=wrong)
                self.assertEqual(admission.exception.code, "coordinator_authority_denied")
                self.assertEqual(read_managed_dispatch(state, row["dispatch_id"]), row)

    def test_vacant_provider_and_state_read_failures(self) -> None:
        """Only session holders qualify; genuine state read errors remain visible.

        The provider double deliberately carries matching identity strings to
        isolate holder kind. A failed read must propagate rather than look vacant.
        """
        with tempfile.TemporaryDirectory() as tmp:
            state, row, actor, register = _successor_fixture(Path(tmp), "ases-different")
            provider = ResolvedRole("Coordinator-Main", "", actor.agent_instance_id, "", HOLDER_KIND_INFERENCE_PROVIDER, actor.agent_session_id)
            target = "agent_messaging_plugin._managed_dispatch_authority.resolve_role_binding"
            for effect in (RoleBindingVacantError("Coordinator-Main"), provider):
                with self.subTest(effect=effect), patch(target) as resolver:
                    if isinstance(effect, Exception):
                        resolver.side_effect = effect
                    else:
                        resolver.return_value = effect
                    with self.assertRaises(DispatchError) as denied:
                        _successor_cancel(state, row, actor, register, event=str(effect))
                    self.assertEqual(denied.exception.code, "coordinator_authority_denied")
            with (
                patch(target, side_effect=RuntimeError("state unavailable")),
                self.assertRaisesRegex(
                    RuntimeError,
                    "state unavailable",
                ),
            ):
                _successor_cancel(state, row, actor, register, event="state-failure")
            self.assertEqual(read_managed_dispatch(state, row["dispatch_id"]), row)

    def test_prospective_creation_requires_original_instance(self) -> None:
        """Succession resolves assigned work without granting prospective creation.

        The prospective spec omits assignment even when the existing dispatch is
        assigned. Authority must be established before looking up or reusing it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, row, actor, register = _successor_fixture(root, "ases-different")
            with self.assertRaises(DispatchError, msg="PROSPECTIVE_SUCCESSOR_DENIED") as denied:
                prepare_managed_dispatch(state, _spec(root, repository_root=str(root)), actor=actor, register=register, now=T0)
            self.assertEqual(denied.exception.code, "coordinator_authority_denied")
            self.assertIn("original spawning instance", denied.exception.message)
            self.assertEqual(read_managed_dispatch(state, row["dispatch_id"]), row)

    def test_original_displaced_and_worker_authority(self) -> None:
        """Past provenance grants no current role, and coordinator status grants no worker role.

        The original actor lost its coordinator binding in the fixture. The new
        holder has that binding but cannot ACK as the distinct worker attempt.
        """
        with tempfile.TemporaryDirectory() as tmp:
            state, row, actor, register = _successor_fixture(Path(tmp), "ases-different")
            with self.assertRaises(DispatchError) as denied:
                _successor_cancel(state, row, _coordinator_actor(), register)
            self.assertEqual(denied.exception.code, "coordinator_authority_denied")
            with self.assertRaises(DispatchError) as worker:
                report_managed_dispatch(
                    state,
                    dispatch_id=row["dispatch_id"],
                    event_id="successor-worker",
                    event_kind="ack",
                    attempt_agent_instance_id=actor.agent_instance_id,
                    actor=actor,
                    prior_version=row["version"],
                    payload={},
                    observed_at=T0,
                )
            self.assertEqual(worker.exception.code, "worker_authority_denied")
            self.assertEqual(read_managed_dispatch(state, row["dispatch_id"]), row)


class AssignmentFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = _state()
        self.spec = _spec(Path(self.tmp.name))
        self.register = RegisterUnitDouble()
        self.assigned = self.register.add_unit()
        self.launches: list[Mapping[str, Any]] = []

    def launch(self, row: Mapping[str, Any]) -> Mapping[str, Any]:
        self.launches.append(dict(row))
        return {"agent_instance_id": f"agi-attempt-{len(self.launches)}", "host_ref": "recorded-only"}

    def dispatch(self, spec: DispatchSpec | None = None) -> dict[str, Any]:
        return dispatch_managed_work(self.state, spec or self.spec, register=self.register, actor=_coordinator_actor(), spawn_attempt=self.launch, now=T0)

    def assert_no_effect(self, code: str, spec: DispatchSpec | None = None) -> None:
        with self.assertRaises(DispatchError) as caught:
            self.dispatch(spec)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(self.launches)
        for table in (TABLE_MANAGED_DISPATCH, TABLE_MANAGED_DISPATCH_EVENT, TABLE_MANAGED_SESSION):
            self.assertEqual(cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, table), [])

    def retry(self, row: Mapping[str, Any]) -> dict[str, Any]:
        lost = mark_dispatch_worker_lost(self.state, dispatch_id=self.spec.dispatch_id, agent_instance_id=str(row["current_agent_instance_id"]), observed_at=T0, detail="fixture", register=None)
        return resolve_managed_dispatch(self.state, dispatch_id=self.spec.dispatch_id, event_id="retry", action="request_retry", actor=_coordinator_actor(), prior_version=int(lost["version"]), payload={"reason": "fixture"}, observed_at=T0, register=self.register, spawn_attempt=self.launch)


class AssignedTerminalCancellationTests(AssignmentFixture):
    def test_failed_start_and_worker_lost_cancel_preserve_audit(self) -> None:
        for state_name, transition in (
            ("failed_start", lambda row: _update_dispatch(self.state, row, fail_start_updates(row, DispatchError("fixture_failure", "fixture failure")))),
            ("worker_lost", lambda row: mark_dispatch_worker_lost(self.state, dispatch_id=self.spec.dispatch_id, agent_instance_id=str(row["current_agent_instance_id"]), observed_at=T0, detail="host detail", register=None)),
        ):
            with self.subTest(state=state_name):
                self.state = _state()
                self.spec = replace(self.spec, dispatch_id=state_name)
                transition(self.dispatch()["dispatch"])
                row = read_managed_dispatch(self.state, self.spec.dispatch_id)
                failure, detail = row["attempt_failure"], row.get("host_liveness_detail")
                try:
                    cancelled = resolve_managed_dispatch(
                        self.state, dispatch_id=self.spec.dispatch_id, event_id="cancel-" + state_name, action="cancel", actor=_coordinator_actor(), prior_version=int(row["version"]), payload={"reason": "coordinator cancel"}, observed_at=T0, register=self.register, spawn_attempt=self.launch
                    )
                except DispatchError as exc:
                    cancelled = {"state": exc.code}
                self.assertEqual(cancelled.get("state"), "cancelled", "failed_start and worker_lost cancellations are accepted")
                self.assertEqual(cancelled["terminal_reason"], "coordinator cancel")
                self.assertEqual((cancelled.get("attempt_failure"), cancelled.get("host_liveness_detail")), (failure, detail))
                event = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH_EVENT)[-1]
                self.assertEqual(event["payload"]["reason"], "coordinator cancel")
                unit_event = self.register.events[-1][1]
                self.assertEqual(unit_event["event"], "cancelled")
                self.assertEqual(unit_event["reason"], "coordinator cancel")


class AssignmentReadTests(AssignmentFixture):
    def test_file_free_existing_unit_is_frozen_before_launcher(self) -> None:
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        result = self.dispatch()
        self.assertEqual(assignment_from_record(self.launches[0]), self.assigned)
        self.assertEqual(result["attempt"]["agent_instance_id"], "agi-attempt-1")
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        self.assertNotIn("brief_ref", self.launches[0])
        self.assertNotIn("brief_sha256", self.launches[0])
        self.assertNotIn("unit_mint_request", self.launches[0])
        self.assertIn(self.assigned.revision.text, render_assignment(self.assigned))

    def test_no_unit_address_refuses_before_any_effect(self) -> None:
        self.assert_no_effect("unit_id_required", replace(self.spec, unit_id=""))

    def test_missing_unit_refuses_before_any_effect(self) -> None:
        self.assert_no_effect("unit_not_found", replace(self.spec, unit_id="unt_missing"))

    def test_retired_unit_refuses_before_any_effect(self) -> None:
        self.register.units[FIXTURE_UNIT_ID] = replace(self.register.units[FIXTURE_UNIT_ID], state="cancelled")
        self.assert_no_effect("unit_not_dispatchable")

    def test_deleted_unit_refuses_before_any_effect(self) -> None:
        self.register.units[FIXTURE_UNIT_ID] = replace(self.register.units[FIXTURE_UNIT_ID], is_deleted=True)
        self.assert_no_effect("unit_not_found")

    def test_wrong_unit_repository_refuses_before_any_effect(self) -> None:
        self.register.units[FIXTURE_UNIT_ID] = replace(self.register.units[FIXTURE_UNIT_ID], repository_id="rep_other")
        self.assert_no_effect("unit_repository_mismatch")

    def test_wrong_declared_repository_refuses_before_any_effect(self) -> None:
        self.assert_no_effect("unit_repository_mismatch", replace(self.spec, repository_id="rep_other"))

    def test_wrong_caller_binding_refuses_before_register_read(self) -> None:
        _seed_binding(self.state, role="Coordinator-Main", instance="agi-replaced", session="ases-coordinator")
        self.assert_no_effect("coordinator_authority_denied")
        self.assertFalse(self.register.reads)

    def test_missing_caller_identity_refuses_before_register_read(self) -> None:
        with self.assertRaises(DispatchError) as caught:
            prepare_managed_dispatch(self.state, self.spec, register=self.register, actor=DispatchActor("", "", "live_peer_binding"), now=T0)
        self.assertEqual(caught.exception.code, "coordinator_authority_denied")
        self.assertFalse(self.register.reads)

    def test_wrong_kind_refuses_before_any_effect(self) -> None:
        self.assert_no_effect("unit_kind_mismatch", replace(self.spec, dispatch_kind="review"))

    def test_stale_revision_refuses_before_any_effect(self) -> None:
        self.assert_no_effect("unit_revision_stale", replace(self.spec, expected_revision_id="drv_stale"))

    def test_register_outage_refuses_before_any_effect(self) -> None:
        with patch.object(self.register, "show_unit", side_effect=RegisterUnitError("unit_register_unavailable", "offline")):
            self.assert_no_effect("unit_register_unavailable")

    def test_missing_revision_refuses_before_any_effect(self) -> None:
        self.register.units[FIXTURE_UNIT_ID] = replace(self.register.units[FIXTURE_UNIT_ID], revision_id="")
        self.assert_no_effect("unit_revision_missing")

    def test_changed_bytes_refuse_before_any_effect(self) -> None:
        revision = self.assigned.revision
        self.register.revisions[revision.revision_id] = replace(revision, text="tampered")
        self.assert_no_effect("unit_revision_mismatch")

    def test_revision_race_refuses_before_any_effect(self) -> None:
        original = self.register.read_brief

        def raced(unit: UnitReadback) -> RegisteredRevision:
            revision = original(unit)
            self.register.units[unit.unit_id] = replace(unit, revision_id="drv_changed")
            return revision

        with patch.object(self.register, "read_brief", side_effect=raced):
            self.assert_no_effect("unit_revision_stale")


class AssignmentRaceTests(AssignmentFixture):
    def test_insert_race_replays_winner_without_overwriting_revision(self) -> None:
        original = self.state.upsert_state
        winner: list[dict[str, Any]] = []

        def race(namespace: str, query: dict[str, Any]) -> dict[str, Any]:
            if query["table"] == TABLE_MANAGED_DISPATCH and not winner:
                self.register.rebind(FIXTURE_UNIT_ID, replace(self.assigned.revision, revision_id="drv_race-winner"))
                with patch.object(self.state, "upsert_state", side_effect=original):
                    winner.append(prepare_managed_dispatch(self.state, self.spec, register=self.register, actor=_coordinator_actor(), now=T0))
            return original(namespace, query)

        with patch.object(self.state, "upsert_state", side_effect=race):
            observed = prepare_managed_dispatch(self.state, self.spec, register=self.register, actor=_coordinator_actor(), now=T0)
        self.assertEqual(observed, winner[0])
        self.assertEqual(assignment_from_record(observed).revision.revision_id, "drv_race-winner")
        self.assertEqual(len(cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)), 1)


class AssignmentReplayTests(AssignmentFixture):
    def test_duplicate_after_plan_edit_returns_same_attempt_without_register(self) -> None:
        first = self.dispatch()
        self.register.rebind(FIXTURE_UNIT_ID, replace(self.assigned.revision, revision_id="drv_edited"))
        with patch.object(self.register, "show_unit", side_effect=AssertionError("duplicate must replay")):
            second = self.dispatch()
        self.assertEqual(first, second)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(assignment_from_record(second["dispatch"]), self.assigned)

    def test_failed_launch_duplicate_replays_the_same_refusal(self) -> None:
        def refused(_row: Mapping[str, Any]) -> Mapping[str, Any]:
            raise DispatchError("host_cannot_spawn", "fixture refusal")

        with self.assertRaises(DispatchError) as first:
            dispatch_managed_work(self.state, self.spec, register=self.register, actor=_coordinator_actor(), spawn_attempt=refused, now=T0)
        with self.assertRaises(DispatchError) as second:
            self.dispatch()
        self.assertEqual((first.exception.code, first.exception.data), (second.exception.code, second.exception.data))
        self.assertFalse(self.launches)

    def test_changed_duplicate_is_not_silent_correction(self) -> None:
        self.dispatch()
        with self.assertRaises(DispatchError) as caught:
            self.dispatch(replace(self.spec, model="another-model"))
        self.assertEqual(caught.exception.code, "dispatch_id_conflict")
        self.assertEqual(len(self.launches), 1)

    def test_reentrant_duplicate_cannot_create_another_attempt(self) -> None:
        def during_launch(row: Mapping[str, Any]) -> Mapping[str, Any]:
            with self.assertRaises(DispatchError) as caught:
                self.dispatch()
            self.assertEqual(caught.exception.code, "dispatch_attempt_in_progress")
            return self.launch(row)

        dispatch_managed_work(self.state, self.spec, register=self.register, actor=_coordinator_actor(), spawn_attempt=during_launch, now=T0)
        self.assertEqual(len(self.launches), 1)

    def test_retry_keeps_old_revision_after_rebinding(self) -> None:
        first = self.dispatch()["dispatch"]
        self.register.rebind(FIXTURE_UNIT_ID, replace(self.assigned.revision, revision_id="drv_edited"))
        retried = self.retry(first)
        self.assertEqual(retried["attempt_number"], 2)
        self.assertEqual(assignment_from_record(retried), self.assigned)
        self.assertEqual(len(self.launches), 2)
        self.assertIn(f"revision:{self.assigned.revision.revision_id}", self.register.reads)

    def test_retirement_blocks_retry_without_launch(self) -> None:
        first = self.dispatch()["dispatch"]
        self.register.units[FIXTURE_UNIT_ID] = replace(self.register.units[FIXTURE_UNIT_ID], state="cancelled")
        retried = self.retry(first)
        self.assertEqual(retried["state"], "failed_start")
        self.assertIn("unit_not_dispatchable", retried["terminal_reason"])
        self.assertEqual(len(self.launches), 1)

    def test_retry_refuses_changed_repository_mapping(self) -> None:
        first = self.dispatch()["dispatch"]
        self.register.repository_id = "rep_other"
        retried = self.retry(first)
        self.assertEqual(retried["state"], "failed_start")
        self.assertIn("unit_repository_mismatch", retried["terminal_reason"])
        self.assertEqual(len(self.launches), 1)

    def test_launcher_first_turn_failure_is_not_overwritten_by_receipt(self) -> None:
        def failed_delivery(row: Mapping[str, Any]) -> Mapping[str, Any]:
            receipt = self.launch(row)
            record_first_turn_evidence(self.state, dispatch_id=self.spec.dispatch_id, agent_instance_id=str(receipt["agent_instance_id"]), source="charter", delivered=False, error="fixture transport refusal", host="headless", host_ref="recorded-only", agent_runtime="codex", observed_at=T0, register=None, actor=None)
            return receipt

        result = dispatch_managed_work(self.state, self.spec, register=self.register, actor=_coordinator_actor(), spawn_attempt=failed_delivery, now=T0)
        self.assertEqual(result["dispatch"]["state"], "uptake_uncertain")
        self.assertEqual(result["dispatch"]["first_turn_error"], "fixture transport refusal")

    def test_cancel_then_new_dispatch_is_explicit_correction(self) -> None:
        row = self.dispatch()["dispatch"]
        cancelled = resolve_managed_dispatch(self.state, dispatch_id=self.spec.dispatch_id, event_id="cancel", action="cancel", actor=_coordinator_actor(), prior_version=int(row["version"]), payload={"reason": "correct assignment"}, observed_at=T0, register=self.register)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.register.units[FIXTURE_UNIT_ID].state, "dispatched")
        self.register.rebind(FIXTURE_UNIT_ID, replace(self.assigned.revision, revision_id="drv_corrected"))
        new = self.dispatch(replace(self.spec, dispatch_id="mdp-corrected", expected_revision_id="drv_corrected"))
        self.assertEqual(assignment_from_record(new["dispatch"]).revision.revision_id, "drv_corrected")
        self.assertEqual(assignment_from_record(read_managed_dispatch(self.state, self.spec.dispatch_id)), self.assigned)

    def test_assignment_cannot_be_mutated_in_place(self) -> None:
        row = self.dispatch()["dispatch"]
        with self.assertRaises(DispatchError) as caught:
            _update_dispatch(self.state, row, {"assignment_sha256": "0" * 64})
        self.assertEqual(caught.exception.code, "unit_assignment_immutable")

    def test_stale_causal_write_cannot_change_assignment(self) -> None:
        row = self.dispatch()["dispatch"]
        _update_dispatch(self.state, row, {"next_required_action": "fixture-decision"})
        with self.assertRaises(DispatchError) as caught:
            _update_dispatch(self.state, row, {"state": "cancelled"})
        self.assertEqual(caught.exception.code, "stale_dispatch_version")
        self.assertEqual(assignment_from_record(read_managed_dispatch(self.state, self.spec.dispatch_id)), self.assigned)

    def test_ack_and_completion_require_pinned_assignment_identity(self) -> None:
        row = self.dispatch()["dispatch"]
        _seed_binding(self.state, role=self.spec.role_name, instance="agi-attempt-1", session="ases-attempt-1")
        actor = DispatchActor("agi-attempt-1", "ases-attempt-1", "live_peer_binding")
        common: dict[str, Any] = {"dispatch_id": self.spec.dispatch_id, "attempt_agent_instance_id": "agi-attempt-1", "actor": actor, "prior_version": int(row["version"]), "observed_at": T0}
        payload = {**_assignment_identity(row), "revision_id": "drv_wrong", "role_binding": self.spec.role_name, "scope_readback_sha256": "a" * 64, "plan_sha256": "b" * 64}
        with self.assertRaises(DispatchError) as caught:
            report_managed_dispatch(self.state, event_id="wrong-ack", event_kind="ack", payload=payload, **common)
        self.assertEqual(caught.exception.code, "assignment_identity_mismatch")
        payload.update(_assignment_identity(row))
        active = report_managed_dispatch(self.state, event_id="ack", event_kind="ack", payload=payload, **common)
        common["prior_version"] = int(active["version"])
        with self.assertRaises(DispatchError) as caught:
            report_managed_dispatch(self.state, event_id="wrong-completion", event_kind="completion", payload={}, **common)
        self.assertEqual(caught.exception.code, "assignment_identity_mismatch")

    def test_historical_path_row_remains_readable_but_cannot_resume(self) -> None:
        self.dispatch()
        raw = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0]
        raw.pop("assignment")
        raw["brief_ref"] = "/historical/file.md"
        raw["brief_sha256"] = "a" * 64
        row = read_managed_dispatch(self.state, self.spec.dispatch_id)
        self.assertEqual(row["brief_ref"], "/historical/file.md")
        with self.assertRaises(DispatchError) as caught:
            self.dispatch()
        self.assertEqual(caught.exception.code, "unit_assignment_historical")
        with self.assertRaises(DispatchError):
            _update_dispatch(self.state, row, {"state": "preparing"})
        self.assertEqual(len(self.launches), 1)

    def test_schema_keeps_only_nullable_historical_file_columns(self) -> None:
        schema = get_managed_dispatch_schema()
        for name in ("brief_ref", "brief_sha256", "assignment", "assignment_sha256", "request_sha256"):
            self.assertFalse(schema.columns[name].not_null)


class RestoredInvariantTests(AssignmentFixture):
    def test_one_live_dispatch_per_unit_refuses_second_id_without_launch(self) -> None:
        original = self.dispatch()["dispatch"]
        with self.assertRaises(DispatchError) as caught:
            self.dispatch(replace(self.spec, dispatch_id="mdp-other-live"))
        self.assertEqual(caught.exception.code, "unit_already_dispatched")
        self.assertEqual(len(self.launches), 1)
        rows = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)
        self.assertEqual(len(rows), 1)
        self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), original)

    def test_annotation_failure_is_recorded_without_blocking_transitions(self) -> None:
        failure = RegisterUnitError("unit_register_unavailable", "fixture annotation outage")
        with patch.object(self.register, "record_unit_event", side_effect=failure):
            row = self.dispatch()["dispatch"]
            self.assertEqual(row["state"], "uptake_pending")
            cancelled = resolve_managed_dispatch(
                self.state,
                dispatch_id=self.spec.dispatch_id,
                event_id="cancel-annotation-outage",
                action="cancel",
                actor=_coordinator_actor(),
                prior_version=int(row["version"]),
                payload={"reason": "fixture cancellation"},
                observed_at=T0,
                register=self.register,
            )
        self.assertEqual(cancelled["state"], "cancelled")
        events = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH_EVENT)
        failures = [event for event in events if event["event_kind"] == "register_annotation_failed"]
        self.assertEqual([event["payload"]["event"] for event in failures], ["spawned", "cancelled"])
        for event in failures:
            self.assertEqual(event["payload"]["code"], failure.code)
            self.assertEqual(event["payload"]["detail"], failure.detail)
            self.assertEqual(event["payload"]["unit_id"], FIXTURE_UNIT_ID)
            self.assertTrue(event["accepted"])
        self.assertEqual(len(self.launches), 1)

    def test_same_instance_wrong_coordinator_session_refuses_before_register_read(self) -> None:
        _seed_binding(self.state, role="Coordinator-Main", instance="agi-coordinator", session="ases-replaced")
        self.assert_no_effect("coordinator_authority_denied")
        self.assertFalse(self.register.reads)

    def test_stored_assignment_digest_and_unit_tampering_refuse_read_and_replay(self) -> None:
        self.dispatch()
        raw = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0]
        for field, value in (("assignment_sha256", "0" * 64), ("unit_id", "unt_tampered")):
            with self.subTest(field=field), patch.dict(raw, {field: value}):
                with self.assertRaises(DispatchError) as status_error:
                    managed_dispatch_status(self.state, self.spec.dispatch_id, now=T0)
                self.assertEqual(status_error.exception.code, "unit_assignment_mismatch")
                with self.assertRaises(DispatchError) as replay_error:
                    self.dispatch()
                self.assertEqual(replay_error.exception.code, "unit_assignment_mismatch")
        self.assertEqual(len(self.launches), 1)

    def test_retry_refuses_different_valid_bytes_under_same_pinned_revision(self) -> None:
        row = self.dispatch()["dispatch"]
        text = "different but internally valid registered bytes\n"
        revision = replace(self.assigned.revision, text=text, byte_length=len(text.encode()), sha256=hashlib.sha256(text.encode()).hexdigest())
        revision.verify()
        self.register.revisions[revision.revision_id] = revision
        result = self.retry(row)
        self.assertEqual(result["state"], "failed_start")
        self.assertIn("unit_revision_mismatch", result["terminal_reason"])
        self.assertEqual(assignment_from_record(result), self.assigned)
        self.assertEqual(len(self.launches), 1)

    def test_repository_is_resolved_from_lane_root_never_cwd(self) -> None:
        with chdir(self.tmp.name):
            self.assertNotEqual(Path.cwd(), REPO_ROOT)
            row = self.dispatch()["dispatch"]
            result = self.retry(row)
        self.assertEqual(result["state"], "uptake_pending")
        roots = [read for read in self.register.reads if read.startswith("repository:")]
        self.assertEqual(roots, [f"repository:{REPO_ROOT}", f"repository:{REPO_ROOT}"])
        self.assertEqual(len(self.launches), 2)


class _HistoricalCancellationFixture(AssignmentFixture):
    def setUp(self) -> None:
        super().setUp()
        self._seed_main()

    def _seed_main(self) -> None:
        _seed_binding(self.state, role="Fixture-Main", instance="agi-current-main", session="ases-current-main")

    def _main_actor(self) -> DispatchActor:
        return DispatchActor("agi-current-main", "ases-current-main", "live_peer_binding")

    def _historical_row(self, state_name: str = "active", *, unit_id: str = FIXTURE_UNIT_ID) -> dict[str, Any]:
        self.dispatch()
        raw = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0]
        for field in ("assignment", "assignment_sha256", "request_sha256"):
            raw.pop(field)
        raw.update(state=state_name, unit_id=unit_id, brief_ref="/historical/file.md", brief_sha256="a" * 64)
        return read_managed_dispatch(self.state, self.spec.dispatch_id)

    def _cancel(self, row: Mapping[str, Any], *, event_id: str = "cancel-historical", actor: DispatchActor | None = None, prior_version: int | None = None) -> dict[str, Any]:
        return resolve_managed_dispatch(
            self.state,
            dispatch_id=self.spec.dispatch_id,
            event_id=event_id,
            action="cancel",
            actor=actor or self._main_actor(),
            prior_version=int(row["version"]) if prior_version is None else prior_version,
            payload={"reason": "explicit historical close-out"},
            observed_at=T0,
            register=self.register,
            spawn_attempt=self.launch,
            historical_cancel_role="Fixture-Main",
        )


class HistoricalCancellationTests(_HistoricalCancellationFixture):
    def test_all_nonterminal_historical_states_cancel_without_conversion(self) -> None:
        for state_name in sorted(DISPATCH_NONTERMINAL_STATES):
            with self.subTest(state=state_name):
                self.state = _state()
                self._seed_main()
                row = self._historical_row(state_name)
                status = managed_dispatch_status(self.state, self.spec.dispatch_id, now=T0)
                self.assertEqual(status["assignment_status"], "historical")
                self.assertEqual(status["allowed_actions"], ["cancel"])
                self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), row)
                validate_resolution_admission(self.state, dispatch_id=self.spec.dispatch_id, event_id="cancel-historical", action="cancel", actor=self._main_actor(), historical_cancel_role="Fixture-Main")
                with patch.object(self.register, "record_unit_event", side_effect=AssertionError("must not annotate old rows")):
                    cancelled = self._cancel(row)
                expected = dict(row, state="cancelled", terminal_reason="explicit historical close-out", next_required_action="none", responsible_role="", version=int(row["version"]) + 1, cancelled_by_instance_id="agi-current-main")
                self.assertEqual(cancelled, expected)
                self.assertEqual(self._cancel(row), cancelled)
                events = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH_EVENT)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event_kind"], "resolve:cancel")
                self.assertTrue(events[0]["accepted"])
                self.assertEqual(events[0]["actor_role"], "Fixture-Main")
                self.assertEqual(events[0]["actor_instance_id"], "agi-current-main")
                self.assertEqual(managed_dispatch_status(self.state, self.spec.dispatch_id, now=T0)["allowed_actions"], [])
        self.assertEqual(len(self.launches), len(DISPATCH_NONTERMINAL_STATES))

    def test_cancel_unpins_unit_for_new_native_dispatch(self) -> None:
        row = self._historical_row("completion_reported")
        with self.assertRaises(DispatchError) as caught:
            self.dispatch(replace(self.spec, dispatch_id="mdp-after-history"))
        self.assertEqual(caught.exception.code, "unit_already_dispatched")
        self._cancel(row)
        new = self.dispatch(replace(self.spec, dispatch_id="mdp-after-history"))
        self.assertEqual(assignment_from_record(new["dispatch"]), self.assigned)
        self.assertEqual(len(self.launches), 2)

    def test_historical_row_without_unit_or_with_null_assignment_can_cancel(self) -> None:
        row = self._historical_row(unit_id="")
        raw = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0]
        raw["assignment"] = None
        cancelled = self._cancel(row)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["unit_id"], "")
        self.assertIsNone(cancelled["assignment"])
        self.assertNotIn("assignment_sha256", cancelled)

    def test_historical_cancel_retains_session_binding_and_causal_checks(self) -> None:
        row = self._historical_row()
        wrong = replace(self._main_actor(), agent_session_id="ases-wrong")
        for event_id, actor, version, code in (
            ("wrong-session", wrong, int(row["version"]), "historical_cancel_caller_mismatch"),
            ("stale", self._main_actor(), int(row["version"]) - 1, "stale_dispatch_version"),
        ):
            with self.subTest(code=code), self.assertRaises(DispatchError) as caught:
                self._cancel(row, event_id=event_id, actor=actor, prior_version=version)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), row)
        self.assertEqual(self._cancel(row)["state"], "cancelled")
        with self.assertRaises(DispatchError) as terminal:
            self._cancel(read_managed_dispatch(self.state, self.spec.dispatch_id), event_id="second-cancel")
        self.assertEqual(terminal.exception.code, "cancel_not_allowed")

    def test_historical_rows_refuse_every_other_resolution_before_effect(self) -> None:
        row = self._historical_row()
        for action in ("request_retry", "resolve_blocker", "accept_completion", "reject_completion", "accept_verified_legacy_completion", "cancel_legacy_expiry"):
            with self.subTest(action=action):
                with self.assertRaises(DispatchError) as admission:
                    validate_resolution_admission(self.state, dispatch_id=self.spec.dispatch_id, event_id=action, action=action, actor=_coordinator_actor())
                self.assertEqual(admission.exception.code, "unit_assignment_historical")
                with self.assertRaises(DispatchError) as transition:
                    resolve_managed_dispatch(self.state, dispatch_id=self.spec.dispatch_id, event_id=action, action=action, actor=_coordinator_actor(), prior_version=int(row["version"]), payload={"reason": "forbidden"}, observed_at=T0, register=self.register, spawn_attempt=self.launch)
                self.assertEqual(transition.exception.code, "unit_assignment_historical")
        self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), row)
        self.assertEqual(len(self.launches), 1)

    def test_malformed_native_assignment_cannot_use_historical_cancel_or_status(self) -> None:
        row = self.dispatch()["dispatch"]
        raw = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0]
        raw["assignment"] = {}
        with self.assertRaises(DispatchError):
            self._cancel(row)
        with self.assertRaises(DispatchError):
            managed_dispatch_status(self.state, self.spec.dispatch_id, now=T0)
        self.assertEqual(raw["state"], row["state"])

    def test_current_main_cancels_rows_from_different_spawners_and_roles(self) -> None:
        row = self._historical_row()
        raw = cast(RealShapeState, self.state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH)[0]
        for role in ("Fixture-Main", "Former-Coordinator", "Other-Main"):
            with self.subTest(role=role), patch.dict(raw, {**row, "spawned_by_role": role, "spawned_by_instance_id": "agi-former-spawner"}):
                cancelled = self._cancel(raw, event_id="cancel-former-" + role)
                self.assertEqual(cancelled["state"], "cancelled")
                self.assertEqual(cancelled["cancelled_by_instance_id"], self._main_actor().agent_instance_id)
                self.assertEqual(cancelled["terminal_reason"], "explicit historical close-out")
                self.assertEqual(cancelled["spawned_by_role"], role)
                self.assertEqual(cancelled["spawned_by_instance_id"], "agi-former-spawner")

    def test_historical_cancel_refuses_caller_without_current_main_binding(self) -> None:
        row = self._historical_row()
        with self.assertRaises(DispatchError) as caught:
            self._cancel(row, actor=_coordinator_actor())
        self.assertEqual(caught.exception.code, "historical_cancel_caller_mismatch")
        self.assertEqual(caught.exception.message, "The caller does not hold the configured historical_cancel_role binding.")
        with self.assertRaises(DispatchError) as admission:
            validate_resolution_admission(self.state, dispatch_id=self.spec.dispatch_id, event_id="cancel", action="cancel", actor=_coordinator_actor(), historical_cancel_role="Fixture-Main")
        self.assertEqual(admission.exception.code, "historical_cancel_caller_mismatch")
        self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), row)

    def test_native_row_still_refuses_current_main_when_not_its_spawner(self) -> None:
        row = self.dispatch()["dispatch"]
        with self.assertRaises(DispatchError) as caught:
            self._cancel(row)
        self.assertEqual(caught.exception.code, "coordinator_authority_denied")
        with self.assertRaises(DispatchError) as admission:
            validate_resolution_admission(self.state, dispatch_id=self.spec.dispatch_id, event_id="cancel", action="cancel", actor=self._main_actor(), historical_cancel_role="Fixture-Main")
        self.assertEqual(admission.exception.code, "coordinator_authority_denied")
        self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), row)

    def test_historical_cancel_requires_reason_and_records_caller_on_row(self) -> None:
        row = self._historical_row()
        with self.assertRaises(DispatchError) as caught:
            resolve_managed_dispatch(self.state, dispatch_id=self.spec.dispatch_id, event_id="no-reason", action="cancel", actor=self._main_actor(), prior_version=int(row["version"]), payload={"reason": "  "}, observed_at=T0, historical_cancel_role="Fixture-Main")
        self.assertEqual(caught.exception.code, "reason_required")
        self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), row)
        self.assertFalse(get_managed_dispatch_schema().columns["cancelled_by_instance_id"].not_null)
        cancelled = self._cancel(row)
        self.assertEqual(cancelled["terminal_reason"], "explicit historical close-out")
        self.assertEqual(cancelled["cancelled_by_instance_id"], "agi-current-main")

    def test_unknown_states_are_not_assumed_nonterminal(self) -> None:
        native = self.dispatch()["dispatch"] | {"state": "unknown-state"}
        self.state = _state()
        self._seed_main()
        self.spec = replace(self.spec, dispatch_id="historical-unknown")
        row = self._historical_row("unknown-state")
        self.assertEqual(managed_dispatch_status(self.state, self.spec.dispatch_id, now=T0)["allowed_actions"], [])
        with self.assertRaises(DispatchError) as caught:
            self._cancel(row)
        self.assertEqual(caught.exception.code, "cancel_not_allowed")
        self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), row)
        with self.assertRaises(DispatchError) as native_caught:
            _cancel_updates(native, {}, T0, "Coordinator-Main")
        self.assertEqual(native_caught.exception.code, "cancel_not_allowed")


class HistoricalConfiguredRoleTests(_HistoricalCancellationFixture):
    def _plugin(self, config: dict[str, object]) -> AgentMessagingPlugin:
        plugin = AgentMessagingPlugin()
        plugin.initialize(config)
        for name, value in (("_get_state_service", self.state), ("_dispatch_actor_from_state", self._main_actor()), ("_register_unit_client", self.register)):
            patched = patch.object(plugin, name, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)
        return plugin

    def _request(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {"dispatch_id": self.spec.dispatch_id, "event_id": "public-historical-cancel", "action": "cancel", "prior_version": row["version"], "payload": {"reason": "public configured close-out"}}

    def test_configured_plugin_passes_preflight_and_cancels(self) -> None:
        row = self._historical_row()
        plugin = self._plugin({"historical_cancel_role": "Fixture-Main"})
        raw = self._request(row)
        _resolution_preflight(plugin, raw, {})
        result = plugin.resolve_managed_dispatch(raw, {})
        self.assertEqual(result["action_status"], "completed")
        self.assertEqual(result["data"]["state"], "cancelled")
        self.assertEqual(result["data"]["terminal_reason"], "public configured close-out")
        self.assertEqual(result["data"]["cancelled_by_instance_id"], self._main_actor().agent_instance_id)
        self.assertNotIn("assignment", result["data"])

    def test_unset_plugin_key_refuses_even_if_request_supplies_role(self) -> None:
        row = self._historical_row()
        plugin = self._plugin({})
        raw = dict(self._request(row), historical_cancel_role="Fixture-Main")
        with self.assertRaises(DispatchError) as caught:
            _resolution_preflight(plugin, raw, {})
        self.assertEqual(caught.exception.code, "historical_cancel_role_required")
        self.assertIn("historical_cancel_role", caught.exception.message)
        result = plugin.resolve_managed_dispatch(raw, {})
        self.assertEqual(result["action_status"], "failed")
        self.assertEqual(result["error"]["code"], "historical_cancel_role_required")
        self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), row)

    def test_configured_role_must_match_current_binding(self) -> None:
        row = self._historical_row()
        plugin = self._plugin({"historical_cancel_role": "Different-Main"})
        with self.assertRaises(DispatchError) as caught:
            _resolution_preflight(plugin, self._request(row), {})
        self.assertEqual(caught.exception.code, "historical_cancel_caller_mismatch")
        result = plugin.resolve_managed_dispatch(self._request(row), {})
        self.assertEqual(result["error"]["code"], "historical_cancel_caller_mismatch")
        self.assertEqual(read_managed_dispatch(self.state, self.spec.dispatch_id), row)


_FAKE_CLI = r"""#!/usr/bin/env python3
import json, pathlib, sys
base = pathlib.Path(__file__).parent
scenario = json.loads((base / "scenario.json").read_text())
args = sys.argv[1:]
if scenario.get("failure"):
    print(scenario["failure"], file=sys.stderr)
    raise SystemExit(2)
with (base / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
if args[:3] == ["db", "repository", "resolve"]:
    print(json.dumps({"repository_id": "rep_fixture-solet"}))
elif args[:3] == ["db", "unit", "show"]:
    print(json.dumps(scenario["unit"]))
elif args[:2] in (["brief", "show"], ["brief", "show-revision"]):
    metadata = dict(scenario["metadata"])
    if args[1] == "show-revision":
        assert args[2] == "--revision-id"
    sys.stdout.buffer.write(scenario["text"].encode("utf-8"))
    print(json.dumps(metadata), file=sys.stderr)
elif args[:3] == ["db", "unit", "evidence"]:
    cursor = args[args.index("--landing-cursor") + 1] if "--landing-cursor" in args else ""
    print(json.dumps(scenario["evidence_pages"][cursor]))
elif args[:3] in (["db", "candidate-revision", "list"], ["db", "reference", "list"]):
    limit, offset = (int(args[args.index(flag) + 1]) for flag in ("--limit", "--offset"))
    print(json.dumps(scenario[args[1]][offset:offset + limit]))
else:
    raise SystemExit("unexpected command: " + repr(args))
"""


class RegisterCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / "psolet-fixture"
        self.binary.write_text(_FAKE_CLI, encoding="utf-8")
        self.binary.chmod(0o700)
        text = "registered Unicode café\r\nno trailing newline"
        self.scenario: dict[str, Any] = {
            "text": text,
            "metadata": {"revision_id": "drv_fixture", "unit_id": FIXTURE_UNIT_ID, "unit_key": "fixture-key", "sha256": hashlib.sha256(text.encode()).hexdigest(), "byte_length": len(text.encode())},
            "unit": {"unit": [{"id": FIXTURE_UNIT_ID, "unit_key": "fixture-key", "kind": "infrastructure", "repository_id": "rep_fixture-solet", "is_deleted": False, "brief_document_revision_id": "drv_fixture"}], "unit_state_event": [{"to_state": "dispatched", "observed_at": "2026-10-09", "created_at": "2026-10-09"}]},
        }
        self.save()
        self.client = PsoletRegisterUnitClient(str(self.binary), solet_name="fixture")
        self.reader = PsoletRetirementEvidenceReader(str(self.binary), solet_name="fixture")

    def save(self) -> None:
        (self.root / "scenario.json").write_text(json.dumps(self.scenario), encoding="utf-8")

    def test_cli_exact_bytes_and_native_verbs(self) -> None:
        self.assertEqual(self.client.resolve_repository(self.root), "rep_fixture-solet")
        unit = self.client.show_unit(FIXTURE_UNIT_ID)
        brief = self.client.read_brief(unit)
        self.assertEqual(brief.text, self.scenario["text"])
        self.assertEqual(self.client.read_revision("drv_fixture"), brief)
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(calls[-2:], [["brief", "show", FIXTURE_UNIT_ID], ["brief", "show-revision", "--revision-id", "drv_fixture"]])
        self.assertFalse(any("dispatched" in call or "--brief" in call for call in calls))

    def test_cli_wrong_unit_provenance_refuses(self) -> None:
        unit = self.client.show_unit(FIXTURE_UNIT_ID)
        self.scenario["metadata"]["unit_id"] = "unt_wrong"
        self.save()
        with self.assertRaises(RegisterUnitError) as caught:
            self.client.read_brief(unit)
        self.assertEqual(caught.exception.code, "unit_identity_mismatch")

    def test_cli_wrong_revision_refuses(self) -> None:
        self.scenario["metadata"]["revision_id"] = "drv_wrong"
        self.save()
        with self.assertRaises(RegisterUnitError) as caught:
            self.client.read_revision("drv_fixture")
        self.assertEqual(caught.exception.code, "unit_revision_stale")

    def test_cli_bad_byte_length_and_digest_refuse(self) -> None:
        for field, value in (("byte_length", 1), ("sha256", "0" * 64)):
            with self.subTest(field=field):
                original = self.scenario["metadata"][field]
                self.scenario["metadata"][field] = value
                self.save()
                with self.assertRaises(RegisterUnitError) as caught:
                    self.client.read_revision("drv_fixture")
                self.assertEqual(caught.exception.code, "unit_revision_mismatch")
                self.scenario["metadata"][field] = original

    def test_cli_outage_and_timeout_are_named(self) -> None:
        for error in (OSError("missing"), subprocess.TimeoutExpired("psolet", 1)):
            with self.subTest(error=type(error).__name__), patch("subprocess.run", side_effect=error):
                with self.assertRaises(RegisterUnitError) as caught:
                    self.client.show_unit(FIXTURE_UNIT_ID)
                self.assertEqual(caught.exception.code, "unit_register_unavailable")

    def test_cli_missing_unit_and_database_outage_stay_distinct(self) -> None:
        messages = (
            (f"project-solet: <unit_id>: unit prefix {FIXTURE_UNIT_ID!r} resolves to zero records; candidates: none", "unit_not_found"),
            ("project-solet: connection refused", "unit_register_unavailable"),
        )
        for message, expected in messages:
            self.scenario["failure"] = message
            self.save()
            with self.assertRaises(RegisterUnitError) as caught:
                self.client.show_unit(FIXTURE_UNIT_ID)
            self.assertEqual(caught.exception.code, expected)

    def test_cli_missing_unit_payload_refuses(self) -> None:
        with self.assertRaises(RegisterUnitError) as caught:
            parse_unit_show(FIXTURE_UNIT_ID, {"unit": []})
        self.assertEqual(caught.exception.code, "unit_not_found")

    def _calls(self) -> list[list[str]]:
        return [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]

    def test_cli_unit_evidence_follows_the_landing_cursor(self) -> None:
        first = {"unit_id": FIXTURE_UNIT_ID, "landings": {"rows": [{"merge_sha": "a"}], "truncated": True, "next_cursor": "c2"}}
        second = {"unit_id": FIXTURE_UNIT_ID, "landings": {"rows": [{"merge_sha": "b"}], "truncated": False, "next_cursor": None}}
        self.scenario["evidence_pages"] = {"": first, "c2": second}
        self.save()
        evidence = self.reader.read_unit_evidence(FIXTURE_UNIT_ID)
        self.assertEqual([row["merge_sha"] for row in evidence["landings"]["rows"]], ["a", "b"])
        self.assertEqual(
            self._calls(),
            [
                ["db", "unit", "evidence", FIXTURE_UNIT_ID, "--limit", "100"],
                ["db", "unit", "evidence", FIXTURE_UNIT_ID, "--limit", "100", "--landing-cursor", "c2"],
            ],
        )

    def test_cli_truncated_landings_without_cursor_refuse(self) -> None:
        self.scenario["evidence_pages"] = {"": {"landings": {"rows": [], "truncated": True, "next_cursor": None}}}
        self.save()
        with self.assertRaises(RegisterUnitError) as caught:
            self.reader.read_unit_evidence(FIXTURE_UNIT_ID)
        self.assertEqual(caught.exception.code, "unit_register_invalid")

    def test_cli_unit_listings_page_by_offset_to_the_end(self) -> None:
        self.scenario["candidate-revision"] = [{"revision_id": f"crv_{index}"} for index in range(101)]
        self.scenario["reference"] = []
        self.save()
        self.assertEqual(len(self.reader.list_candidate_revisions(FIXTURE_UNIT_ID)), 101)
        self.assertEqual(self.reader.list_unit_references(FIXTURE_UNIT_ID), [])
        self.assertEqual(
            self._calls(),
            [
                ["db", "candidate-revision", "list", "--unit", FIXTURE_UNIT_ID, "--limit", "100", "--offset", "0"],
                ["db", "candidate-revision", "list", "--unit", FIXTURE_UNIT_ID, "--limit", "100", "--offset", "100"],
                ["db", "reference", "list", "--unit", FIXTURE_UNIT_ID, "--limit", "100", "--offset", "0"],
            ],
        )

    def test_cli_unit_listing_of_non_objects_refuses(self) -> None:
        self.scenario["reference"] = [1]
        self.save()
        with self.assertRaises(RegisterUnitError) as caught:
            self.reader.list_unit_references(FIXTURE_UNIT_ID)
        self.assertEqual(caught.exception.code, "unit_register_invalid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
