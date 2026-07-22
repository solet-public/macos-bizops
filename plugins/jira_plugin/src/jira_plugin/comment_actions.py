"""Comment + transition verb implementations — pure functions over a JIRA client.

Same shape as issue_actions: take an already-built ``jira.JIRA`` client + a
``params`` dict, return plain result dicts, ``raise ValueError`` on bad params.
No blob I/O here. Nested Jira objects (comment author, transition target status)
are flattened None-safely.
"""

from __future__ import annotations

from typing import Any

from .constants import COMMENTS_DEFAULT_MAX, COMMENTS_MAX_CAP


def add_comment(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Add a plain-text comment to an issue; return the new comment id."""
    key = _require_str(params, "key")
    body = _require_str(params, "body")
    comment = client.add_comment(key, body)
    return {"comment_id": _as_str(getattr(comment, "id", None))}


def list_comments(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List an issue's comments (author/body/created), capped."""
    key = _require_str(params, "key")
    max_results = _clamp(params.get("max"), COMMENTS_DEFAULT_MAX, COMMENTS_MAX_CAP)
    comments = client.comments(key, max_results=max_results)
    rows = [_comment_row(c) for c in _as_list(comments)[:max_results]]
    return {"comments": rows}


def list_transitions(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List the transitions available from the issue's current status."""
    key = _require_str(params, "key")
    transitions = client.transitions(key)
    rows = [_transition_row(t) for t in _as_list(transitions) if isinstance(t, dict)]
    return {"transitions": rows}


def transition_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Move an issue through a transition (explicit id), optionally with a comment."""
    key = _require_str(params, "key")
    transition_id = _require_str(params, "transition_id")
    comment = _as_str(params.get("comment")) or None
    client.transition_issue(key, transition_id, comment=comment)
    raw = _resource_raw(client.issue(key))
    new_status = _status_name((raw.get("fields") or {}).get("status"))
    return {"ok": True, "new_status": new_status}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _comment_row(comment: Any) -> dict[str, Any]:
    raw = _resource_raw(comment)
    return {
        "id": _as_str(raw.get("id")),
        "author": _display_name(raw.get("author")),
        "body": _as_str(raw.get("body")),
        "created": _as_str(raw.get("created")),
    }


def _transition_row(transition: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _as_str(transition.get("id")),
        "name": _as_str(transition.get("name")),
        "to_status": _status_name(transition.get("to")),
    }


def _status_name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


def _display_name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("displayName")
        return name if isinstance(name, str) else None
    return None


# ---------------------------------------------------------------------------
# Param coercion + resource access
# ---------------------------------------------------------------------------


def _resource_raw(resource: Any) -> dict[str, Any]:
    raw = getattr(resource, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _clamp(value: Any, default: int, cap: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(1, min(cap, value))
