"""Regression coverage for default tracked debt, keyword keys and source citations."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quality_gates import wint2_driver_import_check as drivers  # noqa: E402
from quality_gates import wint2_vault_key_declaration_check as vault  # noqa: E402
from quality_gates.allowlist_schema import load_allowlist  # noqa: E402
from quality_gates.gate_smoke_register import load as load_register  # noqa: E402


class DefaultDebt(unittest.TestCase):
    def test_default_driver_register_suppresses_only_listed_findings(self) -> None:
        path = "fixture.py"
        listed = drivers.Finding(drivers.CHECK_ID, path, "29::psycopg", "listed")
        new = drivers.Finding(drivers.CHECK_ID, path, "30::psycopg", "new")
        output = io.StringIO()
        keys = frozenset({f"{drivers.CHECK_ID}::{path}::29::psycopg"})
        with patch.object(drivers, "_load_tagged_allowlist", return_value=keys) as loader, patch.object(drivers, "collect_findings", return_value=[listed, new]), contextlib.redirect_stdout(output):
            self.assertEqual(drivers.main(["--json"]), 1)
        loader.assert_called_once_with(ROOT / "quality_gates/wint2_driver_import_allowlist.txt")
        report = json.loads(output.getvalue())
        self.assertEqual((report["allowlisted"], report["blocking"]), (1, 1))
        self.assertEqual([row["allowlisted"] for row in report["findings"]], [True, False])

    def test_default_vault_register_suppresses_wildcard_debt(self) -> None:
        path = "fixture.py"
        listed = vault.Finding(vault.CHECK_ID, path, "307::retrieve::dynamic", "listed")
        new = vault.Finding(vault.CHECK_ID, "other.py", "1::retrieve::dynamic", "new")
        output = io.StringIO()
        keys = frozenset({f"{vault.CHECK_ID}::{path}::*"})
        with patch.object(vault, "_load_tagged_allowlist", return_value=keys) as loader, patch.object(vault, "collect_findings", return_value=[listed, new]), contextlib.redirect_stdout(output):
            self.assertEqual(vault.main(["--json"]), 1)
        loader.assert_called_once_with(ROOT / "quality_gates/wint2_vault_key_declaration_allowlist.txt")
        report = json.loads(output.getvalue())
        self.assertEqual((report["allowlisted"], report["blocking"]), (1, 1))

    def test_explicit_register_override_and_empty_register(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            register = Path(temporary) / "debt.txt"
            register.write_text("# deliberately empty\n", encoding="utf-8")
            for gate in (drivers, vault):
                with self.subTest(gate=gate.CHECK_ID):
                    finding = gate.Finding(gate.CHECK_ID, "fixture.py", "1::dynamic", "new")
                    output = io.StringIO()
                    with patch.object(gate, "collect_findings", return_value=[finding]), contextlib.redirect_stdout(output):
                        self.assertEqual(gate.main(["--json", "--allowlist", str(register)]), 1)
                    self.assertEqual(json.loads(output.getvalue())["allowlisted"], 0)

    def test_matcher_retains_check_path_and_specifier_boundaries(self) -> None:
        for gate in (drivers, vault):
            with self.subTest(gate=gate.CHECK_ID):
                register = gate.Allowlist(frozenset({gate.AllowlistEntry(gate.CHECK_ID, "fixture.py", "1::value")}))
                self.assertTrue(register.covers(gate.Finding(gate.CHECK_ID, "fixture.py", "1::value", "listed")))
                for check, path, specifier in (("other", "fixture.py", "1::value"), (gate.CHECK_ID, "other.py", "1::value"), (gate.CHECK_ID, "fixture.py", "2::value")):
                    self.assertFalse(register.covers(gate.Finding(check, path, specifier, "new")))


class KeywordKeys(unittest.TestCase):
    def test_keyword_and_positional_constant_keys_have_the_same_classification(self) -> None:
        source = "\n".join(
            (
                "from typing import Final",
                'KEY: Final[str] = "fixture.token"',
                "ALIAS = KEY",
                'vault_service.store(KEY, "value")',
                'vault_service.store(key=KEY, value="value")',
                "vault_service.retrieve(key=ALIAS)",
                'vault_service.delete(key="fixture.token")',
                "vault_service.retrieve(key=unknown)",
                'vault_service.retrieve(key="undeclared.token")',
            )
        )
        source_lines = list[str](source.splitlines())
        hits = vault._scan_module_for_vault_calls(ast.parse(source), {"fixture.token"}, source_lines)
        self.assertEqual([hit[2] for hit in hits], ["ok-literal"] * 4 + ["unresolved", "undeclared"])
        self.assertEqual([hit[3] for hit in hits[:4]], ["fixture.token"] * 4)


class SmokeMembership(unittest.TestCase):
    def test_optional_client_smoke_has_one_explicit_disposition(self) -> None:
        registered = load_register(ROOT / "quality_gates/gate_smokes.txt")
        excluded = load_allowlist(ROOT / "quality_gates/gate_smoke_exclusions.txt")
        target = "plugins/github_midwife_plugin/tests/codex_optional_smoke.py"
        self.assertNotEqual(target in registered.entries, target in excluded, "optional client smoke must be registered or deliberately excluded, exclusively")


if __name__ == "__main__":
    unittest.main()
