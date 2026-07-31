"""Hermetic smoke for the `homunculus wake` Stop-hook waker.

The wake command is the MCP-free turn-injection half of the watch/wake pair:
it blocks on the per-session spool `watch` tees deliveries into, surfaces new
lines on stderr, and exits 2 (the Claude Code hook wake code, valid for both
the asyncRewake background shape and the synchronous block-stop shape).
Everything here runs against temp files — no bridge, no network, no sleep.
"""

from __future__ import annotations

import fcntl
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from ananta.constants import ExitCodes
from click.testing import CliRunner, Result

import agent_messaging_plugin.local_cli.cli as cli_mod
import agent_messaging_plugin.local_cli.spool as spool_mod
import agent_messaging_plugin.local_cli.wake as wake_mod

_FLEET_ENV = {
    "AGENT_SESSION_LABEL": "Worker-A",
    "AGENT_SESSION_ID": "ases-1753200000-101-11111",
}
_BARE_ENV = {
    "AGENT_SESSION_LABEL": "",
    "AGENT_SESSION_ID": "",
}


def _invoke_wake(
    spool: Path, *, max_wait: float = 0.05, env: dict[str, str] | None = None,
) -> Result:
    with patch.object(wake_mod, "resolve_homunculus_name", lambda: "testling"):
        return CliRunner().invoke(
            wake_mod.wake,
            ["--spool", str(spool), "--max-wait", str(max_wait)],
            env=dict(env if env is not None else _FLEET_ENV),
            obj={},
        )


def _tmp_spool() -> Path:
    return Path(tempfile.mkdtemp(prefix="wake-smoke-")) / "testling.agi.spool"


def test_wake_is_a_no_op_outside_fleet_sessions() -> None:
    # The Stop hook is installed at USER scope, so it fires in plain
    # unlabeled sessions too — there it must exit 0 with zero output.
    spool = _tmp_spool()
    spool.write_text('{"watch": "event"}\n', encoding="utf-8")
    result = _invoke_wake(spool, env=_BARE_ENV)
    assert result.exit_code == 0, result.output
    assert not result.output
    assert not result.stderr


def test_wake_fires_on_pending_spool_content() -> None:
    # Lines already in the spool (delivered while no waker was armed) wake
    # immediately: exit 2, content + role on stderr, offset advanced.
    spool = _tmp_spool()
    line = json.dumps({"watch": "event", "event": {"content": "ping-A"}})
    spool.write_text(line + "\n", encoding="utf-8")
    result = _invoke_wake(spool)
    assert result.exit_code == wake_mod.WAKE_EXIT_SIGNAL, result.output
    assert "ping-A" in result.stderr
    assert "Worker-A" in result.stderr
    offset = spool_mod.spool_offset_path(spool)
    assert int(offset.read_text().strip()) == len(line) + 1


def test_wake_expires_idle_without_waking() -> None:
    # Fully-consumed spool + no new deliveries -> --max-wait expiry, exit 0.
    spool = _tmp_spool()
    line = '{"watch": "event"}\n'
    spool.write_text(line, encoding="utf-8")
    spool_mod.spool_offset_path(spool).write_text(f"{len(line)}\n")
    with patch.object(wake_mod, "WAKE_POLL_INTERVAL_S", 0.01):
        result = _invoke_wake(spool)
    assert result.exit_code == 0, result.output
    assert not result.stderr


def test_wake_ignores_trailing_partial_line() -> None:
    # A torn append (no trailing newline) must not wake with half a JSON
    # line; it waits for the line to complete.
    spool = _tmp_spool()
    spool.write_text('{"watch": "eve', encoding="utf-8")
    with patch.object(wake_mod, "WAKE_POLL_INTERVAL_S", 0.01):
        result = _invoke_wake(spool)
    assert result.exit_code == 0, result.output


def test_wake_singleton_yields_to_armed_waker() -> None:
    # Every turn's Stop spawns a waker; the flock collapses them to one so a
    # single delivery never produces N duplicate wakes.
    spool = _tmp_spool()
    spool.write_text('{"watch": "event"}\n', encoding="utf-8")
    lock = spool_mod.spool_lock_path(spool)
    with lock.open("w", encoding="utf-8") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _invoke_wake(spool)
    assert result.exit_code == 0, result.output
    assert not result.stderr


def test_wake_truncates_fully_consumed_oversized_spool() -> None:
    spool = _tmp_spool()
    content = ('{"watch": "event"}\n' * 200)
    spool.write_text(content, encoding="utf-8")
    spool_mod.spool_offset_path(spool).write_text(f"{len(content)}\n")
    with (
        patch.object(wake_mod, "WAKE_SPOOL_TRUNCATE_BYTES", 100),
        patch.object(wake_mod, "WAKE_POLL_INTERVAL_S", 0.01),
    ):
        result = _invoke_wake(spool)
    assert result.exit_code == 0, result.output
    assert spool.stat().st_size == 0
    assert int(spool_mod.spool_offset_path(spool).read_text().strip()) == 0


def test_wake_resurfaces_when_spool_shrank_below_offset() -> None:
    # Spool recreated shorter than the recorded offset (manual cleanup):
    # resurface from the start rather than silently skipping deliveries.
    spool = _tmp_spool()
    spool.write_text('{"watch": "event", "event": {"content": "re"}}\n')
    spool_mod.spool_offset_path(spool).write_text("9999\n")
    result = _invoke_wake(spool)
    assert result.exit_code == wake_mod.WAKE_EXIT_SIGNAL, result.output
    assert '"re"' in result.stderr


def test_wake_caps_surfaced_lines() -> None:
    spool = _tmp_spool()
    lines = [json.dumps({"watch": "event", "n": i}) for i in range(50)]
    spool.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with patch.object(wake_mod, "WAKE_MAX_SURFACED_LINES", 3):
        result = _invoke_wake(spool)
    assert result.exit_code == wake_mod.WAKE_EXIT_SIGNAL, result.output
    assert "(+47 more line(s)" in result.stderr


def test_wake_corrupt_offset_dies_loud_not_as_wake() -> None:
    # A corrupt offset sidecar must be a plain visible error — never exit 2,
    # which would impersonate a delivery wake.
    spool = _tmp_spool()
    spool.write_text('{"watch": "event"}\n', encoding="utf-8")
    spool_mod.spool_offset_path(spool).write_text("not-a-number\n")
    result = _invoke_wake(spool)
    assert result.exit_code not in (0, wake_mod.WAKE_EXIT_SIGNAL), result.output


def test_watch_spools_deliveries_but_not_armed_line() -> None:
    # The watch side of the contract: event + inbox lines are teed to the
    # spool for the waker; the armed line is not (re-arming is not a
    # delivery and must not wake anyone).
    spool = _tmp_spool()
    with patch.object(cli_mod.click, "echo", lambda _s, **_kw: None):
        cli_mod._emit_line({"watch": "armed", "role": "Worker-A"})
        cli_mod._emit_line({"watch": "inbox", "entry": {"text": "hi"}}, spool)
        cli_mod._emit_line({"watch": "event", "event": {"content": "yo"}}, spool)
    lines = spool.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"inbox"' in lines[0]
    assert '"event"' in lines[1]


def test_watch_and_wake_derive_the_same_spool_path() -> None:
    # The pairing contract: watch (writer) and wake (reader) must meet at the
    # SAME derived path with no flags — both key on the launcher session id.
    digest = spool_mod.watch_instance_digest(
        _FLEET_ENV["AGENT_SESSION_ID"],
    )
    instance_id = f"{cli_mod.WATCH_AGENT_INSTANCE_PREFIX}{digest}"
    path = spool_mod.default_spool_path("testling", instance_id)
    assert path.name == f"testling.{instance_id}.spool"
    assert instance_id.startswith("agi-watch-")


def test_wake_identity_error_is_not_a_wake_exit() -> None:
    # HomunculusIdentityError maps to UNKNOWN_ERROR (1) by design:
    # ExitCodes.CONNECTION_ERROR is 2, which the hook contract reads as a
    # wake — an identity failure must never impersonate one.
    def boom() -> str:
        raise cli_mod.HomunculusIdentityError("unreadable root_manifest")

    with patch.object(wake_mod, "resolve_homunculus_name", boom):
        result = CliRunner().invoke(
            wake_mod.wake, ["--max-wait", "0.05"], env=dict(_FLEET_ENV), obj={},
        )
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output
    assert result.exit_code != wake_mod.WAKE_EXIT_SIGNAL


def main() -> None:
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_")]
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
