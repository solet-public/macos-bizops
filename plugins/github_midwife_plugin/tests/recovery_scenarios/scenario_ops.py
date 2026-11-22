"""The scenario interpreter: one op vocabulary for both drivers, and the payload driver that feeds each tool its own hook command line."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, cast

from scenario_expect import check_hook, check_invariants
from scenario_model import TOOL_EVENTS, CellError, Env, HookRun, Json, Registration, registration_matches


class PayloadDriver:
    """Builds the payload a tool sends for one event and runs that tool's recovery hook commands."""

    auto_turn_end = True

    def __init__(self, env: Env) -> None:
        self.env = env

    def install(self, directory: Path) -> None:
        """Payload mode feeds the hook command directly; nothing is registered in a folder."""

    def edit(self, op: Json) -> tuple[HookRun, list[str]]:
        directory = self.env.dir_path(op["dir"])
        existed = write_file(directory / op["path"], str(op["content"]))
        self.env.last_dir[op["actor"]] = directory
        return self.fire("PostToolUse", self.edit_payload(op["actor"], directory, op["path"], str(op["content"]), existed), directory), []

    def shell(self, op: Json, command: str) -> tuple[HookRun, list[str]]:
        directory = self.env.dir_path(op["dir"])
        self.env.last_dir[op["actor"]] = directory
        run = self.fire("PreToolUse", self.payload("PreToolUse", op["actor"], directory, "Bash", {"command": command}), directory, str(op.get("fault", "")))
        new_ids = self.env.new_saves()
        if op.get("run", True):
            done = subprocess.run(["bash", "-c", command], cwd=directory, env=self.env.shell_env(), capture_output=True, text=True, timeout=600, check=False)
            response = {"stdout": done.stdout, "stderr": done.stderr, "exit_code": done.returncode}
            check_invariants(self.fire("PostToolUse", self.payload("PostToolUse", op["actor"], directory, "Bash", {"command": command}, {"tool_response": response}), directory, str(op.get("fault", ""))))
            self.env.new_saves()
        return run, new_ids

    def lifecycle(self, op: Json, event: str) -> tuple[HookRun, list[str]]:
        directory = self.env.dir_path(op["dir"])
        self.env.last_dir[op["actor"]] = directory
        run = self.fire(event, self.payload(event, op["actor"], directory, extra=stop_extra(event, op)), directory, str(op.get("fault", "")))
        return run, self.env.new_saves()

    def raw(self, op: Json) -> tuple[HookRun, list[str]]:
        key = "Bash" if op["event"] in TOOL_EVENTS else ""
        run = self.fire(str(op["event"]), None, self.env.base, stdin=str(self.env.expand(op["stdin"])), key=key)
        return run, self.env.new_saves()

    def _common(self, event: str, actor: str, directory: Path) -> Json:
        self.env.counter += 1
        session = cast(Json, self.env.scenario.get("actors", {})).get(actor) or f"sess-{actor}"
        payload: Json = {"session_id": session, "transcript_path": str(self.env.base / "transcript.jsonl"), "cwd": str(directory), "hook_event_name": event}
        if self.env.tool == "codex":
            payload.update({"turn_id": f"turn-{self.env.counter}", "model": "scenario-model", "permission_mode": "default"})
        else:
            payload["permission_mode"] = "default"
        return payload

    def payload(self, event: str, actor: str, directory: Path, tool_name: str = "", tool_input: Any = None, extra: Json | None = None) -> Json:
        payload = self._common(event, actor, directory)
        if tool_name:
            payload.update({"tool_name": tool_name, "tool_input": tool_input, "tool_use_id": f"toolu-{self.env.counter}"})
        payload.update(extra or {})
        return payload

    def edit_payload(self, actor: str, directory: Path, path: str, content: str, existed: bool) -> Json:
        if self.env.tool == "claude":
            return self.payload("PostToolUse", actor, directory, "Write", {"file_path": str(directory / path), "content": content})
        verb = "Update" if existed else "Add"
        patch = f"*** Begin Patch\n*** {verb} File: {path}\n+{content.rstrip()}\n*** End Patch\n"
        return self.payload("PostToolUse", actor, directory, "apply_patch", patch)

    def fire(self, event: str, payload: Json | None, directory: Path, fault: str = "", stdin: str | None = None, key: str = "") -> HookRun:
        env_vars = self.env.hook_env(fault)
        text = stdin if stdin is not None else json.dumps(payload)
        key = key or matcher_key(event, payload)
        chosen = [item for item in self.env.registrations if item.event == event and registration_matches(item, key)]
        if not chosen:
            return HookRun(event, registered=False)
        run = HookRun(event, registered=True)
        for registration in chosen:
            self.env.hook_runs += 1
            self._run_one(registration, text, directory, env_vars, run)
        return run

    def _run_one(self, registration: Registration, text: str, directory: Path, env_vars: dict[str, str], run: HookRun) -> None:
        started = time.monotonic()
        cwd = directory if directory.is_dir() else self.env.base
        try:
            done = subprocess.run(registration.argv, input=text, capture_output=True, text=True, cwd=cwd, env=env_vars, timeout=registration.timeout, check=False)
            run.exit_code = run.exit_code or done.returncode
            run.stdout += done.stdout
            run.stderr += done.stderr
        except subprocess.TimeoutExpired:
            run.timed_out = True
        except OSError as exc:
            raise CellError(f"{run.event}: could not start {registration.argv[0]!r}: {exc}") from exc
        run.seconds += time.monotonic() - started


def matcher_key(event: str, payload: Json | None) -> str:
    """What a registration's matcher is tested against: the tool name for tool events, the source for SessionStart."""
    if payload is None:
        return ""
    if event in TOOL_EVENTS:
        return str(payload.get("tool_name", ""))
    return "startup" if event == "SessionStart" else ""


def run_git(env: Env, directory: Path, args: Sequence[str], expect_exit: int | None = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=directory, env=env.shell_env(), capture_output=True, text=True, timeout=600, check=False)
    if expect_exit is not None and result.returncode != expect_exit:
        raise CellError(f"git {' '.join(args)} in {directory.name}: exit {result.returncode}, expected {expect_exit}: {result.stderr.strip()[:300]}")
    return result


def write_file(path: Path, content: str) -> bool:
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return existed


def stop_extra(event: str, op: Json) -> Json:
    if event == "Stop":
        return {"stop_hook_active": bool(op.get("stop_hook_active", False)), "last_assistant_message": "done"}
    return {"reason": "other"} if event == "SessionEnd" else {"source": "startup"}


def op_repo(env: Env, op: Json) -> None:
    directory = env.dir_path(op["dir"])
    directory.mkdir()
    run_git(env, directory, ["init", "-q", "-b", "main"])
    for key, value in (("user.name", "Scenario"), ("user.email", "scenario@example.invalid"), ("gc.auto", "0"), ("commit.gpgsign", "false")):
        run_git(env, directory, ["config", key, value])
    for path, content in cast("dict[str, str]", op.get("files", {})).items():
        write_file(directory / path, content)
    run_git(env, directory, ["add", "-A"])
    run_git(env, directory, ["commit", "-q", "-m", "init"])
    env.driver.install(directory)


def op_worktree(env: Env, op: Json) -> None:
    detached = bool(op.get("detached")) or env.tool in cast("list[str]", op.get("detached_for", []))
    if env.tool in cast("list[str]", op.get("detached_for", [])) and op.get("known_difference"):
        env.applied.add(str(op["known_difference"]))
    shape = ["--detach"] if detached else ["-b", str(op["branch"])]
    run_git(env, env.dir_path(op["repo"]), ["worktree", "add", "-q", *shape, str(env.dir_path(op["dir"]))])
    env.driver.install(env.dir_path(op["dir"]))


def op_git(env: Env, op: Json) -> None:
    args = cast("list[str]", env.expand(op["args"]))
    result = run_git(env, env.dir_path(op["dir"]), args, int(op.get("expect_exit", 0)))
    if op.get("store"):
        env.variables[str(op["store"])] = result.stdout.strip()


def op_write(env: Env, op: Json) -> None:
    write_file(env.dir_path(op["dir"]) / op["path"], str(op["content"]))


def op_mkdir(env: Env, op: Json) -> None:
    env.dir_path(op["dir"]).mkdir(parents=True, exist_ok=True)


def op_rm_rf(env: Env, op: Json) -> None:
    shutil.rmtree(str(env.expand(op["path"])))


def op_bulk(env: Env, op: Json) -> None:
    directory = env.dir_path(op["dir"]) / "bulk"
    count = int(env.expand(op["count"]))
    for number in range(count):
        if number % 1000 == 0:
            (directory / f"d{number // 1000}").mkdir(parents=True, exist_ok=True)
        (directory / f"d{number // 1000}" / f"e{number}").touch()


def op_store_config(env: Env, op: Json) -> None:
    (env.store / "config.json").write_text(json.dumps(op["values"]), encoding="utf-8")


def op_file(env: Env, op: Json) -> None:
    path = env.dir_path(op["dir"]) / op["path"]
    why = f" ({op['why']})" if op.get("why") else ""
    if op.get("absent"):
        if path.exists():
            raise CellError(f"{op['dir']}/{op['path']} should be absent{why}")
        return
    if not path.is_file():
        raise CellError(f"{op['dir']}/{op['path']} is missing{why}")
    if "equals" in op and path.read_bytes() != str(op["equals"]).encode():
        raise CellError(f"{op['dir']}/{op['path']}: expected {op['equals']!r}, found {path.read_bytes()[:80]!r}{why}")


def op_assert_equal(env: Env, op: Json) -> None:
    left, right = str(env.expand(op["left"])), str(env.expand(op["right"]))
    if left != right:
        raise CellError(f"expected {right!r}, found {left!r} ({op.get('why', 'assert_equal')})")


def op_cli(env: Env, op: Json) -> None:
    args = cast("list[str]", env.expand(op["args"]))
    expect = cast(Json, op.get("expect", {}))
    result = env.run_cli(args)
    if result.returncode != int(expect.get("exit", 0)):
        raise CellError(f"recovery_cli {args[0]} exited {result.returncode}: {result.stderr.strip()[:300]}")
    outcomes: list[str] = []
    for row in cast("list[Json]", json.loads(result.stdout)) if result.stdout.strip().startswith("[") else []:
        outcomes.append(str(row.get("outcome", "")))
    for wanted in cast("list[str]", expect.get("outcomes", [])):
        if not any(wanted in outcome for outcome in outcomes):
            raise CellError(f"restore outcome {wanted!r} not reported; the CLI reported {outcomes}")
    for name in cast("list[str]", expect.get("output_names", [])):
        if name not in result.stdout:
            raise CellError(f"the restore output does not name {name!r}: {result.stdout.strip()[:300]}")


def op_search_store(env: Env, op: Json) -> None:
    needles = [item.encode() for item in cast("list[str]", env.scenario.get("canaries", []))]
    count = 0
    for directory, _names, files in os.walk(env.store):
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            count += 1
            data = path.read_bytes()
            for needle in needles:
                if needle in data:
                    raise CellError(f"canary {needle.decode()!r} found in store file {path.relative_to(env.store)} ({op.get('when', '')})")
    env.search_counts.append(count)


def op_no_rules_dirs(env: Env, _op: Json) -> None:
    leftovers = [str(path) for path in env.base.rglob("solet-recovery-rules-*") if path.is_dir()]
    if leftovers:
        raise CellError(f"a solet-recovery-rules directory was left behind: {leftovers[:3]}")


def op_edit(env: Env, op: Json) -> None:
    run, new_ids = env.driver.edit(op)
    check_hook(env, op, run, new_ids)


def op_shell(env: Env, op: Json) -> None:
    run, new_ids = env.driver.shell(op, str(env.expand(op["command"])))
    check_hook(env, op, run, new_ids)


def _lifecycle(event: str) -> Callable[[Env, Json], None]:
    def run(env: Env, op: Json) -> None:
        hook_run, new_ids = env.driver.lifecycle(op, event)
        check_hook(env, op, hook_run, new_ids)

    return run


def op_raw_hook(env: Env, op: Json) -> None:
    run, new_ids = env.driver.raw(op)
    check_hook(env, op, run, new_ids)


OPS: Final[dict[str, Callable[[Env, Json], None]]] = {
    "repo": op_repo,
    "worktree": op_worktree,
    "git": op_git,
    "write": op_write,
    "mkdir": op_mkdir,
    "rm_rf": op_rm_rf,
    "bulk": op_bulk,
    "store_config": op_store_config,
    "file": op_file,
    "assert_equal": op_assert_equal,
    "cli": op_cli,
    "search_store": op_search_store,
    "no_rules_dirs": op_no_rules_dirs,
    "edit": op_edit,
    "shell": op_shell,
    "raw_hook": op_raw_hook,
    "stop": _lifecycle("Stop"),
    "session_end": _lifecycle("SessionEnd"),
    "session_start": _lifecycle("SessionStart"),
}


def run_ops(env: Env, ops: Sequence[Json]) -> None:
    for op in ops:
        handler = OPS.get(str(op["op"]))
        if handler is None:
            raise CellError(f"scenario uses an unknown op {op['op']!r}")
        try:
            handler(env, op)
        except CellError as exc:
            raise CellError(f"{op['op']} {op.get('path') or op.get('command') or op.get('args') or ''}: {exc}".replace("  ", " ")) from exc


def turn_ends(env: Env) -> None:
    """Every scenario's turn ends: the Stop hook must print nothing and exit 0 in whatever state the scenario left."""
    if not env.driver.auto_turn_end:
        return
    driver = PayloadDriver(env)
    for actor, directory in env.last_dir.items():
        run = driver.fire("Stop", driver.payload("Stop", actor, directory, extra=stop_extra("Stop", {})), directory)
        check_hook(env, {"expect": {"stdout": "empty"}}, run, [])
