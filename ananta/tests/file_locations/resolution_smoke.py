"""Injected file-location rung and tree-overlap contract."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / "ananta/src"))

from ananta.file_locations.containment import assert_location_admissible  # noqa: E402
from ananta.file_locations.documents import DocumentsFolder, default_solet_home, documents_folder  # noqa: E402
from ananta.file_locations.resolution import (  # noqa: E402
    LocationRequest,
    PathAddress,
    Purpose,
    Rung,
    SoletTree,
    overlaps_solet_tree,
    resolve_location,
)


class ResolutionSmoke(unittest.TestCase):
    def test_rungs_and_advisories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tree = SoletTree(root / "Solets" / "demo", root / "Solets" / "demo" / "profile")
            folders = [root / name for name in ("explicit", "project", "launch", "home")]
            for folder in folders:
                folder.mkdir()
            explicit, project, launch, home = folders
            now = datetime.now(UTC)
            address = PathAddress("acme", project, frozenset({"acme"}), frozenset(), now)
            specific = PathAddress("reports", explicit, frozenset({"acme"}), frozenset({"report"}), now - timedelta(days=1))
            request = LocationRequest(str(explicit), "acme", "report", Purpose.FILE, str(launch))
            self.assertEqual(resolve_location(request, [address, specific], tree, home).rung, Rung.EXPLICIT)
            request = LocationRequest(None, " ACME ", " Report ", Purpose.FILE, str(launch))
            self.assertEqual(resolve_location(request, [address, specific], tree, home).address_name, "reports")
            request = LocationRequest(None, None, None, Purpose.FILE, str(launch))
            self.assertEqual(resolve_location(request, [], tree, home).rung, Rung.LAUNCH_DIRECTORY)
            for unsafe in (root, tree.repo_root, tree.app_home, tree.app_home / "child"):
                unsafe.mkdir(parents=True, exist_ok=True)
                request = LocationRequest(None, None, None, Purpose.FILE, str(unsafe))
                result = resolve_location(request, [], tree, home)
                self.assertEqual(result.directory, home)
                self.assertEqual(result.rung, Rung.SOLET_HOME)
                self.assertTrue(any("overlap" in advisory for advisory in result.advisories))
            for missing in (None, str(root / "missing")):
                request = LocationRequest(None, None, None, Purpose.EXPORT, missing)
                result = resolve_location(request, [], tree, home)
                self.assertEqual(result.directory, home / "Exports.nosync")
                self.assertTrue(any("launch" in advisory for advisory in result.advisories))
                self.assertTrue(any("solet_home" in advisory for advisory in result.advisories))
            home_address = PathAddress("solet_home", project, frozenset(), frozenset(), now)
            result = resolve_location(request, [home_address], tree, home)
            self.assertEqual(result.directory, project / "Exports.nosync")
            self.assertEqual(result.address_name, "solet_home")

    def test_kind_only_recency_and_unusable_rungs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tree = SoletTree(root / "clone", root / "clone" / "profile")
            root.joinpath("first").mkdir()
            root.joinpath("second").mkdir()
            now = datetime.now(UTC)
            addresses = [
                PathAddress("old", root / "first", frozenset(), frozenset({"report"}), now - timedelta(days=1)),
                PathAddress("new", root / "second", frozenset(), frozenset({"report"}), now),
                PathAddress("other-project", root / "first", frozenset({"other"}), frozenset({"report"}), now + timedelta(days=1)),
            ]
            request = LocationRequest("relative", None, "report", Purpose.FILE, None)
            result = resolve_location(request, addresses, tree, root / "home")
            self.assertEqual(result.address_name, "new")
            self.assertTrue(result.advisories)
            request = LocationRequest(str(tree.app_home), "other", "report", Purpose.FILE, str(root / "second"))
            unsafe = PathAddress("unsafe", tree.app_home, frozenset({"other"}), frozenset(), now)
            result = resolve_location(request, [unsafe], tree, root / "home")
            self.assertEqual(result.rung, Rung.LAUNCH_DIRECTORY)
            self.assertGreaterEqual(len(result.advisories), 2)

    def test_documents_and_collision(self) -> None:
        home = Path("/Users/example")
        documents = documents_folder("darwin", {}, home)
        self.assertEqual(documents.path, home / "Documents")
        self.assertTrue(documents.proven)
        self.assertFalse(documents_folder("linux", {}, home).proven)
        tree = SoletTree(documents.path / "demo", documents.path / "demo" / "profile")
        support = home / "Library/Application Support/demo/Files"
        chosen = default_solet_home("demo", DocumentsFolder(documents.path, "test", True), tree, support)
        self.assertEqual(chosen.value, support)
        self.assertTrue(any(str(support) in advisory for advisory in chosen.advisories))

    def test_terminal_and_casefolded_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "fixture"
            root.mkdir()
            documents = DocumentsFolder(root / "Documents", "fixture", True)
            support = root / "Library/Application Support/demo/Files"
            layouts = (
                SoletTree(root / "Documents", root / "Documents/profile"),
                SoletTree(root, root / "profile"),
                SoletTree(root / "Documents", root),
                SoletTree(root, root.parent / "separate-profile"),
            )
            for tree in layouts:
                chosen = default_solet_home("demo", documents, tree, support)
                assert_location_admissible(chosen.value, tree)
                self.assertTrue(chosen.advisories)
            tree = layouts[1]
            sibling = root.parent / f"{root.name} files"
            sibling.write_text("collision")
            try:
                chosen = default_solet_home("demo", documents, tree, support)
                self.assertEqual(chosen.value, root.parent / f"{root.name} files 2")
                assert_location_admissible(chosen.value, tree)
            finally:
                sibling.unlink()
            for platform in ("darwin", "win32"):
                with patch("ananta.file_locations.resolution.sys.platform", platform):
                    self.assertTrue(overlaps_solet_tree(Path(str(tree.repo_root).upper()) / "DATA", tree))
                    with self.assertRaisesRegex(ValueError, "outside repo_root and app_home"):
                        assert_location_admissible(tree.app_home / "data", tree)
            with patch("ananta.file_locations.resolution.sys.platform", "linux"):
                self.assertFalse(overlaps_solet_tree(Path(str(tree.repo_root).upper()) / "DATA", tree))
            link = root.parent / f"{root.name}-link"
            link.symlink_to(root, target_is_directory=True)
            try:
                self.assertTrue(overlaps_solet_tree(link / "data", tree))
            finally:
                link.unlink()


if __name__ == "__main__":
    unittest.main()
