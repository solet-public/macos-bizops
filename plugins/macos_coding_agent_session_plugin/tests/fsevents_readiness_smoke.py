"""Real native-stream readiness, delayed startup, failure and stop lifecycle."""

from __future__ import annotations

import logging
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macos_coding_agent_session_plugin.fsevents_watcher import FSEventsWatcher  # noqa: E402


def _require(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}", flush=True)


def _watcher(directory: Path, changed: threading.Event) -> FSEventsWatcher:
    return FSEventsWatcher(
        watch_path=directory,
        target_filename="fixture.bridge.port",
        on_change=changed.set,
        logger=logging.getLogger("readiness-smoke"),
        latency_seconds=0.1,
    )


def _delayed_start_and_restart(directory: Path) -> None:
    import FSEvents  # noqa: PLC0415 - spy at the real native API boundary

    entered = threading.Event()
    release = threading.Event()
    changed = threading.Event()
    current_event_id = FSEvents.FSEventsGetCurrentEventId
    captured: list[int] = []

    def delayed_cursor() -> int:
        entered.set()
        if not release.wait(30.0):
            raise TimeoutError("test did not release delayed native cursor")
        cursor = int(current_event_id())
        captured.append(cursor)
        return cursor

    target = directory / "fixture.bridge.port"
    target.write_text("8101\n", encoding="utf-8")
    watcher = _watcher(directory, changed)
    try:
        with (
            patch.object(FSEvents, "FSEventsGetCurrentEventId", side_effect=delayed_cursor),
            patch.object(FSEvents, "FSEventStreamCreate", wraps=FSEvents.FSEventStreamCreate) as create,
        ):
            watcher.start()
            _require(entered.wait(30.0), "watcher reached delayed native cursor")
            _require(not watcher.wait_until_ready(timeout_seconds=0.05), "unarmed watcher never reports readiness")
            release.set()
            _require(watcher.wait_until_ready(), "native stream starts before mutation")
            arguments = create.call_args
            _require(arguments is not None and len(captured) == 1 and arguments.args[4] == captured[0], "native stream retains the captured startup cursor")
            target.write_text("8150\n", encoding="utf-8")
            with patch.object(FSEvents, "FSEventStreamFlushAsync", wraps=FSEvents.FSEventStreamFlushAsync) as flush:
                watcher.flush_pending_events()
                _require(flush.call_count == 1, "explicit flush reaches the native event queue")
            _require(changed.wait(30.0), "real event after readiness is delivered")
        watcher.stop()
        _require(not watcher.wait_until_ready(timeout_seconds=0), "stop clears readiness")
        _refuses_flush(watcher)
        changed.clear()
        watcher.start()
        _require(watcher.wait_until_ready(), "restart establishes fresh readiness")
        target.write_text("8200\n", encoding="utf-8")
        watcher.flush_pending_events()
        _require(changed.wait(30.0), "restarted native stream delivers a new change")
    finally:
        release.set()
        watcher.stop()


def _refuses_flush(watcher: FSEventsWatcher) -> None:
    try:
        watcher.flush_pending_events()
    except RuntimeError:
        print("PASS unarmed stream refuses flush", flush=True)
    else:
        raise AssertionError("unarmed stream accepted flush")


def _failed_start(directory: Path) -> None:
    import CoreFoundation  # noqa: PLC0415 - hold the invalid-run-loop mutant deterministically
    import FSEvents  # noqa: PLC0415 - actual native stream with a failed-start boundary

    release = threading.Event()

    def hold_invalid_runloop() -> None:
        release.wait(30.0)

    watcher = _watcher(directory, threading.Event())
    try:
        with (
            patch.object(FSEvents, "FSEventStreamStart", return_value=False),
            patch.object(CoreFoundation, "CFRunLoopRun", side_effect=hold_invalid_runloop),
        ):
            watcher.start()
            _require(not watcher.wait_until_ready(), "failed native start never publishes readiness")
            _refuses_flush(watcher)
    finally:
        release.set()
        watcher.stop()


def _coalesced_directory_event(directory: Path) -> None:
    changed = threading.Event()
    target = directory / "fixture.bridge.port"
    target.write_text("8101\n", encoding="utf-8")
    watcher = _watcher(directory, changed)
    build = cast(Callable[[FSEventsWatcher], Callable[..., None]], vars(FSEventsWatcher)["_build_callback"])
    callback = build(watcher)
    target.write_text("8150\n", encoding="utf-8")
    callback(None, None, 1, [str(directory).encode()], None, None)
    _require(changed.is_set(), "coalesced watched-directory event detects target content change")
    changed.clear()
    callback(None, None, 1, [str(directory) + "/"], None, None)
    _require(not changed.is_set(), "unchanged directory event does not restart again")


def main() -> None:
    logging.basicConfig(level=logging.DEBUG)
    with tempfile.TemporaryDirectory(prefix="fsevents-readiness-") as temporary:
        directory = Path(temporary).resolve()
        _coalesced_directory_event(directory)
        _delayed_start_and_restart(directory)
        _failed_start(directory)
    print("FSEvents readiness: all checks passed", flush=True)


if __name__ == "__main__":
    main()
