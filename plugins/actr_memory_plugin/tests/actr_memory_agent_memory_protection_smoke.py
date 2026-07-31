#!/usr/bin/env python3
"""Unit smoke: ``agent_memory`` records are protected from consolidation AND purge.

Unified-memory-passthrough Slice 1(a). Memory-passthrough projection records
carry the ``agent_memory`` umbrella tag and are recalled by similarity
(``score_by_similarity=True`` skips the retrieval boost), so they are NEVER
reinforced — the identical decay hazard the ``knowledge:official`` fix
(2026-06-21) addressed: an aged, never-boosted record sinks below the
consolidation threshold and, once past the 7-day age gate, would be archived +
vector-deleted. Slice 1(a) adds ``agent_memory`` to ``CONSOLIDATION_EXCLUDED_TAGS``
and to ``PURGE_PROTECTED_TAGS`` (used symmetrically by ``_delete_all_memory_records``
and ``_get_all_memory_ids``), so the record survives both a consolidation pass
and a purge.

RED-GREEN (consolidation): pre-fix the ``agent_memory`` record IS returned as a
candidate (revert the ``AGENT_MEMORY_TAG`` addition to ``CONSOLIDATION_EXCLUDED_TAGS``
to see the FAIL); post-fix it is excluded. The normal-weak-episodic positive case
proves the fix does NOT over-exclude, and the ``knowledge:official`` case is a
regression guard for the sibling protection.

``_filter_consolidation_candidates`` is a PURE UNIT (no DB) — called via
``object.__new__``. The purge tests use an in-memory fake state_service (no DB,
no live dispatch) exercising the REAL ``_delete_all_memory_records`` /
``_get_all_memory_ids`` methods.

Run (no env gate — deterministic, no external deps)::

    .venv/bin/python3 plugins/actr_memory_plugin/tests/actr_memory_agent_memory_protection_smoke.py
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

# Pre-load config_manager (via importlib so the import sorter can't reorder it
# after the backend) to cache the deep plugin_contracts chain before
# ``ananta.utils`` initializes — avoids the utils<->config circular import when
# the backend is imported standalone.
importlib.import_module("ananta.core.config.config_manager")
from actr_memory_plugin.backend import (  # noqa: E402
    AGENT_MEMORY_TAG,
    EPISODIC_CONSOLIDATION_THRESHOLD,
    KNOWLEDGE_OFFICIAL_TAG,
    ACTRMemoryBackend,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# A fixed "now"; every memory is created 30d ago (past the 7d cutoff) and carries
# NO retrieval_times, so compute_strength returns -10.0 (< the -1.5 threshold) —
# every fixture clears BOTH the strength and age gates, isolating the tag check.
_NOW = datetime(2026, 7, 16, 0, 0, 0, tzinfo=UTC)
_CREATED_AT = (_NOW - timedelta(days=30)).isoformat()
_CUTOFF = _NOW - timedelta(days=7)


def _memory(mid: str, *, tags: list[str], content: str) -> dict[str, Any]:
    # No 'retrieval_times' key => compute_strength => -10.0 (deeply below threshold).
    return {"id": mid, "content": content, "tags": tags, "created_at": _CREATED_AT}


def _filter(memories: list[dict[str, Any]]) -> set[str]:
    backend = object.__new__(ACTRMemoryBackend)  # method uses no instance state
    candidates = backend._filter_consolidation_candidates(
        memories, EPISODIC_CONSOLIDATION_THRESHOLD, _CUTOFF, _NOW
    )
    return {m["id"] for m in candidates}


class _FakeState:
    """In-memory stand-in for the state_service purge paths use.

    ``read_state`` / ``query_state`` return the seeded memory rows; ``delete_records``
    removes by id and records the deletion. No DB, no live dispatch.
    """

    def __init__(self, memories: list[dict[str, Any]]) -> None:
        self._memories = [dict(m) for m in memories]
        self.deleted_ids: list[str] = []

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        if query.get("table") == "memory":
            return {"data": {"records": [dict(m) for m in self._memories]}}
        return {"data": {"records": []}}  # memorization table: empty

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        del namespace, filters
        return {
            "action_status": "completed",
            "data": {"records": [dict(m) for m in self._memories]},
        }

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        mid = query.get("filters", {}).get("id")
        if mid is not None:
            self.deleted_ids.append(str(mid))
            self._memories = [m for m in self._memories if m.get("id") != mid]
        return {"action_status": "completed"}


def _purge_fixtures() -> list[dict[str, Any]]:
    return [
        _memory("m-agent", tags=[AGENT_MEMORY_TAG, "agent_memory:origin:x"], content="projection record"),
        _memory("m-kb", tags=[KNOWLEDGE_OFFICIAL_TAG], content="official KB chunk"),
        _memory("m-plain", tags=["conversation"], content="ordinary memory"),
        _memory("m-tool", tags=["tool_use"], content="a tool-use record"),
    ]


def test_agent_memory_excluded_from_consolidation() -> None:
    """An aged, weak agent_memory record is NOT a consolidation candidate (the fix);
    a plain weak one still IS (no over-exclusion); knowledge:official stays excluded."""
    memories = [
        _memory("m-agent", tags=[AGENT_MEMORY_TAG, "agent_memory:origin:x"], content="Projection of a local fact."),
        _memory("m-plain", tags=[], content="A weak episodic conversation snippet."),
        _memory("m-kb", tags=[KNOWLEDGE_OFFICIAL_TAG], content="Official KB chunk."),
    ]
    candidate_ids = _filter(memories)

    _check(
        "m-plain" in candidate_ids,
        "plain weak/aged episodic memory IS a consolidation candidate (fix is non-vacuous)",
    )
    _check(
        "m-agent" not in candidate_ids,
        "agent_memory record is EXCLUDED from consolidation candidates (THE FIX)",
    )
    _check(
        "m-kb" not in candidate_ids,
        "knowledge:official sibling protection still holds",
    )


def test_agent_memory_survives_delete_all_records() -> None:
    """`_delete_all_memory_records(exclude_protected=True)` deletes ordinary records
    but preserves agent_memory AND knowledge:official."""
    fake = _FakeState(_purge_fixtures())
    backend = object.__new__(ACTRMemoryBackend)
    backend.state_service = fake

    deleted_count = backend._delete_all_memory_records(exclude_protected=True)

    _check("m-agent" not in fake.deleted_ids, "agent_memory record survives purge delete (THE FIX)")
    _check("m-kb" not in fake.deleted_ids, "knowledge:official record survives purge delete")
    _check(
        set(fake.deleted_ids) == {"m-plain", "m-tool"},
        f"only non-protected records deleted (got {sorted(fake.deleted_ids)})",
    )
    _check(deleted_count == 2, f"deleted_count is 2 (got {deleted_count})")


def test_agent_memory_survives_get_all_memory_ids() -> None:
    """`_get_all_memory_ids(exclude_protected=True)` (the vector-side of purge) omits
    agent_memory AND knowledge:official, symmetric with the record-side delete."""
    fake = _FakeState(_purge_fixtures())
    backend = object.__new__(ACTRMemoryBackend)
    backend.state_service = fake

    ids = backend._get_all_memory_ids(exclude_protected=True)

    _check("m-agent" not in ids, "agent_memory id NOT returned for vector deletion (THE FIX)")
    _check("m-kb" not in ids, "knowledge:official id NOT returned for vector deletion")
    _check(
        set(ids) == {"m-plain", "m-tool"},
        f"only non-protected ids returned (got {sorted(ids)})",
    )


def main() -> int:
    print("=== actr_memory_agent_memory_protection_smoke ===")
    test_agent_memory_excluded_from_consolidation()
    test_agent_memory_survives_delete_all_records()
    test_agent_memory_survives_get_all_memory_ids()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
