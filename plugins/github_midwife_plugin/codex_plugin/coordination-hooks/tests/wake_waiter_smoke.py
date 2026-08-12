#!/usr/bin/env python3
"""Behavioral and source-level proof for the stock-Codex Stop waiter."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True

from _harness import (  # noqa: E402
    HOOKS_DIR,
    Results,
    base_env,
    preflight,
    run_hook,
)

HOOK = "wake_waiter.js"
WAKE_SIGNAL = 2
NOTE_PREFIX = "[coordination-hooks wake]"
NUDGE = (
    "New coordination deliveries are pending for this session; their durable "
    "contents remain unread in this turn."
)
MARKER_ENV = "STUB_MARKER"
SECRET_STDOUT = "SENSITIVE-STDOUT-message-body-must-not-reach-codex"
SECRET_STDERR = "SENSITIVE-STDERR-message-body-must-not-reach-codex"


def _payload(*, stop_hook_active: bool = False) -> str:
    return json.dumps(
        {
            "cwd": "/tmp/coordination-hooks-test",
            "hook_event_name": "Stop",
            "last_assistant_message": "done",
            "model": "test",
            "permission_mode": "default",
            "session_id": "codex-thread-id-not-a-homunculus-identity",
            "stop_hook_active": stop_hook_active,
            "transcript_path": None,
            "turn_id": "test-turn",
        }
    )


def _env(
    marker: Path,
    cli: Path | str | None,
    *,
    session_id: str | None = "ases-test-wake",
    transport: str | None = "watch",
) -> dict[str, str]:
    env = {MARKER_ENV: str(marker)}
    if cli is not None:
        env["AGENT_WAKE_CLI"] = str(cli)
    if session_id is not None:
        env["AGENT_SESSION_ID"] = session_id
    if transport is not None:
        env["FLEET_TRANSPORT"] = transport
    return env


def _stub(
    directory: Path,
    name: str,
    *,
    exit_code: int | None,
    chatty: bool = False,
    wait: bool = False,
) -> Path:
    lines = [
        "#!/usr/bin/env python3",
        "import json, os, signal, sys, time",
        f"marker = os.environ.get({MARKER_ENV!r})",
        "if marker:",
        "    with open(marker, 'w', encoding='utf-8') as handle:",
        "        json.dump({'argv': sys.argv[1:], 'pid': os.getpid()}, handle)",
    ]
    if chatty:
        lines.extend(
            (
                f"sys.stdout.write({SECRET_STDOUT!r})",
                f"sys.stderr.write({SECRET_STDERR!r})",
                "sys.stdout.flush()",
                "sys.stderr.flush()",
            )
        )
    if wait:
        lines.append("time.sleep(120)")
    elif exit_code is None:
        lines.append("os.kill(os.getpid(), signal.SIGTERM)")
    else:
        lines.append(f"sys.exit({exit_code})")
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _marker_payload(marker: Path) -> dict[str, object]:
    value = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"marker was not an object: {value!r}")
    return value


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_path(path: Path, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def _wait_for_exit(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(0.02)
    return not _is_alive(pid)


def _check_noop(
    res: Results,
    proc: subprocess.CompletedProcess[str],
    label: str,
    *,
    diagnostic: bool = False,
) -> None:
    res.check(proc.returncode == 0, f"{label} exits 0", f"exit {proc.returncode}")
    res.check(proc.stdout.strip() == "{}", f"{label} emits empty Stop JSON", repr(proc.stdout))
    if diagnostic:
        res.check(
            proc.stderr.startswith(NOTE_PREFIX),
            f"{label} emits a bounded fixed-format diagnostic",
            repr(proc.stderr),
        )
        res.check(
            proc.stderr.strip().count("\n") == 0,
            f"{label} diagnostic is one line",
            repr(proc.stderr),
        )
    else:
        res.check(proc.stderr == "", f"{label} is silent", repr(proc.stderr))


def check_disarm_and_loop_guard(res: Results, work: Path) -> None:
    cli = _stub(work, "disarm_stub", exit_code=WAKE_SIGNAL)
    cases = (
        ("missing session id", _env(work / "d1", cli, session_id=None)),
        ("missing wake CLI", _env(work / "d2", None)),
        ("MCP transport", _env(work / "d5", cli, transport="mcp")),
        ("unknown transport", _env(work / "d6", cli, transport="http")),
    )
    for label, env in cases:
        marker = Path(env[MARKER_ENV])
        proc = run_hook(HOOK, env=env, stdin=_payload())
        _check_noop(res, proc, label)
        res.check(not marker.exists(), f"{label} never spawns the CLI", "stub ran")

    loop_marker = work / "loop_guard"
    looped = run_hook(
        HOOK,
        env=_env(loop_marker, cli),
        stdin=_payload(stop_hook_active=True),
    )
    _check_noop(res, looped, "stop_hook_active loop guard")
    res.check(not loop_marker.exists(), "loop guard never spawns the CLI", "stub ran")


def check_arm_matrix(res: Results, work: Path) -> None:
    """RULED 2026-07-31 (Architect, claude_plugin's own arm-matrix comment): an
    unset or empty FLEET_TRANSPORT is EQUIVALENT TO UNSET -- empty is not a
    declaration -- so it ARMS. codex_plugin's wake_waiter.js originally diverged
    on purpose (comment: "prevents accidental double-wake while the patched MCP
    path is active") -- that rationale is retired BY THIS LANE'S OWN GOAL
    (patch-the-application is going away), so the divergence has no remaining
    justification and codex_plugin now follows the same ruling claude_plugin
    already pins."""
    cli = _stub(work, "arm_stub", exit_code=WAKE_SIGNAL)
    cases = (
        ("transport unset", None),
        ("transport watch", "watch"),
        ("transport empty string", ""),
    )
    for label, transport in cases:
        marker = work / f"arm_{label.replace(' ', '_')}"
        marker.unlink(missing_ok=True)
        env = _env(marker, cli, transport=transport)
        proc = run_hook(HOOK, env=env, stdin=_payload())
        res.check(marker.exists(), f"armed ({label}) spawns the CLI", "the stub never ran")
        res.check(proc.returncode == 0, f"armed ({label}) passes through exit 0", f"exit {proc.returncode}")


def check_malformed_input(res: Results, work: Path) -> None:
    cli = _stub(work, "malformed_stub", exit_code=WAKE_SIGNAL)
    for index, stdin in enumerate(("not-json", "[]", '{"hook_event_name":"Other"}')):
        marker = work / f"malformed_{index}"
        proc = run_hook(HOOK, env=_env(marker, cli), stdin=stdin)
        _check_noop(res, proc, f"malformed input {index}", diagnostic=True)
        res.check(not marker.exists(), f"malformed input {index} never spawns", "stub ran")


def check_wake_is_one_bit(res: Results, work: Path) -> None:
    marker = work / "wake_marker"
    cli = _stub(work, "chatty_wake_stub", exit_code=WAKE_SIGNAL, chatty=True)
    proc = run_hook(HOOK, env=_env(marker, cli), stdin=_payload())
    res.check(proc.returncode == 0, "wake returns structured success", f"exit {proc.returncode}")
    res.check(marker.exists(), "wake path actually spawned the CLI", "stub never ran")
    if marker.exists():
        res.check(
            _marker_payload(marker).get("argv") == ["wake", "--max-wait", "2400"],
            "wake CLI receives the exact fixed argv (bounded wait included)",
            repr(_marker_payload(marker)),
        )
    output = json.loads(proc.stdout)
    res.check(
        output == {"decision": "block", "reason": NUDGE},
        "wake emits only the fixed factual continuation nudge",
        repr(output),
    )
    combined = proc.stdout + proc.stderr
    for secret in (SECRET_STDOUT, SECRET_STDERR):
        res.check(secret not in combined, "child content never reaches Codex", f"leaked {secret}")
    res.check(proc.stderr == "", "successful wake emits no diagnostic", repr(proc.stderr))

    quiet = _stub(work, "quiet_wake_stub", exit_code=WAKE_SIGNAL)
    second = run_hook(HOOK, env=_env(work / "wake_quiet", quiet), stdin=_payload())
    res.check(
        second.stdout == proc.stdout,
        "different child content produces byte-identical hook output",
        f"{second.stdout!r} != {proc.stdout!r}",
    )


def check_nonwake_outcomes(res: Results, work: Path) -> None:
    cases: tuple[tuple[str, Path, bool], ...] = (
        ("idle expiry", _stub(work, "exit0_stub", exit_code=0), False),
        ("competing waker", _stub(work, "competing_stub", exit_code=0), False),
        ("unexpected status", _stub(work, "exit7_stub", exit_code=7), True),
        ("child killed by signal", _stub(work, "signal_stub", exit_code=None), True),
    )
    for label, cli, diagnostic in cases:
        proc = run_hook(HOOK, env=_env(work / label.replace(" ", "_"), cli), stdin=_payload())
        _check_noop(res, proc, label, diagnostic=diagnostic)

    missing = run_hook(
        HOOK,
        env=_env(work / "missing_marker", work / "does-not-exist"),
        stdin=_payload(),
    )
    _check_noop(res, missing, "missing executable", diagnostic=True)

    not_executable = work / "not_executable"
    not_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    not_executable.chmod(0o644)
    denied = run_hook(
        HOOK,
        env=_env(work / "denied_marker", not_executable),
        stdin=_payload(),
    )
    _check_noop(res, denied, "non-executable CLI", diagnostic=True)


def check_cancellation_reaps_child(res: Results, work: Path) -> None:
    marker = work / "blocking_marker"
    cli = _stub(work, "blocking_stub", exit_code=0, wait=True)
    env = base_env()
    env.update(_env(marker, cli))
    proc = subprocess.Popen(  # noqa: S603
        ["node", str(HOOKS_DIR / HOOK)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.write(_payload())
    proc.stdin.close()
    marker_seen = _wait_for_path(marker)
    res.check(marker_seen, "cancellation case starts the blocking child")
    child_pid = 0
    if marker_seen:
        raw_pid = _marker_payload(marker).get("pid")
        child_pid = raw_pid if isinstance(raw_pid, int) else 0
    res.check(child_pid > 0, "cancellation case records the child pid", repr(child_pid))

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        res.check(False, "cancelled hook exits promptly", "needed SIGKILL")
    else:
        res.check(proc.returncode == 0, "cancelled hook exits 0", f"exit {proc.returncode}")
    if child_pid > 0:
        res.check(_wait_for_exit(child_pid), "cancelled hook reaps its child", f"pid {child_pid} alive")


def check_bounded_wait(res: Results, work: Path) -> None:
    """The wait must be bounded: `--max-wait` always reaches the CLI.

    Ported 2026-08-12 from claude_plugin's wake_waiter_smoke bounded-wait
    cases. A valid AGENT_WAKE_MAX_WAIT_S override replaces the default; a
    malformed or non-positive override is ANNOUNCED on stderr and falls back
    to the default — never silently, and never passing raw environment text
    through to the argv."""
    cli = _stub(work, "bounded_stub", exit_code=0)
    cases = (
        ("valid override", "900", "900", False),
        ("malformed override", "soon", "2400", True),
        ("non-positive override", "0", "2400", True),
    )
    for label, override, expected, announces in cases:
        marker = work / f"bounded_{label.replace(' ', '_')}"
        marker.unlink(missing_ok=True)
        env = _env(marker, cli)
        env["AGENT_WAKE_MAX_WAIT_S"] = override
        proc = run_hook(HOOK, env=env, stdin=_payload())
        res.check(proc.returncode == 0, f"bounded wait ({label}): exit 0", f"exit {proc.returncode}")
        res.check(marker.exists(), f"bounded wait ({label}): CLI spawned", "the stub never ran")
        if marker.exists():
            res.check(
                _marker_payload(marker).get("argv") == ["wake", "--max-wait", expected],
                f"bounded wait ({label}): CLI argv is ['wake', '--max-wait', {expected!r}]",
                repr(_marker_payload(marker)),
            )
        res.check(
            (NOTE_PREFIX in proc.stderr) == announces,
            f"bounded wait ({label}): fallback is announced exactly when it happens",
            repr(proc.stderr),
        )


def check_source_pins_discard_contract(res: Results) -> None:
    source = (HOOKS_DIR / HOOK).read_text(encoding="utf-8")
    res.check(
        'spawn(cli, ["wake", "--max-wait", String(resolveMaxWaitS())]' in source,
        "source pins the fixed bounded wake argv",
    )
    res.check(
        'stdio: ["ignore", "ignore", "ignore"]' in source,
        "source pins all child streams to ignore",
    )
    res.check("shell: false" in source, "source explicitly disables shell execution")
    for token in ("child.stdout", "child.stderr", "result.stdout", "result.stderr"):
        res.check(token not in source, f"source never reads {token}")


def main() -> int:
    preflight()
    res = Results("coordination-hooks — stock-Codex wake waiter")
    with tempfile.TemporaryDirectory(prefix="codex-coordination-wake-") as raw:
        work = Path(raw)
        check_disarm_and_loop_guard(res, work)
        check_arm_matrix(res, work)
        check_malformed_input(res, work)
        check_bounded_wait(res, work)
        check_wake_is_one_bit(res, work)
        check_nonwake_outcomes(res, work)
        check_cancellation_reaps_child(res, work)
    check_source_pins_discard_contract(res)
    return res.finish()


if __name__ == "__main__":
    raise SystemExit(main())
