"""Fresh-home hydration, preservation, and adopter question contract."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / "ananta/src"))
sys.path.insert(0, str(root / "plugins/github_midwife_plugin/src"))

from unittest.mock import MagicMock  # noqa: E402

from ananta.file_locations.addresses import path_addresses  # noqa: E402
from ananta.file_locations.containment import assert_path_allowed, derive_allowed_roots  # noqa: E402
from ananta.file_locations.resolution import SoletTree  # noqa: E402
from github_midwife_plugin.export_root_validation import configure_solet_home  # noqa: E402


class SoletHomeHydrationSmoke(unittest.TestCase):
    def test_default_explicit_and_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "Solets" / "demo"
            clone.joinpath("plugins", "jira_plugin").mkdir(parents=True)
            app_home = clone / "profile"
            config = app_home / "config/plugins/jira_plugin.json"
            config.parent.mkdir(parents=True)
            original = b'{"operator_setting": "preserve bytes", "export_allowed_roots": ["/legacy"]}\n'
            config.write_bytes(original)
            with patch.dict(os.environ, {"HOME": str(root)}):
                result = configure_solet_home(clone, str(app_home), "demo", None)
                self.assertEqual(result.solet_home, root / "Documents" / "demo")
                self.assertTrue(result.solet_home.is_dir())
                self.assertTrue(result.exports.is_dir())
                seed = app_home / "config/plugins/default_address_book_plugin/entries.json"
                before = seed.read_bytes()
                names = {entry["name"] for entry in json.loads(before)["entries"]}
                self.assertEqual(names, {"solet_home", "solet_home_exports"})
                configure_solet_home(clone, str(app_home), "demo", None)
                self.assertEqual(before, seed.read_bytes())
                self.assertEqual(config.read_bytes(), original, "hydration must preserve existing connector bytes")
                self.assertEqual(list(config.parent.glob("*.json")), [config], "no new connector config is written")
                service = MagicMock()
                service.list_path_addresses.return_value = {"action_status": "completed", "data": {"addresses": [{**entry, "updated_at": "2026-11-08T00:00:00+00:00"} for entry in json.loads(before)["entries"]]}}
                addresses = path_addresses(service)
                roots = derive_allowed_roots(addresses.value, None, SoletTree(clone, app_home), result.solet_home)
                output = str(result.exports / "out.tsv")
                admitted = assert_path_allowed(output, roots, SoletTree(clone, app_home), suffix=".tsv", param_name="output_path", plugin_name="test")
                self.assertEqual(admitted.value, output)
                self.assertIn(result.exports, roots.value)
                other = configure_solet_home(clone, str(clone / "other-profile"), "other", str(root / "Chosen"))
                self.assertEqual(other.solet_home, root / "Chosen")
                configure_solet_home(clone, str(app_home), "demo", str(root / "Replacement"))
                self.assertEqual(before, seed.read_bytes(), "existing reserved names must be preserved")
                unsafe = configure_solet_home(clone, str(clone / "unsafe-profile"), "safe", str(clone))
                self.assertTrue(unsafe.advisories)
                self.assertEqual(unsafe.solet_home, root / "Documents" / "safe")

    def test_documents_root_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            clone = home / "Documents"
            app_home = clone / "profile"
            with patch.dict(os.environ, {"HOME": str(home)}):
                result = configure_solet_home(clone, str(app_home), "demo", None)
            expected = home / "Library/Application Support/demo/Files"
            self.assertEqual(result.solet_home, expected)
            self.assertTrue(expected.is_dir())
            self.assertTrue(any(str(expected) in advisory for advisory in result.advisories))

    def test_runbook_question(self) -> None:
        runbook = Path(__file__).resolve().parents[1] / "knowledge_base/01_hydration_runbook.md"
        step = runbook.read_text().split("## Step 4b", 1)[1].split("## Step 4c", 1)[0]
        self.assertNotIn("parent directory, not any one project", step)
        self.assertIn("configure_solet_home", step)
        self.assertIn("Is that okay", step)


if __name__ == "__main__":
    unittest.main()
