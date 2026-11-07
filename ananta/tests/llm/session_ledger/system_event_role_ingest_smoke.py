#!/usr/bin/env python3
"""Prove SYSTEM normalizers satisfy the real session-ledger ingest contract.

The PostgreSQL cases run only under ``scripts/provision_test_pg.py --exec``.
The fixture lines are synthetic; the repository and its StateManagementInterface
backing are real.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import shlex
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_SOURCE_ROOTS = sorted((REPO_ROOT / "plugins").glob("*_session_source_plugin/src"))
sys.path[:0] = [
    str(REPO_ROOT / "ananta/src"),
    str(REPO_ROOT / "plugins/postgres_state_management_plugin/src"),
    *(str(path) for path in PLUGIN_SOURCE_ROOTS),
]

from ananta.core.config.config_manager import initialize_config  # noqa: E402
from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.interfaces.vault_service_interface import VaultServiceInterface  # noqa: E402
from ananta.llm.session_ledger.ingest import _EVENT_SHAPE_VALIDATORS  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import get_session_ledger_schema  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
    SourceVendor,
)
from ananta.llm.session_ledger.vendor import claude_code, claude_code_tasks, codex, codex_ambient, codex_cloud, codex_goals, codex_state  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402
from postgres_state_management_plugin.plugin import (  # noqa: E402
    PostgresStatePlugin,
)

TOKEN_COUNT_ASSERTION = "SYSTEM_TOKEN_COUNT_INGESTS_THROUGH_REAL_PATH"
STATE_ASSERTION = "CODEX_STATE_SYSTEM_INGESTS"
GOALS_ASSERTION = "CODEX_GOALS_SYSTEM_INGESTS"
CENSUS_ASSERTION = "SYSTEM_PRODUCER_CENSUS_ALL_ACCEPTED"
SYSTEM_ROLE_ERROR = "SYSTEM events require role='system'"
FIXTURE_EVENT_AT = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)

SYSTEM_PRODUCER_IDS = tuple(
    sorted(
        (
            "ananta/src/ananta/llm/session_ledger/vendor/codex.py::_normalize_token_count",
            "ananta/src/ananta/llm/session_ledger/vendor/codex.py::_normalize_reasoning",
            "ananta/src/ananta/llm/session_ledger/vendor/codex_cloud.py::_normalize_turn_error",
            "ananta/src/ananta/llm/session_ledger/vendor/codex_state.py::normalize_raw",
            "ananta/src/ananta/llm/session_ledger/vendor/codex_goals.py::normalize_raw",
            "plugins/agent_messaging_session_source_plugin/src/agent_messaging_session_source_plugin/plugin.py::AgentMessagingSessionSourcePlugin.normalize",
            "plugins/claude_code_pushed_session_source_plugin/src/claude_code_pushed_session_source_plugin/plugin.py::ClaudeCodePushedSessionSourcePlugin.normalize",
            "plugins/codex_ambient_session_source_plugin/src/codex_ambient_session_source_plugin/plugin.py::CodexAmbientSessionSourcePlugin.normalize",
            "plugins/claude_code_filesystem_session_source_plugin/src/claude_code_filesystem_session_source_plugin/plugin.py::ClaudeCodeFilesystemSessionSourcePlugin.normalize",
            "plugins/claude_code_tasks_session_source_plugin/src/claude_code_tasks_session_source_plugin/plugin.py::ClaudeCodeTasksSessionSourcePlugin.normalize",
            "plugins/claude_code_cloud_session_source_plugin/src/claude_code_cloud_session_source_plugin/plugin.py::_normalize_system",
        )
    )
)

OPTIONAL_SOURCE_PLUGIN_FIXTURES: tuple[tuple[str, str, str, str, str, dict[str, Any]], ...] = (
    (
        "plugins/agent_messaging_session_source_plugin/src/agent_messaging_session_source_plugin/plugin.py::AgentMessagingSessionSourcePlugin.normalize",
        "agent_messaging_session_source_plugin",
        "agent_messaging_session_source_plugin.plugin",
        "AgentMessagingSessionSourcePlugin",
        "synthetic-agent-message",
        {
            "kind": "error",
            "role": "system",
            "content": [{"type": "text", "text": "synthetic"}],
            "error": {"code": "fixture_error"},
        },
    ),
    (
        "plugins/claude_code_pushed_session_source_plugin/src/claude_code_pushed_session_source_plugin/plugin.py::ClaudeCodePushedSessionSourcePlugin.normalize",
        "claude_code_pushed_session_source_plugin",
        "claude_code_pushed_session_source_plugin.plugin",
        "ClaudeCodePushedSessionSourcePlugin",
        "synthetic-claude-pushed",
        {
            "kind": claude_code.PAYLOAD_KIND_SYSTEM,
            "role": "system",
            "subtype": "fixture",
            "text": "synthetic",
        },
    ),
    (
        "plugins/codex_ambient_session_source_plugin/src/codex_ambient_session_source_plugin/plugin.py::CodexAmbientSessionSourcePlugin.normalize",
        "codex_ambient_session_source_plugin",
        "codex_ambient_session_source_plugin.plugin",
        "CodexAmbientSessionSourcePlugin",
        "synthetic-codex-ambient",
        {
            "kind": codex_ambient.PAYLOAD_KIND_SUGGESTION,
            "id": "synthetic-suggestion",
            "title": "synthetic",
            "description": "synthetic",
            "prompt": "synthetic",
            "appIds": [],
            "status": "active",
            "createdAtMs": 1,
            "updatedAtMs": 1,
            "is_current": True,
        },
    ),
    (
        "plugins/claude_code_filesystem_session_source_plugin/src/claude_code_filesystem_session_source_plugin/plugin.py::ClaudeCodeFilesystemSessionSourcePlugin.normalize",
        "claude_code_filesystem_session_source_plugin",
        "claude_code_filesystem_session_source_plugin.plugin",
        "ClaudeCodeFilesystemSessionSourcePlugin",
        "synthetic-claude-filesystem",
        {
            "kind": claude_code.PAYLOAD_KIND_SYSTEM,
            "role": "system",
            "subtype": "fixture",
            "text": "synthetic",
        },
    ),
    (
        "plugins/claude_code_tasks_session_source_plugin/src/claude_code_tasks_session_source_plugin/plugin.py::ClaudeCodeTasksSessionSourcePlugin.normalize",
        "claude_code_tasks_session_source_plugin",
        "claude_code_tasks_session_source_plugin.plugin",
        "ClaudeCodeTasksSessionSourcePlugin",
        "synthetic-claude-task",
        {
            "kind": claude_code_tasks.PAYLOAD_KIND_TASK,
            "task_id": "synthetic-task",
            "subject": "synthetic",
            "description": "synthetic",
            "activeForm": "synthetic",
            "status": "pending",
            "blocks": [],
            "blockedBy": [],
        },
    ),
    (
        "plugins/claude_code_cloud_session_source_plugin/src/claude_code_cloud_session_source_plugin/plugin.py::_normalize_system",
        "claude_code_cloud_session_source_plugin",
        "claude_code_cloud_session_source_plugin.plugin",
        "ClaudeCodeCloudSessionSourcePlugin",
        "synthetic-claude-cloud",
        {"kind": claude_code.PAYLOAD_KIND_SYSTEM, "subtype": "fixture", "text": "synthetic"},
    ),
)


class _FixtureVault:
    """Return the private fixture's trust-auth password through the plugin seam."""

    def __init__(self, password: str) -> None:
        self._password = password

    def retrieve(self, key: str) -> ActionResult:
        del key
        return {
            "action_status": "completed",
            "data": {"value": self._password},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }


class _FixturePluginManager:
    """Expose the real state plugin to StateService without starting the application."""

    def __init__(self, plugin: PostgresStatePlugin) -> None:
        self._plugin = plugin

    def get_plugin(self, plugin_name: str) -> object:
        if plugin_name != self._plugin.name:
            raise AssertionError(f"unexpected state plugin lookup: {plugin_name}")
        return self._plugin

    def execute_action(
        self,
        plugin_name: str,
        params: dict[str, object],
        state: dict[str, object],
        APP_HOME: str,
        plugin_config: dict[str, object],
    ) -> dict[str, object]:
        del plugin_name, params, state, APP_HOME, plugin_config
        raise AssertionError("the StateService fixture must not dispatch plugin actions")


@contextmanager
def _running_private_state_plugin(
    raw_app_home: str,
    address: dict[str, str],
) -> Generator[PostgresStatePlugin]:
    """Run the native PostgreSQL plugin against the provisioned private DB."""
    plugin = PostgresStatePlugin()
    config = initialize_config(raw_app_home)
    saved = config.save_plugin_config(
        plugin.name,
        {
            "host": address["host"],
            "port": int(address["port"]),
            "user": address["user"],
            "database": address["dbname"],
            "pg_schema": f"um05_system_role_{uuid4().hex[:12]}",
            "pool_size": 2,
            "connection_timeout": 10,
        },
    )
    if not saved:
        raise RuntimeError("could not save the private state-plugin fixture config")
    plugin.set_vault_service(cast("VaultServiceInterface", _FixtureVault(address.get("password", ""))))
    services_started = False
    try:
        plugin.prepare_for_readiness()
        result = asyncio.run(plugin.start_services())
        if result.get("action_status") != "completed":
            raise RuntimeError(f"private state plugin failed to start: {result!r}")
        services_started = True
        yield plugin
    finally:
        if services_started:
            stopped = asyncio.run(plugin.stop_services())
            if stopped.get("action_status") != "completed":
                raise RuntimeError(f"private state plugin failed to stop: {stopped!r}")


def _private_database_address() -> dict[str, str]:
    dsn = os.environ.get("PROJECT_SOLET_DESK_TEST_DSN")
    if not dsn:
        raise RuntimeError("PROJECT_SOLET_DESK_TEST_DSN is absent")
    address = dict(part.split("=", 1) for part in shlex.split(dsn) if "=" in part)
    if address.get("host") != "127.0.0.1" or address.get("dbname") != "project_solet_desk_test" or int(address.get("port", "5432")) == 5432:
        raise RuntimeError("private PostgreSQL DSN failed its host, database, or port guard")
    return address


@contextmanager
def _private_ledger() -> Generator[SessionLedgerRepository]:
    """Build the native StateService/schema lifecycle on the provisioned private DB."""
    address = _private_database_address()
    with tempfile.TemporaryDirectory(prefix="um05-system-role-app-") as raw_app_home:
        with _running_private_state_plugin(raw_app_home, address) as plugin:
            state_service = StateService(
                _FixturePluginManager(plugin),
                app_home=raw_app_home,
                state_plugin_name=plugin.name,
            )
            schema_manager = SchemaManager(state_service, plugin)
            schema_manager.initialize_schemas([get_session_ledger_schema()])
            state_service.set_schema_manager(schema_manager)
            yield SessionLedgerRepository(cast("StateManagementInterface", state_service))


def _raw_event(
    external_session_id: str,
    payload: dict[str, Any],
    *,
    vendor_event_id: str | None = None,
) -> RawSessionEvent:
    return RawSessionEvent(
        external_session_id=external_session_id,
        payload=payload,
        event_at=FIXTURE_EVENT_AT,
        vendor_event_id=vendor_event_id or f"synthetic-{uuid4().hex}",
        vendor_parent_event_id=None,
    )


def _token_count_event(codex_module: ModuleType | None = None) -> NormalizedSessionEvent:
    normalizer_module = codex if codex_module is None else codex_module
    line = {
        "type": "event_msg",
        "timestamp": FIXTURE_EVENT_AT.isoformat(),
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": 7, "output_tokens": 3},
                "last_token_usage": {"input_tokens": 2, "output_tokens": 1},
                "model_context_window": 128000,
            },
        },
    }
    with tempfile.TemporaryDirectory(prefix="um05-token-count-") as scratch:
        rollout = Path(scratch) / "synthetic-rollout.jsonl"
        rollout.write_text(json.dumps(line) + "\n", encoding="utf-8")
        parsed = list(normalizer_module.iter_events_from_path(rollout))
    if len(parsed) != 1:
        raise AssertionError(f"token_count parser emitted {len(parsed)} events, expected one")
    return normalizer_module.normalize_raw(parsed[0][1])


def _state_event() -> NormalizedSessionEvent:
    return codex_state.normalize_raw(
        _raw_event(
            "synthetic-codex-state",
            {"subtype": "codex_state", "thread_id": "synthetic-thread"},
        )
    )


def _goals_event(goals_module: ModuleType | None = None) -> NormalizedSessionEvent:
    normalizer_module = codex_goals if goals_module is None else goals_module
    return normalizer_module.normalize_raw(
        _raw_event(
            "synthetic-codex-goals",
            {"subtype": "codex_goal", "goal_id": "synthetic-goal", "status": "active"},
        )
    )


def _system_event_fixtures(scan_root: Path) -> list[tuple[str, NormalizedSessionEvent]]:
    """Return one synthetic output from each of the eleven real SYSTEM normalizers."""
    cloud_chunk = json.dumps(
        {
            "external_session_id": "synthetic-codex-cloud",
            "task_event_at": FIXTURE_EVENT_AT.isoformat(),
            "task_details": {"current_assistant_turn": {"error": {"code": "fixture_error", "message": "synthetic"}}},
        }
    )
    cloud_raw = list(codex_cloud.parse_chunk(cloud_chunk))
    if len(cloud_raw) != 1:
        raise AssertionError(f"Codex Cloud parser emitted {len(cloud_raw)} error events")

    token = _token_count_event()
    reasoning = codex.normalize_raw(
        _raw_event(
            "synthetic-codex-reasoning",
            {
                "_codex_item_type": "reasoning",
                "summary": [{"type": "summary_text", "text": "synthetic"}],
            },
        )
    )
    cloud_error = codex_cloud.normalize_raw(cloud_raw[0])
    state = _state_event()
    goals = _goals_event()
    core_fixtures = [
        (
            "ananta/src/ananta/llm/session_ledger/vendor/codex.py::_normalize_token_count",
            token,
        ),
        ("ananta/src/ananta/llm/session_ledger/vendor/codex.py::_normalize_reasoning", reasoning),
        ("ananta/src/ananta/llm/session_ledger/vendor/codex_cloud.py::_normalize_turn_error", cloud_error),
        ("ananta/src/ananta/llm/session_ledger/vendor/codex_state.py::normalize_raw", state),
        ("ananta/src/ananta/llm/session_ledger/vendor/codex_goals.py::normalize_raw", goals),
    ]

    return core_fixtures + _optional_source_plugin_fixtures(scan_root)


def _optional_source_plugin_fixtures(
    scan_root: Path,
) -> list[tuple[str, NormalizedSessionEvent]]:
    fixtures: list[tuple[str, NormalizedSessionEvent]] = []
    for identity, package, module_name, class_name, session_id, payload in OPTIONAL_SOURCE_PLUGIN_FIXTURES:
        source_root = scan_root / "plugins" / package / "src"
        if not source_root.is_dir():
            print(f"UNAVAILABLE_IN_PROFILE: {identity}")
            continue
        try:
            plugin_module = importlib.import_module(module_name)
        except ImportError as error:
            raise AssertionError(f"present plugin source failed to import: {identity}: {error}") from error
        plugin_class = cast("type[Any]", getattr(plugin_module, class_name))
        normalized = cast(
            "NormalizedSessionEvent",
            plugin_class().normalize(_raw_event(session_id, payload)),
        )
        fixtures.append((identity, normalized))
    return fixtures


def _system_producer_inventory(scan_root: Path) -> tuple[str, ...]:
    """Inventory SYSTEM constructors from production source using Python syntax."""
    roots = [scan_root / "ananta/src", *sorted((scan_root / "plugins").glob("*/src"))]
    found: list[str] = []
    for source_root in roots:
        if not source_root.is_dir():
            continue
        for source_path in sorted(source_root.rglob("*.py")):
            relative = source_path.relative_to(scan_root)
            if "tests" in relative.parts:
                continue
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

            def walk(
                node: ast.AST,
                scope: tuple[str, ...] = (),
                source_relative: str = relative.as_posix(),
            ) -> None:
                if isinstance(node, ast.ClassDef):
                    scope = (*scope, node.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = (*scope, node.name)
                if isinstance(node, ast.Call) and (isinstance(node.func, ast.Name) and node.func.id == "NormalizedSessionEvent" or isinstance(node.func, ast.Attribute) and node.func.attr == "NormalizedSessionEvent"):
                    for keyword in node.keywords:
                        if keyword.arg == "event_type" and isinstance(keyword.value, ast.Attribute) and keyword.value.attr == "SYSTEM" and isinstance(keyword.value.value, ast.Name) and keyword.value.value.id == "EventType":
                            found.append(f"{source_relative}::{'.'.join(scope)}")
                for child in ast.iter_child_nodes(node):
                    walk(child, scope, source_relative)

            walk(tree)
    return tuple(sorted(found))


def _assert_census_inventory(scan_root: Path) -> None:
    actual = _system_producer_inventory(scan_root)
    expected = _expected_system_producer_ids(scan_root)
    assert actual == expected, f"{CENSUS_ASSERTION}: expected registered SYSTEM call sites; missing={sorted(set(expected) - set(actual))}; extra={sorted(set(actual) - set(expected))}; actual_count={len(actual)}"


def _expected_system_producer_ids(scan_root: Path) -> tuple[str, ...]:
    expected: list[str] = []
    for identity in SYSTEM_PRODUCER_IDS:
        source_path = Path(identity.split("::", maxsplit=1)[0])
        if source_path.parts[0] != "plugins" or scan_root.joinpath(*source_path.parts[:3]).is_dir():
            expected.append(identity)
    return tuple(sorted(expected))


def _assert_system_producer_census(scan_root: Path) -> None:
    _assert_census_inventory(scan_root)
    fixtures = _system_event_fixtures(scan_root)
    fixture_ids = tuple(sorted(identity for identity, _event in fixtures))
    expected = _expected_system_producer_ids(scan_root)
    assert fixture_ids == expected, f"{CENSUS_ASSERTION}: fixture inventory differs from the AST census"
    validator = _EVENT_SHAPE_VALIDATORS[EventType.SYSTEM]
    for identity, event in fixtures:
        if event.event_type is not EventType.SYSTEM:
            raise AssertionError(f"{CENSUS_ASSERTION}: {identity} emitted {event.event_type}")
        try:
            validator(event)
        except ValueError as error:
            raise AssertionError(f"{CENSUS_ASSERTION}: {identity}: {error}") from error


def _seed_and_append(
    repository: SessionLedgerRepository,
    normalized: NormalizedSessionEvent,
    source_kind: IngestSourceKind,
    assertion_name: str,
) -> None:
    source_id = repository.insert_source(
        source_kind=source_kind,
        root_uri=f"file:///tmp/um05-system-role/{uuid4().hex}",
        account_label=None,
    )
    session_id = repository.upsert_session(
        source_id=source_id,
        external_session_id=normalized.external_session_id,
        vendor=SourceVendor.CODEX,
        source_kind=source_kind,
        vendor_session_label=None,
        project_path="/synthetic/system-event-role-smoke",
        first_event_at=normalized.event_at,
        last_event_at=normalized.event_at,
    )
    batch_id = repository.start_batch(source_id, polling_lease_token=None)
    if not session_id or not batch_id:
        raise AssertionError(f"{assertion_name}: repository failed to seed session or batch")
    external_id = f"synthetic-{uuid4().hex}"
    try:
        inserted = repository.append_event(
            session_id=session_id,
            normalized=normalized,
            batch_id=batch_id,
            content_blob_id=None,
            session_vendor=SourceVendor.CODEX,
            source_kind=source_kind,
            external_id=external_id,
        )
    except ValueError as error:
        raise AssertionError(f"{assertion_name}: SessionLedgerRepository.append_event raised ValueError: {error}") from error
    rows = repository.list_events_by_ids([inserted.event_id])
    if len(rows) != 1:
        raise AssertionError(f"{assertion_name}: append_event read back {len(rows)} event rows")
    row = rows[0]
    if row.get("event_type") != EventType.SYSTEM.value or row.get("role") != MessageRole.SYSTEM.value:
        raise AssertionError(f"{assertion_name}: event readback had event_type={row.get('event_type')!r}, role={row.get('role')!r}")


def _copy_census_sources(destination: Path, source_root: Path = REPO_ROOT) -> None:
    copied: set[str] = set()
    for identity in _expected_system_producer_ids(source_root):
        relative_path = identity.split("::", 1)[0]
        if relative_path in copied:
            continue
        source = source_root / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.add(relative_path)


def _assert_census_controls_and_kill_m3(source_root: Path = REPO_ROOT) -> None:
    expected = _expected_system_producer_ids(source_root)
    with tempfile.TemporaryDirectory(prefix="um05-system-census-") as scratch:
        scan_root = Path(scratch)
        _copy_census_sources(scan_root, source_root)
        comment = scan_root / "plugins/census_controls/src/comment_only.py"
        comment.parent.mkdir(parents=True, exist_ok=True)
        comment.write_text(
            "# NormalizedSessionEvent(event_type=EventType.SYSTEM)\ndescription = 'NormalizedSessionEvent(event_type=EventType.SYSTEM)'\n",
            encoding="utf-8",
        )
        test_fixture = scan_root / "ananta/tests/system_fixture.py"
        test_fixture.parent.mkdir(parents=True, exist_ok=True)
        test_fixture.write_text(
            "NormalizedSessionEvent(event_type=EventType.SYSTEM)\n",
            encoding="utf-8",
        )
        assert _system_producer_inventory(scan_root) == expected, "SYSTEM_PRODUCER_CENSUS_COMMENT_AND_TEST_CONTROLS"

        mutant = scan_root / "plugins/um05_census_mutant/src/mutant.py"
        mutant.parent.mkdir(parents=True, exist_ok=True)
        mutant.write_text(
            "from datetime import UTC, datetime\n"
            "from ananta.llm.session_ledger.types import EventType, NormalizedSessionEvent\n"
            "def added_system_producer() -> NormalizedSessionEvent:\n"
            "    return NormalizedSessionEvent(\n"
            "        external_session_id='synthetic-mutant',\n"
            "        event_type=EventType.SYSTEM,\n"
            "        role=None,\n"
            "        content_text=None,\n"
            "        content_json={'subtype': 'mutation'},\n"
            "        event_at=datetime.now(UTC),\n"
            "        vendor_event_id='synthetic-mutant',\n"
            "        vendor_parent_event_id=None,\n"
            "        attachment_blob_upload=None,\n"
            "        attachment_mime_type=None,\n"
            "        attachment_filename=None,\n"
            "    )\n",
            encoding="utf-8",
        )
        reached = _system_producer_inventory(scan_root)
        mutant_id = "plugins/um05_census_mutant/src/mutant.py::added_system_producer"
        if mutant_id not in reached:
            raise AssertionError(f"M3 mutant call site was not reached: {mutant_id}")
        try:
            _assert_census_inventory(scan_root)
        except AssertionError as error:
            if mutant_id not in str(error):
                raise AssertionError(f"M3 failed outside the intended census assertion: {error}") from error
            frames = traceback.extract_tb(error.__traceback__)
            census_frame = next(frame for frame in reversed(frames) if frame.name == "_assert_census_inventory")
            print(f"KILLED M3 by {CENSUS_ASSERTION} at {Path(census_frame.filename).name}:{census_frame.lineno}; discovered_mutant={mutant_id}; role=None")
        else:
            raise AssertionError(f"M3 escaped {CENSUS_ASSERTION}: {mutant_id}")


def _replace_function_role(
    source: bytes,
    function_name: str,
    old_role: str,
    new_role: str,
) -> bytes:
    text = source.decode("utf-8")
    tree = ast.parse(text)
    function = next(
        (node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name),
        None,
    )
    if function is None:
        raise AssertionError(f"mutant function not found: {function_name}")
    lines = text.splitlines(keepends=True)
    start = function.lineno - 1
    end = function.end_lineno
    if end is None:
        raise AssertionError(f"mutant function has no end line: {function_name}")
    function_text = "".join(lines[start:end])
    needle = f"role={old_role}"
    if function_text.count(needle) != 1:
        raise AssertionError(f"expected one {old_role} role in {function_name}")
    lines[start:end] = [function_text.replace(needle, f"role={new_role}", 1)]
    return "".join(lines).encode("utf-8")


def _load_module_from_source(source_path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load candidate source module: {source_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _run_source_mutant(
    *,
    candidate_source: Path,
    function_name: str,
    module_name: str,
    normalizer: Callable[[ModuleType], NormalizedSessionEvent],
    repository: SessionLedgerRepository,
    source_kind: IngestSourceKind,
    assertion_name: str,
    mutant_name: str,
) -> None:
    original = candidate_source.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    mutated = _replace_function_role(original, function_name, "MessageRole.SYSTEM", "None")
    with tempfile.TemporaryDirectory(prefix=f"um05-{mutant_name.lower()}-") as scratch:
        mutant_path = Path(scratch) / candidate_source.name
        mutant_path.write_bytes(mutated)
        module = _load_module_from_source(mutant_path, module_name)
        try:
            normalized = normalizer(module)
            if normalized.role is not None:
                raise AssertionError(f"{mutant_name} did not reach the role=None normalizer")
            try:
                _seed_and_append(repository, normalized, source_kind, assertion_name)
            except AssertionError as error:
                if SYSTEM_ROLE_ERROR not in str(error):
                    raise AssertionError(f"{mutant_name} failed outside {assertion_name}: {error}") from error
                print(f"KILLED {mutant_name} by {assertion_name}: {error}; normalizer_role=None; candidate_source_sha256={original_hash}")
            else:
                raise AssertionError(f"{mutant_name} escaped {assertion_name}")
        finally:
            mutant_path.write_bytes(original)
            restored_hash = hashlib.sha256(mutant_path.read_bytes()).hexdigest()
            if restored_hash != original_hash:
                raise AssertionError(f"{mutant_name} source restoration mismatch: expected {original_hash}, got {restored_hash}")


def _run_named(name: str, operation: Callable[[], None]) -> bool:
    try:
        operation()
    except Exception as error:
        print(f"FAIL {name}: {type(error).__name__}: {error}")
        return False
    print(f"PASS {name}")
    return True


def _run_named_checks(
    repository: SessionLedgerRepository,
    scan_root: Path,
) -> list[bool]:
    return [
        _run_named(
            TOKEN_COUNT_ASSERTION,
            lambda: _seed_and_append(
                repository,
                _token_count_event(),
                IngestSourceKind.CODEX_LOCAL,
                TOKEN_COUNT_ASSERTION,
            ),
        ),
        _run_named(
            STATE_ASSERTION,
            lambda: _seed_and_append(
                repository,
                _state_event(),
                IngestSourceKind.CODEX_STATE,
                STATE_ASSERTION,
            ),
        ),
        _run_named(
            GOALS_ASSERTION,
            lambda: _seed_and_append(
                repository,
                _goals_event(),
                IngestSourceKind.CODEX_GOALS,
                GOALS_ASSERTION,
            ),
        ),
        _run_named(
            CENSUS_ASSERTION,
            lambda: _assert_system_producer_census(scan_root),
        ),
    ]


def _run_candidate_mutants(
    repository: SessionLedgerRepository,
    source_root: Path,
) -> bool:
    results = [
        _run_named(
            f"{mutant_name}_NAMED_MUTANT",
            lambda mutant_name=mutant_name: _run_candidate_mutant(mutant_name, repository, source_root),
        )
        for mutant_name in ("M1", "M2", "M3")
    ]
    if all(results):
        print("PASS M1-M3 named mutants; scratch source hashes restored and census controls held")
    return all(results)


def _run_candidate_mutant(
    mutant_name: str,
    repository: SessionLedgerRepository,
    source_root: Path,
) -> None:
    codex_path = source_root / "ananta/src/ananta/llm/session_ledger/vendor/codex.py"
    goals_path = source_root / "ananta/src/ananta/llm/session_ledger/vendor/codex_goals.py"
    if mutant_name == "M1":
        _run_source_mutant(
            candidate_source=codex_path,
            function_name="_normalize_token_count",
            module_name="ananta.llm.session_ledger.vendor._um05_mutant_m1",
            normalizer=lambda module: _token_count_event(module),
            repository=repository,
            source_kind=IngestSourceKind.CODEX_LOCAL,
            assertion_name=TOKEN_COUNT_ASSERTION,
            mutant_name="M1",
        )
    elif mutant_name == "M2":
        _run_source_mutant(
            candidate_source=goals_path,
            function_name="normalize_raw",
            module_name="ananta.llm.session_ledger.vendor._um05_mutant_m2",
            normalizer=lambda module: _goals_event(module),
            repository=repository,
            source_kind=IngestSourceKind.CODEX_GOALS,
            assertion_name=GOALS_ASSERTION,
            mutant_name="M2",
        )
    elif mutant_name == "M3":
        _assert_census_controls_and_kill_m3(source_root)
    else:
        raise ValueError(f"unknown SYSTEM role mutant: {mutant_name}")
    print(f"PASS {mutant_name} named mutant; scratch source hash restored where applicable")


def _parse_args() -> tuple[str, Path]:
    if len(sys.argv) == 1:
        return "all", REPO_ROOT
    if sys.argv[1:] == ["--census-only"]:
        return "census", REPO_ROOT
    if len(sys.argv) == 3 and sys.argv[1] == "--census-only":
        return "census", Path(sys.argv[2]).resolve()
    if sys.argv[1:] == ["--candidate"]:
        return "candidate", REPO_ROOT
    if len(sys.argv) == 3 and sys.argv[1] == "--mutant" and sys.argv[2] in {"M1", "M2", "M3"}:
        return f"mutant:{sys.argv[2]}", REPO_ROOT
    raise SystemExit("usage: system_event_role_ingest_smoke.py [--census-only [SCAN_ROOT] | --candidate | --mutant M1|M2|M3]")


def _run_census_mode(scan_root: Path) -> int:
    return 0 if _run_named(CENSUS_ASSERTION, lambda: _assert_system_producer_census(scan_root)) else 1


def _run_mutant_mode(
    mode: str,
    repository: SessionLedgerRepository,
    scan_root: Path,
) -> int:
    mutant_name = mode.split(":", maxsplit=1)[1]
    passed = _run_named(
        f"{mutant_name}_NAMED_MUTANT",
        lambda: _run_candidate_mutant(mutant_name, repository, scan_root),
    )
    return 0 if passed else 1


def _run_candidate_mode(
    mode: str,
    repository: SessionLedgerRepository,
    scan_root: Path,
) -> int:
    checks = _run_named_checks(repository, scan_root)
    if not all(checks):
        print("FAIL system-event role ingest smoke: one or more named assertions failed")
        return 1
    if mode == "all" and not _run_candidate_mutants(repository, scan_root):
        return 1
    print("PASS system-event role ingest smoke")
    return 0


def _run_database_mode(mode: str, scan_root: Path) -> int:
    if not os.environ.get("PROJECT_SOLET_DESK_TEST_DSN"):
        print("SKIP private PostgreSQL fixture absent: PROJECT_SOLET_DESK_TEST_DSN; run under project-solet/scripts/provision_test_pg.py --exec")
        print('BORN_CLONE_SKIP_WITNESS={"reason":"local_postgres_fixture_unreachable"}')
        return 77

    try:
        with _private_ledger() as repository:
            if mode.startswith("mutant:"):
                return _run_mutant_mode(mode, repository, scan_root)
            return _run_candidate_mode(mode, repository, scan_root)
    except Exception as error:
        print(f"PRIVATE_POSTGRES_SETUP_OR_CLEANUP_FAILED: {type(error).__name__}: {error}")
        return 2


def main() -> int:
    mode, scan_root = _parse_args()
    if mode == "census":
        return _run_census_mode(scan_root)
    return _run_database_mode(mode, scan_root)


if __name__ == "__main__":
    raise SystemExit(main())
