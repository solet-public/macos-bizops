"""The live stage's operator surface: admission, the printed plan, the credential probe, the run and its R1 to R6 receipt."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

from scenario_live import CLAUDE_HOOK_FILE, CODEX_HOOK_FILE, RECORDER, LiveDriver, LiveState, evidence_paths, lifecycle_gap, tool_command, trust_override, write_cell_evidence
from scenario_model import ADAPTERS, ADMISSION_INVOCATION_WORDS, ADMISSION_LOAD, ADMISSION_PROCESS, ADMISSION_SWAP_GB, SYSTEM_PROBE_COMMANDS, TURN_CAP, CellError, CellResult, Env, Json, LiveStopError, Options, UnmeasuredError, short_path
from scenario_ops import run_ops
from scenario_run import format_cell, load_scenarios, variants_of
from scenario_selfcheck import declaration_problems, evidence_problems, trust_problems

CREDENTIAL_PROBE: Final[str] = "reply with the word ok"
# What an authentication failure says, in either tool's words. The probe's stderr is matched as a whole: Codex opens with a progress line
# ("Reading additional input from stdin...") before the error, so the first line alone cannot classify it; the first line is still reported.
AUTH_FAILURE: Final[re.Pattern[str]] = re.compile(r"unauthori[sz]ed|forbidden|\b40[13]\b|not logged in|\blog ?in\b|authenticat|api[ _-]?key|credential|token\b.*\b(?:expired|invalid)", re.IGNORECASE)
EVIDENCE_RECORDS: Final[int] = 15
EVIDENCE_FIELD_CHARS: Final[int] = 1500
EVIDENCE_REPLY_CHARS: Final[int] = 1500


def _probe(command: Sequence[str]) -> str:
    done = subprocess.run(list(command), capture_output=True, text=True, timeout=30, check=False)
    return done.stdout


def competing_work(process_table: str) -> list[str]:
    """The heavy host work in a `ps` listing, each as the signature that matched, read from the invocation's first words."""
    found: list[str] = []
    for line in process_table.splitlines():
        match = ADMISSION_PROCESS.search(" ".join(line.split()[:ADMISSION_INVOCATION_WORDS]))
        if match and "run_scenarios" not in line:
            found.append(match.group(0))
    return found


# Pass-path fixtures for the admission rule. Pass condition: load below 16, free swap above 1 GB, and no heavy-work invocation in the
# process table. Running component: this live runner, once per tool, on the session that launches the tool sessions. Schedule: before
# each tool run and again before scenario S6. A table of only the resident services, and of lanes whose prompts mention a landing,
# must read inside bounds; a table with a landing run or a scoped check-in must read outside.
RESIDENT_TABLE: Final[str] = """\
/opt/homebrew/bin/python3 -m macos_self_deployment_plugin.supervisor --app-home /Users/example/profile
/opt/homebrew/bin/python3 -m macos_self_deployment_plugin.blue_green_router.router --solet example --public-port 8801
/opt/homebrew/bin/python3 /Users/example/.claude/hooks/headless_tool_allowlist_gate.py runner prompt: the landing run and a deploy swap are not yours to start
claude -p You are a lane. Read the brief about landing and deploy and scoped_checkin.py --root before acting.
"""
BUSY_TABLE: Final[str] = RESIDENT_TABLE + "/opt/homebrew/bin/python3 /Users/example/tools/scripts/scoped_checkin.py --root /Users/example/wt\nlanding run --repo example --unit unt_example\n"


def admission_self_check() -> list[str]:
    """The pass-path test of the admission rule: resident services read inside bounds, heavy work reads outside. Empty when it holds."""
    problems: list[str] = []
    if competing_work(RESIDENT_TABLE):
        problems.append(f"a table of only resident services read as competing work: {competing_work(RESIDENT_TABLE)}")
    busy = competing_work(BUSY_TABLE)
    if "scoped_checkin.py" not in busy or "landing run" not in busy:
        problems.append(f"a table with a scoped check-in and a landing run did not read as competing work: {busy}")
    return problems


def admission_readings() -> Json:
    """The three read-only readings of the live-stage admission rule: load, free swap, competing host work."""
    swap = re.search(r"free = ([0-9.]+)([MG])", _probe(SYSTEM_PROBE_COMMANDS[1]))
    free_gb = (float(swap.group(1)) / (1024 if swap.group(2) == "M" else 1)) if swap else None
    competing = competing_work(_probe(SYSTEM_PROBE_COMMANDS[2]))
    return {"load_1min": round(os.getloadavg()[0], 2), "swap_free_gb": free_gb, "uptime": _probe(SYSTEM_PROBE_COMMANDS[0]).strip(), "competing_processes": competing}


def outside_bounds(readings: Json) -> list[str]:
    reasons: list[str] = []
    if readings["load_1min"] >= ADMISSION_LOAD:
        reasons.append(f"1-minute load {readings['load_1min']} is not below {ADMISSION_LOAD}")
    if readings["swap_free_gb"] is None:
        reasons.append("free swap could not be read")
    elif readings["swap_free_gb"] <= ADMISSION_SWAP_GB:
        reasons.append(f"free swap {readings['swap_free_gb']:.2f} GB is not above {ADMISSION_SWAP_GB} GB")
    if readings["competing_processes"]:
        reasons.append(f"competing host work is running: {readings['competing_processes'][:3]}")
    return reasons


def planned_folders(scenario: Json) -> list[str]:
    """The git folders a scenario's setup creates (a repository or a worktree), by the name the scenario gives each."""
    return [str(op["dir"]) for op in cast("list[Json]", scenario.get("setup", [])) if op["op"] in {"repo", "worktree"}]


def _turn_line(tool: str, model: str, scenario: Json, op: Json, first: bool) -> str:
    """The printed command line of one live model turn (an edit or a shell step)."""
    work = f"$disposable/work-{scenario['id']}"
    trusted = [f"{work}/{name}" for name in planned_folders(scenario)] if tool == "codex" else []
    prompt = f"<write {op['path']}>" if op["op"] == "edit" else f"<run: {op['command']}>"
    settings = f"{work}/{CLAUDE_HOOK_FILE}" if tool == "claude" else ""
    command = tool_command(tool, model, prompt, f"<session-{op['actor']}>", first, settings=settings, trusted=trusted, workdir=f"{work}/{op['dir']}")
    return "    TURN nice -n 10 " + shlex.join(command)


def live_commands(tool: str, model: str, scenario: Json) -> list[str]:
    """Every command a live run of one scenario would execute, as printable lines."""
    lines: list[str] = []
    for variant in variants_of(scenario):
        label, setup, steps, _case = variant
        if scenario.get("cases"):
            lines.append(f"    case {label}: payload-only (fault injection changes the hook's own environment or payload); no live turn")
            continue
        started: set[str] = set()
        for op in [*setup, *steps]:
            if op["op"] in {"edit", "shell"}:
                lines.append(_turn_line(tool, model, scenario, op, str(op["actor"]) not in started))
                started.add(str(op["actor"]))
            else:
                lines.append(f"    harness: {op['op']} " + json.dumps({key: value for key, value in op.items() if key in {"dir", "path", "args", "repo", "branch", "count"}}))
        break
    return lines


def planned_turns(scenarios: Sequence[Json]) -> int:
    count = 0
    for scenario in scenarios:
        if scenario.get("cases"):
            continue
        count += sum(1 for op in cast("list[Json]", scenario["steps"]) if op["op"] in {"edit", "shell"})
    return count


def print_plan(tool: str, model: str, root: Path, scenarios: Sequence[Json], readings: Json) -> None:
    print(f"DRY RUN live {tool}: nothing below is executed except the three read-only admission readings of the pre-flight")
    print(f"PRE-FLIGHT readings: {json.dumps(readings)}")
    reasons = outside_bounds(readings)
    print("PRE-FLIGHT admission: " + ("inside bounds" if not reasons else "OUTSIDE bounds (a real run would wait and re-read every 5 minutes, never force): " + "; ".join(reasons)))
    print(f"1. parent=$(mktemp -d)  # a new empty parent; disposable=$parent/live-{tool}; store=$disposable/store; logs=$disposable/hooks.jsonl")
    print(f"   evidence=$(mktemp -d)  # outside the parent and kept: each cell writes {tool}-<id>.hooks.jsonl (every hook call, raw) and {tool}-<id>.reply.jsonl (every tool process: command, folder, exit, full stdout and stderr); the reader deletes it after harvesting")
    print(f"2. recorder written to $disposable/record_hook.py; hook entries of {short_path(root / 'hooks' / 'hooks.json')} for {ADAPTERS[tool]} wrapped by it")
    print(
        "   hook file: "
        + (
            f"one {CLAUDE_HOOK_FILE} per scenario, outside every repository folder, passed with --settings (a scenario that stashes ignored files cannot remove it)"
            if tool == "claude"
            else f"{CODEX_HOOK_FILE} in every disposable git folder (ASSUMED location; the live run records where the tool really reads it), kept out of the visible paths via info/exclude; a step that removes it makes that scenario UNMEASURED(live)"
        )
    )
    print(
        "   no credential, setting, plugin list or keychain item is copied; the tool reads the real login as it always does"
        + ("; hook trust is satisfied by --dangerously-bypass-hook-trust for this disposable run only (known difference hook_trust)" if tool == "codex" else "; user-level settings and hooks are skipped by --setting-sources project,local")
    )
    if tool == "codex":
        print_trust_step(scenarios)
    print("3. " + shlex.join(probe_command(tool, model, "<probe-session>")) + "   # one-call authentication measurement in an empty non-git folder; a failure is classified by its stderr, and only an authentication failure says a credential is needed")
    for scenario in scenarios:
        print(f"4. scenario {scenario['id']} ({scenario['title']}), timeout {240 if scenario['id'] == 'S6' else 120}s, nice -n 10" + ("; admission readings re-read first" if scenario["id"] == "S6" else ""))
        if status := live_status_line(scenario):
            print(f"   {status}")
        print("\n".join(live_commands(tool, model, scenario)))
    turns = planned_turns(scenarios)
    print(f"5. planned model turns: {turns} (+1 authentication probe); cap {TURN_CAP} per tool" + ("" if turns < TURN_CAP else "  STOP: the plan exceeds the cap"))
    print("   a step the live tool cannot produce is reported UNMEASURED(live) and the rest of the scenario still runs; a scenario with such a step is UNMEASURED, never PASS")
    print("6. post-run admission readings; rm -rf $parent; ls $parent must fail (recorded as deleted)")
    print("7. harvest, then delete: read the receipt's evidence_dir files (<tool>-<id>.hooks.jsonl, <tool>-<id>.reply.jsonl), copy what the report needs, then rm -rf $evidence; ls $evidence must fail")


def tool_version(tool: str) -> str:
    """The tool's own `--version` line, or why it could not be read. The `-c` parser is version specific, so a run records the version it ran on."""
    try:
        done = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"<unreadable: {exc}>"
    return done.stdout.strip() or f"<no output, exit {done.returncode}>"


def print_trust_step(scenarios: Sequence[Json]) -> None:
    """The step that lets Codex load the hook file in the disposable folders: Codex disables project-local hooks until the project is trusted."""
    print("   CODEX PROJECT TRUST (generated config, passed as a session flag on every codex turn; executed by the run, not by this dry run):")
    print("     Codex disables project-local config and hooks in a folder 'until the project is trusted', and --dangerously-bypass-hook-trust does not cover that.")
    print("     Every disposable git folder is therefore trusted for that turn only. No CODEX_HOME is created: with --ignore-user-config the login still lives in CODEX_HOME,")
    print("     so a disposable CODEX_HOME would need a copied credential; the real $CODEX_HOME/config.toml is neither read nor written.")
    print(f"     One -c flag per turn holds every folder of the scenario as a projects table (a per-folder dotted key never matches: Codex splits -c keys at every dot). Codex version of this host: {tool_version('codex')}. Generated flags:")
    for scenario in scenarios:
        folders = [f"$disposable/work-{scenario['id']}/{name}" for name in planned_folders(scenario)]
        print(f"       {scenario['id']}: -c '{trust_override(folders)}'")
    print("     the live run records, from the first cell's hook log, whether Codex fired the hooks; a cell with no hook record names this step in its FAIL line")


def live_status_line(scenario: Json) -> str:
    """What a scenario's `live` declaration says about measuring it through a real tool, or empty when it is fully measurable."""
    declared = cast(Json, scenario.get("live") or {})
    if not declared:
        return ""
    return f"live status: {declared['status']}; UNMEASURED(live) by design: {'; '.join(cast('list[str]', declared['unmeasured']))}; {declared['reason']}"


def resolve_model(tool: str, requested: str, dry_run: bool) -> str:
    """The model a run uses: the one named, Haiku for Claude, and for Codex none is guessed (a dry-run prints a placeholder)."""
    if requested:
        return requested
    if tool == "claude":
        return "claude-haiku-5-5"
    if dry_run:
        return "<model>"
    raise SystemExit("a real live codex run needs --model naming the smallest model available to exec; the harness does not guess one")


def run_live(args: argparse.Namespace, options: Options) -> int:
    if args.tool == "both":
        raise SystemExit("live mode runs one tool at a time: pass --tool claude or --tool codex")
    tool = str(args.tool)
    model = resolve_model(tool, str(args.model), bool(args.dry_run))
    scenarios = cast("list[Json]", load_scenarios([item for item in args.scenario.split(",") if item])["scenarios"])
    readings = admission_readings()
    if args.dry_run:
        print_plan(tool, model, options.roots[tool], scenarios, readings)
        problems = admission_self_check()
        print("ADMISSION SELF-CHECK: " + ("PASS (resident services and prompts mentioning a landing read inside bounds; a scoped check-in and a landing run read outside)" if not problems else "FAIL: " + "; ".join(problems)))
        honesty = honesty_self_check()
        print(
            "HONESTY SELF-CHECK: "
            + (
                "PASS (the probe skips the git check and tells authentication from other failures; every Codex turn carries its trust entries; every cell names a raw hook log and reply artifact; a step no live tool can produce is declared; a skipped step or a missing precondition is UNMEASURED, never PASS)"
                if not honesty
                else "FAIL: " + "; ".join(honesty)
            )
        )
        return 1 if problems or honesty else 0
    return execute_live(tool, model, options, scenarios, readings)


def wait_for_admission(label: str, limit_minutes: int = 60) -> Json:
    """Read the three admission readings; while any is outside its bound, wait and re-read every 5 minutes. Never forced."""
    waited = 0
    while True:
        readings = admission_readings()
        reasons = outside_bounds(readings)
        if not reasons:
            return readings
        print(f"WAITING before {label}: " + "; ".join(reasons), flush=True)
        if waited >= limit_minutes:
            raise LiveStopError(f"the admission readings stayed outside bounds for {limit_minutes} minutes before {label}: {'; '.join(reasons)}")
        time.sleep(300)
        waited += 5


def probe_command(tool: str, model: str, session: str) -> list[str]:
    """The credential probe's command line. It runs in an empty folder that is not a git repository, which `codex exec` refuses without --skip-git-repo-check."""
    return tool_command(tool, model, CREDENTIAL_PROBE, session, True, skip_git_check=True)


def classify_probe_failure(tool: str, returncode: int, stderr: str) -> tuple[str, str]:
    """(kind, message) of a failed probe. kind is `authentication` when stderr carries an authentication marker, otherwise `other`.

    The message names the first stderr line verbatim, so a reader sees what the tool said and not the harness's reading of it."""
    first = next((line.strip() for line in stderr.splitlines() if line.strip()), "<stderr was empty>")
    if AUTH_FAILURE.search(stderr):
        return "authentication", f"{tool} does not authenticate in the disposable folder without a copied credential or setting (exit {returncode}); first stderr line: {first!r}; name the smallest unblocking change, do not improvise a credential path"
    return "other", f"the {tool} probe failed for a reason that is not authentication (exit {returncode}), so no credential result was measured; first stderr line: {first!r}; stderr: {stderr.strip()[:300]!r}"


def credential_probe(tool: str, model: str, disposable: Path, state: LiveState) -> None:
    """One trivial call per tool: does it authenticate as the tool itself reads its login, with nothing copied?"""
    probe = disposable / "probe"
    probe.mkdir()
    state.turns += 1
    done = subprocess.run(probe_command(tool, model, str(uuid.uuid4())), cwd=probe, capture_output=True, text=True, timeout=120, check=False)
    if done.returncode != 0:
        raise LiveStopError(classify_probe_failure(tool, done.returncode, done.stderr)[1])


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"...[{len(text) - limit} more chars]"


def failure_evidence(log: Path, offset: int, replies: Sequence[str], stderrs: Sequence[str]) -> str:
    """What a FAIL keeps in the harness output, because the disposable folder and its hook log are deleted at the end of the run:
    the model's last reply, the tool's last stderr, and the raw hook records this scenario logged (payload in, output out)."""
    raw = log.read_bytes()[offset:].decode(errors="replace") if log.exists() else ""
    records = [line for line in raw.splitlines() if line.strip()]
    lines = [f"evidence kept for this FAIL: {len(replies)} model turn(s), {len(records)} raw hook record(s) (last {EVIDENCE_RECORDS} shown)"]
    lines.append("last model reply: " + (_clip(replies[-1], EVIDENCE_REPLY_CHARS) if replies else "<no model turn ran>"))
    lines.append("last tool stderr: " + (_clip(stderrs[-1].strip(), EVIDENCE_FIELD_CHARS) if stderrs and stderrs[-1].strip() else "<empty>"))
    for line in records[-EVIDENCE_RECORDS:]:
        try:
            record = cast(Json, json.loads(line))
            lines.append(f"hook {record['event'] or '?'} exit={record['exit']} seconds={float(record['seconds']):.2f} stdin={_clip(str(record['stdin']), EVIDENCE_FIELD_CHARS)!r} stdout={_clip(str(record['stdout']), EVIDENCE_FIELD_CHARS)!r} stderr={_clip(str(record['stderr']), EVIDENCE_FIELD_CHARS)!r}")
        except (ValueError, KeyError, TypeError):
            lines.append(f"hook record (unparsed): {_clip(line, EVIDENCE_FIELD_CHARS)!r}")
    return "\n      ".join(lines)


def cell_status(failures: Sequence[str], skipped: Sequence[str]) -> str:
    """PASS only when every step ran: a step the live tool could not produce makes the scenario UNMEASURED, and a measured failure is FAIL."""
    return "FAIL" if failures else ("UNMEASURED" if skipped else "PASS")


def _describe(op: Json) -> str:
    return " ".join(str(part) for part in (op["op"], op.get("actor", ""), op.get("path") or op.get("command") or "") if part)


def run_live_ops(env: Env, ops: Sequence[Json], skipped: list[str]) -> bool:
    """`run_ops`, except a step the live tool cannot produce is recorded in `skipped` and the rest still run.

    A skipped step that stores a value ends the scenario, because later steps read it; returns whether the scenario ran to its end."""
    for op in ops:
        try:
            run_ops(env, [op])
        except UnmeasuredError as exc:
            skipped.append(f"{_describe(op)}: {exc}")
            if op.get("as") or op.get("store"):
                skipped.append("the steps after it were not run: they read a value that step stores")
                return False
    return True


def _probe_problems() -> list[str]:
    problems: list[str] = []
    command = probe_command("codex", "<model>", "<session>")
    if "--skip-git-repo-check" not in command:
        problems.append(f"the codex probe command line lacks --skip-git-repo-check: {command}")
    stderr = "Reading additional input from stdin...\nNot inside a trusted directory and --skip-git-repo-check was not specified.\n"
    kind, message = classify_probe_failure("codex", 1, stderr)
    if kind != "other" or "Reading additional input from stdin..." not in message:
        problems.append(f"a non-authentication probe failure was reported as {kind}: {message}")
    problems += [f"an authentication failure ({words!r}) was not classified as authentication" for words in ("401 Unauthorized", "Not logged in - please run /login", "Invalid API key") if classify_probe_failure("claude", 1, words)[0] != "authentication"]
    return problems


def _status_problems() -> list[str]:
    problems: list[str] = []
    if (cell_status([], []), cell_status([], ["a step"]), cell_status(["f"], ["a step"])) != ("PASS", "UNMEASURED", "FAIL"):
        problems.append("a scenario with a skipped step must read UNMEASURED, a failed one FAIL, a complete one PASS")
    if not lifecycle_gap({"expect": {"saves": 1}}, "SessionEnd") or lifecycle_gap({"expect": {"stdout": "empty"}}, "SessionEnd") or not lifecycle_gap({"stop_hook_active": True}, "Stop"):
        problems.append("a SessionEnd that needs work changed after the turn-end save, or a stop_hook_active Stop, must have no live precondition; a silent SessionEnd must")
    return problems


def honesty_self_check() -> list[str]:
    """The pass-path test of the instrument's honesty rules on fixtures; empty when they hold. Run by every `--dry-run` and every payload run."""
    return [*_probe_problems(), *_status_problems(), *trust_problems(), *evidence_problems(), *declaration_problems()]


def _recorded(log: Path) -> list[tuple[Json, Json]]:
    """Every hook call the recorder logged, as (record, the payload the tool sent)."""
    pairs: list[tuple[Json, Json]] = []
    for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
        if line.strip():
            record = cast(Json, json.loads(line))
            pairs.append((record, cast(Json, json.loads(record["stdin"] or "{}"))))
    return pairs


def _payload_of(seen: Sequence[tuple[Json, Json]], event: str, tool_name: str) -> Json | None:
    return next((payload for record, payload in seen if record["event"] == event and payload.get("tool_name") == tool_name), None)


def _risk(evidence: Json | None, reason: str) -> Json:
    """MEASURED with its evidence, or UNMEASURED with its reason."""
    return {"status": "MEASURED", "evidence": evidence} if evidence else {"status": "UNMEASURED", "reason": reason}


def _session_end_seconds(seen: Sequence[tuple[Json, Json]]) -> Json | None:
    seconds = [round(float(record["seconds"]), 2) for record, _payload in seen if record["event"] == "SessionEnd"]
    return {"session_end_seconds": seconds} if seconds else None


def _codex_risks(seen: Sequence[tuple[Json, Json]]) -> Json:
    shell = _payload_of(seen, "PreToolUse", "Bash")
    patch = _payload_of(seen, "PostToolUse", "apply_patch")
    command = {"command_type": type(shell["tool_input"].get("command")).__name__} if shell else None
    shape = {"tool_input_type": type(patch["tool_input"]).__name__, "sample": str(patch["tool_input"])[:200]} if patch else None
    return {"R1": _risk(command, "no PreToolUse Bash payload recorded"), "R2": _risk(shape, "no apply_patch PostToolUse recorded"), "R5": _risk(_session_end_seconds(seen), "no SessionEnd recorded")}


def _claude_risks(seen: Sequence[tuple[Json, Json]], replies: Sequence[str]) -> Json:
    spoke = next((str(record["stdout"]) for record, _payload in seen if record["event"] == "PreToolUse" and str(record["stdout"]).strip()), "")
    honoured = any("changed outside this session" in reply or "restore save" in reply for reply in replies)
    return {"R3": _risk({"hook_output": spoke[:300]} if spoke and honoured else None, "no notice was spoken, or no model reply cited it"), "R6": _risk(_session_end_seconds(seen), "no SessionEnd recorded")}


def risk_receipt(tool: str, log: Path, replies: Sequence[str]) -> Json:
    """R1 to R6 of the design, each MEASURED with evidence or UNMEASURED with a reason; none is omitted."""
    other = "claude" if tool == "codex" else "codex"
    risks: Json = {name: {"status": "UNMEASURED", "reason": f"answered from the {other} receipt"} for name in ("R1", "R2", "R3", "R4", "R5", "R6")}
    risks["R4"] = {"status": "UNMEASURED", "reason": "needs a PreToolUse hook that sleeps past its timeout; no scenario installs one, so a successor adds a fixture"}
    seen = _recorded(log)
    risks.update(_codex_risks(seen) if tool == "codex" else _claude_risks(seen, replies))
    return risks


def execute_live(tool: str, model: str, options: Options, scenarios: Sequence[Json], before: Json) -> int:
    try:
        before = before if not outside_bounds(before) else wait_for_admission(f"the {tool} run")
    except LiveStopError as exc:
        print(f"STOPPED: {exc}")
        return 1
    os.nice(10)
    evidence = Path(tempfile.mkdtemp(prefix="recovery-live-evidence-")).resolve()
    parent = Path(tempfile.mkdtemp(prefix="recovery-live-")).resolve()
    disposable = parent / f"live-{tool}"
    disposable.mkdir()
    log, recorder, state = disposable / "hooks.jsonl", disposable / "record_hook.py", LiveState(evidence)
    recorder.write_text(RECORDER, encoding="utf-8")
    failed = 0
    risks: Json = {}
    outcomes: dict[str, str] = {}
    try:
        credential_probe(tool, model, disposable, state)
        for scenario in scenarios:
            if scenario["id"] == "S6":
                wait_for_admission("scenario S6")
            result = run_live_cell(scenario, tool, model, options, disposable, log, recorder, state)
            failed += result.status == "FAIL"
            outcomes[str(scenario["id"])] = result.status
            print(format_cell(scenario, tool, result), flush=True)
        risks = risk_receipt(tool, log, state.replies)
    except LiveStopError as exc:
        print(f"STOPPED: {exc}")
        failed += 1
    finally:
        shutil.rmtree(parent, ignore_errors=True)
    after = admission_readings()
    receipt = {
        "tool": tool,
        "model": model,
        "disposable": str(parent),
        "tool_version": tool_version(tool),
        "deleted": not parent.exists(),
        "evidence_dir": str(evidence),
        "turns": state.turns,
        "admission_before": before,
        "admission_after": after,
        "after_inside_bounds": not outside_bounds(after),
        "outcomes": outcomes,
        "risks": risks,
    }
    print(json.dumps({"receipt": receipt}))
    return 1 if failed else 0


def _run_variant(scenario: Json, tool: str, model: str, options: Options, disposable: Path, log: Path, recorder: Path, state: LiveState) -> tuple[list[str], list[str]]:
    """Run a scenario's first variant live; returns (measured failures, skipped steps)."""
    failures: list[str] = []
    skipped: list[str] = []
    variant = variants_of(scenario)[0]
    base = disposable / f"work-{scenario['id']}"
    base.mkdir()
    env = Env(scenario, tool, options.roots[tool], options, variant[3], base)
    env.driver = LiveDriver(env, model, log, recorder, state)
    try:
        if run_live_ops(env, variant[1], skipped):
            run_live_ops(env, variant[2], skipped)
    except CellError as exc:
        failures.append(str(exc))
    return failures, skipped


def run_live_cell(scenario: Json, tool: str, model: str, options: Options, disposable: Path, log: Path, recorder: Path, state: LiveState) -> CellResult:
    started = time.monotonic()
    paths = evidence_paths(state.evidence, tool, str(scenario["id"]))
    result = CellResult("PASS", 0.0, hook_log=str(paths[0]), reply_artifact=str(paths[1]))
    skipped: list[str] = []
    offset, turns_before, records_before = (log.stat().st_size if log.exists() else 0), len(state.replies), len(state.records)
    if scenario.get("cases"):
        skipped.append("fault injection changes the hook's own environment or payload and is measured in payload mode only")
    try:
        if not skipped:
            result.failures, skipped = _run_variant(scenario, tool, model, options, disposable, log, recorder, state)
    except LiveStopError as exc:
        print(f"{scenario['id']} {tool}: {exc}\n      " + failure_evidence(log, offset, state.replies[turns_before:], state.stderrs[turns_before:]) + f"\n      raw record kept: {paths[0]} and {paths[1]}", flush=True)
        raise
    finally:
        write_cell_evidence(paths, log, offset, state.records[records_before:])
    if status := live_status_line(scenario):
        result.notes.append(status)
    result.notes.extend(f"UNMEASURED(live): {reason}" for reason in skipped)
    if result.failures:
        result.failures.append(failure_evidence(log, offset, state.replies[turns_before:], state.stderrs[turns_before:]))
    result.status = cell_status(result.failures, skipped)
    result.seconds = time.monotonic() - started
    return result
