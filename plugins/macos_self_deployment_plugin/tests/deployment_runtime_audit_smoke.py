#!/usr/bin/env python3
"""Audit transient default-runtime writes in deployment lifecycle smokes."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
_AUDITED_SMOKES = (
    "plugins/macos_self_deployment_plugin/tests/complete_swap_crash_consistency_smoke.py",
    "plugins/macos_self_deployment_plugin/tests/no_sigkill_channel_smoke.py",
    "plugins/macos_self_deployment_plugin/tests/swap_round_trip_smoke.py",
    "plugins/macos_self_deployment_plugin/tests/drain_sigterm_exit_smoke.py",
    "plugins/macos_self_deployment_plugin/tests/slice4_drain_sentinel_smoke.py",
    "plugins/macos_self_deployment_plugin/tests/slice4_5_stop_self_smoke.py",
    "plugins/macos_self_deployment_plugin/tests/slice4_5_stop_self_dry_run_smoke.py",
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _audit_child(target: str, home: Path, *, mutant: bool = False) -> int:
    sys.dont_write_bytecode = True
    sys.path[:0] = [
        str(REPO_ROOT / "ananta" / "src"),
        str(REPO_ROOT),
        *(str(path) for path in (REPO_ROOT / "plugins").glob("*/src")),
        str(REPO_ROOT / "solet_setup_contracts" / "src"),
    ]
    os.environ["SOLET_NAME"] = "deployment-runtime-audit-smoke"
    os.environ.pop("HOMUNCULUS_NAME", None)
    os.environ.pop("XDG_RUNTIME_DIR", None)
    default_runtime = home / ".ananta" / "runtime"
    events: list[tuple[str, str]] = []

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            paths = (args[0],) if len(args) > 2 and isinstance(args[2], int) and args[2] & _WRITE_FLAGS else ()
        elif event == "os.mkdir":
            paths = (args[0],)
        elif event == "os.rename":
            paths = (args[0], args[1])
        elif event in {"os.remove", "os.rmdir"}:
            paths = (args[0],)
        else:
            return
        for raw_path in paths:
            if isinstance(raw_path, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(raw_path)).absolute()
                if path == default_runtime or default_runtime in path.parents:
                    events.append((event, str(path)))

    sys.addaudithook(audit)
    if mutant:
        probe = default_runtime / "audit-mutant.draining"
        probe.touch()
        probe.unlink()
        return 0 if any("audit-mutant" in path for _, path in events) else 1

    result_code = 0
    with patch.object(Path, "home", return_value=home):
        try:
            sys.argv = [str(REPO_ROOT / target)]
            runpy.run_path(str(REPO_ROOT / target), run_name="__main__")
        except SystemExit as exc:
            result_code = int(exc.code) if isinstance(exc.code, int) else 1
    if events:
        print(f"DEFAULT_RUNTIME_WRITE_EVENTS {events!r}")
        return 1
    return result_code


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--audit-child":
        return _audit_child(sys.argv[2], Path(sys.argv[3]))
    if len(sys.argv) == 3 and sys.argv[1] == "--audit-mutant":
        return _audit_child("", Path(sys.argv[2]), mutant=True)

    with tempfile.TemporaryDirectory(prefix="r81-", dir="/tmp") as scratch_name:
        home = Path(scratch_name)
        (home / ".ananta" / "runtime").mkdir(parents=True)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["SOLET_NAME"] = "deployment-runtime-audit-smoke"
        env.pop("HOMUNCULUS_NAME", None)
        env.pop("XDG_RUNTIME_DIR", None)
        for target in ("--audit-mutant", *_AUDITED_SMOKES):
            args = [sys.executable, "-B", str(Path(__file__).resolve())]
            args.extend(
                [target, str(home)]
                if target == "--audit-mutant"
                else ["--audit-child", target, str(home)]
            )
            result = subprocess.run(
                args,
                cwd=REPO_ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode != 0:
                raise AssertionError(
                    f"runtime audit case {target} failed (exit={result.returncode})\n"
                    f"{result.stdout}\n{result.stderr}"
                )
        print(
            f"PASS — audit mutant detected and {len(_AUDITED_SMOKES)} lifecycle smokes "
            "had no transient default-runtime writes"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
