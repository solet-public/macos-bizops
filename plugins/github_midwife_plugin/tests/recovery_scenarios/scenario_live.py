"""Live mode: the same scenarios through claude -p and codex exec, on a measured-load admission."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from scenario_model import TURN_CAP, CellError, Env, HookRun, Json, LiveStopError, UnmeasuredError, recovery_registrations
from scenario_ops import run_git

RECORDER: Final[str] = '''#!/usr/bin/env python3
"""Records one hook call (stdin, stdout, stderr, exit code, seconds) and passes the result through unchanged."""
import json, os, subprocess, sys, time

log, store, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
text = sys.stdin.read()
started = time.monotonic()
done = subprocess.run(argv, input=text, capture_output=True, text=True, env=dict(os.environ, SOLET_RECOVERY_ROOT=store, PYTHONDONTWRITEBYTECODE="1"))
try:
    event = json.loads(text).get("hook_event_name", "")
except ValueError:
    event = ""
record = {"event": event, "stdin": text, "stdout": done.stdout, "stderr": done.stderr, "exit": done.returncode, "seconds": time.monotonic() - started}
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
'''
FILE_TOOLS: Final[frozenset[str]] = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"})


def live_hook_config(tool: str, root: Path, recorder: Path, log: Path, store: Path) -> Json:
    """The recovery entries of the plugin's hooks.json, each wrapped by the recorder, as a project-level hook file."""
    hooks: Json = {}
    for registration in recovery_registrations(tool, root):
        wrapped = ["python3", str(recorder), str(log), str(store), *registration.argv]
        if tool == "claude":
            handler: Json = {"type": "command", "command": wrapped[0], "args": wrapped[1:], "timeout": registration.timeout}
        else:
            handler = {"type": "command", "command": shlex.join(wrapped), "timeout": registration.timeout}
        group: Json = {"hooks": [handler]}
        if registration.matcher:
            group["matcher"] = registration.matcher
        hooks.setdefault(registration.event, []).append(group)
    return {"hooks": hooks}


CODEX_HOOK_FILE: Final[str] = ".codex/hooks.json"
CLAUDE_HOOK_FILE: Final[str] = "claude-hooks.json"


def trust_override(folders: Sequence[Path | str]) -> str:
    """The one session flag that marks every given folder trusted for one Codex invocation: a whole `projects` table value.

    Codex splits a `-c` key at every dot and ignores TOML quotes, so a per-folder `projects."<folder>".trust_level` key never matches the
    folder (measured on codex 0.163.0 with `codex mcp get`: exit 1, project config not loaded). A table value with JSON-quoted keys does
    match (exit 0). Two `-c projects=...` flags replace each other, so all folders go in this one flag.

    Codex disables project-local config and hooks in a folder until the project is trusted, and `--dangerously-bypass-hook-trust` does not
    cover that. The entry is passed as a flag, never written: with `--ignore-user-config` the login still lives in CODEX_HOME, so a
    disposable CODEX_HOME would need a copied credential, and the real `config.toml` must not be edited. TOML quotes a key as JSON does."""
    return "projects={" + ",".join(json.dumps(str(folder)) + '={trust_level="trusted"}' for folder in folders) + "}"


def tool_command(tool: str, model: str, prompt: str, session: str, first: bool, *, settings: str = "", skip_git_check: bool = False, trusted: Sequence[Path | str] = (), workdir: str = "") -> list[str]:
    """One tool turn. `settings` is the Claude hook file, kept outside every repository folder; `skip_git_check` is for a folder that is not a git repository (the credential probe).

    `trusted` lists the folders Codex must treat as trusted for this turn (see `trust_override`). `workdir` names the folder the first Codex turn
    works in (`-C`); `exec resume` has no such flag and keeps the process folder, which the driver also sets."""
    if tool == "claude":
        base = ["claude", "-p", prompt, "--model", model, "--setting-sources", "project,local", "--allowedTools", "Write Edit Bash", "--output-format", "json", "--max-budget-usd", "0.50"]
        return [*base, *(["--settings", settings] if settings else []), "--session-id" if first else "--resume", session]
    flags = ["--ignore-user-config", "-m", model, "-c", 'sandbox_mode="workspace-write"', "--dangerously-bypass-hook-trust", "--json"]  # `exec resume` has no -s; -c carries the sandbox on both forms
    flags += ["-c", trust_override(trusted)] if trusted else []
    flags += ["--skip-git-repo-check"] if skip_git_check else []
    flags += ["-C", workdir] if workdir and first else []
    return ["codex", "exec", *flags, prompt] if first else ["codex", "exec", "resume", *flags, session, prompt]


def shell_prompt(directory: Path, command: str) -> str:
    """The instruction for one shell step. The folder is named in full, and the model is told the command is the intended test: a model that
    judged a destructive command unsafe (S9's `git branch -D`) issued nothing, which left the hook with nothing to measure."""
    return f"The folder {directory} is a disposable scratch git repository made for an automated test, and the command below is the whole test. Running it is intended and safe: run it without asking for confirmation, without refusing or explaining, in that folder, exactly as written, as one shell command, and then stop. Command: {command}"


def edit_prompt(directory: Path, path: str, content: str) -> str:
    return f"In the folder {directory} (a disposable scratch repository made for an automated test), using your file-writing tool, not a shell command, write the file {path} with exactly this content, including every newline: {json.dumps(content)}. Do nothing else."


def lifecycle_gap(op: Json, event: str) -> str:
    """Why a Stop or SessionEnd step cannot be produced by a live tool, or empty when it can.

    One `claude -p` or `codex exec` process runs per model turn, so the turn end and the session end coincide: SessionEnd fires right
    after the same turn's Stop, with no edit in between. A step that needs work changed after the last turn-end save, or a Stop hook
    re-entered by a Stop hook, has no live precondition; running it would measure the engine's fingerprint dedupe, not the adapter."""
    if op.get("stop_hook_active"):
        return "a live tool sets stop_hook_active only when a Stop hook continued the turn, which the recovery hook never does; payload mode measures it"
    if event == "SessionEnd" and int(cast(Json, op.get("expect") or {}).get("saves", 0)) > 0:
        return "one process per turn: SessionEnd fires right after the same turn's Stop with no edit between, so work changed after the last turn-end save cannot occur live; payload mode measures it"
    return ""


def _thread_id(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            value: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(cast(Json, value).get("thread_id"), str):
            return str(cast(Json, value)["thread_id"])
    raise CellError("the codex --json events carry no thread_id; record the first events as a finding")


def _text(value: str | bytes | None) -> str:
    """A subprocess stream as text; a timeout hands back bytes or nothing."""
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode(errors="replace")


def evidence_paths(directory: Path, tool: str, scenario_id: str) -> tuple[Path, Path]:
    """(raw hook log, model reply artifact) of one live cell. The names are fixed before the cell runs, so a cell that stops still names them."""
    return directory / f"{tool}-{scenario_id}.hooks.jsonl", directory / f"{tool}-{scenario_id}.reply.jsonl"


def write_cell_evidence(paths: tuple[Path, Path], log: Path, offset: int, records: Sequence[Json]) -> None:
    """Keep the cell's raw record outside the disposable folder, which is deleted at the end of the run.

    The hook log is the recorder's own bytes for this cell, one JSON line per hook call (payload in, output out, exit, seconds); it is empty
    when no hook fired, and that emptiness is the measurement. The reply artifact holds one JSON line per tool process: its command line,
    folder, exit code, and its whole stdout (Codex's JSONL events, Claude's JSON result) and stderr."""
    hooks, reply = paths
    hooks.write_bytes(log.read_bytes()[offset:] if log.exists() else b"")
    lines = [json.dumps(record) for record in records] or [json.dumps({"note": "no tool process ran for this cell"})]
    reply.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class LiveState:
    """What a whole live run of one tool accumulates: model turns used, what the tool said (clipped, and in full in `records`), and the folder that keeps each cell's raw record."""

    evidence: Path
    turns: int = 0
    replies: list[str] = field(default_factory=lambda: cast("list[str]", []))
    stderrs: list[str] = field(default_factory=lambda: cast("list[str]", []))
    records: list[Json] = field(default_factory=lambda: cast("list[Json]", []))  # every tool process in full: command, folder, exit code, stdout, stderr


class LiveDriver:
    """Makes the real tool act: one `claude -p` or `codex exec` turn per file edit or shell command."""

    auto_turn_end = False

    def __init__(self, env: Env, model: str, log: Path, recorder: Path, state: LiveState) -> None:
        self.env, self.model, self.log, self.recorder, self.state = env, model, log, recorder, state
        self.sessions: dict[str, str] = {}
        self.records: dict[str, list[Json]] = {}
        self.installed: set[Path] = set()
        self.claude_hooks = self.env.base / CLAUDE_HOOK_FILE

    def install(self, directory: Path) -> None:
        """Register the recovery hooks for turns run in this folder.

        Claude reads one hook file passed with --settings, kept in the scenario's base directory, outside every repository folder: a
        scenario that stashes or cleans ignored files (S11's `git stash push --all`) must not remove the instrument's own hooks.
        Codex has no such option, so its project-level file stays in the folder, kept out of the visible paths through info/exclude;
        a step that removes it is reported UNMEASURED by `_turn`."""
        config = live_hook_config(self.env.tool, self.env.root, self.recorder, self.log, self.env.store)
        if self.env.tool == "claude":
            self.claude_hooks.write_text(json.dumps(config, indent=2), encoding="utf-8")
        else:
            target = directory / CODEX_HOOK_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(config, indent=2), encoding="utf-8")
            common = Path(run_git(self.env, directory, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip())
            with (common / "info" / "exclude").open("a", encoding="utf-8") as handle:
                handle.write(".codex/\n")
        self.installed.add(directory)

    def _require_hooks(self, directory: Path) -> None:
        """A turn runs only where its hooks can fire; otherwise the step is UNMEASURED, never run without them."""
        if directory not in self.installed:
            raise UnmeasuredError(f"{directory.name} is not a git folder with hooks installed; live hooks fire only in installed folders")
        if self.env.tool == "codex" and not (directory / CODEX_HOOK_FILE).is_file():
            raise UnmeasuredError(f"{directory.name}: the project-level hook file {CODEX_HOOK_FILE} is gone, removed by an earlier step of this scenario (git stash --all stashes ignored files, and the harness keeps the file out of the visible paths through info/exclude); Codex would run this turn with no hooks")

    def _turn(self, op: Json, directory: Path, prompt: str) -> list[Json]:
        self._require_hooks(directory)
        probe = run_git(self.env, directory, ["rev-parse", "--git-dir"], expect_exit=None)
        if probe.returncode != 0:
            raise LiveStopError(f"harness fault before the turn: {directory} is not a git repository ({probe.stderr.strip()[:200]}); the tool was not launched")
        if self.state.turns >= TURN_CAP:
            raise LiveStopError(f"the turn cap of {TURN_CAP} per tool is reached with scenarios left")
        self.state.turns += 1
        actor = str(op["actor"])
        first = actor not in self.sessions
        session = self.sessions.setdefault(actor, str(uuid.uuid4()))
        offset = self.log.stat().st_size if self.log.exists() else 0
        stdout = self._launch(directory, prompt, session, first)
        self.state.replies.append(stdout[-4000:])
        if first and self.env.tool == "codex":
            self.sessions[actor] = _thread_id(stdout)
        text = self.log.read_bytes()[offset:].decode() if self.log.exists() else ""
        self.records[actor] = [cast(Json, json.loads(line)) for line in text.splitlines() if line.strip()]
        return self.records[actor]

    def _trusted(self) -> list[Path]:
        """The folders Codex must treat as trusted for a turn: every folder the harness installed hooks in (a worktree and its main repository)."""
        return sorted(self.installed) if self.env.tool == "codex" else []

    def _launch(self, directory: Path, prompt: str, session: str, first: bool) -> str:
        """One tool process, bounded by the scenario's timeout (120 s; S6 240 s). Its command, folder, exit code and output are kept whole."""
        limit = 240 if self.env.scenario["id"] == "S6" else 120
        command = tool_command(self.env.tool, self.model, prompt, session, first, settings=str(self.claude_hooks) if self.env.tool == "claude" else "", trusted=self._trusted(), workdir=str(directory))
        record: Json = {"scenario": self.env.scenario["id"], "command": command, "folder": str(directory), "exit": None, "stdout": "", "stderr": ""}
        self.state.records.append(record)
        try:
            done = subprocess.run(command, cwd=directory, env={**os.environ, "SOLET_RECOVERY_ROOT": str(self.env.store)}, capture_output=True, text=True, timeout=limit, check=False)
        except subprocess.TimeoutExpired as exc:
            record["exit"] = f"timeout after {limit}s"
            record["stdout"], record["stderr"] = (_text(exc.stdout), _text(exc.stderr))
            raise LiveStopError(f"scenario {self.env.scenario['id']} exceeded its {limit}s timeout") from exc
        record.update({"exit": done.returncode, "stdout": done.stdout, "stderr": done.stderr})
        self.state.stderrs.append(done.stderr[-2000:])
        if done.returncode != 0:
            raise LiveStopError(f"{self.env.tool} exited {done.returncode} for this turn (a harness or tool error, not an adapter result): {done.stderr.strip()[:300]}")
        return done.stdout

    def _silence(self, actor: str) -> str:
        """What a reader needs when no hook fired: whether any hook fired at all in this turn, and the tail of what the model said."""
        said = self.state.replies[-1][-600:] if self.state.replies else "<no model turn ran>"
        nothing = " No hook of any event fired in this turn" + ("; Codex loads project-local hooks only in a trusted folder, so check the trust entry the command line carried." if self.env.tool == "codex" else ".") if not self.records[actor] else ""
        return f"{nothing} The model's last reply ended: {said!r}"

    def _check_folder(self, actor: str, directory: Path, keep: Callable[[Json], bool]) -> None:
        """The scenario's expectations are about `directory`; a tool that ran the command elsewhere (S6 saw 'not a git repository') measured something else."""
        for record in self.records[actor]:
            if record["event"] != "PreToolUse" or not keep(record):
                continue
            ran_in = str(json.loads(record["stdin"]).get("cwd", ""))
            if ran_in and Path(ran_in).resolve() != directory.resolve():
                raise UnmeasuredError(f"the tool ran the command in {ran_in}, not in the scenario folder {directory}; the expectations of this step are about the scenario folder")

    def _run(self, op: Json, event: str, keep: Callable[[Json], bool]) -> HookRun:
        chosen = [record for record in self.records[str(op["actor"])] if record["event"] == event and keep(record)]
        if not chosen:
            raise CellError(f"the tool fired no {event} recovery hook for this step; recorded events: {[record['event'] for record in self.records[str(op['actor'])]]}{self._silence(str(op['actor']))}")
        return HookRun(event, True, max(int(record["exit"]) for record in chosen), "".join(str(record["stdout"]) for record in chosen), "".join(str(record["stderr"]) for record in chosen), sum(float(record["seconds"]) for record in chosen))

    def edit(self, op: Json) -> tuple[HookRun, list[str]]:
        directory = self.env.dir_path(op["dir"])
        self._turn(op, directory, edit_prompt(directory, str(op["path"]), str(op["content"])))
        written = directory / op["path"]
        if not written.is_file() or written.read_bytes() != str(op["content"]).encode():
            raise CellError(f"the tool did not write {op['path']} with the exact content")
        self.env.last_dir[str(op["actor"])] = directory
        return self._run(op, "PostToolUse", lambda record: json.loads(record["stdin"]).get("tool_name") in FILE_TOOLS), []

    def shell(self, op: Json, command: str) -> tuple[HookRun, list[str]]:
        if op.get("fault") or op.get("run") is False:
            raise UnmeasuredError("fault injection changes the hook's own environment or payload and is measured in payload mode only")
        directory = self.env.dir_path(op["dir"])
        self._turn(op, directory, shell_prompt(directory, command))
        self.env.last_dir[str(op["actor"])] = directory

        def keep(record: Json) -> bool:
            tool_input = json.loads(record["stdin"]).get("tool_input", {})
            return bool(tool_input.get("command") == command or command in json.dumps(tool_input))

        run = self._run(op, "PreToolUse", keep)
        self._check_folder(str(op["actor"]), directory, keep)
        return run, self.env.new_saves("pre_discard")

    def lifecycle(self, op: Json, event: str) -> tuple[HookRun, list[str]]:
        actor = str(op["actor"])
        if gap := lifecycle_gap(op, event):
            raise UnmeasuredError(f"{event} for {actor}: {gap}")
        if actor not in self.records:
            raise UnmeasuredError(f"{event} for {actor} follows no live turn of that actor")
        trigger = {"Stop": "turn_end", "SessionEnd": "session_end"}.get(event, "")
        return self._run(op, event, lambda _record: True), (self.env.new_saves(trigger) if trigger else [])

    def raw(self, op: Json) -> tuple[HookRun, list[str]]:
        raise UnmeasuredError("malformed payloads are injected in payload mode only")
