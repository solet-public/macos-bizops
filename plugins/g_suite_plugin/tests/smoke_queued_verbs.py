#!/usr/bin/env python3
"""Fault-injection smoke: six g_suite verbs return a job handle at once and do their vendor work in the plugin's worker.

test_connection, sheets_export, docs_export, slides_export, start_interface and
disconnect_account are submitted through ActionFactory and executed by the real
ActionProcessor (the queued-action caller path) with connected accounts bound.
Every fake vendor transport waits 300 ms and records the thread that used it;
building a Google client counts as a use too.

Each verb must return ``{job_id, status: queued}`` in under 50 ms with no
transport use on the calling thread. The plugin's own worker thread
(``start_services``) then does the vendor work, and the finished job must hold
exactly the outcome the verb returned inline before this change on the same
fixture: fixtures/queued_verbs_base_results.json, recorded by ``observe_all()``
on the pre-change source. A failing vendor call must end the job in error with
the same ``{code, message}``; an invalid parameter must still fail at once,
with no job and no transport use.

Run:
    SOLET_NAME=<name> .venv/bin/python plugins/g_suite_plugin/tests/smoke_queued_verbs.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor, QueuedActionProtocol  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.plugins.action_discovery import discover_actions  # noqa: E402
from g_suite_plugin import account_processes  # noqa: E402  # pyright: ignore[reportMissingImports]
from g_suite_plugin.oauth.http_server import OAuthServerStartError  # noqa: E402  # pyright: ignore[reportMissingImports]
from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402  # pyright: ignore[reportMissingImports]
from googleapiclient.errors import HttpError  # noqa: E402
from gsuite_fakes import FakeJobs, FakeVault, connect_accounts, job_envelope  # noqa: E402  # pyright: ignore[reportMissingImports]

DELAY_S = 0.3
FAST_S = 0.05
WORKER_TIMEOUT_S = 15.0
WORKER_THREAD = "gsuite-async-jobs"
SESSION = "queued-verbs-session"
FLOW = "flow-queued-verbs"
OWNER = "owner@corp.example"
HOME = "home@gmail.com"
BASE_RESULTS = Path(__file__).resolve().parent / "fixtures" / "queued_verbs_base_results.json"
VERBS = ("test_connection", "sheets_export", "docs_export", "slides_export", "start_interface", "disconnect_account")

# Arguments per verb: SUCCESS also drives the vendor-failure mode; INVALID must fail at dispatch.
SUCCESS: dict[str, dict[str, Any]] = {
    "test_connection": {"account": "home"},
    "sheets_export": {"id": "sheet-1", "format": "xlsx"},
    "docs_export": {"id": "doc-1", "format": "docx", "account": OWNER},
    "slides_export": {"id": "deck-1"},
    "start_interface": {"host": "127.0.0.1", "port": 18765},
    "disconnect_account": {"account": "home"},
}
INVALID: dict[str, dict[str, Any]] = {
    "test_connection": {"account": "nobody@corp.example"},
    "sheets_export": {"id": "sheet-1", "format": "pdf"},
    "docs_export": {"id": "doc-1", "format": "pptx"},
    "slides_export": {"id": "deck-1", "account": "nobody@corp.example"},
    "start_interface": {"port": "not-a-port"},
    "disconnect_account": {"account": "nobody@corp.example"},
}


def _http_error(status: int, message: str) -> HttpError:
    return HttpError(cast(Any, SimpleNamespace(status=status, reason=message)), json.dumps({"error": {"code": status, "message": message}}).encode())


# What the vendor boundary raises in the vendor-failure mode.
FAILURES: dict[str, Callable[[], BaseException]] = {
    "test_connection": lambda: _http_error(401, "Request had invalid authentication credentials."),
    "sheets_export": lambda: _http_error(404, "File not found: sheet-1."),
    "docs_export": lambda: _http_error(403, "The user does not have sufficient permissions for file doc-1."),
    "slides_export": lambda: _http_error(429, "Rate limit exceeded."),
    "start_interface": lambda: OAuthServerStartError("Google OAuth callback server could not bind 127.0.0.1:18765: [Errno 48] Address already in use"),
    "disconnect_account": lambda: RuntimeError("revoke endpoint unavailable"),
}


@dataclass
class Transport:
    """One fake vendor boundary: records each use and its thread; ``answer`` also waits, then answers or raises."""

    failure: BaseException | None = None
    uses: list[tuple[str, str]] = field(default_factory=lambda: [])

    def touch(self, what: str) -> None:
        self.uses.append((what, threading.current_thread().name))

    def answer[T](self, what: str, value: T) -> T:
        self.touch(what)
        time.sleep(DELAY_S)
        if self.failure is not None:
            raise self.failure
        return value


def _factory(transport: Transport) -> Any:
    """A Google service factory whose Gmail and Drive clients answer through ``transport``."""

    def get_profile(*, userId: str) -> Any:
        return SimpleNamespace(execute=lambda: transport.answer(f"gmail getProfile {userId}", {"emailAddress": account_processes.ACTIVE_ACCOUNT.get()}))

    def export_media(*, fileId: str, mimeType: str) -> Any:
        return SimpleNamespace(execute=lambda: transport.answer(f"drive export_media {fileId}", f"{fileId} as {mimeType}".encode()))

    def gmail() -> Any:
        transport.touch("build gmail")
        return SimpleNamespace(users=lambda: SimpleNamespace(getProfile=get_profile))

    def drive() -> Any:
        transport.touch("build drive")
        return SimpleNamespace(files=lambda: SimpleNamespace(export_media=export_media))

    return SimpleNamespace(gmail=gmail, drive=drive)


@dataclass
class Blobs:
    """Blob storage that records what each export wrote."""

    stored: list[dict[str, str]] = field(default_factory=lambda: [])

    def store_blob(self, namespace: str, content: bytes, metadata: dict[str, str]) -> dict[str, Any]:
        self.stored.append({"namespace": namespace, "content": content.decode(), **metadata})
        return {"action_status": "completed", "data": {"blob_id": f"blob-{len(self.stored)}"}}


@dataclass
class CallbackServer:
    """The OAuth callback server: binding is the vendor-like boundary that waits."""

    transport: Transport

    def start(self, **kwargs: Any) -> int:
        port = int(kwargs["preferred_port"] or 18000)
        return self.transport.answer(f"bind {kwargs['host']}:{port}", port)

    def stop(self) -> None:
        self.transport.touch("stop")


@dataclass
class Rig:
    plugin: GSuitePlugin
    jobs: FakeJobs
    transport: Transport
    blobs: Blobs


def rig(failure: BaseException | None) -> Rig:
    """A plugin with two connected accounts (owner default, home aliased) and fake vendor boundaries."""
    plugin = GSuitePlugin()
    plugin.initialize({})
    accounts = connect_accounts(plugin, [OWNER, HOME], FakeVault())
    accounts.upsert(HOME, alias="home", set_default=False)
    transport = Transport(failure)
    blobs = Blobs()
    jobs = FakeJobs()
    plugin._require_factory = lambda: _factory(transport)  # type: ignore[method-assign]  # noqa: SLF001
    plugin._oauth_server = cast(Any, CallbackServer(transport))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    plugin._blob_storage_service = blobs  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    plugin._deferred.manager = jobs  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    return Rig(plugin, jobs, transport, blobs)


@dataclass
class Caller:
    """The queued-action caller path: ActionFactory submission, then ActionProcessor execution."""

    factory: ActionFactory
    processor: ActionProcessor
    recorder: Mock


def caller(plugin: GSuitePlugin) -> Caller:
    registry: dict[str, object] = {"processes": {f"plugin::g_suite_plugin::{action.name}": action.to_dict() for action in discover_actions(plugin)}}
    state_service = Mock()
    state_service.generate_unique_string.return_value = {"action_status": "completed", "data": {"random_string": "queued-verbs"}}
    recorder = Mock()
    recorder.store_action_event.return_value = "act-queued-verbs"
    template_engine = Mock()
    template_engine.resolve_templates.side_effect = lambda definition, _context: definition
    factory = ActionFactory(process_registry=registry, template_engine=template_engine, state_service=state_service, action_event_recorder=recorder)
    manager = Mock()
    manager.get_plugin.return_value = plugin
    contexts = ExecutionContextManager()
    contexts.create_context(FLOW)
    return Caller(factory, ActionProcessor(plugin_manager=manager, process_registry=registry, execution_context_manager=contexts), recorder)


def submit(path: Caller, verb: str, params: dict[str, Any]) -> tuple[dict[str, Any], float]:
    """Submit one action and execute it as the queue does; return the response and the execution wall time."""
    action_id = path.factory.submit_action_definition({"process_key": f"plugin::g_suite_plugin::{verb}", "arguments": params, "flow_id": FLOW})
    saved = path.recorder.store_action_event.call_args.args[0]
    queued = SimpleNamespace(
        id=action_id,
        process_key=saved["process_key"],
        parameters=json.dumps(saved["parameters"]),
        created_at=saved["timestamp"],
        session_id=SESSION,
        flow_id=saved["flow_id"],
        context_id=None,
        result_processor=None,
        template_namespace=None,
        flow_token_id=None,
        source_plugin=None,
    )
    started = time.perf_counter()
    response = path.processor.execute_action(cast(QueuedActionProtocol, queued))
    return cast(dict[str, Any], response), time.perf_counter() - started


@dataclass
class Observation:
    response: dict[str, Any]
    dispatch_s: float
    dispatch_uses: list[tuple[str, str]]
    queued_requests: list[dict[str, Any]]
    outcome: dict[str, Any]
    uses: list[tuple[str, str]]


def _drain(r: Rig, job_id: str) -> dict[str, Any]:
    """Run the plugin's own worker thread until the job finishes, then stop it."""
    asyncio.run(r.plugin.start_services())
    try:
        row = r.jobs.wait_terminal(job_id, WORKER_TIMEOUT_S)
    finally:
        asyncio.run(r.plugin.stop_services())
    return job_envelope(row)


def observe(verb: str, params: dict[str, Any], failure: BaseException | None = None) -> Observation:
    """Call ``verb`` through the caller path and, when it queued a job, run that job in the plugin's worker."""
    r = rig(failure)
    path = caller(r.plugin)
    with patch.object(account_processes, "revoke_token", side_effect=lambda _token: r.transport.answer("revoke", "revoked")):
        response, dispatch_s = submit(path, verb, params)
        dispatch_uses = list(r.transport.uses)
        data = response.get("data")
        job_id = data.get("job_id") if response.get("success") and isinstance(data, dict) else None
        envelope = _drain(r, job_id) if isinstance(job_id, str) else response
    error = envelope.get("error")
    outcome: dict[str, Any] = {"error": error} if error else {"result": envelope.get("data")}
    outcome["stored_blobs"] = r.blobs.stored
    outcome["accounts_after"] = [record.email for record in r.plugin._require_accounts().records()]  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    requests = [r.jobs.requests[job] for job in sorted(r.jobs.requests)]
    return Observation(response, dispatch_s, dispatch_uses, requests, outcome, list(r.transport.uses))


def observe_all() -> dict[str, dict[str, Any]]:
    """Every verb's outcome per mode; run on the pre-change source this produced BASE_RESULTS."""
    return {
        verb: {
            "success": observe(verb, SUCCESS[verb]).outcome,
            "vendor_failure": observe(verb, SUCCESS[verb], FAILURES[verb]()).outcome,
            "invalid": observe(verb, INVALID[verb]).outcome,
        }
        for verb in VERBS
    }


class QueuedVerbsSmoke(unittest.TestCase):
    base: dict[str, dict[str, Any]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.base = json.loads(BASE_RESULTS.read_text())

    def test_dispatch_returns_a_job_handle_at_once_and_the_worker_reproduces_the_inline_outcome(self) -> None:
        for verb in VERBS:
            for mode, failure in (("success", None), ("vendor_failure", FAILURES[verb]())):
                with self.subTest(verb=verb, mode=mode):
                    seen = observe(verb, SUCCESS[verb], failure)
                    print(f"{verb} {mode}: dispatch {seen.dispatch_s * 1000:.2f} ms; calling-thread transport uses {seen.dispatch_uses}; worker uses {seen.uses}", flush=True)
                    self.assertTrue(seen.response.get("success"), seen.response)
                    self.assertEqual(seen.response["data"], {"job_id": "job-1", "status": "queued"})
                    self.assertLess(seen.dispatch_s, FAST_S)
                    self.assertEqual(seen.dispatch_uses, [], "the vendor transport was used on the calling thread")
                    self.assertTrue(seen.uses, "the worker never reached the vendor transport")
                    self.assertTrue(all(thread == WORKER_THREAD for _, thread in seen.uses), seen.uses)
                    self.assertEqual([(request["verb"], request["params"]) for request in seen.queued_requests], [(verb, SUCCESS[verb])])
                    self.assertEqual(seen.outcome, self.base[verb][mode])

    def test_an_invalid_parameter_still_fails_at_once_without_a_job(self) -> None:
        for verb in VERBS:
            with self.subTest(verb=verb):
                seen = observe(verb, INVALID[verb])
                print(f"{verb} invalid: dispatch {seen.dispatch_s * 1000:.2f} ms; outcome {seen.outcome}", flush=True)
                self.assertFalse(seen.response.get("success"), seen.response)
                self.assertLess(seen.dispatch_s, FAST_S)
                self.assertEqual(seen.queued_requests, [])
                self.assertEqual(seen.uses, [])
                self.assertEqual(seen.outcome, self.base[verb]["invalid"])

    def test_registered_metadata_declares_the_queued_handle(self) -> None:
        actions = {action.name: action.to_dict() for action in discover_actions(GSuitePlugin())}
        for verb in VERBS:
            with self.subTest(verb=verb):
                metadata = actions[verb]
                self.assertIs(metadata["is_async"], True)
                schema = cast(dict[str, Any], metadata["return_value_schema"])
                self.assertEqual(set(schema["properties"]), {"job_id", "status"})
                self.assertIn("service_interface::job_service::get_job", schema["description"])


if __name__ == "__main__":
    unittest.main()
