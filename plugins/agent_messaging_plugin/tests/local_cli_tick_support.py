"""Controlled watch-loop transport and real lifecycle-handler fixtures.

The registered local_cli_smoke module owns test discovery and invokes the
scenario assertions retained beside their shared lifecycle fixture.
This support module provides HTTP/tmux doubles and schema-aware lifecycle
state, keeping the real stream, busy parser and report handler in the path.
It performs no model turns and contacts no live Solet.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import httpx

import agent_messaging_plugin.local_cli.cli as cli_mod
import agent_messaging_plugin.local_cli.client as client_mod

_WORKING_SCREEN = "• Working (12m 07s · esc to interrupt)\n› Ask Codex to do anything"

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


def _working_watch_run(
    screens: list[str],
    *,
    report: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    panes: str = "%1 4321\n",
    captured_pid: str = "4321",
    agent_id: str = "codex",
    parent: int | None = 4321,
    instance_env: str = "agi-working-watch",
    capture_error: bool = False,
) -> tuple[list[dict[str, Any]], list[float], list[str]]:
    """Run the real stream, BridgeClient and parser with controlled host I/O.

    Each events poll advances sixty seconds; each screen lasts three polls.
    Thus a screen is sampled at a due boundary, and later screens can correct
    an earlier busy observation. The parent check ends only after all polls.
    """
    clock = [0.0]
    polls = [0]
    calls: list[dict[str, Any]] = []
    registers: list[float] = []
    output: list[str] = []
    outcome: list[dict[str, Any]] = [{}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/events"):
            clock[0] += 60.0
            polls[0] += 1
            return httpx.Response(200, json={"events": [], "next_cursor": polls[0]})
        if path.endswith("/peer/register"):
            registers.append(clock[0])
            return httpx.Response(200, json={})
        if path.endswith("/process/call"):
            body = json.loads(request.content)
            assert body["process_key"] == "plugin::agent_messaging_plugin::report_alive", body
            args = body["arguments"]
            calls.append(args)
            outcome[0] = report(args) if report else {"action_status": "completed", "success": True, "data": {"lifecycle_state": "live"}}
            return httpx.Response(200, json={"action_id": "ae-working"})
        if "/process/result/" in path:
            return httpx.Response(200, json={"status": "completed", "result": outcome[0]})
        return httpx.Response(200, json={"bridge_id": "agc-working", "session_id": "s"})

    def tmux(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[0] == "tmux", args
        if args[1] == "list-panes":
            stdout = panes
        elif args[1] == "capture-pane":
            assert args[-1] == "%1", args
            if capture_error:
                raise subprocess.TimeoutExpired(args, 2)
            stdout = screens[min((polls[0] - 1) // 3, len(screens) - 1)]
        else:
            assert args[1] == "display-message", args
            stdout = captured_pid
        return subprocess.CompletedProcess(args, 0, stdout, "")

    identity = cli_mod.WatchIdentity("test-watch", agent_id, "ases-working", "agi-working-watch")
    with client_mod.BridgeClient("http://test", transport=httpx.MockTransport(handler)) as client:
        with (
            patch.object(cli_mod.time, "monotonic", lambda: clock[0]),
            patch.object(cli_mod, "_parent_is_gone", lambda _pid: polls[0] >= 3 * len(screens)),
            patch.object(cli_mod.subprocess, "run", tmux),
            patch.dict(os.environ, {"AGENT_INSTANCE_ID": instance_env}),
            patch.object(cli_mod.click, "echo", lambda value, **_kw: output.append(str(value))),
        ):
            cli_mod._stream_events(client, identity, None, Path(tempfile.mkdtemp()) / "session.marks", parent)
    return calls, registers, output


_WorkingLifecycleCase = tuple[Any, "StateManagementInterface", Callable[[dict[str, Any]], dict[str, Any]], list[dict[str, Any]], list[str]]


@contextmanager
def _working_lifecycle_case() -> Generator[_WorkingLifecycleCase]:
    """Provide an overdue managed row and the actual plugin report handler.

    HTTP calls from the stream reach that handler through the transport double.
    Captured outcomes and deadlines retain each call for current-state checks;
    the state fake also retains the append-only lifecycle transition records.
    """
    from ananta.core.services.call_context import CallContext

    from agent_messaging_plugin.plugin import AgentMessagingPlugin
    from agent_messaging_plugin.session_lifecycle_store import (
        ManagedSessionSpec,
        insert_managed_session,
        read_managed_session,
        transition_lifecycle_state,
    )

    fake = importlib.import_module("_real_state_fake").RealShapeState()
    state = cast("StateManagementInterface", fake)
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-working-watch",
            lane_id="working-watch",
            assignment=importlib.import_module("_register_unit_double").fixture_assignment(),
            work_class="read_only",
            budget_line="test",
            host="operator",
        ),
    )
    plugin = object.__new__(AgentMessagingPlugin)
    outcomes: list[dict[str, Any]] = []
    deadlines: list[str] = []

    def report(args: dict[str, Any]) -> dict[str, Any]:
        result = plugin.report_alive({"parameters": args}, {"call_context": CallContext.for_operator()})
        outcomes.append(result)
        deadlines.append(str(read_managed_session(state, "agi-working-watch")["report_by"]))
        return result

    def transition(before: str, after: str) -> None:
        transition_lifecycle_state(state, agent_instance_id="agi-working-watch", from_state=before, to_state=after, directed_by="test")

    transition("spawning", "live")
    transition("live", "overdue")
    with patch.object(AgentMessagingPlugin, "_get_state_service", lambda _self: state):
        yield fake, state, report, outcomes, deadlines


def check_working_watch_rearms_and_recovers_real_lifecycle_handler() -> None:
    """Recover overdue state once, then rearm a live row on the next due tick.

    The first response must say recovered and the historical transition must
    carry the watch origin; increasing deadlines alone would not prove recovery.
    """
    from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE

    from agent_messaging_plugin.schema import TABLE_SESSION_TRANSITION
    from agent_messaging_plugin.session_lifecycle_store import read_managed_session

    with _working_lifecycle_case() as (fake, state, report, outcomes, deadlines):
        before = str(read_managed_session(state, "agi-working-watch").get("report_by") or "")
        calls, _, output = _working_watch_run([_WORKING_SCREEN] * 2, report=report)
        assert len(calls) == 2 and not output, (calls, output)
        assert before < deadlines[0] < deadlines[1], deadlines
        assert outcomes[0]["data"]["recovered"] is True, outcomes
        assert read_managed_session(state, "agi-working-watch")["lifecycle_state"] == "live"
        audit = fake.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_SESSION_TRANSITION)
        assert any(row["from_state"] == "overdue" and row["to_state"] == "live" and "Codex watch" in row["reason"] for row in audit), audit


def check_working_watch_stale_transition_is_refused() -> None:
    """A prior overdue observation cannot overwrite the row recovered by a tick.

    Use the actual transition writer so refusal exercises its state predicate,
    independently of the watcher's positive host-evidence decision.
    """
    from agent_messaging_plugin.session_lifecycle_store import StaleLifecycleStateError, transition_lifecycle_state

    with _working_lifecycle_case() as (_fake, state, report, _outcomes, _deadlines):
        _working_watch_run([_WORKING_SCREEN], report=report)
        try:
            transition_lifecycle_state(state, agent_instance_id="agi-working-watch", from_state="overdue", to_state="live", directed_by="test")
        except StaleLifecycleStateError:
            pass
        else:
            raise AssertionError("stale lifecycle transition accepted")


def check_working_watch_terminal_refusal_preserves_deadline() -> None:
    """A busy pane cannot revive a terminal managed row or rearm its deadline.

    Report through the stream after termination and require a bounded advisory;
    read the row again rather than trusting only the receiver's failure result.
    """
    from agent_messaging_plugin.session_lifecycle_store import read_managed_session, transition_lifecycle_state

    with _working_lifecycle_case() as (_fake, state, report, _outcomes, deadlines):
        _working_watch_run([_WORKING_SCREEN], report=report)
        before = deadlines[0]
        transition_lifecycle_state(state, agent_instance_id="agi-working-watch", from_state="live", to_state="terminated", directed_by="test")
        calls, _, output = _working_watch_run([_WORKING_SCREEN], report=report)
        assert len(calls) == 1 and len(output) == 1, (calls, output)
        assert read_managed_session(state, "agi-working-watch")["lifecycle_state"] == "terminated"
        assert str(read_managed_session(state, "agi-working-watch")["report_by"]) == before


def check_working_watch_ticks_independently_and_recaptures() -> None:
    """Two busy intervals report working; the following idle interval does not.

    Registration still runs every sixty seconds, independently of the working
    timer. Reusing the first capture would incorrectly report a third tick.
    """
    calls, registers, output = _working_watch_run([_WORKING_SCREEN, _WORKING_SCREEN, "› Ask Codex to do anything"])
    assert len(calls) == 2, calls
    assert all(call["status"] == "working" and call["agent_instance_id"] == "agi-working-watch" for call in calls)
    assert all("Codex watch parent 4321" in call["status_note"] for call in calls)
    assert registers == [60.0 * n for n in range(1, 10)], registers
    assert not output, output


def check_working_watch_requires_positive_parent_and_turn_evidence() -> None:
    """Only the authenticated watch identity and its exact active pane may tick.

    Missing, ambiguous and replaced panes are distinct from an idle screen:
    host failures emit advisories while ordinary inactivity remains quiet.
    """
    for screen in ("› Ask Codex to do anything", "• Starting MCP servers (1/2)", "unknown TUI", "Working in transcript"):
        calls, _, _ = _working_watch_run([screen])
        assert not calls, (screen, calls)
    evidence_cases: tuple[dict[str, Any], ...] = (
        {"panes": "%2 9999\n"},
        {"panes": "%1 4321\n%2 4321\n"},
        {"captured_pid": "9999"},
        {"instance_env": "agi-other"},
        {"capture_error": True},
    )
    for kwargs in evidence_cases:
        calls, _, output = _working_watch_run([_WORKING_SCREEN], **kwargs)
        assert not calls and len(output) == 1 and "advisory" in output[0], (kwargs, calls, output)
    excluded_cases: tuple[dict[str, Any], ...] = ({"agent_id": "claude_code"}, {"parent": None})
    for kwargs in excluded_cases:
        calls, _, output = _working_watch_run([_WORKING_SCREEN], **kwargs)
        assert not calls and not output, (kwargs, calls, output)


def check_working_watch_report_failure_does_not_stop_stream() -> None:
    """A transport failure, result timeout and receiver refusal all leave mail flowing.

    Three independent due attempts and all nine registrations must survive;
    treating any reporting failure as a stream failure would truncate both.
    """
    count = [0]

    def report(_args: dict[str, Any]) -> dict[str, Any]:
        count[0] += 1
        if count[0] == 1:
            raise client_mod.BridgeCallError("bridge unavailable")
        if count[0] == 2:
            raise client_mod.BridgeResultTimeoutError("report timed out")
        return {"action_status": "failed", "error": {"code": "terminal_session"}}

    calls, registers, output = _working_watch_run([_WORKING_SCREEN] * 3, report=report)
    assert len(calls) == 3 and len(registers) == 9, (calls, registers)
    assert len(output) == 3 and all("advisory" in line for line in output), output


def check_working_watch_real_disposable_tmux_parent_mapping() -> None:
    """Verify real tmux discovery, capture and PID recheck without a Codex session.

    The child first renders an interruptible status, then clears to an idle
    composer. Both observations use the production helper and busy parser.
    """
    # A disposable non-agent pane exits itself; no live worker is driven/killed.
    code = "import time; print('• Working (12s · esc to interrupt)', flush=True); time.sleep(2); print('\\033[2J\\033[H› Ask Codex to do anything', flush=True); time.sleep(2)"
    created = subprocess.run(
        ["tmux", "new-session", "-d", "-P", "-F", "#{pane_id} #{pane_pid}", "-s", f"watch-proof-{os.getpid()}-{time.time_ns()}", sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    _pane, pid = created.stdout.strip().split()
    deadline = time.monotonic() + 1.5
    while cli_mod._codex_parent_busy_reason(int(pid)) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cli_mod._codex_parent_busy_reason(int(pid)) is not None
    time.sleep(2.1)
    assert cli_mod._codex_parent_busy_reason(int(pid)) is None
