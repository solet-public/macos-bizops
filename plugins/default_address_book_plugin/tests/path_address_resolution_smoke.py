"""Path-address lifecycle and resolver using the real plugin and injected state."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / "ananta/src"))
sys.path.insert(0, str(root / "plugins/default_address_book_plugin/src"))

from ananta.file_locations.addresses import path_addresses  # noqa: E402
from default_address_book_plugin.plugin import DefaultAddressBookPlugin  # noqa: E402
from default_address_book_plugin.seed_loader import auto_seed_entries_from_file  # noqa: E402


class MemoryState:
    """Small record store exercising real address CRUD without a database."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {"address": [], "address_entry": []}
        self.counter = 0
        self.fail_table: str | None = None

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        filters = query.get("filters", query.get("where", {}))
        rows = [row.copy() for row in self.tables[query["table"]] if all(row.get(key, 0) == value for key, value in filters.items())]
        return {"action_status": "completed", "data": {"records": rows}}

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        del namespace
        self.counter += 1
        identity = str(self.counter)
        self.tables[data["table"]].append({**data["record"], "id": identity})
        return {"data": {"result": {"generated_id": identity}}}

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        del namespace
        if query["table"] == self.fail_table:
            return {"action_status": "error", "data": {}, "error": {"code": "fixture.write_failed", "message": "injected write failure"}}
        for row in self.tables[query["table"]]:
            if all(row.get(key) == value for key, value in query["filters"].items()):
                row.update(updates)
        return {"action_status": "completed", "data": {}}

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        self.tables[query["table"]] = [row for row in self.tables[query["table"]] if not all(row.get(key) == value for key, value in query["filters"].items())]
        return {"action_status": "completed", "data": {}}


class PathAddressResolutionSmoke(unittest.TestCase):
    def test_boot_matches_paths_and_preserves_name_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            app_home = root / "clone/profile"
            store = MemoryState()
            plugin = DefaultAddressBookPlugin({"auto_ingest_to_memory": False})
            plugin.state_service = cast(Any, store)
            plugin.orchestrator_ref = cast(Any, SimpleNamespace(APP_HOME=str(app_home)))
            folders = [root / name for name in ("unrelated", "existing", "pending")]
            for folder in folders:
                folder.mkdir()
            alias = root / "alias"
            alias.symlink_to(folders[1], target_is_directory=True)

            def entry(name: str, folder: Path, tags: list[str]) -> dict[str, Any]:
                return {"name": name, "address_type": "path", "description": "Directory", "tags": tags, "entries": [{"field_type": "path", "description": "Directory", "value": str(folder)}]}

            for item in (entry("migrated_export_root_1", folders[0], ["keep"]), entry("live", folders[1], ["existing"])):
                self.assertEqual(plugin.register(**item).get("action_status"), "completed")
            unrelated = json.dumps(store.tables["address"][0], sort_keys=True)
            seed = app_home / "config/plugins/default_address_book_plugin/entries.json"
            seed.parent.mkdir(parents=True)
            seed.write_text(json.dumps({"entries": [entry("duplicate", alias, ["migrated", "project: Acme "]), entry("migrated_export_root_1", folders[2], ["migrated"]), entry("unsafe", app_home, [])]}))
            logger = logging.getLogger("path_migration_seed")
            with self.assertLogs(logger, level="WARNING") as logs:
                auto_seed_entries_from_file(app_home, plugin.name, logger, cast(Any, store), None, False)
            live = cast(dict[str, Any], plugin.resolve("live").get("data"))
            self.assertEqual(set(live["tags"]), {"existing", "migrated", "project:acme"})
            self.assertEqual(plugin.resolve("duplicate").get("action_status"), "error")
            renamed = cast(dict[str, Any], plugin.resolve("migrated_export_root_2").get("data"))
            self.assertEqual(renamed["entries"][0]["value"], str(folders[2]))
            self.assertIn("renamed_from:migrated_export_root_1", renamed["tags"])
            self.assertEqual(json.dumps(store.tables["address"][0], sort_keys=True), unrelated)
            self.assertTrue(any("unsafe" in line and "outside" in line for line in logs.output))
            result = plugin.resolve_location_action({"project": "acme"}, {})["data"]
            self.assertTrue(any(str(folders[2]) in advisory and "migrated_export_root_2" in advisory for advisory in result["advisories"]))
            before = json.dumps(store.tables, sort_keys=True)
            pending_bytes = seed.read_bytes()
            with self.assertLogs(logger, level="WARNING"):
                auto_seed_entries_from_file(app_home, plugin.name, logger, cast(Any, store), None, False)
            self.assertEqual(json.dumps(store.tables, sort_keys=True), before)
            self.assertEqual(seed.read_bytes(), pending_bytes)
            seed.write_text(json.dumps({"entries": [entry("duplicate", alias, ["later"])]}))
            store.fail_table = "address"
            with self.assertRaisesRegex(RuntimeError, "failed to merge seed tags"):
                auto_seed_entries_from_file(app_home, plugin.name, logger, cast(Any, store), None, False)
            self.assertEqual(json.dumps(store.tables, sort_keys=True), before)

    def test_lifecycle_and_boot_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            app_home = root / "clone" / "profile"
            folder = root / "Documents" / "x"
            folder.mkdir(parents=True)
            store = MemoryState()
            plugin = DefaultAddressBookPlugin({"auto_ingest_to_memory": False})
            plugin.state_service = cast(Any, store)
            plugin.orchestrator_ref = cast(Any, SimpleNamespace(APP_HOME=str(app_home)))
            entries = [{"field_type": "path", "description": "Reports", "value": str(folder)}]
            self.assertEqual(plugin.register("acme", "path", "Reports", entries, ["project: Acme ", "file_kind: Report "]).get("action_status"), "completed")
            result = plugin.resolve_location_action({"project": "acme", "file_kind": "report"}, {})["data"]
            self.assertEqual(result["directory"], str(folder))
            self.assertEqual(result["rung"], "project")
            self.assertTrue(result["writable"])
            folder.joinpath("report.txt").write_text("report")
            self.assertTrue(folder.joinpath("report.txt").is_file())
            unsafe = [{**entries[0], "value": str(app_home / "data")}]
            rejected = plugin.register("unsafe", "path", "unsafe", unsafe)
            self.assertEqual(rejected.get("action_status"), "error")
            self.assertIn("outside", str(rejected.get("error")))
            self.assertEqual(plugin.register("opaque", "opaque", "not a location", unsafe).get("action_status"), "completed")
            self.assertEqual(plugin.update("opaque", address_type="path").get("action_status"), "completed")
            self.assertEqual(plugin.update("opaque", address_type="opaque").get("action_status"), "completed")
            entry_id = cast(dict[str, Any], plugin.resolve("acme").get("data"))["entries"][0]["id"]
            self.assertEqual(plugin.update_entry(entry_id, value=str(app_home)).get("action_status"), "error")
            self.assertEqual(cast(dict[str, Any], plugin.resolve("acme").get("data"))["entries"][0]["value"], str(folder))
            corrected = root / "Documents" / "corrected"
            corrected.mkdir()
            self.assertEqual(plugin.update_entry(entry_id, value=str(corrected)).get("action_status"), "completed")
            self.assertEqual(path_addresses(plugin).value[0].directory, corrected)
            self.assertEqual(plugin.update("acme", tags=[]).get("action_status"), "completed")
            self.assertFalse(path_addresses(plugin).value[0].projects)
            self.assertEqual(plugin.delete("acme").get("action_status"), "completed")
            self.assertFalse(path_addresses(plugin).value)
            self.assertEqual(plugin.register("append", "path", "append later", []).get("action_status"), "completed")
            self.assertEqual(plugin.add_entry("append", "path", "unsafe", str(app_home)).get("action_status"), "error")
            self.assertEqual(plugin.add_entry("append", "path", "safe", str(folder)).get("action_status"), "completed")
            self.assertEqual(plugin.delete("append").get("action_status"), "completed")
            seed = app_home / "config/plugins/default_address_book_plugin/entries.json"
            seed.parent.mkdir(parents=True)
            seed.write_text(
                json.dumps(
                    {
                        "entries": [
                            {"name": "skip", "address_type": "path", "entries": unsafe},
                            {"name": "keep", "address_type": "path", "entries": entries},
                        ]
                    }
                )
            )
            with self.assertLogs("path_seed", level="WARNING") as logs:
                auto_seed_entries_from_file(app_home, plugin.name, logging.getLogger("path_seed"), cast(Any, store), None, False)
            self.assertTrue(any("skip" in line and "outside" in line for line in logs.output))
            self.assertEqual(plugin.resolve("keep").get("action_status"), "completed")
            self.assertEqual(plugin.resolve("skip").get("action_status"), "error")

    def test_update_consumes_write_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = MemoryState()
            plugin = DefaultAddressBookPlugin({"auto_ingest_to_memory": False})
            plugin.state_service = cast(Any, store)
            plugin.orchestrator_ref = cast(Any, SimpleNamespace(APP_HOME=str(root / "clone/profile")))
            entries = [{"field_type": "path", "description": "Directory", "value": str(root / "files")}]
            self.assertEqual(plugin.register("result-check", "path", "Directory", entries).get("action_status"), "completed")
            entry_id = cast(dict[str, Any], plugin.resolve("result-check").get("data"))["entries"][0]["id"]
            store.fail_table = "address_entry"
            failed = plugin.update_entry(entry_id, value=str(root / "corrected"))
            self.assertEqual(failed.get("action_status"), "error", "failed entry write must be surfaced")
            self.assertEqual(store.tables["address_entry"][0]["value"], str(root / "files"))
            store.fail_table = "address"
            saved = plugin.update_entry(entry_id, value=str(root / "corrected"))
            self.assertEqual(saved.get("action_status"), "completed")
            self.assertEqual(store.tables["address_entry"][0]["value"], str(root / "corrected"))
            self.assertTrue(cast(dict[str, Any], saved.get("data")).get("advisories"), "timestamp failure must be an advisory after a successful entry write")

    def test_complete_listing_with_entries_and_advisories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = MemoryState()
            plugin = DefaultAddressBookPlugin({"auto_ingest_to_memory": False})
            plugin.state_service = cast(Any, store)
            plugin.orchestrator_ref = cast(Any, SimpleNamespace(APP_HOME=str(root / "clone/profile")))
            for number in range(25):
                entries = [{"field_type": "path", "description": "Directory", "value": str(root / "files" / str(number))}]
                self.assertEqual(plugin.register(f"project_{number}", "path", "Directory", entries, [f"project:{number}"]).get("action_status"), "completed")
            listed = cast(dict[str, Any], plugin.list_path_addresses().get("data"))
            self.assertEqual(listed["count"], 25)
            self.assertEqual(len(listed["addresses"]), 25)
            self.assertTrue(all(row["entries"][0]["value"] and row["tags"] for row in listed["addresses"]))
            self.assertEqual(len(path_addresses(plugin).value), 25)
            self.assertEqual(plugin.register("malformed", "path", "no path field", [{"field_type": "note", "description": "note", "value": "not a path"}]).get("action_status"), "completed")
            located = path_addresses(plugin)
            self.assertEqual(len(located.value), 25)
            self.assertTrue(any("malformed" in advisory for advisory in located.advisories))
            store.tables["address"][-1]["is_deleted"] = 1
            self.assertEqual(cast(dict[str, Any], plugin.list_path_addresses().get("data"))["count"], 25)


if __name__ == "__main__":
    unittest.main()
