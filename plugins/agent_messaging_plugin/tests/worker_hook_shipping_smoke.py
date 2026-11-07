#!/usr/bin/env python3
"""Structural drift smoke for the R4 Package C worker-hook resolution
ladder (2026-08-10) — ``headless_adapter._WORKER_INJECTED_HOOK_FILENAMES``
is the single declared list both host adapters resolve against (rung 1:
the origin checkout's own ``.claude/hooks/``; rung 2: this plugin's own
shipped ``coordination-hooks/hooks/`` fallback, see
``headless_adapter._resolve_worker_hook_path``'s own docstring).

This smoke is the guard against the exact defect the ladder itself was
built to fix, recurring in a NEW shape: a future adapter change that
starts injecting a hook filename into a spawned worker's generated
``--settings`` without also vendoring that file into this plugin's
shipped fallback rung. A missing rung-2 file would pass every existing
adapter smoke (which runs from wherever it happens to be invoked) while
silently bricking every worker spawned from a born clone — exactly the
class of gap `manifest_consistency_smoke.py`'s own header epigraph
describes ("the failure mode ... has already happened twice"). This file
is that same discipline applied to the adapter side of the ladder, not
the plugin-document side.

R4 gate-fix (2026-08-10, coordinator-seat un-park dispatch): the first version of
this smoke hard-asserted rung 1's own presence (``REPO_ROOT/.claude/hooks``)
as if it were a universal truth. It isn't — it's a DEV-CHECKOUT-ONLY
truth. A born clone (the seed's own gate target) ships NO
``.claude/hooks/`` at all, correctly, by design; asserting its presence
there is not catching drift, it's asserting the wrong invariant against
the exact tree this smoke exists to protect, and it blocked the born-clone
gate on the re-mint. Fixed: every rung-1-dependent assertion is now
SHAPE-AWARE, keyed on the MEASURED tree shape
(``(REPO_ROOT/".claude"/"hooks").is_dir()``) — never an environment
variable, which could disagree with what's actually on disk. Two
directions, unconditionally checked either way:

  - direction 1 (the born-clone guarantee): every declared filename
    resolves in the plugin's shipped hooks/ dir (rung 2) — unconditional,
    every tree shape, never weakened.
  - direction 2 (the dev-checkout mirror-drift guard): every declared
    filename ALSO exists in the checkout's own ``.claude/hooks/`` (rung 1)
    — only meaningful, and only asserted, when rung 1 is actually present
    in this tree; a born clone prints an explicit N/A classification line
    instead of silently shrinking the check count.

Plus an end-to-end resolution check against the real repo root, itself
shape-aware: a dev checkout must resolve every filename to its rung-1
copy (rung 1 always wins when present); a born clone must resolve every
filename to its rung-2 copy instead (the SAME born-clone guarantee,
restated at the resolution layer rather than the bare-file-presence
layer). Every assertion states which shape it ran under, in its own
label, so a failure is never ambiguous about which tree produced it.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/worker_hook_shipping_smoke.py
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    _PLUGIN_HOOKS_DIR_RELATIVE,
    _WORKER_INJECTED_HOOK_FILENAMES,
    HeadlessHostDriver,
    _resolve_worker_hook_path,
)
from agent_messaging_plugin.tmux_adapter import _tmux_hook_settings_json  # noqa: E402

_passed = 0
_failed: list[str] = []

_CHECKOUT_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
_PLUGIN_HOOKS_DIR = REPO_ROOT / _PLUGIN_HOOKS_DIR_RELATIVE


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _rung1_present() -> bool:
    """The measured tree shape this whole smoke keys on -- never an env
    var, which could disagree with what's actually on disk."""
    return _CHECKOUT_HOOKS_DIR.is_dir()


def test_declared_list_is_non_empty() -> None:
    """A silently-emptied list would make every check below vacuously
    pass -- the population is asserted before anything is concluded from it."""
    _check(
        bool(_WORKER_INJECTED_HOOK_FILENAMES),
        "the declared worker-injected hook filename list is non-empty",
    )


def test_every_declared_filename_ships_in_the_plugin_fallback_rung() -> None:
    """The born-clone guarantee, direction 1: a spawned worker with NO
    ``.claude/hooks/`` at all must still find every one of these files at
    rung 2. Unconditional -- every tree shape, never weakened, never
    gated on rung 1's presence."""
    for name in _WORKER_INJECTED_HOOK_FILENAMES:
        path = _PLUGIN_HOOKS_DIR / name
        _check(
            path.is_file(),
            f"{name} ships in the plugin's shipped hooks/ dir (rung 2): {path}",
        )


def test_every_declared_filename_exists_in_the_checkout_origin_rung() -> None:
    """The mirror-image drift, direction 2: a filename added to the
    declared list without ever having existed as a real checkout hook at
    all. Shape-aware: this leg is a DEV-CHECKOUT-ONLY truth -- a born
    clone shipping no ``.claude/hooks/`` is the correct, expected shape,
    not a drift to catch, so this leg prints an explicit N/A
    classification there instead of silently shrinking the check count."""
    if not _rung1_present():
        print(
            "  N/A   rung-1 mirror-drift leg: no .claude/hooks/ in this tree (born-clone shape) -- dev-checkout-only truth, not applicable here",
        )
        return
    for name in _WORKER_INJECTED_HOOK_FILENAMES:
        path = _CHECKOUT_HOOKS_DIR / name
        _check(
            path.is_file(),
            f"[dev-checkout shape] {name} exists in the checkout's own .claude/hooks/ (rung 1): {path}",
        )


def test_resolution_against_the_real_repo_root_matches_the_measured_shape() -> None:
    """End-to-end against the REAL repo tree (not a synthetic fixture),
    shape-aware. Dev checkout (rung 1 present): every declared filename
    must resolve to the rung-1 copy (rung 1 always wins when present).
    Born clone (rung 1 absent): every declared filename must resolve to
    the rung-2 copy instead -- the SAME born-clone guarantee as direction
    1 above, restated at the resolution layer rather than the
    bare-file-presence layer, so a ladder bug that resolved to the wrong
    rung despite both files existing would still be caught."""
    if _rung1_present():
        for name in _WORKER_INJECTED_HOOK_FILENAMES:
            resolved = _resolve_worker_hook_path(REPO_ROOT, name)
            _check(
                resolved == _CHECKOUT_HOOKS_DIR / name,
                f"[dev-checkout shape] {name} resolves to the checkout's rung-1 copy against the real repo root",
            )
        return
    for name in _WORKER_INJECTED_HOOK_FILENAMES:
        resolved = _resolve_worker_hook_path(REPO_ROOT, name)
        _check(
            resolved == _PLUGIN_HOOKS_DIR / name,
            f"[born-clone shape] {name} resolves to the plugin's rung-2 fallback against the real repo root",
        )


def test_live_session_context_wiring_for_both_adapters() -> None:
    """Generating settings is pure: this test never spawns a worker."""
    paths = {name: _resolve_worker_hook_path(REPO_ROOT, name) for name in _WORKER_INJECTED_HOOK_FILENAMES}
    headless = HeadlessHostDriver(cwd=REPO_ROOT, solet_name="fixture-solet")
    settings = [
        headless._hook_settings_json(paths),
        _tmux_hook_settings_json(
            allowlist_hook_path=paths["headless_tool_allowlist_gate.py"],
            capture_hook_path=paths["capture_session_mapping.py"],
            heartbeat_hook_path=paths["heartbeat_report_alive.py"],
            rotation_due_hook_path=paths["rotation_due_watch.py"],
            wake_hook_path=paths["wake_waiter.py"],
            session_start_hook_path=paths["session_start_context.py"],
            step_zero_hook_path=paths["step_zero_reminder.py"],
            allow_askuserquestion=False,
        ),
    ]
    for index, raw in enumerate(settings):
        hooks = json.loads(raw)["hooks"]
        matches = [(group.get("matcher"), hook) for group in hooks["SessionStart"] for hook in group["hooks"] if "session_start_context.py" in hook["command"]]
        _check(len(matches) == 1, f"adapter {index} wires one live-context hook")
        _check(matches[0][0] == "startup|resume|clear", f"adapter {index} covers clear and startup")
        _check(matches[0][1]["timeout"] == 8, f"adapter {index} bounds live-context hook")
        _check("session_start_context.py" not in json.dumps(hooks["UserPromptSubmit"]), f"adapter {index} does not repeat live reads per prompt")


def _adapter_settings(root: Path) -> list[str]:
    paths = {name: root / name for name in _WORKER_INJECTED_HOOK_FILENAMES}
    return [
        HeadlessHostDriver(cwd=root, solet_name="fixture")._hook_settings_json(paths),
        _tmux_hook_settings_json(
            allowlist_hook_path=paths["headless_tool_allowlist_gate.py"],
            capture_hook_path=paths["capture_session_mapping.py"],
            heartbeat_hook_path=paths["heartbeat_report_alive.py"],
            rotation_due_hook_path=paths["rotation_due_watch.py"],
            wake_hook_path=paths["wake_waiter.py"],
            session_start_hook_path=paths["session_start_context.py"],
            step_zero_hook_path=paths["step_zero_reminder.py"],
            allow_askuserquestion=False,
        ),
    ]


def _run_command(command: str, payload: bytes = b'{"prompt":"next prompt"}\n') -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        shell=True,
        input=payload,
        capture_output=True,
        timeout=10,
        env={"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin"},
        check=False,
    )


def test_deleted_optional_adapter_commands() -> None:
    """The captured command survives deletion; no host or bridge is called."""
    with tempfile.TemporaryDirectory(prefix="hook-boundary-") as tmp:
        root = Path(tmp)
        target = root / "step_zero_reminder.py"
        target.write_text("print('present')\n", encoding="utf-8")
        captured = _adapter_settings(root)
        target.unlink()
        for index, raw in enumerate(captured):
            command = json.loads(raw)["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            done = _run_command(command)
            print(f"REACHED deleted optional adapter {index}: {done.returncode} {done.stderr!r}")
            assert done.returncode == 0, f"adapter {index} missing optional returncode == 0"
            assert done.stdout == b"", f"adapter {index} missing optional empty stdout"
            assert all(part.encode() in done.stderr for part in (target.name, "UserPromptSubmit", str(target))), f"adapter {index} missing optional named stderr advisory"
            assert _adapter_settings(root), f"adapter {index} next prompt settings proceed"


def test_present_hook_fidelity_and_runtime_failures() -> None:
    """Script globals, sibling imports, argv, stdin and exits survive quoting."""
    with tempfile.TemporaryDirectory(prefix="hook path '$ value ") as tmp:
        root = Path(tmp)
        target = root / "step_zero_reminder.py"
        (root / "sibling.py").write_text("VALUE = b' sibling'\n", encoding="utf-8")
        target.write_text(
            "import sys\nfrom sibling import VALUE\nassert __name__ == '__main__' and __file__ == sys.argv[0]\nsys.stdout.buffer.write(sys.stdin.buffer.read() + VALUE)\nsys.stderr.write(repr(sys.argv))\nraise SystemExit(2)\n",
            encoding="utf-8",
        )
        payload = b'{"prompt":"bytes"}\r\n'
        for index, raw in enumerate(_adapter_settings(root)):
            command = json.loads(raw)["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            done = _run_command(command + " " + shlex.join(["a b", "$literal"]), payload)
            print(f"REACHED present adapter {index}: {done.returncode} {done.stderr!r}")
            assert done.stdout == payload + b" sibling", f"adapter {index} spaced path stdin byte fidelity"
            assert done.stderr == repr([str(target), "a b", "$literal"]).encode(), "argv and stderr fidelity"
            assert done.returncode == 2, "present hook exit 2 preserved"
        target.write_text("raise FileNotFoundError('inside-present-hook')\n", encoding="utf-8")
        raw = _adapter_settings(root)[0]
        command = json.loads(raw)["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        done = _run_command(command)
        assert done.returncode == 1 and b"inside-present-hook" in done.stderr, "runtime absence is not initial absence"


def test_initial_open_deletion_race() -> None:
    from agent_messaging_plugin.claude_hook_commands import optional_hook_argv  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "race.py"
        target.write_text("print('must not execute')\n", encoding="utf-8")
        argv = optional_hook_argv(target, "Stop")
        # Delete at the actual open boundary, after any existence check would pass.
        prefix = f"import builtins, os\n_original_open = builtins.open\ndef _racing_open(path, *args, **kwargs):\n    if path == {str(target)!r}: os.unlink(path)\n    return _original_open(path, *args, **kwargs)\nbuiltins.open = _racing_open\n"
        argv[2] = prefix + argv[2]
        done = _run_command(shlex.join(argv))
        assert not target.exists(), "race reached target open and removed fixture"
        assert done.returncode == 0 and done.stdout == b"", "initial-open race is optional success"
        assert b"race.py" in done.stderr and b"Stop" in done.stderr, "race advisory names hook and event"


def main() -> int:
    print("worker-hook-shipping — adapter ladder vs. the real repo tree")
    print("=" * 68)
    shape = "dev-checkout (rung 1 present)" if _rung1_present() else "born-clone (rung 1 absent)"
    print(f"Measured tree shape: {shape}")
    print("-" * 68)
    test_present_hook_fidelity_and_runtime_failures()
    test_initial_open_deletion_race()
    test_deleted_optional_adapter_commands()
    test_declared_list_is_non_empty()
    test_every_declared_filename_ships_in_the_plugin_fallback_rung()
    test_every_declared_filename_exists_in_the_checkout_origin_rung()
    test_resolution_against_the_real_repo_root_matches_the_measured_shape()
    test_live_session_context_wiring_for_both_adapters()
    print("-" * 68)
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
