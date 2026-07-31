"""Shared REAL-SHAPE fake ``StateManagementInterface`` for the v10 role smokes.

The first round of role smokes used in-memory fakes that returned a CONVENIENT
envelope (``data.updated``, no ``action_status``) — which masked Codex CODE
BLOCKER-1: the real postgres/rds providers return ``action_status`` +
``data.result.updated`` (mutations NESTED under ``result``; queries flat under
``data.records``), and do NOT raise on a provider error (they return an error
ActionResult). This harness returns the ACTUAL provider shapes so a smoke
exercises the production extraction path (a smoke against the old extraction
code would read 0/empty and FAIL).

Modelled faithfully:

* ``query_state`` / ``query_ordered`` → ``{action_status:'completed', data:{records:[...]}}``;
  equality-filtered on the supplied ``filters`` (incl. ``is_deleted`` when the
  caller passes it — the real ``query_state`` does NOT auto-exclude soft-deleted).
* ``update_state`` → ``{... data:{result:{updated:N}}}`` (NESTED).
* ``upsert_state`` → default ``{... data:{result:{upserted:1}}}`` merge, or
  ``{... data:{result:{inserted:bool,id}}}`` for ``on_conflict=do_nothing``.
* ``delete_records`` → ``{... data:{result:{deleted:N, soft_delete:bool}}}``;
  respects ``soft_delete`` (default TRUE → sets ``is_deleted=1`` + KEEPS the row;
  False → removes it).
* ``get_key_value`` → ``{... data:{value, found}}``; ``set_key_value`` →
  ``{... data:{...}}``.

``fail_next("update")`` injects a provider-ERROR ActionResult on the next op of
that kind (``action_status='failed'``) so a smoke can prove the fail-loud path.

SCHEMA-AWARE (slice-D fix-round + role outbox drift guard): every mutating
op against the enforced tables is checked against the declared schema
(+ standardizer columns) and a record naming a column OUTSIDE that schema
returns a non-completed envelope — exactly how postgres rejects a
'column does not exist' write. This closes the class that let a phantom
top-level ``session_label`` column ship green in the fake yet fail the live
cutover, and the same class for role/direct wake outbox bookkeeping columns.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ananta.services.state_service.ordered_query import (
    apply_ordered_query_in_memory,
    parse_ordered_query,
)

# The columns the state-plugin standardizer auto-adds to EVERY table (id + the
# external_id UNIQUE key + namespace + audit/timestamps + soft-delete). A write may
# name these ON TOP of the declared schema columns without being a schema drift.
_STANDARDIZER_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "external_id",
        "namespace",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "name",
        "is_deleted",
    },
)


def _schema_enforcement() -> dict[str, frozenset[str]]:
    """Schema-aware column allowlists for tables whose writers must not drift.

    Derived from the declared schemas (the single source of truth) + the
    standardizer columns. Lazy-imported so plugin/core src need only be on
    ``sys.path`` by construction time.
    """
    from ananta.llm.agent_messaging.role_binding import TABLE_ROLE_BINDING
    from ananta.llm.agent_messaging.schema import (
        TABLE_AGENT_DIRECT_WAKE,
        TABLE_AGENT_ROLE_MESSAGE,
        get_agent_direct_wake_schema,
        get_agent_role_message_schema,
    )

    from agent_messaging_plugin.schema import get_role_binding_schema

    role_binding = frozenset(get_role_binding_schema().columns)
    role_message = frozenset(
        get_agent_role_message_schema().tables[TABLE_AGENT_ROLE_MESSAGE].columns,
    )
    direct_wake = frozenset(
        get_agent_direct_wake_schema().tables[TABLE_AGENT_DIRECT_WAKE].columns,
    )
    return {
        TABLE_ROLE_BINDING: role_binding | _STANDARDIZER_COLUMNS,
        TABLE_AGENT_ROLE_MESSAGE: role_message | _STANDARDIZER_COLUMNS,
        TABLE_AGENT_DIRECT_WAKE: direct_wake | _STANDARDIZER_COLUMNS,
    }


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data}


def _err(message: str) -> dict[str, Any]:
    return {"action_status": "failed", "error": {"message": message}, "data": {}}




class RealShapeState:
    """In-memory state returning the real provider ActionResult envelopes."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._kv: dict[tuple[str, str], object] = {}
        self._fail: set[str] = set()
        # Standardizer parity (watcher-ack smoke fix-round): the real state
        # plugin stamps ``created_at``/``updated_at`` on EVERY insert, and
        # readers (role-inbox projection, the escalation sweep's owed-past-time
        # predicate) fail loud on a NULL created_at — a fake that omits the
        # stamp hides that whole reader class. Tests needing determinism
        # override ``now_iso`` with their controlled clock.
        self.now_iso: Callable[[], str] = lambda: datetime.now(UTC).isoformat()
        # SCHEMA-AWARE column enforcement: reject a write whose record names a column
        # outside the declared table schema (+ standardizer columns). The real
        # postgres providers reject these writes, so the fake must fail the same way.
        self._enforced_columns: dict[str, frozenset[str]] = _schema_enforcement()

    # -- test controls -------------------------------------------------
    def fail_next(self, op: str) -> None:
        """Make the NEXT op of kind ``op`` return a provider-error ActionResult."""
        self._fail.add(op)

    def _maybe_fail(self, op: str) -> dict[str, Any] | None:
        if op in self._fail:
            self._fail.discard(op)
            return _err(f"injected {op} failure")
        return None

    def _reject_unknown_columns(
        self, table: str, columns: Any,
    ) -> dict[str, Any] | None:
        """Model the postgres 'column does not exist' rejection for an enforced table."""
        allowed = self._enforced_columns.get(table)
        if allowed is None:
            return None
        unknown = sorted(str(c) for c in columns if c not in allowed)
        if unknown:
            return _err(
                f"column(s) {unknown} do not exist on {table!r} (not in the declared "
                "schema) — postgres would reject this write",
            )
        return None

    def _table(self, namespace: str, table: str) -> list[dict[str, Any]]:
        return self._rows.setdefault((namespace, table), [])

    def _stamp_insert(self, record: dict[str, Any]) -> None:
        """Standardizer parity: fill created_at/updated_at on insert (only)."""
        record.setdefault("created_at", self.now_iso())
        record.setdefault("updated_at", record["created_at"])

    @staticmethod
    def _match(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        return all(row.get(k) == v for k, v in filters.items())

    # -- query --------------------------------------------------------
    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        failed = self._maybe_fail("query")
        if failed is not None:
            return failed
        table = str(query["table"])
        filters = dict(query.get("filters", {}))
        rows = [
            dict(r) for r in self._table(namespace, table) if self._match(r, filters)
        ]
        return _ok({"records": rows, "count": len(rows)})

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        """Ordered / bounded / tie-safe read — DELEGATED to the real matcher.

        Faithful-BY-CONSTRUCTION (RIDER-3): instead of hand-rolling the grammar
        (the first-round fake ignored order_by/limit; the RIDER-1 fix hand-rolled
        them but ``_cell``-stringified INTEGER keys — reintroducing the 9→10
        lexical-order bug Codex fixed — and omitted the >100-refusal / <1-floor /
        composite-order-enforcement / is_deleted-default), this delegates to the
        SAME importable primitive the real in-memory backend runs:
        :func:`parse_ordered_query` (validate + harden) →
        :func:`apply_ordered_query_in_memory` (``_natural_key_value`` type-faithful
        sort + the full filter grammar + the ``after`` cursor + limit). A contract
        violation (non-composite order_by, over-cap limit without ``unbounded``,
        bad identifier) RAISES ``OrderedQueryError`` and propagates — exactly as
        the real in-memory backend does (``bootstrap_database_storage.select_ordered``
        + the postgres/rds providers call ``parse_ordered_query`` with NO
        try/except, so the exception TYPE reaches any test that catches it).
        Closes all four gaps for free and keeps the fake honest for the next test.
        """
        failed = self._maybe_fail("query")
        if failed is not None:
            return failed
        # Contract violations RAISE (via parse_ordered_query) and propagate —
        # the DATA-error path stays the injected failed-ActionResult above; only
        # a malformed ordered-query call raises, matching the real primitive.
        spec = parse_ordered_query(query)
        rows = list(self._table(namespace, spec.table))
        selected = apply_ordered_query_in_memory(rows, spec)
        return _ok(
            {"records": [dict(r) for r in selected], "count": len(selected)},
        )

    # -- mutations (NESTED under data.result) -------------------------
    def upsert_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        failed = self._maybe_fail("upsert")
        if failed is not None:
            return failed
        table = str(query["table"])
        record = dict(query["record"])
        rejected = self._reject_unknown_columns(table, record.keys())
        if rejected is not None:
            return rejected
        conflict = list(query.get("conflict_columns", []))
        on_conflict = query.get("on_conflict")
        if on_conflict not in {None, "do_nothing"}:
            return _err(f"unsupported on_conflict={on_conflict!r}")
        rows = self._table(namespace, table)
        for existing in rows:
            if all(existing.get(c) == record.get(c) for c in conflict):
                if on_conflict == "do_nothing":
                    return _ok({"result": {"inserted": False, "id": None}})
                existing.update(record)
                return _ok({"result": {"upserted": 1}})
        record.setdefault("is_deleted", 0)
        self._stamp_insert(record)
        rows.append(record)
        if on_conflict == "do_nothing":
            return _ok({"result": {"inserted": True, "id": record.get("id")}})
        return _ok({"result": {"upserted": 1}})

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        """INSERT — the v4 first-claim/migration race primitive. A live-``external_id``
        conflict RETURNS a non-completed envelope (never raises); the ``external_id``
        UNIQUE index IGNORES ``is_deleted`` (a tombstone still conflicts), modelling the
        postgres provider. ``fail_next('write')`` injects a provider error."""
        failed = self._maybe_fail("write")
        if failed is not None:
            return failed
        table = str(data["table"])
        record = dict(data["record"])
        rejected = self._reject_unknown_columns(table, record.keys())
        if rejected is not None:
            return rejected
        external_id = record.get("external_id")
        rows = self._table(namespace, table)
        if external_id is not None and any(r.get("external_id") == external_id for r in rows):
            return _err(f"duplicate external_id={external_id!r}")
        record.setdefault("is_deleted", 0)
        self._stamp_insert(record)
        rows.append(record)
        return _ok({"result": {"generated_id": f"gen-{len(rows)}", "inserted": 1}})

    def update_state(
        self, namespace: str, query: dict[str, Any], updates: dict[str, Any],
    ) -> dict[str, Any]:
        failed = self._maybe_fail("update")
        if failed is not None:
            return failed
        table = str(query["table"])
        rejected = self._reject_unknown_columns(table, updates.keys())
        if rejected is not None:
            return rejected
        filters = dict(query.get("filters", {}))
        affected = 0
        for r in self._table(namespace, table):
            if self._match(r, filters):
                r.update(updates)
                affected += 1
        return _ok({"result": {"updated": affected}})

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        failed = self._maybe_fail("delete")
        if failed is not None:
            return failed
        table = str(query["table"])
        filters = dict(query.get("filters", {}))
        soft = bool(query.get("soft_delete", True))
        rows = self._table(namespace, table)
        deleted = 0
        if soft:
            for r in rows:
                if self._match(r, filters):
                    r["is_deleted"] = 1
                    deleted += 1
        else:
            kept = [r for r in rows if not self._match(r, filters)]
            deleted = len(rows) - len(kept)
            self._rows[(namespace, table)] = kept
        return _ok({"result": {"deleted": deleted, "soft_delete": soft}})

    # -- key-value ----------------------------------------------------
    def get_key_value(
        self, namespace: str, key: str, scope: str = "GLOBAL",
    ) -> dict[str, Any]:
        failed = self._maybe_fail("get_kv")
        if failed is not None:
            return failed
        if (namespace, key) in self._kv:
            return _ok({"key": key, "value": self._kv[(namespace, key)], "found": True})
        return _ok({"key": key, "value": None, "found": False})

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: object,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> dict[str, Any]:
        failed = self._maybe_fail("set_kv")
        if failed is not None:
            return failed
        self._kv[(namespace, key)] = value
        return _ok({"key": key})

    # -- assertion helper --------------------------------------------
    def rows(self, namespace: str, table: str) -> list[dict[str, Any]]:
        return self._table(namespace, table)
