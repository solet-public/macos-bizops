#!/usr/bin/env python3
"""Session usage contract through real parsers, normalizers, ingest and repository.

Database cases require provision_test_pg's private DESK fixture. No live DSN,
raw SQL, handwritten DDL, or provider dependency is used by this smoke.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / "ananta/src"), str(ROOT / "plugins/agent_messaging_plugin/tests")]
for package in ("agent_messaging_plugin", "postgres_state_management_plugin", "claude_code_filesystem_session_source_plugin", "codex_filesystem_session_source_plugin"):
    sys.path.insert(0, str(ROOT / "plugins" / package / "src"))
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter  # noqa: E402
from ananta.llm.session_ledger.importer import LedgerPollError, SessionLedgerImporter  # noqa: E402
from ananta.llm.session_ledger.registry import SessionSourceRegistry  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import NAMESPACE, TABLE_EVENT, TABLE_SESSION, TABLE_SOURCE, get_session_ledger_schema  # noqa: E402
from ananta.llm.session_ledger.types import CursorScope, ImportBatchStatus, IngestSourceKind, SourceVendor  # noqa: E402
from ananta.llm.session_ledger.vendor import claude_code, codex  # noqa: E402
from ananta.services.plugin_schema_service.serialization import to_json  # noqa: E402
from ananta.services.session_ledger_service.interfaces.public import SessionLedgerReadAPI  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402

NOW = datetime(2026, 10, 17, 0, tzinfo=UTC)


def check(condition: object, name: str) -> None:
    assert condition, name
    print("PASS", name)


def submit(method: Any, key: str, arguments: dict[str, Any]) -> None:
    metadata = method._service_interface_metadata
    factory = ActionFactory(
        process_registry={"processes": {key: {"parameters": {name: parameter.to_dict() for name, parameter in metadata.parameters.items()}}}},
        template_engine=SimpleNamespace(resolve_templates=lambda action_def, context: action_def),  # pyright: ignore[reportArgumentType]
        state_service=SimpleNamespace(generate_unique_string=lambda **kw: {"action_status": "completed", "data": {"random_string": "fixture"}}),  # pyright: ignore[reportArgumentType]
        action_event_recorder=SimpleNamespace(store_action_event=lambda action: "ae-fixture"),  # pyright: ignore[reportArgumentType]
    )
    factory.submit_action_definition({"process_key": key, "arguments": arguments, "flow_id": "flow-usage", "result_processor_kind": None, "error_processor_kind": None})


def claude_line(index: int, *, identity: str | None = None, model: str | None = "model-a", output: int = 10, minute: int = 0, **extra: Any) -> str:
    message: dict[str, Any] = {"role": "assistant", "content": [{"type": "text", "text": "fixture"}], "usage": {"input_tokens": 3, "output_tokens": output, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 5, **extra}}
    if identity is not None:
        message["id"] = identity
    if model is not None:
        message["model"] = model
    return json.dumps({"type": "assistant", "uuid": f"u-{index}", "sessionId": "fixture", "timestamp": (NOW + timedelta(minutes=minute)).isoformat(), "message": message})


class Fixture:
    def __init__(self) -> None:
        try:
            from postgres_state_management_plugin.plugin import PostgresStatePlugin
        except ModuleNotFoundError as exc:
            print(f"SKIP optional PostgreSQL plugin unavailable: {exc.name}")
            raise SystemExit(77) from exc
        if "PROJECT_SOLET_DESK_TEST_DSN" not in os.environ:
            print("SKIP private PostgreSQL fixture is not provisioned")
            print('BORN_CLONE_SKIP_WITNESS={"reason":"local_postgres_fixture_unreachable"}')
            raise SystemExit(77)
        dsn = dict(word.split("=", 1) for word in shlex.split(os.environ["PROJECT_SOLET_DESK_TEST_DSN"]))
        assert dsn["host"] == "127.0.0.1" and int(dsn["port"]) != 5432 and dsn["dbname"] == "project_solet_desk_test", "private fixture required"
        self.plugin = PostgresStatePlugin()
        self.plugin.set_vault_service(SimpleNamespace(retrieve=lambda **_kw: {"action_status": "completed", "data": {"value": dsn.get("password", "")}}))  # pyright: ignore[reportArgumentType]
        config = {"host": dsn["host"], "port": int(dsn["port"]), "database": dsn["dbname"], "user": dsn["user"], "pg_schema": "usage_meter_" + uuid4().hex[:12], "pool_size": 2}
        # The launcher identity is scrubbed; only this injected fixture vault
        # needs a synthetic namespace while the plugin builds its connection.
        with patch.dict(os.environ, {"SOLET_NAME": "usage-fixture"}):
            self.plugin._initialize(config)  # pyright: ignore[reportArgumentType]
        initialized = asyncio.run(self.plugin.start_services())
        assert initialized["action_status"] == "completed", initialized  # pyright: ignore[reportTypedDictNotRequiredAccess]
        installed = self.plugin.install_plugin_schema(NAMESPACE, to_json(get_session_ledger_schema()))
        assert installed["status"] in ("installed", "installed_and_adopted", "adopted"), installed
        self.schemas = {NAMESPACE: get_session_ledger_schema()}
        self.state = StateService(plugin_manager=SimpleNamespace(get_plugin=lambda name: self.plugin), state_plugin_name="postgres_state_management_plugin")  # pyright: ignore[reportArgumentType]
        self.state.set_schema_manager(SimpleNamespace(get_schema=lambda namespace: self.schemas[namespace]))  # pyright: ignore[reportArgumentType]
        self.repo = SessionLedgerRepository(state_service=self.state, clock=lambda: NOW)  # pyright: ignore[reportArgumentType]
        from ananta.llm.session_ledger.read import SessionUsageReader

        self.reader = SessionUsageReader(self.repo)
        self.sources: dict[str, str] = {}
        self.temp = tempfile.TemporaryDirectory()

    def close(self) -> None:
        asyncio.run(self.plugin.stop_services())
        self.temp.cleanup()

    def source(self, kind: str, label: str = "one") -> str:
        key = kind + label
        if key not in self.sources:
            self.sources[key] = self.repo.insert_source(source_kind=IngestSourceKind(kind), root_uri=f"file:///tmp/usage-fixture-{key}", account_label=None)
        return self.sources[key]

    def session(self, vendor: str, external: str, *, kind: str | None = None, label: str = "one", when: datetime = NOW, vendor_label: str | None = None) -> tuple[str, str]:
        kind = kind or vendor + "_local"
        source = self.source(kind, label)
        session = self.repo.upsert_session(source_id=source, external_session_id=external, vendor=SourceVendor(vendor), source_kind=IngestSourceKind(kind), vendor_session_label=vendor_label, project_path=None, first_event_at=when, last_event_at=when)
        return session, source

    def ingest(self, external: str, lines: list[str], *, vendor: str = "claude_code", label: str = "one") -> str:
        session, source = self.session(vendor, external, label=label)
        batch = self.repo.start_batch(source, polling_lease_token="fixture")
        if vendor == "claude_code":
            package = "claude_code_filesystem_session_source_plugin"
            source_root = ROOT / "plugins" / package / "src"
            if not source_root.is_dir():
                print(f"SKIP optional Claude ingest plugin unavailable: {package}")
                raise SystemExit(77)
            try:
                plugin_module = importlib.import_module(package + ".plugin")
            except ImportError as exc:
                raise AssertionError(f"present Claude ingest plugin failed to import: {exc}") from exc
            normalizer = plugin_module.ClaudeCodeFilesystemSessionSourcePlugin
            transcripts = [{**json.loads(line), "sessionId": external} for line in lines]
            raw = [event for line in transcripts for event in claude_code.parse_line(json.dumps(line))]
            events = list(map(partial(normalizer.normalize, None), raw))
        else:
            path = Path(self.temp.name) / f"{external}.jsonl"
            path.write_text("\n".join(lines) + "\n")
            events = [codex.normalize_raw(event) for _, event in codex.iter_events_from_path(path)]
        for index, event in enumerate(events):
            self.repo.append_event(session_id=session, normalized=event, batch_id=batch, content_blob_id=None, session_vendor=SourceVendor(vendor), source_kind=IngestSourceKind(vendor + "_local"), external_id=f"{session}-{index}")
        assert self.repo.finish_batch(batch, polling_lease_token="fixture", status=ImportBatchStatus.COMPLETED)
        return session


def codex_lines(values: tuple[int, ...]) -> list[str]:
    lines = [json.dumps({"type": "turn_context", "payload": {"model": "codex-model"}})]
    for index, value in enumerate(values):
        lines.append(
            json.dumps(
                {
                    "timestamp": (NOW + timedelta(minutes=index)).isoformat(),
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": value, "output_tokens": value * 2, "cached_input_tokens": value // 2, "reasoning_output_tokens": value}, "last_token_usage": {"input_tokens": 1}, "model_context_window": 1000}},
                }
            )
        )
    return lines


def one(fixture: Fixture, identity: str, **kwargs: Any) -> dict[str, Any]:
    from ananta.llm.session_ledger.usage_totals import session_usage_totals

    return session_usage_totals(fixture.reader, [identity], **kwargs)["sessions"][0]


def required_error(callback: Any) -> None:
    try:
        callback()
    except ValueError as exc:
        assert "parameter_invalid" in str(exc), exc
    else:
        raise AssertionError("parameter_invalid not raised")


def usage_assertions(f: Fixture) -> None:
    lines = [claude_line(1, identity="m1"), claude_line(2, identity="m2", model="model-b", output=20, minute=1), claude_line(3, identity="m1", minute=2)]
    f.ingest("claude", lines[:2])
    result = one(f, "claude")
    check(result["by_model"]["model-a"]["output_tokens"] == 10 and result["by_model"]["model-b"]["output_tokens"] == 20, "CLAUDE_SUM_PER_MODEL")
    vector = result["by_model"]["model-a"]
    check(vector["input_tokens"] == 3 and vector["cached_input_tokens"] == 4 and vector["cache_write_input_tokens"] == 5, "CLAUDE_KEYS_MAPPED")
    f.ingest("claude", lines)
    result = one(f, "claude")
    check(result["usage_event_count"] == 2 and sum(v["output_tokens"] for v in result["by_model"].values()) == 30 and 40 / 30 > 1, "CLAUDE_USAGE_DEDUPED_BY_MESSAGE_ID")
    details_assertion(f)
    legacy_assertion(f)
    result = one(f, "claude", since=NOW + timedelta(seconds=30), until=NOW + timedelta(minutes=1))
    check(set(result["by_model"]) == {"model-b"} and result["by_model"]["model-b"]["output_tokens"] == 20, "CLAUDE_WINDOW_SUMS_BY_EVENT_AT")
    codex_assertions(f)


def details_assertion(f: Fixture) -> None:
    f.ingest("breakdown", [claude_line(1, identity="b1", output_tokens_details={"thinking_tokens": 7}, cache_creation={"ephemeral_1h_input_tokens": 3, "ephemeral_5m_input_tokens": 2}, speed="fast", service_tier="priority")])
    vector = one(f, "breakdown")["by_model"]["model-a"]
    check(
        vector["output_tokens"] == 10 and vector["reasoning_output_tokens"] == 7 and vector["breakdown"]["cache_write_1h_input_tokens"] == 3 and vector["breakdown"]["cache_write_5m_input_tokens"] == 2 and vector["breakdown"]["speed"] == {"fast": 1} and vector["breakdown"]["service_tier"] == {"priority": 1},
        "CLAUDE_BREAKDOWN_KEEPS_THINKING_AND_TTL_AND_SPEED",
    )


def legacy_assertion(f: Fixture) -> None:
    f.ingest("legacy", [claude_line(1, model=None), claude_line(2, model=None, minute=1)])
    result = one(f, "legacy")
    check(sum(v["output_tokens"] for v in result["by_model"].values()) == 20, "UNKNOWN_BUCKET_TOTAL_EQUALS_EVENT_TOTAL")
    check(
        result["by_model"].get("unknown", {}).get("output_tokens") == 20 and sum(v["output_tokens"] for v in result["by_model"].values()) == 20 and "dedupe_unavailable:2 events" in result["advisories"] and "model_unavailable:2 events; their model is not recorded" in result["advisories"],
        "DEDUPE_UNAVAILABLE_COUNTED_FOR_OLD_EVENTS",
    )


def codex_assertions(f: Fixture) -> None:
    from ananta.llm.session_ledger.usage_totals import totals_for_events

    codex_session = f.ingest("codex", codex_lines((10, 20, 35)), vendor="codex")
    result = one(f, "codex")
    pure = totals_for_events("codex", f.reader.list_session_usage_events(session_id=codex_session))
    check(result["by_model"]["codex-model"]["input_tokens"] == pure["by_model"]["codex-model"]["input_tokens"] == 35, "CODEX_LAST_TOTAL_WINS")
    result = one(f, "codex", since=NOW + timedelta(minutes=1), until=NOW + timedelta(minutes=2))
    check(result["by_model"]["codex-model"]["input_tokens"] == 25, "CODEX_WINDOW_IS_DIFFERENCE_OF_TWO_READS")
    f.ingest("decrease", codex_lines((30, 20)), vendor="codex")
    result = one(f, "decrease", since=NOW + timedelta(minutes=1))
    check(result["by_model"]["codex-model"]["input_tokens"] == -10 and any("cumulative_decreased" in a for a in result["advisories"]), "CODEX_DECREASE_REPORTED_NOT_SUMMED")
    check(one(f, "claude")["by_model"]["model-a"]["reasoning_output_tokens"] is None and one(f, "codex")["by_model"]["codex-model"]["tool_calls"] is None, "NONE_KEPT_FOR_UNREPORTED")


def enumeration_assertions(f: Fixture) -> None:
    from ananta.llm.session_ledger.usage_totals import session_usage_totals

    required_error(lambda: session_usage_totals(f.reader, vendor="claude_code", since=NOW))
    result = session_usage_totals(f.reader, vendor="claude_code", since=NOW, until=NOW + timedelta(hours=1))
    check(result["sessions"] and result["status"] == "complete", "ENUMERATES_BY_VENDOR_AND_WINDOW_WITHOUT_IDS")
    future = datetime(2027, 1, 1, tzinfo=UTC)
    for index in range(200):
        f.session("claude_code", f"e-{index:03}", when=future)
    f.session("codex", "other-vendor", when=future)
    f.session("claude_code", "earlier", when=future - timedelta(days=2))
    f.session("claude_code", "later", when=future + timedelta(days=2))
    sizes: list[int] = []
    original = f.state.query_ordered

    def bounded(namespace: str, query: dict[str, Any]) -> Any:
        sizes.append(query["limit"])
        return original(namespace, query)

    with patch.object(f.state, "query_ordered", side_effect=bounded):
        result = session_usage_totals(f.reader, vendor="claude_code", since=future, until=future)
        check(len(result["sessions"]) == 200 and result["status"] == "complete" and not result["overflow"] and max(sizes) <= 100, "ENUMERATION_EXACTLY_200_COMPLETE")
        f.session("claude_code", "e-200", when=future)
        result = session_usage_totals(f.reader, vendor="claude_code", since=future, until=future)
        check(len(result["sessions"]) == 200 and result["status"] == "unknown:bounded" and result["overflow"] and result["sessions_at_least"] == 201 and max(sizes) <= 100, "ENUMERATION_BOUNDED_UNKNOWN_BOUNDED_WITH_COUNT")


def remaining_assertions(f: Fixture) -> None:
    from ananta.llm.session_ledger import usage_totals as module

    check(module.totals_for_events("unknown_harness", [])["adapter"] is None and "unknown_harness" in str(module.totals_for_events("unknown_harness", [])["adapter_notes"]), "UNKNOWN_HARNESS_ANSWERS_ADAPTER_NULL")
    snapshot = dict(module.HARNESS_USAGE_ADAPTERS)
    try:
        module.register_usage_adapter("fixture_harness", lambda events, since, until: {"by_model": {"fixture": {"input_tokens": len(events)}}, "usage_event_count": len(events), "advisories": []})
        check(module.totals_for_events("fixture_harness", [])["adapter"] == "fixture_harness" and not any("fixture_harness" in p.read_text() for root in [ROOT / "ananta/src", *(ROOT / "plugins").glob("*/src")] for p in root.rglob("*.py")), "FIXTURE_HARNESS_REGISTERED_IN_SMOKE_WORKS")
    finally:
        module.HARNESS_USAGE_ADAPTERS.clear()
        module.HARNESS_USAGE_ADAPTERS.update(snapshot)
    bounds_poll_assertions(f)
    truncation_sibling_assertions(f)


def bounds_poll_assertions(f: Fixture) -> None:
    from ananta.llm.session_ledger import usage_totals as module

    result = module.session_usage_totals(f.reader, [f"missing-{i}" for i in range(200)])
    required_error(lambda: module.session_usage_totals(f.reader, [f"missing-{i}" for i in range(201)]))
    check(len(result["not_found"]) == 200, "BOUNDED_200_IDS_REFUSED_BY_NAME")
    check(one(f, "claude")["last_poll_at"] is not None, "LAST_POLL_AT_PRESENT_OR_NULL_WITH_ADVISORY")
    f.session("claude_code", "empty", label="empty")
    result = one(f, "empty")
    check(result["event_count"] == 0 and result["usage_event_count"] == 0 and result["meter"] == "supported" and result["last_poll_at"] is None and "last_poll_at_unavailable" in result["advisories"], "EMPTY_SESSION_ANSWERS_ZERO_EVENTS")


def truncation_sibling_assertions(f: Fixture) -> None:
    from ananta.llm.session_ledger import usage_totals as module

    f.ingest("long", [claude_line(i, identity=f"long-{i}") for i in range(101)])
    with patch.object(module, "MAX_USAGE_PAGES_PER_SESSION", 1):
        result = one(f, "long")
    check(result["status"] == "partial" and "usage_pages_truncated:1" in result["advisories"], "USAGE_PAGES_TRUNCATED_ADVISORY_NOT_REFUSAL")
    sibling_assertion(f)


def sibling_assertion(f: Fixture) -> None:
    from ananta.llm.session_ledger import usage_totals as module

    # Larger unmetered canonical and smaller metered sibling: eligibility must precede ranking.
    cloud, _ = f.session("claude_code", "siblings", kind="claude_code_cloud")
    f.state.update_state(NAMESPACE, {"table": TABLE_SESSION, "filters": {"id": cloud}}, {"event_count": 999})
    f.ingest("siblings", [claude_line(1, identity="sibling")], label="local-a")
    f.ingest("siblings", [claude_line(1, identity="sibling")], label="local-b")
    f.ingest("siblings", [claude_line(1, identity="sibling")], label="local-b")
    result = one(f, "siblings")
    listed = module.session_usage_totals(f.reader, vendor="claude_code", since=NOW, until=NOW)
    enumerated = next(row for row in listed["sessions"] if row["runtime_session_id"] == "siblings")
    f.session("claude_code", "cloud-only", kind="claude_code_cloud", label="cloud-only")
    print("SIBLING_RESULTS", result["by_model"], enumerated["by_model"])
    check(
        result["by_model"].get("model-a", {}).get("output_tokens") == 10 and enumerated["by_model"] == result["by_model"] and result["sibling_rows_resolved"] == 2 and result["meter"] == "supported" and one(f, "cloud-only")["meter"] == "unsupported" and one(f, "empty")["meter"] == "supported",
        "DEDUPE_ACROSS_SOURCES_NOT_DOUBLE_COUNTED",
    )


def usage(f: Fixture, ids: list[str], **kwargs: Any) -> dict[str, Any]:
    from ananta.llm.session_ledger.usage_totals import session_usage_totals

    return session_usage_totals(f.reader, ids, **kwargs)


def metadata(f: Fixture, table: str, identity: str, values: dict[str, Any]) -> None:
    """Private fixture preparation, not a new shipped lifecycle/clear operation."""
    result = f.state.update_state(NAMESPACE, {"table": table, "filters": {"id": identity}}, values)
    assert result["action_status"] == "completed", result  # pyright: ignore[reportTypedDictNotRequiredAccess]


def physical_events(f: Fixture, session: str) -> list[dict[str, object]]:
    """Read physical ids and payloads, independently of the usage projection.

    Sequence plus id preserves ties and makes prefix comparisons meaningful.
    A usage total alone cannot prove that replay avoided duplicate event rows.
    """
    rows: list[dict[str, object]] = []
    after: tuple[object, ...] | None = None
    while True:
        page = f.repo._query_ordered(TABLE_EVENT, filters={"session_id": session}, order_by=[["sequence", "asc"], ["id", "asc"]], limit=100, after=after)
        rows.extend(page)
        if len(page) < 100:
            return rows
        after = (page[-1]["sequence"], page[-1]["id"])


class Rollout:
    """Synthetic file -> actual plugin -> importer -> repository fixture."""

    def __init__(self, f: Fixture, *, failed_prefix: bool) -> None:
        """Start with four events persisted by the unchanged real importer.

        A malformed known record reproduces a per-session parser failure:
        the prefix commits, its event cursor does not, and discovery advances.
        A successful prefix instead supplies the append case's saved offset.
        The legacy discovery cursor is then advanced beyond this old stem.
        """
        self.f = f
        self.uuid = "00000000-0000-4000-8000-000000000001"
        root = Path(f.temp.name) / "sessions"
        root.mkdir()
        self.path = root / f"rollout-2026-10-17T00-00-00-{self.uuid}.jsonl"
        self.stem = self.path.stem
        self.source = f.repo.insert_source(source_kind=IngestSourceKind.CODEX_LOCAL, root_uri=str(root), account_label=None)
        self.header = json.dumps({"type": "session_meta", "timestamp": NOW.isoformat(), "payload": {"id": self.uuid, "cwd": "/synthetic"}})
        self.prefix = [json.dumps({"type": "response_item", "timestamp": NOW.isoformat(), "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"synthetic {i}"}]}}) for i in range(4)]
        self.write([self.header, *self.prefix, *([json.dumps({"timestamp": NOW.isoformat(), "type": "response_item", "payload": {"type": "message", "content": []}})] if failed_prefix else [])])
        self.restart()
        try:
            self.importer.poll_source(self.source)
        except LedgerPollError:
            assert failed_prefix, "only the deliberately malformed prefix may fail"
        else:
            assert not failed_prefix, "malformed known record must report a failed batch"
        rows = f.repo._query_ordered(TABLE_SESSION, filters={"source_id": self.source}, order_by=[["created_at", "asc"], ["id", "asc"]], limit=100)
        assert len(rows) == 1 and rows[0]["event_count"] == 4, "recovery fixture must start with four real prefix events"
        self.session = str(rows[0]["id"])
        self.initial = physical_events(f, self.session)
        if failed_prefix:
            assert self.cursor() is None, "failed prefix must not have a successful event cursor"
        f.repo.write_cursor(source_id=self.source, scope=CursorScope.DISCOVERY, cursor_payload={"highest_filename_stem": "rollout-9999-after-fixture"})

    def restart(self) -> None:
        """Replace both adapter and importer while keeping persisted cursors.

        The real blob adapter is present; tiny synthetic messages stay inline,
        so no blob service, live source, or external payload is needed.
        """
        package = "codex_filesystem_session_source_plugin"
        if not (ROOT / "plugins" / package / "src").is_dir():
            print(f"SKIP optional Codex ingest plugin unavailable: {package}")
            raise SystemExit(77)
        try:
            plugin_module = importlib.import_module(package + ".plugin")
        except ImportError as exc:
            raise AssertionError(f"present Codex ingest plugin failed to import: {exc}") from exc
        self.plugin = plugin_module.CodexFilesystemSessionSourcePlugin()
        self.plugin.set_ready()
        self.importer = SessionLedgerImporter(registry=SessionSourceRegistry({self.plugin.name: self.plugin}), repository=self.f.repo, blob_adapter=SessionLedgerBlobAdapter(cast(Any, SimpleNamespace())))

    def write(self, lines: list[str]) -> None:
        self.path.write_text("\n".join(lines) + "\n")

    def cursor(self) -> dict[str, Any] | None:
        return self.f.repo.read_cursor(source_id=self.source, scope=CursorScope.EVENT_READ, scope_key=self.stem)

    def append(self, lines: list[str]) -> None:
        with self.path.open("a") as stream:
            stream.write("\n".join(lines) + "\n")


def check_recovery(f: Fixture) -> None:
    """Recover the measured four-event shape through an ordinary fixture poll.

    Discovery is the first named assertion, isolating the high-water defect.
    The repaired file also crosses both previously unknown top-level records.
    Neither record contributes a meter, even with a meter-shaped payload.
    Repeat, adapter restart and a replay from zero must preserve physical ids.
    """
    r = Rollout(f, failed_prefix=True)
    tokens = codex_lines((10, 20, 35))
    record = {"type": "token_usage_record", "timestamp": NOW.isoformat(), "payload": json.loads(tokens[1])["payload"]}
    r.write([r.header, *r.prefix, json.dumps({"type": "world_state", "payload": {}}), json.dumps(record), *tokens, json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None}})])
    legacy = f.repo.read_cursor(source_id=r.source, scope=CursorScope.DISCOVERY)
    check(any(ref.external_session_id == r.stem for ref in r.plugin.discover_sessions(str(r.path.parent), legacy)), "OLD_STEM_RECOVERY")
    report = r.importer.poll_source(r.source)
    rows = physical_events(f, r.session)
    check(report.events_persisted == 3 and len(rows) == 7 and rows[:4] == r.initial, "RECOVERY_PRESERVES_PHYSICAL_PREFIX_IDS")
    check(one(f, r.uuid, vendor="codex")["usage_event_count"] == 3 and one(f, r.uuid)["event_count"] == 3, "RECOVERY_EXACT_TOKEN_COUNT_NO_SECOND_METER")
    snapshot = [(row["id"], row["external_id"]) for row in rows]
    repeated = r.importer.poll_source(r.source)
    r.restart()
    restarted = r.importer.poll_source(r.source)
    check(repeated.events_persisted == restarted.events_persisted == 0 and snapshot == [(row["id"], row["external_id"]) for row in physical_events(f, r.session)], "RECOVERY_REPLAY_AND_RESTART_IDEMPOTENT")
    # Cursor loss is fixture preparation: ordinary replay must reconverge too.
    f.repo.write_cursor(source_id=r.source, scope=CursorScope.EVENT_READ, scope_key=r.stem, cursor_payload={"line_offset": 0})
    replayed = r.importer.poll_source(r.source)
    check(replayed.events_persisted == 0 and snapshot == [(row["id"], row["external_id"]) for row in physical_events(f, r.session)], "RECOVERY_LINE_ZERO_REPLAY_STABLE")
    print("RECOVERY_COUNTS", json.dumps({"prefix": len(r.initial), "recovered": len(rows), "after_repeat_restart_replay": len(physical_events(f, r.session)), "recovery_added": report.events_persisted, "replay_added": replayed.events_persisted, "total": one(f, r.uuid)}, default=str))
    # The opted-in whole-session path reaches actual EOF on this finite file.
    # Part B supersedes the vendor fallback with its versioned sweep checkpoint.
    check(f.repo.read_cursor(source_id=r.source, scope=CursorScope.DISCOVERY) == {"version": 1, "state": "caught_up", "after": None}, "LEGACY_DISCOVERY_CURSOR_SUPERSEDED")
    lease_assertions(f, r)


def check_append(f: Fixture) -> None:
    """An old filename can grow after a successful poll and after restart.

    This case owns its successful four-event prefix and line cursor. Its
    appended records use only already-recognized types to isolate discovery.
    The final token has no new turn_context, exercising the prefix model scan.
    """
    r = Rollout(f, failed_prefix=False)
    r.append(codex_lines((10, 20)))
    report = r.importer.poll_source(r.source)
    check(report.sessions_seen == 1 and report.events_persisted == 2, "OLD_STEM_APPEND_RESTART")
    before = physical_events(f, r.session)
    saved = r.cursor()
    r.restart()
    r.append([json.dumps({**json.loads(codex_lines((35,))[1]), "timestamp": (NOW + timedelta(minutes=2)).isoformat()})])
    report = r.importer.poll_source(r.source)
    after = physical_events(f, r.session)
    check(report.events_persisted == 1 and len(after) == 7 and after[:6] == before and r.cursor() != saved, "OLD_STEM_APPENDS_AFTER_ADAPTER_RESTART")
    check(r.importer.poll_source(r.source).events_persisted == 0 and one(f, r.uuid)["by_model"]["codex-model"]["input_tokens"] == 35, "APPEND_REPEAT_USES_PREFIX_MODEL_AND_LAST_TOTAL")
    print("APPEND_COUNTS", json.dumps({"before_restart": len(before), "after_restart_append": len(after), "appended": report.events_persisted, "total": one(f, r.uuid)}, default=str))


def seed_aliases(f: Fixture) -> tuple[str, str]:
    """Build two external-id groups and six physical Codex contributors.

    History and state are deliberately larger than the two local meters.
    An empty local alias is larger still, but usage-bearing locals must win.
    Duplicate local meters represent siblings, never additive consumption.
    A Claude label with the same value is not proof of a Codex relationship.
    """
    identity = "00000000-0000-4000-8000-000000000002"
    stem = "rollout-synthetic-" + identity
    history, _ = f.session("codex", identity, kind="codex_history")
    f.session("codex", stem, kind="codex_history")
    f.session("codex", identity, kind="codex_state")
    metadata(f, TABLE_SESSION, history, {"event_count": 999})
    for label in ("local-a", "local-b"):
        f.ingest(stem, codex_lines((10, 20)), vendor="codex", label=label)
        f.session("codex", stem, label=label, vendor_label=identity)
    empty, _ = f.session("codex", "empty-" + stem, label="empty-alias", vendor_label=identity)
    metadata(f, TABLE_SESSION, empty, {"event_count": 9999})
    f.session("claude_code", "unrelated-cross-vendor", vendor_label=identity)
    return identity, stem


def check_aliases(f: Fixture) -> None:
    """Every requested alias must resolve the same complete sibling population.

    The first assertion uses normalized usage without new parser line types,
    isolating UUID lookup. The combined request then tests runtime grouping.
    Stem-only closure must find the UUID group, including its history rows.
    """
    identity, stem = seed_aliases(f)
    bare = usage(f, [identity], vendor="codex")
    check(len(bare["sessions"]) == 1 and bare["sessions"][0]["meter"] == "supported" and bare["sessions"][0]["by_model"].get("codex-model", {}).get("input_tokens") == 20, "UUID_STEM_ONE_TOTAL")
    for aliases in ([stem], [identity], [identity, stem]):
        result = usage(f, aliases, vendor="codex")
        check(len(result["sessions"]) == 1 and not result["not_found"], "CODEX_ALIASES_GROUP_ONCE")
        row = result["sessions"][0]
        check(row["runtime_session_id"] == identity and row["sibling_rows_resolved"] == 5 and row["by_model"] == bare["sessions"][0]["by_model"], "CODEX_ALIAS_CLOSURE_BEFORE_RANKING")
    print("ALIAS_TOTAL", json.dumps(bare, default=str))
    check(len(usage(f, [stem])["sessions"]) == 1, "CROSS_VENDOR_LABELS_NEVER_JOIN")


def check_all_counts(f: Fixture) -> None:
    """Count a multi-page rollout without adding its cumulative snapshots.

    Equal timestamps force the id tie-break across the 100-row page boundary.
    A separate chronological rollout supplies an exact pre-window baseline.
    Capping that walk retains observed counts and discloses partial coverage.
    Query interception observes page sizes while forwarding every real read.
    """
    from ananta.llm.session_ledger import usage_totals as module

    lines = codex_lines(tuple(range(1, 122)))
    same_time = [lines[0], *(json.dumps({**json.loads(line), "timestamp": NOW.isoformat()}) for line in lines[1:])]
    session = f.ingest("all-counts", same_time, vendor="codex")
    sizes: list[int] = []
    original = f.state.query_ordered

    def bounded(namespace: str, query: dict[str, Any]) -> Any:
        sizes.append(query["limit"])
        return original(namespace, query)

    with patch.object(f.state, "query_ordered", side_effect=bounded):
        result = one(f, "all-counts")
    check(result["event_count"] == result["usage_event_count"] == 121 and result["by_model"]["codex-model"]["input_tokens"] == 121 and max(sizes) <= 100, "CODEX_ALL_USAGE_EVENTS_COUNTED")
    print("ALL_COUNTS_TOTAL", json.dumps(result, default=str))
    check(len(physical_events(f, session)) == 121, "SAME_TIMESTAMP_USAGE_KEYSET_COMPLETE")
    f.ingest("window-counts", lines, vendor="codex")
    result = one(f, "window-counts", since=NOW + timedelta(minutes=60), until=NOW + timedelta(minutes=90))
    check(result["event_count"] == result["usage_event_count"] == 31 and result["by_model"]["codex-model"]["input_tokens"] == 31, "CODEX_COUNTS_AND_PRE_WINDOW_BASELINE")
    with patch.object(module, "MAX_USAGE_PAGES_PER_SESSION", 1):
        result = one(f, "window-counts")
    check(result["status"] == "partial" and result["usage_event_count"] == result["event_count"] == 100 and "usage_pages_truncated:1" in result["advisories"], "CODEX_CAPPED_COUNTS_ARE_OBSERVED_PARTIAL")


def seed_enum(f: Fixture, count: int) -> None:
    """Each runtime has separate local-stem and history-UUID canonicals.

    Their creation times tie, as do their event bounds; enumeration must page
    physical rows yet apply its limit to resolved runtimes before truncating.
    No event ingestion is needed to isolate the enumeration identity contract.
    """
    for index in range(count):
        identity = f"00000000-0000-4000-8000-{index:012d}"
        f.session("codex", identity, kind="codex_history")
        f.session("codex", "rollout-enum-" + identity, vendor_label=identity)


def enumerate_codex(f: Fixture) -> dict[str, Any]:
    from ananta.llm.session_ledger.usage_totals import session_usage_totals

    return session_usage_totals(f.reader, vendor="codex", since=NOW, until=NOW)


def check_enum_200(f: Fixture) -> None:
    """Four hundred canonical aliases describe exactly two hundred runtimes.

    The uncapped walk must complete; one page cannot prove corpus exhaustion.
    The capped response's lower bound is only its observed distinct runtimes,
    not a synthetic 201 sentinel inferred from the number of physical rows.
    """
    from ananta.llm.session_ledger import usage_totals as module

    seed_enum(f, 200)
    result = enumerate_codex(f)
    check(len(result["sessions"]) == 200 and result["status"] == "complete" and not result["overflow"] and result["sessions_at_least"] == 200, "CODEX_ENUM_200_ALIASES")
    check(len({row["runtime_session_id"] for row in result["sessions"]}) == 200 and all(row["meter"] == "supported" for row in result["sessions"]), "CODEX_ENUM_ALIASES_NOT_ADDITIVE")
    with patch.object(module, "MAX_ENUMERATION_PAGES", 1):
        result = enumerate_codex(f)
    check(result["status"] == "partial" and not result["overflow"] and result["sessions_at_least"] == len(result["sessions"]) <= 100 and "enumeration_pages_truncated:1" in result["advisories"], "CODEX_ENUM_CAP_LOWER_BOUND_ONLY")


def check_enum_201(f: Fixture) -> None:
    """Only a distinct 201st runtime proves overflow beyond the response cap.

    The returned list remains bounded at 200; its lower bound is not claimed
    to be the exact corpus size. This case has its own private fixture state.
    """
    seed_enum(f, 201)
    result = enumerate_codex(f)
    check(result["status"] == "unknown:bounded" and result["overflow"] and result["sessions_at_least"] == 201 and len(result["sessions"]) == 200, "CODEX_ENUM_201_RUNTIMES")


def check_alias_lifecycle(f: Fixture) -> None:
    """Derived aliases track current metadata and existing row eligibility.

    Non-null label correction uses the real upsert; None preserves the label.
    Clearing uses the real operator service and retains an immutable receipt.
    Source/session exclusion removes proof and restoration revives the alias.
    Current and historical reads must retain all original event history.
    """
    old, new = "00000000-0000-4000-8000-000000000003", "00000000-0000-4000-8000-000000000004"
    session = f.ingest("rollout-lifecycle", codex_lines((10, 20)), vendor="codex")
    _, source = f.session("codex", "rollout-lifecycle", vendor_label=old)
    check(one(f, old)["usage_event_count"] == 2, "CODEX_ALIAS_CREATED_FROM_METADATA")
    f.session("codex", "rollout-lifecycle", vendor_label=new)
    f.session("codex", "rollout-lifecycle", vendor_label=None)
    check(not usage(f, [old])["sessions"] and one(f, new)["runtime_session_id"] == new, "LABEL_CORRECTION_AND_NONE_PRESERVES")
    check(one(f, new, since=NOW, until=NOW)["by_model"]["codex-model"]["input_tokens"] == 10, "CORRECTED_ALIAS_HISTORICAL_WINDOW")
    snapshot = physical_events(f, session)
    from ananta.core.services.call_context import CallContext
    from ananta.services.session_ledger_service.service import SessionLedgerService

    service = SessionLedgerService(state_service=f.state, blob_storage_service=cast(Any, SimpleNamespace()), plugin_manager=cast(Any, SimpleNamespace(plugins={})))  # pyright: ignore[reportArgumentType]
    service._repository = f.repo
    preview = service.clear_session_label(session, call_context=CallContext.for_operator())
    cleared = service.clear_session_label(session, preview["expected_updated_at"], True, call_context=CallContext.for_operator())
    check(cleared["status"] == "cleared", "REAL_SERVICE_ALIAS_LABEL_CLEAR")
    unresolved = one(f, "rollout-lifecycle")
    check(not usage(f, [new])["sessions"] and unresolved["runtime_session_id"] == "rollout-lifecycle" and any("runtime_identity_unavailable" in a for a in unresolved["advisories"]), "CLEARED_FIXTURE_LABEL_KEEPS_EXACT_ID_ADVISORY")
    f.session("codex", "rollout-lifecycle", vendor_label=new)
    metadata(f, TABLE_SESSION, session, {"is_deleted": 1})
    check(not usage(f, [new])["sessions"], "DELETED_PROVING_SESSION_REMOVES_ALIAS")
    metadata(f, TABLE_SESSION, session, {"is_deleted": 0})
    metadata(f, TABLE_SOURCE, source, {"is_deleted": 1})
    check(not usage(f, [new])["sessions"] and one(f, "rollout-lifecycle")["runtime_session_id"] == "rollout-lifecycle", "EXCLUDED_PROVING_SOURCE_REMOVES_ALIAS")
    metadata(f, TABLE_SOURCE, source, {"is_deleted": 0})
    check(one(f, new)["usage_event_count"] == 2 and physical_events(f, session) == snapshot, "ALIAS_RESTORED_WITHOUT_DELETING_EVENT_HISTORY")


def lease_assertions(f: Fixture, r: Rollout) -> None:
    """Preserve the existing owner fence while recovery reopens normal polls.

    Private metadata preparation installs a successor token; the stale
    heartbeat must fail before the pulling pass can advance either cursor.
    A stale finish cannot finalize the successor, and completed is terminal.
    """
    from ananta.llm.session_ledger.lease import LeaseHeartbeat
    from ananta.llm.session_ledger.repository import LeaseLostError

    handle = f.repo.try_acquire_polling_lease(r.source)
    assert handle is not None, "fixture lease acquisition"
    metadata(f, TABLE_SOURCE, r.source, {"polling_lease_token": "successor"})
    batch = f.repo.start_batch(r.source, polling_lease_token="successor")
    heartbeat = LeaseHeartbeat(f.repo, handle, 600)
    heartbeat._last_refresh_at = datetime.now(UTC) - timedelta(seconds=601)
    source = f.repo.get_source(r.source)
    assert source is not None
    before = r.cursor()
    try:
        r.importer._run_pulling_pass(source_row=source, plugin=r.plugin, batch_id=batch, heartbeat=heartbeat)
    except LeaseLostError:
        check(r.cursor() == before, "STALE_LEASE_CANNOT_ADVANCE_CURSOR")
    else:
        raise AssertionError("STALE_LEASE_CANNOT_ADVANCE_CURSOR: stale actor reached writes")
    check(not f.repo.finish_batch(batch, polling_lease_token=handle.lease_token, status=ImportBatchStatus.COMPLETED), "STALE_LEASE_CANNOT_FINALIZE_SUCCESSOR")
    check(f.repo.finish_batch(batch, polling_lease_token="successor", status=ImportBatchStatus.COMPLETED) and not f.repo.finish_batch(batch, polling_lease_token="successor", status=ImportBatchStatus.RUNNING), "COMPLETED_BATCH_IS_TERMINAL")


class ClaudeUnknownRollout:
    """Private-PG fixture using the actual filesystem adapter and importer."""

    def __init__(self, f: Fixture) -> None:
        module = importlib.import_module("claude_code_filesystem_session_source_plugin.plugin")
        self.plugin = module.ClaudeCodeFilesystemSessionSourcePlugin()
        self.plugin.set_config_provider(SimpleNamespace(get=lambda name, default=None: "*.jsonl" if name == "glob" else default))
        self.plugin.prepare_for_readiness()
        self.f = f
        self.root = Path(f.temp.name) / "claude-unknown"
        self.project = self.root / "synthetic-project"
        self.project.mkdir(parents=True)
        self.path = self.project / "claude-unknown-fixture.jsonl"
        self.source = f.repo.insert_source(source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL, root_uri=str(self.root), account_label=None)
        self.importer = SessionLedgerImporter(registry=SessionSourceRegistry({self.plugin.name: self.plugin}), repository=f.repo, blob_adapter=SessionLedgerBlobAdapter(cast(Any, SimpleNamespace())))

    def append(self, lines: list[str]) -> None:
        prior = self.path.stat().st_mtime_ns if self.path.exists() else 0
        with self.path.open("a") as stream:
            stream.write("\n".join(lines) + "\n")
        # Synthetic file eligibility is explicit, never a live cursor reset.
        current = self.path.stat()
        os.utime(self.path, ns=(current.st_atime_ns, max(current.st_mtime_ns, prior + 1_000_000_000)))

    def cursor(self) -> dict[str, object] | None:
        return self.f.repo.read_cursor(source_id=self.source, scope=CursorScope.EVENT_READ, scope_key=self.path.stem)

    def sessions(self) -> list[dict[str, Any]]:
        return self.f.repo._query_ordered(TABLE_SESSION, filters={"source_id": self.source}, order_by=[["created_at", "asc"], ["id", "asc"]], limit=100)


def check_claude_unknown(f: Fixture) -> None:
    """Known prefix/suffix usage survives drift; unknown tails checkpoint exactly."""
    from ananta.llm.session_ledger.types import UnknownLineAdvisory

    r = ClaudeUnknownRollout(f)
    unknown = json.dumps({"type": "synthetic-unknown-line-type", "message": {"role": "assistant", "content": "PRIVATE_PAYLOAD"}})
    known = [{**json.loads(claude_line(index, identity=f"claude-{index}")), "sessionId": r.path.stem} for index in (1, 2)]
    r.append([json.dumps(known[0]), unknown, json.dumps(known[1]), unknown])
    report = r.importer.poll_source(r.source)
    check(report.events_persisted == 2 and report.batches_failed == 0, "CLAUDE_SUFFIX_PERSISTS_WITHOUT_FAILURE")
    check(report.unknown_line_advisories == (UnknownLineAdvisory(r.source, r.path.stem, "synthetic-unknown-line-type", 1),), "CLAUDE_ONE_ADVISORY_PER_SESSION_PARSER")
    total = one(f, r.path.stem, vendor="claude_code")
    check(total["usage_event_count"] == 2 and total["by_model"]["model-a"]["input_tokens"] == 6 and total["by_model"]["model-a"]["output_tokens"] == 20, "CLAUDE_SUFFIX_EXACT_USAGE_IDENTITY")
    check(r.cursor() is not None and r.cursor()["byte_offset"] == r.path.stat().st_size, "CLAUDE_TRAILING_UNKNOWN_COMPLETE_CURSOR")  # pyright: ignore[reportOptionalSubscript]
    sessions = r.sessions()
    check(len(sessions) == 1 and len(physical_events(f, str(sessions[0]["id"]))) == 2, "CLAUDE_NO_UNKNOWN_LEDGER_EVENT")
    snapshot = physical_events(f, str(sessions[0]["id"]))
    check_claude_reread(f, r, unknown, sessions, snapshot, total)


def check_claude_reread(f: Fixture, r: ClaudeUnknownRollout, unknown: str, sessions: list[dict[str, Any]], snapshot: list[dict[str, object]], total: dict[str, Any]) -> None:
    """An eligible append resumes after unknown input without repeating usage."""
    r.append([unknown])
    tail = r.importer.poll_source(r.source)
    check(tail.events_persisted == 0 and tail.unknown_line_advisories == () and r.cursor()["byte_offset"] == r.path.stat().st_size, "CLAUDE_UNKNOWN_ONLY_TAIL_ADVANCES")  # pyright: ignore[reportOptionalSubscript]
    r.append([json.dumps({"type": "summary"})])
    reread = r.importer.poll_source(r.source)
    check(reread.sessions_seen == 1 and reread.events_persisted == 0 and reread.unknown_line_advisories == () and reread.batches_failed == 0, "CLAUDE_ELIGIBLE_REREAD_IDEMPOTENT")
    check(physical_events(f, str(sessions[0]["id"])) == snapshot and one(f, r.path.stem)["by_model"] == total["by_model"], "CLAUDE_ELIGIBLE_USAGE_UNCHANGED")


def check_claude_unknown_only(f: Fixture) -> None:
    """An all-unknown complete file advances without fabricating a session row."""
    from ananta.llm.session_ledger.types import UnknownLineAdvisory

    r = ClaudeUnknownRollout(f)
    r.append([json.dumps({"type": "future-marker"}), json.dumps({"type": "future-marker"})])
    report = r.importer.poll_source(r.source)
    check(report.events_persisted == 0 and report.batches_failed == 0 and r.sessions() == [], "CLAUDE_UNKNOWN_ONLY_NO_FABRICATED_ROWS")
    check(report.unknown_line_advisories == (UnknownLineAdvisory(r.source, r.path.stem, "future-marker", 1),), "CLAUDE_UNKNOWN_ONLY_GROUP")
    check(r.cursor() is not None and r.cursor()["byte_offset"] == r.path.stat().st_size, "CLAUDE_ALL_UNKNOWN_CURSOR")  # pyright: ignore[reportOptionalSubscript]


CASES: dict[str, Callable[[Fixture], None]] = {
    "CLAUDE_UNKNOWN_SUFFIX": check_claude_unknown,
    "CLAUDE_UNKNOWN_ONLY": check_claude_unknown_only,
    "OLD_STEM_RECOVERY": check_recovery,
    "OLD_STEM_APPEND_RESTART": check_append,
    "UUID_STEM_ONE_TOTAL": check_aliases,
    "CODEX_ALL_USAGE_EVENTS_COUNTED": check_all_counts,
    "CODEX_ENUM_200_ALIASES": check_enum_200,
    "CODEX_ENUM_201_RUNTIMES": check_enum_201,
    "CODEX_ALIAS_LIFECYCLE": check_alias_lifecycle,
}


def run_case(case: Callable[[Fixture], None]) -> None:
    """Each selectable regression owns a schema, connection pool and files.

    Default execution invokes the legacy suite once and each new case once;
    selected execution invokes exactly its named case, never legacy setup.
    """
    fixture = Fixture()
    try:
        case(fixture)
    finally:
        fixture.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES)
    selected = parser.parse_args().case
    if selected is not None:
        run_case(CASES[selected])
        return
    check(hasattr(SessionLedgerReadAPI, "session_usage_totals") and hasattr(SessionLedgerReadAPI.session_usage_totals, "_service_interface_metadata"), "VERB_DECLARED_ON_READ_ABC")
    submit(SessionLedgerReadAPI.session_usage_totals, "service_interface::session_ledger_service::session_usage_totals", {"runtime_session_ids": ["fixture"], "since": NOW.isoformat(), "until": NOW.isoformat(), "vendor": "claude_code"})
    check(True, "BRIDGE_ACCEPTS_SESSION_USAGE_TOTALS_ARGUMENTS")
    fixture = Fixture()
    try:
        usage_assertions(fixture)
        enumeration_assertions(fixture)
        remaining_assertions(fixture)
    finally:
        fixture.close()
    for case in CASES.values():
        run_case(case)
    print("PASS session_usage_totals_smoke whole module; zero skips")


if __name__ == "__main__":
    main()
