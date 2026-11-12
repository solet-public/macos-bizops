"""Public recovery probes; plugins/github_midwife_plugin/tests/recovery_capture_smoke.py.

Self-contained stdlib smoke with disposable repositories and one shared fixture.
"""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from recovery_test_support import COMMON, Fixture, RecoveryBase, git, git_module, layout, recovery, run_tests  # pyright: ignore[reportMissingImports] - retained sibling fixtures


def _subprocess_uses(source: ast.AST) -> list[ast.Attribute]:
    return [node for node in ast.walk(source) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "subprocess"]


def _imports(path: Path) -> list[str | None]:
    source = ast.parse(path.read_text(), feature_version=(3, 11))
    names = [alias.name for node in ast.walk(source) if isinstance(node, ast.Import) for alias in node.names]
    return names + [node.module for node in ast.walk(source) if isinstance(node, ast.ImportFrom)]


class RecoveryTests(RecoveryBase):
    def test_payload_pool_caps_count_only_new_bytes(self) -> None:
        f = self.fixture
        data = b"one copy\n"
        f.configure(cap_bytes=len(data))
        f.write("f", data)
        f.write("g", data)
        git(f.repo, "add", "f", "g")
        first = f.handle()
        self.assertTrue(first.saves, first)
        self.assertEqual(len(list((f.store / "pool").iterdir())), 1)
        f.configure(cap_bytes=0)
        second = f.handle()
        manifest = f.manifest(second.saves[0])
        self.assertEqual({entry["path"] for entry in manifest["entries"]}, {"f", "g"})
        self.assertEqual(manifest["unsaved_counts"], {})

    def test_conflict_index_stages_restore_without_index_refresh(self) -> None:
        f = self.fixture
        git(f.repo, "switch", "-c", "other")
        f.write("f", b"other\n")
        git(f.repo, "add", "f")
        git(f.repo, "commit", "-m", "other")
        git(f.repo, "switch", "main")
        f.write("f", b"main\n")
        git(f.repo, "add", "f")
        git(f.repo, "commit", "-m", "main")
        git(f.repo, "merge", "other", check=False)
        expected = {stage: git(f.repo, "show", f":{stage}:f") for stage in (1, 2, 3)}
        before = layout(f.repo / ".git")
        result = f.handle()
        self.assertEqual(before, layout(f.repo / ".git"))
        manifest = f.manifest(result.saves[0])
        self.assertEqual({entry["stage"] for entry in manifest["entries"] if entry["layer"] == "index"}, {1, 2, 3})
        git(f.repo, "reset", "--hard")
        f.cli("restore", result.saves[0], "--stage")
        self.assertEqual({stage: git(f.repo, "show", f":{stage}:f") for stage in (1, 2, 3)}, expected)

    def test_whole_hook_deadline_interrupts_root_publication_lock(self) -> None:
        import fcntl
        import json

        f = self.fixture
        f.configure(hard_seconds=2, publication_seconds=0.5)
        f.write("f", b"locked publication\n")
        code = """import dataclasses, json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from working_tree_recovery import HookEvent, handle
from recovery_packets import Recovery
original = Recovery.publish
def publish(self, *args, **kwargs):
    print('publication-enter', flush=True)
    return original(self, *args, **kwargs)
Recovery.publish = publish
event = HookEvent('PreToolUse', 'root-lock', cwd=pathlib.Path(sys.argv[2]), command='git restore .', tool_name='Bash')
print(json.dumps(dataclasses.asdict(handle(event, root=pathlib.Path(sys.argv[3])))))
"""
        descriptor = os.open(f.store, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        child = subprocess.Popen([sys.executable, "-c", code, str(COMMON), str(f.repo), str(f.store)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stalled = False
        output, error = b"", b""
        try:
            try:
                output, error = child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stalled = True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            if stalled:
                output, error = child.communicate()
        self.assertFalse(stalled, "publication ignored the shared deadline; fixture lock was released without killing the child")
        self.assertEqual(child.returncode, 0, error)
        self.assertIn(b"publication-enter", output)
        result = json.loads(output.splitlines()[-1])
        self.assertIn("deadline", result["notice"])
        self.assertIn("command will run", result["notice"])

    def test_one_read_retry_then_named_unreadable(self) -> None:
        for rewrites in (1, 2):
            f = self.fixture
            f.write("f", b"old read\n")
            original = recovery.store.read_entry
            calls = 0

            def changing(root: Path, relative: str, f: Fixture = f, rewrites: int = rewrites, original: Any = original, **kwargs: Any) -> Any:
                nonlocal calls
                result = original(root, relative, **kwargs)
                if root.resolve() == f.repo.resolve() and relative == "f" and kwargs.get("digest"):
                    calls += 1
                    if calls <= rewrites:
                        f.write("f", f"rewrite-{calls}\n".encode())
                return result

            with patch.object(recovery.store, "read_entry", side_effect=changing):
                result = f.handle()
            self.assertEqual(calls, 2)
            manifest = f.manifest(result.saves[0])
            if rewrites == 1:
                self.assertEqual(next(entry["sha256"] for entry in manifest["entries"] if entry["path"] == "f"), hashlib.sha256(b"rewrite-1\n").hexdigest())
            else:
                self.assertTrue(any(entry["path"] == "f" and entry["reason"] == "unreadable" for entry in manifest["unsaved"]))

    def test_caps_deadline_disk_floor_and_special_files(self) -> None:
        f = self.fixture
        f.write("f", b"tracked\n")
        f.write("untracked", b"untracked\n")
        f.write("pipe", b"tracked regular file\n")
        git(f.repo, "add", "pipe")
        git(f.repo, "commit", "-m", "tracked special-file victim")
        (f.repo / "pipe").unlink()
        os.mkfifo(f.repo / "pipe")
        first = f.handle()
        self.assertTrue(any(row["reason"] == "special_file" for row in f.manifest(first.saves[0])["unsaved"]))
        f.configure(cap_file_size=1)
        limited = f.handle()
        self.assertTrue(f.manifest(limited.saves[0])["unsaved_counts"].get("cap_file_size"))
        f.configure(cap_files=0)
        limited = f.handle()
        self.assertTrue(f.manifest(limited.saves[0])["unsaved_counts"].get("cap_files"))
        f.configure(disk_floor=1 << 62)
        limited = f.handle()
        self.assertEqual({row["path"] for row in f.manifest(limited.saves[0])["entries"]}, {"f"})
        self.assertIn("only index and tracked", limited.notice)
        f.configure()
        original = git_module._spawn

        def deadline(*args: Any, **kwargs: Any) -> git_module.GitResult:
            if args[1][:1] == ["diff-files"]:
                return git_module.GitResult(-1, timed_out=True)
            return original(*args, **kwargs)

        with patch.object(git_module, "_spawn", side_effect=deadline):
            limited = f.handle()
        self.assertTrue(limited.saves)
        self.assertTrue(f.manifest(limited.saves[0])["unsaved_counts"].get("deadline"))

    def test_index_lock_and_folder_lock(self) -> None:
        f = self.fixture
        f.write("f", b"dirty\n")
        (f.repo / ".git/index.lock").write_bytes(b"other process")
        before = layout(f.repo / ".git")
        result = f.handle()
        self.assertTrue(result.saves, result)
        self.assertEqual(before, layout(f.repo / ".git"))
        (f.repo / ".git/index.lock").unlink()
        manifest = f.manifest(result.saves[0])
        import fcntl

        with (f.store / "folders" / manifest["folder_key"] / "lock").open("wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = time.monotonic()
            again = f.handle()
            self.assertTrue(again.saves)
            self.assertLess(time.monotonic() - started, 2.5)

    def test_large_lockstep_responses(self) -> None:
        f = self.fixture
        # Large lockstep responses prove that request/answer order cannot fill
        # stdout while all requests are being sent.
        f.write("f", b"a" * 70000)
        f.write("g", b"b" * 70000)
        git(f.repo, "add", "f", "g")
        self.assertTrue(f.handle().saves)

    def test_one_spawn_boundary(self) -> None:
        source = ast.parse((COMMON / "recovery_git.py").read_text(), feature_version=(3, 11))
        spawns = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "_spawn"]
        inside = {id(node) for node in ast.walk(spawns[0])}
        uses = _subprocess_uses(source)
        self.assertEqual(len(spawns), 1)
        self.assertTrue(uses)
        self.assertTrue(all(id(node) in inside for node in uses))
        for path in COMMON.glob("*.py"):
            if path.name != "recovery_git.py":
                self.assertNotIn("subprocess", _imports(path), path.name)

    def test_child_stdin_and_environment(self) -> None:
        f = self.fixture
        f.write("f", b"dirty\n")
        seen: list[tuple[Sequence[str], Any, Any]] = []
        original = subprocess.Popen

        def observe(arguments: Sequence[str], **kwargs: Any) -> Any:
            seen.append((arguments, kwargs.get("stdin"), kwargs.get("env")))
            return original(arguments, **kwargs)

        with patch.object(subprocess, "Popen", side_effect=observe):
            self.assertTrue(f.handle().saves)
        for arguments, stdin, environment in seen:
            request = "cat-file" in arguments and any(arg in arguments for arg in ("--batch", "--batch-check")) or "check-ignore" in arguments
            self.assertEqual(stdin, subprocess.PIPE if request else subprocess.DEVNULL)
            self.assertEqual(environment["LC_ALL"], "C")
            self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")

    def test_child_never_answers_watchdog_kills_group(self) -> None:
        f = self.fixture
        binary = f.root / "bin"
        binary.mkdir()
        program = binary / "git"
        program.write_text("#!/bin/sh\nsleep 30 &\nwait\n")
        program.chmod(0o700)
        f.configure(hard_seconds=0.1)
        started = time.monotonic()
        with patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}):
            # The single deadline includes root validation; use a direct
            # payload and an already-configured root, with an argument child.
            with patch.object(recovery, "prepare_root", return_value=(f.store, ())):
                result = f.handle(f.event(command="git restore ."))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn("command will run", result.notice)


if __name__ == "__main__":
    raise SystemExit(run_tests(RecoveryTests))
