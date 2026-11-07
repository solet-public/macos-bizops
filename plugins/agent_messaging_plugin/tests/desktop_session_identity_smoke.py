#!/usr/bin/env python3
"""Env-less (desktop-app) Claude Code sessions get ONE durable fleet identity.

prb_a41724b2 / iss_776b9c77: a session started outside the fleet launchers
(the Claude desktop app, a bare ``claude``) used to get a throwaway instance
id per MCP-bridge start, a cwd label, and five silent hooks. The fix persists
one identity per ``CLAUDE_CODE_SESSION_ID`` in the solet runtime directory,
read env-first by the MCP bridge, the bridge CLI and the coordination hooks.

The headline case runs the same Claude session TWICE: once WITH the MCP bridge
(the bridge resolves and registers first) and once CLI ONLY (no bridge
identity is ever resolved). Both must yield the identical instance id, session
id and label on every surface: bridge registration, ``watch`` presence,
role claim, ``send-by-name`` attribution, ``inbox`` key, and the SessionStart
line. Adding or removing the MCP server therefore never changes who the
session is.

Also covered: each changed hook (both copies), resume reuse, a second session
getting a different identity, correction and retirement of the record, a
malformed record, the Codex-kind guard, read-only overlay, ownership
eligibility through the overlay, and launcher sessions left exactly as before.

Hermetic: XDG_RUNTIME_DIR and HOME point into a temporary directory, the
bridge is an ``httpx.MockTransport`` or a patched ``_post``, the clone root is
a temporary directory carrying a real-shaped receipt, and hook subprocesses
get a PATH with no ``solet-bridge`` on it (or a recording fake). Nothing reads
or writes the user's ``~/.claude``, ``~/.ananta`` or a live solet.

Run:
    .venv/bin/python plugins/agent_messaging_plugin/tests/desktop_session_identity_smoke.py
"""

from __future__ import annotations

# ruff: noqa: E402
import asyncio
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_HOOKS = _ROOT / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks"
_CHECKOUT_HOOKS = _ROOT / ".claude/hooks"
# The shipped plugin copies are always measured. The checkout copies exist only
# in the origin checkout (a seed carries no .claude/), so they are measured
# where present: the shipped behaviour is fully covered either way.
_HOOK_COPIES = tuple(copy for copy in (_CHECKOUT_HOOKS, _PLUGIN_HOOKS) if (copy / "session_start_context.py").is_file())
for _entry in (_ROOT / "plugins/agent_messaging_plugin/src", _ROOT / "ananta/src", _PLUGIN_HOOKS):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from click.testing import CliRunner

import agent_messaging_plugin.env_contract as env_contract
import agent_messaging_plugin.local_cli.cli as cli_mod
import agent_messaging_plugin.local_cli.client as client_mod
import agent_messaging_plugin.mcp_bridge.__main__ as bridge_mod
from agent_messaging_plugin.mcp_bridge.forwarder import Forwarder

hook_identity = importlib.import_module("_session_identity")
coordination_owner = importlib.import_module("coordination_owner")

SOLET = "deskcheck"
SID = "0e5ad1e0-1111-4222-8333-944445555666"
OTHER_SID = "7f00aa11-2222-4333-8444-955556666777"
ROLE = "Desk-Role"
Identity = tuple[str, str, str]


@dataclass(frozen=True)
class Sandbox:
    root: Path
    xdg: Path
    home: Path

    @property
    def records(self) -> Path:
        return self.xdg / "ananta" / env_contract.SESSION_IDENTITY_DIRNAME

    def record(self, sid: str = SID) -> Path:
        return self.records / f"{sid}.json"

    def env(self, sid: str = SID, **extra: str) -> dict[str, str]:
        """An env-less desktop session's environment: Claude's own vars only."""
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.xdg),
            "CLAUDE_CODE_SESSION_ID": sid,
            "CLAUDE_PROJECT_DIR": str(self.root),
            "PROJECT_SOLET_CLI": str(self.root / "no-psolet"),
            **extra,
        }


def _receipt(root: Path) -> dict[str, object]:
    """A receipt coordination_owner.verify accepts for the checkout hook copies."""
    app_home = root / "profile"
    files = {name: hashlib.sha256((_PLUGIN_HOOKS / name).read_bytes()).hexdigest() for name in ("coordination_owner.py", "heartbeat_report_alive.py", "rotation_due_watch.py", "wake_waiter.py")}
    manifest = _PLUGIN_HOOKS / "hooks.json"
    receipt: dict[str, object] = {
        "schema": "solet_coordination_installation_v1",
        "solet_name": SOLET,
        "plugin_selector": f"coordination-hooks@{SOLET}",
        "app_home": str(app_home),
        "default_hook_root": str(_PLUGIN_HOOKS),
        "surfaces": [
            {
                "kind": "checkout",
                "hook_root": str(_PLUGIN_HOOKS),
                "manifest_path": str(manifest),
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "interpreter": sys.executable,
                "reporter_generation": 5,
                "files": files,
            }
        ],
    }
    receipt["receipt_sha256"] = coordination_owner.receipt_sha256(receipt)
    return receipt


@contextmanager
def _sandbox() -> Generator[Sandbox]:
    with tempfile.TemporaryDirectory(prefix="desk-identity-") as raw:
        base = Path(raw).resolve()
        box = Sandbox(root=base / "clone", xdg=base / "xdg", home=base / "home")
        receipt_path = box.root / "profile/data/coordination-hooks/claude/installation.v1.json"
        receipt_path.parent.mkdir(parents=True)
        box.home.mkdir()
        receipt_path.write_text(json.dumps(_receipt(box.root)), encoding="utf-8")
        yield box


def _identity_of(record: Mapping[str, Any]) -> Identity:
    return (str(record["agent_instance_id"]), str(record["agent_session_id"]), str(record["session_label"]))


def _minted(sid: str = SID) -> Identity:
    return _identity_of(env_contract.mint_session_identity_record(sid, SOLET, "smoke"))


def _read_record(box: Sandbox, sid: str = SID) -> dict[str, Any]:
    value = json.loads(box.record(sid).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


# --- MCP-bridge surface --------------------------------------------------------


def _bridge_identity(env: Mapping[str, str], agent_id: str = "claude_code") -> bridge_mod.BridgeIdentity:
    bridge_env = {**env, "SOLET_NAME": SOLET, "AGENT_IDENTITY": agent_id}
    with patch.dict(os.environ, bridge_env, clear=True), patch.object(bridge_mod, "_log", lambda _m: None):
        return bridge_mod._resolve_bridge_identity(agent_id, SOLET)  # noqa: SLF001


def _bridge_register_body(identity: bridge_mod.BridgeIdentity) -> dict[str, Any]:
    """The peer/register body this bridge identity sends (its liveness record)."""
    forwarder = Forwarder(
        "http://127.0.0.1:1",
        SOLET,
        agent_id="claude_code",
        agent_instance_id=identity.agent_instance_id,
        agent_session_id=identity.agent_session_id,
        session_label=identity.session_label,
        parent_pid=4242,
        provides_inference=True,
        session_role=identity.session_role,
    )
    forwarder._bridge_id = "agc-desk"  # noqa: SLF001 -- bridge open is not under test
    bodies: list[dict[str, Any]] = []

    async def post(_path: str, body: dict[str, Any]) -> dict[str, Any]:
        bodies.append(body)
        return {"session_label": body["session_label"]}

    with patch.object(forwarder, "_post", post):
        asyncio.run(forwarder._register_identity())  # noqa: SLF001
    asyncio.run(forwarder._client.aclose())  # noqa: SLF001
    return bodies[0]


# --- CLI surface -----------------------------------------------------------------


def _inbox_page() -> dict[str, Any]:
    return {
        "recipient_agent_id": "claude_code",
        "recipient_agent_instance_id": "agi-desk",
        "entries": [],
        "next_after_created_at": None,
        "next_since_created_at": None,
        "next_since_cursor": None,
        "instance_exhausted": True,
        "role_entries": [],
        "next_role_cursor": None,
        "role_section_status": "ok",
        "role_section_error": None,
        "role_floor_applied": False,
        "role_history_cursor": None,
    }


def _bridge_handler(seen: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """A one-shot mock bridge recording every identity-bearing request."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            seen.setdefault("open", []).append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json={"bridge_id": "agc-desk", "session_id": "s"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        if path.endswith("/peer/send_by_name"):
            seen["send"] = json.loads(request.content)
            return httpx.Response(200, json={"message_id": "arm-desk", "delivery": "queued_for_replay", "thread_id": f"role:{ROLE}"})
        if path.endswith("/peer/inbox_for_session"):
            seen["inbox"] = dict(request.url.params)
            return httpx.Response(200, json=_inbox_page())
        if path.endswith("/peer/register"):
            seen.setdefault("register", []).append(json.loads(request.content))
            return httpx.Response(200, json={"status": "registered"})
        if path.endswith("/peer/claim_role"):
            seen["claim"] = json.loads(request.content)
            return httpx.Response(200, json={"name": ROLE, "action": "registered"})
        if "/events" in path:
            return httpx.Response(404, json={"detail": "bridge gone"})
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    return handler


@contextmanager
def _cli_context(env: Mapping[str, str], seen: dict[str, Any]) -> Generator[httpx.MockTransport]:
    transport = httpx.MockTransport(_bridge_handler(seen))

    def factory(base_url: str, **kw: Any) -> client_mod.BridgeClient:
        return client_mod.BridgeClient(base_url, transport=transport, **kw)

    with (
        patch.dict(os.environ, dict(env), clear=True),
        patch.object(cli_mod, "resolve_solet_name", lambda: SOLET),
        patch.object(cli_mod, "resolve_base_url", lambda name=None: "http://test"),
        patch.object(cli_mod, "BridgeClient", factory),
        patch.object(client_mod.time, "sleep", lambda _s: None),
    ):
        yield transport


def _arm(transport: httpx.MockTransport, identity: cli_mod.WatchIdentity, *, no_claim: bool) -> None:
    """One real watch arm against the mock bridge; the event stream then drops."""
    client = client_mod.BridgeClient("http://test", transport=transport)
    client.open()
    marks = Path(tempfile.mkdtemp()) / "session.marks"
    try:
        with patch.object(cli_mod.click, "echo", lambda *_a, **_k: None):
            cli_mod._arm_and_stream(client, identity, None, marks, no_claim=no_claim)  # noqa: SLF001
    except client_mod.BridgeCallError:
        pass  # a 404 on /events is how a gone bridge surfaces to the streamer
    finally:
        client.close()


def _cli_surfaces(box: Sandbox) -> dict[str, Any]:
    """send-by-name, inbox, presence-only watch and a CLI-only role claim."""
    seen: dict[str, Any] = {}
    report = box.root / "note.txt"
    report.write_text("hello\n", encoding="utf-8")
    with _cli_context(box.env(), seen) as transport:
        sent = CliRunner().invoke(cli_mod.cli, ["send-by-name", ROLE, "--content-file", str(report)], obj={})
        assert sent.exit_code == 0, sent.output
        read = CliRunner().invoke(cli_mod.cli, ["inbox", "--observer"], obj={})
        assert read.exit_code == 0, read.output
        presence = cli_mod._resolve_watch_identity(None, "claude_code")  # noqa: SLF001
        claimed = cli_mod._resolve_watch_identity(ROLE, "claude_code")  # noqa: SLF001
        _arm(transport, presence, no_claim=presence.presence_only)
        presence_register = seen["register"][-1]
        assert "claim" not in seen, "a recorded label must never be claimed as a role"
        _arm(transport, claimed, no_claim=claimed.presence_only)
    return {"seen": seen, "presence": presence, "claimed": claimed, "presence_register": presence_register}


# --- Hook surface ----------------------------------------------------------------


def _run_hook(path: Path, env: Mapping[str, str], payload: Mapping[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(path)],
        input=json.dumps(payload),
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _session_start_line(box: Sandbox, copy: Path, sid: str = SID) -> str:
    payload = {"session_id": sid, "source": "startup", "hook_event_name": "SessionStart"}
    done = _run_hook(copy / "session_start_context.py", box.env(sid), payload)
    assert done.returncode == 0, done.stderr
    context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
    assert isinstance(context, str)
    return context.splitlines()[0]


def _identity_in_line(line: str) -> Identity:
    fields = dict(part.split("=", 1) for part in line.split() if part.count("=") == 1)
    return fields["agent_instance_id"], fields["agent_session_id"], fields["label"].rstrip(".")


# --- the headline: with and without the MCP bridge ---------------------------------


def _mode_identities(mode: str) -> dict[str, Identity]:
    """Every surface's view of one desktop session, in one mode."""
    with _sandbox() as box:
        views: dict[str, Identity] = {}
        if mode == "mcp":
            bridge = _bridge_identity(box.env())
            body = _bridge_register_body(bridge)
            assert bridge.session_role == "", "a recorded label must never autobind a role"
            views["bridge_register"] = (body["agent_instance_id"], body["agent_session_id"], body["session_label"])
        cli = _cli_surfaces(box)
        seen = cli["seen"]
        presence = cli["presence_register"]
        views["watch_presence"] = (presence["agent_instance_id"], presence["agent_session_id"], presence["session_label"])
        claimed: cli_mod.WatchIdentity = cli["claimed"]
        assert seen["claim"] == {"name": ROLE, "takeover": False}, seen.get("claim")
        record = _identity_of(_read_record(box))
        views["claim_registration"] = (claimed.agent_instance_id, claimed.agent_session_id, record[2])
        views["send_attribution"] = (record[0], seen["open"][0]["caller_agent_session_id"], record[2])
        views["inbox_key"] = (record[0], seen["inbox"]["agent_session_id"], record[2])
        views["session_start_line"] = _identity_in_line(_session_start_line(box, _PLUGIN_HOOKS))
        views["record"] = record
        expected_writer = "mcp_bridge" if mode == "mcp" else "solet-bridge"
        assert _read_record(box)["created_by"] == expected_writer, _read_record(box)["created_by"]
        return views


def test_identity_identical_with_and_without_mcp_bridge() -> None:
    with_bridge = _mode_identities("mcp")
    cli_only = _mode_identities("cli_only")
    expected = env_contract.mint_session_identity_record(SID, SOLET, "x")
    want: Identity = (expected["agent_instance_id"], expected["agent_session_id"], expected["session_label"])
    for mode, views in (("mcp", with_bridge), ("cli_only", cli_only)):
        for surface, seen in views.items():
            assert seen == want, f"{mode}/{surface}: {seen} != {want}"
    assert "bridge_register" in with_bridge
    assert "bridge_register" not in cli_only


# --- each hook, both copies ---------------------------------------------------------


def test_session_start_line_names_identity_and_claim_path_in_both_copies() -> None:
    for copy in _HOOK_COPIES:
        with _sandbox() as box:
            line = _session_start_line(box, copy)
            assert line.startswith("Fleet identity (env-less session"), line
            assert "solet-bridge watch --role <Name>" in line, line
            assert _identity_in_line(line) == _minted(), line
            assert not box.records.exists(), "hooks only read; the bridge or the CLI writes the record"


def test_capture_session_mapping_spools_under_the_record_id_in_both_copies() -> None:
    for copy in _HOOK_COPIES:
        with _sandbox() as box:
            done = _run_hook(copy / "capture_session_mapping.py", box.env(), {"session_id": SID, "source": "startup"})
            assert done.returncode == 0, done.stderr
            spool = box.root / "profile/data/session_claude_mapping_spool"
            files = sorted(spool.glob("*.json"))
            assert len(files) == 1, (files, done.stderr)
            captured = json.loads(files[0].read_text(encoding="utf-8"))
            assert captured["agent_instance_id"] == _minted()[0]
            assert captured["claude_session_id"] == SID


def _fake_bridge(directory: Path, log: Path) -> Path:
    """A recording ``solet-bridge`` that answers the sentinel's receipt check."""
    script = directory / "solet-bridge"
    receipt = {"status": "completed", "result": {"success": True, "data": {"pass_id": "p-desk", "append_only": True}}}
    script.write_text(
        f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\nprint(json.dumps({receipt!r}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return directory


def test_session_cron_sentinel_reports_the_session_identity_read_only() -> None:
    for copy in _HOOK_COPIES:
        assert (copy / "session_cron_sentinel.py").read_bytes() == (_PLUGIN_HOOKS / "session_cron_sentinel.py").read_bytes()
    with _sandbox() as box:
        log = box.root / "bridge.log"
        bin_dir = _fake_bridge(box.root, log)
        env = box.env(PATH=f"{bin_dir}:/usr/bin:/bin")
        event = {"tool_name": "CronCreate", "session_id": SID, "tool_input": {"prompt": "p", "cron": "* * * * *"}}
        done = _run_hook(_PLUGIN_HOOKS / "session_cron_sentinel.py", env, event)
        assert done.returncode == 0, done.stderr
        assert "append-only receipt" in done.stderr, done.stderr
        argv = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        observation = json.loads(argv[2])["observation"]
        assert observation["agent_instance_id"] == _minted()[0], observation
        assert observation["session_id"] == SID
        assert not box.records.exists(), "the hook overlay is read-only"


def test_overlay_makes_the_context_gauge_owner_eligible() -> None:
    with _sandbox() as box:
        watch_hook = str(_PLUGIN_HOOKS / "rotation_due_watch.py")
        before = box.env()
        assert not coordination_owner.verify("context_watch", watch_hook, environ=before).managed
        env: dict[str, str] = box.env()
        identity = hook_identity.apply(watch_hook, environ=env)
        assert identity is not None
        result = coordination_owner.verify("context_watch", watch_hook, environ=env)
        assert result.eligible, result.diagnostic
        assert coordination_owner.runtime_identity_directory(result, env) is not None
        assert env["AGENT_INSTANCE_ID"] == _minted()[0]
        assert env["ANANTA_SESSION_MAPPING_SPOOL_DIR"] == str(box.root / "profile/data/session_claude_mapping_spool")


def test_rotation_and_heartbeat_hooks_exit_cleanly_env_less() -> None:
    with _sandbox() as box:
        payload = {"session_id": SID, "transcript_path": str(box.root / "absent.jsonl"), "tool_name": "Bash"}
        for name in ("rotation_due_watch.py", "heartbeat_report_alive.py"):
            done = _run_hook(_PLUGIN_HOOKS / name, box.env(), payload)
            assert done.returncode == 0, (name, done.stderr)
        heartbeat = _run_hook(_PLUGIN_HOOKS / "heartbeat_report_alive.py", box.env(), payload)
        assert heartbeat.stdout == "" and heartbeat.stderr == "", "heartbeat is unchanged: no ledger row exists for an env-less session"


# --- record lifecycle -------------------------------------------------------------


def _package_identity(box: Sandbox, sid: str = SID, warn: Callable[[str], None] | None = None) -> env_contract.SessionIdentity:
    resolved = env_contract.resolve_session_identity(box.env(sid), runtime_dir=box.xdg / "ananta", solet_name=SOLET, created_by="smoke", warn=warn)
    assert resolved is not None
    return resolved


def test_resume_reuses_and_a_new_session_gets_a_new_identity() -> None:
    with _sandbox() as box:
        first = _package_identity(box)
        stamp = box.record().read_bytes()
        resumed = _package_identity(box)
        assert resumed == first
        assert box.record().read_bytes() == stamp, "a resume must not rewrite the record"
        other = _package_identity(box, OTHER_SID)
        assert other.agent_instance_id != first.agent_instance_id
        assert other.agent_session_id != first.agent_session_id
        assert other.session_label != first.session_label


def test_correction_retirement_and_malformed_record() -> None:
    with _sandbox() as box:
        _package_identity(box)
        record = _read_record(box)
        record["session_label"] = "Desk-Corrected"
        box.record().write_text(json.dumps(record), encoding="utf-8")
        assert _package_identity(box).session_label == "Desk-Corrected"
        assert _bridge_identity(box.env()).session_label == "Desk-Corrected"
        with _cli_context(box.env(), {}):
            assert cli_mod._resolve_watch_identity(None, "claude_code").role == "Desk-Corrected"  # noqa: SLF001
        assert _identity_in_line(_session_start_line(box, _PLUGIN_HOOKS))[2] == "Desk-Corrected"
        box.record().unlink()
        assert _package_identity(box).session_label == f"claude-{SID[:8]}", "retire then re-mint"
        box.record().write_text("{not json", encoding="utf-8")
        warnings: list[str] = []
        assert _package_identity(box, warn=warnings.append).session_label == f"claude-{SID[:8]}"
        assert warnings and "malformed" in warnings[0], warnings


def test_package_and_hook_twins_derive_and_read_the_same_identity() -> None:
    for sid in (SID, OTHER_SID):
        assert _identity_of(hook_identity.minted(sid)) == _minted(sid)
    with _sandbox() as box:
        before = hook_identity.resolve(box.env())
        assert before is not None and not box.records.exists()
        written = _package_identity(box)
        after = hook_identity.resolve(box.env())
        assert after is not None
        assert (before.agent_instance_id, before.agent_session_id, before.session_label) == _minted()
        assert (after.agent_instance_id, after.agent_session_id, after.session_label) == (written.agent_instance_id, written.agent_session_id, written.session_label)
        assert after.path == written.path, "both twins name the same record path"


def test_codex_kind_never_adopts_a_claude_session_record() -> None:
    with _sandbox() as box:
        codex = _bridge_identity(box.env(), agent_id="codex")
        assert codex.agent_session_id == ""
        assert not box.records.exists()


# --- launcher sessions are unchanged ------------------------------------------------


def test_launcher_sessions_are_unchanged() -> None:
    launcher = {"AGENT_INSTANCE_ID": "agi-launcher", "AGENT_SESSION_ID": "ases-launcher", "AGENT_SESSION_LABEL": "Lane-X"}
    with _sandbox() as box:
        managed = _bridge_identity(box.env(**launcher))
        assert managed == bridge_mod.BridgeIdentity("agi-launcher", "ases-launcher", "Lane-X", "Lane-X")
        operator = _bridge_identity(box.env(AGENT_SESSION_ID="ases-op", AGENT_SESSION_LABEL="Op"))
        assert operator.agent_session_id == "ases-op" and operator.session_label == "Op"
        assert operator.agent_instance_id != env_contract.mint_session_identity_record(SID, SOLET, "x")["agent_instance_id"]
        with _cli_context(box.env(**launcher), {}):
            assert cli_mod._caller_agent_session_id() == "ases-launcher"  # noqa: SLF001
            watch = cli_mod._resolve_watch_identity(None, "claude_code")  # noqa: SLF001
            assert (watch.role, watch.agent_instance_id, watch.presence_only) == ("Lane-X", "agi-launcher", False)
        payload = {"session_id": SID, "source": "startup", "hook_event_name": "SessionStart"}
        done = _run_hook(_PLUGIN_HOOKS / "session_start_context.py", box.env(**launcher), payload)
        assert "Fleet identity" not in done.stdout, done.stdout
        assert hook_identity.apply(str(_PLUGIN_HOOKS / "rotation_due_watch.py"), environ=box.env(**launcher)) is None
        assert not box.records.exists(), "a launcher session must never read or write the record"


def main() -> None:
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        print(f"{failures}/{len(tests)} failed")
        sys.exit(1)
    print(f"all {len(tests)} passed")


if __name__ == "__main__":
    main()
