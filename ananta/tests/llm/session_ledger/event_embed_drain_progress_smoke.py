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
6. a backfill sweep that skipped an event records no policy, so the sweep repeats.

Each mutant below is run through the same scenarios and must be killed by a named
failing check: budget not passed, split bypassed, soft delete restored, no skip
(cursor not advanced on a failed event), no outage probe (cursor advanced on an
outage).

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/event_embed_drain_progress_smoke.py
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "pgvector_service_plugin" / "src"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.interfaces.embedding_service_interface import TokenBudget  # noqa: E402
from ananta.llm.session_ledger import event_embeddings as event_embeddings_module  # noqa: E402
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
        action_status="completed", data={"result": payload}, actions=[], error=None,
        timestamp=datetime.now(UTC).isoformat(),
    )


def _err(code: str, message: str) -> ActionResult:
    return ActionResult(
        action_status="error", data=None, actions=[],
        error={"code": code, "message": message, "type": "SmokeError"},
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

    def input_token_budget(self) -> TokenBudget:
        return TokenBudget(_BUDGET_TOKENS, _tokens)

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
                        "message": f'duplicate key value violates unique constraint "session_ledger_event__embeddings_external_id_key" '
                        f"Key (external_id)=({record.get('external_id')}) already exists.",
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
        self._provider = PGVectorProvider.__new__(PGVectorProvider)
        self._provider._state_service = table  # pyright: ignore[reportPrivateUsage]
        self._provider.config = SimpleNamespace(schema_name="public")  # type: ignore[assignment]

    def _call(self, call: Callable[[], dict[str, Any]]) -> ActionResult:
        try:
            return _ok(call())
        except Exception as exc:  # the plugin wraps every provider failure into an error envelope
            return _err("pgvector.operation_failed", str(exc))

    def store_vectors(self, namespace: str, vectors: list[dict[str, object]]) -> ActionResult:
        return self._call(lambda: self._provider.store_vectors(namespace, [dict(v) for v in vectors]))

    def delete_by_external_ids(self, namespace: str, external_ids: list[str]) -> ActionResult:
        return self._call(lambda: self._provider.delete_by_external_ids(namespace=namespace, external_ids=external_ids))

    def find_missing_external_ids(self, namespace: str, candidate_external_ids: list[str]) -> ActionResult:
        return self._call(lambda: self._provider.find_missing_external_ids(namespace=namespace, candidate_external_ids=candidate_external_ids))


class _DrainRepo(SessionLedgerRepository):
    """Real repository (real KV cursor / policy / counter) with only the candidate read serving a fixed corpus."""

    __slots__ = ("corpus",)

    def __init__(self, state_service: Any, corpus: list[dict[str, object]]) -> None:
        super().__init__(state_service)
        self.corpus = list(corpus)

    def list_event_embedding_candidates(
        self, *, limit: int, after: tuple[object, object] | None = None, order_column: str = "event_at", ascending: bool = False,
    ) -> list[dict[str, object]]:
        keyed = sorted(self.corpus, key=lambda row: (str(row[order_column]), str(row["id"])), reverse=not ascending)
        if after is not None:
            key = (str(after[0]), str(after[1]))
            keyed = [r for r in keyed if ((str(r[order_column]), str(r["id"])) > key) == ascending and (str(r[order_column]), str(r["id"])) != key]
        return [dict(row) for row in keyed[:limit]]


def _event(row_id: str, hour: int, text: str) -> dict[str, object]:
    stamp = f"2026-10-01T{hour:02d}:00:00"
    return {
        "id": row_id, "session_id": "les_1", "sequence": hour, "event_type": "MESSAGE", "role": "assistant",
        "content_text": text, "content_json": None, "event_at": stamp, "imported_at": stamp,
    }


def _big_text(tokens: int) -> str:
    return " ".join(["ab"] * tokens)


class _World:
    def __init__(self, corpus: list[dict[str, object]], *, policy_recorded: bool) -> None:
        self.state = StubStateService()
        self.repo = _DrainRepo(self.state, corpus)
        self.embedder = _Embedder()
        self.table = _UniqueVectorTable()
        self.writer = EventEmbeddingWriter(
            repository=self.repo, embedding_service=self.embedder,  # type: ignore[arg-type]
            vector_service=_VectorService(self.table),  # type: ignore[arg-type]
        )
        if policy_recorded:
            self.repo.set_event_embed_chunk_policy(chunk_policy(self.embedder.input_token_budget()))


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


_SINK = _Warnings()


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
    _check(world.repo.get_event_embed_chunk_policy() is None, "[6] a sweep that skipped an event records no policy, so the backfill repeats")


def scenario_outage_halts_without_advancing() -> None:
    corpus = [_event("evt_a", 1, "one"), _event("evt_b", 2, "two")]
    world = _World(corpus, policy_recorded=True)
    world.embedder.down = True
    outcome = world.writer.drain_missing_events(page_size=100)
    _check(outcome["halted_on_error"] is True and outcome["events_embedded"] == 0, "[5] an embedder outage halts the drain")
    _check(world.repo.get_event_embed_cursor() is None, "[5] the cursor is NOT advanced past an outage")


_SCENARIOS = (
    scenario_big_event_is_split_and_the_cursor_advances,
    scenario_backfill_reembed_lands,
    scenario_tombstone_is_cleared_on_the_next_fire,
    scenario_one_failed_event_is_skipped_loudly,
    scenario_a_skipped_event_keeps_the_backfill_pending,
    scenario_outage_halts_without_advancing,
)


def _run_scenarios() -> list[str]:
    """Run every scenario; return the failing check labels."""
    global _passed
    _failed.clear()
    passed_before = _passed
    logger = logging.getLogger(_PROBE_LOGGER)
    logger.addHandler(_SINK)
    logger.propagate = False  # the outage scenario's halt ERROR and the skip WARNING are expected, captured output
    try:
        for scenario in _SCENARIOS:
            scenario()
    finally:
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
                namespace=namespace, query={"table": "embeddings", "filters": {"external_id": [external_id], "is_deleted": 0}},
            )
            for row in active["data"]["records"]:
                result = self._state_service.delete_records(
                    namespace=namespace, query={"table": "embeddings", "filters": {"id": row["id"]}, "soft_delete": True},
                )
                deleted += result["data"]["result"]["deleted"]
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
    original = event_embeddings_module._record_policy_backfill

    def record_always(repository: Any, policy: str, *, completed: bool) -> None:
        del completed
        repository.set_event_embed_chunk_policy(policy)

    event_embeddings_module._record_policy_backfill = record_always  # type: ignore[assignment]
    restore.append(lambda: setattr(event_embeddings_module, "_record_policy_backfill", original))


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
