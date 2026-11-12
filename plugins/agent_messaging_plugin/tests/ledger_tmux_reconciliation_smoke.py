#!/usr/bin/env python3
"""Fixture-only metadata reconciliation smoke; never probes a live tmux server."""

from __future__ import annotations

import logging
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, BinaryIO, cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from _register_unit_double import RegisterUnitDouble  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


def _row(number: int = 1, **changes: Any) -> dict[str, Any]:
    return {
        "id": f"row-{number:04d}",
        "agent_instance_id": f"agi-{number:04d}",
        "created_at": datetime.now(UTC).isoformat(),
        "host": "tmux",
        "host_ref": "worker",
        "lifecycle_state": "live",
        "report_by": None,
        **changes,
    }


def _state(rows: list[dict[str, Any]]) -> RealShapeState:
    state = RealShapeState()
    state._rows[(AGENT_ROLE_BINDING_NAMESPACE, "managed_session")] = rows  # noqa: SLF001
    return state


def _sweep(state: RealShapeState) -> None:
    register = RegisterUnitDouble()
    fake = SimpleNamespace(
        _get_state_service=lambda: cast("StateManagementInterface", state),
        _register_unit_client=lambda: register,
        _peer_registry=None,
        _bridge_manager=None,
        _session_role_claim_pruner=None,
    )
    AgentMessagingPlugin._run_session_lifecycle_sweep(cast("Any", fake))  # noqa: SLF001


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.reasons: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.reasons.extend(getattr(record, "advisory_reasons", []))


def test_sweep_emits_reconciliation_advisory() -> None:
    """Base-red leg imports no new collector and drives the existing real sweep."""
    state = _state([_row()])
    records = _Records()
    plugin_module.logger.addHandler(records)
    try:
        with (
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"")),
            patch.dict("os.environ", {"ANANTA_SESSION_MAPPING_SPOOL_DIR": "/nonexistent/fixture-spool"}),
        ):
            _sweep(state)
    finally:
        plugin_module.logger.removeHandler(records)
    print("SETUP OK: real sweep and existing riders completed on valid ledger fake", flush=True)
    advisory_reasons = records.reasons
    assert "ledger_tmux_missing" in advisory_reasons


def _report(rows: list[dict[str, Any]], output: str = "") -> dict[str, Any]:
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    state = _state(rows)
    before = deepcopy(state._rows)  # noqa: SLF001
    report = collect_ledger_tmux_report(
        cast("StateManagementInterface", state),
        runner=_runner(output),
    )
    assert state._rows == before, "Reconciliation must leave the ledger unchanged"  # noqa: SLF001
    return report


def _runner(output: str) -> Any:
    def run(argv: list[str], sink: BinaryIO) -> int:
        from agent_messaging_plugin.ledger_tmux_reconciliation import METADATA_ARGV

        assert argv == list(METADATA_ARGV), "Only the metadata census is permitted"
        sink.write(output.encode())
        return 0

    return run


def _pane(sid: str = "$1", name: str = "worker", dead: str = "0", pane: str = "%1") -> str:
    return f"{sid}\t{name}\t{pane}\t123\t{dead}\tpython\n"


def _reasons(report: dict[str, Any]) -> list[str]:
    return [row["reason"] for row in report["observations"]]


def test_exact_joins_and_evidence() -> None:
    panes = _pane() + _pane(pane="%2", dead="1")
    report = _report([_row()], panes)
    assert _reasons(report) == ["matched_process_present"]
    assert report["complete"] is True
    observation = report["observations"][0]
    assert observation["ledger_row"]["id"] == "row-0001"
    assert observation["session_ids"] == ["$1"]
    assert [pane["pane_id"] for pane in observation["panes"]] == ["%1", "%2"]
    assert report["native_source"]["started_at"] <= report["native_source"]["ended_at"]
    assert report["started_at"] <= report["ended_at"]
    assert report["atomic_snapshot"] is False


def test_terminal_rows_remain_visible() -> None:
    panes = _pane() + _pane(pane="%2", dead="1")
    terminal = _report([_row(lifecycle_state="terminated")], panes)
    assert _reasons(terminal) == ["terminal_ledger_pane_present"]
    retired = _report([_row(lifecycle_state="retired", host_ref="$1")], panes)
    assert _reasons(retired) == ["terminal_ledger_pane_present"]


def test_dead_panes_are_not_process_presence() -> None:
    report = _report([_row()], _pane(dead="1") + _pane(pane="%2", dead="1"))
    assert "matched_process_present" not in _reasons(report)
    assert _reasons(report) == ["matched_native_pane_dead"]
    evidence = report["observations"][0]["panes"]
    assert [(row["pane_id"], row["pane_dead"]) for row in evidence] == [("%1", "1"), ("%2", "1")]
    mixed = _report([_row()], _pane(dead="1") + _pane(pane="%2"))
    assert _reasons(mixed) == ["matched_process_present"]
    assert [row["pane_dead"] for row in mixed["observations"][0]["panes"]] == ["1", "0"]
    for dead in ("", "invalid"):
        malformed = _report([_row()], _pane(dead=dead))
        assert _reasons(malformed) == ["probe_unknown"]
        assert malformed["observations"][0]["panes"][0]["pane_dead"] == dead


def test_identity_collision_is_unresolved() -> None:
    report = _report([_row(host_ref="worker-short")], _pane(name="worker-short-long"))
    assert _reasons(report) == ["ledger_tmux_missing", "unbound_tmux"]
    assert report["observations"][0]["session_ids"] == []
    duplicates = _report([_row(), _row(2, host_ref="$1")], _pane())
    assert _reasons(duplicates) == ["duplicate_host_ref", "duplicate_host_ref"]
    ambiguous = _report([_row()], _pane() + _pane(sid="$2", pane="%2"))
    assert _reasons(ambiguous) == ["duplicate_host_ref"]
    absent = _report([_row(), _row(2)], "")
    assert _reasons(absent) == ["duplicate_host_ref", "duplicate_host_ref"]


def test_native_only_and_operator_rows() -> None:
    report = _report([_row(host="operator", host_ref="")], _pane(name="foreign"))
    assert _reasons(report) == ["host_ref_missing", "unbound_tmux"]
    assert report["observations"][1]["scope"] == "scope_unknown"
    assert report["native_source"]["panes"][0]["session_name"] == "foreign"
    assert report["observations"][0]["ledger_row"]["host"] == "operator"


def test_probe_faults_are_unknown() -> None:
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    state = cast("StateManagementInterface", _state([_row()]))
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("fixture", 5)):
        timeout = collect_ledger_tmux_report(state)
    assert _reasons(timeout) == ["probe_unknown"]
    assert "ledger_tmux_missing" not in _reasons(timeout)
    assert timeout["native_source"]["returncode"] is None
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
        nonzero = collect_ledger_tmux_report(state)
    assert _reasons(nonzero) == ["probe_unknown"]
    assert nonzero["native_source"]["returncode"] == 1
    malformed = _report([_row()], "not metadata\n")
    assert _reasons(malformed) == ["probe_unknown"]
    assert malformed["native_source"]["rejected_rows"] == ["not metadata"]
    empty = _report([_row()])
    assert _reasons(empty) == ["ledger_tmux_missing"]
    assert empty["complete"] is True


def test_tied_paging() -> None:
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    stamp = "2026-01-01T00:00:00+00:00"
    rows = [_row(i, created_at=stamp, host_ref=f"worker-{i}") for i in range(126)]
    rows.append(_row(500, host="operator", created_at=stamp, host_ref="operator"))
    state = _state(rows)
    with patch.object(state, "query_ordered", wraps=state.query_ordered) as query:
        report = collect_ledger_tmux_report(cast("StateManagementInterface", state), runner=_runner(""))
    assert len(report["observations"]) == 127
    assert len({row["ledger_row"]["id"] for row in report["observations"]}) == 127
    assert report["complete"] is True
    assert len(query.call_args_list) == 3
    _assert_query_pages(query.call_args_list, stamp)


def test_invalid_utf8_is_unknown() -> None:
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    def run(_argv: list[str], sink: BinaryIO) -> int:
        sink.write(b"$1\tworker\t%1\t123\t0\t\xff\n")
        return 0

    report = collect_ledger_tmux_report(cast("StateManagementInterface", _state([_row()])), runner=run)
    assert _reasons(report) == ["probe_unknown"]
    assert report["native_source"]["panes"][0]["pane_current_command"] == "\ufffd"
    assert report["native_source"]["complete"] is False


def _assert_query_pages(calls: list[Any], stamp: str) -> None:
    assert all(call.args[1]["limit"] <= 101 for call in calls)
    assert calls[1].args[1]["after"] == [stamp, "row-0099"]
    assert {call.args[1]["filters"]["host"] for call in calls} == {"operator", "tmux"}


def test_partial_coverage() -> None:
    stamp = "2026-01-01T00:00:00+00:00"
    limited = _report([_row(i, created_at=stamp, host_ref=f"w-{i}") for i in range(251)])
    assert "partial_coverage" in limited["advisories"]
    assert limited["ledger_sources"][0]["continuation"] == [stamp, "row-0249"]
    assert limited["ledger_sources"][0]["rows_read"] == 250
    assert "ledger_tmux_missing" not in _reasons(limited)
    assert "matched_process_present" not in _reasons(limited)


def test_native_limits_and_failed_ledger() -> None:
    from agent_messaging_plugin.ledger_tmux_reconciliation import (
        MAX_NATIVE_ROWS,
        MAX_OUTPUT_BYTES,
        collect_ledger_tmux_report,
    )

    output = "".join(_pane(pane=f"%{i}") for i in range(MAX_NATIVE_ROWS + 1))
    limited = _report([_row()], output)
    assert "partial_coverage" in limited["advisories"]
    assert len(limited["native_source"]["panes"]) == MAX_NATIVE_ROWS
    over_bytes = _report([_row()], "x" * (MAX_OUTPUT_BYTES + 1))
    assert over_bytes["complete"] is False
    assert "partial_coverage" in over_bytes["advisories"]
    state = _state([_row()])
    state.fail_next("query")
    report = collect_ledger_tmux_report(cast("StateManagementInterface", state), runner=_runner(_pane()))
    assert "probe_unknown" in report["advisories"]
    assert report["ledger_sources"][0]["status"] == "probe_unknown"
    assert report["complete"] is False
    assert report["observations"][0]["scope"] == "scope_unknown"


def test_fresh_snapshots_correct_history() -> None:
    old = _row(created_at="2020-01-01T00:00:00+00:00")
    missing = _report([old])
    present = _report([old], _pane())
    disappeared = _report([old])
    assert _reasons(missing) == ["ledger_tmux_missing"]
    assert _reasons(present) == ["matched_process_present"]
    assert _reasons(disappeared) == ["ledger_tmux_missing"]
    assert missing["native_source"]["panes"] == []
    assert missing["ended_at"] <= present["started_at"] <= disappeared["ended_at"]


def test_sweep_fault_isolation() -> None:
    state = _state([_row()])
    with (
        patch.object(plugin_module, "collect_ledger_tmux_report", side_effect=RuntimeError("fixture")) as collect,
        patch.object(plugin_module, "_run_session_claude_mapping_riders") as sibling,
    ):
        try:
            _sweep(state)
        except RuntimeError:
            pass  # A guard-removal mutant must reach the sibling assertion below.
    assert collect.call_count == 1, "The existing sweep must invoke the observer"
    assert sibling.call_count == 1, "Collector failure must not skip sibling riders"


def test_existing_tick_runs_observer() -> None:
    state = _state([_row()])
    fake = SimpleNamespace(
        _autonomic_assignment=None,
        _get_state_service=lambda: state,
        _run_session_lifecycle_sweep=lambda: _sweep(state),
        _run_rotation_surface_sweep=lambda: None,
    )
    with (
        patch.object(plugin_module, "sweep_codex_pickups"),
        patch.object(plugin_module, "collect_ledger_tmux_report", return_value={"observations": [], "advisories": []}) as collect,
        patch.object(plugin_module, "_run_session_claude_mapping_riders"),
    ):
        AgentMessagingPlugin._on_sweep_tick(cast("Any", fake))  # noqa: SLF001
    assert collect.call_count == 1, "The existing sweep tick must reach the observer"


def test_process_is_read_only() -> None:
    plugin = AgentMessagingPlugin()
    state = _state([_row()])
    before = deepcopy(state._rows)  # noqa: SLF001
    plugin.orchestrator_ref = SimpleNamespace(get_service=lambda _name: state)
    with (
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run,
        patch.object(state, "update_state", wraps=state.update_state) as update,
        patch.object(state, "write_state", wraps=state.write_state) as write,
    ):
        result = plugin._reconcile_ledger_tmux()  # noqa: SLF001 — worker collector retains read-only behavior
    assert result["action_status"] == "completed"
    assert _reasons(result["data"]) == ["ledger_tmux_missing"]
    assert state._rows == before, "Reconciliation must leave the ledger unchanged"  # noqa: SLF001
    assert update.call_count == write.call_count == 0, "No lifecycle or state writes are permitted"
    assert run.call_count == 1
    assert run.call_args.args[0][:4] == ["tmux", "list-panes", "-a", "-F"]
    assert run.call_args.kwargs["timeout"] == 5
    assert "reconcile_ledger_tmux" in plugin.get_edge_process_definitions()


def main() -> None:
    tests = [
        test_sweep_emits_reconciliation_advisory,
        test_exact_joins_and_evidence,
        test_terminal_rows_remain_visible,
        test_dead_panes_are_not_process_presence,
        test_identity_collision_is_unresolved,
        test_native_only_and_operator_rows,
        test_probe_faults_are_unknown,
        test_tied_paging,
        test_invalid_utf8_is_unknown,
        test_partial_coverage,
        test_native_limits_and_failed_ledger,
        test_fresh_snapshots_correct_history,
        test_sweep_fault_isolation,
        test_existing_tick_runs_observer,
        test_process_is_read_only,
    ]
    assert {test.__name__ for test in tests} == {name for name in globals() if name.startswith("test_")}, "Every named fixture must run from main"
    selected = sys.argv[1] if len(sys.argv) > 1 else None
    for test in tests:
        if selected is None or selected == test.__name__:
            test()
            print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
