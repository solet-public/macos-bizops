"""Rendered launchers preserve the user's folder while starting in the clone."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta/src"))

from ananta.file_locations.resolution import LocationRequest, Purpose, Rung, SoletTree, resolve_location  # noqa: E402

TEMPLATES = ROOT / "plugins/github_midwife_plugin/knowledge_base/hydration_templates"


class LauncherLaunchDirectorySmoke(unittest.TestCase):
    def test_rendered_launchers_and_resolution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="launcher-location-") as temporary:
            root = Path(temporary).resolve()
            clone, project, home, binaries = (root / name for name in ("Solets/test", "project folder", "Documents/test", "bin"))
            for folder in (clone, project, home, binaries):
                folder.mkdir(parents=True)
            # Required launcher fixture: the agent records only inherited state.
            stub = '#!/bin/zsh\nprintf "%s\\n%s\\n" "${SOLET_LAUNCH_DIRECTORY:-}" "$PWD"\n'
            for binary in ("claude", "codex"):
                path = binaries / binary
                path.write_text(stub)
                path.chmod(0o700)
            environment = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "SOLET_PERMISSION_POSTURE": "inherit", "SOLET_LAUNCH_DIRECTORY": "/inherited/stale", "CODEX_BIN": str(binaries / "codex"), "test_FLEET_TRANSPORT": "none"}
            tree = SoletTree(clone, clone / "profile")
            for name in ("claude_launcher.template", "codex_launcher.template"):
                rendered = (TEMPLATES / name).read_text().replace("{{CLONE_DIR_ZSH}}", shlex.quote(str(clone))).replace("{{CLONE_DIR}}", str(clone)).replace("{{SOLET_NAME}}", "test")
                for launch_from, expected_rung in ((project, Rung.LAUNCH_DIRECTORY), (clone, Rung.SOLET_HOME), (root, Rung.SOLET_HOME)):
                    with self.subTest(template=name, launch_from=launch_from):
                        result = subprocess.run(["/bin/zsh", "-c", rendered], cwd=launch_from, env=environment, text=True, capture_output=True, timeout=10, check=True)
                        captured, actual_cwd = result.stdout.splitlines()
                        self.assertEqual(captured, str(launch_from), "launcher must overwrite stale inherited capture before cd")
                        self.assertEqual(actual_cwd, str(clone), "launcher must still start the agent in the clone")
                        resolved = resolve_location(LocationRequest(None, None, None, Purpose.FILE, captured), (), tree, home)
                        self.assertEqual(resolved.rung, expected_rung)
                        self.assertEqual(resolved.directory, project if expected_rung == Rung.LAUNCH_DIRECTORY else home)
                        if expected_rung == Rung.SOLET_HOME:
                            self.assertTrue(any("launch from a project folder" in item for item in resolved.advisories))


if __name__ == "__main__":
    unittest.main()
