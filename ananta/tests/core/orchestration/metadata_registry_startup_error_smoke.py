"""Registry startup diagnostics retain safe causes instead of blaming enabled flags."""

from __future__ import annotations

import os
import sys
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "ananta/src"))

from ananta.core.orchestration import startup_sequence as startup  # noqa: E402
from ananta.core.orchestration.service_manager import ServiceManager  # noqa: E402
from ananta.platform.platform_services_manager import PlatformServicesManager  # noqa: E402


class MetadataRegistryStartupErrorTests(unittest.TestCase):
    def test_registry_cause_survives_service_adapter_and_startup(self) -> None:
        original = ValueError("registry component missing at /fixture/app")
        manager = ServiceManager()
        manager.app_home = "/fixture"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                PlatformServicesManager,
                "_initialize_unified_metadata_registry",
                side_effect=original,
            ),
            self.assertLogs(level="ERROR") as logs,
        ):
            manager._initialize_platform_services()
            orch = SimpleNamespace(
                service_manager=manager,
                APP_HOME="/fixture",
                unified_metadata_registry=None,
                **dict.fromkeys(
                    (
                        "plugin_manager",
                        "state_service",
                        "state_manager",
                        "async_job_manager",
                        "event_bus",
                        "event_handler_registry",
                        "action_recorder",
                        "session_manager",
                        "flow_manager",
                        "blob_storage_service",
                        "discovery_service",
                        "memory_service",
                        "knowledge_service",
                        "io_interface_service",
                    )
                ),
            )
            manager.inference_service = None
            manager.embedding_service = None
            manager.vector_service = None
            with (
                patch(
                    "ananta.core.orchestration.action_coordinator.ActionCoordinator.initialize_action_components",
                    side_effect=RuntimeError("Unified metadata registry not available. Feature flags must enable NewTemplateEngine."),
                ),
                self.assertRaises(startup.StartupError) as caught,
            ):
                startup.StartupSequenceRunner(
                    [
                        startup.StartupStep("init_actions", startup._init_actions, []),
                    ]
                ).run(orch)
        error = caught.exception
        self.assertIn("ValueError: registry component missing at /fixture/app", str(error))
        self.assertIn("ANANTA_USE_METADATA_REGISTRY=True", str(error))
        self.assertIn("ANANTA_USE_NEW_TEMPLATE_ENGINE=True", str(error))
        self.assertEqual(error.step_name, "init_actions")
        self.assertIsNotNone(error.__cause__)
        self.assertIn("ValueError: registry component missing", "".join(traceback.format_exception(error)))
        initializer_logs = [record for record in logs.records if record.name.endswith("platform_services_manager")]
        self.assertEqual(len(initializer_logs), 1)
        self.assertIn("ValueError: registry component missing", initializer_logs[0].getMessage())

    def test_credentials_are_absent_from_logs_cause_and_traceback(self) -> None:
        secret = "synthetic-registry-credential"
        original = RuntimeError(f"registry /fixture/app password='{secret}' dsn=postgresql://operator:{secret}@localhost/db; Bearer {secret}")
        manager = PlatformServicesManager()
        with (
            patch.dict(os.environ, {"REGISTRY_SECRET": secret}, clear=True),
            patch.object(manager, "_initialize_unified_metadata_registry", side_effect=original),
            self.assertLogs("ananta.platform.platform_services_manager", level="ERROR") as logs,
        ):
            self.assertFalse(manager.initialize())
        safe_error = manager.initialization_error
        assert safe_error is not None
        self.assertIsNot(safe_error, original)
        self.assertIsNone(safe_error.__context__)
        orch = SimpleNamespace(service_manager=SimpleNamespace(platform_services_manager=manager))
        with self.assertRaises(startup.StartupError) as caught:
            startup._init_actions(orch)
        error = caught.exception
        self.assertIs(error.__cause__, safe_error)
        for text in (
            str(error),
            str(error.__cause__),
            "".join(traceback.format_exception(error)),
            *logs.output,
        ):
            self.assertNotIn(secret, text)
            self.assertIn("RuntimeError", text)
        self.assertIn("/fixture/app", str(error))
        self.assertIn("[REDACTED]", str(error))

    def test_credentials_without_environment_values_are_redacted(self) -> None:
        from ananta.platform.platform_services_manager import _safe_initialization_error

        values = (
            "quoted password with spaces",
            "url-credential",
            "bearer-credential",
            "key-credential",
            "access-credential",
        )
        original = RuntimeError(f"registry /fixture/app password='{values[0]}' postgresql://operator:{values[1]}@localhost/db Bearer {values[2]} api_key={values[3]} access_token={values[4]}")
        with patch.dict(os.environ, {}, clear=True):
            safe = _safe_initialization_error(original)
        for value in values:
            self.assertNotIn(value, str(safe))
        self.assertIn("registry /fixture/app", str(safe))
        self.assertEqual(str(safe).count("[REDACTED]"), 5)

    def test_url_credentials_are_absent_from_all_startup_surfaces(self) -> None:
        cases = (
            ("https://userinfo-token@example.invalid/metadata", ("userinfo-token",)),
            (
                "https://operator:userinfo-password@example.invalid/metadata",
                ("operator", "userinfo-password"),
            ),
            ("https://empty-password-user:@example.invalid/metadata", ("empty-password-user",)),
            (
                "https://encoded%2Duserinfo%40credential@example.invalid/metadata",
                ("encoded%2Duserinfo%40credential",),
            ),
            (
                "https://example.invalid/metadata?token=query%2Dtoken&access_token=query-access&api_key=query-key&password=query-password&secret=query-secret&component=registry",
                ("query%2Dtoken", "query-access", "query-key", "query-password", "query-secret"),
            ),
            (
                "https://example.invalid/metadata?access%5Ftoken=encoded%2Dquery&component=registry",
                ("encoded%2Dquery",),
            ),
        )
        for url, credentials in cases:
            with self.subTest(url=url):
                original = ValueError(f"registry component /fixture/app unavailable: {url}")
                manager = PlatformServicesManager()
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch.object(manager, "_initialize_unified_metadata_registry", side_effect=original),
                    self.assertLogs("ananta.platform.platform_services_manager", level="ERROR") as logs,
                ):
                    self.assertFalse(manager.initialize())
                orch = SimpleNamespace(service_manager=SimpleNamespace(platform_services_manager=manager))
                with self.assertRaises(startup.StartupError) as caught:
                    startup._init_actions(orch)
                surfaces = (
                    *logs.output,
                    str(caught.exception),
                    "".join(traceback.format_exception(caught.exception)),
                )
                for surface in surfaces:
                    for credential in credentials:
                        self.assertNotIn(credential, surface)
                    self.assertIn("ValueError", surface)
                    self.assertIn("registry component /fixture/app", surface)
                    self.assertIn("example.invalid/metadata", surface)
                    if "component=registry" in url:
                        self.assertIn("component=registry", surface)

    def test_false_flags_are_reported_without_legacy_fallback(self) -> None:
        from ananta.core.actions.action_manager import ActionManager

        with (
            patch.dict(
                os.environ,
                {
                    "ANANTA_USE_METADATA_REGISTRY": "false",
                    "ANANTA_USE_NEW_TEMPLATE_ENGINE": "false",
                },
                clear=True,
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            ActionManager._initialize_template_engine(SimpleNamespace(), None, None, None)  # type: ignore[arg-type]
        self.assertIn("ANANTA_USE_METADATA_REGISTRY=False", str(caught.exception))
        self.assertIn("ANANTA_USE_NEW_TEMPLATE_ENGINE=False", str(caught.exception))
        self.assertIn("prohibited", str(caught.exception))

    def test_successful_retry_clears_failure_snapshot(self) -> None:
        manager = PlatformServicesManager()
        with patch.object(manager, "_initialize_unified_metadata_registry", side_effect=ValueError("missing app")):
            with self.assertLogs("ananta.platform.platform_services_manager", level="ERROR"):
                self.assertFalse(manager.initialize())
        with (
            patch.object(manager, "_initialize_unified_metadata_registry"),
            patch.object(manager, "_initialize_optional_services"),
        ):
            self.assertTrue(manager.initialize())
        self.assertIsNone(manager.initialization_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
