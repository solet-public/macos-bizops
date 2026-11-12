#!/usr/bin/env python3
"""Regression test for address-book memory auto-ingest id capture.

Guards the 2026-07-17 fix in ``memory_integration.ingest_to_memory``.

Root cause: ``memory_service.remember`` returns the actr backend shape
directly — ``{"memory_id": <str>, "message": <str>}`` — where ``memory_id``
is TOP-LEVEL. The pre-fix code read ``result["data"]["memory_id"]``, so it
silently returned ``None`` on every registration: the memory was created and
recallable, but its id was never written back into the address record
(``address.memory_id`` stayed null → the memory is orphaned on address delete,
because the delete path archives only when ``address.memory_id`` is set).

Cases:
  (a) real backend shape (top-level memory_id) -> returns the id (the regression).
  (b) auto_ingest disabled -> returns None, remember NOT called.
  (c) memory_service is None -> returns None.
  (d) legacy/None result / missing id -> returns None (no crash).

Pattern A direct-executable (matches aws_self_deployment_plugin/tests/test_restart.py):

    .venv/bin/python3 plugins/default_address_book_plugin/tests/test_memory_integration.py

Exits 0 on success, 1 on first failure with a labeled message.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3]))
sys.path.insert(0, str(_HERE.parents[1] / "src"))
sys.path.insert(0, str(_HERE.parents[3] / "ananta" / "src"))

from default_address_book_plugin import plugin as location_module  # noqa: E402
from default_address_book_plugin.memory_integration import (  # noqa: E402
    ingest_to_memory,
)

from plugins.agent_messaging_plugin.tests.small_plugin_job_support import Ledger, terminal  # noqa: E402
from plugins.default_address_book_plugin.tests.path_address_resolution_smoke import MemoryState  # noqa: E402

_LOGGER = logging.getLogger("test_memory_integration")

_ENTRIES = [
    {"field_type": "city", "description": "Municipality", "value": "Langford"},
]


class _FakeMemoryService:
    """Records remember() calls and returns a configurable result."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.remember_calls: list[dict[str, Any]] = []

    def remember(self, content: str, tags: list[str]) -> Any:
        self.remember_calls.append({"content": content, "tags": tags})
        return self._result


def _fail(label: str) -> None:
    print(f"FAIL: {label}")
    sys.exit(1)


def _case_a_real_backend_shape() -> None:
    svc = _FakeMemoryService({"memory_id": "mem-test123", "message": "Remembered: ..."})
    got = ingest_to_memory(svc, True, "op_home", "postal", "desc", _ENTRIES, ["operator"], _LOGGER)
    if got != "mem-test123":
        _fail(f"(a) expected 'mem-test123' from top-level memory_id, got {got!r}")
    if len(svc.remember_calls) != 1:
        _fail(f"(a) expected exactly one remember() call, got {len(svc.remember_calls)}")
    tags = svc.remember_calls[0]["tags"]
    if tags[:2] != ["address-book", "type:postal"]:
        _fail(f"(a) expected leading address-book/type tags, got {tags!r}")


def _case_b_disabled() -> None:
    svc = _FakeMemoryService({"memory_id": "mem-should-not-be-read"})
    got = ingest_to_memory(svc, False, "op_home", "postal", "desc", _ENTRIES, [], _LOGGER)
    if got is not None:
        _fail(f"(b) expected None when auto_ingest disabled, got {got!r}")
    if svc.remember_calls:
        _fail("(b) remember() must NOT be called when auto_ingest disabled")


def _case_c_no_service() -> None:
    got = ingest_to_memory(None, True, "op_home", "postal", "desc", _ENTRIES, [], _LOGGER)
    if got is not None:
        _fail(f"(c) expected None when memory_service is None, got {got!r}")


def _case_d_missing_id() -> None:
    for result in ({"message": "no id here"}, None, {"data": {"memory_id": "x"}}):
        svc = _FakeMemoryService(result)
        got = ingest_to_memory(svc, True, "op_home", "postal", "desc", _ENTRIES, [], _LOGGER)
        if got is not None:
            _fail(f"(d) expected None for result={result!r}, got {got!r}")


def main() -> None:
    _case_location_owner_hook()
    _case_a_real_backend_shape()
    _case_b_disabled()
    _case_c_no_service()
    _case_d_missing_id()
    print("OK: address-book memory-ingest id capture (4 cases)")


def _case_location_owner_hook() -> None:
    plugin = location_module.DefaultAddressBookPlugin()
    hook = getattr(plugin, "admit_registered_operation", None)
    assert callable(hook), "registered location owner admission hook is missing"
    assert hook("unselected", {}, {}) is None
    for denied in (False, True):
        _location_probe_case(denied)


def _location_probe_case(denied: bool) -> None:
    """The real resolver/probe runs after admission using captured server state."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder).resolve()
        target = root / "reports"
        target.mkdir()
        ledger = Ledger()
        plugin = location_module.DefaultAddressBookPlugin({"auto_ingest_to_memory": False})
        plugin.logger = _LOGGER
        plugin.state_service = cast(Any, MemoryState())
        plugin.orchestrator_ref = cast(Any, SimpleNamespace(APP_HOME=str(root / "clone/profile"), async_job_manager=ledger))
        release = threading.Event()
        touches: list[int] = []
        captured: list[str] = []
        real_probe = tempfile.TemporaryFile

        def launch(state: dict[str, Any]) -> dict[str, str]:
            captured.append(state["session_id"])
            return {"path": str(target), "source": "launcher"}

        def probe(*args: Any, **kwargs: Any) -> Any:
            touches.append(threading.get_ident())
            assert Path(kwargs["dir"]).is_relative_to(root)
            assert release.wait(3), "owned location probe was not released"
            if denied:
                raise PermissionError("owned fixture denies writing")
            return real_probe(*args, **kwargs)

        with (
            patch.object(plugin, "_launch_directory_capture", side_effect=launch),
            patch.object(location_module.tempfile, "TemporaryFile", side_effect=probe),
            patch.object(location_module.Path, "home", return_value=root / "owned-home"),
        ):
            parameters = {"launch_directory": "/untrusted/request/path"}
            state = {"session_id": "trusted-session", "flow_id": "fixture-flow"}
            caller = threading.get_ident()
            started = time.perf_counter()
            receipt = plugin.admit_registered_operation("resolve_location", parameters, state)
            assert receipt is not None and receipt["data"]["status"] == "queued", receipt
            milliseconds = 1000 * (time.perf_counter() - started)
            assert milliseconds < 50 and caller not in touches, (milliseconds, touches)
            assert len(ledger.rows) == 1
            state["session_id"] = "spoofed-after-admission"
            parameters["path"] = "/untrusted/changed/path"
            release.set()
            row = terminal(plugin, ledger, receipt["data"]["job_id"])
            result = row["result"]["data"]
            assert row["status"] == "completed" and result["writable"] is not denied, row
            expected_root, expected_rung, expected_advisory = {
                False: (target, "launch_directory", ""),
                True: (root / "owned-home" / "Documents", "solet_home", f"Cannot write to {target}"),
            }[denied]
            actual_directory = Path(result["directory"])
            assert (
                expected_root in (actual_directory, *actual_directory.parents),
                result["rung"] == expected_rung,
                expected_advisory in "\n".join(result["advisories"]),
            ) == (True, True, True)
            assert captured == ["trusted-session"] and touches and caller not in touches
            assert bool(result["advisories"]) if denied else not any("Cannot write" in item for item in result["advisories"])
            assert not list(target.iterdir()), "the real probe must clean up its temporary file"
            print(f"PASS location owner admission: {milliseconds:.3f}ms writable={result['writable']} actual_probe=true captured_context=true")


if __name__ == "__main__":
    main()
