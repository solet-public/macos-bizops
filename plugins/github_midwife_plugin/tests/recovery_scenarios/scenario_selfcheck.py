"""The live instrument's own honesty checks on fixtures: Codex trust entries, per-cell raw-record names and the live declarations.

Each check returns its problems, empty when the rule holds. `scenario_plan.honesty_self_check` runs them on every payload run and every
dry run, so the gate battery measures them without a model or an installed adapter."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import cast

from scenario_live import evidence_paths, lifecycle_gap, tool_command, trust_override, write_cell_evidence
from scenario_model import CellResult, Json
from scenario_run import format_cell, load_scenarios

TRUST_FORMAT = 'projects={"/x/y"={trust_level="trusted"},"/x/z"={trust_level="trusted"}}'
STEP_EVENTS = {"stop": "Stop", "session_end": "SessionEnd"}


def _flag_values(command: list[str], flag: str) -> list[str]:
    """The word after each occurrence of `flag` in a command line."""
    return [command[index + 1] for index, word in enumerate(command[:-1]) if word == flag]


def trust_problems() -> list[str]:
    """The first Codex turn carries ONE trust flag holding every installed folder (Codex splits a dotted `-c` key at every dot, so a per-folder key
    never matches, and two `projects=` flags replace each other) and names its working folder; a resumed turn carries the flag and no -C
    (exec resume has none); a Claude turn carries no trust flag."""
    problems: list[str] = []
    if trust_override(["/x/y", "/x/z"]) != TRUST_FORMAT:
        problems.append(f"the trust flag for two folders is not one projects table with JSON-quoted keys: {trust_override(['/x/y', '/x/z'])}")
    first = tool_command("codex", "<model>", "p", "<session>", True, trusted=["/x/y", "/x/z"], workdir="/x/y")
    again = tool_command("codex", "<model>", "p", "<session>", False, trusted=["/x/y", "/x/z"], workdir="/x/y")
    carried = [value for value in _flag_values(first, "-c") if "projects" in value]
    if carried != [TRUST_FORMAT]:
        problems.append(f"a first codex turn must carry exactly one trust flag naming every installed folder: {carried}")
    if _flag_values(first, "-C") != ["/x/y"]:
        problems.append(f"a first codex turn does not name its working folder with -C: {first}")
    if TRUST_FORMAT not in _flag_values(again, "-c") or "-C" in again:
        problems.append(f"a resumed codex turn must carry the trust flag and no -C: {again}")
    if any("trust_level" in word for word in tool_command("claude", "<model>", "p", "<session>", True, trusted=["/x/y"])):
        problems.append("a claude turn carries a codex trust flag")
    return problems


def evidence_problems() -> list[str]:
    """Every live cell names a raw hook log and a model reply artifact, and the files exist, with or without a turn."""
    problems: list[str] = []
    old, new = b'{"event": "old"}\n', b'{"event": "PreToolUse"}\n'
    with tempfile.TemporaryDirectory() as raw:
        kept = Path(raw)
        paths = evidence_paths(kept, "codex", "S1")
        log = kept / "hooks.jsonl"
        log.write_bytes(old + new)
        write_cell_evidence(paths, log, len(old), [{"command": ["codex"], "stdout": "events", "stderr": ""}])
        if paths[0].read_bytes() != new or '"stdout": "events"' not in paths[1].read_text(encoding="utf-8"):
            problems.append("a cell's evidence does not hold exactly that cell's hook records and its tool process in full")
        write_cell_evidence(paths, kept / "missing.jsonl", 0, [])
        if paths[0].read_bytes() or "no tool process ran" not in paths[1].read_text(encoding="utf-8"):
            problems.append("a cell with no hook call and no turn must still leave an empty hook log and a reply file saying no process ran")
        line = format_cell({"id": "S1", "title": "t"}, "codex", CellResult("PASS", 0.0, hook_log=str(paths[0]), reply_artifact=str(paths[1])))
        if f"hook_log={paths[0]}" not in line or f"reply_artifact={paths[1]}" not in line:
            problems.append(f"a cell line does not name its hook log and reply artifact: {line}")
    return problems


def unproducible_steps(scenario: Json) -> list[str]:
    """The Stop and SessionEnd steps of a scenario that no live tool can produce, each as `op actor`."""
    steps = [*scenario.get("setup", []), *scenario.get("steps", [])]
    return [f"{op['op']} {op.get('actor', '')}" for op in steps if op["op"] in STEP_EVENTS and lifecycle_gap(op, STEP_EVENTS[op["op"]])]


def declaration_problems() -> list[str]:
    """A scenario with a step no live tool can produce must say so in its `live` declaration, so its UNMEASURED(live) reading is by design and stated."""
    problems: list[str] = []
    for scenario in cast("list[Json]", load_scenarios([])["scenarios"]):
        gaps, declared = unproducible_steps(scenario), cast(Json, scenario.get("live") or {})
        if gaps and not declared:
            problems.append(f"{scenario['id']} has steps with no live precondition ({gaps}) but no `live` declaration")
        if declared and not all(declared.get(key) for key in ("status", "unmeasured", "reason")):
            problems.append(f"{scenario['id']}: the `live` declaration needs a status, the unmeasured steps and a reason")
    return problems
