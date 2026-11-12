#!/usr/bin/env python3
"""Startup hydration must leave registered Knowledge search available."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from plugins.default_knowledge_plugin.tests.g1_knowledge_registered_admission_smoke import Harness, assert_job_lineage, make_kb  # noqa: E402
from quality_gates.private_test_pg import MANIFEST_ENV, ledger_fixture, skip_fixture  # noqa: E402


def assert_search(harness: Harness) -> None:
    action = harness.action("search", {"query": "Original", "top_k": 8})
    response = harness.dispatch(action)
    assert response["success"] is True and response["data"]["status"] == "queued", response
    identity = response["data"]["job_id"]
    deadline = time.monotonic() + 2
    while True:
        job = harness.jobs.get_job(identity)["data"]["result"]["job"]
        if job["status"] in {"completed", "error"} or time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    print("WITNESS registered search with startup install still parked:", job["status"], job["result"])
    assert job["status"] == "completed" and job["result"] is not None, "registered search must finish while startup auto-install remains parked"
    assert job["result"]["data"]["results"], job
    assert_job_lineage(harness, job, action)


def startup_search(harness: Harness) -> None:
    """A real registered search finishes while startup is parked mid-install."""
    from ananta.core.orchestration import startup_sequence
    from default_knowledge_plugin import kb_lifecycle

    make_kb(harness, "startup")
    entered, release = threading.Event(), threading.Event()
    threads: list[threading.Thread] = []

    def parked_install(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(10), "owned startup install leaf must be released"
        return {}

    orchestrator = harness.plugin.orchestrator_ref
    assert orchestrator is not None
    orchestrator.service_bindings = SimpleNamespace(get_plugin_name=lambda _service: harness.plugin.name)
    orchestrator.shutdown_event = threading.Event()
    with patch("ananta.core.plugins.profile_manifest.load_manifest_plugin_set", return_value=None), patch.object(kb_lifecycle, "install_kb", side_effect=parked_install):
        startup_sequence._auto_install_knowledge_bases(orchestrator)
        try:
            assert entered.wait(5), "actual startup must reach the parked install leaf"
            assert threads[0].name == "knowledge-base-auto-install" and threads[0].daemon
            assert_search(harness)
            assert threads[0].is_alive() and not release.is_set(), "startup must still be parked at search completion"
        finally:
            release.set()
            for thread in threads:
                thread.join(10)
                assert not thread.is_alive(), "owned startup thread must drain"
            harness.join()
    print("PASS real startup hydration does not block terminal registered search")


def main() -> int:
    if not os.environ.get(MANIFEST_ENV):
        return skip_fixture()
    with ledger_fixture("fast_return_census", schema="knowledge_startup_fixture") as fixture:
        harness = Harness(fixture)
        make_kb(harness, "owned")
        installed = harness.service.install("owned")
        assert installed["chunk_count"] == 1, installed
        startup_search(harness)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
