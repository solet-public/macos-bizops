#!/usr/bin/env python3
"""JIR-C comment + transition smoke tests (no pytest, no live Jira).

Hermetic — a faked JIRA client with .raw-bearing Comment resources and
list-of-dict transitions. Red-first: each check asserts real parsing / cap /
transition behavior.

Exercises:
  1. add_comment      — returns the new comment id; passes (key, body) through
  2. add_comment      — missing body raises ValueError
  3. list_comments    — .raw parse (id/author/body/created); author None-safe
  4. list_comments    — cap enforcement (max slices the returned list)
  5. list_transitions — list-of-dict transitions -> {id,name,to_status}
  6. transition_issue — transition invoked with id+comment; re-fetch -> new_status
  7. transition_issue — missing transition_id raises ValueError

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 plugins/jira_plugin/tests/smoke_comments.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "jira_plugin" / "src"))

from jira_plugin import comment_actions  # noqa: E402

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


def _comment(raw: dict[str, Any]) -> MagicMock:
    c = MagicMock()
    c.raw = raw
    return c


def test_add_comment() -> None:
    client = MagicMock()
    created = MagicMock()
    created.id = "cmt-1"
    client.add_comment.return_value = created
    result = comment_actions.add_comment(client, {"key": "EXAMPLE-1", "body": "looks good"})
    _assert("returns comment_id", result["comment_id"] == "cmt-1")
    args = client.add_comment.call_args.args
    _assert("passes key + body", args == ("EXAMPLE-1", "looks good"))


def test_add_comment_requires_body() -> None:
    raised = False
    try:
        comment_actions.add_comment(MagicMock(), {"key": "EXAMPLE-1"})
    except ValueError:
        raised = True
    _assert("missing body raises ValueError", raised)


def test_list_comments_parse() -> None:
    client = MagicMock()
    client.comments.return_value = [
        _comment(
            {
                "id": "c1",
                "author": {"displayName": "Alice"},
                "body": "first",
                "created": "2026-07-01T00:00:00Z",
            }
        ),
        _comment({"id": "c2", "author": None, "body": "second", "created": "2026-07-02T00:00:00Z"}),
    ]
    result = comment_actions.list_comments(client, {"key": "EXAMPLE-1"})
    rows = result["comments"]
    _assert("two comments", len(rows) == 2)
    _assert("author flattened", rows[0]["author"] == "Alice")
    _assert("body parsed", rows[0]["body"] == "first")
    _assert("null author -> None (no crash)", rows[1]["author"] is None)


def test_list_comments_caps() -> None:
    client = MagicMock()
    client.comments.return_value = [
        _comment({"id": f"c{i}", "author": None, "body": "x", "created": "2026-07-01T00:00:00Z"})
        for i in range(5)
    ]
    result = comment_actions.list_comments(client, {"key": "EXAMPLE-1", "max": 2})
    _assert("client-side cap to 2", len(result["comments"]) == 2)
    _assert("max_results passed to api", client.comments.call_args.kwargs.get("max_results") == 2)


def test_list_transitions() -> None:
    client = MagicMock()
    client.transitions.return_value = [
        {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
        {"id": "21", "name": "Done", "to": {"name": "Done"}},
    ]
    result = comment_actions.list_transitions(client, {"key": "EXAMPLE-1"})
    rows = result["transitions"]
    _assert("two transitions", len(rows) == 2)
    _assert("id carried", rows[0]["id"] == "11")
    _assert("name carried", rows[0]["name"] == "Start Progress")
    _assert("to_status flattened", rows[0]["to_status"] == "In Progress")


def test_transition_issue() -> None:
    client = MagicMock()
    client.issue.return_value.raw = {"fields": {"status": {"name": "In Progress"}}}
    result = comment_actions.transition_issue(
        client, {"key": "EXAMPLE-1", "transition_id": "11", "comment": "moving it"}
    )
    _assert("returns ok", result["ok"] is True)
    _assert("new_status re-fetched", result["new_status"] == "In Progress")
    args = client.transition_issue.call_args
    _assert("transition invoked with key+id", args.args == ("EXAMPLE-1", "11"))
    _assert("comment passed through", args.kwargs.get("comment") == "moving it")


def test_transition_requires_id() -> None:
    raised = False
    try:
        comment_actions.transition_issue(MagicMock(), {"key": "EXAMPLE-1"})
    except ValueError:
        raised = True
    _assert("missing transition_id raises ValueError", raised)


def main() -> int:
    print("\njira_plugin JIR-C comment + transition smoke tests")
    print("=" * 40)
    test_add_comment()
    test_add_comment_requires_body()
    test_list_comments_parse()
    test_list_comments_caps()
    test_list_transitions()
    test_transition_issue()
    test_transition_requires_id()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All JIR-C comment smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
