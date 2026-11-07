#!/usr/bin/env python3
"""Exercise the five real transactional insert callers without a database."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(ROOT / "ananta/src"),
    str(ROOT / "plugins/default_knowledge_plugin/src"),
    str(ROOT / "ananta/tests/llm/session_ledger"),
]

from _stub_state_service import StubStateService  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface, StateTransaction  # noqa: E402
from ananta.llm.agent_messaging import repository as messages  # noqa: E402
from ananta.llm.agent_messaging.models import MessageKind, MessageRole, TextPart  # noqa: E402
from ananta.llm.session_ledger import polling_driver, summarize  # noqa: E402
from ananta.llm.session_ledger.types import CursorScope  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402
from default_knowledge_plugin import kb_lifecycle  # noqa: E402

NOW = datetime(2026, 10, 4, tzinfo=UTC)


class RecordingTransaction:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []
        self.existing: list[dict[str, object]] = []
        self.updates = 0

    def write_state(self, namespace: str, data: dict[str, object]) -> str:
        record = cast(dict[str, object], data["record"])
        self.records.append((namespace, str(data["table"]), record))
        return str(record["id"])

    def query_state(self, namespace: str, data: dict[str, object]) -> list[dict[str, object]]:
        return self.existing

    def update_state(self, namespace: str, data: dict[str, object], updates: dict[str, object]) -> int:
        self.updates += 1
        return 1

    def increment_and_return(self, namespace: str, data: dict[str, object]) -> int:
        return 1


class RecordingState(StateService):
    """Keep REAL prepare_record; replace only the persistence boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.transaction = RecordingTransaction()

    @contextmanager
    def transactional(self) -> Generator[StateTransaction]:
        yield cast(StateTransaction, self.transaction)


def check_record(test: unittest.TestCase, state: RecordingState, prefix: str, name: str | None = None) -> dict[str, object]:
    test.assertEqual(len(state.transaction.records), 1)
    namespace, _table, record = state.transaction.records[0]
    test.assertTrue(str(record["id"]).startswith(prefix), "id_prefix")
    test.assertEqual(record["namespace"], namespace)
    test.assertEqual(record.get("name"), name or record["id"], "default_name")
    test.assertEqual(record.get("created_by"), "ananta.services.state_service", "created_by")
    test.assertEqual(record.get("updated_by"), "ananta.services.state_service", "updated_by")
    return record


class TransactionalStandardFields(unittest.TestCase):
    def test_live_fixture_adapters_inherit_real_population(self) -> None:
        paths = (
            "ananta/tests/llm/agent_messaging/repository_write_migration_live_smoke.py",
            "ananta/tests/llm/session_ledger/polling_driver_migration_live_smoke.py",
            "ananta/tests/llm/session_ledger/summarize_migration_live_smoke.py",
            "ananta/tests/llm/session_ledger/ingest_migration_live_smoke.py",
        )
        for relative in paths:
            with self.subTest(fixture=relative):
                spec = importlib.util.spec_from_file_location(Path(relative).stem, ROOT / relative)
                self.assertIsNotNone(spec)
                if spec is None or spec.loader is None:
                    raise AssertionError(f"missing fixture module {relative}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                print(f"fixture module.__file__: {module.__file__}", flush=True)
                factory = cast(Callable[[object], StateService], module._LiveStateAdapter)
                adapter = factory(object())
                self.assertIsInstance(adapter, StateService, "fixture_real_state_service")
                self.assertIs(adapter.prepare_record.__func__, StateService.prepare_record, "fixture_inherited_population")
                record = adapter.prepare_record("core", "agent_message", {"id": "agm-explicit"})
                self.assertEqual(record["id"], "agm-explicit")
                self.assertEqual(record["name"], "agm-explicit")
                self.assertEqual(record["namespace"], "core")
                self.assertEqual(record["created_by"], "ananta.services.state_service")
                self.assertEqual(record["updated_by"], "ananta.services.state_service")

    def test_message_append_preserves_explicit_id_and_content(self) -> None:
        state = RecordingState()
        repository = messages.AgentMessagingRepository(cast(StateManagementInterface, state), clock=lambda: NOW)
        message = messages.NewMessage(role=MessageRole.ORIGINATOR, kind=MessageKind.MESSAGE, content=[TextPart(type="text", text="hello")])
        result = repository.append_message(thread_id="ath-existing", message=message)
        record = check_record(self, state, "agm-")
        self.assertEqual(record["id"], result.id)
        self.assertEqual(record["created_at"], NOW)
        self.assertEqual(record["content"], [{"type": "text", "text": "hello"}])

    def test_source_cursor_insert_and_existing_update(self) -> None:
        state = RecordingState()
        repository = polling_driver.SessionLedgerPollingDriverMixin(cast(StateManagementInterface, state), clock=lambda: NOW)
        repository.write_cursor(source_id="src-existing", scope=CursorScope.EVENT_READ, scope_key="scope", cursor_payload={"offset": 4})
        record = check_record(self, state, "scu_")
        self.assertEqual(record["cursor_payload"], {"offset": 4})
        self.assertEqual(record["created_at"], NOW)
        state.transaction.existing = [{"id": record["id"]}]
        repository.write_cursor(source_id="src-existing", scope=CursorScope.EVENT_READ, scope_key="scope", cursor_payload={"offset": 5})
        self.assertEqual(len(state.transaction.records), 1)
        self.assertEqual(state.transaction.updates, 1)

    def test_active_lease_insert_and_existing_refresh(self) -> None:
        state = RecordingState()
        repository = polling_driver.SessionLedgerPollingDriverMixin(cast(StateManagementInterface, state), clock=lambda: NOW)
        repository.record_lease_ping(session_id="ses-existing", source_id="src-existing", ttl_seconds=60)
        record = check_record(self, state, "lse_")
        self.assertEqual(record["created_at"], NOW)
        state.transaction.existing = [{"id": record["id"]}]
        repository.record_lease_ping(session_id="ses-existing", source_id="src-existing", ttl_seconds=120)
        self.assertEqual(len(state.transaction.records), 1)
        self.assertEqual(state.transaction.updates, 1)

    def test_summary_insert_preserves_returned_id_and_timestamp(self) -> None:
        state = RecordingState()
        repository = summarize.SessionLedgerSummarizeMixin(cast(StateManagementInterface, state), clock=lambda: NOW)
        summary_id = repository.persist_summary(session_id="ses-existing", chunk_index=0, summary_text="summary", embedding_vector_id="ev-fixture", generated_by_client_id="client", generated_at=NOW)
        record = check_record(self, state, "sum_")
        self.assertEqual(record["id"], summary_id)
        self.assertEqual(record["summary_text"], "summary")
        self.assertEqual(record["generated_at"], NOW)

    def test_kb_install_preserves_explicit_id_and_business_name(self) -> None:
        state = RecordingState()
        manifest = SimpleNamespace(name="manifest", tags=["test"], process_keys=[])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample").mkdir()
            with (
                patch.object(kb_lifecycle, "resolve_source", return_value=(None, None, None)),
                patch.object(kb_lifecycle, "classify_source_type", return_value="local"),
                patch.object(kb_lifecycle, "resolve_manifest", return_value=manifest),
                patch.object(kb_lifecycle, "get_install_record", return_value=None),
                patch.object(kb_lifecycle, "index_files", return_value=(["mem-test"], 1)),
                patch.object(state, "generate_id", return_value="kin-explicit"),
            ):
                result = kb_lifecycle.install_kb("sample", None, root, state, object(), object(), budget=None)
        record = check_record(self, state, "kin-", "sample")
        self.assertEqual(record["id"], "kin-explicit")
        self.assertEqual(record["memory_ids"], ["mem-test"])
        self.assertEqual(result["status"], "success")

    def test_stub_prepare_record_matches_real_population(self) -> None:
        stub = StubStateService()
        real = StateService()
        explicit_id: dict[str, object] = {"id": "agm-explicit"}
        explicit_all: dict[str, object] = {"id": "agm-explicit", "name": "business", "created_by": "creator", "updated_by": "updater"}
        for record in (explicit_id, explicit_all):
            with self.subTest(record=record):
                before = dict(record)
                self.assertEqual(stub.prepare_record("core", "agent_message", record), real.prepare_record("core", "agent_message", record), "stub_population_parity")
                self.assertEqual(record, before, "stub_input_unchanged")
        with self.assertRaises(ValueError, msg="stub_missing_id_fails"):
            stub.prepare_record("core", "agent_message", {"name": "no-id"})


if __name__ == "__main__":
    for module in (messages, polling_driver, summarize, kb_lifecycle):
        print(f"module.__file__: {module.__file__}", flush=True)
    unittest.main()
