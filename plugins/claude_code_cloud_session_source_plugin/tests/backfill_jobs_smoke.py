"""Hermetic cloud-job regressions, real POSIX subprocess locks, fake product effects.

This shipped sibling runs against its own adapter and independent instances.
The Codex plugin smoke exercises the combined cross-vendor contract. No account credentials,
HTTP network or product process is used. Test children are disposable.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "claude_code_cloud_session_source_plugin" / "src"))
_NAME = Path(__file__).resolve().parents[1].name
_OTHER = _NAME  # Standalone shipped contract; the Codex smoke owns cross-vendor pairing.
product = importlib.import_module(f"{_NAME}.plugin")
jobs = importlib.import_module(f"{_NAME}.backfill_jobs")
walker = importlib.import_module(f"{_NAME}.walker")
peer_jobs = importlib.import_module(f"{_OTHER}.backfill_jobs")

from ananta.llm.session_ledger.importer import SessionLedgerImporter  # noqa: E402
from ananta.llm.session_ledger.repository import LedgerRepositoryError  # noqa: E402


class FakeManager:
    """Atomic fake job/request and job/outcome stores with transaction fault points."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[dict[str, Any]] = []
        self.fault = ""
        self.before_update: Callable[[dict[str, Any]], None] = lambda update: None
        self.after_commit: Callable[[dict[str, Any]], None] = lambda update: None

    def create_job(self, plugin_name: str, action_name: str, **kwargs: Any) -> dict[str, Any]:
        if self.fault == "create":
            return {"action_status": "error", "error": "atomic create/request fault"}
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs[job_id] = {"id": job_id, "status": "queued", "provider_name": f"{plugin_name}.{action_name}", "metadata": kwargs.get("job_metadata")}
        self.requests[job_id] = kwargs["request_data"].copy()
        return {"action_status": "completed", "data": {"job_id": job_id, "status": "queued"}}

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        self.before_update(updates)
        self.writes.append((job_id, updates.copy()))
        if self.fault == updates.get("status", "progress"):
            return {"action_status": "error", "data": {"outcome_committed": False}, "error": "atomic update/payload fault"}
        assert self.jobs[job_id]["status"] in {"queued", "processing"}, "terminal outcome is immutable"
        self.jobs[job_id].update(updates)
        committed = updates.get("status") in {"completed", "error"}
        self.after_commit(updates)
        if committed and self.fault == "routing":
            return {"action_status": "error", "data": {"outcome_committed": True}, "error": "completion delivery failed"}
        return {"action_status": "completed", "data": {"outcome_committed": committed}}

    def list_jobs(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(kwargs)
        rows = [row.copy() for row in self.jobs.values() if row["provider_name"] == kwargs["provider_name"] and row["status"] == kwargs["status"]]
        return {"action_status": "completed", "data": {"jobs": rows[: kwargs["limit"]]}}

    def get_job(self, job_id: str) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"job": self.jobs[job_id].copy()}}

    def get_job_payload(self, job_id: str, kind: str) -> dict[str, Any]:
        payload = self.requests[job_id] if kind == "request" else self.jobs[job_id][kind]
        return {"action_status": "completed", "data": {"payload": payload}}


def _plugin() -> Any:
    cls = product.CodexCloudSessionSourcePlugin if _NAME.startswith("codex") else product.ClaudeCodeCloudSessionSourcePlugin
    return cls()


def _config() -> Any:
    common = {"api_base_url": "https://fake.invalid", "list_page_limit": 1, "walker_concurrency": 1, "fetch_timeout_seconds": 1, "rate_limit_backoff_seconds": (0, 0)}
    if _NAME.startswith("codex"):
        return walker.WalkerConfig(auth_path=Path("/unread-fake-auth"), **common)
    return walker.WalkerConfig(keychain_service="unread-fake", keychain_envelope_path="fake.token", anthropic_version="fake", anthropic_beta="fake", events_page_limit=1, **common)


def _handler(plugin: Any) -> Any:
    return plugin.backfill_from_cloud_action if _NAME.startswith("codex") else plugin.backfill_from_cloud


def _runtime(manager: FakeManager, path: Path, run: Any = None, module: Any = jobs, name: str = _NAME) -> Any:
    return module.BackfillRuntime(name, lambda: manager, lambda: path, run or (lambda parameters, stop, progress: {"fetched_count": 1}))


def _join(runtime: Any) -> None:
    thread = runtime._worker
    if thread is not None:
        thread.join(3)
        assert not thread.is_alive(), "disposable worker settled"


def _dispatch(runtime: Any, force: bool = False) -> str:
    result = runtime.dispatch({"force": force}, {"session_id": "fake-session", "flow_id": "fake-flow"})
    assert result["data"]["status"] == "queued", result
    return cast(str, result["data"]["job_id"])


def _child() -> None:
    """The parent kills no product process; os._exit models only this test child."""
    module = importlib.import_module(f"{sys.argv[2]}.backfill_jobs")
    path = Path(sys.argv[3])
    stage = sys.argv[4]
    input()  # coordinated simultaneous admission
    if stage == "before_lock":
        os._exit(0)
    manager = FakeManager()
    if stage in {"after_lock", "queued"}:
        held = module.try_lock(path)
        assert held is not None
        if stage == "queued":
            held.create(manager, sys.argv[2], {"force": False}, {})
        print(json.dumps({"jobs": manager.jobs, "requests": manager.requests, "inode": os.fstat(held.descriptor).st_ino}), flush=True)
        os._exit(0)
    release = threading.Event()

    def work(parameters: Any, stop: Any, progress: Any) -> dict[str, object]:
        del parameters, stop, progress
        release.wait(5)
        return {"fetched_count": 1}

    runtime = _runtime(manager, path, work, module, sys.argv[2])
    result = runtime.dispatch({"force": False}, {})
    print(json.dumps({"result": result, "inode": path.stat().st_ino}), flush=True)
    if result["data"]["status"] == "queued":
        command = input()
        if command == "crash":
            os._exit(0)
        release.set()
        _join(runtime)


def _spawn(path: Path, name: str = _NAME, stage: str = "hold") -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "child", name, str(path), stage], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, close_fds=True)


def _send(child: subprocess.Popen[str], text: str) -> None:
    assert child.stdin is not None
    child.stdin.write(text + "\n")
    child.stdin.flush()


def _read(child: subprocess.Popen[str]) -> dict[str, Any]:
    assert child.stdout is not None
    line = child.stdout.readline()
    assert line, child.stderr.read() if child.stderr is not None else "no child output"
    return cast(dict[str, Any], json.loads(line))


class FakeRepository:
    """Exercise the real importer's synchronous session/event upserts with fake storage."""

    def __init__(self, plugin: Any) -> None:
        self.kind = plugin.describe().source_kind
        self.events: dict[str, str] = {}
        self.sessions: set[str] = set()
        self.fail_after_first = True
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def list_sources(self, **kwargs: Any) -> list[Any]:
        del kwargs
        return [SimpleNamespace(id="fake-source", source_kind=self.kind, enabled=True)]

    def start_batch(self, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        return "fake-batch"

    def finish_batch(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def upsert_session(self, **kwargs: Any) -> str:
        self.sessions.add(kwargs["external_session_id"])
        return cast(str, kwargs["external_session_id"])

    def find_event_id_by_vendor_id(self, **kwargs: Any) -> str | None:
        return self.events.get(f"{kwargs['session_id']}:{kwargs['vendor_event_id']}")

    def append_event(self, **kwargs: Any) -> Any:
        normalized = kwargs["normalized"]
        key = f"{kwargs['session_id']}:{normalized.vendor_event_id or kwargs['external_id']}"
        if self.fail_after_first and self.events and key not in self.events:
            raise LedgerRepositoryError("injected crash after partial persisted ingest")
        self.entered.set()
        self.release.wait(4)
        deduped = key in self.events
        self.events[key] = str(normalized.content_text)
        return SimpleNamespace(event_id=key, deduped=deduped)


def _http_fixture(request: httpx.Request) -> httpx.Response:
    """Two roster pages and, for Claude, two event pages. All effects are local."""
    cursor = request.url.params.get("cursor")
    if request.url.path.endswith("/tasks/list"):
        row = {"id": "task-alpha" if cursor is None else "task-beta", "updated_at": "2026-10-18T00:00:00Z"}
        return httpx.Response(200, json={"tasks": [row], "next_cursor": "page-2" if cursor is None else None})
    if request.url.path.endswith("/sessions"):
        row = {"id": "cse_alpha" if cursor is None else "cse_beta", "created_at": "2026-10-18T00:00:00Z"}
        return httpx.Response(200, json={"data": [row], "has_more": cursor is None, "next_cursor": "page-2" if cursor is None else None})
    if request.url.path.endswith("/events"):
        return _events_page(cursor)
    return httpx.Response(200, json={"current_user_turn": {"id": "turn", "input_items": [{"type": "message", "id": f"msg-{i}", "role": "user", "content": [{"content_type": "text", "text": f"event-{i}"}]} for i in (1, 2)]}})


def _events_page(cursor: str | None) -> httpx.Response:
    event_id = "evt-1" if cursor is None else "evt-2"
    event = {"event_type": "user", "created_at": "2026-10-18T00:00:00Z", "payload": {"type": "user", "message": {"role": "user", "content": event_id}, "uuid": event_id}}
    return httpx.Response(200, json={"data": [event], "has_more": cursor is None, "next_cursor": "events-2" if cursor is None else None})


def _live_fake(plugin: Any, home: Path, manager: FakeManager, repository: FakeRepository) -> tuple[Any, Any]:
    """Use real importer/parser/upsert boundaries; fake only HTTP, auth and storage."""
    importer = SessionLedgerImporter(registry=cast(Any, SimpleNamespace(require_by_kind=lambda kind: plugin)), repository=cast(Any, repository), blob_adapter=cast(Any, SimpleNamespace(should_offload_text=lambda text: False)))
    ledger = SimpleNamespace(importer=importer, list_sessions=lambda **kwargs: {"sessions": [{"external_session_id": value} for value in repository.sessions]})
    plugin.orchestrator_ref = SimpleNamespace(APP_HOME=str(home), async_job_manager=manager, get_service=lambda name: ledger)
    client_class = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return client_class(transport=httpx.MockTransport(_http_fixture), **kwargs)

    credentials = walker.CodexCloudCredentials("fake", "fake-account", False) if _NAME.startswith("codex") else walker.ClaudeCodeCloudCredentials("fake")
    return client, credentials


class BackfillJobsFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.home = Path(self.scratch.name)
        (self.home / "data").mkdir()
        self.path = (self.home / "data").resolve() / "cloud_backfill.lock"
        self.manager = FakeManager()

    def tearDown(self) -> None:
        self.scratch.cleanup()


class BackfillJobsSmoke(BackfillJobsFixture):
    def test_dispatch_returns_before_credentials_and_serial_sentinel(self) -> None:
        entered, release = threading.Event(), threading.Event()
        credential_threads: list[int] = []
        caller_threads: list[int] = []

        def credentials(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            credential_threads.append(threading.get_ident())
            entered.set()
            release.wait(4)
            error = walker.CodexCloudWalkerError if _NAME.startswith("codex") else walker.ClaudeCodeCloudWalkerError
            raise error("fake_auth", "fake credential seam")

        plugin = _plugin()
        plugin.orchestrator_ref = SimpleNamespace(APP_HOME=str(self.home), async_job_manager=self.manager)

        def invoke() -> dict[str, Any]:
            caller_threads.append(threading.get_ident())
            return cast(dict[str, Any], _handler(plugin)({"force": True}, {"session_id": "fake-session", "flow_id": "fake-flow"}))

        with patch.object(plugin, "_build_walker_config", return_value=_config()), patch.object(product, "load_credentials", side_effect=credentials), ThreadPoolExecutor(max_workers=1) as serial:
            dispatch = serial.submit(invoke)
            sentinel = serial.submit(lambda: "serial-sentinel")
            try:
                self.assertTrue(entered.wait(2), "credential seam reached off dispatcher")
                result = dispatch.result(timeout=1)  # M1: inline wait strands this serial sentinel
                self.assertEqual(result["data"]["status"], "queued", "canonical queued return")
                self.assertEqual(sentinel.result(timeout=1), "serial-sentinel", "serial poller sentinel completes")
                self.assertNotEqual(credential_threads, caller_threads, "zero dispatcher credential reads")
                self.assertEqual(len(self.manager.jobs), 1)
                request = self.manager.requests[result["data"]["job_id"]]
                self.assertTrue(request["parameters"]["force"])
                self.assertNotIn("credentials", request)
            finally:
                release.set()
                _join(plugin._backfill)

    def test_same_process_both_vendors_instances_and_positive_readmission(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def run(parameters: Any, stop: Any, progress: Any) -> dict[str, object]:
            del parameters, stop, progress
            entered.set()
            release.wait(4)
            return {"fetched_count": 1}

        first = _runtime(self.manager, self.path, run)
        second = _runtime(self.manager, self.path, module=peer_jobs, name=_OTHER)
        third = _runtime(self.manager, self.path)
        _dispatch(first)
        try:
            self.assertTrue(entered.wait(2))
            for runtime in (second, third):
                result = runtime.dispatch({}, {})
                self.assertEqual(result["data"]["status"], "busy", "cross-vendor and separate-instance busy")
                self.assertIsNone(runtime._worker, "zero queued worker futures on busy admission")
            self.assertEqual(len(self.manager.jobs), 1)
        finally:
            release.set()
            _join(first)
        _dispatch(second)
        _join(second)
        self.assertEqual(len(self.manager.jobs), 2, "new independent attempt after release")

    def test_simultaneous_real_processes_cross_vendor_and_crash_release(self) -> None:
        children = [_spawn(self.path, name) for name in (_NAME, _OTHER)]
        try:
            for child in children:
                _send(child, "go")
            rows = [_read(child) for child in children]
            statuses = [row["result"]["data"]["status"] for row in rows]
            self.assertEqual(sorted(statuses), ["busy", "queued"], "two-process single accepted job")
            self.assertEqual(rows[0]["inode"], rows[1]["inode"], "both vendors/colors share inode")
            index = statuses.index("queued")
            held = jobs.try_lock(self.path)
            self.assertIsNone(held, "handoff retains kernel lock in real child")
            _send(children[index], "crash")
            self.assertEqual(children[index].wait(3), 0)
            fresh = jobs.try_lock(self.path)
            self.assertIsNotNone(fresh, "OS releases only after disposable holder process exits")
            fresh.release()
        finally:
            for child in children:
                if child.poll() is None:
                    _send(child, "crash")
                child.communicate(timeout=5)

    def test_crash_before_lock_after_lock_and_durable_queued_before_submit(self) -> None:
        for stage in ("before_lock", "after_lock", "queued"):
            child = _spawn(self.path, stage=stage)
            _send(child, "go")
            row = _read(child) if stage != "before_lock" else {"jobs": {}, "requests": {}}
            self.assertEqual(child.wait(3), 0)
            child.communicate(timeout=1)
            self.manager.jobs.update(row["jobs"])
            self.manager.requests.update(row["requests"])
            runtime = _runtime(self.manager, self.path)
            count = runtime.poll_once()
            self.assertEqual(count, 1 if stage == "queued" else 0, "crash-before-submit reconciliation reached")
            if stage == "queued":
                self.assertEqual(self.manager.jobs["job-1"]["error"]["code"], "worker_interrupted", "durable queued orphan eventually interrupted")
                self.assertIn("job-1", self.manager.requests)

    def test_terminal_routing_return_holds_lock_and_committed_failure_is_preserved(self) -> None:
        entered, release = threading.Event(), threading.Event()
        self.manager.fault = "routing"
        committed, route_return = threading.Event(), threading.Event()

        def after(update: dict[str, Any]) -> None:
            if update.get("status") == "completed":
                committed.set()
                route_return.wait(4)

        self.manager.after_commit = after

        def before(update: dict[str, Any]) -> None:
            if update.get("status") == "completed":
                entered.set()
                release.wait(4)

        self.manager.before_update = before
        runtime = _runtime(self.manager, self.path)
        job_id = _dispatch(runtime)
        try:
            self.assertTrue(entered.wait(2), "old writer paused at terminal transaction")
            self.assertIsNone(peer_jobs.try_lock(self.path), "successor cannot acquire before routing returns")
            release.set()
            self.assertTrue(committed.wait(2), "durable terminal committed before routing returns")
            self.assertEqual(self.manager.jobs[job_id]["status"], "completed")
            self.assertIsNone(peer_jobs.try_lock(self.path), "lock spans durable outcome through routing return")
        finally:
            release.set()
            route_return.set()
            _join(runtime)
        self.assertEqual(self.manager.jobs[job_id]["status"], "completed", "outcome_committed delivery failure preserves success")
        terminal = [updates for _, updates in self.manager.writes if updates.get("status") in {"completed", "error"}]
        self.assertEqual(len(terminal), 1, "never overwrite success after completion delivery failure")
        self.assertEqual(jobs.status(self.manager, _NAME, job_id)["data"]["result"]["fetched_count"], 1)

    def test_released_handles_reject_all_writes_and_submit_with_new_owner(self) -> None:
        held = jobs.try_lock(self.path)
        self.assertIsNotNone(held)
        job_id = held.create(self.manager, _NAME, {}, {})
        token = held.token
        held.release()
        newer = jobs.try_lock(self.path)
        self.assertIsNotNone(newer)
        try:
            newer_id = newer.create(self.manager, _OTHER, {}, {})
            before = list(self.manager.writes)
            for updates in ({"result": {"fetched_count": 1}}, {"status": "completed"}, {"status": "error"}):
                with self.assertRaises(RuntimeError, msg="released stale adapter must reject before manager"):
                    held.update(self.manager, job_id, token, updates)
            with self.assertRaises(RuntimeError):
                held.submit(threading.Thread(target=lambda: None), job_id, token)
            with self.assertRaises(RuntimeError):
                held.create(self.manager, _NAME, {}, {})
            self.assertEqual(self.manager.writes, before, "stale adapter zero manager calls after release")
            self.assertEqual(self.manager.jobs[newer_id]["status"], "queued", "newer outcome unchanged")
        finally:
            newer.release()

    def test_reconciliation_is_four_reads_four_writes_and_no_replay(self) -> None:
        for provider in jobs.PROVIDERS:
            name = provider.split(".")[0]
            for status in ("queued", "processing"):
                for _ in range(3):
                    job_id = self.manager.create_job(name, "backfill_from_cloud", request_data={"notes": "old request"})["data"]["job_id"]
                    self.manager.jobs[job_id]["status"] = status
        runtime = _runtime(self.manager, self.path, lambda *args: self.fail("reconciliation must not run product work"))
        for _ in range(3):
            self.manager.reads.clear()
            before = len(self.manager.writes)
            self.assertEqual(runtime.poll_once(), 4)
            self.assertEqual(len(self.manager.reads), 4, "fixed four-read tick")
            self.assertTrue(all(query["limit"] == 1 for query in self.manager.reads))
            self.assertEqual(len(self.manager.writes) - before, 4, "at most four writes per tick")
        self.assertTrue(all(row["error"]["code"] == "worker_interrupted" for row in self.manager.jobs.values()))
        fresh = _runtime(self.manager, self.path)
        _dispatch(fresh)
        _join(fresh)

    def test_atomic_create_update_faults_retention_and_no_product_after_publication_failure(self) -> None:
        self.manager.fault = "create"
        runtime = _runtime(self.manager, self.path)
        with self.assertRaises(jobs.PublicationError):
            _dispatch(runtime)
        self.assertEqual((self.manager.jobs, self.manager.requests), ({}, {}), "atomic create fault accepts nothing")
        for fault in ("processing", "progress", "completed"):
            manager = FakeManager()
            manager.fault = fault
            reached: list[str] = []

            def run(parameters: Any, stop: Any, progress: Any, reached: list[str] = reached) -> dict[str, object]:
                del parameters, stop
                reached.append("partial-ingest")
                progress({"fetched_count": 1})
                reached.append("after-progress")
                return {"fetched_count": 1}

            runtime = _runtime(manager, self.path, run)
            job_id = _dispatch(runtime)
            _join(runtime)
            self.assertIn(job_id, manager.requests, "accepted request survives failed publication")
            self.assertNotEqual(manager.jobs[job_id]["status"], "completed")
            if fault == "processing":
                self.assertEqual(reached, [], "no product effects after processing write failure")
            if fault == "progress":
                self.assertEqual(reached, ["partial-ingest"], "no effects after failed progress transaction")
            manager.fault = ""
            self.assertEqual(runtime.poll_once(), 1)
            self.assertEqual(manager.jobs[job_id]["error"]["code"], "worker_interrupted")

    def test_submission_exception_is_terminal_and_releases_only_after_publication(self) -> None:
        runtime = _runtime(self.manager, self.path)
        with patch.object(jobs.HeldBackfillLock, "submit", side_effect=RuntimeError("injected submission failure")):
            with self.assertRaises(RuntimeError):
                runtime.dispatch({}, {})
        self.assertEqual(self.manager.jobs["job-1"]["error"]["code"], "submission_failed")
        self.assertIn("job-1", self.manager.requests)
        held = jobs.try_lock(self.path)
        self.assertIsNotNone(held, "failed submission releases after synchronous terminal update")
        held.release()

    def test_status_is_read_only_raw_processing_is_not_measured_alive(self) -> None:
        job_id = self.manager.create_job(_NAME, "backfill_from_cloud", request_data={"notes": "request"})["data"]["job_id"]
        self.manager.jobs[job_id]["status"] = "processing"
        result = jobs.status(self.manager, _NAME, job_id)
        self.assertEqual(result["data"]["status"], "processing")
        self.assertIs(result["data"]["execution_measured"], False, "raw processing never claims live execution")
        self.assertEqual(self.manager.writes, [], "polling status never writes or resumes")
        with self.assertRaises(ValueError):
            jobs.status(self.manager, "other_provider", job_id)
        plugin = _plugin()
        self.assertNotIn("resume_backfill", plugin.get_edge_process_definitions())
        self.assertNotIn("cancel_backfill", plugin.get_edge_process_definitions())
        self.assertFalse(hasattr(plugin, "resume_backfill"), "no successor or automatic replay API")

    def test_lifecycle_tick_idempotence_stop_retains_held_worker(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def run(parameters: Any, stop: Any, progress: Any) -> dict[str, object]:
            del parameters, stop, progress
            entered.set()
            release.wait(4)
            return {"fetched_count": 1}

        runtime = _runtime(self.manager, self.path, run)
        with patch.object(jobs, "RECONCILE_INTERVAL_SECONDS", 0.01), patch.object(jobs, "STOP_JOIN_SECONDS", 0.01):
            _dispatch(runtime)
            self.assertTrue(entered.wait(2))
            self.assertTrue(runtime.start())
            tick = runtime._tick
            self.assertTrue(runtime.start())
            self.assertIs(tick, runtime._tick, "one lifecycle tick thread")
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(runtime.stop(), "stop does not report settled while effects held")
                self.assertIsNone(peer_jobs.try_lock(self.path), "stop timeout retains descriptor")
                self.assertEqual(runtime.dispatch({}, {})["data"]["status"], "stopping")
                self.assertFalse(runtime.start(), "restart cannot revive an unsettled worker")
            finally:
                release.set()
                _join(runtime)
            self.assertTrue(runtime.stop())
            self.assertTrue(runtime.stop(), "idempotent settled stop")
            self.assertEqual(self.manager.jobs["job-1"]["error"]["code"], "worker_interrupted")

    def test_plugins_and_colors_derive_one_canonical_lock_path(self) -> None:
        other_product = importlib.import_module(f"{_OTHER}.plugin")
        peer_cls = other_product.ClaudeCodeCloudSessionSourcePlugin if _OTHER.startswith("claude") else other_product.CodexCloudSessionSourcePlugin
        alias = self.home / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        first, second = _plugin(), peer_cls()
        first.orchestrator_ref = SimpleNamespace(APP_HOME=str(self.home))
        second.orchestrator_ref = SimpleNamespace(APP_HOME=str(alias))
        paths = [first._backfill_lock_path(), second._backfill_lock_path()]
        self.assertEqual(paths[0], paths[1], "both plugin/color paths canonicalize to the same cloud_backfill.lock")
        held = jobs.try_lock(paths[0])
        try:
            self.assertIsNone(peer_jobs.try_lock(paths[1]), "canonical path cross-vendor contention")
            self.assertEqual(os.fstat(held.descriptor).st_ino, paths[1].stat().st_ino)
        finally:
            held.release()

    def test_importer_persistence_precedes_terminal_and_forced_repair_recovers_exact_events(self) -> None:
        plugin = _plugin()
        repository = FakeRepository(plugin)
        client, credentials = _live_fake(plugin, self.home, self.manager, repository)
        repository.release.clear()
        with patch.object(plugin, "_build_walker_config", return_value=_config()), patch.object(product, "load_credentials", return_value=credentials), patch.object(product.httpx, "AsyncClient", side_effect=client):
            result = _handler(plugin)({"force": False}, {"session_id": "fake", "flow_id": "fake"})
            old_id = result["data"]["job_id"]
            try:
                self.assertTrue(repository.entered.wait(2), "real importer append_event boundary reached")
                self.assertEqual(self.manager.jobs[old_id]["status"], "processing", "terminal must wait for synchronous importer persistence")
                self.assertIsNone(peer_jobs.try_lock(self.path), "importer hold excludes second admission")
            finally:
                repository.release.set()
                _join(plugin._backfill)
            self.assertEqual(self.manager.jobs[old_id]["status"], "error")
            self.assertEqual(len(repository.events), 1, "partial persisted event retained after failure")
            old_request = self.manager.requests[old_id].copy()
            repository.fail_after_first = False
            result = _handler(plugin)({"force": True}, {"session_id": "fake", "flow_id": "fake"})
            new_id = result["data"]["job_id"]
            _join(plugin._backfill)
            self.assertNotEqual(old_id, new_id, "fresh forced repair is a new job")
            self.assertEqual(self.manager.jobs[new_id]["status"], "completed")
            self.assertEqual(self.manager.jobs[new_id]["result"]["fetched_count"], 2, "forced repair refetches complete multipage roster")
            self.assertEqual(len(repository.events), 4, "exact logical event set recovered without duplicates")
            self.assertEqual(self.manager.requests[old_id], old_request, "historical request retained unchanged")

    def test_walker_auth_rate_limit_item_failure_and_stop_boundaries(self) -> None:
        config = _config()
        credentials = walker.CodexCloudCredentials("fake", "fake-account", False) if _NAME.startswith("codex") else walker.ClaudeCodeCloudCredentials("fake")
        listing = walker.list_all_task_summaries if _NAME.startswith("codex") else walker.list_all_session_summaries
        error = walker.CodexCloudWalkerError if _NAME.startswith("codex") else walker.ClaudeCodeCloudWalkerError
        calls: list[str] = []

        async def exercise() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_http_fixture)) as client:
                summaries = await listing(client=client, config=config, creds=credentials)
                self.assertEqual(len(summaries), 2, "all roster pages retained")
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401))) as client:
                with self.assertRaises(error):
                    await listing(client=client, config=config, creds=credentials)
            attempts = 0

            def retry(request: httpx.Request) -> httpx.Response:
                nonlocal attempts
                attempts += 1
                return httpx.Response(429) if attempts == 1 else _http_fixture(request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(retry)) as client:
                report = walker.WalkerReport()
                await walker.fetch_and_dispatch_concurrent(summaries=[{"id": "cse_alpha", "updated_at": "2026-10-18T00:00:00Z"}], config=config, creds=credentials, dispatch_callable=calls.append, client=client, report=report)
                self.assertEqual(report.fetched_count, 1, "429 retry succeeds")
                self.assertGreaterEqual(attempts, 2)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
                report = walker.WalkerReport()
                await walker.fetch_and_dispatch_concurrent(summaries=[{"id": "bad"}], config=config, creds=credentials, dispatch_callable=calls.append, client=client, report=report)
                self.assertEqual(report.errored_count, 1, "per-item failures retained")
            from dataclasses import replace

            stopped = replace(config, should_stop=lambda: True)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.fail("HTTP after stop"))) as client:
                with self.assertRaises(error):
                    await listing(client=client, config=stopped, creds=credentials)

        asyncio.run(exercise())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        _child()
    else:
        unittest.main()
