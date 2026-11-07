#!/usr/bin/env python3
"""Smoke: the session-ledger event-embedding drain always makes progress (iss_6aa96266, unt_99b1886d).

Live defect (measured 2026-10-01, in the log since 2026-09-28 09:34 PDT): every
``drain_event_embeddings`` fire halted on the same event
(``evt_616944f0f8744cff9935592e7c41bcbd:0``) with ``UniqueViolation
session_ledger_event__embeddings_external_id_key`` and never advanced the cursor,
so session-ledger search went stale.  The mechanism:

* the first fire after the chunking-policy release was a backfill sweep that
  re-embeds an event whose old-policy chunk differs from the token-budget cut;
* ``embed_event`` deletes the old chunk id and stores the new one under the SAME id;
* ``delete_by_external_ids`` SOFT-deleted (``is_deleted = 1``) while the embeddings
  table carries a standalone ``UNIQUE(external_id)``, so the tombstone kept the id
  and every store of that id failed; ``find_missing_external_ids`` counted the
  tombstone as missing, so every later fire retried the same store and halted.

This smoke drives the REAL drain (``EventEmbeddingWriter.drain_missing_events``
over the real ``SessionLedgerRepository`` cursor) against the REAL
``PGVectorProvider`` (``store_vectors`` / ``delete_by_external_ids`` /
``find_missing_external_ids``) over an in-memory state service that enforces
``UNIQUE(external_id)`` and the postgres state plugin's soft / hard delete
semantics, plus a fake embedder that refuses any input over a 2048-token budget
like ``coreai_embeddings`` does.  It proves:

1. a 13610-token event is split to the provider budget, embedded and the cursor
   advances past it (the over-budget regression);
2. a backfill re-embed of an already-embedded event lands (the live defect);
3. a tombstone a soft delete left behind is cleared and the event re-embeds on
   the next fire (the live state after deploy);
4. one event the embedder refuses is skipped with a WARNING naming its event_id,
   counted, the cursor advances, and a later reconciliation sweep embeds it once
   the refusal clears;
5. an embedder outage still halts the page WITHOUT advancing the cursor;
6. failed policy sweeps checkpoint pages and retry only on scheduled reconciliation.

Each mutant below is run through the same scenarios and must be killed by a named
failing check: budget not passed, split bypassed, soft delete restored, no skip
(cursor not advanced on a failed event), no outage probe (cursor advanced on an
outage).

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/event_embed_drain_progress_smoke.py
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
_ROOT = REPO_ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "pgvector_service_plugin" / "src"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.interfaces.embedding_service_interface import TokenBudget  # noqa: E402
from ananta.llm.session_ledger import event_embeddings as event_embeddings_module  # noqa: E402
from ananta.llm.session_ledger.event_embedding_checkpoint import (  # noqa: E402
    SweepCheckpoint,
    finish_sweep,
)
from ananta.llm.session_ledger.event_embeddings import (  # noqa: E402
    _RECONCILE_EVERY_FIRES,
    EventEmbeddingWriter,
    chunk_policy,
)
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from pgvector_service_plugin.postgres_backend.vector.provider import PGVectorProvider  # noqa: E402

_BUDGET_TOKENS = 2048
_BIG_EVENT_TOKENS = 13610
_PROBE_LOGGER = "ananta.llm.session_ledger.event_embeddings"
_failed: list[str] = []
_passed = 0
_verbose = True


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        if _verbose:
            print(f"  PASS  {label}")
    else:
        _failed.append(label)
        if _verbose:
            print(f"  FAIL  {label}")


def _ok(payload: dict[str, Any]) -> ActionResult:
    return ActionResult(
        action_status="completed",
        data={"result": payload},
        actions=[],
        error=None,
        timestamp=datetime.now(UTC).isoformat(),
    )


def _err(code: str, message: str) -> ActionResult:
    return ActionResult(
        action_status="error",
        actions=[],
        error={
            "code": code,
            "message": message,
            "type": "SmokeError",
            "details": {},
            "severity": "error",
            "timestamp": datetime.now(UTC).isoformat(),
        },
        timestamp=datetime.now(UTC).isoformat(),
    )


def _tokens(text: str) -> int:
    """One token per whitespace-separated word: a stand-in for the provider's counter."""
    return len(text.split())


class _Embedder:
    """Refuses any input over the budget (like coreai_embeddings), plus switchable poison / outage."""

    def __init__(self) -> None:
        self.sent_token_counts: list[int] = []
        self.refused_over_budget = 0
        self.poison: str | None = None
        self.down = False
        self.budget_tokens = _BUDGET_TOKENS

    def input_token_budget(self) -> TokenBudget:
        return TokenBudget(self.budget_tokens, _tokens)

    def generate_embeddings(self, inputs: list[str], model: str | None = None, input_type: str = "text") -> ActionResult:
        del model, input_type
        if self.down:
            return _err("embedder.unavailable", "embedder is down")
        counts = [_tokens(text) for text in inputs]
        if any(count > _BUDGET_TOKENS for count in counts):
            self.refused_over_budget += 1
            return _err("coreai_embeddings.input_too_long", f"inputs {counts} exceed {_BUDGET_TOKENS} tokens")
        if self.poison is not None and any(self.poison in text for text in inputs):
            return _err("embedder.refused", "the embedder refused this content")
        self.sent_token_counts.extend(counts)
        return _ok({"embeddings": [[float(len(text)), 1.0, 0.0] for text in inputs], "dimension": 3, "model": "fake"})


class _UniqueVectorTable:
    """The postgres state plugin's embeddings table: UNIQUE(external_id) over active AND tombstoned rows."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._next_id = 0

    def seed(self, external_id: str, *, is_deleted: int = 0) -> None:
        self._next_id += 1
        self.rows.append({"id": f"vec_{self._next_id}", "external_id": external_id, "is_deleted": is_deleted, "dimension": 3})

    def active_ids(self) -> set[str]:
        return {str(row["external_id"]) for row in self.rows if row["is_deleted"] == 0}

    def tombstones(self) -> set[str]:
        return {str(row["external_id"]) for row in self.rows if row["is_deleted"] == 1}

    def _matching(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        matched = []
        for row in self.rows:
            if all(row.get(col) in val if isinstance(val, list) else row.get(col) == val for col, val in filters.items()):
                matched.append(row)
        return matched

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        rows = self._matching(query.get("filters", {}))[: query.get("limit", 10**9)]
        return {"action_status": "completed", "data": {"records": [dict(row) for row in rows]}}

    def execute_sql(self, sql_query: str, sql_params: list[str] | None = None) -> dict[str, Any]:
        """Only the pre-fix ``delete_by_external_ids`` lookup: ids of the ACTIVE rows holding the given external_ids."""
        assert "SELECT id" in sql_query and "external_id IN" in sql_query, sql_query
        rows = [[row["id"]] for row in self.rows if row["external_id"] in (sql_params or []) and row["is_deleted"] == 0]
        return {"action_status": "completed", "data": {"records": rows}}

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        del namespace
        ids = []
        for record in data["records"]:
            if any(row["external_id"] == record.get("external_id") for row in self.rows):
                return {
                    "action_status": "error",
                    "error": {
                        "code": "state.write_failed",
                        "message": f'duplicate key value violates unique constraint "session_ledger_event__embeddings_external_id_key" Key (external_id)=({record.get("external_id")}) already exists.',
                    },
                }
            self.seed(str(record["external_id"]))
            ids.append(self.rows[-1]["id"])
        return {"action_status": "completed", "data": {"result": {"generated_ids": ids}}}

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        matched = self._matching(query["filters"])
        if query.get("soft_delete", True):
            for row in matched:
                row["is_deleted"] = 1
        else:
            self.rows = [row for row in self.rows if row not in matched]
        return {"action_status": "completed", "data": {"result": {"deleted": len(matched), "soft_delete": query.get("soft_delete", True)}}}


class _VectorService:
    """The vector service seam over the REAL PGVectorProvider (no pool: only the state-service methods run)."""

    def __init__(self, table: _UniqueVectorTable) -> None:
        self.table = table
        self.fail_phase: str | None = None
        self.fail_on: str | None = None
        self.duplicate_external_id: str | None = None
        self.store_attempts = 0
        self.delete_attempts = 0
        self._provider = PGVectorProvider.__new__(PGVectorProvider)
        self._provider._state_service = table  # pyright: ignore[reportPrivateUsage]
        self._provider.config = SimpleNamespace(schema_name="public")  # type: ignore[assignment]

    def _call(self, call: Callable[[], dict[str, Any]]) -> ActionResult:
        try:
            return _ok(call())
        except Exception as exc:  # the plugin wraps every provider failure into an error envelope
            return _err("pgvector.operation_failed", str(exc))

    def store_vectors(self, namespace: str, vectors: list[dict[str, object]]) -> ActionResult:
        self.store_attempts += 1
        if self.fail_phase == "store" and (self.fail_on is None or any(self.fail_on in str(v["external_id"]) for v in vectors)):
            return _err("store.unavailable", "store is down")
        if self.duplicate_external_id is not None and any(str(v["external_id"]) == self.duplicate_external_id for v in vectors):
            return _err(
                "state.write_failed",
                f"duplicate key value violates unique constraint external_id ({self.duplicate_external_id})",
            )
        return self._call(lambda: self._provider.store_vectors(namespace, [dict(v) for v in vectors]))

    def delete_by_external_ids(self, namespace: str, external_ids: list[str]) -> ActionResult:
        self.delete_attempts += 1
        if self.fail_phase == "delete":
            return _err("delete.unavailable", "delete is down")
        return self._call(lambda: self._provider.delete_by_external_ids(namespace=namespace, external_ids=external_ids))

    def find_missing_external_ids(self, namespace: str, candidate_external_ids: list[str]) -> ActionResult:
        if self.fail_phase == "lookup":
            return _err("lookup.unavailable", "lookup is down")
        return self._call(lambda: self._provider.find_missing_external_ids(namespace=namespace, candidate_external_ids=candidate_external_ids))


class _DrainRepo(SessionLedgerRepository):
    """Real repository (real KV cursor / policy / counter) with only the candidate read serving a fixed corpus."""

    __slots__ = ("corpus", "reads")

    def __init__(self, state_service: Any, corpus: list[dict[str, object]]) -> None:
        super().__init__(state_service)
        self.corpus = list(corpus)
        self.reads: list[tuple[object, object] | None] = []

    def list_event_embedding_candidates(
        self,
        *,
        limit: int,
        after: tuple[object, object] | None = None,
        order_column: str = "event_at",
        ascending: bool = False,
    ) -> list[dict[str, object]]:
        if ascending:  # walk reads only; the drain's newest-arrival idle read is not a walk (prb_f6964132)
            self.reads.append(after)
        keyed = sorted(self.corpus, key=lambda row: (str(row[order_column]), str(row["id"])), reverse=not ascending)
        if after is not None:
            key = (str(after[0]), str(after[1]))
            keyed = [r for r in keyed if ((str(r[order_column]), str(r["id"])) > key) == ascending and (str(r[order_column]), str(r["id"])) != key]
        return [dict(row) for row in keyed[:limit]]


def _event(row_id: str, hour: int, text: str) -> dict[str, object]:
    stamp = f"2026-10-01T{hour:02d}:00:00"
    return {
        "id": row_id,
        "session_id": "les_1",
        "sequence": hour,
        "event_type": "MESSAGE",
        "role": "assistant",
        "content_text": text,
        "content_json": None,
        "event_at": stamp,
        "imported_at": stamp,
    }


def _big_text(tokens: int) -> str:
    return " ".join(["ab"] * tokens)


class _FaultState(StubStateService):
    """Checked persistence envelopes with switchable read/write/retire outages."""

    def __init__(self) -> None:
        super().__init__()
        self.fail: tuple[str, str] | None = None
        self.write_keys: list[str] = []

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> dict[str, Any]:
        if self.fail == ("read", key):
            return dict(_err("kv.read", "unavailable"))
        return super().get_key_value(namespace, key, scope)

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: object,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> dict[str, Any]:
        self.write_keys.append(key)
        if self.fail == ("write", key):
            return dict(_err("kv.write", "unavailable"))
        if isinstance(value, dict | list):
            value = json.dumps(value)
        return super().set_key_value(namespace, key, value, scope, ttl)

    def delete_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> dict[str, Any]:
        self.write_keys.append("retire:" + key)
        if self.fail == ("retire", key):
            return dict(_err("kv.retire", "unavailable"))
        return super().delete_key_value(namespace, key, scope)


class _World:
    def __init__(self, corpus: list[dict[str, object]], *, policy_recorded: bool) -> None:
        self.state = _FaultState()
        self.repo = _DrainRepo(self.state, corpus)
        self.embedder = _Embedder()
        self.table = _UniqueVectorTable()
        self.vectors = _VectorService(self.table)
        self.writer = EventEmbeddingWriter(
            repository=self.repo,
            embedding_service=self.embedder,  # type: ignore[arg-type]
            vector_service=self.vectors,  # type: ignore[arg-type]
        )
        if policy_recorded:
            self.repo.set_event_embed_chunk_policy(chunk_policy(self.embedder.input_token_budget()))


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


_SINK = _Warnings()


def scenario_store_outage_defers_page() -> None:
    """A healthy generation service cannot establish vector-store availability.

    The first assertion is the registered base-red contract. The attempt
    counter proves the error envelope came from the storage path.
    """
    world = _World([_event("evt_store", 1, "healthy generation")], policy_recorded=True)
    world.vectors.fail_phase = "store"
    outcome = world.writer.drain_missing_events(page_size=1)
    _check(outcome["halted_on_error"] is True, "[F1] store failure defers page")
    _check(world.repo.get_event_embed_cursor() is None, "[F1] store failure retains cursor")
    _check(world.vectors.store_attempts == 1, "[F1] failed storage path reached")


def scenario_big_event_is_split_and_the_cursor_advances() -> None:
    corpus = [_event("evt_a", 1, "small one"), _event("evt_big", 2, _big_text(_BIG_EVENT_TOKENS)), _event("evt_c", 3, "small two")]
    world = _World(corpus, policy_recorded=True)
    outcome = world.writer.drain_missing_events(page_size=100)
    big_chunks = {eid for eid in world.table.active_ids() if eid.startswith("evt_big:")}
    _check(outcome["halted_on_error"] is False and outcome["events_embedded"] == 3, "[1] all three events embed, no halt")
    _check(world.embedder.refused_over_budget == 0 and max(world.embedder.sent_token_counts) <= _BUDGET_TOKENS, "[1] every input sent fits the 2048-token budget")
    _check(len(big_chunks) >= _BIG_EVENT_TOKENS // _BUDGET_TOKENS + 1, f"[1] the {_BIG_EVENT_TOKENS}-token event was split into >= 7 stored chunks")
    _check(world.repo.get_event_embed_cursor() == "2026-10-01T03:00:00", "[1] the cursor advanced past the 13610-token event")


def scenario_backfill_reembed_lands() -> None:
    corpus = [_event("evt_a", 1, "small one"), _event("evt_stuck", 2, _big_text(5000))]
    world = _World(corpus, policy_recorded=False)
    world.table.seed("evt_a:0")
    world.table.seed("evt_stuck:0")  # the old-policy single 8192-char chunk, active
    outcome = world.writer.drain_missing_events(page_size=100)
    stuck_chunks = {eid for eid in world.table.active_ids() if eid.startswith("evt_stuck:")}
    _check(outcome["halted_on_error"] is False and outcome.get("events_failed") == 0, "[2] a backfill re-embed does not halt or fail")
    _check(len(stuck_chunks) >= 3 and "evt_stuck:0" not in world.table.tombstones(), "[2] the stale chunk was replaced by the budget-cut chunks, no tombstone left")
    _check(world.repo.get_event_embed_cursor() == "2026-10-01T02:00:00", "[2] the cursor advanced past the re-embedded event")
    _check(world.repo.get_event_embed_chunk_policy() == chunk_policy(world.embedder.input_token_budget()), "[2] the completed backfill recorded its policy")


def scenario_tombstone_is_cleared_on_the_next_fire() -> None:
    corpus = [_event("evt_t", 1, "recoverable content")]
    world = _World(corpus, policy_recorded=True)
    world.table.seed("evt_t:0", is_deleted=1)  # what the soft delete left in the live table
    outcome = world.writer.drain_missing_events(page_size=100)
    _check(outcome["halted_on_error"] is False and outcome["events_embedded"] == 1, "[3] the tombstoned event re-embeds on the next fire")
    _check("evt_t:0" in world.table.active_ids() and not world.table.tombstones(), "[3] the id is active again and the tombstone is gone")


def scenario_duplicate_external_id_is_skipped_loudly() -> None:
    corpus = [_event("evt_duplicate", 1, "duplicate content")]
    world = _World(corpus, policy_recorded=False)
    world.vectors.duplicate_external_id = "evt_duplicate:0"
    seen = len(_SINK.messages)
    outcome = world.writer.drain_missing_events(page_size=100)
    _check(
        outcome["halted_on_error"] is False and outcome["events_skipped_duplicate"] == 1 and outcome["events_failed"] == 0 and world.repo.get_event_embed_cursor() == "2026-10-01T01:00:00",
        "[D3] a duplicate external_id is skipped/counts and the drain advances",
    )
    _check(
        any("events_skipped_duplicate=1" in message for message in _SINK.messages[seen:]),
        "[D3] duplicate external_id emits a counted advisory",
    )


def scenario_one_failed_event_is_skipped_loudly() -> None:
    corpus = [_event("evt_a", 1, "fine one"), _event("evt_poison", 2, "POISON content"), _event("evt_c", 3, "fine two")]
    world = _World(corpus, policy_recorded=True)
    world.embedder.poison = "POISON"
    seen = len(_SINK.messages)
    outcome = world.writer.drain_missing_events(page_size=100)
    _check(outcome["halted_on_error"] is False and outcome.get("events_failed") == 1 and outcome["events_embedded"] == 2, "[4] one failed event: counted, the other two embed")
    _check(world.repo.get_event_embed_cursor() == "2026-10-01T03:00:00", "[4] the cursor advanced past the failed event")
    _check(any("evt_poison" in message for message in _SINK.messages[seen:]), "[4] a WARNING names the failed event_id")
    world.embedder.poison = None
    for _ in range(_RECONCILE_EVERY_FIRES - 2):
        world.repo.bump_event_embed_drain_counter()
    retry = world.writer.drain_missing_events(page_size=100)
    _check(retry["reconcile"] is True and "evt_poison:0" in world.table.active_ids(), "[4] the next reconciliation sweep embeds the skipped event once the refusal clears")


def scenario_a_skipped_event_keeps_the_backfill_pending() -> None:
    corpus = [_event("evt_a", 1, "fine one"), _event("evt_poison", 2, "POISON content")]
    world = _World(corpus, policy_recorded=False)
    world.embedder.poison = "POISON"
    outcome = world.writer.drain_missing_events(page_size=100)
    _check(outcome["policy_backfill"] is True and outcome.get("events_failed") == 1, "[6] a backfill sweep that skipped an event reports it")
    _check(world.repo.get_event_embed_chunk_policy() is None, "[6] a sweep that skipped an event retains its pending policy")


def scenario_outage_halts_without_advancing() -> None:
    corpus = [_event("evt_a", 1, "one"), _event("evt_b", 2, "two")]
    world = _World(corpus, policy_recorded=True)
    world.embedder.down = True
    outcome = world.writer.drain_missing_events(page_size=100)
    _check(outcome["halted_on_error"] is True and outcome["events_embedded"] == 0, "[5] an embedder outage halts the drain")
    _check(world.repo.get_event_embed_cursor() is None, "[5] the cursor is NOT advanced past an outage")


def _restart(world: _World) -> None:
    world.repo = _DrainRepo(world.state, world.repo.corpus)
    world.writer = EventEmbeddingWriter(
        repository=world.repo,
        embedding_service=world.embedder,  # type: ignore[arg-type]
        vector_service=world.vectors,  # type: ignore[arg-type]
    )


def _schedule(world: _World) -> None:
    raw = world.state.key_values.get(("session_ledger", "event_embed_drain_fires", "GLOBAL"), "0")
    for _ in range((_RECONCILE_EVERY_FIRES - 1 - int(str(raw))) % _RECONCILE_EVERY_FIRES):
        world.repo.bump_event_embed_drain_counter()


def scenario_delete_failure_propagates() -> None:
    """Pin both direct failure propagation and scheduled page custody.

    No store may follow an unsuccessful hard delete: the old deterministic
    chunk ids might still exist. A healthy embedder does not change that.
    """
    row = _event("evt_delete", 1, "healthy embedder")
    world = _World([row], policy_recorded=True)
    world.vectors.fail_phase = "delete"
    raised = False
    try:
        world.writer.embed_event(dict(row))
    except RuntimeError:
        raised = True
    _check(raised, "[D1] direct failed hard delete raises")
    _check(world.vectors.delete_attempts == 1 and world.vectors.store_attempts == 0, "[D1] delete reached and no store attempted")
    seen = len(_SINK.messages)
    outcome = world.writer.drain_missing_events(page_size=1)
    _check(outcome["halted_on_error"] and world.repo.get_event_embed_cursor() is None, "[D2] delete outage defers page with healthy embedder")
    _check(any("phase=delete" in m and "next heartbeat" in m for m in _SINK.messages[seen:]), "[D2] deferred warning names delete and retry")
    _check(not any("skipping" in m for m in _SINK.messages[seen:]), "[D2] delete outage never logs skipping")


def scenario_probe_and_unknown_defer() -> None:
    """A failed generation probe has not classified an event as poison.

    Lookup errors have no event-local cause either. Manual subset callers
    must observe these failures directly, even without a durable cursor.
    """
    world = _World([_event("evt_outage", 1, "one")], policy_recorded=True)
    world.embedder.down = True
    seen = len(_SINK.messages)
    outcome = world.writer.drain_missing_events(page_size=1)
    _check(outcome["halted_on_error"], "[L1] failed generation probe defers")
    _check(not any("skipping" in m for m in _SINK.messages[seen:]), "[L1] no skip log before failed probe")
    world.embedder.down = False
    world.vectors.fail_phase = "lookup"
    outcome = world.writer.drain_missing_events(page_size=1)
    _check(outcome["halted_on_error"] and world.repo.get_event_embed_cursor() is None, "[L2] unknown lookup failure retains page")
    for phase in ("store", "delete"):
        world.vectors.fail_phase = phase
        raised = False
        try:
            world.writer.embed_missing_events(batch_limit=1)
        except RuntimeError:
            raised = True
        _check(raised, f"[L3] manual subset propagates {phase} outage")


def scenario_poison_bounded_retry() -> None:
    """Use an already-stored poison vector to distinguish policy provenance.

    A missing-only retry would skip it. Recreated repositories share KV but
    no transient sweep state; ordinary fires must stay incremental.
    """
    corpus = [_event("evt_a", 1, "fine"), _event("evt_poison", 2, "POISON"), _event("evt_z", 3, "later")]
    world = _World(corpus, policy_recorded=False)
    previous = "tokens:4096+chars:8192"
    world.repo.set_event_embed_chunk_policy(previous)
    world.table.seed("evt_poison:0")
    world.embedder.poison = "POISON"
    first = world.writer.drain_missing_events(page_size=1)
    pending = world.repo.get_event_embed_checkpoint()
    _check(first["events_failed"] == 1 and not first["halted_on_error"] and "evt_z:0" in world.table.active_ids(), "[P1] sweep reaches healthy events after poison")
    _check(pending is not None and pending.status == "exhausted-with-failures" and pending.previous == previous, "[P1] failed policy exhausts with original previous policy")
    _check(world.repo.get_event_embed_chunk_policy() == previous, "[P1] failed sweep never marks policy complete")
    for _ in range(3):
        _restart(world)
        ordinary = world.writer.drain_missing_events(page_size=1)
        _check(not ordinary["reconcile"] and ordinary["candidates_scanned"] <= 1, "[P2] recreated writer ordinary fire performs no full sweep")
        _check(ordinary["pending_checkpoint"] is not None, "[P2] pending work remains visible")
    _schedule(world)
    retried = world.writer.drain_missing_events(page_size=1)
    _check(retried["events_failed"] == 1, "[P3] scheduled retry retains old policy for existing poison vector")
    world.embedder.poison = None
    _schedule(world)
    recovered = world.writer.drain_missing_events(page_size=1)
    _check(recovered["reconcile"] and not recovered["halted_on_error"] and recovered["events_embedded"] == 3, "[P4] refusal clearing retries stale vectors on eligible sweep")
    _check(world.repo.get_event_embed_checkpoint() is None and world.repo.get_event_embed_chunk_policy() == chunk_policy(world.embedder.input_token_budget()), "[P4] clean sweep records policy and retires checkpoint")


def scenario_sweep_outage_restart() -> None:
    """Interrupt the second page after one safe completed-page checkpoint.

    The normal cursor deliberately starts ahead of the sweep, proving old
    sweep progress cannot move the arrival frontier backwards on restart.
    """
    corpus = [_event("evt_a", 1, "first"), _event("evt_b", 2, "failed store"), _event("evt_z", 3, "last")]
    world = _World(corpus, policy_recorded=False)
    world.repo.set_event_embed_cursor(str(corpus[-1]["imported_at"]))
    world.vectors.fail_phase, world.vectors.fail_on = "store", "evt_b"
    outcome = world.writer.drain_missing_events(page_size=1)
    checkpoint = world.repo.get_event_embed_checkpoint()
    _check(outcome["halted_on_error"] and checkpoint is not None and checkpoint.after == (str(corpus[0]["imported_at"]), "evt_a"), "[R1] failed page never advances sweep checkpoint")
    _check(world.repo.get_event_embed_cursor() == str(corpus[-1]["imported_at"]), "[R1] old sweep never regresses normal cursor")
    world.vectors.fail_phase = None
    _restart(world)
    # Simulate a reconciliation fire while an interrupted sweep is running.
    _schedule(world)
    resumed = world.writer.drain_missing_events(page_size=1)
    cursor = str(corpus[-1]["imported_at"])
    sweep_reads = [read for read in world.repo.reads if read is None or read[0] != cursor]  # new arrivals are read first (prb_f6964132)
    _check(sweep_reads[0] == (str(corpus[0]["imported_at"]), "evt_a"), "[R2] restart resumes failed page even on scheduled reconciliation")
    _check(not resumed["halted_on_error"] and {"evt_b:0", "evt_z:0"} <= world.table.active_ids(), "[R2] retry recovers failed and later searchable vectors")


def scenario_checkpoint_lifecycle() -> None:
    """Exercise the derived record without an embedding-provider dependency.

    Correction requires the expected generation. A superseded writer may
    neither overwrite current progress nor record its old target policy.
    """
    world = _World([], policy_recorded=False)
    original = SweepCheckpoint("generation-a", "policy-a", None)
    world.repo.set_event_embed_checkpoint(original, expected=None)
    _check(world.repo.get_event_embed_checkpoint() == original, "[C1] checkpoint create/read")
    corrected = replace(original, after=("stamp", "evt_a"), failed_count=1)
    world.repo.set_event_embed_checkpoint(corrected, expected=original)
    _check(world.repo.get_event_embed_checkpoint() == corrected, "[C1] checkpoint correct/read")
    newer = SweepCheckpoint("generation-b", "policy-b", "policy-a")
    world.repo.set_event_embed_checkpoint(newer, expected=corrected)
    for operation in (lambda: world.repo.set_event_embed_checkpoint(original, expected=corrected), lambda: finish_sweep(world.repo, corrected)):
        raised = False
        try:
            operation()
        except RuntimeError:
            raised = True
        _check(raised and world.repo.get_event_embed_checkpoint() == newer, "[C2] stale generation cannot overwrite newer progress or policy")
    _check(world.repo.get_event_embed_chunk_policy() is None, "[C2] stale completion cannot record policy")
    world.repo.set_event_embed_checkpoint(None, expected=world.repo.get_event_embed_checkpoint())
    _check(world.repo.get_event_embed_checkpoint() is None, "[C3] checkpoint clear/read")


def scenario_persistence_failures() -> None:
    """Storage error envelopes must remain distinguishable from missing state.

    Creation/read failures leave no frontier. Completion failures retain a
    replayable checkpoint, with policy persistence ordered before retirement.
    """
    for operation, key in (("read", "event_embed_sweep_checkpoint"), ("write", "event_embed_sweep_checkpoint"), ("read", "event_embed_chunk_policy"), ("write", "event_embed_drain_fires")):
        world = _World([_event("evt_a", 1, "one")], policy_recorded=False)
        world.state.fail = (operation, key)
        outcome = world.writer.drain_missing_events(page_size=1)
        _check(outcome["halted_on_error"] and world.repo.get_event_embed_cursor() is None, f"[K1] failed KV {operation} {key} cannot advance progress")
    for operation, key in (("write", "event_embed_chunk_policy"), ("retire", "event_embed_sweep_checkpoint")):
        world = _World([_event("evt_a", 1, "one")], policy_recorded=False)
        world.state.fail = (operation, key)
        outcome = world.writer.drain_missing_events(page_size=1)
        _check(outcome["halted_on_error"] and world.repo.get_event_embed_checkpoint() is not None, f"[K2] failed {operation} completion retains checkpoint")
        world.state.fail = None
        _restart(world)
        outcome = world.writer.drain_missing_events(page_size=1)
        _check(not outcome["halted_on_error"] and world.repo.get_event_embed_checkpoint() is None, "[K2] completion replay recovers")
        _check(world.state.write_keys.index("event_embed_chunk_policy") < world.state.write_keys.index("retire:event_embed_sweep_checkpoint"), "[K2] policy writes before retirement")


def scenario_checkpoint_write_after_page() -> None:
    """Model both sides of the checkpoint/frontier crash boundary.

    A failed checkpoint write cannot advance normal progress. A successful
    checkpoint followed by a failed frontier write repairs on reconstruction.
    """
    world = _World([_event("evt_a", 1, "one")], policy_recorded=False)
    checkpoint = SweepCheckpoint("generation", chunk_policy(world.embedder.input_token_budget()), None)
    world.repo.set_event_embed_checkpoint(checkpoint, expected=None)
    world.state.fail = ("write", "event_embed_sweep_checkpoint")
    outcome = world.writer.drain_missing_events(page_size=1)
    _check(outcome["halted_on_error"] and world.repo.get_event_embed_checkpoint() == checkpoint, "[K3] failed page checkpoint write retains durable prior progress")
    _check(world.repo.get_event_embed_cursor() is None and world.vectors.store_attempts == 1, "[K3] checkpoint safety precedes normal frontier advancement")
    world.state.fail = None
    _restart(world)
    outcome = world.writer.drain_missing_events(page_size=1)
    _check(not outcome["halted_on_error"], "[K3] completed-vector replay safely recovers checkpoint")
    world = _World([_event("evt_a", 1, "POISON")], policy_recorded=False)
    world.embedder.poison = "POISON"
    world.state.fail = ("write", "event_embed_cursor")
    outcome = world.writer.drain_missing_events(page_size=1)
    _check(outcome["halted_on_error"] and world.repo.get_event_embed_cursor() is None, "[K4] failed normal frontier write defers after safe checkpoint")
    world.state.fail = None
    _restart(world)
    outcome = world.writer.drain_missing_events(page_size=1)
    _check(not outcome["halted_on_error"] and world.repo.get_event_embed_cursor() == "2026-10-01T01:00:00", "[K4] replay repairs normal frontier from completed sweep checkpoint")


def scenario_policy_reversal_and_corruption() -> None:
    """A completed A policy can still contain partially replaced B vectors.

    Reverting to A therefore supersedes B with a fresh sweep. Corrupt
    disposable progress similarly restarts safely without blocking recovery.
    """
    world = _World([_event("evt_a", 1, "POISON")], policy_recorded=True)
    policy_a = chunk_policy(world.embedder.input_token_budget())
    world.embedder.budget_tokens = 1024
    world.embedder.poison = "POISON"
    world.writer.drain_missing_events(page_size=1)
    old = world.repo.get_event_embed_checkpoint()
    world.embedder.budget_tokens = _BUDGET_TOKENS
    world.embedder.poison = None
    reversed_policy = world.writer.drain_missing_events(page_size=1)
    _check(old is not None and reversed_policy["reconcile"] and reversed_policy["events_embedded"] == 1, "[V1] A to B to A supersedes foreign checkpoint with fresh full sweep")
    _check(world.repo.get_event_embed_checkpoint() is None and world.repo.get_event_embed_chunk_policy() == policy_a, "[V1] reversal cleanly completes")
    world.state.key_values[("session_ledger", "event_embed_sweep_checkpoint", "GLOBAL")] = {"broken": True}
    recovered = world.writer.drain_missing_events(page_size=1)
    _check(recovered["reconcile"] and not recovered["halted_on_error"], "[V2] corrupt disposable progress restarts safe sweep")


def scenario_page_progress_logs() -> None:
    """Bound routine records to two per page while exposing durable work.

    Transition history and progress carry ids, policies and counts. Event
    content is unnecessary for operational readback and must stay out.
    """
    world = _World([_event("evt_a", 1, "secret content"), _event("evt_b", 2, "second secret")], policy_recorded=False)
    seen = len(_SINK.messages)
    world.writer.drain_missing_events(page_size=1)
    lines = [m for m in _SINK.messages[seen:] if "event embedding page-" in m]
    _check(len(lines) == 4 and sum("page-start" in m for m in lines) == 2, "[I1] exactly one start and complete progress line per page")
    _check(all(all(field in m for field in ("cursor=", "embedded=", "failed=", "deferred=", "sweep=", "pending=")) for m in lines), "[I1] progress lines include cursor totals and pending status")
    _check(not any("secret" in m for m in _SINK.messages[seen:]), "[I1] progress/history logs contain no event content")


_SCENARIOS = (
    scenario_store_outage_defers_page,
    scenario_delete_failure_propagates,
    scenario_probe_and_unknown_defer,
    scenario_poison_bounded_retry,
    scenario_sweep_outage_restart,
    scenario_checkpoint_lifecycle,
    scenario_persistence_failures,
    scenario_checkpoint_write_after_page,
    scenario_policy_reversal_and_corruption,
    scenario_page_progress_logs,
    scenario_big_event_is_split_and_the_cursor_advances,
    scenario_backfill_reembed_lands,
    scenario_tombstone_is_cleared_on_the_next_fire,
    scenario_duplicate_external_id_is_skipped_loudly,
    scenario_one_failed_event_is_skipped_loudly,
    scenario_a_skipped_event_keeps_the_backfill_pending,
    scenario_outage_halts_without_advancing,
)


def _run_scenarios() -> list[str]:
    """Run every scenario; return the failing check labels."""
    global _passed
    _failed.clear()
    passed_before = _passed
    loggers = [logging.getLogger(name) for name in (_PROBE_LOGGER, "ananta.llm.session_ledger.search", "ananta.llm.session_ledger.event_embedding_checkpoint")]
    for logger in loggers:
        logger.setLevel(logging.INFO)
        logger.addHandler(_SINK)
        logger.propagate = False  # the outage scenario's halt ERROR and the skip WARNING are expected, captured output
    try:
        for scenario in _SCENARIOS:
            scenario()
    finally:
        for logger in loggers:
            logger.removeHandler(_SINK)
            logger.propagate = True
    failed = list(_failed)
    _passed = passed_before
    return failed


# ─── Mutants: each must be killed by a named failing check ───────────────────


def _mutant_budget_not_passed(restore: list[Callable[[], None]]) -> None:
    original = EventEmbeddingWriter._input_budget
    EventEmbeddingWriter._input_budget = lambda self: None  # type: ignore[method-assign, assignment]
    restore.append(lambda: setattr(EventEmbeddingWriter, "_input_budget", original))


def _mutant_split_bypassed(restore: list[Callable[[], None]]) -> None:
    original = event_embeddings_module.chunk_event_content
    event_embeddings_module.chunk_event_content = lambda text, budget=None: [text]  # type: ignore[assignment]
    restore.append(lambda: setattr(event_embeddings_module, "chunk_event_content", original))


def _mutant_soft_delete_restored(restore: list[Callable[[], None]]) -> None:
    original = PGVectorProvider.delete_by_external_ids

    def soft(self: PGVectorProvider, namespace: str, external_ids: list[str]) -> dict[str, Any]:
        deleted = 0
        for external_id in external_ids:
            active = self._state_service.read_state(
                namespace=namespace,
                query={"table": "embeddings", "filters": {"external_id": [external_id], "is_deleted": 0}},
            )
            active_data = cast(dict[str, Any], active.get("data", {}))
            for row in active_data["records"]:
                result = self._state_service.delete_records(
                    namespace=namespace,
                    query={"table": "embeddings", "filters": {"id": row["id"]}, "soft_delete": True},
                )
                result_data = cast(dict[str, Any], result.get("data", {}))
                deleted += result_data["result"]["deleted"]
        return {"deleted_count": deleted}

    PGVectorProvider.delete_by_external_ids = soft  # type: ignore[method-assign, assignment]
    restore.append(lambda: setattr(PGVectorProvider, "delete_by_external_ids", original))


def _mutant_no_skip(restore: list[Callable[[], None]]) -> None:
    original = event_embeddings_module._embed_event_or_skip

    def propagate(writer: EventEmbeddingWriter, embed_texts: Callable[[list[str]], Any], row: dict[str, object], replaces: int, tally: dict[str, int]) -> None:
        del embed_texts
        outcome = writer.embed_event(dict(row), replaces_chunks=replaces)
        tally["events_embedded"] += 1
        tally["chunks_stored"] += int(outcome["chunks_stored"])

    event_embeddings_module._embed_event_or_skip = propagate  # type: ignore[assignment]
    restore.append(lambda: setattr(event_embeddings_module, "_embed_event_or_skip", original))


def _mutant_no_outage_probe(restore: list[Callable[[], None]]) -> None:
    original = event_embeddings_module._embed_event_or_skip

    def skip_without_probe(writer: EventEmbeddingWriter, embed_texts: Callable[[list[str]], Any], row: dict[str, object], replaces: int, tally: dict[str, int]) -> None:
        del embed_texts
        try:
            outcome = writer.embed_event(dict(row), replaces_chunks=replaces)
        except Exception:
            tally["events_failed"] += 1
            return
        tally["events_embedded"] += 1
        tally["chunks_stored"] += int(outcome["chunks_stored"])

    event_embeddings_module._embed_event_or_skip = skip_without_probe  # type: ignore[assignment]
    restore.append(lambda: setattr(event_embeddings_module, "_embed_event_or_skip", original))


def _mutant_policy_recorded_despite_skip(restore: list[Callable[[], None]]) -> None:
    original = event_embeddings_module.finish_sweep

    def record_always(repository: Any, checkpoint: SweepCheckpoint) -> SweepCheckpoint | None:
        repository.set_event_embed_chunk_policy(checkpoint.target)
        repository.set_event_embed_checkpoint(None, expected=checkpoint)
        return None

    event_embeddings_module.finish_sweep = record_always  # type: ignore[assignment]
    restore.append(lambda: setattr(event_embeddings_module, "finish_sweep", original))


_MUTANTS: tuple[tuple[str, Callable[[list[Callable[[], None]]], None]], ...] = (
    ("budget not passed", _mutant_budget_not_passed),
    ("split bypassed", _mutant_split_bypassed),
    ("soft delete restored", _mutant_soft_delete_restored),
    ("no skip: a failed event halts the page / cursor not advanced", _mutant_no_skip),
    ("no outage probe: an outage advances the cursor", _mutant_no_outage_probe),
    ("policy recorded despite a skipped event", _mutant_policy_recorded_despite_skip),
)


def main() -> int:
    run_mutants = "--no-mutants" not in sys.argv
    print("=== event_embed_drain_progress_smoke ===")
    print(f"module under test: {event_embeddings_module.__file__}")
    print(f"provider under test: {sys.modules[PGVectorProvider.__module__].__file__}")
    global _verbose
    baseline = _run_scenarios()
    print(f"baseline: {len(baseline)} failing check(s)")
    for label in baseline:
        print(f"  baseline FAIL  {label}")
    survivors: list[str] = []
    _verbose = False
    if run_mutants:
        for name, apply in _MUTANTS:
            restore: list[Callable[[], None]] = []
            apply(restore)
            try:
                killed_by = _run_scenarios()
            finally:
                for undo in restore:
                    undo()
            if killed_by:
                print(f"  KILLED   mutant '{name}' by {len(killed_by)} failing check(s):")
                for label in killed_by:
                    print(f"             - {label}")
            else:
                survivors.append(name)
                print(f"  SURVIVED mutant '{name}' (no check failed)")
    ok = not baseline and not survivors
    print(f"=== {'OK' if ok else 'FAILED'}: baseline_failures={len(baseline)} surviving_mutants={len(survivors)} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
