#!/usr/bin/env python3
"""Born-async Marketo writes and diagnostics: a job handle in milliseconds, vendor work in the worker.

The corrected fast-return census (iss_59840e08, Change chg_fabf151c) measured
eight verbs calling Marketo on the dispatch thread: create_or_update_leads,
delete_leads, merge_leads, trigger_campaign, add_leads_to_list,
remove_leads_from_list, test_connection and check_setup. This smoke drives
each through the platform's own plugin call shape —
``ActionProcessor._execute_plugin_method`` invokes the verb with
``params=``/``state=`` built from a queued action — against a MarketoPlugin
that went through ``initialize`` and ``prepare_for_readiness`` with its
``marketo_instance`` address-book entry bound. The real AsyncJobManager keeps
the job over the in-memory job ledger fixture, the plugin's real worker
thread runs the job, and the real MarketoClient talks to an
``httpx.MockTransport`` that takes 300 ms per call and records the thread
that called it.

Fault-injection cases: with that 300 ms fake transport each verb returns
``{job_id, status: queued}`` in under 50 ms and the fake is not touched on
the calling thread; the worker then completes the job and
check_marketo_job_status returns the result the verb returned before this
change (``EXPECTED_*`` below are the base-blob a0c1d06db outputs for the same
fixtures); a failing vendor call ends the job in error with the same
``{code, message}``; an invalid parameter is still refused immediately, with
no job and no transport call.

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/marketo_plugin/tests/born_async_verbs_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "core" / "actions"))

from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import QueuedAction  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph  # noqa: E402
from async_job_fixture_support import JobState  # noqa: E402  # pyright: ignore[reportMissingImports]
from marketo_plugin.app_config import MarketoInstanceConfig, _missing_entry_message  # noqa: E402  # pyright: ignore[reportPrivateUsage]
from marketo_plugin.constants import CHECK_SETUP_PROBES, CHECK_SETUP_UNVERIFIED_WRITE_VERBS, IDENTITY_TOKEN_PATH, LEADS_DESCRIBE_PATH  # noqa: E402
from marketo_plugin.http_client import MarketoClient  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402

BASE_URL = "https://tenant.mktorest.test"
CLIENT_ID = "test-client"
CONFIG = MarketoInstanceConfig(base_url=BASE_URL, client_id=CLIENT_ID, client_secret="fixture-secret")
ENTRIES = [
    {"field_type": "base_url", "value": BASE_URL},
    {"field_type": "client_id", "value": CLIENT_ID},
    {"field_type": "client_secret", "value": "fixture-secret"},
]
VENDOR_DELAY_SECONDS = 0.3
FAST_RETURN_MS = 50.0
JOB_DEADLINE_SECONDS = 20.0
VERBS = (
    "create_or_update_leads",
    "delete_leads",
    "merge_leads",
    "trigger_campaign",
    "add_leads_to_list",
    "remove_leads_from_list",
    "test_connection",
    "check_setup",
)

Vendor = Callable[[httpx.Request], httpx.Response]


def _envelope(result: list[dict[str, Any]] | None = None, request_id: str = "e42b#1") -> dict[str, Any]:
    return {"requestId": request_id, "success": True, **({"result": result} if result is not None else {})}


def _denied(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"requestId": "d#1", "success": False, "errors": [{"code": "603", "message": "Access denied"}]})


def _lead_vendor(request: httpx.Request) -> httpx.Response:
    """Describe marks only id read-only; the write echoes per-record statuses."""
    if request.url.path == LEADS_DESCRIBE_PATH:
        return httpx.Response(200, json=_envelope([{"rest": {"name": "id", "readOnly": True}}, {"rest": {"name": "email", "readOnly": False}}, {"rest": {"name": "firstName", "readOnly": False}}]))
    return httpx.Response(200, json=_envelope([{"seq": 0, "id": 101, "status": "created"}, {"seq": 1, "id": 102, "status": "updated"}]))


def _read_only_vendor(request: httpx.Request) -> httpx.Response:
    if request.url.path == LEADS_DESCRIBE_PATH:
        return httpx.Response(200, json=_envelope([{"rest": {"name": "leadScore", "readOnly": True}}]))
    raise AssertionError(f"a refused batch must not reach the write: {request.method} {request.url.path}")


def _setup_vendor(request: httpx.Request) -> httpx.Response:
    """Every read probe succeeds except the campaign listing, which the Role denies."""
    if request.url.path == "/rest/v1/campaigns.json":
        return _denied(request)
    return httpx.Response(200, json=_envelope([]))


def _batch(path_status: list[dict[str, Any]]) -> Vendor:
    return lambda _request: httpx.Response(200, json=_envelope(path_status))


@dataclass(frozen=True)
class Case:
    verb: str
    params: dict[str, Any]
    vendor: Vendor
    expected: dict[str, Any]
    calls: tuple[tuple[str, str], ...]
    token_status: int = 200
    address_book_bound: bool = True


_CHECKS = [
    {"capability": label, "verb": verb, "status": "ok"}
    if verb != "list_campaigns"
    else {
        "capability": label,
        "verb": verb,
        "status": "failed",
        "error_code": "marketo.permission_denied",
        "error_message": "Marketo rejected the request (code 603): Access denied",
        "guidance": f"Add '{permission}' under Admin > Users & Roles > Roles > <the API role> > Access API, then re-run check_setup.",
    }
    for verb, label, permission in CHECK_SETUP_PROBES
]

SUCCESS_CASES: tuple[Case, ...] = (
    Case(
        "create_or_update_leads",
        {"records": [{"email": "a@example.com", "firstName": "Grace"}, {"id": 102, "firstName": "Bo"}], "action": "createOrUpdate", "lookup_field": "email"},
        _lead_vendor,
        {"results": [{"seq": 0, "id": 101, "status": "created"}, {"seq": 1, "id": 102, "status": "updated"}], "row_count": 2, "tallies": {"created": 1, "updated": 1}},
        (("GET", LEADS_DESCRIBE_PATH), ("POST", "/rest/v1/leads.json")),
    ),
    Case(
        "delete_leads",
        {"lead_ids": [101, 102]},
        _batch([{"id": 101, "status": "deleted"}, {"id": 102, "status": "skipped", "reasons": [{"code": "1004", "message": "Lead not found"}]}]),
        {"results": [{"id": 101, "status": "deleted"}, {"id": 102, "status": "skipped", "reasons": [{"code": "1004", "message": "Lead not found"}]}], "row_count": 2, "tallies": {"deleted": 1, "skipped": 1}},
        (("POST", "/rest/v1/leads/delete.json"),),
    ),
    Case(
        "merge_leads",
        {"winning_lead_id": "101", "losing_lead_ids": [102, 103], "merge_in_crm": False},
        lambda _request: httpx.Response(200, json=_envelope(request_id="m#1")),
        {"success": True, "request_id": "m#1"},
        (("POST", "/rest/v1/leads/101/merge.json"),),
    ),
    Case(
        "trigger_campaign",
        {"campaign_id": "42", "lead_ids": [101], "tokens": [{"name": "{{my.token}}", "value": "x"}]},
        lambda _request: httpx.Response(200, json=_envelope([{"id": 42}], request_id="t#1")),
        {"success": True, "request_id": "t#1"},
        (("POST", "/rest/v1/campaigns/42/trigger.json"),),
    ),
    Case(
        "add_leads_to_list",
        {"list_id": "9", "lead_ids": [101, 102]},
        _batch([{"id": 101, "status": "added"}, {"id": 102, "status": "skipped"}]),
        {"results": [{"id": 101, "status": "added"}, {"id": 102, "status": "skipped"}], "row_count": 2, "tallies": {"added": 1, "skipped": 1}},
        (("POST", "/rest/v1/lists/9/leads.json"),),
    ),
    Case(
        "remove_leads_from_list",
        {"list_id": "9", "lead_ids": [101]},
        _batch([{"id": 101, "status": "removed"}]),
        {"results": [{"id": 101, "status": "removed"}], "row_count": 1, "tallies": {"removed": 1}},
        (("DELETE", "/rest/v1/lists/9/leads.json"),),
    ),
    Case(
        "test_connection",
        {},
        _denied,
        {"ok": True, "base_url": BASE_URL, "client_id": CLIENT_ID},
        (),
    ),
    Case(
        "check_setup",
        {},
        _setup_vendor,
        {
            "reads_verified": False,
            "checks": _CHECKS,
            "writes_unverified": list(CHECK_SETUP_UNVERIFIED_WRITE_VERBS),
            "writes_unverified_note": (
                "These verbs perform a write/execute action, so their Access API permission cannot be probed without actually running them. A missing permission surfaces as marketo.permission_denied on first real use — re-run check_setup after fixing it to confirm the reads still pass, then retry the write."
            ),
        },
        tuple(("GET", path) for path in ("/rest/v1/leads/describe.json", "/rest/v1/leads.json", "/rest/v1/activities/types.json", "/rest/v1/stats/usage.json", "/rest/v1/campaigns.json", "/rest/v1/lists.json")),
    ),
)

_DENIED_ERROR = {"code": "marketo.permission_denied", "message": "Marketo rejected the request (code 603): Access denied"}
_AUTH_ERROR = {"code": "marketo.auth_failed", "message": "Marketo OAuth token request failed."}

FAILURE_CASES: tuple[Case, ...] = (
    *(Case(case.verb, case.params, _denied, _DENIED_ERROR, case.calls[:1]) for case in SUCCESS_CASES[:6]),
    Case(
        "create_or_update_leads",
        {"records": [{"email": "a@example.com", "leadScore": 7}]},
        _read_only_vendor,
        {"code": "marketo.invalid_params", "message": "Marketo REST read-only fields cannot be written: leadScore (records 1). Remove every listed field and retry; no write was attempted."},
        (("GET", LEADS_DESCRIBE_PATH),),
    ),
    Case("test_connection", {}, _denied, _AUTH_ERROR, (), token_status=401),
    Case("check_setup", {}, _setup_vendor, _AUTH_ERROR, (), token_status=401),
    Case("test_connection", {}, _denied, {"code": "marketo.not_configured", "message": _missing_entry_message()}, (), address_book_bound=False),
)

INVALID_CASES: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("create_or_update_leads", {"records": []}, "'records' is required and must be a non-empty list"),
    ("create_or_update_leads", {"records": [{"email": "a@example.com"}], "action": "upsert"}, "'action' must be one of ['createDuplicate', 'createOnly', 'createOrUpdate', 'updateOnly']"),
    ("delete_leads", {"lead_ids": list(range(301))}, "'lead_ids' has 301 entries; the Marketo API caps this call at 300"),
    ("merge_leads", {"winning_lead_id": "1", "losing_lead_ids": [2, 3], "merge_in_crm": True}, "'losing_lead_ids' has 2 entries; the Marketo API caps this call at 1"),
    ("trigger_campaign", {"campaign_id": "42", "lead_ids": [1], "tokens": [{"name": "x"}]}, "every entry in 'tokens' must be an object with 'name' and 'value'"),
    ("add_leads_to_list", {"lead_ids": [1]}, "'list_id' is required and must be a non-empty string"),
    ("remove_leads_from_list", {"list_id": "9", "lead_ids": []}, "'lead_ids' is required and must be a non-empty list"),
)


@dataclass
class FixtureAction(QueuedAction):
    """A queued action as the poller hands it over; no plugin originated it."""

    source_plugin: str | None = None


class LedgerState(JobState):  # pyright: ignore[reportUntypedBaseClass]
    """The in-memory job ledger plus the created_at the database stamps by default.

    The plugin's worker lists queued jobs ordered by created_at, which the
    in-memory fixture does not stamp on its own.
    """

    def write_state(self, namespace: str, data: dict[str, Any], calling_service: str | None = None, calling_namespace: str | None = None) -> dict[str, Any]:
        record = {"created_at": datetime.now(UTC).isoformat(), **data["record"]}
        result: dict[str, Any] = super().write_state(namespace, {**data, "record": record}, calling_service, calling_namespace)
        return result


@dataclass
class Transport:
    """A vendor that answers after VENDOR_DELAY_SECONDS and records the calling thread."""

    vendor: Vendor
    token_status: int = 200
    touches: list[tuple[int, str, str]] = field(default_factory=lambda: [])
    requests: list[httpx.Request] = field(default_factory=lambda: [])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.touches.append((threading.get_ident(), request.method, request.url.path))
        time.sleep(VENDOR_DELAY_SECONDS)
        if request.url.path == IDENTITY_TOKEN_PATH:
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": "minted", "token_type": "bearer", "expires_in": 3599, "scope": "api@tenant"})
        self.requests.append(request)
        return self.vendor(request)


class AddressBook:
    """The address-book service as AppConfigLoader reads it, secret already swapped in."""

    def __init__(self, bound: bool) -> None:
        self.bound = bound

    def resolve_with_secrets(self, name: str) -> dict[str, Any]:
        if not self.bound or name != "marketo_instance":
            return {"action_status": "error", "error": {"message": f"no entry named {name}"}}
        return {"action_status": "completed", "data": {"entries": [dict(entry) for entry in ENTRIES]}}


@dataclass
class Harness:
    plugin: MarketoPlugin
    transport: Transport
    state: Any
    processor: ActionProcessor

    def call(self, verb: str, params: dict[str, Any], *, session_id: str | None = "session-1", flow_id: str | None = "flow-1") -> tuple[dict[str, Any], float]:
        """One verb through the platform's plugin call shape; returns (response, wall ms)."""
        action = FixtureAction(
            id=f"action-{verb}",
            process_key=f"plugin::marketo_plugin::{verb}",
            parameters=json.dumps(params),
            notes="born-async smoke",
            created_at="2026-11-08T00:00:00Z",
            session_id=session_id,
            flow_id=flow_id,
        )
        started = time.perf_counter()
        response = self.processor._execute_plugin_method(self.plugin, verb, params, action)  # pyright: ignore[reportPrivateUsage]
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert isinstance(response, dict)
        return response, elapsed_ms

    def start_worker(self) -> int:
        self.plugin.WORKER_POLL_INTERVAL = 0.02
        asyncio.run(self.plugin.start_services())
        worker = self.plugin._worker_thread  # pyright: ignore[reportPrivateUsage]
        assert worker is not None and worker.ident is not None
        return worker.ident

    def wait_for(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + JOB_DEADLINE_SECONDS
        while True:
            polled, _ = self.call("check_marketo_job_status", {"job_id": job_id})
            data = polled["data"]
            if data["status"] in {"completed", "error", "cancelled"}:
                return data
            if time.monotonic() > deadline:
                raise AssertionError(f"job {job_id} still {data['status']} after {JOB_DEADLINE_SECONDS}s")
            time.sleep(0.02)

    def jobs(self) -> list[dict[str, Any]]:
        return list(self.state.rows.get("job", []))


@contextmanager
def harness(case: Case) -> Generator[Harness]:
    state = LedgerState()
    state.rows["flows"] = [{"id": "flow-1", "status": "active", "trigger_type": "bridge_process_call"}]
    manager = AsyncJobManager(state, FlowRuntimeGraph(state))
    address_book = AddressBook(case.address_book_bound)
    plugin = MarketoPlugin()
    plugin.initialize({})
    plugin.orchestrator_ref = SimpleNamespace(async_job_manager=manager, get_service=lambda name: address_book if name == "address_book_service" else None)
    plugin.prepare_for_readiness()
    transport = Transport(case.vendor, case.token_status)
    client = MarketoClient(CONFIG, timeout_seconds=5.0)
    client._http.close()  # pyright: ignore[reportPrivateUsage]
    client._http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(transport.handle))  # pyright: ignore[reportPrivateUsage]
    plugin._client = client  # pyright: ignore[reportPrivateUsage]
    try:
        yield Harness(plugin, transport, state, ActionProcessor(PluginManager()))
    finally:
        asyncio.run(plugin.stop_services())
        client.close()


class BornAsyncVerbTests(unittest.TestCase):
    def _dispatch_and_finish(self, case: Case) -> tuple[Harness, dict[str, Any], float, int]:
        with harness(case) as run:
            calling_thread = threading.get_ident()
            response, elapsed_ms = run.call(case.verb, case.params)
            self.assertEqual(response["action_status"], "completed", response)
            self.assertIsNone(response["error"], response)
            self.assertEqual(set(response["data"]), {"job_id", "status"}, response)
            self.assertEqual(response["data"]["status"], "queued")
            self.assertEqual(run.transport.touches, [], "dispatch touched the vendor transport")
            self.assertEqual([job["provider_name"] for job in run.jobs()], [f"marketo_plugin.{case.verb}"])
            worker_thread = run.start_worker()
            finished = run.wait_for(response["data"]["job_id"])
            touch_threads = {thread for thread, _method, _path in run.transport.touches}
            self.assertNotIn(calling_thread, touch_threads, "a vendor call ran on the calling thread")
            self.assertTrue(touch_threads <= {worker_thread}, (touch_threads, worker_thread))
            self.assertEqual(tuple((r.method, r.url.path) for r in run.transport.requests), case.calls)
            return run, finished, elapsed_ms, worker_thread

    def test_each_verb_returns_a_handle_fast_and_the_worker_produces_the_base_result(self) -> None:
        timings: dict[str, float] = {}
        for case in SUCCESS_CASES:
            with self.subTest(verb=case.verb):
                _run, finished, elapsed_ms, _worker = self._dispatch_and_finish(case)
                timings[case.verb] = elapsed_ms
                self.assertLess(elapsed_ms, FAST_RETURN_MS, f"{case.verb} dispatch took {elapsed_ms:.2f} ms")
                self.assertEqual(finished["status"], "completed", finished)
                self.assertIsNone(finished["error"])
                self.assertEqual(finished["result"], case.expected)
        self.assertEqual(set(timings), set(VERBS))
        print("dispatch wall ms: " + ", ".join(f"{verb}={ms:.3f}" for verb, ms in timings.items()))

    def test_vendor_request_bytes_are_those_the_inline_verbs_sent(self) -> None:
        sent: dict[str, httpx.Request] = {}
        for case in SUCCESS_CASES[:6]:
            run, _finished, _elapsed, _worker = self._dispatch_and_finish(case)
            sent[case.verb] = run.transport.requests[-1]
        self.assertEqual(json.loads(sent["create_or_update_leads"].content), {"action": "createOrUpdate", "input": SUCCESS_CASES[0].params["records"], "lookupField": "email"})
        self.assertEqual(json.loads(sent["delete_leads"].content), {"input": [{"id": 101}, {"id": 102}]})
        self.assertEqual(dict(sent["merge_leads"].url.params), {"leadIds": "102,103", "mergeInCRM": "false"})
        self.assertEqual(json.loads(sent["trigger_campaign"].content), {"input": {"leads": [{"id": 101}], "tokens": [{"name": "{{my.token}}", "value": "x"}]}})
        self.assertEqual(json.loads(sent["add_leads_to_list"].content), {"input": [{"id": 101}, {"id": 102}]})
        self.assertEqual(json.loads(sent["remove_leads_from_list"].content), {"input": [{"id": 101}]})

    def test_a_failing_vendor_call_ends_the_job_in_error_with_the_base_error(self) -> None:
        for case in FAILURE_CASES:
            with self.subTest(verb=case.verb, error=case.expected["code"]):
                _run, finished, elapsed_ms, _worker = self._dispatch_and_finish(case)
                self.assertLess(elapsed_ms, FAST_RETURN_MS)
                self.assertEqual(finished["status"], "error", finished)
                self.assertIsNone(finished["result"])
                self.assertEqual(finished["error"], case.expected)

    def test_an_invalid_parameter_is_refused_immediately_with_no_job(self) -> None:
        for verb, params, message in INVALID_CASES:
            with self.subTest(verb=verb, message=message), harness(Case(verb, params, _denied, {}, ())) as run:
                response, elapsed_ms = run.call(verb, params)
                self.assertLess(elapsed_ms, FAST_RETURN_MS)
                self.assertEqual(response["action_status"], "error", response)
                self.assertEqual(response["error"], {"code": "marketo.invalid_params", "message": message})
                self.assertEqual(run.jobs(), [])
                self.assertEqual(run.transport.touches, [])

    def test_a_dispatch_without_flow_context_is_refused_before_any_job(self) -> None:
        for verb in ("test_connection", "check_setup"):
            with self.subTest(verb=verb), harness(Case(verb, {}, _denied, {}, ())) as run:
                response, _elapsed = run.call(verb, {}, session_id=None)
                self.assertEqual(response["error"], {"code": "marketo.invalid_params", "message": "session_id missing from state context"})
                self.assertEqual(run.jobs(), [])
                self.assertEqual(run.transport.touches, [])

    def test_job_completion_instructions_match_what_each_result_carries(self) -> None:
        expected_phrase = {
            "create_or_update_leads": "per-record results",
            "delete_leads": "per-record results",
            "add_leads_to_list": "per-record results",
            "remove_leads_from_list": "per-record results",
            "merge_leads": "request_id",
            "trigger_campaign": "request_id",
            "test_connection": "diagnostic report",
            "check_setup": "diagnostic report",
        }
        for case in SUCCESS_CASES:
            with self.subTest(verb=case.verb), harness(case) as run:
                run.call(case.verb, case.params)
                metadata = json.loads(run.jobs()[0]["metadata"]) if isinstance(run.jobs()[0]["metadata"], str) else run.jobs()[0]["metadata"]
                handlers = metadata["completion_handlers"]
                result_text = json.dumps(handlers["result"]["template"])
                self.assertIn(expected_phrase[case.verb], result_text)
                self.assertNotIn("TSV file path", result_text)
                error_text = json.dumps(handlers["error"]["template"])
                writes = case.verb not in {"test_connection", "check_setup"}
                self.assertEqual("read the records back before any retry" in error_text, writes)

    def test_each_verb_registers_the_job_handle_in_all_three_artifacts(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions
        from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator

        plugin = MarketoPlugin()
        actions = {action.name: action for action in discover_actions(plugin)}
        PluginRegistrationValidator().validate_edge_process_provider("marketo_plugin", plugin, list(actions.values()))
        edges = plugin.get_edge_process_definitions()
        for verb in VERBS:
            with self.subTest(verb=verb):
                schema = actions[verb].return_value_schema
                assert schema is not None
                self.assertEqual(set(schema.properties or {}), {"job_id", "status"})
                self.assertIn("check_marketo_job_status", actions[verb].description)
                self.assertIn(verb, edges)
                kb = json.loads((REPO_ROOT / "plugins/marketo_plugin/knowledge_base/processes" / f"{verb}.json").read_text(encoding="utf-8"))
                self.assertEqual(kb["process_key"], f"plugin::marketo_plugin::{verb}")
                self.assertIn("check_marketo_job_status", kb["description"])
                self.assertIn("job_id", kb["result_processor_customizations"]["result_description"])
        status_kb = json.loads((REPO_ROOT / "plugins/marketo_plugin/knowledge_base/processes/check_marketo_job_status.json").read_text(encoding="utf-8"))
        for verb in VERBS:
            self.assertIn(verb, status_kb["description"])


if __name__ == "__main__":
    unittest.main()
