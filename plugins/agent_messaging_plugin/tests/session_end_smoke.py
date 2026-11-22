#!/usr/bin/env python3
"""Smoke for the one writer of a session end (engine-6 a1).

A host notice is a trigger, never proof: a record ends only after the exact
host check says gone; a check that cannot be made leaves the record exactly as
it is and is counted. A session's own report of its end needs no host check.

Checks E0-E17, one test method each. tmux is patched at
``host_end_notice._run`` and state is in memory: no test touches a real tmux
server or creates a file.

Run:
    SOLET_NAME=<name>-test PYTHONPATH=plugins/agent_messaging_plugin/src:ananta/src \
        .venv/bin/python3 plugins/agent_messaging_plugin/tests/session_end_smoke.py
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

from agent_messaging_plugin import host_end_notice, session_end  # noqa: E402
from agent_messaging_plugin.host_end_notice import HostObservation, observe_tmux_session  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_DEPENDENCY,
    TABLE_SESSION_TRANSITION,
)
from agent_messaging_plugin.session_end import (  # noqa: E402
    SessionEndError,
    end_records_if_gone,
    end_reported_session,
    end_session_record,
    observe_host,
    on_host_session_closed,
)
from agent_messaging_plugin.session_end_reason import (  # noqa: E402
    END_REASONS,
    FAILED,
    FINISHED,
    REFUSED_BY_PROVIDER,
    STOPPED_BY_ASKER,
    STOPPED_BY_SOLET,
    UNKNOWN,
    end_reason_of,
    require_end_reason,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    ArmSessionDependencyRequest,
    list_sessions,
    session_status,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    arm_session_dependency as verb_arm_session_dependency,
)

TMUX_ARGV = ["/fake/tmux"]
GONE_STDERR = "can't find session: fleet-a-1"
DENIED_STDERR = "error connecting to /tmp/tmux-501/default (Permission denied)"


class _FakeRun:
    """Stands in for ``subprocess.run``: records argv, returns a fixed answer."""

    def __init__(self, returncode: int = 0, stderr: str = "", raises: Exception | None = None) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> Any:
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr=self.stderr)


class _Orchestrator:
    """Minimal state-service provider for the public verb fixture."""

    def __init__(self, state: StateManagementInterface) -> None:
        self._state = state

    def get_service(self, name: str) -> StateManagementInterface | None:
        return self._state if name == "state_service" else None


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _insert(
    state: StateManagementInterface,
    agent_instance_id: str,
    *,
    host: str = "tmux",
    host_ref: str | None = "fleet-a-1",
    to_state: str = LIFECYCLE_LIVE,
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            label="session-end-fixture",
            session_origin="registered" if host == "operator" else "spawned",
            host=host,
        ),
    )
    if host_ref is not None:
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": agent_instance_id}},
            {"host_ref": host_ref},
        )
    if to_state != LIFECYCLE_SPAWNING:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="fixture",
        )
    if to_state == LIFECYCLE_IDLE:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_IDLE,
            directed_by="fixture",
        )


def _row(state: StateManagementInterface, agent_instance_id: str) -> dict[str, Any]:
    return read_managed_session(state, agent_instance_id)


def _reasons(state: StateManagementInterface, agent_instance_id: str, to_state: str) -> list[str]:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_TRANSITION,
            "filters": {"agent_instance_id": agent_instance_id, "to_state": to_state},
        },
    )
    return [str(row["reason"]) for row in require_records(result)]


def _patched(fake: _FakeRun) -> Any:
    return mock.patch.object(host_end_notice, "_run", fake)


class SessionEndSmoke(unittest.TestCase):
    def test_e0_modules_load_from_the_worktree(self) -> None:
        for module in (session_end, host_end_notice):
            self.assertTrue(
                Path(str(module.__file__)).resolve().is_relative_to(REPO_ROOT),
                f"{module.__name__} loaded from {module.__file__}, outside {REPO_ROOT}",
            )

    def test_e1_observe_tmux_session_classifies_exactly(self) -> None:
        cases: list[tuple[_FakeRun, str]] = [
            (_FakeRun(0), "present"),
            (_FakeRun(1, "can't find session: x"), "gone"),
            (_FakeRun(1, "no server running on /p"), "gone"),
            (_FakeRun(1, "error connecting to /p (No such file or directory)"), "gone"),
            (_FakeRun(1, DENIED_STDERR), "unknown"),
            (_FakeRun(2, GONE_STDERR), "unknown"),
            (_FakeRun(raises=OSError("tmux not found")), "unknown"),
            (_FakeRun(raises=subprocess.TimeoutExpired(cmd="tmux", timeout=5)), "unknown"),
        ]
        for fake, expected in cases:
            with _patched(fake):
                observation = observe_tmux_session("fleet-a-1", tmux=TMUX_ARGV)
            self.assertIsInstance(observation, HostObservation)
            self.assertEqual(observation.state, expected, f"{fake.returncode} {fake.stderr!r} {fake.raises!r}")
            self.assertEqual(fake.calls, [[*TMUX_ARGV, "has-session", "-t", "=fleet-a-1"]])
            self.assertEqual(fake.calls[0][-3:], ["has-session", "-t", "=fleet-a-1"])
        with _patched(_FakeRun(1, DENIED_STDERR)):
            self.assertEqual(observe_tmux_session("fleet-a-1", tmux=TMUX_ARGV).detail, DENIED_STDERR)
        with _patched(_FakeRun(raises=OSError("tmux not found"))):
            self.assertEqual(observe_tmux_session("fleet-a-1", tmux=TMUX_ARGV).detail, "OSError: tmux not found")

    def test_e2_a_closed_host_session_ends_every_record_that_shares_it(self) -> None:
        state = _state()
        _insert(state, "agi-end-a")
        _insert(state, "agi-end-b", to_state=LIFECYCLE_IDLE)
        fake = _FakeRun(1, GONE_STDERR)
        with _patched(fake):
            result = on_host_session_closed(state, host="tmux", host_ref="fleet-a-1", directed_by="tmux:test", tmux=TMUX_ARGV)
        self.assertEqual(result["ended"], ["agi-end-a", "agi-end-b"])
        self.assertEqual(len(fake.calls), 1, "one distinct host session is observed once")
        for agent_instance_id in ("agi-end-a", "agi-end-b"):
            row = _row(state, agent_instance_id)
            self.assertEqual(row["lifecycle_state"], LIFECYCLE_TERMINATED)
            self.assertEqual(row["host_liveness"], "dead")
            self.assertEqual(row["host_liveness_detail"], GONE_STDERR)
            reasons = _reasons(state, agent_instance_id, LIFECYCLE_TERMINATED)
            self.assertEqual(len(reasons), 1)
            self.assertTrue(reasons[0].startswith("ended: tmux reported host session fleet-a-1 closed"), reasons[0])

    def test_e3_a_present_host_session_ends_nothing(self) -> None:
        state = _state()
        _insert(state, "agi-pres-a")
        _insert(state, "agi-pres-b")
        with _patched(_FakeRun(0)):
            result = on_host_session_closed(state, host="tmux", host_ref="fleet-a-1", directed_by="tmux:test", tmux=TMUX_ARGV)
        self.assertEqual(result["present"], ["agi-pres-a", "agi-pres-b"])
        self.assertEqual(result["ended"], [])
        for agent_instance_id in ("agi-pres-a", "agi-pres-b"):
            self.assertEqual(_row(state, agent_instance_id)["lifecycle_state"], LIFECYCLE_LIVE)
            self.assertEqual(_reasons(state, agent_instance_id, LIFECYCLE_TERMINATED), [])

    def test_e4_a_host_that_cannot_be_checked_leaves_the_records_as_they_are(self) -> None:
        state = _state()
        _insert(state, "agi-unk-a")
        _insert(state, "agi-unk-b")
        before = {key: _row(state, key) for key in ("agi-unk-a", "agi-unk-b")}
        with _patched(_FakeRun(1, DENIED_STDERR)):
            result = on_host_session_closed(state, host="tmux", host_ref="fleet-a-1", directed_by="tmux:test", tmux=TMUX_ARGV)
        self.assertEqual(result["ended"], [])
        self.assertEqual(
            result["unknown"],
            [
                {"agent_instance_id": "agi-unk-a", "detail": DENIED_STDERR},
                {"agent_instance_id": "agi-unk-b", "detail": DENIED_STDERR},
            ],
        )
        for agent_instance_id, row_before in before.items():
            self.assertEqual(_row(state, agent_instance_id), row_before)

    def test_e5_other_records_are_untouched(self) -> None:
        state = _state()
        _insert(state, "agi-mine")
        _insert(state, "agi-other-ref", host_ref="fleet-b-2")
        _insert(state, "agi-already-over")
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-already-over",
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_TERMINATED,
            directed_by="fixture",
            reason="terminated earlier",
        )
        over_before = _row(state, "agi-already-over")
        other_before = _row(state, "agi-other-ref")
        with _patched(_FakeRun(1, GONE_STDERR)):
            result = on_host_session_closed(state, host="tmux", host_ref="fleet-a-1", directed_by="tmux:test", tmux=TMUX_ARGV)
        self.assertEqual(result["ended"], ["agi-mine"])
        self.assertEqual(_row(state, "agi-already-over"), over_before)
        self.assertEqual(_row(state, "agi-other-ref"), other_before)
        self.assertEqual(_reasons(state, "agi-already-over", LIFECYCLE_TERMINATED), ["terminated earlier"])

    def test_e6_ending_a_record_fires_its_session_terminal_edge(self) -> None:
        state = _state()
        _insert(state, "agi-watched")
        verb_arm_session_dependency(
            state,
            ArmSessionDependencyRequest(
                waiter_instance_id="agi-waiter01",
                condition_kind=CONDITION_SESSION_TERMINAL,
                condition_ref="agi-watched",
            ),
        )

        def edges() -> list[dict[str, Any]]:
            result = state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {"table": TABLE_SESSION_DEPENDENCY, "filters": {"condition_ref": "agi-watched"}},
            )
            return require_records(result)

        self.assertIsNone(edges()[0].get("fired_at"))
        with _patched(_FakeRun(1, GONE_STDERR)):
            on_host_session_closed(state, host="tmux", host_ref="fleet-a-1", directed_by="tmux:test", tmux=TMUX_ARGV)
        self.assertTrue(edges()[0].get("fired_at"), "the armed edge fired when the record ended")

    def test_e7_a_record_that_moved_since_it_was_read_is_not_ended(self) -> None:
        state = _state()
        _insert(state, "agi-moved")
        stale_row = _row(state, "agi-moved")
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-moved",
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_IDLE,
            directed_by="fixture",
        )
        observation = HostObservation(state="gone", detail=GONE_STDERR)
        self.assertFalse(end_session_record(state, stale_row, why="test", observation=observation, directed_by="test"))
        self.assertEqual(_row(state, "agi-moved")["lifecycle_state"], LIFECYCLE_IDLE)
        with _patched(_FakeRun(1, GONE_STDERR)):
            swept = end_records_if_gone(
                state,
                [stale_row],
                cause="tmux reported host session fleet-a-1 closed",
                directed_by="test",
                tmux=TMUX_ARGV,
            )
        self.assertEqual(swept["changed_meanwhile"], ["agi-moved"])
        self.assertEqual(swept["ended"], [])
        self.assertEqual(_row(state, "agi-moved")["lifecycle_state"], LIFECYCLE_IDLE)

    def test_e8_a_session_reporting_its_own_end(self) -> None:
        state = _state()
        _insert(state, "agi-self", host="operator", host_ref=None)
        with mock.patch.object(host_end_notice, "_run", side_effect=AssertionError("no host check for a session's own report")):
            first = end_reported_session(state, agent_instance_id="agi-self", reason="prompt_input_exit", directed_by="test")
            second = end_reported_session(state, agent_instance_id="agi-self", reason="prompt_input_exit", directed_by="test")
        self.assertEqual(
            first,
            {"agent_instance_id": "agi-self", "ended": True, "already_ended": False, "lifecycle_state": LIFECYCLE_TERMINATED, "end_reason": UNKNOWN},
        )
        self.assertEqual(_row(state, "agi-self")["lifecycle_state"], LIFECYCLE_TERMINATED)
        reasons = _reasons(state, "agi-self", LIFECYCLE_TERMINATED)
        self.assertEqual(len(reasons), 1)
        self.assertIn("(prompt_input_exit)", reasons[0])
        self.assertEqual(
            second,
            {"agent_instance_id": "agi-self", "ended": False, "already_ended": True, "lifecycle_state": LIFECYCLE_TERMINATED, "end_reason": UNKNOWN},
        )
        with self.assertRaises(SessionEndError) as unknown:
            end_reported_session(state, agent_instance_id="agi-nobody", reason="x", directed_by="test")
        self.assertEqual(unknown.exception.code, "session_not_found")
        with self.assertRaises(SessionEndError) as blank:
            end_reported_session(state, agent_instance_id="  ", reason="x", directed_by="test")
        self.assertEqual(blank.exception.code, "parameter_invalid")

    def test_e9_observe_host_by_host_kind(self) -> None:
        state = _state()
        _insert(state, "agi-op", host="operator", host_ref="x")
        _insert(state, "agi-noref", host="tmux", host_ref=None)
        _insert(state, "agi-hl", host="headless", host_ref="12345")
        with mock.patch.object(host_end_notice, "_run", side_effect=AssertionError("no tmux call expected")):
            operator = observe_host(_row(state, "agi-op"))
            no_ref = observe_host(_row(state, "agi-noref"))
            self.assertEqual(operator.state, "unknown")
            self.assertEqual(operator.detail, "host operator has no exact check")
            self.assertEqual(no_ref, HostObservation(state="unknown", detail="no host reference recorded"))

            def driver_answering(alive: bool | Exception) -> Any:
                def _alive(_host_ref: str) -> bool:
                    if isinstance(alive, Exception):
                        raise alive
                    return alive

                return mock.patch.object(
                    session_end,
                    "resolve_host_driver",
                    return_value=(SimpleNamespace(alive=_alive), "headless"),
                )

            with driver_answering(False):
                gone = observe_host(_row(state, "agi-hl"))
            with driver_answering(True):
                present = observe_host(_row(state, "agi-hl"))
            with driver_answering(RuntimeError("probe broke")):
                unknown = observe_host(_row(state, "agi-hl"))
        self.assertEqual(gone, HostObservation(state="gone", detail="headless process 12345 is not running"))
        self.assertEqual(present.state, "present")
        self.assertEqual(unknown.state, "unknown")
        self.assertIn("RuntimeError: probe broke", unknown.detail)

    def test_e10_the_public_verbs_take_any_caller(self) -> None:
        state = _state()
        _insert(state, "agi-pub-a")
        _insert(state, "agi-pub-b", host="operator", host_ref=None)
        plugin = AgentMessagingPlugin()
        plugin.orchestrator_ref = _Orchestrator(state)
        for params in ({"host": "operator", "host_ref": "x"}, {"host": "tmux"}, {"host": "tmux", "host_ref": "  "}):
            refused = plugin.session_host_closed(params, {})
            self.assertEqual(refused["action_status"], "failed", params)
            self.assertEqual(refused["error"]["code"], "parameter_invalid", params)
        with _patched(_FakeRun(1, GONE_STDERR)):
            closed = plugin.session_host_closed({"host": "tmux", "host_ref": "fleet-a-1"}, {})
        self.assertEqual(closed["action_status"], "completed")
        self.assertEqual(closed["data"]["ended"], ["agi-pub-a"])
        self.assertEqual(_row(state, "agi-pub-a")["lifecycle_state"], LIFECYCLE_TERMINATED)
        reported = plugin.session_ended({"agent_instance_id": "agi-pub-b", "reason": "logout"}, {})
        self.assertEqual(reported["action_status"], "completed")
        self.assertTrue(reported["data"]["ended"])
        self.assertEqual(_row(state, "agi-pub-b")["lifecycle_state"], LIFECYCLE_TERMINATED)
        missing = plugin.session_ended({"agent_instance_id": "agi-nobody"}, {})
        self.assertEqual(missing["error"]["code"], "session_not_found")
        blank = plugin.session_ended({}, {})
        self.assertEqual(blank["error"]["code"], "parameter_invalid")


class SessionEndReasonSmoke(unittest.TestCase):
    """E11-E17: the typed reason an ended record carries, written by the one writer and read back."""

    def test_e11_the_end_reason_value_set(self) -> None:
        self.assertEqual(
            END_REASONS,
            ("finished", "failed", "stopped_by_asker", "stopped_by_solet", "refused_by_provider", "unknown"),
        )
        self.assertEqual((FINISHED, FAILED, STOPPED_BY_ASKER, STOPPED_BY_SOLET, REFUSED_BY_PROVIDER, UNKNOWN), END_REASONS)
        for value in END_REASONS:
            self.assertEqual(require_end_reason(value), value)
        for bad in ("x", "", "Finished", None, 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                require_end_reason(bad)

    def test_e12_the_one_writer_stores_the_reason_with_the_terminated_transition(self) -> None:
        state = _state()
        for agent_instance_id in ("agi-r-none", "agi-r-done", "agi-r-bad", "agi-r-idle"):
            _insert(state, agent_instance_id)
        transition_lifecycle_state(state, agent_instance_id="agi-r-none", from_state=LIFECYCLE_LIVE, to_state=LIFECYCLE_TERMINATED, directed_by="test")
        self.assertEqual(_row(state, "agi-r-none")["end_reason"], UNKNOWN, "a terminal write naming no reason stores unknown")
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-r-done",
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_TERMINATED,
            directed_by="test",
            end_reason=FINISHED,
        )
        self.assertEqual(_row(state, "agi-r-done")["end_reason"], FINISHED)
        with self.assertRaises(ValueError):
            transition_lifecycle_state(
                state,
                agent_instance_id="agi-r-bad",
                from_state=LIFECYCLE_LIVE,
                to_state=LIFECYCLE_TERMINATED,
                directed_by="test",
                end_reason="nope",
            )
        with self.assertRaises(ValueError):
            transition_lifecycle_state(
                state,
                agent_instance_id="agi-r-idle",
                from_state=LIFECYCLE_LIVE,
                to_state=LIFECYCLE_IDLE,
                directed_by="test",
                end_reason=FINISHED,
            )
        for agent_instance_id, to_state in (("agi-r-bad", LIFECYCLE_TERMINATED), ("agi-r-idle", LIFECYCLE_IDLE)):
            row = _row(state, agent_instance_id)
            self.assertEqual(row["lifecycle_state"], LIFECYCLE_LIVE, "a refused write leaves the row live")
            self.assertFalse(row.get("end_reason"), "a refused write leaves the reason blank")
            self.assertEqual(_reasons(state, agent_instance_id, to_state), [], "a refused write leaves no audit row")
        transition_lifecycle_state(state, agent_instance_id="agi-r-done", from_state=LIFECYCLE_TERMINATED, to_state=LIFECYCLE_RETIRED, directed_by="test")
        retired = _row(state, "agi-r-done")
        self.assertEqual(retired["lifecycle_state"], LIFECYCLE_RETIRED)
        self.assertEqual(retired["end_reason"], FINISHED, "retiring leaves the reason the end wrote")

    def test_e13_the_end_functions_pass_the_reason_kind(self) -> None:
        state = _state()
        for agent_instance_id in ("agi-k-fail", "agi-k-default", "agi-k-notice", "agi-k-kind"):
            _insert(state, agent_instance_id, host_ref=f"fleet-{agent_instance_id}")
        self.assertTrue(end_session_record(state, _row(state, "agi-k-fail"), why="t", observation=None, directed_by="test", reason_kind=FAILED))
        self.assertEqual(_row(state, "agi-k-fail")["end_reason"], FAILED)
        self.assertTrue(end_session_record(state, _row(state, "agi-k-default"), why="t", observation=None, directed_by="test"))
        self.assertEqual(_row(state, "agi-k-default")["end_reason"], UNKNOWN)
        with _patched(_FakeRun(1, GONE_STDERR)):
            on_host_session_closed(state, host="tmux", host_ref="fleet-agi-k-notice", directed_by="tmux:test", tmux=TMUX_ARGV)
        self.assertEqual(_row(state, "agi-k-notice")["end_reason"], UNKNOWN, "a gone-host notice cannot say why")
        with _patched(_FakeRun(1, GONE_STDERR)):
            on_host_session_closed(
                state,
                host="tmux",
                host_ref="fleet-agi-k-kind",
                directed_by="tmux:test",
                tmux=TMUX_ARGV,
                reason_kind=STOPPED_BY_SOLET,
            )
        self.assertEqual(_row(state, "agi-k-kind")["end_reason"], STOPPED_BY_SOLET, "the kind reaches the writer through the sweep")

    def test_e14_a_session_reports_its_own_typed_outcome(self) -> None:
        state = _state()
        for agent_instance_id, outcome, expected in (
            ("agi-o-fin", "finished", FINISHED),
            ("agi-o-fail", "failed", FAILED),
            ("agi-o-none", None, UNKNOWN),
        ):
            _insert(state, agent_instance_id, host="operator", host_ref=None)
            result = end_reported_session(state, agent_instance_id=agent_instance_id, reason="x", outcome=outcome, directed_by="test")
            self.assertEqual(result["end_reason"], expected, agent_instance_id)
            self.assertEqual(_row(state, agent_instance_id)["end_reason"], expected, agent_instance_id)
        _insert(state, "agi-o-bad", host="operator", host_ref=None)
        for bad in ("stopped_by_asker", "unknown", "", 7):
            with self.assertRaises(SessionEndError, msg=repr(bad)) as refused:
                end_reported_session(state, agent_instance_id="agi-o-bad", reason="x", outcome=bad, directed_by="test")
            self.assertEqual(refused.exception.code, "parameter_invalid", repr(bad))
            self.assertEqual(_row(state, "agi-o-bad")["lifecycle_state"], LIFECYCLE_LIVE, "a refused outcome writes nothing")
        self.assertEqual(_reasons(state, "agi-o-bad", LIFECYCLE_TERMINATED), [])

    def test_e15_end_reason_of_reads_a_record_without_inventing_one(self) -> None:
        self.assertIsNone(end_reason_of({"lifecycle_state": LIFECYCLE_LIVE}))
        self.assertIsNone(end_reason_of({"lifecycle_state": LIFECYCLE_LIVE, "end_reason": FAILED}), "a live row never reads as ended")
        self.assertEqual(end_reason_of({"lifecycle_state": LIFECYCLE_TERMINATED, "end_reason": ""}), UNKNOWN)
        self.assertEqual(end_reason_of({"lifecycle_state": LIFECYCLE_TERMINATED}), UNKNOWN)
        self.assertEqual(end_reason_of({"lifecycle_state": LIFECYCLE_TERMINATED, "end_reason": "bogus"}), UNKNOWN)
        self.assertEqual(end_reason_of({"lifecycle_state": LIFECYCLE_RETIRED, "end_reason": FAILED}), FAILED)
        state = _state()
        _insert(state, "agi-old")
        transition_lifecycle_state(state, agent_instance_id="agi-old", from_state=LIFECYCLE_LIVE, to_state=LIFECYCLE_TERMINATED, directed_by="test")
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": "agi-old"}},
            {"end_reason": ""},
        )
        self.assertEqual(end_reason_of(_row(state, "agi-old")), UNKNOWN, "a row that ended before the column existed reads unknown")

    def test_e16_list_sessions_and_session_status_carry_end_reason(self) -> None:
        state = _state()
        _insert(state, "agi-v-live", host="operator", host_ref=None)
        _insert(state, "agi-v-over", host="operator", host_ref=None)
        end_reported_session(state, agent_instance_id="agi-v-over", reason="x", outcome="failed", directed_by="test")
        ended = list_sessions(state, {"lifecycle_state": LIFECYCLE_TERMINATED})["sessions"]
        live = list_sessions(state, {"lifecycle_state": LIFECYCLE_LIVE})["sessions"]
        self.assertEqual([(row["agent_instance_id"], row["end_reason"]) for row in ended], [("agi-v-over", FAILED)])
        self.assertEqual([(row["agent_instance_id"], row["end_reason"]) for row in live], [("agi-v-live", None)])
        self.assertEqual(session_status(state, "agi-v-over")["end_reason"], FAILED)
        self.assertIsNone(session_status(state, "agi-v-live")["end_reason"])

    def test_e17_session_ended_through_the_plugin_takes_the_outcome(self) -> None:
        state = _state()
        _insert(state, "agi-pl-ok", host="operator", host_ref=None)
        _insert(state, "agi-pl-bad", host="operator", host_ref=None)
        plugin = AgentMessagingPlugin()
        plugin.orchestrator_ref = _Orchestrator(state)
        done = plugin.session_ended({"agent_instance_id": "agi-pl-ok", "outcome": "finished"}, {})
        self.assertEqual(done["action_status"], "completed")
        self.assertEqual(done["data"]["end_reason"], FINISHED)
        refused = plugin.session_ended({"agent_instance_id": "agi-pl-bad", "outcome": "stopped_by_asker"}, {})
        self.assertEqual(refused["action_status"], "failed")
        self.assertEqual(refused["error"]["code"], "parameter_invalid")
        self.assertEqual(_row(state, "agi-pl-bad")["lifecycle_state"], LIFECYCLE_LIVE)


if __name__ == "__main__":
    unittest.main()
