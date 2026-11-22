#!/usr/bin/env python3
"""One recovery scenario set (S1 to S11), run against both coding-tool adapters.

The harness measures one promise on two tools: save before a discarding command,
notify only for changes the session did not make, save at every turn end, never
block, and restore byte-identical work. `scenarios.json` holds the scenarios as
data. This file interprets them. The tool is a column of the run (claude or codex),
never a fork of a scenario; a tool may differ from the common expectation only
through a named known difference of design section 5.3.

Modes:
  payload (default)  Each tool's hook command line, exactly as its hooks.json declares
                     it, is fed that tool's payload on stdin; the actor's shell command
                     then runs for real in a scratch repository. Offline, no model cost,
                     safe in the gate battery.
  live               The same scenarios through `claude -p` and `codex exec` with the
                     hooks registered in a disposable repository. Run once per tool, by a
                     session that may launch unattended tool sessions, on a measured-load
                     admission. `--dry-run` prints every command and launches nothing.

The default invocation (no arguments) is `--mode payload --tool both`, which is what the
gate smoke battery runs. Every payload run first prints a HONESTY SELF-CHECK line for the live
instrument (exit 1 when it fails, even with no adapter installed). Exit codes: 0 when every cell
passes or when neither adapter is installed in the default roots (one loud ADVISORY line), 1 on
any FAIL or when exactly one adapter is installed. A cell fails only on a measured difference between expected and
observed bytes, saves, notices or exit codes; a fault the host cannot produce is reported
UNMEASURED with its reason and is never a FAIL.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from scenario_model import ADVISORY_LINE, DEFAULT_ROOTS, TOOLS, Json, Options, adapter_present
from scenario_plan import honesty_self_check, run_live
from scenario_run import format_cell, load_scenarios, run_cell


def selection(args: argparse.Namespace) -> tuple[list[Json], list[str]]:
    """The scenarios and tools this invocation covers."""
    data = load_scenarios([item for item in args.scenario.split(",") if item])
    return cast("list[Json]", data["scenarios"]), (list(TOOLS) if args.tool == "both" else [args.tool])


def missing_adapters(tools: Sequence[str], options: Options) -> tuple[list[str], bool]:
    """(tools whose default root lacks an adapter, whether no adapter at all is installed and no root was named)."""
    present = {tool: adapter_present(tool, options.roots[tool]) for tool in tools}
    nothing_installed = not options.explicit and not any(present.values())
    return [tool for tool in tools if not present[tool] and tool not in options.explicit], nothing_installed


def run_payload(args: argparse.Namespace, options: Options) -> int:
    """The payload run, preceded by the live instrument's honesty self-check, which needs no adapter and no model, so it runs in every state of the gate invocation."""
    problems = honesty_self_check()
    print("HONESTY SELF-CHECK: " + ("PASS (the live stage's probe, Codex trust entries, per-cell raw-record names, S4 and S7 live declarations, and its PASS, FAIL and UNMEASURED readings behave as documented)" if not problems else "FAIL: " + "; ".join(problems)))
    cells_status = run_payload_cells(args, options)
    return 1 if problems else cells_status


def run_payload_cells(args: argparse.Namespace, options: Options) -> int:
    scenarios, tools = selection(args)
    cells = len(scenarios) * len(tools)
    missing, nothing_installed = missing_adapters(tools, options)
    if nothing_installed:
        print(ADVISORY_LINE.format(cells=cells))
        return 0
    ran, failed = run_cells([tool for tool in tools if tool not in missing], scenarios, options)
    for tool in missing:
        print(f"FAIL {tool}: the recovery adapter is not installed in {options.roots[tool]} while the other tool's is; {len(scenarios)} cells not run")
    summary = f"{ran} of {cells} cells run, {ran - failed} PASS, {failed} FAIL"
    print(summary + (f", missing adapter(s): {', '.join(missing)}" if missing else ""))
    return 1 if failed or missing else 0


def run_cells(tools: Sequence[str], scenarios: Sequence[Json], options: Options) -> tuple[int, int]:
    """Run every selected scenario for each tool, printing one line per cell; returns (cells run, cells failed)."""
    ran = failed = 0
    for tool in tools:
        for scenario in scenarios:
            result = run_cell(scenario, tool, options)
            ran += 1
            failed += result.status == "FAIL"
            print(format_cell(scenario, tool, result), flush=True)
    return ran, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("payload", "live"), default="payload")
    parser.add_argument("--tool", choices=("claude", "codex", "both"), default="both")
    parser.add_argument("--claude-root", type=Path)
    parser.add_argument("--codex-root", type=Path)
    parser.add_argument("--scenario", default="", help="comma-separated scenario ids; default all")
    parser.add_argument("--bulk-files", type=int, help="development override of S6's 50,000 untracked files; the unsaved-remainder assertion is skipped")
    parser.add_argument("--model", default="", help="live mode: the model the tool runs on; claude defaults to claude-haiku-5-5, codex needs the smallest model available to exec")
    parser.add_argument("--dry-run", action="store_true", help="live mode: print every command, launch nothing")
    parser.add_argument("--list", action="store_true", help="print the scenario ids and titles")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    explicit = frozenset(tool for tool, value in (("claude", args.claude_root), ("codex", args.codex_root)) if value is not None)
    roots = {"claude": (args.claude_root or DEFAULT_ROOTS["claude"]).resolve(), "codex": (args.codex_root or DEFAULT_ROOTS["codex"]).resolve()}
    options = Options(roots, args.bulk_files, explicit)
    if args.list:
        for item in cast("list[Json]", load_scenarios([])["scenarios"]):
            print(f"{item['id']:<4}{item['title']}")
        return 0
    if args.mode == "live":
        return run_live(args, options)
    return run_payload(args, options)


if __name__ == "__main__":
    sys.exit(main())
