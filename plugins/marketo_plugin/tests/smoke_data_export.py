#!/usr/bin/env python3
"""Data-export containment-gate smoke tests for marketo_plugin.

Business-data limits + data-export migration, 2026-08-02
(workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md,
§7.1). describe_lead_fields, get_leads, list_activity_types, get_activities,
list_campaigns, and list_static_lists now ALWAYS write to the caller-supplied
output_tsv_path — blob storage has retired from this plugin entirely.

Hermetic — a MagicMock standing in for MarketoClient, no live instance. Drives
the REAL export_containment gate (via the plugin's own ``_export_path_gate``,
bound to a fake config_provider) and the real ``assert_export_path_allowed``
directly — the containment boundary is exactly what must not be mocked.

Exercises:
  1. red-first: export path OUTSIDE every allowed root -> PathRefusedError,
     no file written, the vendor call never runs
  2. red-first: EMPTY export_allowed_roots -> refused naming the config key
  3. red-first: RELATIVE or BLANK configured root -> refused as misconfigured
  4. a path INSIDE an allowed root is admitted and the vendor call proceeds
  5. check_setup's probes work with NO export_allowed_roots configured at all —
     a permission check must never depend on the operator's real export
     workspace being set up (regression guard on the tempfile-passthrough
     design of _run_read_probe)
  6. the plugin's _export_path_gate rejects a malformed (non-list)
     export_allowed_roots config value loudly, not silently

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/marketo_plugin/tests/smoke_data_export.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import NonCallableMagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))
import tempfile  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from ananta.file_locations.containment import PathRefusedError, assert_path_allowed, derive_allowed_roots  # noqa: E402
from ananta.file_locations.resolution import PathAddress, SoletTree  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))

from marketo_plugin import marketing_actions  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402

_passed = 0
_failed: list[str] = []


_LOCATION_ROOT = Path(__file__).resolve().parents[3]
_LOCATION_TREE = SoletTree(_LOCATION_ROOT, _LOCATION_ROOT / "profile")


def _checked_path(path: str, directories: list[str], *, suffix: str | None = ".tsv", param_name: str = "output_tsv_path", reading: bool = False) -> str:
    """Exercise real address-derived containment, never a mocked boundary."""
    addresses = [PathAddress(f"project-{index}", Path(directory), frozenset(), frozenset(), datetime.now(UTC)) for index, directory in enumerate(directories)]
    roots = derive_allowed_roots(addresses, None, _LOCATION_TREE, _LOCATION_ROOT.parent / "test-solet-files")
    result = assert_path_allowed(path, roots, _LOCATION_TREE, suffix=suffix, param_name=param_name, plugin_name="test")
    if reading and not Path(result.value).is_file():
        raise PathRefusedError(f"{param_name} must name an existing regular file")
    return result.value


def _bind_locations(plugin: Any, directories: list[str]) -> NonCallableMagicMock:
    """Supply a live address-book service through the production binding."""
    orchestrator = NonCallableMagicMock()
    orchestrator.APP_HOME = str(_LOCATION_TREE.app_home)
    orchestrator.plugin_manager.get_plugin.return_value = None
    orchestrator.get_service.return_value.list_path_addresses.return_value = {
        "action_status": "completed",
        "data": {"addresses": [{"name": f"project-{index}", "tags": [], "updated_at": datetime.now(UTC).isoformat(), "entries": [{"field_type": "path", "value": directory}]} for index, directory in enumerate(directories)]},
    }
    plugin.orchestrator_ref = orchestrator
    return orchestrator


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _gate_for(roots: list[str]) -> Any:
    def gate(output_tsv_path: str) -> str:
        return _checked_path(output_tsv_path, roots, suffix=".tsv", param_name="output_tsv_path", reading=False)

    return gate


def _fake_get_json_client() -> Any:
    client = MagicMock()
    client.get_json.return_value = {"success": True, "result": []}
    return client


def test_export_path_outside_allowed_root_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="marketo_allowed_") as allowed_root, tempfile.TemporaryDirectory(prefix="marketo_outside_") as outside_root:
        gate = _gate_for([allowed_root])
        out_path = str(Path(outside_root) / "out.tsv")
        client = _fake_get_json_client()
        raised = False
        try:
            marketing_actions.list_activity_types(client, {"output_tsv_path": out_path}, gate)
        except PathRefusedError:
            raised = True
        _assert("outside-root path refused", raised)
        _assert("vendor never called when the path is refused", client.get_json.call_count == 0)
        _assert("no file written", not Path(out_path).exists())


def test_empty_allowed_roots_refused() -> None:
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory).resolve() / "home"
        roots = derive_allowed_roots([], None, _LOCATION_TREE, home)
        result = assert_path_allowed(str(home / "Exports.nosync/out.tsv"), roots, _LOCATION_TREE, suffix=".tsv", param_name="output_tsv_path", plugin_name="test")
        _assert("empty address book admits computed exports folder", result.value == str(home / "Exports.nosync/out.tsv"))
        _assert("computed default carries an advisory", bool(result.advisories))


def test_relative_root_misconfigured() -> None:
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory).resolve() / "home"
        for raw in ("relative-root", ""):
            addresses = [PathAddress("malformed", Path(raw), frozenset(), frozenset(), datetime.now(UTC))]
            roots = derive_allowed_roots(addresses, None, _LOCATION_TREE, home)
            _assert("malformed address is disclosed", any("malformed" in message for message in roots.advisories))
            raised = False
            try:
                assert_path_allowed(str(Path(directory).resolve() / "outside.tsv"), roots, _LOCATION_TREE, suffix=".tsv", param_name="output_tsv_path", plugin_name="test")
            except PathRefusedError as exc:
                raised = "path address" in str(exc)
            _assert("malformed address never authorizes another folder", raised)


def test_path_inside_allowed_root_admitted() -> None:
    with tempfile.TemporaryDirectory(prefix="marketo_allowed_") as allowed_root:
        gate = _gate_for([allowed_root])
        out_path = str(Path(allowed_root) / "out.tsv")
        client = MagicMock()
        client.get_json.return_value = {"success": True, "result": [{"id": 1, "name": "Send Email"}]}
        result = marketing_actions.list_activity_types(client, {"output_tsv_path": out_path}, gate)
        _assert("admitted path resolves and the vendor call proceeds", client.get_json.call_count == 1)
        _assert("file written under the allowed root", Path(out_path).exists())
        _assert("row_count reflects the written record", result["row_count"] == 1)


def test_check_setup_needs_no_export_allowed_roots() -> None:
    """check_setup is a permission probe, not an export — it must succeed even
    when export_allowed_roots is entirely unconfigured (the operator has not
    opted any workspace root in yet). Regression guard on _run_read_probe's
    tempfile-passthrough design: if a future edit accidentally routed the
    probes through the plugin's real _export_path_gate, this would fail with
    marketo.export_path_refused instead of running the probes."""
    client = MagicMock()
    client.get_json.return_value = {"success": True, "result": [], "searchableFields": ["id"]}
    result = marketing_actions.check_setup(client)
    _assert("check_setup succeeds with zero export_allowed_roots configured", result["reads_verified"] is True)
    _assert("check_setup ran all 6 read probes", len(result["checks"]) == 6, str(result["checks"]))


def test_plugin_export_path_gate_rejects_malformed_config() -> None:
    from ananta.file_locations.containment import location_advisories, location_session

    plugin = MarketoPlugin()
    plugin.initialize({"export_allowed_roots": "retired setting is ignored"})
    orchestrator = _bind_locations(plugin, [])
    orchestrator.get_service("address_book_service").list_path_addresses.return_value = {"action_status": "error", "error": "unavailable"}
    with location_session(None):
        roots, _ = plugin._location_roots()
        path = str(roots.value[-1] / "out.tsv")
        _assert("unavailable addresses admit computed home", plugin._export_path_gate(path) == path)
        _assert("unavailable address service is advisory", any("unavailable" in text.lower() for text in location_advisories()))


def test_address_root_replaces_config() -> None:
    """A live address authorizes exports without a connector root setting."""
    from marketo_plugin.plugin import MarketoPlugin

    with tempfile.TemporaryDirectory() as directory:
        plugin = MarketoPlugin()
        plugin.initialize({})
        orchestrator = MagicMock()
        orchestrator.APP_HOME = str(REPO_ROOT / "profile")
        orchestrator.get_service.return_value.list_path_addresses.return_value = {"action_status": "completed", "data": {"addresses": [{"name": "project", "tags": [], "updated_at": "2026-11-08T00:00:00+00:00", "entries": [{"field_type": "path", "value": directory}]}]}}
        orchestrator.plugin_manager.get_plugin.return_value = None
        plugin.orchestrator_ref = orchestrator
        path = str(Path(directory).resolve() / "out.tsv")
        _assert("live path address authorizes export without connector config", plugin._export_path_gate(path) == path)


def main() -> int:
    test_address_root_replaces_config()
    print("\nmarketo_plugin data-export containment-gate smoke tests")
    print("=" * 57)
    test_export_path_outside_allowed_root_refused()
    test_empty_allowed_roots_refused()
    test_relative_root_misconfigured()
    test_path_inside_allowed_root_admitted()
    test_check_setup_needs_no_export_allowed_roots()
    test_plugin_export_path_gate_rejects_malformed_config()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All data-export containment-gate smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
