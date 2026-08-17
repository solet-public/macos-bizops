#!/usr/bin/env python3
"""Unit smoke for the D2 ``tmux`` HostDriver (§5) — ``tmux_adapter.py``.
Mirrors ``headless_adapter_smoke.py``'s two-tier fake pattern:

  - a RECORDS-ONLY fake ``run_fn`` (no real tmux) to assert the exact
    ``tmux new-session`` argv/env content ``spawn()`` builds — the identity
    wiring (``AGENT_INSTANCE_ID``/``AGENT_SESSION_ID``/label) that
    ``backfill_registration`` depends on;
  - REAL tmux (this machine has 3.7b installed, per the D2 dispatch brief)
    to exercise ``spawn``/``alive``/``terminate``/``driver_channel`` against
    an actual detached tmux session, using a harmless ``sh``-script stand-in
    for ``claude_bin`` — never the real Claude Code CLI.

SKIPS (does not fail the gate — exits the dedicated SKIP code, 77, the
automake/Meson/CTest convention run_smokes.py reports distinctly from
pass/fail) on a machine with no ``tmux`` binary — the real-tmux tier is
genuinely environment-dependent (C5 universality: tmux is not part of
default macOS), unlike the records-only tier, which always runs.

Also covers ``emit_role_tag.sh`` (the shipped reference script,
``tmux_support/emit_role_tag.sh``) directly: RED-FIRST proof that the
wrapped/raw branches actually differ — the exact "raw OSC is swallowed
inside tmux" trap this driver is built to avoid.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/tmux_adapter_smoke.py
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    _WORKER_INJECTED_HOOK_FILENAMES,
)
from agent_messaging_plugin.session_hosts import HostCannotSpawnError  # noqa: E402
from agent_messaging_plugin.tmux_adapter import (  # noqa: E402
    _THIRD_PARTY_PROVIDER_ENV_MARKERS,
    DEFAULT_PASTE_STABLE_SAMPLES_REQUIRED,
    DEFAULT_PASTE_STABLE_TIMEOUT_SECONDS,
    TmuxHostDriver,
    _effective_spawn_env,
    _emit_role_tag_path,
    _needs_dev_channels_confirmation,
    _parse_tmux_version,
    _provider_ignores_dev_channels,
    _sanitize_session_name,
    _TmuxSendKeysDriverChannel,
)

_passed = 0
_failed: list[str] = []

# The automake/Meson/CTest SKIP_RETURN_CODE convention, matching
# run_smokes.py's own _SKIP_EXIT_CODE -- set when the real-tmux tier's two
# tests find no tmux binary and skip their live-driver checks, so this
# smoke's overall exit distinguishes "real tmux ran and passed" from "the
# real-tmux tier was never actually exercised." The records-only tier
# (24 of 26 tests) always runs regardless. Undeclared-dependency audit:
# workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
_SKIP_EXIT_CODE = 77
_tmux_tier_skipped = False


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeCompleted:
    # stdout defaults to a valid, current tmux version string -- most fake
    # run_fn callables in this file don't care about `-V` specifically, and
    # a bare _FakeCompleted() must not accidentally fail verify_config()'s
    # version check for THEM; tests that care about the version response
    # override stdout explicitly (test_verify_config_refuses_old_tmux_version).
    def __init__(self, returncode: int = 0, stdout: str = "tmux 3.7b\n", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _executable_stub(tmp_dir: Path, name: str = "fake-tmux") -> str:
    stub = tmp_dir / name
    # The "fake-tmux" stub answers -V with a real, current version string so
    # a default (real subprocess.run) run_fn's version check passes -- tests
    # that need a DIFFERENT version response inject their own run_fn instead.
    body = "#!/bin/sh\nif [ \"$1\" = '-V' ]; then echo 'tmux 3.7b'; fi\nexit 0\n"
    stub.write_text(body)
    stub.chmod(0o755)
    return str(stub)


def _stub_worker_hook_files(tmp_dir: Path) -> None:
    """R4 Package C (2026-08-10): populate rung 1 (``.claude/hooks/``) with
    a stub for every file the worker-hook resolution ladder requires --
    matching a real dev checkout's own shape (rung 1 always present), same
    helper as ``headless_adapter_smoke.py``'s own (duplicated rather than
    cross-imported between two independent test files, same reasoning as
    the production code's own duplicated helpers)."""
    hooks_dir = tmp_dir / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in _WORKER_INJECTED_HOOK_FILENAMES:
        (hooks_dir / name).write_text("#!/usr/bin/env python3\n")


def _configured_driver(
    tmp_dir: Path, *, run_fn: Any = None,
    confirm_timeout_seconds: float | None = None,
    confirm_poll_interval_seconds: float | None = None,
) -> TmuxHostDriver:
    mcp_config = tmp_dir / ".mcp.json"
    mcp_config.write_text("{}")
    _stub_worker_hook_files(tmp_dir)
    kwargs: dict[str, Any] = {
        "tmux_bin": _executable_stub(tmp_dir, "fake-tmux"),
        "claude_bin": _executable_stub(tmp_dir, "fake-claude"),
        "solet_name": "testhom",
        # Injected, never resolved from the ambient environment: the CLI/PATH
        # and presence-sidecar assertions would otherwise pass or fail on
        # whether the machine running the gate happens to have a `solet` on
        # PATH or beside its interpreter.
        "solet_bin": _executable_stub(tmp_dir, "fake-solet"),
        "permission_mode": "bypassPermissions",
        "mcp_config_path": mcp_config,
        "cwd": tmp_dir,
        # No real waiting in unit smokes -- the confirm-loop's own poll
        # cadence is exercised by the dedicated confirm-flow tests below,
        # not by every spawn-shape test that happens to go through it.
        "sleep_fn": lambda _seconds: None,
    }
    if run_fn is not None:
        kwargs["run_fn"] = run_fn
    if confirm_timeout_seconds is not None:
        kwargs["confirm_timeout_seconds"] = confirm_timeout_seconds
    if confirm_poll_interval_seconds is not None:
        kwargs["confirm_poll_interval_seconds"] = confirm_poll_interval_seconds
    return TmuxHostDriver(**kwargs)


def _check_wake_cli_and_path(cmd: list[str]) -> None:
    """Registration-loss fix (2026-08-14) assertions on the ``new-session -e``
    environment. Split out of ``test_spawn_command_and_env_wiring`` so that
    test stays under the radon cyclomatic-complexity gate."""
    wake_cli = next(
        (v.split("=", 1)[1] for v in cmd if v.startswith("AGENT_WAKE_CLI=")), "",
    )
    _check(
        Path(wake_cli).name == "fake-solet" and "testhom" not in wake_cli,
        "AGENT_WAKE_CLI is the wake-CLI EXECUTABLE, never the solet instance "
        "name -- `which <instance-name>` cannot resolve, so a value that "
        "tracked solet_name (e.g. 'testhom' here) silently broke every "
        "worker's idle-wake Stop hook; deaf-wake fix, 2026-08-08",
    )
    _check(
        Path(wake_cli).is_absolute(),
        "AGENT_WAKE_CLI is ABSOLUTE, not the bare name -- a tmux pane "
        "inherits the tmux SERVER's minimal PATH, under which a bare 'solet' "
        "is unresolvable, and both the Stop-hook waker and the PostToolUse "
        "heartbeat then died silently (FileNotFoundError, exit 0); "
        "registration-loss fix, 2026-08-14",
    )
    pane_path = next(
        (v.split("=", 1)[1] for v in cmd if v.startswith("PATH=")), "",
    )
    _check(
        pane_path.split(os.pathsep)[0] == str(Path(wake_cli).parent),
        "PATH crosses the `new-session -e` allowlist boundary with the CLI's "
        "directory first, so hooks and skills invoking a BARE `solet` resolve "
        "it too",
    )


def _check_presence_sidecar(pane_command: str) -> None:
    """On the watch transport the emit is followed by the BACKGROUNDED
    presence sidecar -- the step that actually puts this worker in
    ``peer_list`` -- and only then the exec."""
    _check(
        "fake-solet" in pane_command and " watch " in pane_command
        and "--no-claim" in pane_command,
        "the pane arms the --no-claim presence sidecar before exec",
    )
    _check(
        0 <= pane_command.find(" watch ") < pane_command.find("exec "),
        "the sidecar is backgrounded BEFORE exec replaces the pane's shell",
    )


def _is_send_enter(cmd: list[str]) -> bool:
    return "send-keys" in cmd and cmd[-1:] == ["Enter"] and "-l" not in cmd


def _confirm_flow_run_fn(calls: list[list[str]]) -> Any:
    """A fake ``run_fn`` that plays the dev-channels confirmation flow: the
    prompt shows on every ``capture-pane`` poll until the confirm loop's
    literal ``send-keys ... Enter`` is observed, then clears -- so every
    test using this fake exercises the SAME real state machine
    :meth:`TmuxHostDriver._confirm_dev_channels_prompt` drives, not a
    hand-tuned per-test schedule."""
    state = {"confirmed": False}

    def _fake(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        if "capture-pane" in cmd:
            if state["confirmed"]:
                return _FakeCompleted(stdout="TUI booted, channels active\n")
            return _FakeCompleted(
                stdout="WARNING: Loading development channels\n"
                "... Enter to confirm · Esc to cancel\n",
            )
        if _is_send_enter(cmd):
            state["confirmed"] = True
        return _FakeCompleted(returncode=0)

    return _fake


def test_parse_tmux_version() -> None:
    _check(_parse_tmux_version("tmux 3.7b") == (3, 7), "'tmux 3.7b' parses to (3, 7)")
    _check(_parse_tmux_version("tmux 3.3a") == (3, 3), "'tmux 3.3a' parses to (3, 3)")
    _check(_parse_tmux_version("garbage") is None, "unparseable version output returns None")


def test_sanitize_session_name() -> None:
    _check(
        _sanitize_session_name("fleet/lane mgmt:d2.test") == "fleet-lane-mgmt-d2-test",
        "'.', ':', '/', and spaces (tmux target-syntax separators / shell-unsafe "
        "chars) are all stripped from a session name",
    )
    _check(_sanitize_session_name("") == "session", "an empty name falls back to 'session'")


def test_verify_config_remedies_are_independent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        unconfigured = TmuxHostDriver(
            tmux_bin="/nonexistent/tmux", claude_bin="/nonexistent/claude",
            solet_name="", permission_mode="",
            mcp_config_path=tmp_dir / "missing.json", cwd=tmp_dir,
        )
        # transport="mcp" is what makes the MCP-config remedy reachable at
        # all (Dax Part 36 §36.3) -- see the dedicated watch/mcp legs below.
        remedies = unconfigured.verify_config(transport="mcp")
        _check(
            len(remedies) == 5,
            f"all 5 remedies fire when nothing is configured, mcp transport "
            f"(got {len(remedies)}: {remedies})",
        )
        _check_each_remedy_names_its_own_gap(remedies)
        configured = _configured_driver(tmp_dir)
        _check(
            configured.verify_config(transport="mcp") == [],
            "a fully-configured driver has zero remedies",
        )


def _check_each_remedy_names_its_own_gap(remedies: list[str]) -> None:
    """Split out of ``test_verify_config_remedies_are_independent`` to keep
    it under the radon cc threshold — one assertion per remedy instead of
    one `and`-chained mega-assertion."""
    _check(any("tmux" in r and "binary" in r for r in remedies), "the tmux-binary remedy names its gap")
    _check(any("claude" in r for r in remedies), "the claude-binary remedy names its gap")
    _check(any("SOLET_NAME" in r for r in remedies), "the SOLET_NAME remedy names its gap")
    _check(any("permission mode" in r for r in remedies), "the permission-mode remedy names its gap")
    _check(any("MCP config" in r for r in remedies), "the MCP-config remedy names its gap")


def test_verify_config_mcp_config_required_only_for_mcp_transport() -> None:
    """Regression guard for Dax Part 36 §36.3, ported to the tmux driver
    2026-08-10 (authorized scope addition — the identical unconditional check
    landed here too, and was fixed in ``headless_adapter`` by ``dc7c7c9bf``
    without this site). ``verify_config()`` used to require ``.mcp.json``
    unconditionally, even though ``_spawn_command()`` only ever reads
    ``self._mcp_config_path`` when the resolved transport is ``'mcp'`` -- a
    ``'watch'`` spawn passes an inline literal empty MCP config
    (``'{"mcpServers":{}}'``) and never touches the file. A born clone ships
    no ``.mcp.json`` at all, so every watch-transport tmux spawn (the charter
    default) refused for a file it was never going to read -- and tmux is the
    swap-durable host, so the durable substrate was exactly what went missing.

    FAILING MUTATION: drop the ``resolved_transport == "mcp"`` conjunct from
    ``_claude_launch_remedies`` (i.e. revert to the unconditional
    ``exists()`` check) -> the watch and bare legs both red.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = TmuxHostDriver(
            tmux_bin=_executable_stub(tmp_dir, "fake-tmux"),
            claude_bin=_executable_stub(tmp_dir, "fake-claude"),
            solet_name="testhom", permission_mode="bypassPermissions",
            mcp_config_path=tmp_dir / "missing.mcp.json",  # never created -- born-clone shape
            cwd=tmp_dir,
            run_fn=_confirm_flow_run_fn([]),
            sleep_fn=lambda _seconds: None,
        )
        _check(
            not (tmp_dir / "missing.mcp.json").exists(),
            "precondition: .mcp.json genuinely absent, born-clone shape",
        )
        _check(
            driver.verify_config(transport="watch") == [],
            "watch transport never reads .mcp.json -- verify_config must not "
            "refuse a tmux spawn for its absence",
        )
        _check(
            driver.verify_config() == [],
            "bare verify_config() with no transport arg resolves through the same "
            "floor spawn() would ('' -> charter default 'watch') and likewise does "
            "not require .mcp.json",
        )
        _check(
            any("MCP config" in r for r in driver.verify_config(transport="mcp")),
            "an MCP-reading transport still refuses when .mcp.json is genuinely absent",
        )


def test_spawn_watch_transport_succeeds_without_mcp_json_present() -> None:
    """End-to-end companion: ``spawn()`` itself -- not just ``verify_config()``
    in isolation -- must not refuse a watch-transport tmux worker for a missing
    ``.mcp.json``. This is the leg that proves the resolved transport actually
    REACHES the gate: ``spawn()`` resolves it once and threads it in, so a fix
    that only widened ``verify_config``'s signature without rewiring the call
    site would still red here.

    FAILING MUTATION: revert ``spawn()`` to calling ``verify_config`` without
    ``transport=`` -> the driver's own constructor floor is '' -> 'watch', so
    this specific leg would still pass; ALSO drop the ``== "mcp"`` conjunct and
    it reds. The stronger guard is the pair: this leg plus the mcp leg above.
    """
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _stub_worker_hook_files(tmp_dir)
        driver = TmuxHostDriver(
            tmux_bin=_executable_stub(tmp_dir, "fake-tmux"),
            claude_bin=_executable_stub(tmp_dir, "fake-claude"),
            solet_name="testhom", permission_mode="bypassPermissions",
            mcp_config_path=tmp_dir / "missing.mcp.json",  # never created
            cwd=tmp_dir,
            run_fn=_confirm_flow_run_fn(calls),
            sleep_fn=lambda _seconds: None,
        )
        session_name = driver.spawn(
            {"agent_instance_id": "agi-bornclone-1", "transport": "watch"},
        )
        _check(
            bool(session_name),
            "a watch-transport tmux spawn succeeds on a clone with no .mcp.json "
            "(pre-fix: host_cannot_spawn, 'no MCP config found at ...')",
        )
        new_session_call = next(c for c in calls if "new-session" in c)
        claude_argv = _extract_claude_argv_from_pane_command(new_session_call[-1])
        idx = claude_argv.index("--mcp-config")
        _check(
            claude_argv[idx + 1] == '{"mcpServers":{}}',
            "and it spawned with the inline empty MCP config -- confirming the file "
            "the old gate demanded is genuinely never read on this path",
        )
        _check(
            str(tmp_dir / "missing.mcp.json") not in " ".join(claude_argv),
            "the absent .mcp.json path appears nowhere in the spawn argv",
        )


def test_verify_config_refuses_old_tmux_version() -> None:
    """RED-FIRST: force an old-version report through a fake run_fn, confirm
    the remedy names the version gap, then confirm a current version clears it."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        def _old_version_run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
            if cmd[-1:] == ["-V"]:
                return _FakeCompleted(stdout="tmux 3.0a\n")
            return _FakeCompleted()

        driver = _configured_driver(tmp_dir, run_fn=_old_version_run_fn)
        remedies = driver.verify_config()
        _check(
            any("3.3" in r and "3.0a" in r for r in remedies),
            "RED: an old tmux version (3.0a, below the 3.3 allow-passthrough "
            "floor) is refused, naming both the found and required version",
        )

        def _current_version_run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
            if cmd[-1:] == ["-V"]:
                return _FakeCompleted(stdout="tmux 3.7b\n")
            return _FakeCompleted()

        driver2 = _configured_driver(tmp_dir, run_fn=_current_version_run_fn)
        _check(
            driver2.verify_config() == [],
            "GREEN: a current tmux version (3.7b) clears the version remedy",
        )


def test_spawn_refuses_when_unconfigured() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = TmuxHostDriver(
            tmux_bin="/nonexistent/tmux", claude_bin="/nonexistent/claude",
            solet_name="", permission_mode="",
            mcp_config_path=Path(tmp) / "missing.json", cwd=Path(tmp),
        )
        refused = False
        try:
            driver.spawn({"agent_instance_id": "agi-x"})
        except HostCannotSpawnError:
            refused = True
        _check(refused, "spawn() refuses (HostCannotSpawnError) before ever calling run_fn")


def test_spawn_refuses_without_agent_instance_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=lambda *a, **k: _FakeCompleted())
        refused = False
        try:
            driver.spawn({"lane_id": "lane-x"})
        except HostCannotSpawnError as exc:
            refused = True
            _check("agent_instance_id" in str(exc), "the refusal names the missing agent_instance_id")
        _check(refused, "spawn() with no agent_instance_id refuses even when otherwise configured")


def test_spawn_command_and_env_wiring() -> None:
    calls: list[list[str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        host_ref = driver.spawn(
            {
                "agent_instance_id": "agi-abc123ef", "lane_id": "lane-x",
                "model": "opus", "effort": "high",
            },
        )
        _check(
            host_ref == "fleet-lane-x-abc123ef",
            f"spawn() returns the sanitized session name as host_ref (got {host_ref!r})",
        )
        new_session_calls = [c for c in calls if "new-session" in c]
        _check(len(new_session_calls) == 1, "exactly one tmux new-session call")
        cmd = new_session_calls[0]
        env_str = " ".join(cmd)
        _check("AGENT_INSTANCE_ID=agi-abc123ef" in env_str, "env carries the ledger's agent_instance_id")
        _check(
            "AGENT_SESSION_ID=ases-agi-abc123ef" in env_str,
            "AGENT_SESSION_ID is derived from agent_instance_id",
        )
        _check("AGENT_SESSION_LABEL=lane-x" in env_str, "label prefers lane_id when given")
        _check("SOLET_NAME=testhom" in env_str, "SOLET_NAME flows from driver config")
        _check_wake_cli_and_path(cmd)
        _check(
            "FLEET_TRANSPORT=watch" in env_str,
            "an unspecified transport resolves to the charter's default 'watch' "
            "(fleet-watch-transport-migration phase 2 slice 1, 2026-08-06 -- "
            "non-MCP is now the fleet's PRIMARY transport, not 'mcp')",
        )
        pane_command = cmd[-1]
        _check(pane_command.startswith("sh "), "the tag emit runs BEFORE exec (pane command prefix)")
        _check("exec " in pane_command, "the pane command execs into claude after the emit")
        _check_presence_sidecar(pane_command)
        _check(
            "--model opus" in pane_command.replace("'", "") or "opus" in pane_command,
            "model override flows into the claude argv",
        )
        allow_passthrough_calls = [c for c in calls if "allow-passthrough" in c]
        _check(
            len(allow_passthrough_calls) == 1 and allow_passthrough_calls[0][-1] == "on",
            "allow-passthrough is set to 'on' exactly once, after session creation",
        )


def test_spawn_local_name_drives_label_session_name_and_claude_name() -> None:
    """W6 (#13 §44.3): a local_name replaces lane_id as the worker's label,
    and reaches all three places that matter — the tmux session name, the
    AGENT_SESSION_LABEL env var, and (Z-Q4 ruling) claude's own --name, which
    is what populates the file the Git-Controller mutation guard reads."""
    calls: list[list[str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        host_ref = driver.spawn(
            {
                "agent_instance_id": "agi-abc123ef", "lane_id": "lane-x",
                "local_name": "Git-Controller",
            },
        )
        _check(
            host_ref == "fleet-Git-Controller-abc123ef",
            f"the tmux session name derives from local_name, not lane_id (got {host_ref!r})",
        )
        cmd = next(c for c in calls if "new-session" in c)
        _check(
            "AGENT_SESSION_LABEL=Git-Controller" in " ".join(cmd),
            "AGENT_SESSION_LABEL carries the local_name",
        )
        pane_command = cmd[-1]
        _check(
            "--name Git-Controller" in pane_command.replace("'", ""),
            f"claude is launched with --name <local_name> -- without this the "
            f"guard reads an auto-derived name and blocks (got {pane_command!r})",
        )


def test_spawn_without_local_name_keeps_lane_id_behaviour() -> None:
    """The compatibility half: a caller that sends no local_name gets exactly
    the previous behaviour, so no existing lane's naming changes."""
    calls: list[list[str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        host_ref = driver.spawn(
            {"agent_instance_id": "agi-abc123ef", "lane_id": "lane-x"},
        )
        _check(
            host_ref == "fleet-lane-x-abc123ef",
            f"no local_name -> the session name still derives from lane_id (got {host_ref!r})",
        )
        pane_command = next(c for c in calls if "new-session" in c)[-1]
        _check(
            "--name lane-x" in pane_command.replace("'", ""),
            "and --name falls back to the lane_id label",
        )


def test_spawn_threads_allowed_tools_into_env() -> None:
    calls: list[list[str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        driver.spawn(
            {
                "agent_instance_id": "agi-allow1", "lane_id": "lane-allow",
                "allowed_tools": ["mcp__testhom__peer_register", "mcp__testhom__process_call"],
            },
        )
        new_session_call = next(c for c in calls if "new-session" in c)
        env_str = " ".join(new_session_call)
        _check(
            "FLEET_HEADLESS_TOOL_ALLOWLIST=mcp__testhom__peer_register,mcp__testhom__process_call" in env_str,
            "a provided allowed_tools list is joined into the gate env var verbatim",
        )


def test_spawn_transport_mcp_override_uses_real_mcp_config() -> None:
    """fleet-watch-transport-migration phase 2 slice 1 (2026-08-06): an
    explicit spec-level transport='mcp' gets the REAL --mcp-config path and
    --strict-mcp-config, never the watch-transport's explicit-empty one."""
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir, run_fn=_confirm_flow_run_fn(calls))
        driver.spawn({"agent_instance_id": "agi-mcp-1", "transport": "mcp"})
        new_session_call = next(c for c in calls if "new-session" in c)
        env_str = " ".join(new_session_call)
        _check("FLEET_TRANSPORT=mcp" in env_str, "an explicit transport='mcp' flows into the env var")
        pane_command = new_session_call[-1]
        claude_argv = _extract_claude_argv_from_pane_command(pane_command)
        idx = claude_argv.index("--mcp-config")
        _check(
            claude_argv[idx + 1] == str(tmp_dir / ".mcp.json"),
            "transport='mcp' gets the real --mcp-config file path, not the explicit-empty JSON",
        )
        _check(
            "--strict-mcp-config" in claude_argv,
            "--strict-mcp-config is present for the mcp transport too",
        )


def test_spawn_transport_watch_uses_explicit_empty_mcp_config() -> None:
    """The watch-transport counterpart: --mcp-config carries an EXPLICIT
    empty '{"mcpServers":{}}', matching the WS-6-verified precedent --
    never simply omitted."""
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        driver.spawn({"agent_instance_id": "agi-watch-1", "transport": "watch"})
        new_session_call = next(c for c in calls if "new-session" in c)
        env_str = " ".join(new_session_call)
        _check(
            "FLEET_TRANSPORT=watch" in env_str,
            "an explicit transport='watch' flows into the env var",
        )
        pane_command = new_session_call[-1]
        claude_argv = _extract_claude_argv_from_pane_command(pane_command)
        idx = claude_argv.index("--mcp-config")
        _check(
            claude_argv[idx + 1] == '{"mcpServers":{}}',
            "transport='watch' gets an EXPLICIT empty MCP config, not the real file "
            "path and not an omitted flag",
        )
        _check(
            "--strict-mcp-config" in claude_argv,
            "--strict-mcp-config is still present for the watch transport",
        )
        _check(
            "--dangerously-load-development-channels" in claude_argv,
            "dev-channel loading is orthogonal to MCP-vs-watch -- the watch "
            "transport does not by itself drop the flag (§39.1 gates it on the "
            "PROVIDER, never on the transport)",
        )


def _extract_claude_argv_from_pane_command(pane_command: str) -> list[str]:
    """The pane command is ``[emit_prefix] exec <claude argv...>`` (shell-
    joined via ``shlex.join`` at construction) -- recover the claude argv by
    splitting on the LAST ``exec `` (the emit prefix's own script path could
    theoretically contain the substring, so anchor on it being the argv's
    own leading token instead of a raw string split)."""
    tokens = shlex.split(pane_command)
    exec_idx = tokens.index("exec")
    return tokens[exec_idx + 1 :]


def test_spawn_settings_json_wires_all_three_hooks_and_the_deny_rule() -> None:
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        driver.spawn({"agent_instance_id": "agi-settings-1"})
        new_session_call = next(c for c in calls if "new-session" in c)
        pane_command = new_session_call[-1]
        claude_argv = _extract_claude_argv_from_pane_command(pane_command)
        settings_idx = claude_argv.index("--settings")
        settings_json = json.loads(claude_argv[settings_idx + 1])
        pretool_command = settings_json["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        _check(
            pretool_command.endswith("headless_tool_allowlist_gate.py"),
            "the tmux driver's --settings injects the PreToolUse allowlist gate hook",
        )
        session_start_command = settings_json["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        _check(
            session_start_command.endswith("capture_session_mapping.py"),
            "the tmux driver's --settings ALSO injects the T1 usage-capture "
            "SessionStart hook, merged alongside the PreToolUse gate",
        )
        post_tool_use_command = settings_json["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        _check(
            post_tool_use_command.endswith("heartbeat_report_alive.py"),
            "the tmux driver's --settings ALSO injects the T2 heartbeat "
            "PostToolUse hook, merged alongside the other two",
        )
        rotation_due_command = settings_json["hooks"]["PostToolUse"][0]["hooks"][1]["command"]
        _check(
            rotation_due_command.endswith("rotation_due_watch.py"),
            "the tmux driver's --settings ALSO injects the rotation-due watch "
            "PostToolUse hook (rotation-systematization P2 slice B), SECOND in the "
            "same PostToolUse group as the heartbeat hook, not replacing it",
        )
        _check(
            settings_json["permissions"]["deny"] == ["Agent", "Task", "AskUserQuestion"],
            "the tmux driver's --settings ALSO injects the Agent/Task/AskUserQuestion "
            "tool deny rule (capability-tier guardrail redesign, 2026-08-06; "
            "AskUserQuestion default-deny, operator ruling 2026-08-14), merged "
            "alongside the three hooks -- 'Agent' is the live-registry-confirmed "
            "name, 'Task' a defensive alias, AskUserQuestion denied by default "
            "because this driver launches a real interactive claude CLI where the "
            "tool's blocking picker can actually render",
        )


def test_spawn_allow_askuserquestion_omits_it_from_the_deny_list() -> None:
    """The per-spawn escape hatch (SpawnSessionRequest.allow_askuserquestion,
    mirrors the seed launcher's SOLET_ALLOW_ASKUSERQUESTION=1): Agent/Task
    stay denied unconditionally, only AskUserQuestion's presence in the deny
    list is conditional on this spec key."""
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        driver.spawn({"agent_instance_id": "agi-settings-2", "allow_askuserquestion": True})
        new_session_call = next(c for c in calls if "new-session" in c)
        pane_command = new_session_call[-1]
        claude_argv = _extract_claude_argv_from_pane_command(pane_command)
        settings_idx = claude_argv.index("--settings")
        settings_json = json.loads(claude_argv[settings_idx + 1])
        _check(
            settings_json["permissions"]["deny"] == ["Agent", "Task"],
            "allow_askuserquestion=True omits AskUserQuestion from the deny list "
            "while leaving the unconditional Agent/Task deny untouched",
        )


def test_spawn_env_session_mapping_spool_dir() -> None:
    """Same declared-not-derived contract as headless_adapter's own spool-dir
    env var: present and rooted under APP_HOME's data dir when APP_HOME is
    set, entirely ABSENT when it is unset."""
    calls: list[list[str]] = []
    orig = os.environ.get("APP_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        app_home = Path(tmp) / "profile"
        os.environ["APP_HOME"] = str(app_home)
        try:
            driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
            driver.spawn({"agent_instance_id": "agi-spool-tmux-1"})
        finally:
            if orig is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = orig
    new_session_call = next(c for c in calls if "new-session" in c)
    env_str = " ".join(new_session_call)
    expected = str(app_home / "data" / "session_claude_mapping_spool")
    _check(
        f"ANANTA_SESSION_MAPPING_SPOOL_DIR={expected}" in env_str,
        "ANANTA_SESSION_MAPPING_SPOOL_DIR is rooted under APP_HOME's data dir",
    )

    calls.clear()
    orig = os.environ.get("APP_HOME")
    os.environ.pop("APP_HOME", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
            driver.spawn({"agent_instance_id": "agi-spool-tmux-2"})
    finally:
        if orig is None:
            os.environ.pop("APP_HOME", None)
        else:
            os.environ["APP_HOME"] = orig
    new_session_call = next(c for c in calls if "new-session" in c)
    env_str = " ".join(new_session_call)
    _check(
        "ANANTA_SESSION_MAPPING_SPOOL_DIR" not in env_str,
        "APP_HOME unset -> the spool env var is ENTIRELY ABSENT from the tmux "
        "new-session -e pairs",
    )


def test_spawn_env_heartbeat_marker_dir() -> None:
    """Same declared-not-derived contract as the spool-dir env var, rooted
    under a SEPARATE APP_HOME subdirectory."""
    calls: list[list[str]] = []
    orig = os.environ.get("APP_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        app_home = Path(tmp) / "profile"
        os.environ["APP_HOME"] = str(app_home)
        try:
            driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
            driver.spawn({"agent_instance_id": "agi-hb-tmux-1"})
        finally:
            if orig is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = orig
    new_session_call = next(c for c in calls if "new-session" in c)
    env_str = " ".join(new_session_call)
    expected = str(app_home / "data" / "heartbeat_marker")
    _check(
        f"AGENT_HEARTBEAT_MARKER_DIR={expected}" in env_str,
        "AGENT_HEARTBEAT_MARKER_DIR is rooted under APP_HOME's data dir",
    )

    calls.clear()
    orig = os.environ.get("APP_HOME")
    os.environ.pop("APP_HOME", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
            driver.spawn({"agent_instance_id": "agi-hb-tmux-2"})
    finally:
        if orig is None:
            os.environ.pop("APP_HOME", None)
        else:
            os.environ["APP_HOME"] = orig
    new_session_call = next(c for c in calls if "new-session" in c)
    env_str = " ".join(new_session_call)
    _check(
        "AGENT_HEARTBEAT_MARKER_DIR" not in env_str,
        "APP_HOME unset -> the heartbeat marker dir env var is ENTIRELY ABSENT "
        "from the tmux new-session -e pairs",
    )


def test_spawn_wires_authority_system_prompt_with_full_substitution() -> None:
    """T2 authority-template: same full-substitution proof as the headless
    adapter's own test, through the tmux driver's pane-command argv."""
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        driver.spawn(
            {
                "agent_instance_id": "agi-authority-tmux-1",
                "lane_id": "fleet-authority-test",
                "role_class": "project",
                "brief_ref": "workbench/2026-08-04_x.md",
                "spawned_by_role": "Coordinator-Main",
            },
        )
        new_session_call = next(c for c in calls if "new-session" in c)
        pane_command = new_session_call[-1]
        claude_argv = _extract_claude_argv_from_pane_command(pane_command)
        idx = claude_argv.index("--append-system-prompt")
        rendered = claude_argv[idx + 1]
        _check("agi-authority-tmux-1" in rendered, "the rendered prompt carries agent_instance_id")
        _check("'project'" in rendered, "the rendered prompt carries role_class")
        _check("'fleet-authority-test'" in rendered, "the rendered prompt carries lane_id")
        _check("workbench/2026-08-04_x.md" in rendered, "the rendered prompt carries brief_ref")
        _check("'Coordinator-Main'" in rendered, "the rendered prompt carries spawned_by_role")
        _check("{role_class}" not in rendered, "no unresolved placeholder ships into the command")


def test_spawn_wraps_run_fn_failure() -> None:
    def _raiser(*_a: Any, **_k: Any) -> _FakeCompleted:
        raise OSError("no such file")

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_raiser)
        wrapped = False
        try:
            driver.spawn({"agent_instance_id": "agi-y"})
        except HostCannotSpawnError:
            wrapped = True
        _check(wrapped, "a run_fn OSError is wrapped as HostCannotSpawnError, never leaks raw")


def test_spawn_wraps_nonzero_exit() -> None:
    def _fail(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        if "new-session" in cmd:
            return _FakeCompleted(returncode=1, stderr="duplicate session: fleet-x")
        return _FakeCompleted()

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_fail)
        refused = False
        try:
            driver.spawn({"agent_instance_id": "agi-dup"})
        except HostCannotSpawnError as exc:
            refused = True
            _check("duplicate session" in str(exc), "a tmux new-session failure's stderr reaches the caller")
        _check(refused, "a non-zero tmux new-session exit raises HostCannotSpawnError")


def test_capability_report_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = _configured_driver(Path(tmp)).capability_report()
        _check(report.get("host") == "tmux", "capability_report names the host")
        _check(report.get("topology") == "detached-session", "capability_report names the topology")
        _check(
            "attach_hint" in report and "tmux" in str(report["attach_hint"]),
            "capability_report carries a human-usable attach hint",
        )


def test_driver_channel_enter_waits_for_baseline_change() -> None:
    """RED-FIRST (driver-channel strand fix, 2026-08-14, hermetically
    reproduced against the unmodified class — workbench/2026-08-14_driver_
    channel_strand_fix_report_lane_d.md): ``capture-pane`` returns the
    PRE-send screen until a TIME threshold (never gated on Enter, so neither
    the pre-fix nor the fixed algorithm's own control flow can move it),
    then the real post-render screen. Enter must only fire once the pane
    has actually re-rendered — never while it still shows the pre-send
    content the pre-fix code could mistake for "stable".

    FAILING MUTATION: drop the ``current != baseline`` conjunct from
    ``_wait_for_paste_stable`` (i.e. revert to the pre-fix comparison) — the
    unfixed code declares stability against
    ``DEFAULT_PASTE_STABLE_SAMPLES_REQUIRED`` identical pre-send samples
    well before the render threshold, and the assertion below reds (the
    pane content observed immediately before Enter is the pre-send screen,
    not the post-render one).
    """
    pre_screen = "> claude is thinking...\n(pre-send screen, busy event loop)"
    post_screen = "> claude is thinking...\n[Pasted text #1 +42 lines]"
    render_at_t = 2.0
    clock = {"t": 0.0}
    last_capture: dict[str, str | None] = {"value": None}
    last_capture_before_enter: dict[str, str | None] = {"value": None}

    def now_fn() -> float:
        return clock["t"]

    def sleep_fn(seconds: float) -> None:
        clock["t"] += seconds

    def run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        if "capture-pane" in cmd:
            content = post_screen if clock["t"] >= render_at_t else pre_screen
            last_capture["value"] = content
            return _FakeCompleted(stdout=content)
        if "send-keys" in cmd and cmd[-1:] == ["Enter"]:
            last_capture_before_enter["value"] = last_capture["value"]
        return _FakeCompleted()

    channel = _TmuxSendKeysDriverChannel(
        tmux_bin="tmux",
        session="repro-baseline-gate",
        run_fn=run_fn,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
        stable_samples_required=DEFAULT_PASTE_STABLE_SAMPLES_REQUIRED,
        stable_timeout_seconds=DEFAULT_PASTE_STABLE_TIMEOUT_SECONDS,
    )
    channel.send("a paste that renders slowly")
    _check(
        last_capture_before_enter["value"] == post_screen,
        "Enter is sent only once the pane shows the ACTUAL post-render "
        f"content, never the pre-send screen (observed: {last_capture_before_enter['value']!r})",
    )


def test_driver_channel_fails_open_when_baseline_never_changes() -> None:
    """Degenerate-case companion (brief item 2): a paste whose render
    leaves the visible pane UNCHANGED (e.g. output scrolled away) must
    still submit via the existing fail-open timeout — never hang waiting
    for a change that will never come. ``capture-pane`` returns the exact
    same content on every poll, so ``current != baseline`` never holds; the
    only way out is the pre-existing timeout path, unmodified by this fix.
    """
    constant_screen = "> claude is thinking...\n(never visibly changes)"
    clock = {"t": 0.0}
    calls: list[list[str]] = []

    def now_fn() -> float:
        return clock["t"]

    def sleep_fn(seconds: float) -> None:
        clock["t"] += seconds

    def run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        if "capture-pane" in cmd:
            return _FakeCompleted(stdout=constant_screen)
        return _FakeCompleted()

    channel = _TmuxSendKeysDriverChannel(
        tmux_bin="tmux",
        session="repro-baseline-unchanged",
        run_fn=run_fn,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
        stable_timeout_seconds=5.0,
        poll_interval_seconds=0.5,
    )
    channel.send("a paste that never visibly renders")
    enter_calls = [c for c in calls if "send-keys" in c and c[-1:] == ["Enter"]]
    _check(
        len(enter_calls) == 1,
        "Enter still fires exactly once via the fail-open timeout when the "
        "pane content never differs from the baseline",
    )
    _check(
        clock["t"] >= 5.0,
        f"the fail-open path was reached only after the full timeout elapsed (t={clock['t']:.2f}s)",
    )


def test_needs_dev_channels_confirmation_guard() -> None:
    """The no-flag path: an argv that never carries
    --dangerously-load-development-channels must never trigger the confirm
    loop -- the pure gate function is the single place that decision is
    made, so proving it here proves every non-dev-channels spawn stays
    exactly as untouched as before this fix (zero capture-pane polls, zero
    extra send-keys)."""
    _check(
        _needs_dev_channels_confirmation(
            ["claude", "--dangerously-load-development-channels", "server:example"],
        )
        is True,
        "an argv carrying the flag needs confirmation",
    )
    _check(
        _needs_dev_channels_confirmation(["claude", "--model", "opus"]) is False,
        "an argv without the flag skips the confirm loop entirely (no-flag path untouched)",
    )


_BEDROCK_MARKER = "CLAUDE_CODE_USE_BEDROCK"


@contextlib.contextmanager
def _effective_env_marker(value: str | None) -> Any:
    """Set (or SCRUB) the third-party-provider marker in this process's own
    environment for the duration of a leg, then restore it exactly.

    Scrubbing matters as much as setting: this smoke can run inside a spawned
    fleet worker, whose environment is whatever the daemon that spawned it
    carried. A leg asserting "no third-party behaviour" that merely INHERITED
    the ambient environment would be a negative control that never controlled
    anything -- it would pass on a clean box and silently invert on a
    Bedrock-configured one. ``value=None`` is the ``env -u`` equivalent.
    """
    previous = os.environ.get(_BEDROCK_MARKER)
    if value is None:
        os.environ.pop(_BEDROCK_MARKER, None)
    else:
        os.environ[_BEDROCK_MARKER] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_BEDROCK_MARKER, None)
        else:
            os.environ[_BEDROCK_MARKER] = previous


def _third_party_provider_run_fn(calls: list[list[str]]) -> Any:
    """A fake ``run_fn`` that plays what a third-party provider ACTUALLY does
    (adopter's live pane capture, §39.1): Claude Code reports the dev-channels
    flag as ignored and sits at a ready prompt -- the confirmation markers
    never appear, on any poll, ever. Under the pre-fix driver this is precisely
    the input that burned the full confirm timeout and then killed a
    fully-booted worker."""

    def _fake(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        if "capture-pane" in cmd:
            return _FakeCompleted(
                stdout=(
                    "--dangerously-load-development-channels ignored (server:testhom)\n"
                    "Channels are not available on third-party providers\n"
                    "> \n"
                ),
            )
        return _FakeCompleted(returncode=0)

    return _fake


def test_provider_ignores_dev_channels_predicate() -> None:
    """The pure predicate, §39.1/§40.1 -- adopter's shape, keyed on OUR
    effective spawn environment. Only the literal ``"1"`` counts: Claude Code's
    own switch is a 1/unset flag, and treating any truthy-looking string as
    enabled would omit the flag (and silently drop dev channels) on an
    Anthropic spawn that merely had the variable set to ``0``.

    FAILING MUTATION: return a constant ``False`` -> the first leg reds.
    Return a constant ``True`` -> every remaining leg reds.
    """
    _check(
        _provider_ignores_dev_channels({_BEDROCK_MARKER: "1"}) is True,
        "marker set to '1' -> the flag is inert, omit it",
    )
    _check(
        _provider_ignores_dev_channels({_BEDROCK_MARKER: " 1 "}) is True,
        "surrounding whitespace is stripped before comparison",
    )
    _check(
        _provider_ignores_dev_channels({}) is False,
        "no marker at all -> Anthropic path, keep the flag",
    )
    _check(
        _provider_ignores_dev_channels({_BEDROCK_MARKER: "0"}) is False,
        "marker explicitly disabled ('0') -> keep the flag",
    )
    _check(
        _provider_ignores_dev_channels({_BEDROCK_MARKER: ""}) is False,
        "empty marker -> keep the flag",
    )
    _check(
        _provider_ignores_dev_channels({_BEDROCK_MARKER: "true"}) is False,
        "only the literal '1' counts -- 'true' is not the vendor's switch value",
    )
    _check(
        _BEDROCK_MARKER in _THIRD_PARTY_PROVIDER_ENV_MARKERS,
        "the field-verified marker is the one the predicate actually reads",
    )


def test_effective_spawn_env_is_process_env_plus_optional_overlay() -> None:
    """The effective spawn environment is what the WORKER will receive, which
    today is this process's own environment (the driver injects only identity
    vars via ``new-session -e``). The ``provider_env`` overlay read is the seam
    the per-spawn provider-selection lane composes into -- inert until that
    lands, and asserted here so the seam cannot rot unnoticed."""
    with _effective_env_marker(None):
        _check(
            _effective_spawn_env({}).get(_BEDROCK_MARKER) is None,
            "with the marker scrubbed, the effective env carries no provider switch",
        )
        _check(
            _effective_spawn_env({"provider_env": {_BEDROCK_MARKER: "1"}}).get(
                _BEDROCK_MARKER,
            )
            == "1",
            "a per-spawn overlay reaches the effective env (forward-compat seam)",
        )
    with _effective_env_marker("1"):
        _check(
            _effective_spawn_env({"provider_env": {_BEDROCK_MARKER: "0"}}).get(
                _BEDROCK_MARKER,
            )
            == "0",
            "the per-spawn overlay WINS over the inherited daemon environment",
        )
    # Fast failure, not a silent fallback: a malformed overlay must never be
    # quietly ignored -- ignoring it would resolve to the daemon environment
    # and silently re-arm the confirm loop on a third-party spawn.
    raised = False
    try:
        _effective_spawn_env({"provider_env": "CLAUDE_CODE_USE_BEDROCK=1"})
    except TypeError:
        raised = True
    _check(raised, "a non-mapping provider_env fails loud rather than being ignored")


def test_third_party_provider_spawn_omits_flag_and_skips_confirm_loop() -> None:
    """RED-FIRST (§39.1/§40.1): with the marker set, the pre-fix driver appended
    the dev-channels flag unconditionally, entered the confirm loop, never saw a
    prompt the provider does not raise, and KILLED a fully-booted worker --
    ``spawn`` raised ``host_cannot_spawn`` every single time, making the
    swap-durable tmux fleet unspawnable on that provider. This asserts all three
    halves of the fix: flag omitted, loop never entered, spawn succeeds.

    FAILING MUTATION: revert ``_provider_ignores_dev_channels`` to a constant
    ``False`` -> the flag returns, the loop runs against a pane that never shows
    the prompt, and this whole leg reds with the original kill.
    """
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp, _effective_env_marker("1"):
        driver = _configured_driver(
            Path(tmp), run_fn=_third_party_provider_run_fn(calls),
        )
        session_name = driver.spawn(
            {"agent_instance_id": "agi-bedrock-1", "transport": "watch"},
        )
        _check(
            bool(session_name),
            "spawn RETURNS a session on a third-party provider (pre-fix: killed + raised)",
        )
        new_session_call = next(c for c in calls if "new-session" in c)
        claude_argv = _extract_claude_argv_from_pane_command(new_session_call[-1])
        _check(
            "--dangerously-load-development-channels" not in claude_argv,
            "the inert dev-channels flag is OMITTED when the provider ignores it",
        )
        _check(
            f"server:{'testhom'}" not in claude_argv,
            "the flag's server: argument goes with it -- no orphaned value left in argv",
        )
        _check(
            not any("capture-pane" in c for c in calls),
            "the expect loop is never ENTERED -- zero capture-pane polls, so zero "
            "chance of the assume-hung branch killing a ready worker",
        )
        _check(
            not any(_is_send_enter(c) for c in calls),
            "no confirmation Enter is sent for a prompt that never appears",
        )
        _check(
            not any("kill-session" in c for c in calls),
            "the booted worker is NOT killed",
        )


def test_anthropic_path_keeps_flag_and_confirm_loop_byte_for_byte() -> None:
    """The negative control for the leg above, with the marker explicitly
    SCRUBBED rather than merely assumed absent (this smoke may run inside a
    spawned worker whose environment is inherited, not clean).

    FAILING MUTATION: make ``_provider_ignores_dev_channels`` return a constant
    ``True`` -> the flag disappears from the Anthropic path and this leg reds.
    """
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp, _effective_env_marker(None):
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        driver.spawn({"agent_instance_id": "agi-anthropic-1", "transport": "watch"})
        new_session_call = next(c for c in calls if "new-session" in c)
        claude_argv = _extract_claude_argv_from_pane_command(new_session_call[-1])
        idx = claude_argv.index("--dangerously-load-development-channels")
        _check(
            claude_argv[idx + 1] == "server:testhom",
            "with no third-party marker the flag AND its server: argument are unchanged",
        )
        _check(
            any("capture-pane" in c for c in calls),
            "the confirm loop still runs on the Anthropic path",
        )
        _check(
            any(_is_send_enter(c) for c in calls),
            "the confirmation Enter is still sent on the Anthropic path",
        )


def test_spawn_confirms_dev_channels_prompt_before_returning() -> None:
    """RED-FIRST: before this fix, spawn() returned immediately once ``tmux
    new-session`` exited zero -- it never looked at the pane again, so a
    real claude sitting at the --dangerously-load-development-channels
    confirmation (D2 live-acceptance evidence, 2026-08-04 13:0xZ: no CLI
    bypass flag exists, one send-keys Enter clears it) would be reported as
    a successfully spawned session while actually hung forever. This
    fixture's fake pane shows the prompt on every poll until it observes
    the driver's own literal Enter -- a spawn() that doesn't poll/confirm
    would either hang against this fixture or (the pre-fix code) return a
    host_ref without ever having sent Enter at all."""
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        host_ref = driver.spawn({"agent_instance_id": "agi-confirm01", "lane_id": "lane-confirm"})
        _check(
            host_ref == "fleet-lane-confirm-onfirm01",
            f"spawn() returns the sanitized host_ref once the prompt is confirmed (got {host_ref!r})",
        )
        enter_indices = [i for i, c in enumerate(calls) if _is_send_enter(c)]
        _check(
            len(enter_indices) == 1,
            f"send-keys Enter is sent exactly once to clear the prompt (got {len(enter_indices)})",
        )
        enter_index = enter_indices[0]
        pre_enter_captures = sum(1 for i, c in enumerate(calls) if i < enter_index and "capture-pane" in c)
        post_enter_captures = sum(1 for i, c in enumerate(calls) if i > enter_index and "capture-pane" in c)
        _check(
            pre_enter_captures >= 1,
            f"capture-pane was polled to detect the prompt before Enter was sent (got {pre_enter_captures})",
        )
        _check(
            post_enter_captures >= 1,
            "capture-pane was polled again after Enter, to verify the TUI actually arrived "
            f"(got {post_enter_captures})",
        )


def test_spawn_kills_pane_and_fails_closed_when_prompt_never_appears() -> None:
    """RED-FIRST: a pane that never shows the confirmation prompt (e.g. a
    genuinely hung/crashed launch) must not be handed back as a live
    host_ref -- fail-closed means HostCannotSpawnError AND the half-alive
    tmux session gets killed, never left dangling for a caller to discover
    later via a silent hang."""
    calls: list[list[str]] = []

    def _fake(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        if "capture-pane" in cmd:
            return _FakeCompleted(stdout="")
        return _FakeCompleted(returncode=0)

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(
            Path(tmp), run_fn=_fake,
            confirm_timeout_seconds=0.05, confirm_poll_interval_seconds=0.01,
        )
        refused = False
        try:
            driver.spawn({"agent_instance_id": "agi-hang01", "lane_id": "lane-hang"})
        except HostCannotSpawnError as exc:
            refused = True
            _check(
                "confirmation prompt" in str(exc),
                f"the refusal names the confirmation-prompt timeout (got: {exc})",
            )
        _check(
            refused,
            "a confirmation prompt that never appears fails closed -- never returns a host_ref",
        )
        kill_calls = [c for c in calls if "kill-session" in c]
        _check(len(kill_calls) == 1, "the half-alive pane is killed before the error is raised")
        enter_calls = [c for c in calls if _is_send_enter(c)]
        _check(len(enter_calls) == 0, "Enter is never sent when the prompt itself never appeared")


def test_spawn_kills_pane_and_fails_closed_when_prompt_never_clears() -> None:
    """RED-FIRST companion: the prompt DOES appear (so Enter gets sent) but
    never clears -- a stuck confirmation, distinct from one that never
    showed up at all. Same fail-closed contract: kill the pane, raise,
    never return a host_ref for a pane still sitting at the prompt."""
    calls: list[list[str]] = []

    def _fake(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        if "capture-pane" in cmd:
            return _FakeCompleted(
                stdout="WARNING: Loading development channels\nEnter to confirm\n",
            )
        return _FakeCompleted(returncode=0)

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(
            Path(tmp), run_fn=_fake,
            confirm_timeout_seconds=0.05, confirm_poll_interval_seconds=0.01,
        )
        refused = False
        try:
            driver.spawn({"agent_instance_id": "agi-stuck01", "lane_id": "lane-stuck"})
        except HostCannotSpawnError as exc:
            refused = True
            _check(
                "confirmation prompt" in str(exc),
                f"the refusal names the still-showing confirmation prompt (got: {exc})",
            )
        _check(refused, "a prompt that never clears after Enter fails closed -- never returns a host_ref")
        kill_calls = [c for c in calls if "kill-session" in c]
        _check(len(kill_calls) == 1, "the half-alive pane is killed before the error is raised")
        enter_calls = [c for c in calls if _is_send_enter(c)]
        _check(
            len(enter_calls) == 1,
            "Enter WAS sent once (the prompt appeared) before the clear-check timed out",
        )


def test_emit_role_tag_wrapped_vs_raw() -> None:
    """RED-FIRST proof of the exact FINDINGS.md trap this whole driver
    exists to avoid: a raw OSC 1337 emitted inside tmux is swallowed, so the
    shipped script MUST choose the DCS-passthrough-wrapped form whenever
    $TMUX is set, and the two forms must be provably different (not the
    same bytes under a different label)."""
    script = _emit_role_tag_path()
    _check(script.exists(), f"the shipped emit_role_tag.sh reference impl exists at {script}")
    if not script.exists():
        return
    expected_b64 = base64.b64encode(b"probe-role").decode()

    wrapped = subprocess.run(  # noqa: S603
        ["sh", str(script), "probe-role"], capture_output=True, text=True,
        env={"TMUX": "/tmp/fake,0,0"},
    )
    _check(
        wrapped.stdout.startswith("\x1bPtmux;"),
        "RED-vs-GREEN: with $TMUX set, the script emits the DCS tmux-passthrough "
        "wrapper prefix (ESC P tmux;) -- the wrapped form",
    )
    _check(
        f"1337;SetUserVar=role={expected_b64}" in wrapped.stdout,
        "the wrapped form carries the correctly base64-encoded role label",
    )

    raw = subprocess.run(  # noqa: S603
        ["sh", str(script), "probe-role"], capture_output=True, text=True, env={},
    )
    _check(
        not raw.stdout.startswith("\x1bPtmux;"),
        "GREEN: with $TMUX unset, the script emits the RAW (unwrapped) form -- "
        "confirms the two branches are genuinely different, not the same output "
        "gated on a condition that never actually changes anything",
    )
    _check(
        f"1337;SetUserVar=role={expected_b64}" in raw.stdout,
        "the raw form still carries the correctly base64-encoded role label",
    )
    _check(wrapped.stdout != raw.stdout, "the wrapped and raw outputs are provably distinct byte sequences")


def _real_tmux_available() -> str | None:
    return shutil.which("tmux")


def test_real_tmux_spawn_alive_driver_channel_terminate() -> None:
    global _tmux_tier_skipped
    tmux_bin = _real_tmux_available()
    if tmux_bin is None:
        print("  SKIP  no 'tmux' binary on this machine -- real-tmux tier skipped, not failed")
        _tmux_tier_skipped = True
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        fake_claude = tmp_dir / "fake-claude"
        # Plays the real --dangerously-load-development-channels confirmation
        # flow (prompt text, wait for the driver's Enter, then a full
        # terminal reset standing in for the real TUI's alt-screen takeover)
        # so spawn()'s PTY-confirm expect loop has something real to drive,
        # then echoes exactly one line from stdin -- lets the test observe
        # what driver_channel().send() delivered AFTER confirmation.
        fake_claude.write_text(
            "#!/bin/sh\n"
            "printf 'WARNING: Loading development channels\\nEnter to confirm\\n'\n"
            "read confirm\n"
            "printf '\\033c'\n"
            "read line\n"
            "echo \"GOT:$line\"\n"
            "sleep 30\n",
        )
        fake_claude.chmod(0o755)
        (tmp_dir / ".mcp.json").write_text("{}")
        _stub_worker_hook_files(tmp_dir)
        driver = TmuxHostDriver(
            tmux_bin=tmux_bin, claude_bin=str(fake_claude), solet_name="testhom",
            permission_mode="default", mcp_config_path=tmp_dir / ".mcp.json", cwd=tmp_dir,
        )
        host_ref = driver.spawn({"agent_instance_id": "agi-realtmux01", "lane_id": "smoke-real"})
        try:
            deadline = time.monotonic() + 5
            while not driver.alive(host_ref) and time.monotonic() < deadline:
                time.sleep(0.1)
            _check(driver.alive(host_ref), "a freshly-spawned real tmux session reports alive=True")
            _check(driver.alive("no-such-session-xyz") is False, "a nonexistent session is never alive")

            channel = driver.driver_channel(host_ref)
            _check(channel is not None, "driver_channel() returns a live channel for an alive session")
            assert channel is not None
            channel.send("hello from the smoke")
            time.sleep(0.5)
            capture = subprocess.run(  # noqa: S603
                [tmux_bin, "capture-pane", "-t", host_ref, "-p"],
                capture_output=True, text=True, timeout=5,
            )
            _check(
                "GOT:hello from the smoke" in capture.stdout,
                "the pane's process received exactly the text send() delivered, "
                f"via literal keystrokes (captured: {capture.stdout!r})",
            )

            driver.terminate(host_ref, grace_seconds=2)
            time.sleep(0.3)
            _check(not driver.alive(host_ref), "terminate() leaves the tmux session dead")
            _check(
                driver.driver_channel(host_ref) is None,
                "driver_channel() is None once terminate() has killed the session",
            )
        finally:
            subprocess.run(  # noqa: S603
                [tmux_bin, "kill-session", "-t", host_ref], capture_output=True, text=True, timeout=5,
            )


def test_real_tmux_terminate_is_idempotent_on_already_dead_session() -> None:
    global _tmux_tier_skipped
    tmux_bin = _real_tmux_available()
    if tmux_bin is None:
        print("  SKIP  no 'tmux' binary on this machine -- real-tmux tier skipped, not failed")
        _tmux_tier_skipped = True
        return
    driver = TmuxHostDriver(tmux_bin=tmux_bin)
    raised = False
    try:
        driver.terminate("no-such-session-never-existed", grace_seconds=1)
    except Exception:  # noqa: BLE001 -- the exact thing under test: nothing escapes
        raised = True
    _check(not raised, "terminate() on an already-dead/nonexistent session is a no-op, never raises")


def main() -> int:
    # Provider-marker hygiene (§39.1/§40.1): every pre-existing leg below was
    # written when the dev-channels flag was unconditional, so each one
    # implicitly assumes an Anthropic-path spawn. This smoke can run inside a
    # SPAWNED fleet worker, which inherits the daemon's environment -- on a
    # third-party-configured box that inheritance would silently flip those
    # legs' meaning. Scrub the marker once for the whole process; the two legs
    # that genuinely care set and restore it themselves via
    # _effective_env_marker.
    for marker in _THIRD_PARTY_PROVIDER_ENV_MARKERS:
        os.environ.pop(marker, None)
    test_parse_tmux_version()
    test_sanitize_session_name()
    test_verify_config_remedies_are_independent()
    test_verify_config_mcp_config_required_only_for_mcp_transport()
    test_spawn_watch_transport_succeeds_without_mcp_json_present()
    test_verify_config_refuses_old_tmux_version()
    test_spawn_refuses_when_unconfigured()
    test_spawn_refuses_without_agent_instance_id()
    test_spawn_command_and_env_wiring()
    test_spawn_local_name_drives_label_session_name_and_claude_name()
    test_spawn_without_local_name_keeps_lane_id_behaviour()
    test_spawn_threads_allowed_tools_into_env()
    test_spawn_transport_mcp_override_uses_real_mcp_config()
    test_spawn_transport_watch_uses_explicit_empty_mcp_config()
    test_spawn_settings_json_wires_all_three_hooks_and_the_deny_rule()
    test_spawn_allow_askuserquestion_omits_it_from_the_deny_list()
    test_spawn_env_session_mapping_spool_dir()
    test_spawn_env_heartbeat_marker_dir()
    test_spawn_wires_authority_system_prompt_with_full_substitution()
    test_spawn_wraps_run_fn_failure()
    test_spawn_wraps_nonzero_exit()
    test_capability_report_shape()
    test_driver_channel_enter_waits_for_baseline_change()
    test_driver_channel_fails_open_when_baseline_never_changes()
    test_needs_dev_channels_confirmation_guard()
    test_provider_ignores_dev_channels_predicate()
    test_effective_spawn_env_is_process_env_plus_optional_overlay()
    test_third_party_provider_spawn_omits_flag_and_skips_confirm_loop()
    test_anthropic_path_keeps_flag_and_confirm_loop_byte_for_byte()
    test_spawn_confirms_dev_channels_prompt_before_returning()
    test_spawn_kills_pane_and_fails_closed_when_prompt_never_appears()
    test_spawn_kills_pane_and_fails_closed_when_prompt_never_clears()
    test_emit_role_tag_wrapped_vs_raw()
    test_real_tmux_spawn_alive_driver_channel_terminate()
    test_real_tmux_terminate_is_idempotent_on_already_dead_session()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    if _failed:
        return 1
    if _tmux_tier_skipped:
        print(
            "SKIP: no tmux binary on this machine -- the real-tmux tier "
            "disclosed a gap rather than running; the records-only tier "
            "(24 of 26 checks) ran and passed."
        )
        return _SKIP_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
