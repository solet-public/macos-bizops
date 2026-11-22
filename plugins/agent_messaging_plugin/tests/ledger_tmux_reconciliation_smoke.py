#!/usr/bin/env python3
"""Fixture-only metadata reconciliation smoke; never probes a live tmux server."""

from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock, patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from agent_messaging_plugin.ledger_tmux_reconciliation import MetadataRunner


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
    fake = SimpleNamespace(
        _get_state_service=lambda: cast("StateManagementInterface", state),
        _peer_registry=None,
        _bridge_manager=None,
        _session_role_claim_pruner=None,
    )
    AgentMessagingPlugin._run_session_lifecycle_sweep(cast("Any", fake))  # noqa: SLF001


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


def _runner(output: str, *, refs: tuple[str, ...] = ("worker",), returncode: int = 0, stderr: str = "") -> MetadataRunner:
    def run(argv: list[str]) -> tuple[int, bytes, str]:
        from agent_messaging_plugin.ledger_tmux_reconciliation import _FORMAT  # noqa: PLC2701

        expected = [["tmux", "list-panes", "-t", f"={ref}:", "-F", _FORMAT] for ref in refs]
        assert argv in expected, f"Probe only an exact recorded session: {argv!r}"
        assert "-a" not in argv
        return returncode, output.encode(), stderr

    return run


def _pane(sid: str = "$1", name: str = "worker", dead: str = "0", pane: str = "%1") -> str:
    return f"{sid}\t{name}\t{pane}\t123\t{dead}\tpython\n"


def _reasons(report: dict[str, Any]) -> list[str]:
    return [row["reason"] for row in report["observations"]]


def test_exact_joins_and_evidence() -> None:
    """Retain every pane for the recorded name and the original ledger row.

    A mixed live/dead pair supplies presence evidence without making an atomic-snapshot claim.
    """
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
    """Terminal records remain eligible for observation.

    A surviving pane is evidence of divergence, so filtering out those records would hide it.
    """
    panes = _pane() + _pane(pane="%2", dead="1")
    terminal = _report([_row(lifecycle_state="terminated")], panes)
    assert _reasons(terminal) == ["terminal_ledger_pane_present"]
    retired = _report([_row(lifecycle_state="retired")], panes)
    assert _reasons(retired) == ["terminal_ledger_pane_present"]


def test_dead_panes_are_not_process_presence() -> None:
    """Only an explicit live flag supports process presence.

    Keep both flags in mixed sessions and preserve invalid flags as uncertain evidence.
    """
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
    """A longer native name does not satisfy a shorter recorded reference.

    Duplicate records remain ambiguous even when the host returns no pane metadata.
    """
    report = _report([_row()], _pane(name="worker-long"))
    assert _reasons(report) == ["ledger_tmux_missing"]
    assert report["observations"][0]["session_ids"] == []
    duplicates = _report([_row(), _row(2)], _pane())
    assert _reasons(duplicates) == ["duplicate_host_ref", "duplicate_host_ref"]
    ambiguous = _report([_row()], _pane() + _pane(sid="$2", pane="%2"))
    assert _reasons(ambiguous) == ["duplicate_host_ref"]
    absent = _report([_row(), _row(2)], "")
    assert _reasons(absent) == ["duplicate_host_ref", "duplicate_host_ref"]


def test_native_only_and_operator_rows() -> None:
    """An unresolved operator record supplies no reference to probe.

    Its presence does not authorize a host census or adoption of an unrelated session.
    """
    report = _report([_row(host="operator", host_ref="")], _pane(name="foreign"))
    assert _reasons(report) == ["host_ref_missing"]
    assert report["native_source"]["panes"] == []
    assert report["observations"][0]["ledger_row"]["host"] == "operator"


def test_distinct_recorded_references_only() -> None:
    """The ledger determines the query set and the report's observation identities.

    Repeated references share a probe; extra fixture output does not create another observation.
    """
    from agent_messaging_plugin.ledger_tmux_reconciliation import _FORMAT, collect_ledger_tmux_report  # noqa: PLC2701

    calls: list[list[str]] = []
    outputs = {"=r1:": _pane(name="r1") + _pane(sid="$3", name="foreign", pane="%3"), "=r2:": _pane(sid="$2", name="r2", pane="%2")}

    def run(argv: list[str]) -> tuple[int, bytes, str]:
        calls.append(argv)
        assert argv in [["tmux", "list-panes", "-t", target, "-F", _FORMAT] for target in outputs]
        assert "-a" not in argv
        return 0, outputs[argv[3]].encode(), ""

    rows = [_row(host_ref="r1"), _row(2, host_ref="r2"), _row(3, host_ref="r1")]
    report = collect_ledger_tmux_report(cast("StateManagementInterface", _state(rows)), runner=run)
    assert [call[3] for call in calls] == ["=r1:", "=r2:"], "Probe each distinct recorded reference once"
    assert [row["session_ids"] for row in report["observations"]] == [["$1"], ["$2"], ["$1"]]
    assert _reasons(report) == ["duplicate_host_ref", "matched_process_present", "duplicate_host_ref"]
    assert report["complete"] is True


def test_definitive_absence_is_complete() -> None:
    """The measured missing-session and missing-server prefixes establish absence.

    These exit-one results leave coverage complete, unlike an unexplained failed probe.
    """
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    for stderr in ("can't find session: worker", "error connecting to /x", "no server running on /x"):
        report = collect_ledger_tmux_report(cast("StateManagementInterface", _state([_row()])), runner=_runner("", returncode=1, stderr=stderr))
        assert _reasons(report) == ["ledger_tmux_missing"]
        assert report["complete"] is True
        assert report["native_source"]["returncode"] == 0


def test_invalid_reference_and_empty_ledger_do_not_probe() -> None:
    """Both cases avoid a host read, but their evidence differs.

    An invalid recorded reference is unknown; no records produce the complete empty golden shape.
    """
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    runner = Mock(side_effect=AssertionError("No usable recorded reference to probe"))
    invalid = collect_ledger_tmux_report(cast("StateManagementInterface", _state([_row(host_ref="a:b")])), runner=runner)
    assert _reasons(invalid) == ["probe_unknown"]
    assert invalid["complete"] is False
    assert "reference is not an exact session name" in invalid["native_source"]["detail"]
    empty = collect_ledger_tmux_report(cast("StateManagementInterface", _state([])), runner=runner)
    assert empty["complete"] is True
    assert empty["observations"] == empty["advisories"] == []
    assert {key: value for key, value in empty["native_source"].items() if key not in {"started_at", "ended_at"}} == {
        "complete": True,
        "detail": "",
        "panes": [],
        "rejected_rows": [],
        "returncode": 0,
        "status": "ok",
    }
    assert set(empty["limits"]) == {"ledger_rows", "native_rows", "output_bytes", "timeout_seconds"}
    runner.assert_not_called()


def test_probe_faults_are_unknown() -> None:
    """Unavailable probes retain uncertainty instead of reporting a missing session."""
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    state = cast("StateManagementInterface", _state([_row()]))
    for fault in (subprocess.TimeoutExpired("fixture", 5), OSError("fixture unavailable")):
        with patch("subprocess.run", side_effect=fault):
            unknown = collect_ledger_tmux_report(state)
        assert _reasons(unknown) == ["probe_unknown"]
        assert unknown["complete"] is False
        assert unknown["native_source"]["returncode"] is None


def test_unrecognized_exits_are_unknown() -> None:
    """An exit code without a measured absence prefix does not establish absence."""
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    state = cast("StateManagementInterface", _state([_row()]))
    for returncode in (1, 2):
        nonzero = collect_ledger_tmux_report(state, runner=_runner("", returncode=returncode, stderr="unexpected failure"))
        assert _reasons(nonzero) == ["probe_unknown"]
        assert nonzero["complete"] is False
        assert nonzero["native_source"]["returncode"] == returncode


def test_malformed_and_empty_metadata_are_distinct() -> None:
    """Malformed output is retained as unknown; complete empty output is absence."""
    malformed = _report([_row()], "not metadata\n")
    assert _reasons(malformed) == ["probe_unknown"]
    assert malformed["native_source"]["rejected_rows"] == ["not metadata"]
    empty = _report([_row()])
    assert _reasons(empty) == ["ledger_tmux_missing"]
    assert empty["complete"] is True


def test_tied_paging() -> None:
    """Equal creation times still advance by row identity across pages.

    The operator domain has its own page, so it survives a larger tmux domain read.
    """
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    stamp = "2026-01-01T00:00:00+00:00"
    rows = [_row(i, created_at=stamp, host_ref=f"worker-{i}") for i in range(126)]
    rows.append(_row(500, host="operator", created_at=stamp, host_ref="operator"))
    state = _state(rows)
    with patch.object(state, "query_ordered", wraps=state.query_ordered) as query:
        report = collect_ledger_tmux_report(cast("StateManagementInterface", state), runner=_runner("", refs=tuple(str(row["host_ref"]) for row in rows)))
    assert len(report["observations"]) == 127
    assert len({row["ledger_row"]["id"] for row in report["observations"]}) == 127
    assert report["complete"] is True
    assert len(query.call_args_list) == 3
    _assert_query_pages(query.call_args_list, stamp)


def test_invalid_utf8_is_unknown() -> None:
    """Replacement text preserves the observed command without endorsing corrupted metadata.

    Decoding damage keeps the report incomplete even when its other fields appear valid.
    """
    from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report

    def run(_argv: list[str]) -> tuple[int, bytes, str]:
        return 0, b"$1\tworker\t%1\t123\t0\t\xff\n", ""

    report = collect_ledger_tmux_report(cast("StateManagementInterface", _state([_row()])), runner=run)
    assert _reasons(report) == ["probe_unknown"]
    assert report["native_source"]["panes"][0]["pane_current_command"] == "\ufffd"
    assert report["native_source"]["complete"] is False


def _assert_query_pages(calls: list[Any], stamp: str) -> None:
    assert all(call.args[1]["limit"] <= 101 for call in calls)
    assert calls[1].args[1]["after"] == [stamp, "row-0099"]
    assert {call.args[1]["filters"]["host"] for call in calls} == {"operator", "tmux"}


def test_partial_coverage() -> None:
    """A ledger budget retains its exact frontier instead of claiming absence.

    Rows sharing a reference exercise the ledger limit independently of native pane volume.
    """
    stamp = "2026-01-01T00:00:00+00:00"
    limited = _report([_row(i, created_at=stamp) for i in range(251)])
    assert "partial_coverage" in limited["advisories"]
    assert limited["ledger_sources"][0]["continuation"] == [stamp, "row-0249"]
    assert limited["ledger_sources"][0]["rows_read"] == 250
    assert "ledger_tmux_missing" not in _reasons(limited)
    assert "matched_process_present" not in _reasons(limited)


def test_native_limits_and_failed_ledger() -> None:
    """Byte and row limits disclose partial native coverage.

    A failed ledger read supplies no reference and consequently no native-only observation.
    """
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
    assert report["observations"] == []


def test_native_row_budget_is_shared_across_probes() -> None:
    """The retained-pane budget belongs to the whole inventory.

    Two individually small responses cannot each spend the full report-wide allowance.
    """
    from agent_messaging_plugin.ledger_tmux_reconciliation import MAX_NATIVE_ROWS, collect_ledger_tmux_report

    def run(argv: list[str]) -> tuple[int, bytes, str]:
        second = argv[3] == "=r2:"
        start = MAX_NATIVE_ROWS // 2 if second else 0
        name, sid = ("r2", "$2") if second else ("r1", "$1")
        output = "".join(_pane(sid=sid, name=name, pane=f"%{i}") for i in range(start, start + MAX_NATIVE_ROWS // 2 + 1))
        return 0, output.encode(), ""

    rows = [_row(host_ref="r1"), _row(2, host_ref="r2")]
    report = collect_ledger_tmux_report(cast("StateManagementInterface", _state(rows)), runner=run)
    assert len(report["native_source"]["panes"]) == MAX_NATIVE_ROWS
    assert report["complete"] is False
    assert _reasons(report) == ["probe_unknown", "probe_unknown"]


def test_fresh_snapshots_correct_history() -> None:
    """New observations can reverse presence without rewriting previous evidence.

    The original report retains its empty inventory after the later present and absent reads.
    """
    old = _row(created_at="2020-01-01T00:00:00+00:00")
    missing = _report([old])
    present = _report([old], _pane())
    disappeared = _report([old])
    assert _reasons(missing) == ["ledger_tmux_missing"]
    assert _reasons(present) == ["matched_process_present"]
    assert _reasons(disappeared) == ["ledger_tmux_missing"]
    assert missing["native_source"]["panes"] == []
    assert missing["ended_at"] <= present["started_at"] <= disappeared["ended_at"]


def test_sweep_does_not_run_ledger_tmux_observer() -> None:
    """Reconciliation remains an on-demand observation.

    An ordinary lifecycle sweep does not silently add a recurring host read.
    """
    state = _state([_row()])
    with (
        patch.object(plugin_module, "collect_ledger_tmux_report") as collect,
        patch.object(plugin_module, "_run_session_claude_mapping_riders"),
    ):
        _sweep(state)
    assert collect.call_count == 0, "The sweep tick must not probe tmux against the ledger"


def test_process_is_read_only() -> None:
    """Exercise the plugin entry point with metadata-only subprocess arguments.

    Job handling leaves the observed session ledger untouched and exposes the registered process.
    """
    plugin = AgentMessagingPlugin()
    state = _state([_row()])
    before = deepcopy(state._rows)  # noqa: SLF001
    plugin.orchestrator_ref = SimpleNamespace(get_service=lambda _name: state)
    with (
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")) as run,
        patch.object(state, "update_state", wraps=state.update_state) as update,
        patch.object(state, "write_state", wraps=state.write_state) as write,
    ):
        result = plugin._reconcile_ledger_tmux()  # noqa: SLF001 — worker collector retains read-only behavior
    assert result["action_status"] == "completed"
    assert _reasons(result["data"]) == ["ledger_tmux_missing"]
    assert state._rows == before, "Reconciliation must leave the ledger unchanged"  # noqa: SLF001
    assert update.call_count == write.call_count == 0, "No lifecycle or state writes are permitted"
    assert run.call_count == 1
    assert run.call_args.args[0][:5] == ["tmux", "list-panes", "-t", "=worker:", "-F"]
    assert run.call_args.kwargs["timeout"] == 5
    assert run.call_args.kwargs["capture_output"] is True
    assert "reconcile_ledger_tmux" in plugin.get_edge_process_definitions()


def main() -> None:
    tests = [
        test_exact_joins_and_evidence,
        test_terminal_rows_remain_visible,
        test_dead_panes_are_not_process_presence,
        test_identity_collision_is_unresolved,
        test_native_only_and_operator_rows,
        test_distinct_recorded_references_only,
        test_definitive_absence_is_complete,
        test_invalid_reference_and_empty_ledger_do_not_probe,
        test_probe_faults_are_unknown,
        test_unrecognized_exits_are_unknown,
        test_malformed_and_empty_metadata_are_distinct,
        test_tied_paging,
        test_invalid_utf8_is_unknown,
        test_partial_coverage,
        test_native_limits_and_failed_ledger,
        test_native_row_budget_is_shared_across_probes,
        test_fresh_snapshots_correct_history,
        test_sweep_does_not_run_ledger_tmux_observer,
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
