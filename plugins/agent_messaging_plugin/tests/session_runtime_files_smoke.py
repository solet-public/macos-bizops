#!/usr/bin/env python3
"""A session's watch and wake runtime files end with the session (chg_0a463909, Disk A3, plan disk-a3-a2).

The watch and wake halves write six files per session into the runtime folder
(``<solet>.<agent instance id>`` plus ``.watch.lock``, ``.watchpair``, ``.marks``,
``.spool``, ``.spool.lock``, ``.spool.offset``) and nothing removed them. Three
things now do, and each case below drives one of them against a temporary runtime
folder (``XDG_RUNTIME_DIR`` redirected, so no real folder is read or changed):

* (a)-(c) the module: one instance's families, a held watcher lock, and the
  families of every instance with no live session;
* (d) the session's own terminal event, both ways it happens;
* (e) the solet's start check, which sweeps only when asked;
* (f) the containment promise: a runtime-file fault never fails a terminal event.

The six names are built with the production writers, not restated, so a rename in
``local_cli/spool.py`` that the module does not follow fails here.

Run::

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/session_runtime_files_smoke.py
"""

from __future__ import annotations

import fcntl
import logging
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SOLET_NAME", "runtime-files-smoke")

if TYPE_CHECKING:
    from collections.abc import Generator

    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.core.runtime.port_manager import get_runtime_dir  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import host_end_notice, session_end, session_lifecycle_control, session_runtime_files  # noqa: E402
from agent_messaging_plugin.local_cli.spool import (  # noqa: E402
    default_spool_path,
    spool_lock_path,
    spool_offset_path,
    watch_marks_path,
    watch_pairing_path,
    watch_singleton_lock_path,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_MANAGED_SESSION,
)
from agent_messaging_plugin.session_lifecycle_control import terminate_session  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_runtime_files import (  # noqa: E402
    RuntimeFilesRemoval,
    end_session_runtime_files,
    instance_watch_held,
    remove_files_without_live_session,
    remove_instance_files,
)

_X = "agi-" + "1" * 32
_Y = "agi-" + "2" * 32
_Z = "agi-" + "3" * 32
_W = "agi-" + "4" * 32
_FIXTURE = "agi-0123456789abcdef0123456789abcdef"
_BARE_WATCH = "agi-watch-" + "5" * 24
_PRESENT_REF = "present-1"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str, detail: str = "") -> bool:
    global _passed
    text = f"runtime-files: {label}"
    if condition:
        _passed += 1
        print(f"  PASS  {text}")
        return True
    _failed.append(f"{text}: {detail}" if detail else text)
    print(f"  FAIL  {_failed[-1]}")
    return False


@contextmanager
def _runtime() -> Generator[Path]:
    """A runtime folder of this case's own, resolved through the production helper."""
    previous = os.environ.get("XDG_RUNTIME_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_RUNTIME_DIR"] = tmp
        try:
            yield get_runtime_dir("runtime-files-smoke")
        finally:
            if previous is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = previous


def _family(solet: str, instance: str) -> list[Path]:
    """Every file the watch and wake halves write for one session; the lock file comes first."""
    spool = default_spool_path(solet, instance)
    paths = [
        watch_singleton_lock_path(solet, instance),
        watch_pairing_path(solet, instance),
        watch_marks_path(solet, instance),
        spool,
        spool_lock_path(spool),
        spool_offset_path(spool),
    ]
    for path in paths:
        path.write_text("x\n", encoding="utf-8")
    return paths


def _names(paths: list[Path]) -> list[str]:
    return sorted(path.name for path in paths)


def _exist(paths: list[Path]) -> bool:
    return all(path.exists() for path in paths)


def _gone(paths: list[Path]) -> bool:
    return not any(path.exists() for path in paths)


@contextmanager
def _lock_held(path: Path) -> Generator[None]:
    """Hold the exclusive flock a running watcher holds, on a file description of its own."""
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _record(state: StateManagementInterface, key: str, *, host: str, ref: str = "", terminated: bool = False) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=key,
            label="runtime-files-fixture",
            session_origin="registered" if host == "operator" else "spawned",
            host=host,
        ),
    )
    if ref:
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": key}},
            {"host_ref": ref},
        )
    transition_lifecycle_state(state, agent_instance_id=key, from_state=LIFECYCLE_SPAWNING, to_state=LIFECYCLE_LIVE, directed_by="fixture")
    if terminated:
        transition_lifecycle_state(state, agent_instance_id=key, from_state=LIFECYCLE_LIVE, to_state=LIFECYCLE_TERMINATED, directed_by="fixture")


def _ledger() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _warnings_logged() -> Generator[list[str]]:
    """The module's warnings, kept for the case to read instead of printed."""
    handler = _Warnings()
    session_runtime_files.logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        session_runtime_files.logger.removeHandler(handler)


def case_modules_and_names() -> None:
    """The modules under test are this checkout's, and the six names are the writers' six."""
    for module in (session_runtime_files, session_lifecycle_control, session_end):
        _check(Path(str(module.__file__)).resolve().is_relative_to(ROOT), f"{module.__name__} loads from this checkout")
    with _runtime():
        family = _family("a", _X)
        _check(
            {path.name.removeprefix(f"a.{_X}") for path in family} == set(session_runtime_files.SUFFIXES),
            "SUFFIXES are exactly the six names the watch and wake writers use",
        )


def case_remove_instance_files() -> None:
    """(a) One instance's families go, whatever the solet; everything else stays."""
    with _runtime() as runtime:
        a_x, b_x, x_x, a_y = _family("a", _X), _family("b", _X), _family("x", _X), _family("a", _Y)
        no_lock = runtime / f"d.{_X}.spool"
        no_lock.write_text("x\n", encoding="utf-8")
        other = runtime / f"a.{_X}.other"
        other.write_text("x\n", encoding="utf-8")
        port = runtime / "smoke.router.port"
        port.write_text("1\n", encoding="utf-8")
        folder = runtime / f"c.{_X}.marks"
        folder.mkdir()
        link = runtime / f"e.{_X}.marks"
        link.symlink_to(port)
        removal = remove_instance_files(runtime, _X, keep_stem=f"x.{_X}")
        _check(_gone(a_x) and _gone(b_x), "(a) all six files of a.X and of b.X are removed")
        _check(not no_lock.exists(), "(a) a family with no lock file is free and is removed")
        _check(_exist(x_x), "(a) keep_stem keeps that family whole")
        _check(_exist(a_y), "(a) another instance's family is untouched")
        _check(other.exists() and port.exists(), "(a) a name that is not one of the six suffixes is untouched")
        _check(folder.is_dir(), "(a) a directory with a family name is untouched")
        _check(link.is_symlink() and port.read_text(encoding="utf-8") == "1\n", "(a) a symlink with a family name is untouched, and so is its target")
        _check(sorted(removal.removed) == _names([*a_x, *b_x, no_lock]), "(a) removed names exactly the thirteen files", f"got {sorted(removal.removed)}")
        _check(removal.kept_locked == () and removal.failed == (), "(a) nothing kept locked and nothing failed")


def case_held_lock_keeps_the_family() -> None:
    """(b) A watcher's flock keeps its family; only that family; and releasing it ends the protection."""
    with _runtime() as runtime:
        held, free = _family("a", _X), _family("b", _X)
        _check(instance_watch_held(runtime, _W) is False, "(b) an instance with no files holds no lock")
        with _lock_held(held[0]):
            _check(instance_watch_held(runtime, _X) is True, "(b) instance_watch_held sees the held lock")
            removal = remove_instance_files(runtime, _X)
            _check(_exist(held), "(b) the family whose lock is held is kept whole")
            _check(_gone(free), "(b) the same instance's free family under another solet is removed")
            _check(removal.kept_locked == (f"a.{_X}",), "(b) kept_locked names the held stem", f"got {removal.kept_locked}")
            _check(sorted(removal.removed) == _names(free) and removal.failed == (), "(b) removed names only the free family")
        _check(instance_watch_held(runtime, _X) is False, "(b) the lock reads free once its holder lets go")
        after = remove_instance_files(runtime, _X)
        _check(_gone(held) and after.kept_locked == (), "(b) the released family is removed on the next call")


def case_sweep_keeps_only_live_instances() -> None:
    """(c) Every family whose instance is not live goes; bare-watch ids are not session families."""
    with _runtime() as runtime:
        x, y, z = _family("a", _X), _family("a", _Y), _family("a", _Z)
        bare = _family("a", _BARE_WATCH)
        removal = remove_files_without_live_session(runtime, {_Y})
        _check(_gone(x) and _gone(z), "(c) the families of X and Z are removed")
        _check(_exist(y), "(c) the live instance's family is kept")
        _check(_exist(bare), "(c) an agi-watch-<24 hex> family is not matched by the stem rule and is kept")
        _check(sorted(removal.removed) == _names([*x, *z]), "(c) removed names exactly the twelve files")
        with _warnings_logged() as warnings:
            missing = remove_files_without_live_session(runtime / "absent", {_Y})
        _check(missing.removed == () and len(missing.failed) == 1, "(c) a folder that cannot be read is one failure, not an exception", f"got {missing}")
        _check(len(warnings) == 1, "(c) and it is logged", f"got {warnings}")


def case_terminal_events_remove_the_files() -> None:
    """(d) Ending a session removes its files in the same act, on every path that fires its terminal edges."""
    with _runtime():
        catch_up, dying, reported, bystander = _family("a", _X), _family("a", _Z), _family("a", _W), _family("a", _Y)
        state = _ledger()
        _record(state, _X, host="operator", terminated=True)
        _record(state, _Z, host="operator")
        _record(state, _W, host="operator")
        _record(state, _Y, host="operator")
        repeat = terminate_session(state, agent_instance_id=_X, directed_by="fixture")
        _check(repeat["already_terminal"] is True and _gone(catch_up), "(d) terminate_session on an already-terminal session removes its family")
        _check(_exist(dying), "(d) a session that has not ended keeps its family")
        first = terminate_session(state, agent_instance_id=_Z, directed_by="fixture")
        _check(first["already_terminal"] is False and _gone(dying), "(d) terminate_session on a live session removes its family")
        ended = session_end.end_reported_session(state, agent_instance_id=_W, reason="smoke", directed_by="fixture")
        _check(ended["ended"] is True and _gone(reported), "(d) a session that reports its own end has its family removed")
        _check(_exist(bystander), "(d) another live session's family is untouched")


def case_start_check_sweeps_only_when_asked() -> None:
    """(e) check_session_records leaves the folder alone by default and sweeps it with sweep_session_files."""
    with _runtime():
        ended, orphan, live, watched = _family("a", _X), _family("a", _FIXTURE), _family("a", _Y), _family("a", _Z)
        state = _ledger()
        _record(state, _X, host="operator", terminated=True)
        _record(state, _Y, host="tmux", ref=_PRESENT_REF)
        present = SimpleNamespace(returncode=0, stderr="")
        with mock.patch.object(host_end_notice, "_run", return_value=present):
            default = session_end.check_session_records(state, directed_by="fixture", tmux=["/fake/tmux"])
            _check(_exist(ended) and _exist(orphan) and _exist(live) and _exist(watched), "(e) the default call removes nothing")
            _check("session_files" not in default, "(e) the default call reports no session_files")
            with _lock_held(watched[0]):
                swept = session_end.check_session_records(state, directed_by="fixture", tmux=["/fake/tmux"], sweep_session_files=True)
        _check(_gone(ended) and _gone(orphan), "(e) the sweep removes a terminated record's family and a fixture-shaped id with no record")
        _check(_exist(live), "(e) the sweep keeps the family of the record still live")
        _check(_exist(watched), "(e) the sweep keeps a family whose watcher lock is held")
        _check(swept["session_files"] == {"removed": 12, "kept_locked": 1, "failed": 0}, "(e) result['session_files'] counts them", f"got {swept.get('session_files')}")
        _check(swept["counts"]["present"] == 1, "(e) the host-gone half of the check still ran")


def case_a_runtime_file_fault_never_fails_a_terminal_event() -> None:
    """(f) end_session_runtime_files logs and returns an empty result; an id that names no family never reads the folder."""
    with _warnings_logged() as warnings:
        with mock.patch.object(session_runtime_files, "get_runtime_dir", side_effect=RuntimeError("runtime folder unavailable")):
            result = end_session_runtime_files(_X)
        _check(result == RuntimeFilesRemoval(), "(f) a failing runtime folder returns an empty result and raises nothing", f"got {result}")
        _check(any("runtime folder unavailable" in message for message in warnings), "(f) the fault is logged", f"got {warnings}")
        with mock.patch.object(session_runtime_files, "get_runtime_dir") as resolve:
            nameless = end_session_runtime_files("present")
        _check(nameless == RuntimeFilesRemoval() and resolve.call_count == 0, "(f) an id that cannot name a family does not read the runtime folder")


def main() -> int:
    print("agent_messaging — a session's runtime files end with the session")
    print("=" * 72)
    cases: tuple[Any, ...] = (
        case_modules_and_names,
        case_remove_instance_files,
        case_held_lock_keeps_the_family,
        case_sweep_keeps_only_live_instances,
        case_terminal_events_remove_the_files,
        case_start_check_sweeps_only_when_asked,
        case_a_runtime_file_fault_never_fails_a_terminal_event,
    )
    for case in cases:
        print(case.__name__)
        case()
    print("-" * 72)
    if _failed:
        print(f"{_passed} passed, {len(_failed)} FAILED")
        return 1
    print(f"{_passed} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
