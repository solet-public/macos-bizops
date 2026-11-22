"""Cells: variants of a scenario, one cell per scenario and tool, and the printed result line."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from scenario_model import HERE, CellError, CellResult, Env, Json, Options, UnmeasuredError
from scenario_ops import PayloadDriver, run_ops, turn_ends


def variants_of(scenario: Json) -> list[tuple[str, list[Json], list[Json], str]]:
    if scenario.get("cases"):
        return [(str(case["id"]), cast("list[Json]", case["setup"]), cast("list[Json]", case["steps"]), "outside_store") for case in cast("list[Json]", scenario["cases"])]
    if scenario.get("tmpdir_cases"):
        return [(label, cast("list[Json]", scenario["setup"]), cast("list[Json]", scenario["steps"]), label) for label in cast("list[str]", scenario["tmpdir_cases"])]
    return [("", cast("list[Json]", scenario["setup"]), cast("list[Json]", scenario["steps"]), "outside_store")]


def record_notes(env: Env, label: str, result: CellResult) -> None:
    """What a passing variant discloses: the known differences it applied and the S11 byte-search count."""
    for name in sorted(env.applied):
        note = f"known difference {name} applied"
        if note not in result.notes:
            result.notes.append(note)
    if env.search_counts:
        result.notes.append(f"{label}: byte-searched {env.search_counts[-1]} files under the store root (find -type f), TMPDIR case {label}")


def run_variant(scenario: Json, tool: str, root: Path, options: Options, variant: tuple[str, list[Json], list[Json], str], result: CellResult) -> None:
    label, setup, steps, tmp_case = variant
    env: Env | None = None
    try:
        env = Env(scenario, tool, root, options, tmp_case)
        env.driver = PayloadDriver(env)
        run_ops(env, setup)
        run_ops(env, steps)
        if scenario.get("auto_turn_end", True):
            turn_ends(env)
        if not env.hook_runs:
            raise CellError(f"no recovery hook command ran: {env.root / 'hooks' / 'hooks.json'} registers none for the events this scenario fires")
        record_notes(env, label, result)
    except UnmeasuredError as exc:
        result.notes.append(f"UNMEASURED({label or 'cell'}): {exc}")
    except CellError as exc:
        result.failures.append(f"{label + ': ' if label else ''}{exc}")
    finally:
        if env is not None:
            env.cleanup()


def run_cell(scenario: Json, tool: str, options: Options) -> CellResult:
    started = time.monotonic()
    result = CellResult("PASS", 0.0)
    for variant in variants_of(scenario):
        run_variant(scenario, tool, options.roots[tool], options, variant, result)
    result.status = "FAIL" if result.failures else "PASS"
    result.seconds = time.monotonic() - started
    return result


def load_scenarios(selected: Sequence[str]) -> Json:
    data = cast(Json, json.loads((HERE / "scenarios.json").read_text(encoding="utf-8")))
    if selected:
        known = {item["id"] for item in cast("list[Json]", data["scenarios"])}
        missing = [name for name in selected if name not in known]
        if missing:
            raise SystemExit(f"unknown scenario id(s) {missing}; known: {sorted(known)}")
        data["scenarios"] = [item for item in cast("list[Json]", data["scenarios"]) if item["id"] in selected]
    return data


def format_cell(scenario: Json, tool: str, result: CellResult) -> str:
    historical = f" [historical case: {scenario['historical']}]" if scenario.get("historical") else ""
    notes = "".join(f"; {note}" for note in result.notes)
    record = f" [hook_log={result.hook_log}; reply_artifact={result.reply_artifact}]"
    line = f"{result.status} {scenario['id']:<3} {tool:<6} {scenario['title']}{historical}{notes}{record} ({result.seconds:.1f}s)"
    return line + "".join(f"\n      {failure}" for failure in result.failures)
