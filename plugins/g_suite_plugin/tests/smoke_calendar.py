#!/usr/bin/env python3
"""Hermetic smoke for the 38 Google Calendar API v3 verbs (no live Google, no network).

The real google-api-python-client builds every request from the plugin's
Calendar document (bundled revision plus the 20261102 overlay) and sends it to
googleapiclient's ``HttpMockSequence``, which records the exact method, URL and
body and answers with vendor-shaped transcripts: list envelopes with
nextPageToken / nextSyncToken, Google's JSON error bodies for 400, 401, 403,
404, 409, 410 and 429, and 204 deletes.

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_calendar.py
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_metadata import ParameterType  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor, QueuedActionProtocol  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.plugins.action_discovery import discover_actions  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from calendar_test_fixtures import TOKEN, _error, _minimal, _ok, _plugin, _service  # noqa: E402  # pyright: ignore[reportMissingImports]
from g_suite_plugin import calendar_discovery, calendar_operations  # noqa: E402  # pyright: ignore[reportMissingImports]
from g_suite_plugin.calendar_operations import CalendarContext, CalendarError, CalendarOperation  # noqa: E402  # pyright: ignore[reportMissingImports]
from g_suite_plugin.constants import CALENDAR_OAUTH_SCOPES, OAUTH_SCOPES  # noqa: E402  # pyright: ignore[reportMissingImports]
from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402  # pyright: ignore[reportMissingImports]

KB = REPO_ROOT / "plugins" / "g_suite_plugin" / "knowledge_base" / "processes"
BASE = "/calendar/v3/"


def _context(service: Any, **kwargs: Any) -> CalendarContext:
    return CalendarContext(service=service, **kwargs)


def _outward(verb: str, params: dict[str, Any], service: Any) -> dict[str, Any]:
    """One vendor call through the Calendar exit, as the plugin's boundary runs it."""
    produce = partial(calendar_operations.execute, calendar_operations.operation(verb), params, _context(service))
    return calendar_operations.outward_call(produce)


def _path(op: CalendarOperation, params: dict[str, Any]) -> str:
    values = {"calendarId": params.get("calendar_id"), "eventId": params.get("event_id"), "ruleId": params.get("rule_id"), "setting": params.get("setting")}
    path = op.path
    for key, value in values.items():
        path = path.replace("{" + key + "}", str(value))
    return BASE + path


class VendorCoverage(unittest.TestCase):
    """The verb table is exactly the vendor's 38 methods at revision 20261102."""

    def test_every_vendor_method_has_one_verb(self) -> None:
        document = calendar_discovery.calendar_document()
        vendor = {(resource, method) for resource, entry in document["resources"].items() for method in entry["methods"]}
        ours = {(op.resource, op.method) for op in calendar_operations.OPERATIONS}
        self.assertEqual(len(vendor), 38)
        self.assertEqual(ours, vendor)
        self.assertEqual(len({op.verb for op in calendar_operations.OPERATIONS}), 38)

    def test_overlay_adds_exactly_the_revision_deltas_and_is_idempotent(self) -> None:
        bundled = calendar_discovery._bundled_document()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        once = calendar_discovery._apply_overlay(bundled)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(calendar_discovery._apply_overlay(once), once)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        transfer = once["resources"]["calendars"]["methods"]["transferOwnership"]
        self.assertEqual((transfer["httpMethod"], transfer["path"]), ("POST", "calendars/{calendarId}/transferOwnership"))
        self.assertIn("showOwnOrganizationOnly", once["resources"]["calendarList"]["methods"]["list"]["parameters"])
        self.assertIn("eventLabelVersion", once["resources"]["events"]["methods"]["import"]["parameters"])
        if "transferOwnership" in bundled["resources"]["calendars"]["methods"]:
            # A newer client library ships the method itself: the overlay must not change it.
            self.assertEqual(bundled["resources"]["calendars"]["methods"]["transferOwnership"], transfer)

    def test_building_a_client_never_mutates_the_shared_document(self) -> None:
        before = copy.deepcopy(calendar_discovery.calendar_document())
        calendar_discovery.offline_service()
        calendar_discovery.build_service("token")
        self.assertEqual(calendar_discovery.calendar_document(), before)
        self.assertNotIn("oauth_token", calendar_discovery.vendor_parameters("events", "list"))

    def test_api_wide_parameters_are_declared_on_every_verb(self) -> None:
        document = calendar_discovery.calendar_document()
        global_names = set(document["parameters"]) - {"key", "access_token", "oauth_token"}
        self.assertTrue({"fields", "prettyPrint", "alt", "quotaUser", "userIp"} <= global_names)
        for op in calendar_operations.OPERATIONS:
            with self.subTest(verb=op.verb):
                metadata = calendar_discovery.parameter_metadata(op.resource, op.method)
                names = calendar_discovery.vendor_parameters(op.resource, op.method)
                self.assertTrue(global_names <= set(names.values()))
                self.assertTrue(calendar_discovery.CREDENTIAL_PARAMETERS.isdisjoint(names))
                for credential in calendar_discovery.CREDENTIAL_PARAMETERS:
                    self.assertFalse(metadata[credential].required)
                    self.assertIn("Refused", metadata[credential].description)
                    self.assertIn("account contract", metadata[credential].description)
                for name in global_names:
                    declared = metadata[calendar_discovery.snake_name(name)]
                    self.assertFalse(declared.required)
                self.assertEqual(metadata["pretty_print"].type, ParameterType.BOOLEAN)

    def test_scopes_are_the_narrowest_requested_and_vendor_accepted(self) -> None:
        self.assertEqual(len(CALENDAR_OAUTH_SCOPES), 10)
        self.assertTrue(set(CALENDAR_OAUTH_SCOPES) <= set(OAUTH_SCOPES))
        self.assertNotIn("https://www.googleapis.com/auth/calendar", OAUTH_SCOPES)
        for op in calendar_operations.OPERATIONS:
            with self.subTest(verb=op.verb):
                self.assertIn(op.scope, CALENDAR_OAUTH_SCOPES)
                self.assertIn(op.scope, op.vendor_scopes)
                self.assertEqual(op.retryable, not op.writes)


class Routing(unittest.TestCase):
    """Each verb sends its exact vendor method and path, once."""

    def test_every_verb_reaches_its_vendor_method_and_path(self) -> None:
        for op in calendar_operations.OPERATIONS:
            with self.subTest(verb=op.verb):
                params = _minimal(op)
                params.update(fields="kind", pretty_print=False, quota_user="calendar-smoke", user_ip="192.0.2.1", alt="json")
                service, http = _service([_ok({"kind": "calendar#probe"})])
                result = calendar_operations.execute(op, params, _context(service))
                self.assertEqual(len(http.request_sequence), 1)
                uri, method, body, _headers = http.request_sequence[0]
                self.assertEqual(method, op.http_method)
                self.assertEqual(unquote(urlsplit(uri).path), _path(op, params))
                query = parse_qs(urlsplit(uri).query)
                self.assertEqual(query["fields"], ["kind"])
                self.assertEqual(query["prettyPrint"], ["false"])
                self.assertEqual(query["quotaUser"], ["calendar-smoke"])
                self.assertEqual(query["userIp"], ["192.0.2.1"])
                self.assertEqual(query["alt"], ["json"])
                # A method with no vendor response schema (deletes, clear, channels.stop,
                # transferOwnership) has its body discarded by the vendor client.
                has_response = "response" in calendar_discovery.vendor_method(op.resource, op.method)
                self.assertEqual(result["response"], {"kind": "calendar#probe"} if has_response else {})
                if op.takes_body:
                    self.assertEqual(json.loads(body), params["body"])

    def test_punctuated_vendor_parameter_uses_build3_client_spelling(self) -> None:
        # Calendar revision 20261102 does not publish $.xgafv. Exercise that
        # documented Google-wide spelling with an explicit discovery fixture.
        document = copy.deepcopy(calendar_discovery.calendar_document())
        document["parameters"]["$.xgafv"] = {"type": "string", "location": "query", "enum": ["1", "2"]}
        self.assertEqual(calendar_discovery.snake_name("$.xgafv"), "x__xgafv")
        with patch.object(calendar_discovery, "calendar_document", return_value=document):
            service, http = _service([_ok({"items": []})])
            with patch.object(calendar_discovery, "offline_service", return_value=service):
                metadata = calendar_discovery.parameter_metadata("events", "list")
                self.assertIn("x__xgafv", metadata)
                calendar_operations.execute(calendar_operations.operation("calendar_events_list"), {"calendar_id": "primary", "x__xgafv": "2"}, _context(service))
        query = parse_qs(urlsplit(http.request_sequence[0][0]).query)
        self.assertEqual(query["$.xgafv"], ["2"])

    def test_query_values_are_vendor_encoded(self) -> None:
        op = calendar_operations.operation("calendar_events_list")
        service, http = _service([_ok({"items": []})])
        params = {"calendar_id": "primary", "time_min": "2026-11-07T00:00:00Z", "single_events": True, "order_by": "startTime", "event_types": ["default", "focusTime"], "i_cal_uid": "abc@google.com", "max_results": 2500}
        calendar_operations.execute(op, params, _context(service))
        query = parse_qs(urlsplit(http.request_sequence[0][0]).query)
        self.assertEqual(query["timeMin"], ["2026-11-07T00:00:00Z"])
        self.assertEqual(query["singleEvents"], ["true"])
        self.assertEqual(query["eventTypes"], ["default", "focusTime"])
        self.assertEqual(query["iCalUID"], ["abc@google.com"])
        self.assertEqual(query["maxResults"], ["2500"])

    def test_insert_with_attendees_meet_and_recurrence(self) -> None:
        op = calendar_operations.operation("calendar_events_insert")
        event = {
            "summary": "Planning",
            "start": {"dateTime": "2026-11-09T10:00:00+01:00", "timeZone": "Europe/Zurich"},
            "end": {"dateTime": "2026-11-09T11:00:00+01:00", "timeZone": "Europe/Zurich"},
            "attendees": [{"email": "ana@example.com"}],
            "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
            "conferenceData": {"createRequest": {"requestId": "req-1", "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
        }
        service, http = _service([_ok({"id": "evt1", "hangoutLink": "https://meet.google.com/abc-defg-hij"})])
        result = calendar_operations.execute(op, {"calendar_id": "primary", "conference_data_version": 1, "send_updates": "none", "body": event}, _context(service))
        uri, _method, body, _headers = http.request_sequence[0]
        self.assertEqual(json.loads(body), event)
        self.assertEqual(parse_qs(urlsplit(uri).query)["conferenceDataVersion"], ["1"])
        self.assertEqual(result["response"]["hangoutLink"], "https://meet.google.com/abc-defg-hij")

    def test_delete_answers_empty(self) -> None:
        op = calendar_operations.operation("calendar_events_delete")
        service, _http = _service([({"status": "204"}, "")])
        result = calendar_operations.execute(op, {"calendar_id": "primary", "event_id": "evt1"}, _context(service, account="ana@gmail.com"))
        self.assertEqual(result, {"response": {}, "account": "ana@gmail.com"})


class PagingAndSync(unittest.TestCase):
    def test_page_tokens_and_sync_token_are_lifted(self) -> None:
        op = calendar_operations.operation("calendar_events_list")
        service, http = _service([_ok({"kind": "calendar#events", "items": [{"id": "a"}], "nextPageToken": "p2"}), _ok({"kind": "calendar#events", "items": [{"id": "b"}], "nextSyncToken": "s1"})])
        first = calendar_operations.execute(op, {"calendar_id": "primary"}, _context(service))
        second = calendar_operations.execute(op, {"calendar_id": "primary", "page_token": first["next_page_token"]}, _context(service))
        self.assertEqual(first["next_page_token"], "p2")
        self.assertNotIn("next_sync_token", first)
        self.assertEqual(second["next_sync_token"], "s1")
        self.assertEqual(parse_qs(urlsplit(http.request_sequence[1][0]).query)["pageToken"], ["p2"])

    def test_expired_sync_token_asks_for_a_full_resync(self) -> None:
        op = calendar_operations.operation("calendar_events_list")
        service, _http = _service([_error(410, "fullSyncRequired", "Sync token is no longer valid, a full sync is required.")])
        with self.assertRaises(CalendarError) as caught:
            calendar_operations.execute(op, {"calendar_id": "primary", "sync_token": "old"}, _context(service))
        self.assertEqual(caught.exception.code, "gsuite.sync_token_expired")
        self.assertIn("without sync_token", str(caught.exception))

    def test_sync_token_conflicts_are_refused_per_vendor_rules(self) -> None:
        cases = [
            ("calendar_events_list", {"calendar_id": "primary", "sync_token": "s", "time_min": "2026-01-01T00:00:00Z"}),
            ("calendar_events_list", {"calendar_id": "primary", "sync_token": "s", "show_deleted": False}),
            ("calendar_calendarlist_list", {"sync_token": "s", "min_access_role": "owner"}),
            ("calendar_calendarlist_list", {"sync_token": "s", "show_hidden": False}),
            ("calendar_acl_list", {"calendar_id": "primary", "sync_token": "s", "show_deleted": False}),
        ]
        for verb, params in cases:
            with self.subTest(verb=verb, params=params), self.assertRaisesRegex(ValueError, "sync_token"):
                calendar_operations.validate(calendar_operations.operation(verb), params)
        calendar_operations.validate(calendar_operations.operation("calendar_settings_list"), {"sync_token": "s", "max_results": 10})


class Refusals(unittest.TestCase):
    """Bad input is refused at dispatch, before any job or request."""

    def test_vendor_shape_refusals(self) -> None:
        events_list = calendar_operations.operation("calendar_events_list")
        cases: list[tuple[CalendarOperation, dict[str, Any], str]] = [
            (events_list, {}, "calendar_id"),
            (events_list, {"calendarId": "primary"}, "unknown argument"),
            (events_list, {"calendar_id": "primary", "max_results": 0}, "at least 1"),
            (events_list, {"calendar_id": "primary", "max_results": "10"}, "integer"),
            (events_list, {"calendar_id": "primary", "single_events": "yes"}, "true or false"),
            (events_list, {"calendar_id": "primary", "order_by": "title"}, "not an allowed value"),
            (events_list, {"calendar_id": "primary", "event_types": "default"}, "list of strings"),
            (events_list, {"calendar_id": "primary", "account": ""}, "account"),
            (calendar_operations.operation("calendar_events_insert"), {"calendar_id": "primary"}, "'body' is required"),
            (calendar_operations.operation("calendar_events_insert"), {"calendar_id": "primary", "body": {}, "conference_data_version": 2}, "at most 1"),
            (calendar_operations.operation("calendar_events_quick_add"), {"calendar_id": "primary"}, "text"),
            (calendar_operations.operation("calendar_calendars_transfer_ownership"), {"calendar_id": "c", "new_data_owner": "b@example.com", "use_admin_access": False}, "must be true"),
        ]
        for op, params, fragment in cases:
            with self.subTest(verb=op.verb, params=params), self.assertRaisesRegex(ValueError, fragment):
                calendar_operations.validate(op, params)


class VendorErrors(unittest.TestCase):
    def _fail(self, verb: str, params: dict[str, Any], response: tuple[dict[str, str], str], **context: Any) -> CalendarError:
        service, _http = _service([response])
        with self.assertRaises(CalendarError) as caught:
            calendar_operations.execute(calendar_operations.operation(verb), params, _context(service, **context))
        return caught.exception

    def test_status_codes_map_to_typed_codes(self) -> None:
        get = ("calendar_events_get", {"calendar_id": "primary", "event_id": "e"})
        cases = [
            (_error(400, "invalid", "Invalid value for: timeMin"), "gsuite.invalid_params"),
            (_error(401, "authError", "Invalid Credentials", status_text="UNAUTHENTICATED"), "gsuite.auth_expired"),
            (_error(403, "insufficientPermissions", "Request had insufficient authentication scopes.", status_text="PERMISSION_DENIED"), "gsuite.missing_scope"),
            (_error(403, "rateLimitExceeded", "Rate Limit Exceeded"), "gsuite.rate_limited"),
            (_error(403, "forbidden", "Forbidden"), "gsuite.permission_denied"),
            (_error(404, "notFound", "Not Found"), "gsuite.not_found"),
            (_error(409, "duplicate", "The requested identifier already exists."), "gsuite.conflict"),
            (_error(410, "deleted", "Resource has been deleted"), "gsuite.gone"),
            (_error(429, "rateLimitExceeded", "Too Many Requests"), "gsuite.rate_limited"),
            (_error(500, "backendError", "Backend Error"), "gsuite.api_error"),
        ]
        for response, code in cases:
            with self.subTest(code=code, status=response[0]["status"]):
                error = self._fail(*get, response)
                self.assertEqual(error.code, code)
                self.assertIn("Google said:", str(error))

    def test_missing_scope_message_names_the_scope_and_reconnect(self) -> None:
        error = self._fail("calendar_events_insert", {"calendar_id": "primary", "body": {}}, _error(403, "insufficientPermissions", "Request had insufficient authentication scopes."))
        self.assertIn("calendar.events", str(error))
        self.assertIn("connect_account", str(error))

    def test_recorded_grants_refuse_before_any_request(self) -> None:
        service, http = _service([])
        read_only = frozenset({"https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/calendar.events.readonly"})
        with self.assertRaises(CalendarError) as caught:
            calendar_operations.execute(calendar_operations.operation("calendar_events_insert"), {"calendar_id": "primary", "body": {}}, _context(service, granted_scopes=read_only))
        self.assertEqual(caught.exception.code, "gsuite.missing_scope")
        self.assertEqual(http.request_sequence, [])
        service, http = _service([_ok({"items": []})])
        calendar_operations.execute(calendar_operations.operation("calendar_events_list"), {"calendar_id": "primary"}, _context(service, granted_scopes=read_only))
        self.assertEqual(len(http.request_sequence), 1)


class AccountTier(unittest.TestCase):
    """Workspace-only requests from a consumer account fail with one tier-naming message."""

    def test_consumer_account_is_refused_before_io(self) -> None:
        transfer = ("calendar_calendars_transfer_ownership", {"calendar_id": "c@group.calendar.google.com", "new_data_owner": "b@example.com", "use_admin_access": True})
        domain_share = ("calendar_acl_insert", {"calendar_id": "primary", "body": {"role": "reader", "scope": {"type": "domain", "value": "example.com"}}})
        for verb, params in (transfer, domain_share):
            with self.subTest(verb=verb):
                service, http = _service([])
                with self.assertRaises(CalendarError) as caught:
                    calendar_operations.execute(calendar_operations.operation(verb), params, _context(service, account="ana@gmail.com", tier=lambda: "consumer"))
                self.assertEqual(caught.exception.code, "gsuite.workspace_only")
                self.assertIn("consumer", str(caught.exception))
                self.assertIn("ana@gmail.com", str(caught.exception))
                self.assertEqual(http.request_sequence, [])

    def test_user_share_from_a_consumer_account_is_sent(self) -> None:
        service, http = _service([_ok({"id": "user:bo@example.com", "role": "reader"})])
        params = {"calendar_id": "primary", "body": {"role": "reader", "scope": {"type": "user", "value": "bo@example.com"}}}
        calendar_operations.execute(calendar_operations.operation("calendar_acl_insert"), params, _context(service, tier=lambda: "consumer"))
        self.assertEqual(len(http.request_sequence), 1)

    def test_unknown_tier_lets_google_decide_and_names_the_tier_requirement(self) -> None:
        service, http = _service([_error(403, "forbidden", "Not a Workspace administrator")])
        params = {"calendar_id": "c@group.calendar.google.com", "new_data_owner": "b@example.com", "use_admin_access": True}
        with self.assertRaises(CalendarError) as caught:
            calendar_operations.execute(calendar_operations.operation("calendar_calendars_transfer_ownership"), params, _context(service))
        self.assertEqual(len(http.request_sequence), 1)
        self.assertEqual(caught.exception.code, "gsuite.workspace_only")
        self.assertIn("Google Workspace administrator", str(caught.exception))

    def test_tier_reads_the_account_email_domain(self) -> None:
        self.assertEqual(calendar_operations.account_tier("Ana@Gmail.com"), "consumer")
        self.assertEqual(calendar_operations.account_tier("ana@googlemail.com"), "consumer")
        self.assertIsNone(calendar_operations.account_tier("ana@example.com"))


class AsReceived(unittest.TestCase):
    """The vendor response is returned as received; a failure is typed."""

    def test_result_is_returned_as_received(self) -> None:
        echo = {"items": [{"id": "e1", "description": "vendor text"}], "access_token": "anything", "nextPageToken": "keep-me"}
        service, _http = _service([_ok(echo)])
        result = _outward("calendar_events_list", {"calendar_id": "primary"}, service)
        self.assertEqual(result["response"]["access_token"], "anything")
        self.assertEqual(result["next_page_token"], "keep-me")
        self.assertEqual(result["response"]["items"], [{"id": "e1", "description": "vendor text"}])

    def test_error_is_typed_with_the_vendor_message(self) -> None:
        service, _http = _service([_error(400, "invalid", "bad request text")])
        with self.assertRaises(CalendarError) as caught:
            _outward("calendar_events_get", {"calendar_id": "primary", "event_id": "e"}, service)
        self.assertIn("bad request text", str(caught.exception))


class Wiring(unittest.TestCase):
    STATE: dict[str, Any] = {"session_id": "s1", "flow_id": "f1"}

    def test_dispatch_validates_then_queues(self) -> None:
        plugin, jobs = _plugin()
        refused = plugin.calendar_events_list({"calendar_id": "primary", "max_results": 0}, self.STATE)
        queued = plugin.calendar_events_list({"calendar_id": "primary"}, self.STATE)
        self.assertEqual(refused["error"]["code"], "gsuite.invalid_params")
        self.assertEqual(queued["data"], {"job_id": "job-1", "status": "queued"})
        self.assertEqual(jobs.payloads["job-1"]["verb"], "calendar_events_list")

    def test_partial_response_dispatch_worker_transcript_and_request_correction(self) -> None:
        plugin, jobs = _plugin()
        params = {"calendar_id": "primary", "fields": "items(id),nextPageToken", "pretty_print": False}
        queued = plugin.calendar_events_list(params, self.STATE)
        self.assertEqual(queued["data"], {"job_id": "job-1", "status": "queued"})
        service, http = _service([_ok({"items": [{"id": "evt1"}], "nextPageToken": "p2"}), _ok({"items": [{"id": "evt1", "summary": "Planning"}]}), _ok({"items": []})])
        with patch.object(calendar_discovery, "build_service", return_value=service):
            plugin._process_job({"id": "job-1"})  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(jobs.updates[-1][1]["result"]["response"], {"items": [{"id": "evt1"}], "nextPageToken": "p2"})
            self.assertEqual(jobs.updates[-1][1]["result"]["next_page_token"], "p2")
            plugin.calendar_events_list({**params, "fields": "items(id,summary)", "pretty_print": True}, self.STATE)
            plugin._process_job({"id": "job-2"})  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            plugin.calendar_events_list({"calendar_id": "primary"}, self.STATE)
            plugin._process_job({"id": "job-3"})  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        queries = [parse_qs(urlsplit(row[0]).query) for row in http.request_sequence]
        self.assertEqual(queries[0]["fields"], ["items(id),nextPageToken"])
        self.assertEqual(queries[0]["prettyPrint"], ["false"])
        self.assertEqual(queries[1]["fields"], ["items(id,summary)"])
        self.assertEqual(queries[1]["prettyPrint"], ["true"])
        self.assertNotIn("fields", queries[2])
        self.assertEqual(len(http.request_sequence), 3)
        self.assertEqual([status["status"] for _, status in jobs.updates], ["processing", "completed"] * 3)

    def test_account_owned_credentials_refuse_before_any_job(self) -> None:
        for credential in ("access_token", "oauth_token", "key"):
            with self.subTest(credential=credential):
                plugin, jobs = _plugin()
                result = plugin.calendar_events_list({"calendar_id": "primary", credential: "never-echo-this-secret"}, self.STATE)
                self.assertEqual(result["error"]["code"], "gsuite.invalid_params")
                self.assertIn("account contract", result["error"]["message"])
                self.assertIn(credential, result["error"]["message"])
                self.assertNotIn("never-echo-this-secret", str(result))
                self.assertEqual(jobs.payloads, {})

    def test_framework_submission_refuses_credentials_on_every_verb(self) -> None:
        plugin, jobs = _plugin()
        # Discover the real registered metadata, submit through ActionFactory,
        # then execute the recorded action through the real ActionProcessor.
        registry: dict[str, object] = {"processes": {f"plugin::g_suite_plugin::{action.name}": action.to_dict() for action in discover_actions(plugin)}}
        state_service = Mock()
        state_service.generate_unique_string.return_value = {"action_status": "completed", "data": {"random_string": "calendar-test"}}
        recorder = Mock()
        recorder.store_action_event.return_value = "act-calendar-test"
        template_engine = Mock()
        template_engine.resolve_templates.side_effect = lambda definition, _context: definition
        factory = ActionFactory(process_registry=registry, template_engine=template_engine, state_service=state_service, action_event_recorder=recorder)
        manager = Mock()
        manager.get_plugin.return_value = plugin
        contexts = ExecutionContextManager()
        contexts.create_context("flow-calendar-test")
        processor = ActionProcessor(plugin_manager=manager, process_registry=registry, execution_context_manager=contexts)
        for op in calendar_operations.OPERATIONS:
            for credential in ("key", "access_token", "oauth_token"):
                with self.subTest(verb=op.verb, credential=credential):
                    params = {**_minimal(op), credential: "never-echo-this-secret"}
                    try:
                        action_id = factory.submit_action_definition({"process_key": f"plugin::g_suite_plugin::{op.verb}", "arguments": params, "flow_id": "flow-calendar-test"})
                    except FrameworkError as exc:
                        self.fail(f"Framework intercepted Calendar credential refusal: {exc.error_code}: {exc}")
                    saved = recorder.store_action_event.call_args.args[0]
                    queued = SimpleNamespace(id=action_id, process_key=saved["process_key"], parameters=json.dumps(saved["parameters"]), created_at=saved["timestamp"], session_id=None, flow_id=saved["flow_id"], context_id=None, result_processor=None, template_namespace=None, flow_token_id=None, source_plugin=None)
                    result = processor.execute_action(cast(QueuedActionProtocol, queued))
                    self.assertFalse(result["success"], result)
                    error = cast(dict[str, Any], result["error"])
                    self.assertEqual(error["code"], "gsuite.invalid_params")
                    self.assertIn("account contract", error["message"])
                    self.assertIn(credential, error["message"])
                    self.assertNotIn("never-echo-this-secret", str(result))
                    self.assertEqual(jobs.payloads, {})

    def test_worker_runs_the_vendor_call_and_completes(self) -> None:
        plugin, jobs = _plugin()
        plugin.calendar_events_quick_add({"calendar_id": "primary", "text": "Lunch tomorrow noon", "account": "Ana@Gmail.com"}, self.STATE)
        service, http = _service([_ok({"id": "evt9", "description": TOKEN})])
        with patch.object(calendar_discovery, "build_service", return_value=service):
            plugin._process_job({"id": "job-1"})  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        status = jobs.updates[-1][1]
        self.assertEqual(status["status"], "completed", status)
        self.assertEqual(status["result"]["account"], "ana@gmail.com")
        self.assertEqual(status["result"]["response"]["description"], TOKEN)
        self.assertEqual([request[1] for request in http.request_sequence], ["POST"])

    def test_dispatch_refuses_an_unknown_account_without_a_job(self) -> None:
        plugin, jobs = _plugin()
        result = plugin.calendar_colors_get({"account": "missing"}, self.STATE)
        self.assertEqual(result["error"]["code"], "gsuite.unknown_account")
        self.assertIn("ana@gmail.com", result["error"]["message"])
        self.assertEqual(jobs.payloads, {})

    def test_every_verb_uses_the_selected_or_default_account(self) -> None:
        for op in calendar_operations.OPERATIONS:
            for selector, expected in ((None, "ana@gmail.com"), ("work", "work@example.com"), ("WORK@EXAMPLE.COM", "work@example.com")):
                with self.subTest(verb=op.verb, account=selector):
                    plugin, jobs = _plugin()
                    params = _minimal(op)
                    if selector is not None:
                        params["account"] = selector
                    # The transfer is exercised as Workspace; consumer refusal is
                    # checked separately below. No principal mutation is made.
                    if op.method == "transferOwnership" and selector is None:
                        continue
                    getattr(plugin, op.verb)(params, self.STATE)
                    service, http = _service([_ok({"kind": "calendar#probe"})])
                    with patch.object(calendar_discovery, "build_service", return_value=service) as build:
                        plugin._process_job({"id": "job-1"})  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    build.assert_called_once_with(TOKEN if expected.endswith("@gmail.com") else "workspace-access-token")
                    completed = jobs.updates[-1][1]
                    self.assertEqual(completed["status"], "completed", completed)
                    self.assertEqual(completed["result"]["account"], expected)
                    self.assertEqual(len(http.request_sequence), 1)

    def test_consumer_tier_and_missing_scope_pass_through_the_worker(self) -> None:
        for missing_grant in (False, True):
            with self.subTest(missing_grant=missing_grant):
                plugin, jobs = _plugin()
                if missing_grant:
                    store = plugin._require_accounts().token_store("ana@gmail.com")  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    store.record_credential_facts(granted_scopes=(), account_email="ana@gmail.com", account_tier="consumer")
                    verb = "calendar_events_list"
                    params: dict[str, Any] = {"calendar_id": "primary"}
                else:
                    verb = "calendar_calendars_transfer_ownership"
                    params = _minimal(calendar_operations.operation(verb))
                getattr(plugin, verb)(params, self.STATE)
                service, http = _service([])
                with patch.object(calendar_discovery, "build_service", return_value=service):
                    plugin._process_job({"id": "job-1"})  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                error = jobs.updates[-1][1]["error"]
                self.assertEqual(error["code"], "gsuite.missing_scope" if missing_grant else "gsuite.workspace_only")
                self.assertIn("connect_account" if missing_grant else "consumer", error["message"])
                self.assertEqual(http.request_sequence, [])

    def test_unknown_grants_are_advisory_and_single_account_needs_no_selector(self) -> None:
        plugin, jobs = _plugin()
        accounts = plugin._require_accounts()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        accounts.remove("work")
        store = accounts.token_store("ana@gmail.com")
        store.store_initial_tokens(refresh_token="refresh-home", access_token=TOKEN, expires_in=3600)
        plugin.calendar_events_list({"calendar_id": "primary"}, self.STATE)
        service, http = _service([_ok({"items": []})])
        with patch.object(calendar_discovery, "build_service", return_value=service):
            plugin._process_job({"id": "job-1"})  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(jobs.updates[-1][1]["result"]["account"], "ana@gmail.com")
        self.assertEqual(len(http.request_sequence), 1)

    def test_worker_completes_typed_vendor_errors(self) -> None:
        plugin, jobs = _plugin()
        plugin.calendar_events_get({"calendar_id": "primary", "event_id": "nope"}, self.STATE)
        service, _http = _service([_error(404, "notFound", "Not Found")])
        with patch.object(calendar_discovery, "build_service", return_value=service):
            plugin._process_job({"id": "job-1"})  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(jobs.updates[-1][1]["error"]["code"], "gsuite.not_found")


class Registration(unittest.TestCase):
    """Each process needs its decorator, EDGE entry and knowledge-base JSON."""

    def test_three_artifacts_per_process(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        plugin = GSuitePlugin()
        decorated = {action.name: action for action in discover_actions(plugin)}
        edges = plugin.get_edge_process_definitions()
        jsons = {path.stem: json.loads(path.read_text()) for path in KB.glob("*.json")}
        self.assertEqual(set(decorated), set(edges))
        self.assertEqual(set(decorated), set(jsons))
        self.assertTrue(set(plugin._deferred_producers()) >= set(calendar_operations.OPERATIONS_BY_VERB))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        for op in calendar_operations.OPERATIONS:
            with self.subTest(verb=op.verb):
                doc = jsons[op.verb]
                self.assertEqual(doc["process_key"], f"plugin::g_suite_plugin::{op.verb}")
                self.assertEqual(doc["result_processor_customizations"]["result_type"], op.result_type)
                self.assertEqual(edges[op.verb].result_processor_template_customizations.result_type, op.result_type)  # type: ignore[union-attr]
                self.assertEqual(doc["description"], decorated[op.verb].description)
                self.assertTrue(200 <= len(doc["embedding_description"]) <= 400)
                self.assertIn(f"{op.http_method} {op.path}", decorated[op.verb].description)

    def test_each_verb_declares_vendor_parameters_and_refused_credentials(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        decorated = {action.name: action for action in discover_actions(GSuitePlugin())}
        for op in calendar_operations.OPERATIONS:
            with self.subTest(verb=op.verb):
                expected = {*calendar_discovery.vendor_parameters(op.resource, op.method), *calendar_discovery.CREDENTIAL_PARAMETERS, "account", *(["body"] if op.takes_body else [])}
                self.assertEqual(set(decorated[op.verb].parameters), expected)


if __name__ == "__main__":
    unittest.main(verbosity=1)
