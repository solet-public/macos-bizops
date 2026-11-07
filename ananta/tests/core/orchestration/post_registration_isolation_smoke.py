#!/usr/bin/env python3
"""No-database post-registration isolation and served degraded-health contract."""
from __future__ import annotations

import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[4]
for source in ("ananta/src", "plugins/agent_messaging_plugin/src"):
    sys.path.insert(0, str(ROOT / source))

from ananta.core.orchestration import startup_sequence as startup  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin import http_routes  # noqa: E402
from agent_messaging_plugin.local_cli import cli as bridge_cli  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin, _BridgeRuntimeConfig  # noqa: E402


class PostRegistrationTests(unittest.TestCase):
    def run_steps(self, failed: str | None = None) -> tuple[SimpleNamespace, list[str]]:
        calls: list[str] = []

        def step(name: str) -> None:
            calls.append(name)
            if failed == name:
                raise RuntimeError("injected " + name)

        orch = SimpleNamespace(
            plugin_manager=SimpleNamespace(plugins={"openai_embeddings_plugin": SimpleNamespace(
                start_post_registration_qualification=lambda: step("embeddings_qualification"),
                wait_for_post_registration_qualification=lambda: None,
            )}),
            action_coordinator=SimpleNamespace(
                populate_discovery_after_registration=lambda: step("discovery"),
            ),
        )
        with ExitStack() as stack:
            for function, name in (("_seed_identity_memories", "identity_seed"),
                                   ("_reindex_orphaned_memories", "memory_reindex"),
                                   ("_auto_install_knowledge_bases", "knowledge_auto_install")):
                stack.enter_context(patch.object(startup, function,
                    side_effect=lambda _orch, name=name: step(name)))

            def inference(_orch: Any) -> dict[str, Any]:
                step("inference_start")
                return {"state": "started", "provider": "fixture"}

            stack.enter_context(patch.object(startup, "_start_selected_inference_work",
                                            side_effect=inference))
            startup._run_post_registration_work(orch)
        return orch, calls

    def test_reindex_failure_keeps_independent_work_and_reports(self) -> None:
        with self.assertLogs(startup.logger, level="ERROR") as logs:
            orch, calls = self.run_steps("memory_reindex")
        self.assertIn("knowledge_auto_install", calls)
        self.assertIn("inference_start", calls)
        self.assertEqual(orch.post_registration_work_status["state"], "degraded")
        self.assertEqual(orch.post_registration_work_status["failed_steps"], [
            {"step": "memory_reindex", "error": "injected memory_reindex"},
        ])
        self.assertIn("memory_reindex", " ".join(logs.output))
        self.assertTrue(any(record.exc_info for record in logs.records))

    def test_qualification_failure_skips_dependencies_only(self) -> None:
        with self.assertLogs(startup.logger, level="ERROR"):
            orch, calls = self.run_steps("embeddings_qualification")
        self.assertEqual(calls, ["embeddings_qualification", "discovery", "inference_start"])
        self.assertEqual(orch.post_registration_work_status["state"], "degraded")
        skipped = orch.post_registration_work_status["skipped_steps"]
        self.assertEqual([item["step"] for item in skipped], [
            "identity_seed", "memory_reindex", "knowledge_auto_install",
        ])
        self.assertTrue(all("embeddings_qualification" in item["reason"] for item in skipped))

    def test_each_other_step_failure_is_isolated(self) -> None:
        for failed in ("discovery", "identity_seed", "knowledge_auto_install", "inference_start"):
            with self.subTest(failed=failed), self.assertLogs(startup.logger, level="ERROR"):
                orch, calls = self.run_steps(failed)
            self.assertEqual(len(calls), 6)
            self.assertEqual(orch.post_registration_work_status["failed_steps"][0]["step"], failed)
            self.assertEqual(orch.post_registration_work_status["state"], "degraded")

    def test_all_success_preserves_exact_inference_status(self) -> None:
        orch, calls = self.run_steps()
        self.assertEqual(len(calls), 6)
        self.assertEqual(orch.post_registration_work_status, {"state": "started", "provider": "fixture"})

    def test_partial_reindex_result_is_not_successful_startup(self) -> None:
        orch = SimpleNamespace(memory_service=SimpleNamespace(reindex_orphaned_vectors=lambda: {
            "action_status": "completed", "data": {"reindex_count": 1, "failed_records": [
                {"memory_id": "too-long", "token_count": 3074, "error": "too long"},
            ]},
        }))
        with self.assertRaisesRegex(startup.StartupError, "too-long"):
            startup._reindex_orphaned_memories(orch)

    def test_real_plugin_serves_degraded_and_recovery_without_restart(self) -> None:
        plugin = AgentMessagingPlugin()
        orch = SimpleNamespace(post_registration_work_status={
            "state": "degraded", "failed_steps": [{"step": "memory_reindex", "error": "too long"}],
        })
        plugin.orchestrator_ref = orch
        config = _BridgeRuntimeConfig(streamable_enabled=False)
        with ExitStack() as stack:
            for name in ("_require_service", "_get_state_service", "_maybe_get_session_ledger_service",
                         "_maybe_get_vault_oauth_registry"):
                stack.enter_context(patch.object(plugin, name, return_value=None))
            for name in ("_register_bridge_lifecycle_routes", "_register_platform_surface_routes",
                         "_register_direct_read_routes", "_register_peer_routes"):
                stack.enter_context(patch.object(http_routes, name))
            app = plugin._build_fastapi_app(bridge_manager=Mock(), peer_registry=Mock(),
                                           platform_surface=Mock(), bridge_config=config)
            with TestClient(app) as client:
                response = client.get("/api/v1/bridge/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "degraded")
                self.assertEqual(response.json()["post_registration_work"], orch.post_registration_work_status)
                with patch.object(bridge_cli, "resolve_base_url", return_value="http://fixture"), \
                     patch.object(bridge_cli, "BridgeClient") as client_type:
                    client_type.return_value.health.return_value = response.json()
                    output = CliRunner().invoke(bridge_cli.cli, ["health"])
                self.assertEqual(output.exit_code, 0)
                self.assertEqual(json.loads(output.output)["status"], "degraded")
                orch.post_registration_work_status = {"state": "started", "provider": "fixture"}
                self.assertEqual(client.get("/api/v1/bridge/health").json()["status"], "healthy")


if __name__ == "__main__":
    print("startup module:", startup.__file__, flush=True)
    print("health module:", http_routes.__file__, flush=True)
    unittest.main(verbosity=2)
