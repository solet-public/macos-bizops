#!/usr/bin/env python3
"""Hermetic coverage for the ``peer_inbox`` forward ``since`` cursor (iss_17aefd54).

``after`` is the BACKWARD newest-first walking cursor (it returns rows OLDER
than the timestamp); the tool text called it a forward oldest-first cursor, so a
caller polling "what is new since T" with ``after=T`` was handed history. This
smoke pins the honest pair: ``since`` is a strict forward cursor (rows NEWER
than the timestamp, oldest-first, truthful exhaustion, a cursor that advances),
``after`` keeps its backward contract, and naming both is refused. Real
service, real bounded ordered-query fake, no DB.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402, TC002
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.schema import NAMESPACE, TABLE_AGENT_MESSAGE  # noqa: E402
from peer_inbox_instance_exhaustion_smoke import (  # noqa: E402
    _AGENT_ID,
    _INSTANCE_ID,
    _SESSION_ID,
    _START,
    _plugin,
    _seed,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _at(index: int) -> str:
    """The seeded ``created_at`` of row ``index`` (naive ISO, as the store holds it)."""
    return (_START + timedelta(seconds=index)).replace(tzinfo=None).isoformat()


def _call(plugin: Any, **params: object) -> dict[str, Any]:
    return cast("dict[str, Any]", plugin.peer_inbox_action({"agent_session_id": _SESSION_ID, **params}, {}))


def _read(plugin: Any, **params: object) -> dict[str, Any]:
    result = _call(plugin, **params)
    assert result["action_status"] == "completed", result
    return cast("dict[str, Any]", result["data"])


def _cursors(data: dict[str, Any]) -> list[int]:
    return [entry["message"]["cursor"] for entry in data["entries"]]


def test_since_returns_only_newer_rows_oldest_first() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    page = _read(_plugin(state), since=_at(2), limit=50)
    _check(_cursors(page) == [3, 4, 5], "since=T(row 2) returns exactly the rows newer than T, oldest-first")
    _check(page["instance_exhausted"] is True, "the since page that reaches the newest row is exhausted")
    _check(page["next_since_created_at"] == _at(5), "the since cursor advances to the last returned row")
    _check(page["next_after_created_at"] is None, "a since page offers no backward cursor to misuse")


def test_cov_boundaries() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    plugin = _plugin(state)
    first = _read(plugin, since=_at(-1), limit=2, observer=True)
    token = first["next_since_cursor"]
    second = _read(plugin, since_cursor=token, limit=2, observer=True)
    direct = second["read_coverage"]["direct"]
    _check(direct["effective_since"] == _at(-1), "cov: continuation retains original horizon")
    _check(direct["since_cursor"] == token and direct["requested_since"] is None, "cov: supplied continuation retained")
    _check(direct["continuation_safety"] == "tie_safe", "cov: opaque continuation tie safe")
    _check(direct["continuation_after"] == first["read_coverage"]["direct"]["last"], "cov: decoded boundary exact")
    _check(direct["first"] != direct["last"], "cov: distinct endpoints")
    _check(direct["observer"] is True and direct["unread_count"] is None, "cov: observer unread unknown")
    # A manual horizon jump skips the already seeded row 2 outside B's query.
    later = _read(plugin, since=_at(3), limit=2)
    _check(first["read_coverage"]["direct"]["last"]["created_at"] == _at(1) and later["read_coverage"]["direct"]["requested_since"] == _at(3), "cov: manual horizon jump visible")
    _check(later["read_coverage"]["direct"]["outside_query"] == "unknown", "cov: skipped interval unverified")
    for params in ({"since": _at(5)}, {"after": _at(0)}):
        empty = _read(plugin, **params)["read_coverage"]["direct"]
        _check(empty["count"] == 0 and empty["first"] is None and empty["last"] is None, "cov: empty null endpoints")
        boundary = "requested_since" if "since" in params else "after"
        _check(empty[boundary] == next(iter(params.values())), "cov: empty request boundary retained")


def test_cov_ties() -> None:
    state = RealShapeState()
    _seed(state, count=12, threads=2)
    for row in state.rows(NAMESPACE, TABLE_AGENT_MESSAGE):
        row["created_at"] = _at(0)
    plugin = _plugin(state)
    first = _read(plugin, since=_at(-1), limit=5)
    cov = first["read_coverage"]["direct"]
    _check(cov["continuation_safety"] == "horizon_only" and "next_since_cursor" in cov["advisory"], "cov: tied forward timestamp warning")
    skipped = _read(plugin, since=first["next_since_created_at"], limit=5)
    _check(len(first["entries"]) == 5 and skipped["entries"] == [], "cov: timestamp tie omission unchanged")
    backward = _read(plugin, limit=5)
    cov = backward["read_coverage"]["direct"]
    _check(cov["continuation_safety"] == "timestamp_only" and "tied rows" in cov["advisory"], "cov: tied backward timestamp warning")
    older = _read(plugin, after=backward["next_after_created_at"], limit=5)
    _check(older["entries"] == [] and older["read_coverage"]["direct"]["after"] == _at(0), "cov: backward tie omission unchanged")


def test_since_is_strict_and_not_the_backward_cursor() -> None:
    """The reported shape: a timestamp past the newest row got history back."""
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    plugin = _plugin(state)
    past_newest = _read(plugin, since=_at(5))
    _check(past_newest["entries"] == [], "since=T(newest) returns nothing: the boundary row is not re-read")
    _check(past_newest["instance_exhausted"] is True, "nothing newer than the newest row is exhausted")
    _check(past_newest["next_since_created_at"] == _at(5), "an empty since page keeps the caller's cursor")
    backward = _read(plugin, after=_at(5), limit=50)
    _check(_cursors(backward) == [4, 3, 2, 1, 0], "after keeps its documented backward newest-first contract")
    _check(backward["next_since_created_at"] is None, "an after page carries no since cursor")


def test_since_offset_forms_mean_the_same_instant() -> None:
    """``created_at`` is naive UTC: an offset-bearing cursor must compare as the same instant."""
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    plugin = _plugin(state)
    for label, value in (
        ("Z suffix", _at(2) + "Z"),
        ("+00:00", _at(2) + "+00:00"),
        ("+02:00 local time of the same instant", "2026-08-01T02:00:02+02:00"),
    ):
        _check(_cursors(_read(plugin, since=value)) == [3, 4, 5], f"since with {label} bounds the same instant as naive UTC")


def test_after_offset_forms_mean_the_same_instant() -> None:
    """The shared ordered-query seam normalizes aware backward cursors."""
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    plugin = _plugin(state)
    for label, value in (
        ("Z suffix", _at(3) + "Z"),
        ("+00:00", _at(3) + "+00:00"),
        ("+02:00 local time of the same instant", "2026-08-01T02:00:03+02:00"),
    ):
        _check(
            _cursors(_read(plugin, after=value)) == [2, 1, 0],
            f"after with {label} is normalized to the naive-UTC storage instant",
        )


def test_since_paging_reaches_every_row_exactly_once() -> None:
    for count in (0, 1, 5, 6, 12):
        state = RealShapeState()
        _seed(state, count=count, threads=2)
        plugin = _plugin(state)
        collected: list[int] = []
        params: dict[str, object] = {"since": _at(-1)}
        for _ in range(count + 2):
            page = _read(plugin, **params, limit=5)
            collected.extend(_cursors(page))
            if page["instance_exhausted"]:
                break
            params = {"since_cursor": page["next_since_cursor"]}
        _check(collected == list(range(count)), f"depth {count}: echoed since cursor reaches every row once, oldest-first")


def test_since_cursor_is_lossless_across_equal_timestamps() -> None:
    """The opaque continuation must split a same-timestamp group by message id."""
    state = RealShapeState()
    _seed(state, count=12, threads=2)
    for row in state.rows(NAMESPACE, TABLE_AGENT_MESSAGE):
        row["created_at"] = _at(0)
    plugin = _plugin(state)
    collected: list[int] = []
    page = _read(plugin, since=_at(-1), limit=5)
    for _ in range(4):
        collected.extend(_cursors(page))
        if page["instance_exhausted"]:
            break
        continuation = page.get("next_since_cursor")
        _check(
            isinstance(continuation, str) and bool(continuation),
            "a non-terminal forward page returns an opaque tie-safe cursor",
        )
        page = _read(plugin, since_cursor=continuation, limit=5)
        _check(
            page["role_since_created_at"] == _at(-1),
            "the continuation preserves the original role-section horizon",
        )
    _check(
        collected == list(range(12)),
        "the opaque since cursor reaches every equal-timestamp row exactly once",
    )


def test_since_page_is_the_oldest_rows_when_a_thread_overflows_it() -> None:
    """One thread deeper than the page: the first forward page is the OLDEST rows, not the newest."""
    state = RealShapeState()
    _seed(state, count=12, threads=1)
    plugin = _plugin(state)
    first = _read(plugin, since=_at(-1), limit=5)
    _check(_cursors(first) == [0, 1, 2, 3, 4], "a forward page over a deep thread starts at the oldest row after the cursor")
    _check(first["instance_exhausted"] is False, "a forward page over a deep thread is not exhausted")
    second = _read(plugin, since_cursor=first["next_since_cursor"], limit=5)
    _check(_cursors(second) == [5, 6, 7, 8, 9], "the next forward page continues without a gap")


def test_since_exhaustion_is_truthful_at_the_page_boundary() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    plugin = _plugin(state)
    full = _read(plugin, since=_at(-1), limit=5)
    _check(_cursors(full) == [0, 1, 2, 3, 4] and full["instance_exhausted"] is False, "a full page with one newer row is not exhausted")
    exact = _read(plugin, since=_at(0), limit=5)
    _check(_cursors(exact) == [1, 2, 3, 4, 5] and exact["instance_exhausted"] is True, "a page that ends on the newest row is exhausted")


def test_since_and_after_together_are_refused() -> None:
    state = RealShapeState()
    _seed(state, count=3, threads=1)
    result = _call(_plugin(state), since=_at(0), after=_at(2))
    _check(
        result["action_status"] == "failed" and result["error"]["code"] == "peer_inbox_rejected",
        "naming both cursors fails loud instead of picking a direction",
    )


def test_since_cursor_is_fail_closed_and_mutually_exclusive() -> None:
    state = RealShapeState()
    _seed(state, count=3, threads=1)
    plugin = _plugin(state)
    malformed = _call(plugin, since_cursor="not-a-cursor")
    _check(
        malformed["action_status"] == "failed" and malformed["error"]["code"] == "invalid_since_cursor",
        "a malformed opaque continuation fails loud",
    )
    first = _read(plugin, since=_at(-1), limit=1)
    both = _call(
        plugin,
        since=_at(-1),
        since_cursor=first["next_since_cursor"],
    )
    _check(
        both["action_status"] == "failed" and both["error"]["code"] == "peer_inbox_rejected",
        "the first-page horizon and continuation token are mutually exclusive",
    )


def test_repository_refuses_both_directions() -> None:
    """The service refuses first; the repository guards its own direct callers."""
    state = RealShapeState()
    _seed(state, count=3, threads=1)
    repo = AgentMessagingRepository(cast("StateManagementInterface", state))
    start = _START.replace(tzinfo=None)
    try:
        repo.list_peer_messages_for(
            recipient_agent_id=_AGENT_ID,
            recipient_agent_instance_id=_INSTANCE_ID,
            after_created_at=start,
            since_created_at=start,
            limit=5,
        )
    except ValueError:
        _check(True, "the repository refuses opposite cursors from a direct caller")
    else:
        _check(False, "the repository refuses opposite cursors from a direct caller")


def test_malformed_since_fails_loud() -> None:
    state = RealShapeState()
    _seed(state, count=3, threads=1)
    result = _call(_plugin(state), since="last tuesday")
    _check(
        result["action_status"] == "failed" and result["error"]["code"] == "invalid_since",
        "a malformed 'since' fails rather than silently re-reading the newest page",
    )


def main() -> None:
    print("=== peer_inbox since cursor smoke ===")
    test_since_returns_only_newer_rows_oldest_first()
    test_cov_boundaries()
    test_cov_ties()
    test_since_is_strict_and_not_the_backward_cursor()
    test_since_offset_forms_mean_the_same_instant()
    test_after_offset_forms_mean_the_same_instant()
    test_since_paging_reaches_every_row_exactly_once()
    test_since_cursor_is_lossless_across_equal_timestamps()
    test_since_page_is_the_oldest_rows_when_a_thread_overflows_it()
    test_since_exhaustion_is_truthful_at_the_page_boundary()
    test_since_and_after_together_are_refused()
    test_since_cursor_is_fail_closed_and_mutually_exclusive()
    test_repository_refuses_both_directions()
    test_malformed_since_fails_loud()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
