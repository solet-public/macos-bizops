"""A denied macOS protected folder advises and continues to the next rung."""

from __future__ import annotations

import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))

from ananta.file_locations.resolution import LocationRequest, Purpose, Rung, SoletTree, resolve_location  # noqa: E402


class ProtectedFolderAdvisorySmoke(unittest.TestCase):
    def test_denial_advises_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            launch = home / "project"
            launch.mkdir()
            tree = SoletTree(home / "Solets/demo", home / "Solets/demo/profile")
            is_directory = Path.is_dir
            for folder in ("Documents", "Desktop", "Downloads"):
                denied = home / folder / "reports"
                denied.mkdir(parents=True)
                for code in (errno.EPERM, errno.EACCES):

                    def denied_directory(path: Path, *, denied: Path = denied, code: int = code) -> bool:
                        if path == denied:
                            raise PermissionError(code, "simulated macOS denial", str(path))
                        return is_directory(path)

                    with self.subTest(folder=folder, code=code), patch("ananta.file_locations.resolution.sys.platform", "darwin"), patch.object(Path, "home", return_value=home), patch.object(Path, "is_dir", denied_directory):
                        result = resolve_location(LocationRequest(str(denied), None, None, Purpose.FILE, str(launch)), (), tree, home / "files")
                        self.assertEqual(result.directory, launch)
                        self.assertEqual(result.rung, Rung.LAUNCH_DIRECTORY)
                        message = " ".join(result.advisories)
                        for text in ("System Settings", "Privacy & Security", "Files and Folders", sys.executable, folder):
                            self.assertIn(text, message)
                        terminal = resolve_location(LocationRequest(None, None, None, Purpose.FILE, None), (), tree, denied)
                        self.assertEqual(terminal.directory, denied)
                        self.assertEqual(terminal.rung, Rung.SOLET_HOME)
                        self.assertIn("Files and Folders", " ".join(terminal.advisories))


if __name__ == "__main__":
    unittest.main()
