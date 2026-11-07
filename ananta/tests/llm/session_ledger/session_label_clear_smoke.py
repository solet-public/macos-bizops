#!/usr/bin/env python3
"""Guarded label clearing through the public service and private PostgreSQL.

Requires provision_test_pg's private fixture; a database skip is not green.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / "ananta/src")]
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_SESSION  # noqa: E402
from ananta.services.session_ledger_service.interfaces.public import SessionLedgerIngestAPI  # noqa: E402
from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: E402
from session_usage_totals_smoke import NOW, Fixture, check, physical_events, submit  # noqa: E402

OPERATOR = CallContext.for_operator()
AUDIT = "session_label_clear"


def service(f: Fixture) -> SessionLedgerService:
    """Construct the real facade with inert services unrelated to this verb."""
    result = SessionLedgerService(state_service=f.state, blob_storage_service=cast(Any, SimpleNamespace()), plugin_manager=cast(Any, SimpleNamespace(plugins={})))
    result._repository = f.repo
    return result


def rows(f: Fixture, table: str, filters: dict[str, object]) -> list[dict[str, object]]:
    return f.repo._query(table, filters)


def label(f: Fixture, sid: str) -> object:
    return rows(f, TABLE_SESSION, {"id": sid})[0]["vendor_session_label"]


def clear(s: SessionLedgerService, sid: str, **kwargs: Any) -> dict[str, Any]:
    return s.clear_session_label(sid, call_context=OPERATOR, **kwargs)


def confirmed(s: SessionLedgerService, sid: str) -> dict[str, Any]:
    preview = clear(s, sid)
    return clear(s, sid, expected_updated_at=preview["expected_updated_at"], confirm=True)


def expect_error(callback: Any, name: str) -> None:
    try:
        callback()
    except (ValueError, RuntimeError):
        check(True, name)
    else:
        raise AssertionError(name)


def immutable_fields(row: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in row.items() if k not in {"vendor_session_label", "updated_at"}}


def lifecycle(f: Fixture, s: SessionLedgerService) -> None:
    sid, _ = f.session("codex", "same", vendor_label="old")
    sibling, _ = f.session("claude_code", "same", vendor_label="sibling")
    same_vendor, _ = f.session("codex", "same", label="sibling", vendor_label="other-proof")
    snapshot = rows(f, TABLE_SESSION, {"id": [sibling, same_vendor]})
    events = physical_events(f, sid)
    target_before = rows(f, TABLE_SESSION, {"id": sid})[0]
    preview = clear(s, sid)
    check(preview["status"] == "preview" and label(f, sid) == "old" and not rows(f, AUDIT, {}), "PREVIEW_NO_WRITES")
    result = clear(s, sid, confirm=True, expected_updated_at=preview["expected_updated_at"])
    check(label(f, sid) is None, "LABEL_IS_NULL")
    target_after = rows(f, TABLE_SESSION, {"id": sid})[0]
    check(immutable_fields(target_before) == immutable_fields(target_after), "CONTENT_IDENTITY_UNCHANGED")
    receipts = rows(f, AUDIT, {"session_id": sid})
    check(result["status"] == "cleared" and len(receipts) == 1 and result["clear_id"] == receipts[0]["id"], "CLEAR_READBACK_AND_RECEIPT")
    check(rows(f, TABLE_SESSION, {"id": [sibling, same_vendor]}) == snapshot and physical_events(f, sid) == events, "NON_TARGET_ROWS_UNCHANGED")
    check(confirmed(s, sid)["status"] == "already_clear" and len(rows(f, AUDIT, {"session_id": sid})) == 1, "REPEATED_CLEAR_NO_RECEIPT")
    restoration(f, s, sid, preview)


def restoration(f: Fixture, s: SessionLedgerService, sid: str, preview: dict[str, Any]) -> None:
    f.session("codex", "same", vendor_label="restored")
    f.session("codex", "same", vendor_label=None)
    check(label(f, sid) == "restored", "INGEST_RESTORE_NONE_PRESERVES")
    f.repo._clock = lambda: NOW + timedelta(seconds=1)
    f.session("codex", "same", vendor_label="corrected")
    stale = clear(s, sid, confirm=True, expected_updated_at=preview["expected_updated_at"])
    check(stale["status"] == "conflict" and label(f, sid) == "corrected" and len(rows(f, AUDIT, {"session_id": sid})) == 1, "STALE_CLEAR_NO_CHANGE")
    check(confirmed(s, sid)["status"] == "cleared" and len(rows(f, AUDIT, {"session_id": sid})) == 2, "RESTORE_RECLEAR_RETAINS_HISTORY")
    null, _ = f.session("chatgpt", "null", kind="chatgpt_export")
    deleted, _ = f.session("claude_code", "deleted", vendor_label="keep")
    f.repo._update(TABLE_SESSION, {"id": deleted}, {"is_deleted": 1})
    check(clear(s, null)["status"] == "already_clear" and confirmed(s, deleted)["status"] == "not_found" and confirmed(s, "les-missing")["status"] == "not_found", "NULL_DELETED_MISSING_NO_WRITES")


def auth_reads(f: Fixture, s: SessionLedgerService) -> None:
    sid, _ = f.session("claude_code", "auth", vendor_label="auth")
    before = rows(f, TABLE_SESSION, {"id": sid})
    for context in (None, CallContext.for_external(), CallContext.for_plugin("fixture")):
        for method in (s.clear_session_label, s.list_session_label_clears):
            try:
                method(sid, call_context=context)
            except Exception as exc:
                check("operator" in str(exc), "NON_OPERATOR_NO_WRITES")
            else:
                raise AssertionError("NON_OPERATOR_NO_WRITES")
    check(rows(f, TABLE_SESSION, {"id": sid}) == before and not rows(f, AUDIT, {"session_id": sid}), "AUTH_SNAPSHOT_UNCHANGED")
    preview = s.clear_session_label(sid, call_context=CallContext.for_operator_equivalent("fixture"))
    check(preview["status"] == "preview", "OPERATOR_EQUIVALENT_PASS")
    for token in (None, "bad", "2026-01-01T00:00:00"):
        expect_error(lambda token=token: clear(s, sid, confirm=True, expected_updated_at=token), "INVALID_TOKEN_NO_WRITES")
    for envelope in ({"action_status": "failed", "error": "fixture"}, {"action_status": "completed"}, {"action_status": "completed", "data": []}, {"action_status": "completed", "data": {"records": {}}}, {"action_status": "completed", "data": {"records": [1]}}):
        with patch.object(f.state, "read_state", return_value=envelope):
            expect_error(lambda: clear(s, sid), "MALFORMED_SESSION_READ_RAISES")
        with patch.object(f.state, "query_ordered", return_value=envelope):
            expect_error(lambda: s.list_session_label_clears(sid, call_context=OPERATOR), "MALFORMED_HISTORY_READ_RAISES")
    check(rows(f, TABLE_SESSION, {"id": sid}) == before and not rows(f, AUDIT, {"session_id": sid}), "READ_FAILURE_ZERO_WRITES")


def atomicity(f: Fixture, s: SessionLedgerService) -> None:
    sid, _ = f.session("claude_code", "race", vendor_label="race")
    token = clear(s, sid)["expected_updated_at"]
    transaction = f.state.transactional
    barrier = Barrier(2)

    @contextmanager
    def synchronized() -> Any:
        with transaction() as txn:
            query = txn.query_state

            def read(namespace: str, filters: dict[str, object]) -> list[dict[str, object]]:
                result = query(namespace, filters)
                barrier.wait(timeout=10)
                return result

            with patch.object(txn, "query_state", side_effect=read):
                yield txn

    with patch.object(f.state, "transactional", side_effect=synchronized), ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: clear(s, sid, confirm=True, expected_updated_at=token), range(2)))
    check(sum(r["status"] == "cleared" for r in results) == 1 and len(rows(f, AUDIT, {"session_id": sid})) == 1, "COMPETING_CLEAR_SINGLE_RECEIPT")
    sid, _ = f.session("claude_code", "rollback", vendor_label="keep")
    transaction = f.state.transactional

    @contextmanager
    def broken() -> Any:
        with transaction() as txn:
            original = txn.write_state

            def write(namespace: str, data: dict[str, object]) -> str:
                if data["table"] == AUDIT:
                    raise RuntimeError("AUDIT_INSERT_REACHED")
                return original(namespace, data)

            with patch.object(txn, "write_state", side_effect=write):
                yield txn

    with patch.object(f.state, "transactional", side_effect=broken), patch.object(f.state, "write_state", side_effect=RuntimeError("AUDIT_INSERT_REACHED")):
        try:
            confirmed(s, sid)
        except RuntimeError as exc:
            check(str(exc) == "AUDIT_INSERT_REACHED", "AUDIT_INSERT_REACHED")
        else:
            raise AssertionError("AUDIT_INSERT_REACHED")
    check(label(f, sid) == "keep" and not rows(f, AUDIT, {"session_id": sid}), "AUDIT_FAILURE_ROLLS_BACK")


def history(f: Fixture, s: SessionLedgerService) -> None:
    sid, source = f.session("claude_code", "history", vendor_label="0")
    for index in range(102):
        f.session("claude_code", "history", vendor_label=str(index))
        confirmed(s, sid)
    f.repo._update("source", {"id": source}, {"enabled": False, "is_deleted": 1})
    first = s.list_session_label_clears(sid, limit=1000, call_context=OPERATOR)
    second = s.list_session_label_clears(sid, **first["next_cursor"], call_context=OPERATOR)
    identities = [r["id"] for r in first["clears"] + second["clears"]]
    check(len(first["clears"]) == 100 and len(identities) == len(set(identities)) == 102 and second["next_cursor"] is None, "HISTORY_PAGING_NO_GAPS")
    check(first["current"]["status"] == "found" and first["current"]["current_label"] is None, "DISABLED_SOURCE_HISTORY_READABLE")
    check(len({str(row["cleared_at"]) for row in first["clears"] + second["clears"]}) == 1, "HISTORY_TIMESTAMP_TIES")
    f.repo._update(TABLE_SESSION, {"id": sid}, {"is_deleted": 1})
    historical = s.list_session_label_clears(sid, call_context=OPERATOR)
    check(len(historical["clears"]) == 100 and historical["current"]["status"] == "not_found", "DELETED_SESSION_RETAINS_RECEIPTS")
    empty = s.list_session_label_clears("les-missing", limit=0, call_context=OPERATOR)
    check(empty["clears"] == [] and empty["next_cursor"] is None and empty["current"]["status"] == "not_found", "VALID_EMPTY_HISTORY")
    expect_error(lambda: s.list_session_label_clears(sid, after_id="orphan", call_context=OPERATOR), "CURSOR_PAIR_REQUIRED")
    categories(f, s)


def category_vendor(kind: str) -> str:
    for prefix, vendor in (("codex", "codex"), ("claude_code", "claude_code"), ("claude_ai", "claude_ai"), ("chatgpt", "chatgpt")):
        if kind.startswith(prefix):
            return vendor
    return "agent_messaging"


def categories(f: Fixture, s: SessionLedgerService) -> None:
    from ananta.llm.session_ledger.types import IngestSourceKind

    for kind in IngestSourceKind:
        vendor = category_vendor(kind.value)
        target, source = f.session(vendor, "category-" + kind.value, kind=kind.value, vendor_label="any")
        f.repo._update("source", {"id": source}, {"enabled": False})
        check(confirmed(s, target)["status"] == "cleared", "VENDOR_SOURCE_UNRESTRICTED:" + kind.value)


def independent_alias(f: Fixture, s: SessionLedgerService) -> None:
    from session_usage_totals_smoke import codex_lines, one

    runtime = "00000000-0000-4000-8000-000000000099"
    target = f.ingest("proof-a", codex_lines((10, 20)), vendor="codex")
    f.session("codex", "proof-a", vendor_label=runtime)
    f.ingest("proof-b", codex_lines((10, 20)), vendor="codex")
    f.session("codex", "proof-b", vendor_label=runtime)
    check(one(f, runtime)["usage_event_count"] == 2, "INDEPENDENT_ALIAS_BEFORE")
    confirmed(s, target)
    check(one(f, runtime)["usage_event_count"] == 2, "INDEPENDENT_ALIAS_SURVIVES_CLEAR")


def main() -> None:
    f = Fixture()
    try:
        check(hasattr(SessionLedgerIngestAPI, "clear_session_label"), "PUBLIC_CLEAR_DECLARED")
        s = service(f)
        for name, arguments in (("clear_session_label", {"session_id": "les-fixture", "expected_updated_at": NOW.isoformat(), "confirm": True}), ("list_session_label_clears", {"session_id": "les-fixture", "limit": 100, "after_cleared_at": NOW.isoformat(), "after_id": "slc-fixture"})):
            method = getattr(SessionLedgerIngestAPI, name)
            submit(method, "service_interface::session_ledger_service::" + name, arguments)
            check(method._service_interface_metadata.requires_call_context, "BRIDGE_ARGUMENTS_AND_CONTEXT")
        lifecycle(f, s)
        auth_reads(f, s)
        atomicity(f, s)
        history(f, s)
        independent_alias(f, s)
    finally:
        f.close()


if __name__ == "__main__":
    main()
