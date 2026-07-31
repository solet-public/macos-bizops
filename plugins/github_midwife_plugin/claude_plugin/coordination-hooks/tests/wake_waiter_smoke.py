#!/usr/bin/env python3
"""Behavioural proof of the wake waiter's two load-bearing security claims.

This is the plugin's only privileged hook -- the only one that executes
anything -- so it is where a reviewer's attention belongs, and it is the hook
whose SECURITY.md claims are hardest to confirm by reading alone:

1. **Exactly one bit.** "The waiter discards the child's stdout and stderr
   unread; on the wake signal it emits its own compiled-in fixed nudge." Proved
   by giving the stub CLI a payload of marker strings on both streams and
   asserting not one byte of it reaches the hook's output -- and that the nudge
   is byte-identical across stubs that emit wildly different things.

2. **It can never trap a session.** "A broken wake path degrades to 'messages
   wait for the next turn', never to a stuck session." Proved by exercising
   every non-wake exit path -- clean exit, arbitrary nonzero, death by signal,
   missing executable, non-executable file -- and asserting exit 0 each time.

The arming matrix is verified with a CONTROLLED environment: a stub that records
whether it ran, so a "disarmed" case proves the CLI was never invoked rather
than merely producing no output.

Run directly; exit 0 on success, non-zero on failure.
"""

from __future__ import annotations

import sys

# Must precede the _harness import — see manifest_consistency_smoke.py for why.
sys.dont_write_bytecode = True

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _harness import Results, preflight, run_hook  # noqa: E402

HOOK = "wake_waiter.js"
WAKE_SIGNAL = 2
LABEL = "Coordinator-Day"
MARKER_ENV = "STUB_MARKER"
NOTE_PREFIX = "[coordination-hooks wake]"

# Strings the stub writes to stdout and stderr. If the hook relayed child output
# in any form, one of these would surface in its own streams.
SECRET_STDOUT = "SENSITIVE-STDOUT-b3d1f0-message-body-should-never-appear"
SECRET_STDERR = "SENSITIVE-STDERR-9a72cc-message-body-should-never-appear"


def _stub(directory: Path, name: str, *, exit_code: int | None, chatty: bool = False) -> Path:
    """Write an executable stand-in for the operator's coordination CLI.

    exit_code None means "die from a signal", which makes Node's spawnSync
    report status null -- the path most likely to be mishandled.
    """
    lines = [
        "#!/usr/bin/env python3",
        "import os, signal, sys",
        f"marker = os.environ.get({MARKER_ENV!r})",
        "if marker:",
        "    open(marker, 'w', encoding='utf-8').write('ran')",
    ]
    if chatty:
        lines += [
            f"sys.stdout.write({SECRET_STDOUT!r})",
            f"sys.stderr.write({SECRET_STDERR!r})",
            "sys.stdout.flush()",
            "sys.stderr.flush()",
        ]
    if exit_code is None:
        lines.append("os.kill(os.getpid(), signal.SIGTERM)")
    else:
        lines.append(f"sys.exit({exit_code})")

    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _env(marker: Path, cli: Path | str | None, *, label: str | None = LABEL, transport: str | None = None) -> dict[str, str]:
    env = {MARKER_ENV: str(marker)}
    if label is not None:
        env["AGENT_SESSION_LABEL"] = label
    if cli is not None:
        env["AGENT_WAKE_CLI"] = str(cli)
    if transport is not None:
        env["FLEET_TRANSPORT"] = transport
    return env


def check_disarm_matrix(res: Results, work: Path) -> None:
    """Disarmed means the CLI is never invoked -- not merely that output is empty."""
    cli = _stub(work, "disarm_stub", exit_code=WAKE_SIGNAL)
    cases = (
        ("no session label", _env(work / "m1", cli, label=None)),
        ("no wake CLI", _env(work / "m2", None)),
        ("declared transport mcp", _env(work / "m3", cli, transport="mcp")),
        ("declared transport http", _env(work / "m4", cli, transport="http")),
    )
    for label, env in cases:
        marker = Path(env[MARKER_ENV])
        marker.unlink(missing_ok=True)
        proc = run_hook(HOOK, env=env)
        res.check(proc.returncode == 0, f"disarmed ({label}) exits 0", f"exit {proc.returncode}")
        res.check(proc.stdout == "", f"disarmed ({label}) writes no stdout", f"got {proc.stdout[:80]!r}")
        res.check(proc.stderr == "", f"disarmed ({label}) writes no stderr", f"got {proc.stderr[:80]!r}")
        res.check(not marker.exists(), f"disarmed ({label}) never spawns the CLI", "the stub ran")


def check_arm_matrix(res: Results, work: Path) -> None:
    cli = _stub(work, "arm_stub", exit_code=0)
    cases = (
        ("transport unset", None),
        ("transport watch", "watch"),
        # Documented divergence, not a ruling: an EMPTY FLEET_TRANSPORT arms the
        # hook, because `transport && transport !== "watch"` is falsy on "".
        # The rename skill reads ${FLEET_TRANSPORT:-mcp}, which resolves empty to
        # "mcp" and would DISarm. Recorded here so a change to either side
        # surfaces as a test diff rather than a silent behaviour drift.
        ("transport empty string", ""),
    )
    for label, transport in cases:
        marker = work / f"arm_{label.replace(' ', '_')}"
        marker.unlink(missing_ok=True)
        proc = run_hook(HOOK, env=_env(marker, cli, transport=transport))
        res.check(marker.exists(), f"armed ({label}) spawns the CLI", "the stub never ran")
        res.check(proc.returncode == 0, f"armed ({label}) passes through exit 0", f"exit {proc.returncode}")


def check_one_bit_claim(res: Results, work: Path) -> None:
    """The child's output must not reach the session by any route."""
    cli = _stub(work, "chatty_wake_stub", exit_code=WAKE_SIGNAL, chatty=True)
    marker = work / "chatty_marker"
    proc = run_hook(HOOK, env=_env(marker, cli))

    res.check(marker.exists(), "one-bit case actually spawned the CLI", "the stub never ran")
    res.check(proc.returncode == WAKE_SIGNAL, "wake signal is propagated as exit 2", f"exit {proc.returncode}")
    res.check(proc.stdout == "", "wake path writes nothing to stdout", f"got {proc.stdout[:80]!r}")

    combined = proc.stdout + proc.stderr
    for secret in (SECRET_STDOUT, SECRET_STDERR):
        res.check(secret not in combined, "child output never reaches the hook's streams", f"leaked {secret!r}")
    # Substring checks alone could miss a mangled relay, so also bound the size:
    # the nudge is a single fixed sentence, not a transcript.
    res.check(
        len(proc.stderr) < 400,
        "wake nudge is a short fixed message",
        f"stderr was {len(proc.stderr)} bytes",
    )
    res.check(
        proc.stderr.strip().count("\n") == 0,
        "wake nudge is a single line",
        f"got {proc.stderr!r}",
    )


def check_nudge_is_compiled_in(res: Results, work: Path) -> None:
    """Differential proof: two very different children yield the identical nudge."""
    quiet = _stub(work, "quiet_wake_stub", exit_code=WAKE_SIGNAL)
    chatty = _stub(work, "loud_wake_stub", exit_code=WAKE_SIGNAL, chatty=True)
    first = run_hook(HOOK, env=_env(work / "n1", quiet))
    second = run_hook(HOOK, env=_env(work / "n2", chatty))
    res.check(
        first.stderr == second.stderr and first.stderr != "",
        "the nudge is a compiled-in literal, independent of the child",
        f"{first.stderr[:80]!r} != {second.stderr[:80]!r}",
    )
    res.check(
        first.returncode == second.returncode == WAKE_SIGNAL,
        "both wake cases exit 2",
        f"{first.returncode} / {second.returncode}",
    )


def check_never_traps_the_session(res: Results, work: Path) -> None:
    """Every non-wake outcome must degrade to exit 0."""
    missing = work / "does_not_exist_at_all"
    not_executable = work / "not_executable"
    not_executable.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    not_executable.chmod(0o644)

    cases: tuple[tuple[str, Path, bool], ...] = (
        ("clean exit 0", _stub(work, "exit0_stub", exit_code=0), False),
        ("arbitrary nonzero", _stub(work, "exit7_stub", exit_code=7), True),
        ("killed by signal", _stub(work, "signal_stub", exit_code=None), True),
        ("executable missing", missing, True),
        ("file not executable", not_executable, True),
    )
    for label, cli, expect_note in cases:
        proc = run_hook(HOOK, env=_env(work / f"t_{label.replace(' ', '_')}", cli))
        res.check(proc.returncode == 0, f"non-wake outcome exits 0 ({label})", f"exit {proc.returncode}")
        res.check(proc.stdout == "", f"non-wake outcome writes no stdout ({label})", f"got {proc.stdout[:80]!r}")
        if expect_note:
            res.check(
                proc.stderr.startswith(NOTE_PREFIX),
                f"failure note is fixed-format ({label})",
                f"got {proc.stderr[:80]!r}",
            )
            res.check(
                proc.stderr.strip().count("\n") == 0,
                f"failure note is one line ({label})",
                f"got {proc.stderr!r}",
            )
        else:
            res.check(proc.stderr == "", f"clean exit is silent ({label})", f"got {proc.stderr[:80]!r}")

    seven = run_hook(HOOK, env=_env(work / "t_seven_status", _stub(work, "exit7b_stub", exit_code=7)))
    res.check("7" in seven.stderr, "the disclosed variable part is the numeric status", f"got {seven.stderr!r}")


def check_hook_writes_no_files(res: Results, work: Path) -> None:
    """SECURITY.md: 'No hook writes a file as an action of its own.'"""
    probe = work / "write_probe"
    probe.mkdir()
    cli = _stub(probe, "probe_stub", exit_code=WAKE_SIGNAL)
    marker = probe / "probe_marker"
    before = {path.name for path in probe.iterdir()}
    run_hook(HOOK, env=_env(marker, cli))
    after = {path.name for path in probe.iterdir()}
    created = after - before - {marker.name}
    res.check(not created, "the hook creates no files of its own", f"appeared: {sorted(created)}")


def main() -> int:
    preflight()
    res = Results("coordination-hooks — wake waiter")
    with tempfile.TemporaryDirectory(prefix="coordination-hooks-wake-") as raw:
        work = Path(raw)
        check_disarm_matrix(res, work)
        check_arm_matrix(res, work)
        check_one_bit_claim(res, work)
        check_nudge_is_compiled_in(res, work)
        check_never_traps_the_session(res, work)
        check_hook_writes_no_files(res, work)
    return res.finish()


if __name__ == "__main__":
    sys.exit(main())
