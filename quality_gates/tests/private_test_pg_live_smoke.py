#!/usr/bin/env python3
"""Physical full-schema uniqueness and fixture-only public bootstrap proof."""

from __future__ import annotations

import builtins
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta/src"))
sys.path.insert(0, str(_ROOT / "plugins/postgres_state_management_plugin/src"))

from ananta.core.config.config_manager import initialize_config  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.session_ledger.schema import get_session_ledger_schema  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402

from quality_gates.private_test_pg import (  # noqa: E402
    MANIFEST_ENV,
    LedgerFixture,
    ledger_fixture,
    load_fixture,
    skip_fixture,
)


def _prove_uniqueness(fixture: LedgerFixture) -> None:
    state = cast("StateManagementInterface", fixture.state)
    record: dict[str, Any] = {
        "session_id": "private-fixture-session",
        "chunk_index": 0,
        "summary_text": "untouched first row",
        "generated_at": datetime.now(UTC),
        "generated_by_client_id": "private-fixture-client",
    }
    first = state.write_state("session_ledger", {"table": "summary", "record": record | {"id": "ssu-_fixture_first"}})
    assert first.get("action_status") == "completed", ("FULL_SCHEMA_VALID_SETUP", first)
    duplicate_failed = False
    try:
        second = state.write_state("session_ledger", {"table": "summary", "record": record | {"id": "ssu-_fixture_second", "summary_text": "must fail"}})
        duplicate_failed = second.get("action_status") == "error"
    except Exception:
        duplicate_failed = True
    result = state.query_state("session_ledger", {"table": "summary", "filters": {"session_id": record["session_id"]}})
    rows = cast("dict[str, Any]", result.get("data", {})).get("records", [])
    assert duplicate_failed and len(rows) == 1 and rows[0]["id"] == "ssu-_fixture_first" and rows[0]["summary_text"] == record["summary_text"], ("FULL_SCHEMA_INDEX_ENFORCED", duplicate_failed, rows)
    print("PASS FULL_SCHEMA_INDEX_ENFORCED distinct ids, duplicate (session_id,chunk_index), original row untouched")


def main() -> int:
    if not os.environ.get(MANIFEST_ENV):
        return skip_fixture()
    config = load_fixture("ledger_ingest")
    homes: list[Path] = []
    reads: list[Path] = []
    original_open = Path.open
    original_builtin_open = builtins.open

    def initialize(raw_app_home: str) -> object:
        home = Path(raw_app_home).resolve()
        assert home.parent == config.root and home.name.startswith("app-"), "NO_PROFILE_READ"
        assert not any(home.iterdir()), "EMPTY_FIXTURE_APP_HOME"
        homes.append(home)
        return initialize_config(raw_app_home)

    def validate_path(path: Path) -> None:
        assert "profile" not in path.parts, "NO_PROFILE_READ"
        if path.name == "postgres_state_management_plugin.json":
            assert homes and path.resolve().is_relative_to(homes[0]), "NO_PROFILE_READ"
            reads.append(path.resolve())

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        validate_path(path)
        return original_open(path, *args, **kwargs)

    def guarded_builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, str | os.PathLike):
            validate_path(Path(file))
        return original_builtin_open(file, *args, **kwargs)

    before = dict(os.environ)
    with patch("ananta.core.config.config_manager.initialize_config", side_effect=initialize), patch.object(Path, "open", guarded_open), patch.object(builtins, "open", guarded_builtin_open):
        with ledger_fixture("ledger_ingest", "private_test_contract") as fixture:
            assert fixture.vault_keys == [fixture.expected_vault_key], ("FIXTURE_VAULT_KEY_ONLY", fixture.vault_keys)
            assert dict(os.environ) == before, "CALLER_ENVIRONMENT_UNCHANGED"
            installed = fixture.plugin.get_installed_schema("session_ledger")
            indexes = installed["tables"]["summary"]["schema_snapshot"]["indexes"]
            assert any(item["name"] == "idx_summary_session_chunk_unique" and item["unique"] for item in indexes), "PRODUCTION_INDEX_RECORDED"
            SchemaManager(fixture.state, fixture.plugin).initialize_schemas([get_session_ledger_schema()])
            print("PASS FULL_SCHEMA_IDEMPOTENT_INSTALL")
            _prove_uniqueness(fixture)
            assert reads and all(path.is_relative_to(fixture.app_home) for path in reads), "NO_PROFILE_READ"
    assert dict(os.environ) == before, "CALLER_ENVIRONMENT_UNCHANGED"
    print("PASS NO_PROFILE_READ FIXTURE_VAULT_KEY_ONLY")
    print("PRIVATE_PG_BODY fixture_contract FULL_SCHEMA_INDEX_ENFORCED FULL_SCHEMA_IDEMPOTENT_INSTALL NO_PROFILE_READ FIXTURE_VAULT_KEY_ONLY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
