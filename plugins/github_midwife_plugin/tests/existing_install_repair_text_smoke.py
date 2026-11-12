"""Update refusals that used to drop the outcome they had just read now report it (iss_67d2597e, public #86).

Hermetic, on the same fake runtime and Git target as ``existing_install_operations_smoke``:

- disagreeing export roots are carried as a union, with one advisory per odd
  root so even long listings retain every connector and folder;
- a refused ``claude plugin uninstall`` / ``install`` / ``marketplace add`` carries the exit code (or the timeout) and
  the CLI's stderr, with secret-shaped text removed, because the Manager refuses a whole envelope that carries it;
- ``coding_agent_running`` says when ``pgrep`` could not answer, and why, instead of saying a process is running.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

_REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(_REPO / "solet_cli" / "src"), str(_REPO / "solet_setup_contracts" / "src")]
import existing_install_operations_smoke as base  # noqa: E402
from existing_install_operations_smoke import FakeRuntime, _request  # noqa: E402
from github_midwife_plugin.export_root_validation import BUSINESS_CONNECTOR_PLUGINS  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import REPAIR_LIMIT  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, bounded_command_outcome, describe_outcome  # noqa: E402
from solet_manager.adapter_validation import public_string  # noqa: E402

_checks = 0
_NAME = base._NAME  # noqa: SLF001
_EXPORT = "existing::migration.export_root_containment"
_RENAME = "existing::migration.solet_rename"
_CACHE = "existing::runtime.plugin_cache_refresh"


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


class _Runtime(FakeRuntime):
    """The operations smoke's runtime, plus a pgrep that can fail and Claude CLI steps that can fail or find nothing installed."""

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.pgrep_outcome: CommandOutcome | None = None
        self.claude_visible = True
        self.claude_failures: dict[tuple[str, ...], CommandOutcome] = {}

    def _claude(self, argv: tuple[str, ...], output_limit: int) -> CommandOutcome:
        if argv[1:3] in self.claude_failures:
            return self.claude_failures[argv[1:3]]
        if argv[1:3] == ("plugin", "list") and not self.claude_visible:
            return bounded_command_outcome(returncode=0, timed_out=False, duration_ms=1, stdout="[]", stderr="", output_limit=output_limit)
        return super()._claude(argv, output_limit)

    def _host_probe(self, argv: tuple[str, ...]) -> CommandOutcome:
        if argv[:2] == ("/usr/bin/pgrep", "-x") and self.pgrep_outcome is not None:
            return self.pgrep_outcome
        return super()._host_probe(argv)


def _dispatch(target: Path, runtime: _Runtime, ref: str, *, phase: str = "probe") -> dict[str, Any]:
    return cast(dict[str, Any], dispatch_request(_request(target, ref, phase=phase), runtime))


def _check_export_root(root: Path) -> None:
    target, _ = base._target(root)  # noqa: SLF001
    runtime = _Runtime(root / "home")
    runtime.home.mkdir()
    config = target / "profile" / "config" / "plugins"
    config.mkdir(parents=True)
    work, stale = str(root / "work"), str(root / "stale")
    for plugin, roots in (("jira_plugin", [work]), ("salesforce_plugin", [work, stale])):
        (target / "plugins" / plugin).mkdir()
        (config / f"{plugin}.json").write_text(json.dumps({"export_allowed_roots": roots}))
    preview = _dispatch(target, runtime, _EXPORT)
    _check(preview["checkpoint_status"] == "pending" and preview["error_kind"] is None, "disagreeing connectors plan a union without refusal")
    notes = json.dumps(preview["evidence"])
    _check(work in notes and stale in notes and "missing" in notes, "each missing folder has an advisory naming it")
    empty = next(plugin for plugin in BUSINESS_CONNECTOR_PLUGINS if plugin not in ("jira_plugin", "salesforce_plugin"))
    (target / "plugins" / empty).mkdir()
    _check(_dispatch(target, runtime, _EXPORT)["checkpoint_status"] == "pending", "a connector holding no root does not block the union")
    _check_long_roots(target, runtime, config)


def _check_long_roots(target: Path, runtime: _Runtime, config: Path) -> None:
    for plugin in BUSINESS_CONNECTOR_PLUGINS:
        (target / "plugins" / plugin).mkdir(exist_ok=True)
        (config / f"{plugin}.json").write_text(json.dumps({"export_allowed_roots": [f"/{plugin}/{'a' * 300}", f"/{plugin}/{'b' * 300}"]}))
    before = {path: path.read_bytes() for path in config.glob("*.json")}
    carried = _dispatch(target, runtime, _EXPORT, phase="apply")
    entries = json.loads((config / "default_address_book_plugin/entries.json").read_text())["entries"]
    migrated = [entry for entry in entries if "migrated" in entry.get("tags", [])]
    _check(carried["checkpoint_status"] == "applied" and len(migrated) == 14, "all fourteen long roots are preserved without an ambiguity refusal")
    _check_long_root_evidence(migrated, carried)
    _check({path: path.read_bytes() for path in before} == before, "long-root migration leaves all configs byte-identical")


def _check_long_root_evidence(migrated: list[dict[str, Any]], carried: dict[str, Any]) -> None:
    _check(all(sum(f"from:{plugin}" in entry["tags"] for entry in migrated) == 2 for plugin in BUSINESS_CONNECTOR_PLUGINS), "every connector has both source-tagged saved folders")
    observed = json.dumps(carried["evidence"])
    _check(all(f"/{plugin}/{'a' * 300}" in observed and f"/{plugin}/{'b' * 300}" in observed for plugin in BUSINESS_CONNECTOR_PLUGINS), "separate advisory evidence retains every long root instead of truncating a combined repair")


def _check_coding_agent_state(root: Path) -> None:
    target, _ = base._target(root)  # noqa: SLF001
    runtime = _Runtime(root / "home")
    (runtime.home / "Library" / "LaunchAgents").mkdir(parents=True)
    (runtime.home / "Library" / "LaunchAgents" / f"local.homunculus.{_NAME}.plist").write_bytes(plistlib.dumps({"Label": f"local.homunculus.{_NAME}", "ProgramArguments": ["--homunculus", _NAME], "EnvironmentVariables": {"HOMUNCULUS_NAME": _NAME}}))
    (runtime.home / ".claude.json").write_text(json.dumps({"mcpServers": {_NAME: {"env": {"HOMUNCULUS_NAME": _NAME}}}}))
    runtime.claude_running = True
    running = _dispatch(target, runtime, _RENAME, phase="apply")
    _check(running["error_kind"] == "coding_agent_running" and "could not be determined" not in running["repair"], f"a running Claude Code is reported as running: {running['repair']}")
    runtime.claude_running = False
    for outcome, reason in ((CommandOutcome(None, False, 0, "", "", executable_missing=True), "pgrep missing"), (CommandOutcome(None, True, 0, "", ""), "pgrep timed out"), (CommandOutcome(2, False, 1, "", ""), "pgrep exited 2")):
        runtime.pgrep_outcome = outcome
        unknown = _dispatch(target, runtime, _RENAME, phase="apply")
        _check(unknown["error_kind"] == "coding_agent_running" and "could not be determined" in unknown["repair"] and reason in unknown["repair"] and unknown["repair"] != running["repair"], f"an undetermined Claude Code state names why ({reason}): {unknown['repair']}")


def _cache_target(root: Path) -> tuple[Path, _Runtime]:
    """The operations smoke's plugin-cache fixture, settled as create leaves it, then with a stale hook so a reinstall is planned."""
    target, _ = base._target(root)  # noqa: SLF001
    runtime = _Runtime(root / "home")
    runtime.home.mkdir()
    cache = runtime.home / ".claude" / "plugins" / "cache" / _NAME / "coordination-hooks" / "1.0.0"
    (cache / "hooks").mkdir(parents=True)
    (target / "profile").mkdir(exist_ok=True)
    (target / ".venv/bin").mkdir(parents=True, exist_ok=True)
    (target / ".venv/bin/python3").write_text("fixture")
    (runtime.home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {f"coordination-hooks@{_NAME}": [{"installPath": str(cache)}]}}))
    base._copy_hooks(target, cache)  # noqa: SLF001
    base._settle_as_create(target, runtime, cache)  # noqa: SLF001
    (cache / "hooks" / "wake_waiter.py").write_bytes(b"# stale\n")
    return target, runtime


def _check_cli_refusals(root: Path) -> None:
    target, runtime = _cache_target(root)
    for step, kind in (("uninstall", "claude_plugin_uninstall_failed"), ("install", "claude_plugin_install_failed")):
        runtime.claude_failures = {("plugin", step): CommandOutcome(7, False, 1, "", "boom")}
        failed = _dispatch(target, runtime, _CACHE, phase="apply")
        _check(failed["error_kind"] == kind and "exit 7" in failed["repair"] and "'boom'" in failed["repair"], f"{step} refusal carries the exit code and stderr: {failed['repair']}")
        _check(failed["repair"].endswith("--dry-run` again.") and len(failed["repair"]) < REPAIR_LIMIT, f"and keeps its remedy inside the cap: {len(failed['repair'])}")
    runtime.claude_failures = {("plugin", "uninstall"): CommandOutcome(None, True, 1, "", "")}
    _check("timed out" in _dispatch(target, runtime, _CACHE, phase="apply")["repair"], "a timed-out CLI step says it timed out")
    runtime.claude_failures = {("plugin", "uninstall"): CommandOutcome(1, False, 1, "", "token: hunter2 " + "x" * 600)}
    secret = _dispatch(target, runtime, _CACHE, phase="apply")["repair"]
    _check("hunter2" not in secret and "exit 1" in secret and secret.endswith("--dry-run` again."), f"secret-shaped stderr never reaches the repair, and a long stderr never cuts its remedy: {secret}")
    runtime.claude_failures = {}


def _check_marketplace_refusals(root: Path) -> None:
    target, runtime = _cache_target(root)
    plugin_json = target / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/.claude-plugin/plugin.json"
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.write_text("{}")
    runtime.claude_visible = False
    for step, vector, kind in (("marketplace add", ("plugin", "marketplace"), "claude_marketplace_add_failed"), ("install", ("plugin", "install"), "claude_plugin_install_failed")):
        runtime.claude_failures = {vector: CommandOutcome(9, False, 1, "", "nope")}
        failed = _dispatch(target, runtime, _CACHE, phase="apply")
        _check(failed["error_kind"] == kind and "exit 9" in failed["repair"] and "'nope'" in failed["repair"], f"{step} registration refusal carries the exit code and stderr: {failed['error_kind']} {failed['repair']}")


#: stderr a real CLI could print that the Manager's validator refuses whole: a solet keg path, secret-shaped text, both, and a keg path
#: that only appears once an inner one is cut out.
_HOSTILE_STDERR = (
    "failed to read /opt/homebrew/Cellar/solet/1.2.3/libexec/hooks/hook.json",
    "token: hunter2",
    "Authorization: Bearer abc.def-123 at /usr/local/Cellar/solet/9/bin/claude",
    "/Cellar/so/Cellar/solet/let/ password=x",
    "line one\nsecret =\n  swordfish " + "y" * 300,
)


def _validated(text: str, label: str) -> None:
    """The Manager's own rule for an envelope's repair (``_result_from_fields``): ``public_string`` at 2048 characters."""
    try:
        public_string(text, "repair", maximum=2048)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"{label}: the Manager's validator refuses it ({exc}): {text}") from exc


def _check_manager_validator(root: Path) -> None:
    """iss_67d2597e (review round 2): every repair built from CLI output passes the Manager's real ``public_string``, not a copy of it."""
    for stderr in _HOSTILE_STDERR:
        refused = False
        try:
            public_string(f"refused ({stderr})", "repair", maximum=2048)
        except ValueError:
            refused = True
        _check(refused, f"control: the Manager's validator does refuse this stderr as it is: {stderr[:50]!r}")
        _validated(describe_outcome(CommandOutcome(3, False, 1, "", stderr)), f"describe_outcome {stderr[:30]!r}")
    target, runtime = _cache_target(root)
    for stderr in _HOSTILE_STDERR:
        for vector, kind in ((("plugin", "uninstall"), "claude_plugin_uninstall_failed"), (("plugin", "install"), "claude_plugin_install_failed")):
            runtime.claude_failures = {vector: CommandOutcome(5, False, 1, "", stderr)}
            failed = _dispatch(target, runtime, _CACHE, phase="apply")
            _check(failed["error_kind"] == kind, f"{kind} still refuses as itself")
            _validated(failed["repair"], f"{kind} {stderr[:30]!r}")
        runtime.claude_failures = {("plugin", "list"): CommandOutcome(5, False, 1, "", stderr)}
        listed = _dispatch(target, runtime, _CACHE)
        _check(listed["error_kind"] == "claude_plugin_list_failed", "a failed list refuses as itself")
        _validated(listed["repair"], f"claude_plugin_list_failed {stderr[:30]!r}")
    runtime.claude_failures = {}
    _check_marketplace_validator(root / "market")


def _fixture_git_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a fresh fixture Git environment without caller-selected repositories."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    if extra_env:
        env.update(extra_env)
    return env


def test_hostile_git_environment_isolation() -> None:
    """A caller's GIT_DIR must not redirect the fixture's real Git setup."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        if root.is_relative_to(_REPO.resolve()) or any(parent.name == ".git" for parent in root.parents):
            raise RuntimeError(f"fixture-only temporary directory is inside Git metadata or the checkout: {root}")
        clean_env = _fixture_git_env()
        control_root = root / "negative-control"
        control_root.mkdir()
        control = subprocess.run(
            (sys.executable, str(Path(__file__).resolve()), "--fixture-git-target", str(control_root)),
            cwd=root,
            capture_output=True,
            text=True,
            env=clean_env,
            check=False,
        )
        _check(control.returncode == 0 and (control_root / "target" / ".git").is_dir(), "no-GIT control child creates the normal target")

        sentinel = root / "sentinel.git"
        initialized = subprocess.run(
            ("git", "init", "--quiet", "--bare", str(sentinel)),
            cwd=root,
            capture_output=True,
            text=True,
            env=clean_env,
            check=False,
        )
        if initialized.returncode != 0:
            raise RuntimeError(f"fixture-only bare sentinel setup failed: {initialized.stderr}")
        sentinel = sentinel.resolve()
        if not sentinel.is_relative_to(root) or sentinel == (_REPO / ".git").resolve():
            raise RuntimeError(f"fixture-only sentinel escaped its temporary directory: {sentinel}")
        config = sentinel / "config"
        before_config = config.read_bytes()

        hostile_root = root / "hostile"
        hostile_root.mkdir()
        hostile_env = _fixture_git_env({"GIT_DIR": str(sentinel)})
        child = subprocess.run(
            (sys.executable, str(Path(__file__).resolve()), "--fixture-git-target", str(hostile_root)),
            cwd=root,
            capture_output=True,
            text=True,
            env=hostile_env,
            check=False,
        )
        after_config = config.read_bytes()
        print(
            f"sentinel_config_sha256_before={hashlib.sha256(before_config).hexdigest()} after={hashlib.sha256(after_config).hexdigest()}",
            flush=True,
        )
        _check(after_config == before_config, "hostile GIT_DIR leaves scratch Git config byte-identical")
        _check(child.returncode == 0, "fixture-only hostile child exits successfully after config comparison")


def _check_marketplace_validator(root: Path) -> None:
    target, runtime = _cache_target(root)
    plugin_json = target / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/.claude-plugin/plugin.json"
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.write_text("{}")
    runtime.claude_visible = False
    for stderr in _HOSTILE_STDERR:
        for vector in (("plugin", "marketplace"), ("plugin", "install")):
            runtime.claude_failures = {vector: CommandOutcome(5, False, 1, "", stderr)}
            failed = _dispatch(target, runtime, _CACHE, phase="apply")
            _validated(failed["repair"], f"registration {vector[1]} {stderr[:30]!r}")


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _check_export_root(root / "export")
        _check_coding_agent_state(root / "agent")
        _check_cli_refusals(root / "cli")
        _check_marketplace_refusals(root / "marketplace")
        _check_manager_validator(root / "validator")
    test_hostile_git_environment_isolation()
    print(f"existing_install_repair_text_smoke OK: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture-git-target":
        base._target(Path(sys.argv[2]))  # noqa: SLF001
        raise SystemExit(0)
    raise SystemExit(main())
