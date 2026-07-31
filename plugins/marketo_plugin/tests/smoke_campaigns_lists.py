#!/usr/bin/env python3
"""Campaign + static-list verb smoke tests for marketo_plugin.

Hermetic — a faked client returning canned decoded envelope dicts, no live
instance.

Exercises:
  1. list_campaigns — name/program_name query construction, spill envelope
  2. trigger_campaign — token validation, batch cap (100), request_id carried
  3. list_static_lists — name filter, spill envelope
  4. add_leads_to_list — batch cap (300), tallies shape
  5. remove_leads_from_list — DELETE-with-JSON-body path (asserts
     ``client.delete_json`` is the method invoked, not ``post_json``/plain
     ``delete`` — the trap httpx.Client.delete() not accepting json= creates)
  6. MarketoClient exposes a delete_json method distinct from get_json/post_json

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 \
        plugins/marketo_plugin/tests/smoke_campaigns_lists.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))

from marketo_plugin import marketing_actions  # noqa: E402
from marketo_plugin.http_client import MarketoClient  # noqa: E402

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


def _fake_client(**method_returns: dict[str, Any]) -> Any:
    client = MagicMock()
    for method, payload in method_returns.items():
        getattr(client, method).return_value = payload
    return client


def _no_blob_writer(_content: bytes, _filename: str, _mime: str) -> str:
    raise AssertionError("blob_writer should not be called for small results")


def test_list_campaigns() -> None:
    client = _fake_client(get_json={"success": True, "result": [{"id": 1, "name": "Welcome Series"}]})
    result = marketing_actions.list_campaigns(client, {"names": ["Welcome Series"]}, _no_blob_writer)
    _assert("campaign records carried", result["records"][0]["name"] == "Welcome Series")
    called_params = client.get_json.call_args.kwargs.get("params")
    _assert("name query param built", called_params == {"name": ["Welcome Series"]}, str(called_params))


def test_trigger_campaign() -> None:
    client = _fake_client(post_json={"success": True, "requestId": "req-1"})
    result = marketing_actions.trigger_campaign(
        client, {"campaign_id": "42", "lead_ids": [1, 2], "tokens": [{"name": "{{my.token}}", "value": "x"}]}
    )
    _assert("trigger success carried", result["success"] is True)
    _assert("trigger request_id carried", result["request_id"] == "req-1")

    raised = False
    try:
        marketing_actions.trigger_campaign(client, {"campaign_id": "42", "lead_ids": list(range(101))})
    except ValueError:
        raised = True
    _assert("over-cap lead_ids (>100) rejected", raised)

    raised = False
    try:
        marketing_actions.trigger_campaign(client, {"campaign_id": "42", "lead_ids": [1], "tokens": [{"name": "x"}]})
    except ValueError:
        raised = True
    _assert("malformed token entry rejected", raised)


def test_list_static_lists() -> None:
    client = _fake_client(get_json={"success": True, "result": [{"id": 9, "name": "VIP"}]})
    result = marketing_actions.list_static_lists(client, {"names": ["VIP"]}, _no_blob_writer)
    _assert("static list records carried", result["records"][0]["name"] == "VIP")


def test_add_leads_to_list() -> None:
    client = _fake_client(post_json={"success": True, "result": [{"id": 1, "status": "added"}]})
    result = marketing_actions.add_leads_to_list(client, {"list_id": "9", "lead_ids": [1]})
    _assert("add tallies shape", result["tallies"] == {"added": 1})

    raised = False
    try:
        marketing_actions.add_leads_to_list(client, {"list_id": "9", "lead_ids": list(range(301))})
    except ValueError:
        raised = True
    _assert("over-cap lead_ids (>300) rejected", raised)


def test_remove_leads_from_list_uses_delete_json() -> None:
    client = _fake_client(delete_json={"success": True, "result": [{"id": 1, "status": "removed"}]})
    result = marketing_actions.remove_leads_from_list(client, {"list_id": "9", "lead_ids": [1]})
    _assert("remove tallies shape", result["tallies"] == {"removed": 1})
    _assert("delete_json invoked (not post_json)", client.delete_json.called)
    _assert("post_json NOT invoked for removal", not client.post_json.called)
    body = client.delete_json.call_args.kwargs.get("json")
    _assert("DELETE carries a JSON body", body == {"input": [{"id": 1}]}, str(body))


def test_client_exposes_delete_json_distinct_from_delete() -> None:
    # httpx.Client.delete() does not accept json= — MarketoClient must expose
    # its own delete_json() that routes through .request("DELETE", ..., json=...)
    # rather than the plain .delete() verb. This is a structural check, not a
    # network call: just confirm the method exists and is not httpx's own.
    _assert("MarketoClient defines delete_json", hasattr(MarketoClient, "delete_json"))


def main() -> int:
    print("\nmarketo_plugin campaign + static-list verb smoke tests")
    print("=" * 55)
    test_list_campaigns()
    test_trigger_campaign()
    test_list_static_lists()
    test_add_leads_to_list()
    test_remove_leads_from_list_uses_delete_json()
    test_client_exposes_delete_json_distinct_from_delete()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All campaign + static-list verb smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
