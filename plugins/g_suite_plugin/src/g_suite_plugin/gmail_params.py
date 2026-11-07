"""Parameter coercion shared by every Gmail verb.

Invalid parameters raise ``ValueError``; the plugin maps that to
``gsuite.invalid_params`` with the message intact, so each message names the
parameter and the rule it broke.
"""

from __future__ import annotations

from typing import Any

from .constants import GMAIL_BATCH_IDS_CAP, GMAIL_DEFAULT_MAX_RESULTS, GMAIL_LABEL_IDS_CAP, GMAIL_MAX_RESULTS_CAP

# Convenience flags on the modify verbs, mapped to Gmail system labels:
# flag -> (label id, add the label when the flag is True?)
_LABEL_FLAGS: dict[str, tuple[str, bool]] = {
    "mark_read": ("UNREAD", False),
    "archive": ("INBOX", False),
    "star": ("STARRED", True),
}


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]  # pyright: ignore[reportUnknownVariableType]


def optional_bool(params: dict[str, Any], key: str) -> bool | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"'{key}' must be true or false")
    return value


def clamp_max(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return GMAIL_DEFAULT_MAX_RESULTS
    return max(1, min(GMAIL_MAX_RESULTS_CAP, value))


def list_args(params: dict[str, Any]) -> dict[str, Any]:
    """Shared paging/filter kwargs for messages.list, threads.list and drafts.list."""
    kwargs: dict[str, Any] = {"userId": "me", "maxResults": clamp_max(params.get("max"))}
    query = as_str(params.get("query"))
    if query:
        kwargs["q"] = query
    page_token = as_str(params.get("page_token"))
    if page_token:
        kwargs["pageToken"] = page_token
    if optional_bool(params, "include_spam_trash"):
        kwargs["includeSpamTrash"] = True
    return kwargs


def require_ids(params: dict[str, Any], key: str = "ids") -> list[str]:
    ids = as_str_list(params.get(key))
    if not ids:
        raise ValueError(f"'{key}' is required: a non-empty list of Gmail ids")
    if len(ids) > GMAIL_BATCH_IDS_CAP:
        raise ValueError(f"'{key}' holds {len(ids)} ids; Gmail accepts at most {GMAIL_BATCH_IDS_CAP} per call")
    return ids


def label_changes(params: dict[str, Any]) -> dict[str, list[str]]:
    """Build a modify body from add/remove label ids plus the convenience flags.

    mark_read true removes UNREAD (false adds it); archive true removes INBOX
    (false moves back to the inbox); star true adds STARRED (false removes it).
    """
    add = as_str_list(params.get("add_label_ids"))
    remove = as_str_list(params.get("remove_label_ids"))
    for flag, (label, add_when_true) in _LABEL_FLAGS.items():
        value = optional_bool(params, flag)
        if value is None:
            continue
        target = add if value == add_when_true else remove
        if label not in target:
            target.append(label)
    if not add and not remove:
        raise ValueError("nothing to change: pass add_label_ids, remove_label_ids, mark_read, archive or star")
    overlap = sorted(set(add) & set(remove))
    if overlap:
        raise ValueError(f"label ids cannot be both added and removed: {overlap}")
    if len(add) > GMAIL_LABEL_IDS_CAP or len(remove) > GMAIL_LABEL_IDS_CAP:
        raise ValueError(f"Gmail accepts at most {GMAIL_LABEL_IDS_CAP} label ids per add or remove list")
    return {"addLabelIds": add, "removeLabelIds": remove}


def page_fields(response: dict[str, Any]) -> dict[str, Any]:
    """The paging fields every Gmail list response carries."""
    return {
        "next_page_token": as_str(response.get("nextPageToken")),
        "result_size_estimate": int(response.get("resultSizeEstimate") or 0),
    }
