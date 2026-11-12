"""Vendor-shaped Jira project/sprint/release/worklog transcripts and process custody."""

from __future__ import annotations

import inspect
import io
import json
import logging
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/jira_plugin/src"))
sys.path.insert(0, str(Path(__file__).parent))

from jira_plugin import async_jobs  # noqa: E402
from jira_plugin.errors import JiraServiceError  # noqa: E402
from jira_plugin.families import INVENTORY, execute, validate  # noqa: E402
from jira_plugin.family_processes import FAMILY_NAMES  # noqa: E402
from jira_plugin.http import Response  # noqa: E402
from jira_plugin.plugin import JiraPlugin  # noqa: E402
from jira_plugin.site_context import CURRENT_SITE  # noqa: E402
from jira_plugin.sites import SiteStore  # noqa: E402
from smoke_sites import AddressBook, Vault  # noqa: E402


class FamilySmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.response = Response(200, {"id": 37, "name": "Sprint 1", "state": "future", "originBoardId": 84}, {})
        self.store = SiteStore(Vault(), AddressBook(), self.http)
        self.store.manage({"operation": "bind_api_token", "site": "first", "entry_name": "first_entry", "expected_version": 0})
        self.store.manage({"operation": "bind_api_token", "site": "second", "entry_name": "second_entry", "expected_version": 1})

    def http(self, method: str, url: str, headers: dict[str, str], body: Any = None) -> Response:
        self.calls.append((method, url, headers, body))
        return self.response

    def call(self, family: str, operation: str, path: dict[str, Any] | None = None, body: Any = None, query: dict[str, Any] | None = None, site: str = "second") -> dict[str, Any]:
        return execute(self.store, site, family, {"operation": operation, "path": path or {}, "query": query or {}, **({"body": body} if body is not None else {})})

    def test_sprint_end_to_end_and_vendor_state_reversal(self) -> None:
        create = {"name": "Sprint 1", "originBoardId": 84, "startDate": "2026-11-09T09:00:00.000Z", "endDate": "2026-11-23T09:00:00.000Z", "goal": "Ship connector"}
        self.call("sprints", "createSprint", body=create)
        self.assertEqual(self.calls[-1][0:2], ("POST", "https://second.atlassian.net/rest/agile/1.0/sprint"))
        self.assertEqual(self.calls[-1][3], create)
        self.call("sprints", "partiallyUpdateSprint", {"sprintId": 37}, {"state": "active", "name": "Sprint 1 corrected", "goal": ""})
        self.assertEqual(self.calls[-1][0], "POST")
        rank = {"issues": ["DEMO-12", "DEMO-13"], "rankBeforeIssue": "DEMO-14", "rankCustomFieldId": 10019}
        self.call("agile_issues", "rankIssues", body=rank)
        self.assertEqual(self.calls[-1][0:2], ("PUT", "https://second.atlassian.net/rest/agile/1.0/issue/rank"))
        move = {"issues": ["DEMO-12"], "rankAfterIssue": "DEMO-13", "rankCustomFieldId": 10019}
        self.call("sprints", "moveIssuesToSprintAndRank", {"sprintId": 37}, move)
        self.assertEqual(self.calls[-1][3], move)
        self.call("backlog", "moveIssuesToBacklogForBoard", {"boardId": 84}, {"issues": ["DEMO-13"], "rankBeforeIssue": "DEMO-14"})
        self.assertEqual(self.calls[-1][1], "https://second.atlassian.net/rest/agile/1.0/backlog/84/issue")
        self.call("sprints", "updateSprint", {"sprintId": 37}, {"name": "Sprint 1", "state": "closed"})
        self.assertEqual(self.calls[-1][0], "PUT")
        self.call("sprints", "deleteSprint", {"sprintId": 37})
        self.assertEqual(self.calls[-1][0], "DELETE")
        self.call("sprints", "setProperty", {"sprintId": "37", "propertyKey": "team property"}, {"owner": "Engineering"})
        self.assertTrue(self.calls[-1][1].endswith("/properties/team%20property"))
        self.call("sprints", "deleteProperty", {"sprintId": "37", "propertyKey": "team property"})
        self.assertEqual(self.calls[-1][0], "DELETE")

    def test_project_board_version_and_worklog_lifecycles(self) -> None:
        project = {"key": "DEMO", "name": "Demo", "projectTypeKey": "software", "leadAccountId": "5b10a2844c20165700ede21g", "assigneeType": "PROJECT_LEAD", "description": "Team planning"}
        self.call("projects", "createProject", body=project)
        self.assertEqual(self.calls[-1][1], "https://second.atlassian.net/rest/api/3/project")
        self.assertEqual(self.calls[-1][3]["leadAccountId"], project["leadAccountId"])
        self.call("projects", "updateProject", {"projectIdOrKey": "DEMO"}, {"name": "Corrected", "description": ""})
        for operation, suffix in [("archiveProject", "/archive"), ("restore", "/restore"), ("deleteProject", "")]:
            self.call("projects", operation, {"projectIdOrKey": "DEMO"})
            self.assertTrue(self.calls[-1][1].endswith("/project/DEMO" + suffix))
        self.call("boards", "createBoard", body={"name": "Planning", "type": "scrum", "filterId": 10040, "location": {"type": "project", "projectKeyOrId": "DEMO"}})
        self.assertEqual(self.calls[-1][0:2], ("POST", "https://second.atlassian.net/rest/agile/1.0/board"))
        self.call("boards", "setBoardProperty", {"boardId": "84", "propertyKey": "owner"}, {"accountId": "abc123"})
        self.call("boards", "deleteBoard", {"boardId": 84})
        self.assertEqual(self.calls[-1][0], "DELETE")
        self.call("versions", "createVersion", body={"name": "1.0", "projectId": 10000, "released": False, "releaseDate": "2026-11-23"})
        self.assertEqual(self.calls[-1][1], "https://second.atlassian.net/rest/api/3/version")
        for released in [True, False]:
            self.call("versions", "updateVersion", {"id": "10010"}, {"released": released, "description": ""})
            self.assertIs(self.calls[-1][3]["released"], released)
        self.call("versions", "deleteVersion", {"id": "10010"}, query={"moveFixIssuesTo": "10011"})
        self.assertEqual(self.calls[-1][0], "DELETE")
        comment = {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Connector work"}]}]}
        work = {"started": "2026-11-07T09:00:00.000+0000", "timeSpentSeconds": 3600, "comment": comment}
        self.call("worklogs", "addWorklog", {"issueIdOrKey": "DEMO-12"}, work, {"adjustEstimate": "leave", "notifyUsers": False})
        self.assertIn("adjustEstimate=leave", self.calls[-1][1])
        self.assertIn("notifyUsers=false", self.calls[-1][1])
        self.assertEqual(self.calls[-1][3]["comment"], comment)
        self.call("worklogs", "updateWorklog", {"issueIdOrKey": "DEMO-12", "id": "10020"}, {"timeSpentSeconds": 7200})
        self.call("worklogs", "deleteWorklog", {"issueIdOrKey": "DEMO-12", "id": "10020"})
        self.assertEqual(self.calls[-1][0:2], ("DELETE", "https://second.atlassian.net/rest/api/3/issue/DEMO-12/worklog/10020"))

    def test_pagination_and_optional_vendor_parameters_are_preserved(self) -> None:
        self.response = Response(200, {"startAt": 0, "maxResults": 2, "total": 3, "isLast": False, "values": [{"id": 84, "name": "A"}, {"id": 85, "name": "B"}]}, {})
        result = self.call("boards", "getAllBoards", query={"startAt": 0, "maxResults": 2, "type": "scrum", "name": "Plan", "projectKeyOrId": "DEMO", "accountIdLocation": "abc", "includePrivate": True, "negateLocationFiltering": False})
        self.assertEqual(result["next_query"]["startAt"], 2)
        self.assertEqual(result["response"]["values"][1]["id"], 85)
        self.assertIn("includePrivate=true", self.calls[-1][1])
        self.response = Response(200, {"issues": [{"key": "DEMO-12"}], "nextPageToken": "cursor-2"}, {})
        cursor = self.call("sprints", "getIssuesForSprintJSIS", {"sprintId": 37}, query={"fields": ["summary", "status"], "expand": "names", "maxResults": 1})
        self.assertEqual(cursor["next_query"]["nextPageToken"], "cursor-2")
        self.assertIn("fields=summary%2Cstatus", self.calls[-1][1])

    def test_vendor_errors_and_validation_do_not_make_requests(self) -> None:
        for status, code in [(400, "bad_request"), (401, "auth_failed"), (403, "permission_denied"), (404, "not_found"), (409, "conflict"), (429, "rate_limited"), (500, "api_error")]:
            self.response = Response(status, {"errorMessages": ["Cannot perform operation"], "errors": {"name": "required"}}, {"retry-after": "17"})
            with self.assertRaises(JiraServiceError) as caught:
                self.call("projects", "getProject", {"projectIdOrKey": "DEMO"})
            self.assertEqual(caught.exception.code, "jira." + code)
            if status in (401, 403):
                self.assertNotIn("Cannot perform", str(caught.exception))
        count = len(self.calls)
        for params in [{"operation": "getProject"}, {"operation": "createProject", "body": {"name": "No key"}}, {"operation": "bogus"}]:
            with self.assertRaises(ValueError):
                execute(self.store, "second", "projects", params)
        with self.assertRaises(ValueError):
            validate(True, {"type": "integer"}, "id")
        self.assertEqual(len(self.calls), count)

    def test_deferred_completion_pins_site_and_restores_context(self) -> None:
        plugin = JiraPlugin()
        plugin._sites = self.store
        manager = MagicMock()
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": {"site": "second", "operation": "getSprint", "path": {"sprintId": 37}}}}
        async_jobs._process_job(plugin, manager, "job-1", "sprints")
        result = manager.update_job.call_args.args[1]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["site"], "second")
        # The vendor response publishes as received.
        self.assertEqual(result["result"]["response"], {"id": 37, "name": "Sprint 1", "state": "future", "originBoardId": 84})
        self.assertNotIn('x/y\\"é', json.dumps(result["result"], ensure_ascii=False))
        self.assertTrue(self.calls[-1][1].startswith("https://second.atlassian.net/"))
        self.assertEqual(CURRENT_SITE.get(), "legacy")
        with patch.object(async_jobs, "create_job", return_value={"action_status": "completed", "data": {"job_id": "job-2", "status": "queued"}}) as create:
            self.store.manage({"operation": "set_default", "site": "first", "expected_version": 2})
            queued = plugin.sprints({"operation": "getSprint", "path": {"sprintId": 37}}, {})
            self.assertEqual(queued["action_status"], "completed")
            self.assertEqual(queued["data"]["site"], "first")
            self.assertEqual(create.call_args.kwargs["params"]["site"], "first")
            unknown = plugin.sprints({"site": "missing", "operation": "getSprint"}, {})
            self.assertEqual(unknown["action_status"], "error")
            self.assertEqual(unknown["error"]["code"], "jira.unknown_site")
            self.assertEqual(create.call_count, 1)

    def test_rebinding_after_queue_refuses_target_change_before_http(self) -> None:
        plugin = JiraPlugin()
        plugin._sites = self.store
        queued_key = self.store.credential_key("second")
        self.store.manage({"operation": "bind_api_token", "site": "second", "entry_name": "first_entry", "expected_version": 2})
        manager = MagicMock()
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": {"site": "second", "_site_binding_key": queued_key, "operation": "getSprint", "path": {"sprintId": 37}}}}
        async_jobs._process_job(plugin, manager, "queued-before-rebind", "sprints")
        self.assertEqual(manager.update_job.call_args.args[1]["error"]["code"], "jira.binding_changed")
        self.assertEqual(manager.update_job.call_args.args[1]["status"], "error")
        self.assertEqual(self.calls, [])
        # A fresh binding is the normal, shipped pass path.
        fresh_payload: dict[str, Any] = {"site": "second", "_site_binding_key": self.store.credential_key("second"), "operation": "getSprint", "path": {"sprintId": 37}}
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": fresh_payload}}
        async_jobs._process_job(plugin, manager, "queued-after-rebind", "sprints")
        self.assertEqual(manager.update_job.call_args.args[1]["status"], "completed")
        self.assertTrue(self.calls[-1][1].startswith("https://first.atlassian.net/"))

    def test_prose_backed_filter_list_and_scalar_json_property_forms(self) -> None:
        for values in ["scrum", "scrum,kanban", ["scrum", "kanban"]]:
            self.call("boards", "getAllBoards", query={"type": values})
            self.assertIn("type=scrum", self.calls[-1][1])
        for values in ["active", "future,active", ["future", "active"]]:
            self.call("boards", "getAllSprints", {"boardId": 84}, query={"state": values})
            self.assertIn("state=", self.calls[-1][1])
        for raw, expected in [('"team"', "team"), ("[1,2]", [1, 2]), ("null", None)]:
            execute(self.store, "second", "sprints", {"operation": "setProperty", "path": {"sprintId": "37", "propertyKey": "example"}, "body_json": raw})
            self.assertEqual(self.calls[-1][3], expected)

    def test_vendor_secret_echoes_never_reach_logs(self) -> None:
        from base64 import b64encode
        from urllib.parse import quote

        plugin = JiraPlugin()
        plugin._sites = self.store
        stream = io.StringIO()
        plugin.logger = logging.getLogger("jira-transcript")
        plugin.logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(stream)
        plugin.logger.addHandler(handler)
        self.addCleanup(plugin.logger.removeHandler, handler)
        secret = self.store.credentials("second")["api_token"]
        echoes = [secret, quote(secret, safe=""), b64encode(secret.encode()).decode(), json.dumps(secret)[1:-1]]
        for status in [200, 400, 401, 403]:
            self.response = Response(status, {v: [v] for v in echoes}, {})
            plugin._run_for_site({"site": "second"}, lambda: execute(self.store, "second", "sprints", {"operation": "getSprint", "path": {"sprintId": 37}}), "sprints")
        # Log lines are built without bodies, so a vendor echo never reaches them.
        for value in echoes:
            self.assertNotIn(value, stream.getvalue())

    def test_three_process_artifacts_and_complete_vendor_operation_counts(self) -> None:
        plugin = JiraPlugin()
        edges = plugin.get_edge_process_definitions()
        counts = {name: len(INVENTORY["families"][name]) for name in FAMILY_NAMES}
        self.assertEqual(counts, {"projects": 13, "boards": 33, "sprints": 13, "backlog": 2, "agile_issues": 4, "versions": 15, "worklogs": 10})
        for name in edges:
            function = getattr(plugin, name)
            self.assertTrue(callable(function))
            metadata = function._platform_process_metadata
            self.assertIn("site", metadata.parameters)
            article = json.loads((ROOT / "plugins/jira_plugin/knowledge_base/processes" / (name + ".json")).read_text())
            self.assertEqual(article["process_key"], "plugin::jira_plugin::" + name)
            if name in (*FAMILY_NAMES, "sites", "oauth"):
                self.assertGreaterEqual(len(article["embedding_description"]), 200)
                self.assertLessEqual(len(article["embedding_description"]), 400)
        # Registration is inherited from the mixin and must remain discoverable.
        self.assertIn("sprints", dict(inspect.getmembers(plugin, predicate=callable)))

    def test_raw_controls_publish_as_received(self) -> None:
        plugin = JiraPlugin()
        plugin._sites = self.store
        self.response = Response(200, {"values": [{"id": 7}], "startAt": 6, "total": 8, "isLast": False}, {})
        outcome = plugin._run_for_site({"site": "second"}, lambda: self.call("boards", "getAllBoards", query={"startAt": 6}), "boards")
        self.assertEqual(outcome["action_status"], "completed")
        raw = outcome["data"]
        self.assertEqual(raw["status"], 200)
        self.assertEqual(raw["response"]["values"][0]["id"], 7)
        self.assertIs(raw["response"]["isLast"], False)
        self.assertEqual(raw["next_query"]["startAt"], 7)

    def test_early_and_raw_interpretation_faults_never_echo_original_text(self) -> None:
        plugin = JiraPlugin()  # No credentials have been resolved or remembered.
        marker = "unloaded-credential-must-never-escape"
        with patch.object(plugin, "_select_site", side_effect=ValueError(marker)):
            outcome = inspect.unwrap(plugin.get_issue)(plugin, {"issue_key": "DEMO-1"}, {})
            self.assertNotIn(marker, json.dumps(outcome))
        for fault in [ValueError(marker), RuntimeError(marker), JiraServiceError("jira.not_configured", marker)]:
            with patch.object(async_jobs, "create_job", side_effect=fault):
                outcome = plugin._dispatch_async("get_issue", {}, {})
                self.assertNotIn(marker, json.dumps(outcome))
        for result in [{"action_status": "error", "error": {"message": marker}}, {"action_status": "error", "error": None}, None]:
            with patch.object(async_jobs, "create_job", return_value=result):
                outcome = plugin._dispatch_async("get_issue", {}, {})
                self.assertEqual(outcome["action_status"], "error")
                self.assertNotIn(marker, json.dumps(outcome))
        plugin._sites = self.store

        class BrokenOffset(int):
            def __add__(self, other: Any) -> Any:
                raise ValueError(marker)

        self.response = Response(200, {"values": [1], "startAt": BrokenOffset(0), "isLast": False}, {})
        outcome = plugin._run_for_site({"site": "second"}, lambda: self.call("boards", "getAllBoards"), "boards")
        self.assertEqual(outcome["action_status"], "error")
        self.assertNotIn(marker, json.dumps(outcome))
        manager = MagicMock()
        manager.get_job_payload.side_effect = ValueError(marker)
        async_jobs._process_job(plugin, manager, "job-early", "boards")
        terminal = manager.update_job.call_args.args[1]
        self.assertEqual(terminal["status"], "error")
        self.assertNotIn(marker, json.dumps(terminal))


if __name__ == "__main__":
    unittest.main()
