#!/usr/bin/env python3
"""Smoke: a lane spawned in tmux resolves ``solet-bridge`` by name.

Problem prb_e03ec2bb (Change chg_1f9fa4db). ``tmux_adapter._env_pairs`` builds a
PATH with the CLI directory first (``solet_cli.expose_worker_cli``) and passes it
as ``new-session -e PATH=...``, yet a spawned lane could not run a bare
``solet-bridge``.

Where it was lost (measured 2026-11-20, tmux 3.7b, private servers): tmux keeps
the ``-e PATH`` value in the SESSION environment (``show-environment -t`` shows
it) but hands the pane process the PATH of the tmux CLIENT that ran the command.
Other ``-e`` variables reach the pane; PATH does not. With server-global PATH
``/serverA``, ``-e PATH=/eflagC`` and client PATH ``/clientB`` the pane saw
``/clientB``. The fix therefore exports the same PATH inside the pane command
before ``exec`` (``_pane_command``).

T1 (offline): ``_env_pairs`` with an absolute ``solet_bin`` returns a ``PATH=``
pair whose first entry is that binary's directory, even when the ambient PATH is
the tmux default.

T2 (real tmux, hermetic): a private tmux server on a temp socket (``-S``, config
``-f /dev/null``) is started with the argv shape ``TmuxHostDriver.spawn`` builds
(the real ``_env_pairs`` output and the real ``_pane_command``), the final
``exec`` running a probe that prints ``command -v solet-bridge``; the pane must
resolve the stub CLI. The smoke kills only its own server and deletes only its
own temp directory; it never touches the operator's tmux server.

Without the PATH export in ``_pane_command``, T2 fails: the pane resolves nothing.
A host without a tmux binary skips T2 loudly (advisory: the tmux driver cannot
spawn there either), never silently.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.tmux_adapter import (  # noqa: E402
    _env_pairs,
    _pane_command,
)

#: The PATH a tmux pane (or any launchd-started process) runs with: no CLI directory.
TMUX_DEFAULT_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_PROBE_TIMEOUT_SECONDS = 10.0

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


@contextmanager
def _ambient_path(value: str) -> Generator[None]:
    """Run with this process's PATH set to ``value``.

    Production hands the tmux client this process's environment, so this PATH is
    both what ``_env_pairs`` extends and what tmux gives the pane.
    """
    saved = os.environ.get("PATH")
    os.environ["PATH"] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved


def _stub_cli(root: Path) -> str:
    """A stub ``solet-bridge`` in its own directory; returns its absolute path."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "solet-bridge"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return str(stub)


def _lane_env_pairs(solet_bin: str, cwd: Path) -> list[str]:
    return _env_pairs(
        agent_instance_id="agi-lane-path-smoke",
        agent_session_id="ases-agi-lane-path-smoke",
        label="lane-path-smoke",
        solet_name="lanepathsmoke",
        solet_bin=solet_bin,
        allowed_tools=(),
        transport="watch",
        context_gauge_reporter_path=cwd / "rotation_due_watch.py",
        cwd=cwd,
    )


def test_t1_env_pairs_put_the_cli_directory_first() -> None:
    print("\nT1: _env_pairs puts the CLI directory first on the lane PATH")
    with tempfile.TemporaryDirectory() as tmp:
        solet_bin = _stub_cli(Path(tmp))
        with _ambient_path(TMUX_DEFAULT_PATH):
            pairs = _lane_env_pairs(solet_bin, Path(tmp))
    assignments = [pair for flag, pair in zip(pairs[::2], pairs[1::2], strict=True) if flag == "-e"]
    path_values = [pair.partition("=")[2] for pair in assignments if pair.startswith("PATH=")]
    _check(len(path_values) == 1, "T1: exactly one -e PATH= pair is built")
    first_entry = path_values[0].split(os.pathsep)[0] if path_values else ""
    _check(
        first_entry == str(Path(solet_bin).parent),
        f"T1: the first PATH entry is the CLI's directory (got {first_entry!r})",
    )


def _find_tmux() -> str | None:
    return shutil.which("tmux", path=os.pathsep.join([os.environ.get("PATH", ""), TMUX_DEFAULT_PATH]))


def _client_env() -> dict[str, str]:
    """The tmux client's environment: this process's, minus any enclosing tmux."""
    return {key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}


def _wait_for_probe(out_file: Path) -> str:
    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        text = out_file.read_text() if out_file.exists() else ""
        if text.endswith("\n"):
            return text
        time.sleep(0.1)
    return ""


def test_t2_a_real_tmux_pane_resolves_the_cli_by_name() -> None:
    print("\nT2: a pane spawned with the driver's argv shape resolves solet-bridge by name")
    tmux = _find_tmux()
    if tmux is None:
        print("  SKIP  T2: no tmux binary on this host (advisory: the tmux driver cannot spawn here either)")
        return
    # A short directory under /tmp: a unix socket path is limited to ~104 bytes and
    # a gate TMPDIR can be deeper than that.
    root = Path(tempfile.mkdtemp(prefix="lp-", dir="/tmp"))  # noqa: S108
    socket = root / "sock"
    out_file = root / "resolved.txt"
    try:
        solet_bin = _stub_cli(root)
        probe = ["sh", "-c", 'printf "resolved:%s\\n" "$(command -v solet-bridge)" > ' + shlex.quote(str(out_file))]
        with _ambient_path(TMUX_DEFAULT_PATH):
            env_pairs = _lane_env_pairs(solet_bin, root)
            pane = _pane_command(probe, label="lane-path-smoke", solet_bin=solet_bin, transport="watch")
            argv = [tmux, "-S", str(socket), "-f", "/dev/null", "new-session", "-d", "-s", "probe", "-x", "120", "-y", "30", *env_pairs, "-c", str(root), "sh", "-c", pane]
            started = subprocess.run(argv, env=_client_env(), capture_output=True, text=True, timeout=15, check=False)  # noqa: S603
        if started.returncode != 0:
            _check(False, f"T2: private tmux server started (exit {started.returncode}: {started.stderr.strip()})")
            return
        observed = _wait_for_probe(out_file).strip()
        _check(
            observed == f"resolved:{solet_bin}",
            f"T2: the pane's `command -v solet-bridge` is the stub CLI (got {observed!r}, want {'resolved:' + solet_bin!r})",
        )
    finally:
        subprocess.run([tmux, "-S", str(socket), "kill-server"], env=_client_env(), capture_output=True, timeout=10, check=False)  # noqa: S603
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    test_t1_env_pairs_put_the_cli_directory_first()
    test_t2_a_real_tmux_pane_resolves_the_cli_by_name()
    print(f"\nPASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
