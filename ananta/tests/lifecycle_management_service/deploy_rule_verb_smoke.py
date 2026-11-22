#!/usr/bin/env python3
"""The solet's ``run_deploy_rule`` verb through the real bridge route (no pytest).

The real ``solet-bridge call`` CLI posts over HTTP to the real bridge routes and
PlatformSurface. The real ActionFactory and ActionProcessor dispatch the
``service_interface::lifecycle_management_service::run_deploy_rule`` entry,
scanned from the shipped interface, to the real LifecycleManagementService.
The only fake is the verb's start port for the detached guarded deploy rule. It
records each launch, holds the inherited attempt lock the way the child would,
and writes the child's terminal receipt when the smoke ends that child. Each
answer is read the way a landing caller outside the solet reads one: status
completed, action completed, no error, a data object.

Pins:

* a new landed master starts exactly one rule child, which answers deferred /
  in_progress with its attempt id;
* a repeat call attaches to the running attempt instead of starting another;
* reading by attempt id gives the child's own terminal receipt as deployed,
  no-delta (already_current), deferred, failed, skipped or abandoned;
* one child-start line names the target, the trigger, the child pid and the
  server-built caller;
* nothing else is produced: no other action, job, schedule, session or service
  lookup.

On a base that lacks the verb it is red, at the registered-interface assertion.

Run from the repository root (the smoke sets its own solet name):
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python3 -B ananta/tests/lifecycle_management_service/deploy_rule_verb_smoke.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
for _source in (
    REPO_ROOT / "ananta" / "src",
    REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src",
    REPO_ROOT / "plugins" / "agent_messaging_plugin" / "tests",
):
    sys.path.insert(0, str(_source))

from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.services.lifecycle_management_service.interfaces import public as lifecycle_public  # noqa: E402
from ananta.services.lifecycle_management_service.service import LifecycleManagementService  # noqa: E402
from bridge_wait_fixture_support import Harness  # noqa: E402

from agent_messaging_plugin.process_exposure import ProcessExportPolicy  # noqa: E402

KEY = "service_interface::lifecycle_management_service::run_deploy_rule"
SOLET = "deploy-rule-smoke"
RULE = Path("ananta/src/ananta/services/lifecycle_management_service/_deploy_by_rule.py")
ACTOR: dict[str, str] = {"agent_instance_id": "agi-engine-smoke", "actor_model": "claude-sonnet-5-5", "actor_effort": "medium"}
VERB_LOGGER = "ananta.services.lifecycle_management_service.deployment_record_verbs"
GIT = ("git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", "commit.gpgsign=false", "-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid")

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class FakeRule:
    """The detached rule child, faked only at the verb's start port."""

    def __init__(self) -> None:
        self.launches: list[Any] = []
        self._held: list[int] = []

    def start(self, launch: Any) -> int:
        self.launches.append(launch)
        self._held.append(os.dup(launch.lock_fd))  # the child inherits the attempt lock and keeps it
        return 41000 + len(self.launches)

    def end(self, receipt: dict[str, Any] | None, stderr: str = "") -> None:
        launch = self.launches[-1]
        if receipt is not None:
            launch.stdout.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
        launch.stderr.write_text(stderr, encoding="utf-8")
        os.close(self._held.pop())  # the child's exit releases the attempt lock


class FailingStart:
    def start(self, launch: Any) -> int:
        del launch
        raise OSError("exec format error")


class Orchestrator:
    """APP_HOME is the solet's own; every service lookup is recorded."""

    def __init__(self, app_home: Path) -> None:
        self.APP_HOME = str(app_home)
        self.asked: list[str] = []

    def get_service(self, name: str) -> object | None:
        self.asked.append(name)
        return None


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def starts(self) -> list[str]:
        return [line for line in self.lines if line.startswith("deploy_rule ")]


def _registered_entries() -> dict[str, Any]:
    processes: dict[str, Any] = {}
    ServiceInterfaceScanner(InvocationSchemaGenerator())._scan_module_classes(lifecycle_public, lifecycle_public.__name__, processes)  # noqa: SLF001 — the registry builder's own scan
    return processes


def _git_env(home: Path) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return {**environment, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo: Path, home: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_git_env(home)).stdout.strip()


def _land(repo: Path, home: Path, message: str) -> str:
    _git(repo, home, "commit", "--allow-empty", "-q", "-m", message)
    return _git(repo, home, "rev-parse", "HEAD")


def _layout(root: Path) -> tuple[Path, Path, Path]:
    """A home, a checkout with the rule's interpreter, bridge and script, and its profile."""
    home = root / "home"
    home.mkdir()
    repo = root / "checkout"
    for relative in (".venv/bin/python3", ".venv/bin/solet-bridge", str(RULE)):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# never run: the smoke fakes the start port\n", encoding="utf-8")
    subprocess.run([*GIT, "init", "-q", "-b", "master", str(repo)], check=True, capture_output=True, env=_git_env(home))
    app_home = repo / "profile"
    app_home.mkdir()
    return home, repo, app_home


def _arguments(**extra: Any) -> dict[str, Any]:
    return {"solet_name": SOLET, **ACTOR, "trigger": "landing", **extra}


def _completed_data(envelope: object) -> dict[str, Any]:
    """Read the answer exactly as a landing caller's bridge client reads it."""
    assert isinstance(envelope, dict), f"NOT_AN_ENVELOPE: {envelope!r}"
    result = envelope.get("result")
    assert envelope.get("status") == "completed" and isinstance(result, dict), f"NOT_COMPLETED: {envelope!r}"
    assert result.get("action_status") == "completed" and result.get("error") is None, f"ACTION_NOT_COMPLETED: {result!r}"
    data = result.get("data")
    assert isinstance(data, dict), f"NO_DATA: {result!r}"
    return data


def _production_exports(harness: Harness) -> None:
    """The installed bridge exports every service verb (profile/config/plugins/agent_messaging_plugin.json)."""
    harness.surface._export_policy = ProcessExportPolicy(enabled=True, allow_patterns=("plugin::*::*", "service_interface::*::*"))  # noqa: SLF001 — the installed export patterns


def _base_answer() -> str:
    """What the real caller route answers on a tree without the verb."""
    with Harness() as harness:
        _production_exports(harness)
        completed = harness.call(("--no-wait",), process_key=KEY, arguments=_arguments())
    return f"rc={completed.returncode} stdout={completed.stdout.strip()[-400:]} stderr={completed.stderr.strip()[-400:]}"


def _bridge(harness: Harness, arguments: dict[str, Any]) -> dict[str, Any]:
    completed = harness.call(("--no-wait",), process_key=KEY, arguments=arguments)
    assert completed.returncode == 0, f"CLI_CALL_FAILED rc={completed.returncode} stdout={completed.stdout[-800:]} stderr={completed.stderr[-800:]}"
    return _completed_data(json.loads(completed.stdout))


def _refused(result: dict[str, Any], code: str, label: str) -> None:
    error = str(result.get("error", ""))
    _check((result.get("action_status"), (result.get("data") or {}).get("error_code"), error[: len(code)]) == ("error", code, code), label)


def _typed(answer: dict[str, Any]) -> tuple[Any, Any]:
    return answer["outcome"], answer["decision"]


def _git_names(environment: dict[str, str]) -> list[str]:
    return sorted(name for name in environment if name.startswith("GIT_"))


def _expected_argv(paths: dict[str, Path]) -> list[str]:
    head = [str(paths["repo"] / ".venv/bin/python3"), str(paths["repo"] / RULE), "--trigger", "landing", "--repo", str(paths["repo"]), "--app-home", str(paths["app_home"])]
    places = ["--releases-root", str(paths["home"] / ".ananta" / "releases" / SOLET), "--bridge", str(paths["repo"] / ".venv/bin/solet-bridge")]
    return [*head, *places, "--agent-instance-id", ACTOR["agent_instance_id"], "--actor-model", ACTOR["actor_model"], "--actor-effort", ACTOR["actor_effort"]]


def _first_start(harness: Harness, rule: FakeRule, lines: Lines, paths: dict[str, Path], landed: str) -> str:
    print("a new landed master: one rule child, answered deferred / in_progress")
    data = _bridge(harness, _arguments())
    attempt = str(data.get("attempt_id"))
    _check(_typed(data) == ("deferred", "in_progress"), "the landed master answers deferred / in_progress")
    _check(len(rule.launches) == 1, "exactly one guarded rule child was started")
    _check((attempt[:4], data["target"], data["child_pid"]) == ("dra-", landed, 41001), "the answer names the attempt, the landed master and the child pid")
    _check((data["receipt"], data["detail"]) == (None, None), "a running attempt carries no receipt yet")
    launch = rule.launches[0]
    _check(list(launch.argv) == _expected_argv(paths), "the rule runs from the solet's own checkout, profile, releases root and bridge")
    _check((launch.env.get("SOLET_NAME"), _git_names(launch.env)) == (SOLET, []), "the child gets this solet's name and no inherited GIT_* setting")
    where = (launch.cwd, launch.stdout, launch.stderr)
    _check(where == (paths["attempts"], paths["attempts"] / f"{attempt}.out", paths["attempts"] / f"{attempt}.err"), "the child runs in the attempt folder and writes its receipt there")
    _check(lines.starts() == [f"deploy_rule target={landed} trigger=landing child_pid=41001 caller=operator"], "one structured child-start line names target, trigger, child pid and the server-built caller")
    record = json.loads((paths["attempts"] / f"{attempt}.json").read_text(encoding="utf-8"))
    kept = (record["child_pid"], record["target"], record["caller"], record["agent_instance_id"])
    _check(kept == (41001, landed, "operator", ACTOR["agent_instance_id"]), "the attempt record keeps pid, target, caller and actor")
    return attempt


def _attach_and_read(harness: Harness, rule: FakeRule, lines: Lines, attempt: str) -> None:
    print("a repeat call attaches; a read by attempt id answers in_progress")
    again = _bridge(harness, _arguments())
    _check((again["attempt_id"], again["decision"]) == (attempt, "in_progress"), "a repeat call attaches to the running attempt")
    read = _bridge(harness, _arguments(attempt_id=attempt))
    _check((read["attempt_id"], *_typed(read)) == (attempt, "deferred", "in_progress"), "reading the running attempt answers in_progress")
    _check((len(rule.launches), len(lines.starts())) == (1, 1), "repeat and read started no second child")


def _finished(harness: Harness, rule: FakeRule, receipt: dict[str, Any] | None, stderr: str = "") -> dict[str, Any]:
    started = _bridge(harness, _arguments())
    rule.end(receipt, stderr)
    return _bridge(harness, _arguments(attempt_id=started["attempt_id"]))


def _deployed_and_no_delta(harness: Harness, rule: FakeRule, first: str, landed: str) -> None:
    print("terminal outcomes come from the child's own receipt: deployed, then no delta")
    deployed = {"decision": "deployed", "target": landed, "release": "rel-smoke", "deployment_id": "dpr-smoke", "trigger": "landing"}
    rule.end(deployed)
    read = _bridge(harness, _arguments(attempt_id=first))
    _check((*_typed(read), read["receipt"]) == ("deployed", "deployed", deployed), "the finished attempt answers deployed with the rule's receipt")
    no_delta = _finished(harness, rule, {"decision": "already_current", "target": landed})
    _check((*_typed(no_delta), len(rule.launches)) == ("deployed", "already_current", 2), "no delta: the next run answers already_current, typed deployed")


def _held_failed_skipped(harness: Harness, rule: FakeRule, first: str, paths: dict[str, Path]) -> None:
    print("terminal outcomes on a new master: deferred, failed, skipped, abandoned")
    moved = _land(paths["repo"], paths["home"], "landing 2")
    deferred = _finished(harness, rule, {"decision": "deferred_gate", "target": moved, "receipt": "/gate.json"})
    _check((*_typed(deferred), deferred["target"]) == ("deferred", "deferred_gate", moved), "a held gate answers deferred_gate for the new master")
    failed = _finished(harness, rule, {"decision": "failed", "target": moved, "error": "swap_deadline_unresolved"})
    _check((*_typed(failed), "swap_deadline_unresolved" in str(failed["detail"])) == ("failed", "failed", True), "a failed run answers failed with the rule's error")
    skipped = _finished(harness, rule, {"decision": "skipped_nonproduction", "target": moved})
    _check(_typed(skipped) == ("skipped", "skipped_nonproduction"), "a non-production landing answers skipped")
    abandoned = _finished(harness, rule, None, "Traceback (most recent call last):\nRuntimeError: boom\n")
    _check((*_typed(abandoned), "boom" in str(abandoned["detail"])) == ("failed", "attempt_abandoned", True), "a child that ended without a receipt answers attempt_abandoned with its stderr")
    earlier = _bridge(harness, _arguments(attempt_id=first))
    _check(earlier["decision"] == "deployed", "an earlier attempt stays readable by its id")
    _check(len(rule.launches) == 6, "each started run is one launch")


def _nothing_else(harness: Harness, orchestrator: Orchestrator, calls: int) -> None:
    print("no other action, job, schedule, session or service lookup")
    actions = harness.runtime.state.read_state("core", {"table": "action_events", "filters": {}})["data"]["records"]
    _check((len(actions), {row["process_key"] for row in actions}) == (calls, {KEY}), "only the caller's own calls were dispatched")
    jobs = harness.runtime.state.read_state("core", {"table": "job", "filters": {}})["data"]["records"]
    _check(jobs == [], "no job was created")
    _check(orchestrator.asked == [], "the verb asked for no service (no scheduler, inference or session)")


def _direct_refusals(service: LifecycleManagementService, rule: FakeRule, paths: dict[str, Path]) -> None:
    print("argument and configuration refusals happen before any start")
    before = len(rule.launches)
    _refused(service.run_deploy_rule(**_arguments(solet_name="other-solet")), "solet_mismatch", "a call naming another solet is refused solet_mismatch")
    _refused(service.run_deploy_rule(**_arguments(trigger="manual")), "invalid_trigger", "an unknown trigger is refused invalid_trigger")
    _refused(service.run_deploy_rule(**_arguments(attempt_id="../outside")), "invalid_argument", "a malformed attempt id is refused invalid_argument")
    _refused(service.run_deploy_rule(**_arguments(attempt_id="dra-20000101T000000000000Z-00000000")), "unknown_attempt", "an unknown attempt id is refused unknown_attempt")
    bare = paths["home"] / "no-checkout" / "profile"
    bare.mkdir(parents=True)
    unconfigured = LifecycleManagementService(Orchestrator(bare))
    _refused(unconfigured.run_deploy_rule(**_arguments()), "deploy_rule_unconfigured", "a profile with no checkout above it is refused deploy_rule_unconfigured")
    _check((len(rule.launches), (bare / "data").exists()) == (before, False), "no refusal started a child or wrote a file")


def _start_failure_and_retention(service: LifecycleManagementService, rule: FakeRule, paths: dict[str, Path]) -> None:
    from ananta.services.lifecycle_management_service.deployment_record_verbs import DeployRulePorts  # noqa: PLC0415 — after the red-first assertion

    print("a start that fails, and the bounded attempt history")
    service._deploy_rule_ports = DeployRulePorts(start=FailingStart().start)  # noqa: SLF001 — the verb's one start port
    failed = service.run_deploy_rule(**_arguments())["data"]
    _check((*_typed(failed), failed["attempt_id"], "exec format error" in str(failed["detail"])) == ("failed", "start_failed", None, True), "a start that raises answers start_failed")
    service._deploy_rule_ports = DeployRulePorts(start=rule.start)  # noqa: SLF001
    names: list[str] = []
    for _ in range(22):
        names.append(service.run_deploy_rule(**_arguments())["data"]["attempt_id"])
        rule.end({"decision": "already_current", "target": "c" * 40})
    kept = sorted(path.stem for path in paths["attempts"].glob("dra-*.json"))
    _check((len(kept), kept[-1]) == (20, names[-1]), "the newest twenty attempts are kept")
    _check((list(paths["attempts"].glob("*.tmp")), (paths["attempts"] / f"{names[0]}.out").exists()) == ([], False), "older attempts are removed with their output")
    _refused(service.run_deploy_rule(**_arguments(attempt_id=names[0])), "unknown_attempt", "a removed attempt is unknown")


STAND_IN_RULE = """import json, os, sys, time
time.sleep(1.5)
print(json.dumps({"decision": "already_current", "target": "a" * 40, "argv": sys.argv[1:], "solet": os.environ.get("SOLET_NAME"), "git_env": sorted(n for n in os.environ if n.startswith("GIT_"))}))
"""


def _real_start_port(root: Path) -> None:
    """The real start port: own session, inherited lock, stdout receipt."""
    import time  # noqa: PLC0415 — only this case waits on a real process

    print("the real start port: a detached child keeps the attempt lock and leaves its receipt")
    repo = root / "real-checkout"
    interpreter = repo / ".venv/bin/python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    interpreter.chmod(0o755)
    (repo / ".venv/bin/solet-bridge").write_text("# never run by the stand-in rule\n", encoding="utf-8")
    (repo / RULE).parent.mkdir(parents=True)
    (repo / RULE).write_text(STAND_IN_RULE, encoding="utf-8")
    (repo / "profile").mkdir()
    service = LifecycleManagementService(Orchestrator(repo / "profile"))
    started = service.run_deploy_rule(**_arguments())["data"]
    pid = started["child_pid"]
    _check((started["decision"], os.getsid(int(pid or 0))) == ("in_progress", pid), "the rule child leads its own session, so a swap of the solet does not end it")
    running = service.run_deploy_rule(**_arguments(attempt_id=started["attempt_id"]))["data"]
    _check(running["decision"] == "in_progress", "the child holds the attempt lock after the verb closed its own descriptor")
    deadline = time.monotonic() + 30
    while running["decision"] == "in_progress" and time.monotonic() < deadline:
        time.sleep(0.2)
        running = service.run_deploy_rule(**_arguments(attempt_id=started["attempt_id"]))["data"]
    receipt = running["receipt"] or {}
    _check(_typed(running) == ("deployed", "already_current"), "the child's exit releases the lock and its stdout receipt is the answer")
    _check((receipt.get("solet"), receipt.get("git_env"), receipt.get("argv", [])[:2]) == (SOLET, [], ["--trigger", "landing"]), "the real child got this solet's name, no GIT_* setting and the rule's arguments")


def _kb_matches_contract() -> None:
    print("KB JSON: process key, every return key, embedding bound")
    document = json.loads((REPO_ROOT / "ananta/knowledge_base/processes/lifecycle_management_service/run_deploy_rule.json").read_text(encoding="utf-8"))
    verb: Any = lifecycle_public.LifecycleManagementAPI.run_deploy_rule
    declared = set(verb._service_interface_metadata.return_value_schema.properties)  # noqa: SLF001 — the decorator's own schema
    _check(document["process_key"] == KEY, "the KB JSON carries the verb's process key")
    _check(all(name in document["result_processor_customizations"]["result_description"] for name in declared), "the result description names every declared return key")
    _check(200 <= len(document["embedding_description"]) <= 400, "the embedding description is within the registry's 200..400 bound")


def _run(root: Path, lines: Lines) -> None:
    home, repo, app_home = _layout(root)
    paths = {"home": home, "repo": repo, "app_home": app_home, "attempts": app_home / "data" / "runtime" / "deploy-by-rule"}
    _land(repo, home, "deployed release")
    landed = _land(repo, home, "landing 1")
    entries = _registered_entries()
    orchestrator = Orchestrator(app_home)
    service = LifecycleManagementService(orchestrator)
    rule = FakeRule()
    from ananta.services.lifecycle_management_service.deployment_record_verbs import DeployRulePorts  # noqa: PLC0415 — after the red-first assertion

    service._deploy_rule_ports = DeployRulePorts(start=rule.start)  # noqa: SLF001 — the verb's one start port
    with Harness(extra_entries={KEY: entries[KEY]}) as harness:
        _production_exports(harness)
        dispatch: Any = harness.runtime.processor.orchestrator  # the harness's service lookup, extended by the real lifecycle service
        lookup = dispatch.get_service
        dispatch.get_service = lambda name: service if name == "lifecycle_management_service" else lookup(name)
        first = _first_start(harness, rule, lines, paths, landed)
        _attach_and_read(harness, rule, lines, first)
        _deployed_and_no_delta(harness, rule, first, landed)
        _held_failed_skipped(harness, rule, first, paths)
        _nothing_else(harness, orchestrator, calls=4 + 2 * 5 + 1)
    _direct_refusals(service, rule, paths)
    _start_failure_and_retention(service, rule, paths)
    _real_start_port(root)


def main() -> int:
    with patch.dict(os.environ, {"SOLET_NAME": SOLET}):
        return _main()


def _main() -> int:
    print("registered interface: the verb exists on the shipped lifecycle interface")
    entries = _registered_entries()
    _check(KEY in entries, f"MISSING_VERB: the scanned lifecycle interface registers {KEY}")
    if KEY not in entries:
        print(f"  the real caller route answers: {_base_answer()}")
        print(f"\n{_passed} passed, {len(_failed)} failed")
        return 1
    _check(entries[KEY].get("requires_call_context") is True, "the server builds the caller identity for attribution")
    _kb_matches_contract()
    lines = Lines()
    logger = logging.getLogger(VERB_LOGGER)
    logger.addHandler(lines)
    logger.setLevel(logging.INFO)
    with tempfile.TemporaryDirectory(prefix="deploy-rule-verb-") as raw:
        root = Path(raw).resolve()
        environment = {"SOLET_NAME": SOLET, "HOME": str(root / "home"), "GIT_DIR": str(root / "not-a-repository")}
        with patch.dict(os.environ, environment):
            _run(root, lines)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
