#!/usr/bin/env python3
"""ROUTER-BINDING smoke for ``deployment/scripts/migrate_to_solet.py --scan-stale``.

After a solet rename, a process started under the OLD name keeps the router's
active binding for as long as it keeps heartbeating (the router drops a binding
only after ``heartbeat_timeout_seconds`` of silence). ``--scan-stale`` read
files only, so it printed a clean report while that process still held the
binding, and the next swap refused with ``not_active_instance`` (adopter
report #20).

The scan now asks each router for its ``status`` and reports every colour whose
instance id carries the old name. This smoke pins:

* T1 the stale binding is found, with colour, port, state, heartbeat age and
  pid, and a stale binding does not change the exit code;
* T2 a clean router prints "1 router(s) checked ... 0 stale", so a check that
  ran is distinguishable from one that did not;
* T3 an id of neither shape is reported as unrecognised, not as stale or an
  error;
* T4 an unreachable router, an error reply and a missing runtime directory
  print as NOT checked, the exit code stays 0, and nothing is created;
* T5 read-only: the router receives only ``status``, nothing is written, and the
  only subprocess is ``lsof``;
* T6 the listening pid: exactly one numeric line is a pid, every other outcome
  is unknown and the report says "pid unresolved".

Hermetic: a fake router thread on a temp-dir socket answers ``status`` from a
fixture and records every verb; ``LAUNCH_AGENTS``, ``REPO``, ``CLAUDE_DIR`` and
the runtime directory are redirected into the temp tree and ``_run`` is stubbed,
so the real machine, the real router and the real ``lsof`` are never touched.
The fixture solet name is the neutral sentinel ``origin`` for the reason the
ownership smoke documents: this file ships in every seed.

Run:
    .venv/bin/python3 \
        plugins/macos_self_deployment_plugin/tests/migrate_to_solet_router_binding_smoke.py
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"),
    str(_REPO_ROOT / "ananta" / "src"),
    str(_REPO_ROOT / "deployment" / "scripts"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from macos_self_deployment_plugin import stale_runtime_cleanup  # noqa: E402

# ``deployment/scripts`` is no package and sits outside every pyright root, so the
# script is loaded by name and typed as ``Any``: this smoke asserts its behaviour.
mts: Any = importlib.import_module("migrate_to_solet")

_passed = 0
_failed: list[str] = []

# The names under test. They do not exist on unfixed code: a missing one is a
# named FAIL below, never a crash, so the red-first run shows what is absent.
_scan: Any = getattr(mts, "scan_router_bindings", None)
_report: Any = getattr(mts, "print_router_binding_report", None)
_pid_of: Any = getattr(mts, "_listening_pid", None)


def _check(condition: bool, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed.append(label)
        print(f"  FAIL {label}")


def _need(test: str, **names: Any) -> bool:
    missing = [name for name, value in names.items() if value is None]
    for name in missing:
        _check(False, f"{test} {name} is defined in migrate_to_solet.py")
    return not missing


class _FakeRouter:
    """A router's management socket: answers ``status`` from a fixture and records every verb."""

    def __init__(self, path: Path, status_reply: dict[str, Any]) -> None:
        self.verbs: list[str] = []
        self._reply = status_reply
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(4)
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                self._answer(conn)

    def _answer(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        line = b""
        try:
            while not line.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                line += chunk
            verb = str(json.loads(line).get("verb"))
        except (OSError, ValueError, AttributeError):
            return
        self.verbs.append(verb)
        reply = self._reply if verb == "status" else {"error": "unknown_verb", "verb": verb}
        conn.sendall(json.dumps(reply).encode() + b"\n")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._server.close()


class _RunStub:
    """Stands in for ``mts._run`` so no test shells out to the real ``lsof``."""

    def __init__(
        self,
        stdout: str = "",
        returncode: int = 1,
        raises: Exception | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self._stdout = stdout
        self._returncode = returncode
        self._raises = raises

    def __call__(self, cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(cmd))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(cmd, self._returncode, self._stdout, "")


def _row(color: str, instance_id: str, port: int, status: str, age_s: float) -> dict[str, Any]:
    return {
        "color": color,
        "port": port,
        "instance_id": instance_id,
        "status": status,
        "last_heartbeat": time.time() - age_s,
        "streamable_port": None,
    }


def _status_reply(*rows: dict[str, Any]) -> dict[str, Any]:
    active = next((r for r in rows if r["status"] == "active"), None)
    return {
        "router_started_at": time.time() - 3600.0,
        "active_color": active["color"] if active else None,
        "active_instance_id": active["instance_id"] if active else None,
        "colors": list(rows),
        "drain_entries": [],
    }


def _abandoned_socket(path: Path) -> None:
    """A crashed router's leftover: a socket file with nothing listening on it."""
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    leftover.bind(str(path))
    leftover.close()


def _capture(fn: Callable[..., Any], *args: Any) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        fn(*args)
    return buffer.getvalue()


def _run_main(runtime: Path) -> tuple[int, str]:
    """``main()`` as ``--scan-stale``, with the router runtime directory pointed at ``runtime``."""
    stale_runtime_cleanup.runtime_dir = lambda: runtime
    saved_argv = sys.argv
    sys.argv = ["migrate_to_solet.py", "--scan-stale"]
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = mts.main()
    finally:
        sys.argv = saved_argv
    return code, buffer.getvalue()


def _listing(root: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*"))


def _verdicts(result: Any, verdict: str) -> list[Any]:
    return [b for b in result.bindings if b.verdict == verdict]


def _is_binding(binding: Any, instance_id: str, color: str, port: int, active: bool) -> bool:
    return binding is not None and (binding.instance_id, binding.color, binding.port, binding.active) == (
        instance_id,
        color,
        port,
        active,
    )


def _has(text: str, *needles: str) -> bool:
    return all(needle in text for needle in needles)


def _t1_check_result(result: Any) -> None:
    stale, fine = _verdicts(result, "stale"), _verdicts(result, "ok")
    _check(len(stale) == 1, "T1 exactly one stale binding is found")
    found = stale[0] if stale else None
    _check(
        _is_binding(found, "homunculus-blue-0a1b2c3d", "blue", 9101, True),
        "T1 the stale binding carries its id, colour, port and active state",
    )
    _check(found is not None and abs(found.heartbeat_age_s - 2.0) < 3.0, "T1 the heartbeat age is near 2 s")
    _check(found is not None and found.pid == 4242, "T1 the listening pid is attached")
    _check(
        [(b.instance_id, b.active) for b in fine] == [("solet-green-1a2b3c4d", False)],
        "T1 the inactive solet-green binding is ok, not stale",
    )
    text = _capture(_report, result)
    _check(
        _has(text, "homunculus-blue-0a1b2c3d", "9101", "pid 4242"),
        "T1 the report names the stale id, its port and its pid",
    )
    _check(_has(text, "ps -p 4242", "--scan-stale"), "T1 the report prints the recovery recipe")


def _t1_stale(root: Path) -> None:
    print("T1 stale: a pre-rename process holds the active binding")
    runtime = root / "t1"
    runtime.mkdir()
    stub = _RunStub(stdout="4242\n", returncode=0)
    mts._run = stub
    reply = _status_reply(
        _row("blue", "homunculus-blue-0a1b2c3d", 9101, "active", 2.0),
        _row("green", "solet-green-1a2b3c4d", 9102, "inactive", 2.0),
    )
    router = _FakeRouter(runtime / "origin.router.sock", reply)
    try:
        if _need("T1", scan_router_bindings=_scan, print_router_binding_report=_report):
            _t1_check_result(_scan(runtime_dir=runtime))
        code, output = _run_main(runtime)
        _check(
            "homunculus-blue-0a1b2c3d" in output,
            "T1 --scan-stale prints the stale router binding",
        )
        _check(code == 0, "T1 a stale binding is a finding, not a refusal: exit 0")
    finally:
        router.close()


def _t2_clean(root: Path) -> None:
    print("T2 clean: every binding is post-rename")
    runtime = root / "t2"
    runtime.mkdir()
    stub = _RunStub()
    mts._run = stub
    reply = _status_reply(
        _row("blue", "solet-blue-0a1b2c3d", 9101, "active", 1.0),
        _row("green", "solet-green-1a2b3c4d", 9102, "inactive", 1.0),
    )
    router = _FakeRouter(runtime / "origin.router.sock", reply)
    try:
        if _need("T2", scan_router_bindings=_scan, print_router_binding_report=_report):
            result = _scan(runtime_dir=runtime)
            _check(
                [b for b in result.bindings if b.verdict == "stale"] == [],
                "T2 no stale binding",
            )
            _check(result.checked == 1, "T2 one router was checked")
            text = _capture(_report, result)
            _check(
                "1 router(s) checked" in text and "0 stale" in text,
                "T2 the report says it checked one router and found 0 stale",
            )
            _check(stub.commands == [], "T2 lsof runs only for a stale binding")
        _, output = _run_main(runtime)
        _check(
            "1 router(s) checked" in output,
            "T2 --scan-stale prints the router-binding summary",
        )
    finally:
        router.close()


def _t3_unrecognized(root: Path) -> None:
    print("T3 unrecognised: an id of neither shape")
    runtime = root / "t3"
    runtime.mkdir()
    mts._run = _RunStub()
    reply = _status_reply(_row("blue", "custom-xyz", 9101, "active", 1.0))
    router = _FakeRouter(runtime / "origin.router.sock", reply)
    try:
        if _need("T3", scan_router_bindings=_scan, print_router_binding_report=_report):
            result = _scan(runtime_dir=runtime)
            _check(
                [b.verdict for b in result.bindings] == ["unrecognized"],
                "T3 custom-xyz is unrecognized, not stale",
            )
            _check(result.unreachable == [], "T3 an unrecognised id is not an error")
            text = _capture(_report, result)
            _check(
                "custom-xyz" in text and "1 unrecognized" in text,
                "T3 the report names it",
            )
    finally:
        router.close()


def _t4_unreachable(root: Path) -> None:
    print("T4 unreachable: no listener, an error reply, no runtime directory")
    runtime = root / "t4"
    runtime.mkdir()
    mts._run = _RunStub()
    _abandoned_socket(runtime / "ghost.router.sock")
    router = _FakeRouter(runtime / "error.router.sock", {"error": "boom"})
    absent = root / "t4-absent"
    try:
        if _need("T4", scan_router_bindings=_scan, print_router_binding_report=_report):
            result = _scan(runtime_dir=runtime)
            _check(result.checked == 0, "T4 neither router counts as checked")
            _check(len(result.unreachable) == 2, "T4 both sockets are unreachable entries")
            text = _capture(_report, result)
            not_checked = [line for line in text.splitlines() if "NOT checked" in line]
            _check(
                any("ghost.router.sock" in line for line in not_checked) and any("error.router.sock" in line for line in not_checked),
                "T4 each unreachable socket prints a line saying it was NOT checked",
            )
            _check("2 unreachable" in text, "T4 the summary counts both")
            missing = _scan(runtime_dir=absent)
            missing_text = _capture(_report, missing)
            _check(
                "NOT checked" in missing_text and str(absent) in missing_text,
                "T4 a missing runtime directory prints that bindings were NOT checked",
            )
        code, output = _run_main(runtime)
        _check(code == 0, "T4 main() still returns 0 with unreachable routers")
        _check("NOT checked" in output, "T4 --scan-stale prints the NOT checked lines")
        code, output = _run_main(absent)
        _check(code == 0, "T4 main() still returns 0 with no runtime directory")
        _check("NOT checked" in output, "T4 --scan-stale says the missing directory was NOT checked")
        _check(not absent.exists(), "T4 the missing runtime directory is still missing")
    finally:
        router.close()


def _t5_read_only(root: Path) -> None:
    print("T5 read-only: only status is sent, nothing is written")
    runtime = root / "t5"
    runtime.mkdir()
    stub = _RunStub(stdout="4242\n", returncode=0)
    mts._run = stub
    reply = _status_reply(
        _row("blue", "homunculus-blue-0a1b2c3d", 9101, "active", 2.0),
        _row("green", "solet-green-1a2b3c4d", 9102, "inactive", 2.0),
    )
    router = _FakeRouter(runtime / "origin.router.sock", reply)
    try:
        before = _listing(runtime)
        if _need("T5", scan_router_bindings=_scan, print_router_binding_report=_report):
            _capture(_report, _scan(runtime_dir=runtime))
        _run_main(runtime)
        _check(
            bool(router.verbs) and set(router.verbs) == {"status"},
            "T5 the router received only status requests",
        )
        _check(_listing(runtime) == before, "T5 the runtime tree is unchanged")
        _check(
            bool(stub.commands) and all(cmd[0] == "lsof" for cmd in stub.commands),
            "T5 the only subprocess is lsof",
        )
    finally:
        router.close()


def _t6_pid(root: Path) -> None:
    print("T6 pid: exactly one numeric line is a pid, anything else is unknown")
    if not _need("T6", _listening_pid=_pid_of, print_router_binding_report=_report):
        return
    cases: list[tuple[str, _RunStub, int | None]] = [
        ("one numeric line", _RunStub(stdout="4242\n", returncode=0), 4242),
        ("no output", _RunStub(stdout="", returncode=1), None),
        ("empty output, exit 0", _RunStub(stdout="", returncode=0), None),
        ("two lines", _RunStub(stdout="11\n22\n", returncode=0), None),
        ("a nonzero exit", _RunStub(stdout="4242\n", returncode=1), None),
        ("a non-numeric line", _RunStub(stdout="not-a-pid\n", returncode=0), None),
        ("lsof missing", _RunStub(raises=FileNotFoundError("lsof")), None),
    ]
    for name, stub, expected in cases:
        mts._run = stub
        _check(_pid_of(9101) == expected, f"T6 {name}: pid {expected}")
    _check(
        cases[0][1].commands == [["lsof", "-nP", "-t", "-iTCP:9101", "-sTCP:LISTEN"]],
        "T6 lsof is asked for the listener on that port only",
    )
    runtime = root / "t6"
    runtime.mkdir()
    mts._run = _RunStub(stdout="", returncode=1)
    reply = _status_reply(_row("blue", "homunculus-blue-0a1b2c3d", 9101, "active", 2.0))
    router = _FakeRouter(runtime / "origin.router.sock", reply)
    try:
        text = _capture(_report, _scan(runtime_dir=runtime))
    finally:
        router.close()
    _check("pid unresolved" in text, "T6 the report prints pid unresolved")


def run_smoke() -> int:
    print("migrate_to_solet_router_binding_smoke")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "claude").mkdir()
        (root / "checkout").mkdir()
        mts.LAUNCH_AGENTS = root / "LaunchAgents"
        mts.REPO = root / "checkout"
        mts.CLAUDE_DIR = root / "claude"

        _t1_stale(root)
        _t2_clean(root)
        _t3_unrecognized(root)
        _t4_unreachable(root)
        _t5_read_only(root)
        _t6_pid(root)

    print(f"\nmigrate_to_solet_router_binding_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
