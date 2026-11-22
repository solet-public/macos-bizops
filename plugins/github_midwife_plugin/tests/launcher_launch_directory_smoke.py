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

    def test_unnamed_claude_launch_is_named_for_its_folder_and_claims_no_role(self) -> None:
        with tempfile.TemporaryDirectory(prefix="launcher-label-") as temporary:
            root = Path(temporary).resolve()
            clone, folder, binaries = (root / name for name in ("Solets/test", "Zed Folder", "bin"))
            for path in (clone, folder, binaries):
                path.mkdir(parents=True)
            # Required launcher fixture: the stub records only what claude inherits.
            stub = '#!/bin/zsh\nprint -r -- "flag=$1"\nprint -r -- "name=$2"\nprint -r -- "label=$AGENT_SESSION_LABEL"\nprint -r -- "autobind=${AGENT_ROLE_AUTOBIND-unset}"\nprint -r -- "role=${AGENT_ROLE-unset}"\n'
            (binaries / "claude").write_text(stub)
            (binaries / "claude").chmod(0o700)
            environment = {key: value for key, value in os.environ.items() if key not in {"AGENT_ROLE", "AGENT_ROLE_AUTOBIND"}}
            environment.update({"PATH": f"{binaries}:{os.environ['PATH']}", "SOLET_PERMISSION_POSTURE": "inherit"})
            rendered = (TEMPLATES / "claude_launcher.template").read_text().replace("{{CLONE_DIR_ZSH}}", shlex.quote(str(clone))).replace("{{CLONE_DIR}}", str(clone)).replace("{{SOLET_NAME}}", "test")

            def launch(launch_from: Path, *arguments: str, stale_role: bool = False) -> dict[str, str]:
                launcher_environment = {**environment, "AGENT_ROLE": "stale-caller-role"} if stale_role else environment
                result = subprocess.run(["/bin/zsh", "-c", rendered, "claude-test", *arguments], cwd=launch_from, env=launcher_environment, text=True, capture_output=True, timeout=10, check=True)
                return dict(line.split("=", 1) for line in result.stdout.splitlines())

            unnamed = launch(folder, stale_role=True)
            self.assertEqual(unnamed["flag"], "--name")
            self.assertEqual(unnamed["name"], "Zed-Folder", "an unnamed launch is named for its folder, unsafe characters replaced")
            self.assertEqual(unnamed["label"], "Zed-Folder")
            self.assertEqual(unnamed["autobind"], "0", "an unnamed launch keeps its label as display metadata, not a role claim")
            self.assertEqual(unnamed["role"], "unset", "an unnamed launch drops an inherited role so the bridge claims none")
            at_root = launch(Path("/"))
            self.assertEqual((at_root["name"], at_root["autobind"]), ("session", "0"), "the filesystem root has no folder name")
            named = launch(folder, "MyName")
            self.assertEqual((named["name"], named["label"]), ("MyName", "MyName"))
            self.assertEqual(named["autobind"], "unset", "a named launch keeps the person's own role claim")


if __name__ == "__main__":
    unittest.main()
