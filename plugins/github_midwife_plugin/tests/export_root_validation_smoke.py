"""Hydration uses one shared tree guard and writes addresses, never root configs."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta/src"))
sys.path.insert(0, str(ROOT / "plugins/github_midwife_plugin/src"))

from github_midwife_plugin import export_root_validation  # noqa: E402
from github_midwife_plugin.export_root_validation import BUSINESS_CONNECTOR_PLUGINS, ExportRootRejectedError, assert_export_root_valid, configure_solet_home  # noqa: E402


class HydrationContainmentSmoke(unittest.TestCase):
    def test_shared_guard_refuses_both_roots_and_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "clone"
            app_home = clone / "profile"
            for bad in (root, clone, app_home, app_home / "child", clone / "exports"):
                with self.subTest(path=bad), self.assertRaises(ExportRootRejectedError):
                    assert_export_root_valid(str(bad), str(app_home))
            with self.assertRaisesRegex(ExportRootRejectedError, "ABSOLUTE"):
                assert_export_root_valid("relative", str(app_home))
            self.assertEqual(assert_export_root_valid(str(root / "project"), str(app_home)), str(root / "project"))

    def test_addresses_and_existing_config_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            clone = root / "clone"
            app_home = clone / "profile"
            for name in BUSINESS_CONNECTOR_PLUGINS:
                clone.joinpath("plugins", name).mkdir(parents=True)
            config = app_home / "config/plugins/jira_plugin.json"
            config.parent.mkdir(parents=True)
            original = b'{"legacy_setting": true}\n'
            config.write_bytes(original)
            with patch.dict(os.environ, {"HOME": str(root)}):
                first = configure_solet_home(clone, str(app_home), "demo", None)
                seed = app_home / "config/plugins/default_address_book_plugin/entries.json"
                before = seed.read_bytes()
                second = configure_solet_home(clone, str(app_home), "demo", None)
            self.assertEqual(first.solet_home, second.solet_home)
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(list(config.parent.glob("*.json")), [config])
            self.assertEqual(seed.read_bytes(), before)
            self.assertEqual({row["name"] for row in json.loads(before)["entries"]}, {"solet_home", "solet_home_exports"})
            self.assertFalse(hasattr(export_root_validation, "configure_export_root"))


if __name__ == "__main__":
    unittest.main()
