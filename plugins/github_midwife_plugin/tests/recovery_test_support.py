"""Disposable Git fixtures shared by self-contained recovery smokes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON = Path(os.environ.get("RECOVERY_TEST_COMMON", str(REPO_ROOT / "plugins/github_midwife_plugin/coordination_hooks_common")))
sys.path.insert(0, str(COMMON))
import recovery_git as git_module  # noqa: E402  # pyright: ignore[reportMissingImports]
import recovery_refs as ref_module  # noqa: E402  # pyright: ignore[reportMissingImports]
import recovery_rules as rules_module  # noqa: E402  # pyright: ignore[reportMissingImports]
import working_tree_recovery as recovery  # noqa: E402  # pyright: ignore[reportMissingImports] - standalone sibling modules

Json = dict[str, Any]
CANARY = b"C4_IGNORED_GITIGNORE_CANARY_6e34d91b"
SECRET = b"C4_IGNORED_ENV_CANARY_1f8bf6be"


def git(folder: Path, *arguments: str, check: bool = True) -> bytes:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost", "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost"})
    result = subprocess.run(["git", *arguments], cwd=folder, env=environment, input=b"", capture_output=True, check=False)
    if check:
        assert result.returncode == 0, (arguments, result.stdout, result.stderr)
    return result.stdout


def layout(folder: Path) -> dict[str, tuple[int, bytes | str]]:
    result: dict[str, tuple[int, bytes | str]] = {}
    for path in folder.rglob("*"):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            result[str(path.relative_to(folder))] = (stat.S_IMODE(info.st_mode), path.read_bytes())
        elif path.is_symlink():
            result[str(path.relative_to(folder))] = (stat.S_IMODE(info.st_mode), os.readlink(path))
    return result


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root, self.repo, self.store, self.rules = root, root / "repo", root / "store", root / "rules"
        self.repo.mkdir()
        self.store.mkdir(mode=0o700)
        self.rules.mkdir(mode=0o700)
        self.configure()
        git(self.repo, "init", "-b", "main")
        self.write("f", b"base\n")
        self.write(".gitignore", b"hidden/\n")
        git(self.repo, "add", "f", ".gitignore")
        git(self.repo, "commit", "-m", "base")

    def configure(self, **limits: Any) -> None:
        path = self.store / "config.json"
        if path.exists():
            path.chmod(0o600)
        path.write_text(json.dumps({"disk_floor": 0, **limits}))

    def write(self, path: str, data: bytes) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def event(self, name: str = "PreToolUse", command: str | list[str] = "git reset --hard", session: str = "test", **extra: Any) -> recovery.HookEvent:
        return recovery.HookEvent(tool="codex", payload={"hook_event_name": name, "session_id": session, "turn_id": "turn-1", "cwd": str(self.repo), "tool_name": "Bash", "tool_input": {"command": command}, **extra})

    def handle(self, event: recovery.HookEvent | None = None) -> recovery.HookResult:
        with patch.object(rules_module.RuleWorkspace, "rule_candidates", return_value=(self.rules,)):
            return recovery.handle(event or self.event(), root=self.store)

    def cli(self, *arguments: str, code: int = 0) -> Any:
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = recovery.main(["--root", str(self.store), *arguments])
        assert result == code, (arguments, result, output.getvalue(), error.getvalue())
        return json.loads(output.getvalue()) if output.getvalue() else error.getvalue()

    def manual(self) -> str:
        return self.cli("save", "--folder", str(self.repo))["save_id"]

    def manifest(self, save_id: str) -> Json:
        return self.cli("show", save_id)["manifest"]

    def stash(self, *, nested: bool = False, hidden: bool = False) -> str:
        self.write(".gitignore", b"hidden/\n.env\n")
        self.write(".env", SECRET)
        self.write("f", b"staged\n")
        git(self.repo, "add", "f")
        self.write("f", b"worktree\n")
        self.write("newfile", b"visible\n")
        if nested:
            self.write("sub/.gitignore", b"*.key\n")
            self.write("sub/a.key", SECRET)
            self.write("sub/n.txt", b"nested visible\n")
        if hidden:
            self.write("hidden/.gitignore", CANARY)
        git(self.repo, "stash", "push", "--all", "-m", "fixture stash")
        return git(self.repo, "rev-parse", "refs/stash").decode().strip()

    def assert_no_canaries(self) -> None:
        for path in self.store.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert SECRET not in data and CANARY not in data, path


class RecoveryBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="recovery-smoke-")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))


def errno_value(name: str) -> int:
    import errno

    return int(getattr(errno, name))


def run_tests(test: type[unittest.TestCase]) -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        suite = unittest.TestSuite([test(sys.argv[2])])
    else:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(test)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


__all__ = ("CANARY", "COMMON", "Fixture", "Json", "RecoveryBase", "SECRET", "errno_value", "git", "git_module", "layout", "recovery", "ref_module", "rules_module", "run_tests")
