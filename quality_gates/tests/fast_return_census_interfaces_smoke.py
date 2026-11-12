"""U4 discovery-contract smoke; synthetic services never leave this file.

Run: PYTHONPATH=src .venv/bin/python quality_gates/tests/fast_return_census_interfaces_smoke.py

All thirty Appendix B and every R6 handler present in the profile are
inspected, never invoked. Absent plugin legs print named SKIP rows; the
stdlib/quality-gate proof and parameter contracts always run.
Only public setup helpers and the four guarded macOS bridge verbs execute.
No production state, model download, native process or vendor is contacted.
"""

from __future__ import annotations

import importlib
import pkgutil
import socket
import sys
import threading
import unittest
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / "ananta/src"), str(ROOT / "solet_setup_contracts/src"), str(ROOT), *(str(path) for path in sorted(ROOT.glob("plugins/*/src")))]

import fast_return_census as census  # noqa: E402
from fast_return_census_fixtures import smoke_scratch  # noqa: E402


def _discover() -> dict[str, ModuleType]:
    return {module.name: importlib.import_module(f"fast_return_census_preparers.{module.name}") for module in pkgutil.iter_modules([str(ROOT / "quality_gates/tests/fast_return_census_preparers")]) if module.name in {"interfaces", "residual"}}


class _Synthetic:
    """Source-only handlers; the prover must never invoke them."""

    jobs: Any
    handler: Any

    def queued(self) -> Any:
        return self.jobs.create_job(action_name="synthetic")

    def inline(self) -> str:
        return Path("never-opened").read_text()

    def dynamic(self) -> Any:
        return self.handler()

    def deferred(self) -> None:
        threading.Thread(target=lambda: Path("never-opened").read_text()).start()

    def constructed(self) -> object:
        return threading.Thread(target=lambda: Path("never-opened").read_text())

    def eager_argument(self) -> None:
        threading.Thread(target=print, args=(Path("never-opened").read_text(),)).start()

    def unknown_before_enqueue(self) -> Any:
        self.handler()
        return self.jobs.create_job(action_name="synthetic")

    def eager_default(self) -> None:
        # This intentionally unsafe default is inspected, never executed.
        threading.Thread(target=lambda value=Path("never-opened").read_text(): value).start()  # noqa: B008

    def reassigned_thread(self) -> None:
        worker = threading.Thread(target=print)
        worker = self.handler
        worker.start()

    def enqueue(self) -> str:
        return Path("never-opened").read_text()

    def named_like_primitive(self) -> str:
        return self.enqueue()


class _SmokeState:
    """Binding helper stand-in, used only to test the contract before U1."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.operations: list[str] = []

    def read_state(self, namespace: str, query: Mapping[str, Any]) -> dict[str, Any]:
        self.operations.append("read")
        filters = query.get("filters", {})
        rows = [row for row in self.records if row["namespace"] == namespace and all(row.get(key) == value for key, value in filters.items())]
        return {"action_status": "completed", "data": {"records": rows}}

    def write_state(self, namespace: str, data: Mapping[str, Any]) -> dict[str, Any]:
        self.operations.append("write")
        identity = f"binding-{len(self.records)}"
        self.records.append(dict(data["record"], namespace=namespace, id=identity))
        return {"action_status": "completed", "data": {"id": identity}}

    def update_state(self, namespace: str, query: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
        self.operations.append("update")
        for row in self.read_state(namespace, query)["data"]["records"]:
            row.update(updates)
        return {"action_status": "completed", "data": {}}


class _SmokeSessions:
    def __init__(self) -> None:
        self.created = 0

    def create_session(self, namespace: str, context_type: str) -> str:
        self.created += 1
        return f"{namespace}-{context_type}-{self.created}"


class _SmokeContext:
    """Synthetic context view: preparers receive only their public contract."""

    def __init__(self, home: Path) -> None:
        self.app_home = home
        self.export_root = home / "exports"
        self.state = _SmokeState()
        self.sessions = _SmokeSessions()
        self.services: dict[str, object] = {"state_service": self.state, "session_manager": self.sessions}

    def get_service(self, name: str) -> object | None:
        return self.services.get(name)

    def store_blob(self, namespace: str, filename: str, content: bytes, mime_type: str) -> str:
        raise AssertionError("interface setup does not need blobs")


def _plugin_present(plugin: str) -> bool:
    directory = ROOT / "plugins" / plugin
    return (directory / "pyproject.toml").is_file() and (directory / "src" / plugin).is_dir()


def _require_plugin(plugin: str) -> None:
    if not _plugin_present(plugin):
        reason = f"SKIP plugin_optional[{plugin}]: source/manifest absent from this profile"
        print(reason)
        raise unittest.SkipTest(reason)


def _class_for(plugin: str) -> type[Any]:
    _require_plugin(plugin)
    entry = next(entry for entry in census.manifest_entries(ROOT) if entry.plugin == plugin)
    module, attribute = entry.target.split(":", 1)
    return getattr(importlib.import_module(module), attribute)


def _actions(cls: type[Any], plugin: str) -> dict[str, Any]:
    from ananta.core.process_registry.registration_census import class_registration_census

    return {action.name: action for action in class_registration_census(cls, plugin).actions}


class InterfaceProofSmoke(unittest.TestCase):
    inventory: ClassVar[list[census.PluginVerbs]]

    @classmethod
    def setUpClass(cls) -> None:
        """Enter the real caller's path isolation before any guarded import."""
        with smoke_scratch(ROOT) as scratch:
            cls.inventory = census._census_inventory(ROOT, scratch, census.Settings())

    def test_handler_state_preserves_unowned_defaults(self) -> None:
        from fast_return_census_context import prepare_handler_state

        self.assertEqual(prepare_handler_state(None, "read"), {"session_id": "census-session", "flow_id": "census-flow"})
        module = _discover()["interfaces"]
        state = {"session_id": "original", "flow_id": "original-flow"}
        self.assertEqual(module.handler_state("other_plugin", "read", state, {}), state)
        self.assertEqual(module.handler_state("agent_messaging_plugin", "list_sessions", state, {}), state)

    def test_platform_rows_through_real_census_contract(self) -> None:
        from quality_gates.private_test_pg import MissingFixtureError, load_fixture

        try:
            load_fixture("fast_return_census")
        except MissingFixtureError as exc:
            self.skipTest(f"SKIP private_pg[fast_return_census]: {exc}")
        representatives = {
            "pgvector_service_plugin": {"get_namespace_stats_action", "get_vector_action", "update_metadata_action"},
            "actr_memory_plugin": {"focus_action", "forget_action", "reinforce_action", "unfocus_action", "memorize_action", "stop_memorizing_action"},
            "postgres_state_management_plugin": {"read_state", "write_state", "delete_records"},
            "default_address_book_plugin": {"resolve", "delete", "delete_entry", "update_entry"},
            "comfyui_image_generation_plugin": {"check_generation_status"},
        }
        with smoke_scratch(ROOT) as scratch:
            for plugin in self.inventory:
                if plugin.entry.plugin not in representatives:
                    continue
                with self.subTest(plugin=plugin.entry.plugin):
                    _require_plugin(plugin.entry.plugin)
                    assert plugin.plugin_class is not None
                    with census.plugin_context(ROOT, scratch, plugin.entry.plugin, plugin.plugin_class, census.Probe(census.Settings()), census.discover_preparers()) as context:
                        for action in plugin.actions:
                            if action.name in representatives[plugin.entry.plugin]:
                                with self.subTest(verb=action.name):
                                    row = census.measure_verb(ROOT, plugin, action, scratch, census.Settings(), context)
                                    self.assertNotEqual(row["class"], "NOT_MEASURABLE", f"{action.name}: {row['missing_piece']}")

    def test_managed_prerequisites_through_real_census_contract(self) -> None:
        """Exercise the discovered preparer against real isolated services."""
        from quality_gates.private_test_pg import MissingFixtureError, load_fixture

        try:
            load_fixture("fast_return_census")
        except MissingFixtureError as exc:
            self.skipTest(f"SKIP private_pg[fast_return_census]: {exc}")
        _require_plugin("agent_messaging_plugin")
        representatives = {
            "resolve_caller_provenance",
            "report_alive",
            "capture_lane_charter",
            "report_context_status",
            "report_inbox_consumption",
            "managed_dispatch_uptake_rate",
            "read_model_capability_history",
            "read_model_catalog_policy",
            "read_session_usage_window",
            "record_fleet_liveness_run",
            "record_fleet_progress_run",
            "arm_session_dependency",
        }
        with smoke_scratch(ROOT) as scratch:
            plugin = next(item for item in self.inventory if item.entry.plugin == "agent_messaging_plugin")
            assert plugin.plugin_class is not None
            with census.plugin_context(ROOT, scratch, plugin.entry.plugin, plugin.plugin_class, census.Probe(census.Settings()), census.discover_preparers()) as context:
                for action in plugin.actions:
                    if action.name not in representatives | {"read_lane_evidence"}:
                        continue
                    with self.subTest(verb=action.name):
                        row = census.measure_verb(ROOT, plugin, action, scratch, census.Settings(), context)
                        print(f"MANAGED_PREREQUISITE {action.name} {row['class']} {row['outcome']}")
                        if action.name == "read_lane_evidence":
                            self.assertNotIn("dispatch_authentication_required", row["outcome"], "N088 must receive a valid principal; later peer-registry prerequisites remain named")
                        else:
                            self.assertNotEqual(row["class"], "NOT_MEASURABLE", f"{action.name}: {row['missing_piece']}")

    def test_proof_results_through_discovery(self) -> None:
        modules = _discover()
        self.assertIn("residual", modules, "the census discovery contract must expose the residual prover")
        prover = modules["residual"].prove
        expected = {
            "queued": "proved_enqueue",
            "inline": "inline_path",
            "dynamic": "inconclusive",
            "deferred": "proved_enqueue",
            "constructed": "inconclusive",
            "eager_argument": "inline_path",
            "unknown_before_enqueue": "inconclusive",
            "eager_default": "inline_path",
            "reassigned_thread": "inconclusive",
            "named_like_primitive": "inline_path",
        }
        for function, result in expected.items():
            with self.subTest(function=function):
                proof = prover(ROOT, _Synthetic, function)
                self.assertEqual(proof["result"], result)
                self.assertTrue(proof["evidence"])
                self.assertTrue(proof["prover"])
                if result == "inconclusive":
                    self.assertNotEqual(proof["result"], "proved_enqueue")

    def test_every_residual_and_interface_row_has_a_proof_or_input(self) -> None:
        modules = _discover()
        residual, interfaces = modules["residual"], modules["interfaces"]
        self.assertEqual(len(residual.RESIDUAL), 30)
        self.assertEqual(Counter(residual.RESIDUAL.values()), {"R1": 6, "R2": 3, "R3": 5, "R4": 3, "R5": 13})
        self.assertEqual(len(interfaces.INTERFACE_VERBS), 27)
        self.assertFalse(set(residual.RESIDUAL) & set(interfaces.INTERFACE_VERBS))
        counts: Counter[str] = Counter()
        with smoke_scratch(ROOT) as scratch:
            probe = census.Probe(census.Settings(delay_seconds=0))
            with census.isolated_boundaries(ROOT, scratch, probe):
                for module in (residual, interfaces):
                    for plugin, verb in module.RESIDUAL:
                        with self.subTest(plugin=plugin, verb=verb):
                            cls = _class_for(plugin)
                            action = _actions(cls, plugin)[verb]
                            # Import setup can probe IPv6 through the existing
                            # socket fake. Only the proof itself must be inert.
                            before_touches = list(probe.touches)
                            before_starts = list(probe.starts)
                            proof = module.prove(ROOT, cls, action.function or action.name)
                            self.assertEqual(probe.touches, before_touches, "static proof invoked a transport")
                            self.assertEqual(probe.starts, before_starts, "static proof started a worker")
                            self.assertIn(proof["result"], {"proved_enqueue", "inline_path", "inconclusive"})
                            self.assertTrue(proof["evidence"])
                            self.assertTrue(proof["prover"])
                            self.assertIn("helper_graph", proof)
                            counts[str(proof["result"])] += 1
                expected = sum(_plugin_present(plugin) for module in (residual, interfaces) for plugin, _verb in module.RESIDUAL)
                self.assertEqual(sum(counts.values()), expected)
        print(f"RESIDUAL_PROOFS={dict(counts)} measured={sum(counts.values())}/52")

    def test_public_channel_helpers_create_and_reuse_bindings(self) -> None:
        interfaces = _discover()["interfaces"]
        with smoke_scratch(ROOT) as scratch:
            probe = census.Probe(census.Settings(delay_seconds=0))
            with census.isolated_boundaries(ROOT, scratch, probe):
                context = _SmokeContext(scratch)
                for plugin in ("discord_plugin", "signal_plugin"):
                    with self.subTest(plugin=plugin):
                        _require_plugin(plugin)
                        first = interfaces.seed(plugin, context)
                        second = interfaces.seed(plugin, context)
                        self.assertTrue(first["session_id"])
                        self.assertEqual(first["session_id"], second["session_id"])
                        self.assertTrue(first["binding_id"])
                        params = interfaces.parameters(plugin, "post_message", {"message": "census"}, first, scratch)
                        self.assertEqual(params["session_id"], first["session_id"])
                expected = sum(_plugin_present(plugin) for plugin in ("discord_plugin", "signal_plugin"))
                self.assertEqual(context.sessions.created, expected)
                self.assertEqual(context.state.operations.count("write"), expected)
                self.assertEqual(context.state.operations.count("update"), expected)
                self.assertEqual(probe.touches, [], "binding setup uses no listener or transport")
                context.services.clear()
                self.assertIn("U1 integration", interfaces.seed("discord_plugin", context)["prerequisite"])

    def test_configuration_parameters_and_r6_are_explicit(self) -> None:
        interfaces = _discover()["interfaces"]
        self.assertEqual(interfaces.RUN_THREADS, {"agent_messaging_plugin-server"})
        self.assertTrue(interfaces.LOOPBACK_LISTENER)
        self.assertEqual(len(interfaces.MEASURE_EXCLUDED), 3)
        self.assertTrue(interfaces.MEASURE_EXCLUDED <= set(interfaces.RESIDUAL))
        with self.assertRaises(unittest.SkipTest):
            _require_plugin("census_missing_optional_plugin")
        with smoke_scratch(ROOT) as scratch:
            for plugin, verb in sorted(interfaces.INTERFACE_VERBS):
                params: dict[str, object] = {"unit_id": "remove-if-instance-selected"}
                prepared = interfaces.parameters(plugin, verb, params, {}, scratch)
                self.assertEqual(prepared, interfaces.parameters(plugin, verb, params, {}, scratch))
                self.assertEqual(params, {"unit_id": "remove-if-instance-selected"})
                if (plugin, verb) in interfaces.RESIDUAL:
                    self.assertEqual(interfaces.RESIDUAL[(plugin, verb)], "R6")
                else:
                    self.assertIn("agent_instance_id", prepared)
            self.assertEqual(interfaces.parameters("unowned", "read", {}, {}, scratch), {})
            config = interfaces.configure("agent_messaging_plugin", scratch, scratch / "exports")
            self.assertEqual((config["host"], config["port"], config["streamable_enabled"]), ("127.0.0.1", 0, False))
            self.assertTrue(str(interfaces.configure("signal_plugin", scratch, scratch)["backend_endpoint"]).endswith(".invalid"))

    def test_guarded_native_bridge_provider_through_contract(self) -> None:
        from quality_gates.private_test_pg import MissingFixtureError, load_fixture

        _require_plugin("macos_coding_agent_session_plugin")
        try:
            load_fixture("fast_return_census")
        except MissingFixtureError as exc:
            self.skipTest(f"SKIP private_pg[fast_return_census]: {exc}")
        plugin = next(item for item in self.inventory if item.entry.plugin == "macos_coding_agent_session_plugin")
        assert plugin.plugin_class is not None
        with smoke_scratch(ROOT) as scratch:
            with census.plugin_context(ROOT, scratch, plugin.entry.plugin, plugin.plugin_class, census.Probe(census.Settings()), census.discover_preparers()) as context:
                for action in plugin.actions:
                    with self.subTest(verb=action.name):
                        row = census.measure_verb(ROOT, plugin, action, scratch, census.Settings(), context)
                        self.assertIn(row["class"], {"FAST_RETURN", "LOCAL_FAST", "INLINE_IO"}, f"{action.name}: {row['missing_piece']}")
                        if action.name == "spawn_bridge":
                            self.assertTrue(row["worker_reachable"], "spawn_bridge must enqueue through the real job service")
                        print(f"INTERFACE_CONTRACT_CLASS {action.name} {row['class']}")

    def test_usage_input_reaches_named_advisory_class(self) -> None:
        interfaces = _discover()["interfaces"]
        with smoke_scratch(ROOT) as scratch:
            probe = census.Probe(census.Settings(delay_seconds=0))
            with census.isolated_boundaries(ROOT, scratch, probe):
                instance = _class_for("agent_messaging_plugin")()
                params = interfaces.parameters("agent_messaging_plugin", "read_unit_usage", {"unit_id": "remove"}, {}, scratch)
                result = instance.read_unit_usage(params, {})
                self.assertEqual(result["action_status"], "completed")
                self.assertEqual(result["data"]["coverage"], "none")
                self.assertFalse(census._failure_reason(result))
                self.assertEqual(probe.touches, [])

    def test_listener_guard_refuses_wildcard_and_nonzero_bind(self) -> None:
        # NM-1 itself denies every bind; U1 may permit ONLY 127.0.0.1:0.
        # The bridge's public allocator attempts wildcard binds, so the R6
        # decision does not depend on actually starting Uvicorn or a thread.
        with smoke_scratch(ROOT) as scratch:
            probe = census.Probe(census.Settings(delay_seconds=0))
            with census.isolated_boundaries(ROOT, scratch, probe):
                from ananta.core.runtime import find_available_port

                with self.assertRaises(OSError):
                    find_available_port(preferred=0)
                for address in (("0.0.0.0", 0), ("127.0.0.1", 45123)):
                    with socket.socket() as sock, self.assertRaises(OSError):
                        sock.bind(address)
                self.assertTrue(probe.touches)
                self.assertTrue(all(touch["boundary"] == "socket.socket.bind" for touch in probe.touches))


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    if not result.wasSuccessful():
        raise SystemExit(1)
    if any("private_pg[" in reason for _test, reason in result.skipped):
        from quality_gates.private_test_pg import skip_fixture

        raise SystemExit(skip_fixture())
