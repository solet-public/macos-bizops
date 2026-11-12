"""Injected Documents-folder contracts; only macOS is ladder-proven."""

from __future__ import annotations

import ctypes
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))

from ananta.file_locations import documents  # noqa: E402
from ananta.file_locations.resolution import SoletTree  # noqa: E402


class DocumentsPlatformSmoke(unittest.TestCase):
    def test_windows_ctypes_api_and_release(self) -> None:
        buffer = ctypes.create_unicode_buffer("C:/Users/example/OneDrive/Documents")

        def known_folder(guid: Any, flags: int, token: Any, result: Any) -> int:
            self.assertEqual(ctypes.string_at(guid, 16), UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7").bytes_le)
            self.assertEqual(flags, 0)
            self.assertIsNone(token)
            ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
            return 0

        shell = SimpleNamespace(SHGetKnownFolderPath=Mock(side_effect=known_folder))
        ole = SimpleNamespace(CoTaskMemFree=Mock())
        with patch.object(ctypes, "WinDLL", side_effect=[shell, ole], create=True) as libraries:
            self.assertEqual(documents._windows_documents(), Path(buffer.value))
        self.assertEqual([call.args[0] for call in libraries.call_args_list], ["shell32", "ole32"])
        ole.CoTaskMemFree.assert_called_once()
        self.assertEqual(ole.CoTaskMemFree.call_args.args[0].value, ctypes.addressof(buffer))
        shell.SHGetKnownFolderPath = Mock(return_value=-1)
        ole.CoTaskMemFree.reset_mock()
        with patch.object(ctypes, "WinDLL", side_effect=[shell, ole], create=True), self.assertRaisesRegex(OSError, "SHGetKnownFolderPath"):
            documents._windows_documents()
        ole.CoTaskMemFree.assert_not_called()

    def test_windows_known_folder_and_onedrive(self) -> None:
        home = Path("/Users/example")
        for folder in (home / "Documents", home / "OneDrive - Company/Documents"):
            with self.subTest(folder=folder), patch.object(documents, "_windows_documents", return_value=folder, create=True):
                result = documents.documents_folder("win32", {}, home)
                self.assertEqual(result.path, folder)
                self.assertFalse(result.proven)
                self.assertIn("known folder", result.source.lower())
                tree = SoletTree(home / "Solets/demo", home / "Solets/demo/profile")
                chosen = documents.default_solet_home("demo", result, tree, home / "AppData/Local/demo/Files")
                self.assertEqual(any("OneDrive" in value for value in chosen.advisories), "OneDrive" in str(folder))

    def test_linux_sources_and_macos_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / "config"
            config.mkdir()
            (config / "user-dirs.dirs").write_text('# user folders\nXDG_DOCUMENTS_DIR="$HOME/My Documents"\n', encoding="utf-8")
            cases = (
                ({"XDG_DOCUMENTS_DIR": str(home / "Environment"), "XDG_CONFIG_HOME": str(config)}, home / "Environment", "environment"),
                ({"XDG_CONFIG_HOME": str(config)}, home / "My Documents", "user-dirs.dirs"),
                ({"XDG_CONFIG_HOME": str(home / "absent")}, home / "Documents", "fallback"),
            )
            for environ, expected, source in cases:
                with self.subTest(source=source):
                    result = documents.documents_folder("linux", environ, home)
                    self.assertEqual(result.path, expected)
                    self.assertIn(source, result.source)
                    self.assertFalse(result.proven)
            self.assertTrue(documents.documents_folder("darwin", {}, home).proven)


if __name__ == "__main__":
    unittest.main()
