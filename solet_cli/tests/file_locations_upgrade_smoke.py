"""Old adopted solets: real update, durable path seeds, then the new boot loader.

Exercises migrate_file_locations through existing_install_migrations.py and _step7_support.py, with a
temporary HOME, opaque stores and the real address-book CRUD over injected state.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT), str(Path(__file__).resolve().parent), str(_ROOT / "ananta/src"), str(_ROOT / "plugins/default_address_book_plugin/src")]

from _step7_support import NAME, Knobs, RealStyleFixture, build_real_style, cli, run_reference_update  # noqa: E402
from ananta.file_locations.resolution import SoletTree, overlaps_solet_tree  # noqa: E402
from default_address_book_plugin.plugin import DefaultAddressBookPlugin  # noqa: E402
from default_address_book_plugin.seed_loader import auto_seed_entries_from_file  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from solet_manager.adapter_protocol import OperationRequest, OperationResult  # noqa: E402
from solet_manager.existing_install_adapters import ExistingInstallAdapterRegistry  # noqa: E402

from plugins.default_address_book_plugin.tests.path_address_resolution_smoke import MemoryState  # noqa: E402
from plugins.github_midwife_plugin.tests.existing_install_operations_smoke import FakeRuntime, _request, _target  # noqa: E402

_checks = 0
_MIGRATION = "existing::migration.export_root_containment"


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _seed(fixture: RealStyleFixture) -> Path:
    return fixture.target / "profile/config/plugins/default_address_book_plugin/entries.json"


def _config_bytes(fixture: RealStyleFixture) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in (fixture.target / "profile/config/plugins").glob("*.json")}


def _entry_paths(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {os.path.realpath(field["value"]): entry for entry in entries for field in entry["entries"] if field["field_type"] == "path"}


def _new_runtime(fixture: RealStyleFixture, collision: bool) -> tuple[DefaultAddressBookPlugin, MemoryState]:
    state = MemoryState()
    plugin = DefaultAddressBookPlugin({"auto_ingest_to_memory": False})
    plugin.state_service = cast(Any, state)
    plugin.orchestrator_ref = cast(Any, SimpleNamespace(APP_HOME=str(fixture.target / "profile")))
    if collision:
        unrelated = fixture.home / "unrelated"
        unrelated.mkdir()
        result = plugin.register("migrated_export_root_1", "path", "User-owned", [{"field_type": "path", "description": "Directory", "value": str(unrelated)}], ["keep"])
        _check(result.get("action_status") == "completed", "the live colliding address exists before migration")
    return plugin, state


def _run_update(fixture: RealStyleFixture) -> list[dict[str, Any]]:
    seams = fixture.request.runtime_seams
    assert seams is not None
    original = seams.invoke_adapter
    responses: list[dict[str, Any]] = []

    def invoke(registry: ExistingInstallAdapterRegistry, request: OperationRequest) -> OperationResult:
        before = list(fixture.host.bridge_calls)
        outcome = original(registry, request)
        if request.operation_ref == _MIGRATION:
            _check(fixture.host.bridge_calls == before, "the pre-runtime migration calls no runtime verb")
            responses.append(cast(dict[str, Any], outcome.to_dict()))
        return outcome

    fixture.request = replace(fixture.request, runtime_seams=replace(seams, invoke_adapter=invoke))
    _check(run_reference_update(fixture) == "promoted", "the real offline update promotes the old-shape solet without refusal")
    _check(any(item["phase"] == "apply" for item in responses), "migrations_pre actually applies export_root_containment")
    _check(all(item["checkpoint_status"] != "blocked" for item in responses), "odd and disagreeing roots never block the update")
    return responses


def _assert_marker(fixture: RealStyleFixture, responses: list[dict[str, Any]], in_documents: bool) -> list[dict[str, Any]]:
    seed = _seed(fixture)
    _check(seed.is_file(), "the durable pending address marker exists after the update")
    entries = cast(list[dict[str, Any]], json.loads(seed.read_text())["entries"])
    _assert_migrated_roots(fixture, entries)
    _assert_home_marker(fixture, entries, responses, in_documents)
    return entries


def _assert_migrated_roots(fixture: RealStyleFixture, entries: list[dict[str, Any]]) -> None:
    paths = _entry_paths(entries)
    expected = {
        fixture.home / "legacy_exports": ("path", {"migrated", "from:jira_plugin", "from:external_postgres_plugin", "from:salesforce_plugin"}),
        fixture.home / "other_exports": ("path", {"migrated", "from:snowflake_plugin", "from:salesforce_plugin"}),
        fixture.home / "missing_exports": ("path", {"migrated", "missing", "from:salesforce_plugin"}),
        fixture.target / "legacy_exports": ("path_inside_solet_tree", {"migrated", "from:snowflake_plugin"}),
    }
    for directory, (address_type, tags) in expected.items():
        entry = paths.get(os.path.realpath(directory))
        _check(entry is not None and entry["address_type"] == address_type and tags <= set(entry["tags"]), f"{directory} is carried once with its type and source tags")
    _check(sum(entry["name"].startswith("migrated_export_root_") for entry in entries) == 4, "aliases and expanded roots deduplicate by realpath")


def _assert_home_marker(fixture: RealStyleFixture, entries: list[dict[str, Any]], responses: list[dict[str, Any]], in_documents: bool) -> None:
    home = fixture.home / "Library/Application Support" / NAME / "Files" if in_documents else fixture.home / "Documents" / NAME
    for name, directory in (("solet_home", home), ("solet_home_exports", home / "Exports.nosync")):
        _check(any(entry["name"] == name and entry["entries"][0]["value"] == str(directory) for entry in entries) and directory.is_dir(), f"{name} is registered in the marker and created outside the clone")
    observations = json.dumps([item["evidence"] for item in responses])
    for odd in (str(fixture.home / "missing_exports"), str(fixture.target / "legacy_exports"), "relative/exports", "blank"):
        _check(odd in observations, f"operation advisories name the odd root {odd}")
    if in_documents:
        _check("Application Support" in observations and "tree-overlapping" in observations, "a Documents-rooted clone reports its application-support home choice")


def _boot(fixture: RealStyleFixture, plugin: DefaultAddressBookPlugin, state: MemoryState, *, collision: bool) -> None:
    output = io.StringIO()
    logger = logging.getLogger("file_locations_upgrade")
    handler = logging.StreamHandler(output)
    logger.addHandler(handler)
    try:
        auto_seed_entries_from_file(fixture.target / "profile", plugin.name, logger, cast(Any, state), None, False)
    finally:
        logger.removeHandler(handler)
    listed = cast(dict[str, Any], plugin.list_path_addresses().get("data"))["addresses"]
    live = _entry_paths(listed)
    for directory in (fixture.home / "legacy_exports", fixture.home / "other_exports", fixture.home / "missing_exports"):
        _check(os.path.realpath(directory) in live, f"new boot completes the pending path {directory}")
    tree = SoletTree(fixture.target, fixture.target / "profile")
    _check(all(not overlaps_solet_tree(Path(directory), tree) for directory in live), "no tree-overlapping entry becomes a live path address")
    if collision:
        _check(any("renamed_from:migrated_export_root_1" in entry["tags"] for entry in listed), "the colliding pending root is registered under the next free name")
        _check("renamed" in output.getvalue() and "migrated_export_root_1" in output.getvalue(), "boot reports the live name collision")


def _fixture(root: Path, *, in_documents: bool = False, collision: bool = False) -> None:
    home = root / "home"
    with patch.dict(os.environ, {"HOME": str(home), "FILE_LOCATIONS_OLD_ROOT": str(home / "other_exports")}):
        fixture = build_real_style(root, knobs=Knobs(manager_state="enrolled", file_locations_old_shape=True, clone_in_documents=in_documents))
        configs = _config_bytes(fixture)
        export = home / "legacy_exports/existing.tsv"
        exported = export.read_bytes()
        _check(not (home / "Documents" / NAME).exists(), "old shape has no solet_home")
        plugin, state = _new_runtime(fixture, collision)
        if collision:
            _seed(fixture).unlink()
        unrelated = json.dumps(state.tables, sort_keys=True)
        responses = _run_update(fixture)
        _assert_marker(fixture, responses, in_documents)
        _check(_config_bytes(fixture) == configs and export.read_bytes() == exported, "update leaves connector configs and old exports byte-identical")
        _check(not hasattr(fixture.host, "list_path_addresses"), "the old runtime exposes no new address-list verb")
        # A rollback reads exactly these untouched old keys; a forward boot still owes the marker.
        _check(json.loads(configs["jira_plugin.json"])["export_allowed_roots"] == [str(home / "legacy_exports")], "rollback retains the old export root")
        _check(json.dumps(state.tables, sort_keys=True) == unrelated, "migration never writes the old runtime state")
        _boot(fixture, plugin, state, collision=collision)
        if collision:
            _check(json.dumps({"address": state.tables["address"][:1], "address_entry": state.tables["address_entry"][:1]}, sort_keys=True) == unrelated, "the user-owned colliding address is untouched")
        marker, before = _seed(fixture).read_bytes(), json.dumps(state.tables, sort_keys=True)
        code, repeated = cli(fixture, ["update", NAME, "--dry-run"])
        _check(code == 0 and repeated["status"] == "already_current", f"a second real update is a no-op: {repeated['status']}")
        _boot(fixture, plugin, state, collision=False)
        _check(_seed(fixture).read_bytes() == marker and json.dumps(state.tables, sort_keys=True) == before, "second update plus boot changes no marker or live address")
        _check(_config_bytes(fixture) == configs and export.read_bytes() == exported, "rollback/forward and second boot preserve configs and exports")


def _recovery_fixture(root: Path) -> None:
    target, _ = _target(root)
    runtime = FakeRuntime(root / "home")
    runtime.home.mkdir()
    config_dir = target / "profile/config/plugins"
    config = config_dir / "jira_plugin.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"export_allowed_roots": [str(root / "work")]}))
    dispatch_request(_request(target, _MIGRATION, phase="apply"), runtime)
    seed = config.parent / "default_address_book_plugin/entries.json"
    _check_export_root_recovery(root, target, runtime, seed)


def _check_export_root_recovery(root: Path, target: Path, runtime: FakeRuntime, seed: Path) -> None:
    ref = "existing::migration.export_root_containment"
    payload = json.loads(seed.read_text())
    live = next(entry for entry in payload["entries"] if entry["name"].startswith("migrated_export_root_"))
    live["name"] = "user_edited_name"
    live["tags"].append("keep")
    payload["entries"].append({"name": "migrated_export_root_90", "address_type": "path", "description": "User-owned", "tags": ["keep"], "entries": [{"field_type": "path", "description": "Directory", "value": str(root / "unrelated")}]})
    seed.write_text(json.dumps(payload))
    saved = seed.read_bytes()
    dispatch_request(_request(target, ref, phase="apply"), runtime)
    _check(seed.read_bytes() == saved, "user-edited names and unrelated seed addresses survive a path-keyed rerun")
    config_dir = target / "profile/config/plugins"
    config = config_dir / "jira_plugin.json"
    config.write_text(json.dumps({"export_allowed_roots": [str(root / "later")]}))
    with patch.object(runtime, "atomic_write", side_effect=OSError("interrupted before rename")) as interrupted:
        try:
            dispatch_request(_request(target, ref, phase="apply"), runtime)
        except OSError:
            pass
    _check(interrupted.call_count == 1, "the half-run fixture reached the atomic pending-marker write")
    _check(seed.read_bytes() == saved, "an interrupted marker write leaves the previous pending addresses intact")
    dispatch_request(_request(target, ref, phase="apply"), runtime)
    added = json.loads(seed.read_text())["entries"]
    _check(any(entry["name"] == "migrated_export_root_91" and entry["entries"][0]["value"] == str(root / "later") for entry in added), "recovery appends above the highest saved migrated name")
    with patch.object(Path, "mkdir", side_effect=PermissionError("Documents access denied")):
        advisory = cast(dict[str, Any], dispatch_request(_request(target, ref, phase="apply"), runtime))
    notes = json.dumps(advisory["evidence"])
    _check(advisory["checkpoint_status"] == "applied" and ("Files and Folders" if sys.platform == "darwin" else "Grant folder access") in notes, "missing folder access is an advisory naming the grant, never a refusal")
    _check(cast(dict[str, Any], dispatch_request(_request(target, ref, purpose="post_apply"), runtime))["checkpoint_status"] == "verified", "a saved marker satisfies the postcondition even if folder access was denied")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="file-locations-upgrade-") as temporary:
        root = Path(temporary).resolve()
        _recovery_fixture(root / "recovery")
        _fixture(root / "old-shape")
        _fixture(root / "documents", in_documents=True)
        _fixture(root / "collision", collision=True)
    print(f"file_locations_upgrade_smoke OK: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
