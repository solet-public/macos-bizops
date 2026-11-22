"""Exercise Claude cloud credential resolution without a real Keychain."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
for _plugin in ("claude_code_cloud_session_source_plugin", "macos_vault_plugin"):
    sys.path.insert(0, str(_ROOT / "plugins" / _plugin / "src"))

from ananta.core.services.call_context import CallContext, VaultAccessDeniedError  # noqa: E402
from ananta.interfaces.vault_service_interface import VaultServiceInterface  # noqa: E402
from ananta.services.vault_service.interfaces.public import ExternalKeychainReadError, VaultServiceAPI  # noqa: E402
from ananta.services.vault_service.vault_service_proxy import VaultServiceProxy  # noqa: E402
from claude_code_cloud_session_source_plugin.plugin import ClaudeCodeCloudSessionSourcePlugin  # noqa: E402
from claude_code_cloud_session_source_plugin.walker import (  # noqa: E402
    ClaudeCodeCloudWalkerError,
    WalkerConfig,
    WalkerReport,
    load_credentials,
)
from macos_vault_plugin import keychain_interaction  # noqa: E402
from macos_vault_plugin import plugin as vault_module  # noqa: E402

_SERVICE = "Claude Code-credentials"
_ENVELOPE_PATH = "claudeAiOauth.accessToken"
_TOKEN = "fixture-token"
_ENVELOPE = json.dumps({"claudeAiOauth": {"accessToken": _TOKEN}})


class CredentialSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("shutil.which", return_value="/fake/security"))
        self.subprocess_run = self.enterContext(patch("subprocess.run", side_effect=AssertionError("walker attempted a direct Keychain subprocess")))
        self.enterContext(patch.dict(sys.modules, {"Security": None}))
        self.enterContext(patch.object(keychain_interaction, "_security_framework", side_effect=AssertionError("smoke attempted a native Keychain call")))
        self.enterContext(patch.object(vault_module.MacosVaultPlugin, "_init_per_credential_keychain"))
        self.provider = vault_module.MacosVaultPlugin()
        self.cloud = ClaudeCodeCloudSessionSourcePlugin()
        self.proxy = VaultServiceProxy(self.provider, self.cloud.name)

    def tearDown(self) -> None:
        self.subprocess_run.assert_not_called()

    def test_walker_extracts_token_from_the_vault_port(self) -> None:
        reader = Mock(return_value=f"\u00a0{_ENVELOPE}\n")
        credentials = load_credentials(keychain_service=_SERVICE, envelope_path=_ENVELOPE_PATH, read_external_keychain=reader)
        reader.assert_called_once_with(_SERVICE)
        self.assertEqual(credentials.access_token, _TOKEN)
        self.assertNotIn(_TOKEN, repr(credentials))

    def test_walker_reports_missing_and_malformed_envelopes(self) -> None:
        cases = (
            (None, "auth_missing"),
            ("", "auth_malformed"),
            (" \n", "auth_malformed"),
            (f"{{{_TOKEN}", "auth_malformed"),
            ("{}", "auth_malformed"),
            ('{"claudeAiOauth": {"accessToken": 7}}', "auth_malformed"),
            ('{"claudeAiOauth": {"accessToken": ""}}', "auth_malformed"),
        )
        for raw, code in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ClaudeCodeCloudWalkerError) as failure:
                    load_credentials(keychain_service=_SERVICE, envelope_path=_ENVELOPE_PATH, read_external_keychain=Mock(return_value=raw))
                self.assertEqual(failure.exception.code, code)
                self.assertNotIn(_TOKEN, str(failure.exception))

    def test_walker_reports_unreadable_without_provider_diagnostics(self) -> None:
        with (
            patch.object(vault_module, "read_generic_password_by_service", side_effect=RuntimeError(_TOKEN)),
            self.assertRaises(ClaudeCodeCloudWalkerError) as failure,
        ):
            load_credentials(keychain_service=_SERVICE, envelope_path=_ENVELOPE_PATH, read_external_keychain=self.proxy.read_external_keychain)
        self.assertEqual(failure.exception.code, "auth_unreadable")
        self.assertNotIn(_TOKEN, str(failure.exception))
        self.assertTrue(failure.exception.__suppress_context__)

        with self.assertRaises(ClaudeCodeCloudWalkerError) as missing_vault:
            load_credentials(keychain_service=_SERVICE, envelope_path=_ENVELOPE_PATH, read_external_keychain=None)
        self.assertEqual(missing_vault.exception.code, "auth_unreadable")

        unsupported = VaultServiceProxy(Mock(spec=VaultServiceInterface), self.cloud.name)
        with self.assertRaises(ExternalKeychainReadError):
            unsupported.read_external_keychain(_SERVICE)

    def test_vault_rejects_other_callers_and_services_before_reading(self) -> None:
        with patch.object(vault_module, "read_generic_password_by_service") as lookup:
            with self.assertRaises(VaultAccessDeniedError):
                VaultServiceProxy(self.provider, "unrelated_plugin").read_external_keychain(_SERVICE)
            with self.assertRaises(VaultAccessDeniedError):
                self.proxy.read_external_keychain("unrelated-service")
            with self.assertRaises(VaultAccessDeniedError):
                self.provider.read_external_keychain(_SERVICE)
            with self.assertRaises(VaultAccessDeniedError):
                self.provider.read_external_keychain(_SERVICE, call_context=CallContext.for_operator())
        lookup.assert_not_called()
        self.assertFalse(hasattr(VaultServiceAPI, "read_external_keychain"))

    def test_native_lookup_is_service_only_and_cannot_prompt(self) -> None:
        interaction_allowed = True
        native_result: tuple[int, bytes | None] = (0, _ENVELOPE.encode())

        def set_interaction(allowed: bool) -> None:
            nonlocal interaction_allowed
            interaction_allowed = allowed

        def lookup(query: dict[str, object], out: None) -> tuple[int, bytes | None]:
            self.assertIsNone(out)
            self.assertFalse(interaction_allowed)
            self.assertEqual(query, {"class": "generic", "service": _SERVICE, "match_limit": "one", "return_data": True})
            return native_result

        security = Mock(
            kSecClass="class",
            kSecClassGenericPassword="generic",
            kSecAttrService="service",
            kSecMatchLimit="match_limit",
            kSecMatchLimitOne="one",
            kSecReturnData="return_data",
            errSecSuccess=0,
            errSecItemNotFound=-25300,
            SecItemCopyMatching=lookup,
        )
        with (
            patch.dict(sys.modules, {"Security": security}),
            patch.object(keychain_interaction, "user_interaction_allowed", side_effect=lambda: interaction_allowed),
            patch.object(keychain_interaction, "set_user_interaction_allowed", side_effect=set_interaction),
        ):
            self.assertEqual(self.proxy.read_external_keychain(_SERVICE), _ENVELOPE)
            self.assertTrue(interaction_allowed)
            native_result = (-25300, None)
            self.assertIsNone(self.proxy.read_external_keychain(_SERVICE))
            native_result = (-25293, None)
            with self.assertRaises(ExternalKeychainReadError):
                self.proxy.read_external_keychain(_SERVICE)
        self.assertTrue(interaction_allowed)

    def test_backfill_uses_the_injected_proxy(self) -> None:
        self.cloud.set_vault_service(self.proxy)
        config = WalkerConfig(
            keychain_service=_SERVICE,
            keychain_envelope_path=_ENVELOPE_PATH,
            api_base_url="https://fixture.invalid",
            anthropic_version="fixture",
            anthropic_beta="fixture",
            list_page_limit=1,
            events_page_limit=1,
            walker_concurrency=1,
            fetch_timeout_seconds=1,
            rate_limit_backoff_seconds=(),
        )
        run_backfill = AsyncMock(return_value=WalkerReport())
        with (
            patch.object(self.cloud, "_build_walker_config", return_value=config),
            patch.object(self.cloud, "_run_backfill", run_backfill),
            patch.object(vault_module, "read_generic_password_by_service", return_value=_ENVELOPE) as lookup,
        ):
            result = self.cloud._execute_backfill({}, lambda: False, lambda _: None)  # pyright: ignore[reportPrivateUsage]
        lookup.assert_called_once_with(_SERVICE)
        run_backfill.assert_awaited_once()
        assert run_backfill.await_args is not None
        self.assertEqual(run_backfill.await_args.kwargs["creds"].access_token, _TOKEN)
        self.assertEqual(result["status"], "completed")


def main() -> None:
    if not unittest.main(exit=False).result.wasSuccessful():
        raise SystemExit(1)
    print("credentials_smoke OK")


if __name__ == "__main__":
    main()
