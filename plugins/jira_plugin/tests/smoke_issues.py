#!/usr/bin/env python3
"""JIR-B issue-verb + error-classification smoke tests (no pytest, no live Jira).

Hermetic — a faked JIRA client, synthetic JIRAError objects. Red-first: each
check asserts real parsing / cap / classification / topology-hygiene behavior.

Exercises:
  1.  jql_search — trimmed row shape (key/summary/status/assignee/updated); rides
      /search/jql (enhanced_search_issues) — the legacy /search client method is
      NEVER called (Atlassian removed it, HTTP 410, 2026)
  2.  jql_search — max_results clamped to the 100 cap
  2b. jql_search — total: single page falls back to page length (endpoint returns
      no total); a nextPageToken page consults approximate_issue_count instead
  3.  jql_search — assignee=None (unassigned) does NOT crash; row assignee is None
  4.  jql_search — over-byte-cap result spills to a blob (result_blob_key, spilled)
  5.  get_issue — .raw parse: description/status/people/labels/attachment metadata
  6.  create_issue — core fields + extra 'fields' merge; returns key+id
  7.  update_issue — non-empty fields required; issue.update called
  8.  delete_issue — issue(key).delete() invoked; returns ok
  9.  classify — 401/403/429 -> generic codes with NO exc detail
  10. classify — 404/400 -> detail from exc.text (errorMessages parsed for JSON body)
  11. TOPOLOGY-LEAK (red-first) — site host in JIRAError.url is ABSENT from the
      classified message on BOTH the generic (401) AND detail (404/400) paths
  12. _run — ValueError/AppConfigError/JIRAError/JiraServiceError/broad routing;
      the broad-except path returns a GENERIC message with no host

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 plugins/jira_plugin/tests/smoke_issues.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "jira_plugin" / "src"))

from jira import JIRAError  # noqa: E402
from jira_plugin import issue_actions  # noqa: E402
from jira_plugin.app_config import AppConfigError  # noqa: E402
from jira_plugin.constants import (  # noqa: E402
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_BAD_REQUEST,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION_DENIED,
    ERROR_RATE_LIMITED,
    INLINE_BYTE_CAP,
)
from jira_plugin.errors import JiraServiceError, classify_jira_error  # noqa: E402
from jira_plugin.plugin import JiraPlugin  # noqa: E402

_SECRET_HOST = "SECRETHOST.atlassian.net"
_HOST_URL = f"https://{_SECRET_HOST}/rest/api/2/issue/EXAMPLE-1"

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _unused_writer(content: bytes, filename: str, mime_type: str) -> str:  # pragma: no cover
    raise AssertionError("blob writer should not be called on the inline path")


# ---------------------------------------------------------------------------
# jql_search
# ---------------------------------------------------------------------------


def _issue(key: str, summary: str, status: str, assignee: str | None, updated: str) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": summary,
        "status": {"name": status},
        "assignee": {"displayName": assignee} if assignee is not None else None,
        "updated": updated,
    }
    return {"key": key, "fields": fields}


def test_jql_row_shape() -> None:
    client = MagicMock()
    client.enhanced_search_issues.return_value = {
        "issues": [
            _issue("EXAMPLE-1", "Fix bug", "In Progress", "Alice", "2026-07-01T00:00:00Z"),
            _issue("EXAMPLE-2", "Add docs", "To Do", None, "2026-07-02T00:00:00Z"),
        ],
    }
    result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE"}, _unused_writer)
    _assert("legacy /search never called (410-removed)", client.search_issues.call_count == 0)
    _assert("single page skips approximate count", client.approximate_issue_count.call_count == 0)
    _assert("not spilled", result["spilled"] is False)
    _assert("total falls back to page length", result["total"] == 2)
    _assert("row_count is 2", result["row_count"] == 2)
    row = result["issues"][0]
    _assert("row keys are the trimmed set", set(row) == {"key", "summary", "status", "assignee", "updated"})
    _assert("status flattened to name", row["status"] == "In Progress")
    _assert("assignee flattened to displayName", row["assignee"] == "Alice")
    _assert("unassigned assignee is None (no crash)", result["issues"][1]["assignee"] is None)


def test_jql_clamps_max() -> None:
    client = MagicMock()
    client.enhanced_search_issues.return_value = {"issues": []}
    issue_actions.jql_search(client, {"jql": "project = EXAMPLE", "max_results": 500}, _unused_writer)
    kwargs = client.enhanced_search_issues.call_args.kwargs
    _assert("maxResults clamped to 100", kwargs.get("maxResults") == 100)


def test_jql_truncated_page_uses_approximate_count() -> None:
    # /search/jql returns no total; when nextPageToken signals more pages, total
    # comes from the approximate-count endpoint queried with the SAME JQL. To see
    # red, drop the nextPageToken branch in jql_search.
    client = MagicMock()
    client.enhanced_search_issues.return_value = {
        "issues": [_issue("EXAMPLE-1", "x", "Done", "Bob", "2026-07-01T00:00:00Z")],
        "nextPageToken": "tok-next",
    }
    client.approximate_issue_count.return_value = 137
    result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE"}, _unused_writer)
    _assert("total from approximate count", result["total"] == 137)
    _assert(
        "approximate count queried with same JQL",
        client.approximate_issue_count.call_args.args[0] == "project = EXAMPLE",
    )


def test_jql_fields_union_no_hollow_rows() -> None:
    # R1 (Rev-B): a caller `fields` list ADDS to the render set — the render set is
    # always fetched, so rows are never hollow even when the caller asks only for
    # unrelated fields. To see red, revert _fetch_fields to `caller or defaults`.
    client = MagicMock()
    client.enhanced_search_issues.return_value = {
        "issues": [_issue("EXAMPLE-1", "Fix bug", "In Progress", "Alice", "2026-07-01T00:00:00Z")],
    }
    result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE", "fields": ["priority"]}, _unused_writer)
    fetched = client.enhanced_search_issues.call_args.kwargs.get("fields")
    for render_field in ("summary", "status", "assignee", "updated"):
        _assert(f"render field '{render_field}' still fetched", render_field in fetched)
    _assert("caller extra field appended", "priority" in fetched)
    _assert("no duplicate render fields", len(fetched) == len(set(fetched)))
    _assert("row not hollow — summary populated", result["issues"][0]["summary"] == "Fix bug")


def test_jql_spills_over_byte_cap() -> None:
    original_cap = issue_actions.INLINE_BYTE_CAP
    issue_actions.INLINE_BYTE_CAP = 10  # force spill for a tiny result set
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["filename"] = filename
        captured["mime"] = mime_type
        return "bl-jql-1"

    try:
        client = MagicMock()
        client.enhanced_search_issues.return_value = {
            "issues": [_issue("EXAMPLE-1", "x", "Done", "Bob", "2026-07-01T00:00:00Z")],
        }
        result = issue_actions.jql_search(client, {"jql": "project = EXAMPLE"}, writer)
    finally:
        issue_actions.INLINE_BYTE_CAP = original_cap
    _assert("spilled True", result["spilled"] is True)
    _assert("returns result_blob_key", result["result_blob_key"] == "bl-jql-1")
    _assert("no inline issues on spill", "issues" not in result)
    _assert("spill filename is json", captured.get("filename", "").endswith(".json"))


def test_jql_requires_jql() -> None:
    raised = False
    try:
        issue_actions.jql_search(MagicMock(), {}, _unused_writer)
    except ValueError:
        raised = True
    _assert("missing jql raises ValueError", raised)


# ---------------------------------------------------------------------------
# get_issue / create / update / delete
# ---------------------------------------------------------------------------


def test_get_issue_parses_raw() -> None:
    client = MagicMock()
    client.issue.return_value.raw = {
        "key": "EXAMPLE-9",
        "fields": {
            "summary": "Ship it",
            "description": "the full body",
            "status": {"name": "Done"},
            "assignee": None,
            "reporter": {"displayName": "Carol"},
            "labels": ["backend", "urgent"],
            "attachment": [
                {"id": "att-1", "filename": "log.txt", "mimeType": "text/plain", "size": "42"}
            ],
        },
    }
    result = issue_actions.get_issue(client, {"key": "EXAMPLE-9"})
    _assert("summary parsed", result["summary"] == "Ship it")
    _assert("description parsed", result["description"] == "the full body")
    _assert("status flattened", result["status"] == "Done")
    _assert("unassigned -> None", result["assignee"] is None)
    _assert("reporter flattened", result["reporter"] == "Carol")
    _assert("labels carried", result["labels"] == ["backend", "urgent"])
    att = result["attachments"][0]
    _assert("attachment id parsed", att["attachment_id"] == "att-1")
    _assert("attachment size coerced to int", att["size"] == 42)


def test_create_issue_merges_fields() -> None:
    client = MagicMock()
    created = MagicMock()
    created.key = "EXAMPLE-100"
    created.id = "10100"
    client.create_issue.return_value = created
    result = issue_actions.create_issue(
        client,
        {
            "project": "EXAMPLE",
            "issue_type": "Task",
            "summary": "New task",
            "description": "desc",
            "fields": {"priority": {"name": "High"}},
        },
    )
    _assert("returns key", result["key"] == "EXAMPLE-100")
    _assert("returns id", result["id"] == "10100")
    sent = client.create_issue.call_args.kwargs["fields"]
    _assert("project key set", sent["project"] == {"key": "EXAMPLE"})
    _assert("issuetype name set", sent["issuetype"] == {"name": "Task"})
    _assert("extra field merged", sent["priority"] == {"name": "High"})


def test_update_issue_requires_fields() -> None:
    raised = False
    try:
        issue_actions.update_issue(MagicMock(), {"key": "EXAMPLE-1", "fields": {}})
    except ValueError:
        raised = True
    _assert("empty fields raises ValueError", raised)


def test_update_issue_applies() -> None:
    client = MagicMock()
    result = issue_actions.update_issue(client, {"key": "EXAMPLE-1", "fields": {"summary": "new"}})
    _assert("update returns ok", result["ok"] is True)
    client.issue.return_value.update.assert_called_once_with(fields={"summary": "new"})


def test_delete_issue() -> None:
    client = MagicMock()
    result = issue_actions.delete_issue(client, {"key": "EXAMPLE-7"})
    _assert("delete returns ok", result["ok"] is True)
    _assert("issue(key).delete() invoked", client.issue.return_value.delete.called)


def test_delete_requires_key() -> None:
    raised = False
    try:
        issue_actions.delete_issue(MagicMock(), {})
    except ValueError:
        raised = True
    _assert("missing key raises ValueError", raised)


# ---------------------------------------------------------------------------
# classification + topology leak
# ---------------------------------------------------------------------------


def test_classify_generic_codes() -> None:
    for status, expected in (
        (401, ERROR_AUTH_FAILED),
        (403, ERROR_PERMISSION_DENIED),
        (429, ERROR_RATE_LIMITED),
    ):
        exc = JIRAError(text="raw provider detail", status_code=status, url=_HOST_URL)
        code, message = classify_jira_error(exc)
        _assert(f"{status} -> {expected}", code == expected)
        _assert(f"{status} message omits exc detail (generic)", "raw provider detail" not in message)


def test_classify_detail_codes() -> None:
    exc404 = JIRAError(text="Issue does not exist", status_code=404, url=_HOST_URL)
    code, message = classify_jira_error(exc404)
    _assert("404 -> not_found", code == ERROR_NOT_FOUND)
    _assert("404 includes body detail", "Issue does not exist" in message)

    exc400 = JIRAError(
        text='{"errorMessages":["Field foo is required"],"errors":{}}',
        status_code=400,
        url=_HOST_URL,
    )
    code, message = classify_jira_error(exc400)
    _assert("400 -> bad_request", code == ERROR_BAD_REQUEST)
    _assert("400 parses errorMessages from JSON body", "Field foo is required" in message)


def test_topology_leak_absent_both_paths() -> None:
    # RED-FIRST: str(JIRAError) embeds exc.url (= the site host). The classifier
    # builds detail from exc.text ONLY, so the host must be absent on BOTH the
    # generic (401) and the detail (404/400) paths. To see red, change errors.py
    # to use str(exc)/f"{exc}" and the host reappears here.
    for status in (401, 403, 429, 404, 400):
        exc = JIRAError(text="body detail", status_code=status, url=_HOST_URL)
        _, message = classify_jira_error(exc)
        _assert(f"host absent from {status} message", _SECRET_HOST not in message, message)


# ---------------------------------------------------------------------------
# _run routing (via a real plugin instance, no readiness needed)
# ---------------------------------------------------------------------------


def test_run_routes_errors() -> None:
    plugin = JiraPlugin()

    def raise_value() -> dict[str, Any]:
        raise ValueError("bad param")

    def raise_config() -> dict[str, Any]:
        raise AppConfigError("address_book_entry_missing", "register the jira_site entry")

    def raise_jira() -> dict[str, Any]:
        raise JIRAError(text="nope", status_code=403, url=_HOST_URL)

    def raise_service() -> dict[str, Any]:
        raise JiraServiceError("blob_storage_service_not_available", "no blob service")

    def raise_broad() -> dict[str, Any]:
        raise RuntimeError(f"could not connect to {_SECRET_HOST}:443")

    _assert("ValueError -> invalid_params", plugin._run(raise_value, "v")["error"]["code"] == ERROR_INVALID_PARAMS)
    _assert("AppConfigError -> not_configured", plugin._run(raise_config, "v")["error"]["code"] == ERROR_NOT_CONFIGURED)
    _assert("JIRAError -> classified", plugin._run(raise_jira, "v")["error"]["code"] == ERROR_PERMISSION_DENIED)
    _assert(
        "JiraServiceError -> its code",
        plugin._run(raise_service, "v")["error"]["code"] == "blob_storage_service_not_available",
    )
    broad = plugin._run(raise_broad, "v")
    _assert("broad -> api_error", broad["error"]["code"] == ERROR_API_ERROR)
    _assert("broad message omits the host (generic)", _SECRET_HOST not in broad["error"]["message"])


def test_run_success_shape() -> None:
    plugin = JiraPlugin()
    ok = plugin._run(lambda: {"x": 1}, "v")
    _assert("success action_status", ok["action_status"] == "completed")
    _assert("success carries data", ok["data"] == {"x": 1})
    _assert("success error is None", ok["error"] is None)


# ---------------------------------------------------------------------------
# Blob spill service resolution
# ---------------------------------------------------------------------------


def test_store_blob_resolves_service_at_point_of_use() -> None:
    """§20.1 regression: blob_storage_service is constructed AFTER plugin
    readiness, so readiness-time resolution cached None forever and every
    spill hard-failed; the fix resolves lazily at first use."""
    plugin = JiraPlugin()
    blob_service = MagicMock()
    blob_service.store_blob.return_value = {
        "action_status": "completed",
        "data": {"blob_id": "blob-jql-1"},
    }
    orch = MagicMock()
    orch.get_service.return_value = blob_service
    plugin.orchestrator_ref = orch
    blob_id = plugin._store_blob(b"x" * 64, "jql_results.json", "application/json")
    _assert("spill succeeds via point-of-use resolution", blob_id == "blob-jql-1")
    plugin._store_blob(b"y", "again.json", "application/json")
    _assert(
        "one get_service call across two spills (cached)",
        orch.get_service.call_count == 1,
        str(orch.get_service.call_count),
    )


def test_store_blob_unavailable_error_is_self_describing() -> None:
    plugin = JiraPlugin()
    orch = MagicMock()
    orch.get_service.return_value = None
    plugin.orchestrator_ref = orch
    raised: JiraServiceError | None = None
    try:
        plugin._store_blob(b"z" * 54321, "jql_results.json", "application/json")
    except JiraServiceError as exc:
        raised = exc
    _assert("unavailable blob storage raises the typed error", raised is not None)
    message = str(raised)
    _assert("error names the observed payload size", "54321" in message, message)
    _assert("error names the inline cap", str(INLINE_BYTE_CAP) in message, message)


def main() -> int:
    print("\njira_plugin JIR-B issue + classification smoke tests")
    print("=" * 40)
    test_jql_row_shape()
    test_jql_clamps_max()
    test_jql_truncated_page_uses_approximate_count()
    test_jql_fields_union_no_hollow_rows()
    test_jql_spills_over_byte_cap()
    test_jql_requires_jql()
    test_get_issue_parses_raw()
    test_create_issue_merges_fields()
    test_update_issue_requires_fields()
    test_update_issue_applies()
    test_delete_issue()
    test_delete_requires_key()
    test_classify_generic_codes()
    test_classify_detail_codes()
    test_topology_leak_absent_both_paths()
    test_run_routes_errors()
    test_run_success_shape()
    test_store_blob_resolves_service_at_point_of_use()
    test_store_blob_unavailable_error_is_self_describing()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All JIR-B issue smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
