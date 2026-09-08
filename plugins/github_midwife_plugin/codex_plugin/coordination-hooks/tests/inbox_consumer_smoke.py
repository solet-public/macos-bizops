#!/usr/bin/env python3
"""Prove the stock-Codex Stop-hook inbox consumer (CDX-06 parts A and C):
gates on the fleet precondition, checks pending via a bounded `wake
--max-wait`, ALWAYS reports its own execution via `report_inbox_consumption`
regardless of outcome, decodes `decision:block` only when something was
found, and always exits 0 -- never a nonzero exit, even on a subprocess
failure (see the hook's own module docstring on why)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from _harness import Results, preflight, run_hook

_NUDGE = (
    "Your peer-message inbox has unread deliveries that arrived while this "
    "session was not looking. Call the registered peer_inbox process for "
    "your own agent_session_id (or run the coordination CLI's merged inbox "
    "read) before ending this turn, and act on what you find."
)


def _fake_cli(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

capture_path = Path(os.environ["REPORT_CAPTURE_PATH"])
drain_capture_path = Path(os.environ["DRAIN_CAPTURE_PATH"])

if sys.argv[1] == "wake":
    assert sys.argv[2] == "--max-wait"
    assert sys.argv[3] == "2400"
    raise SystemExit(int(os.environ.get("FAKE_WAKE_EXIT", "0")))

if sys.argv[1] == "inbox":
    drains = (
        json.loads(drain_capture_path.read_text(encoding="utf-8"))
        if drain_capture_path.exists()
        else []
    )
    drains.append(sys.argv[1:])
    drain_capture_path.write_text(json.dumps(drains), encoding="utf-8")
    raise SystemExit(int(os.environ.get("FAKE_INBOX_EXIT", "0")))

if sys.argv[1] == "call":
    process_key = sys.argv[2]
    parameters = json.loads(sys.argv[3])
    if process_key != "plugin::agent_messaging_plugin::report_inbox_consumption":
        raise SystemExit(f"unexpected process key: {process_key}")
    if os.environ.get("FAKE_REPORT_FAIL") == "1":
        print("simulated report failure", file=sys.stderr)
        raise SystemExit(1)
    captures = (
        json.loads(capture_path.read_text(encoding="utf-8"))
        if capture_path.exists()
        else []
    )
    captures.append(parameters)
    capture_path.write_text(json.dumps(captures), encoding="utf-8")
    print(json.dumps({
        "status": "completed",
        "result": {"success": True, "data": {"status": "recorded"}},
    }))
    raise SystemExit(0)

raise SystemExit(f"unexpected subcommand: {sys.argv[1:]}")
""",
        encoding="utf-8",
    )
    os.chmod(path, 0o700)


def _payload(*, event: str = "Stop") -> str:
    return json.dumps({"hook_event_name": event, "session_id": "thread-1"})


def _env(
    fake_cli: Path, capture: Path, *, wake_exit: int = 0, report_fail: bool = False,
    partial: bool = False, inbox_exit: int = 0,
) -> dict[str, str]:
    env = {
        "AGENT_SESSION_ID": "ases-test",
        "AGENT_WAKE_CLI": str(fake_cli),
        "REPORT_CAPTURE_PATH": str(capture),
        "DRAIN_CAPTURE_PATH": str(capture.with_name(f"{capture.stem}-drain.json")),
        "FAKE_WAKE_EXIT": str(wake_exit),
        "FAKE_INBOX_EXIT": str(inbox_exit),
    }
    if not partial:
        env["AGENT_INSTANCE_ID"] = "agi-test"
    if report_fail:
        env["FAKE_REPORT_FAIL"] = "1"
    return env


def _captured(capture: Path) -> list[dict[str, Any]]:
    if not capture.exists():
        return []
    value = json.loads(capture.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return value


def _drains(capture: Path) -> list[list[str]]:
    drain_capture = capture.with_name(f"{capture.stem}-drain.json")
    if not drain_capture.exists():
        return []
    value = json.loads(drain_capture.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return value


def main() -> int:
    preflight()
    res = Results("Codex peer-inbox consumer")
    with tempfile.TemporaryDirectory(prefix="codex-inbox-consumer-") as raw_tmp:
        tmp = Path(raw_tmp)
        fake_cli = tmp / "solet"
        _fake_cli(fake_cli)

        # 1. Unarmed: no fleet env at all -> inert, no subprocess calls.
        capture = tmp / "unarmed.json"
        proc = run_hook("inbox_consumer.py", env={}, stdin=_payload())
        res.check(proc.returncode == 0, "unarmed: exits 0", proc.stderr)
        res.check(proc.stdout.strip() == "{}", "unarmed: inert JSON output", proc.stdout)
        res.check(not capture.exists(), "unarmed: never calls the reporting verb")

        # 2. Partially armed (AGENT_INSTANCE_ID missing): inert, no calls, stderr note.
        capture = tmp / "partial.json"
        proc = run_hook(
            "inbox_consumer.py",
            env=_env(fake_cli, capture, partial=True),
            stdin=_payload(),
        )
        res.check(proc.returncode == 0, "partially armed: exits 0", proc.stderr)
        res.check(proc.stdout.strip() == "{}", "partially armed: inert JSON output", proc.stdout)
        res.check(not capture.exists(), "partially armed: never calls the reporting verb")
        res.check(
            "partially armed" in proc.stderr,
            "partially armed: stderr names the misconfiguration",
            proc.stderr,
        )

        # 3. Wrong hook event: inert, no calls.
        capture = tmp / "wrong_event.json"
        proc = run_hook(
            "inbox_consumer.py",
            env=_env(fake_cli, capture),
            stdin=_payload(event="PreToolUse"),
        )
        res.check(proc.returncode == 0, "wrong event: exits 0", proc.stderr)
        res.check(proc.stdout.strip() == "{}", "wrong event: inert JSON output", proc.stdout)
        res.check(not capture.exists(), "wrong event: never calls the reporting verb")

        # 4. Armed, nothing pending (wake exits 0): inert decision, but STILL
        #    reports the check (the honesty field's whole point).
        capture = tmp / "nothing_pending.json"
        proc = run_hook(
            "inbox_consumer.py",
            env=_env(fake_cli, capture, wake_exit=0),
            stdin=_payload(),
        )
        res.check(proc.returncode == 0, "nothing pending: exits 0", proc.stderr)
        res.check(proc.stdout.strip() == "{}", "nothing pending: no decision emitted", proc.stdout)
        res.check(not _drains(capture), "nothing pending: does not drain a durable inbox")
        res.check(len(_captured(capture)) == 1, "nothing pending: still reports the check ran")
        if _captured(capture):
            params = _captured(capture)[0]
            res.check(params.get("agent_instance_id") == "agi-test", "report carries agent_instance_id")
            res.check(params.get("agent_session_id") == "ases-test", "report carries agent_session_id")
            res.check(params.get("runtime") == "codex", "report carries runtime=codex")
            res.check(bool(params.get("checked_at")), "report carries a non-empty checked_at")
            res.check(
                "pending_found_at" not in params,
                "nothing pending: no pending_found_at is fabricated",
            )
            res.check(
                "pending_reason" not in params,
                "nothing pending: no pending_reason is fabricated",
            )
            res.check(
                params.get("reporter_surface") in {
                    "checkout", "plugin_cache", "vendored", "release", "unknown",
                },
                "report carries a closed-vocabulary reporter_surface",
                repr(params.get("reporter_surface")),
            )

        # 5. Armed, pending found (wake exits 2): decision:block, and the
        #    report carries the SAME nudge as pending_reason.
        capture = tmp / "pending.json"
        proc = run_hook(
            "inbox_consumer.py",
            env=_env(fake_cli, capture, wake_exit=2),
            stdin=_payload(),
        )
        res.check(proc.returncode == 0, "pending found: exits 0", proc.stderr)
        decision = json.loads(proc.stdout)
        res.check(decision.get("decision") == "block", "pending found: decision is block", proc.stdout)
        res.check(decision.get("reason") == _NUDGE, "pending found: reason is the fixed nudge", proc.stdout)
        res.check(
            _drains(capture) == [["inbox"]],
            "pending found: park drains both inbox sections to the CLI frontier",
            repr(_drains(capture)),
        )
        res.check(len(_captured(capture)) == 1, "pending found: reports the check")
        if _captured(capture):
            params = _captured(capture)[0]
            res.check(
                params.get("pending_found_at") == params.get("checked_at"),
                "pending found: pending_found_at matches this check's own checked_at",
            )
            res.check(
                params.get("pending_reason") == _NUDGE,
                "pending found: pending_reason is the SAME text as the emitted decision reason",
            )

        # 6. A partial/failing CLI drain is surfaced instead of being presented
        #    as completed. The fixed decision still continues the turn so the
        #    message cannot strand behind an operational CLI failure.
        capture = tmp / "partial_drain.json"
        proc = run_hook(
            "inbox_consumer.py",
            env=_env(fake_cli, capture, wake_exit=2, inbox_exit=1),
            stdin=_payload(),
        )
        res.check(proc.returncode == 0, "partial drain: exits 0", proc.stderr)
        decision = json.loads(proc.stdout)
        res.check(decision.get("decision") == "block", "partial drain: decision remains block")
        res.check(_drains(capture) == [["inbox"]], "partial drain: CLI was attempted")
        res.check(
            "parked inbox drain did not complete: exit 1" in proc.stderr,
            "partial drain: incomplete frontier is named on stderr",
            proc.stderr,
        )

        # 7. wake exits something other than 0/2: treated as not-pending, but
        #    the failure is still surfaced on stderr and the check is still
        #    reported (never silently swallowed into a clean run).
        capture = tmp / "wake_error.json"
        proc = run_hook(
            "inbox_consumer.py",
            env=_env(fake_cli, capture, wake_exit=1),
            stdin=_payload(),
        )
        res.check(proc.returncode == 0, "wake error: exits 0 (never traps the session)", proc.stderr)
        res.check(proc.stdout.strip() == "{}", "wake error: degrades to no decision", proc.stdout)
        res.check("exited with status 1" in proc.stderr, "wake error: stderr names the exit status", proc.stderr)
        res.check(len(_captured(capture)) == 1, "wake error: still reports the check ran")

        # 8. report_inbox_consumption itself fails: the hook's OWN decision
        #    (derived from wake, not from the report call) still reaches
        #    stdout untouched, and the hook still exits 0.
        capture = tmp / "report_fails.json"
        proc = run_hook(
            "inbox_consumer.py",
            env=_env(fake_cli, capture, wake_exit=2, report_fail=True),
            stdin=_payload(),
        )
        res.check(proc.returncode == 0, "report failure: still exits 0", proc.stderr)
        decision = json.loads(proc.stdout)
        res.check(
            decision.get("decision") == "block",
            "report failure: the hook's own decision is unaffected by a failed report",
            proc.stdout,
        )
        res.check(
            "report_inbox_consumption call failed" in proc.stderr,
            "report failure: stderr names the failed report call",
            proc.stderr,
        )
        res.check(not capture.exists(), "report failure: the fake CLI never wrote a capture")

    return res.finish()


if __name__ == "__main__":
    raise SystemExit(main())
