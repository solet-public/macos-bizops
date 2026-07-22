"""Issue verb implementations — pure functions over a built ``jira.JIRA`` client.

Each function takes an already-built client + a ``params`` dict (and, for the
spilling ``jql_search``, an injected ``blob_writer``), and returns a plain result
dict. Blob I/O is kept OUT of this module: ``jql_search`` receives a
``blob_writer`` callable so the plugin owns the blob-storage coupling.

Invalid parameters raise ``ValueError`` (mapped to ``jira.invalid_params``);
``jira.JIRAError`` from the client propagates to the plugin's classifier.

Row shapes are FIXED and TRIMMED: ``jql_search`` returns a small, stable row per
issue; ``get_issue`` returns the fuller single-issue view. Nested Jira objects
(status, assignee, reporter) are flattened None-safely — ``assignee`` is nullable
on unassigned issues, so a bare ``fields['assignee']['displayName']`` would crash.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .constants import (
    INLINE_BYTE_CAP,
    JQL_DEFAULT_MAX_RESULTS,
    JQL_MAX_RESULTS_CAP,
    JQL_SPILL_FILENAME,
)

# blob_writer(content, filename, mime_type) -> blob_id (the returned result_blob_key)
BlobWriter = Callable[[bytes, str, str], str]

# The render set jql_search always fetches + renders. A caller `fields` list ADDS
# to it (never narrows below it): the render set is unioned in so the returned row
# shape is always fully populated — a caller who passes only unrelated fields never
# gets hollow rows (empty summary/status/assignee/updated). See _fetch_fields.
_DEFAULT_JQL_FIELDS: tuple[str, ...] = ("summary", "status", "assignee", "updated")


def jql_search(client: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Run a JQL search; return trimmed rows inline, or spill to a blob if large."""
    jql = _require_str(params, "jql")
    max_results = _clamp(params.get("max_results"), JQL_DEFAULT_MAX_RESULTS, JQL_MAX_RESULTS_CAP)
    fields = _fetch_fields(params.get("fields"))
    # Atlassian removed the legacy /rest/api/*/search endpoint (HTTP 410 since
    # 2026); enhanced_search_issues hits its replacement /search/jql, which is
    # Jira-Cloud-only (matches this connector's ratified Cloud-only scope) and
    # returns no total — nextPageToken presence is the only more-pages signal.
    response = client.enhanced_search_issues(
        jql, maxResults=max_results, fields=fields, json_result=True
    )
    issues = response.get("issues") or []
    total = len(issues)
    if response.get("nextPageToken"):
        total = _as_int(client.approximate_issue_count(jql), default=total)
    rows = [_issue_row(item) for item in issues]
    return _search_envelope(rows, total, blob_writer)


def get_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one issue: summary, description, status, people, labels, attachment meta."""
    key = _require_str(params, "key")
    raw = _resource_raw(client.issue(key))
    fields = raw.get("fields") or {}
    return {
        "key": _as_str(raw.get("key")) or key,
        "summary": _as_str(fields.get("summary")),
        "description": _as_str(fields.get("description")),
        "status": _status_name(fields.get("status")),
        "assignee": _display_name(fields.get("assignee")),
        "reporter": _display_name(fields.get("reporter")),
        "labels": _as_str_list(fields.get("labels")),
        "attachments": _attachment_meta(fields.get("attachment")),
    }


def create_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create an issue in a project of a given type. Extra fields merge over the core three."""
    project = _require_str(params, "project")
    issue_type = _require_str(params, "issue_type")
    summary = _require_str(params, "summary")
    description = _as_str(params.get("description"))
    fields: dict[str, Any] = {
        "project": {"key": project},
        "issuetype": {"name": issue_type},
        "summary": summary,
    }
    if description:
        fields["description"] = description
    extra = params.get("fields")
    if isinstance(extra, dict):
        fields.update(extra)
    created = client.create_issue(fields=fields)
    return {"key": _as_str(getattr(created, "key", None)), "id": _as_str(getattr(created, "id", None))}


def update_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Apply a non-empty ``fields`` object to an existing issue."""
    key = _require_str(params, "key")
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("'fields' is required and must be a non-empty object")
    client.issue(key).update(fields=fields)
    return {"ok": True}


def delete_issue(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Delete an issue by key (explicit target — RATIFY-2 acceptable-loss class)."""
    key = _require_str(params, "key")
    client.issue(key).delete()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Rendering + spill
# ---------------------------------------------------------------------------


def _search_envelope(
    rows: list[dict[str, Any]],
    total: int,
    blob_writer: BlobWriter,
) -> dict[str, Any]:
    """Return rows inline, or spill them to a JSON blob when over the byte cap."""
    payload = json.dumps(rows).encode("utf-8")
    if len(payload) > INLINE_BYTE_CAP:
        blob_key = blob_writer(payload, JQL_SPILL_FILENAME, "application/json")
        return {"result_blob_key": blob_key, "total": total, "row_count": len(rows), "spilled": True}
    return {"issues": rows, "total": total, "row_count": len(rows), "spilled": False}


def _issue_row(item: dict[str, Any]) -> dict[str, Any]:
    fields = item.get("fields") or {}
    return {
        "key": _as_str(item.get("key")),
        "summary": _as_str(fields.get("summary")),
        "status": _status_name(fields.get("status")),
        "assignee": _display_name(fields.get("assignee")),
        "updated": _as_str(fields.get("updated")),
    }


def _attachment_meta(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for att in value:
        if not isinstance(att, dict):
            continue
        out.append(
            {
                "attachment_id": _as_str(att.get("id")),
                "filename": _as_str(att.get("filename")),
                "mime": _as_str(att.get("mimeType")),
                "size": _as_int(att.get("size"), default=0),
            }
        )
    return out


def _status_name(value: Any) -> str | None:
    """Flatten a Jira status object to its name; None if absent."""
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


def _display_name(value: Any) -> str | None:
    """Flatten a Jira user object (assignee/reporter) to its displayName; None-safe.

    Assignee is nullable on unassigned issues, so this must tolerate ``None``.
    """
    if isinstance(value, dict):
        name = value.get("displayName")
        return name if isinstance(name, str) else None
    return None


# ---------------------------------------------------------------------------
# Param coercion + resource access
# ---------------------------------------------------------------------------


def _resource_raw(resource: Any) -> dict[str, Any]:
    """Return a pycontribs Resource's ``.raw`` dict (empty dict if unavailable)."""
    raw = getattr(resource, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _clamp(value: Any, default: int, cap: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(1, min(cap, value))


def _fetch_fields(requested: Any) -> list[str]:
    """Fields to fetch: the render set ALWAYS, plus any caller-requested extras.

    The render set is unioned in first so the rendered rows are never hollow, even
    when the caller passes only fields outside the render set. Order preserved:
    render set, then extras (deduplicated).
    """
    fields = list(_DEFAULT_JQL_FIELDS)
    for name in _as_str_list(requested):
        if name not in fields:
            fields.append(name)
    return fields
