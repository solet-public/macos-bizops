"""Public recovery probes; plugins/github_midwife_plugin/tests/recovery_refs_smoke.py.

Self-contained stdlib smoke with disposable repositories and one shared fixture.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from recovery_test_support import CANARY, SECRET, Fixture, RecoveryBase, errno_value, git, git_module, recovery, ref_module, rules_module, run_tests  # pyright: ignore[reportMissingImports] - retained sibling fixtures


class RecoveryTests(RecoveryBase):
    def test_pruned_stash_rebuild_from_u1_copied_payloads(self) -> None:
        f = self.fixture
        f.stash()
        original = recovery.store.publish_packet

        def no_link(*_args: Any, **_kwargs: Any) -> None:
            raise OSError(errno_value("EXDEV"), "fixture copy path")

        def copied(*args: Any, **kwargs: Any) -> Any:
            kwargs["writer"] = replace(recovery.store.DEFAULT_WRITER, link=no_link)
            return original(*args, **kwargs)

        with patch.object(recovery.store, "publish_packet", side_effect=copied):
            saved = f.handle(f.event(command="git stash drop"))
        self.assertTrue(saved.saves, saved)
        f.cli("purge")
        self.assertEqual(list((f.store / "pool").iterdir()), [])
        git(f.repo, "stash", "drop")
        git(f.repo, "reflog", "expire", "--expire-unreachable=now", "--all")
        git(f.repo, "gc", "--prune=now")
        outcomes = f.cli("restore", saved.saves[0], "--refs")
        self.assertTrue(any("rebuilt" in row["outcome"] for row in outcomes), outcomes)
        git(f.repo, "stash", "apply", "--index")
        self.assertEqual(git(f.repo, "show", ":f"), b"staged\n")
        self.assertEqual((f.repo / "f").read_bytes(), b"worktree\n")
        f.assert_no_canaries()

    def test_rebuild_removes_ignored_path_already_present_in_base(self) -> None:
        f = self.fixture
        f.write(".gitignore", b"hidden/\n.env\n")
        f.write(".env", b"old ignored bytes")
        git(f.repo, "add", ".gitignore")
        git(f.repo, "add", "-f", ".env")
        git(f.repo, "commit", "-m", "base with forced tracked path")
        git(f.repo, "switch", "-c", "lost")
        f.write(".env", SECRET)
        git(f.repo, "add", "-f", ".env")
        git(f.repo, "commit", "-m", "branch version")
        git(f.repo, "switch", "main")
        saved = f.handle(f.event(command="git branch -D lost"))
        self.assertEqual(f.manifest(saved.saves[0])["refs"][0]["omitted"], [".env"])
        git(f.repo, "branch", "-D", "lost")
        git(f.repo, "reflog", "expire", "--expire-unreachable=now", "--all")
        git(f.repo, "gc", "--prune=now")
        outcomes = f.cli("restore", saved.saves[0], "--refs")
        self.assertTrue(any("rebuilt" in row["outcome"] for row in outcomes), outcomes)
        self.assertNotIn(b".env", git(f.repo, "ls-tree", "--name-only", "lost"))
        f.assert_no_canaries()

    def test_pruned_branch_rebuild_and_arbitrary_stash_visibility(self) -> None:
        f = self.fixture
        git(f.repo, "switch", "-c", "lost")
        f.write("f", b"branch bytes\n")
        git(f.repo, "add", "f")
        git(f.repo, "commit", "-m", "branch work")
        git(f.repo, "switch", "main")
        result = f.handle(f.event(command="git branch -D lost"))
        git(f.repo, "branch", "-D", "lost")
        git(f.repo, "reflog", "expire", "--expire-unreachable=now", "--all")
        git(f.repo, "gc", "--prune=now")
        outcomes = f.cli("restore", result.saves[0], "--refs")
        self.assertTrue(any("rebuilt" in row["outcome"] for row in outcomes), outcomes)
        self.assertEqual(git(f.repo, "show", "lost:f"), b"branch bytes\n")
        f.write(".gitignore", b"hidden/\n.env\n")
        f.write(".env", SECRET)
        git(f.repo, "add", ".gitignore")
        git(f.repo, "add", "-f", ".env")
        git(f.repo, "commit", "-m", "ordinary commit")
        tree = git(f.repo, "rev-parse", "HEAD^{tree}").decode().strip()
        odd_index = git(f.repo, "commit-tree", tree, "-p", "HEAD", "-m", "non-stash index parent").decode().strip()
        tip = git(f.repo, "commit-tree", tree, "-p", "HEAD~1", "-p", odd_index, "-m", "arbitrary merge").decode().strip()
        git(f.repo, "stash", "store", "-m", "arbitrary shape", tip)
        saved = f.handle(f.event(command="git stash drop"))
        self.assertTrue(saved.saves)
        f.assert_no_canaries()

    def test_visible_paths_p1_p3e_whole_store_and_finally(self) -> None:
        for probe in ("stash_all", "nested_untracked_rules", "ignored_rule_canary", "branch_at_stash"):
            with self.subTest(probe=probe), tempfile.TemporaryDirectory(prefix="recovery-visible-") as temporary:
                f = Fixture(Path(temporary))
                tip = f.stash(nested=probe == "nested_untracked_rules", hidden=probe == "ignored_rule_canary")
                command = "git stash drop"
                if probe == "branch_at_stash":
                    git(f.repo, "stash", "pop")
                    f.write(".gitignore", b"hidden/\n.env\n")
                    git(f.repo, "add", ".gitignore")
                    git(f.repo, "add", "-f", ".env")
                    git(f.repo, "stash", "push", "-m", "tracked secret")
                    tip = git(f.repo, "rev-parse", "refs/stash").decode().strip()
                    git(f.repo, "branch", "lost", tip)
                    command = "git branch -D lost"
                original = git_module._spawn
                inspected: list[bool] = []

                def inspect(*args: Any, f: Fixture = f, inspected: list[bool] = inspected, original: Any = original, **kwargs: Any) -> git_module.GitResult:
                    if "check-ignore" in args[1]:
                        f.assert_no_canaries()
                        inspected.append(True)
                    return original(*args, **kwargs)

                with patch.object(git_module, "_spawn", side_effect=inspect):
                    result = f.handle(f.event(command=command))
                self.assertTrue(result.saves, result)
                self.assertTrue(inspected)
                f.assert_no_canaries()
                self.assertEqual(list(f.rules.iterdir()), [])
                manifest = f.manifest(result.saves[0])
                self.assertTrue(manifest["refs"])
                self.assertTrue(any(row["reason"] == "ignored_in_ref" for row in manifest["unsaved"]), manifest)
                if probe == "nested_untracked_rules":
                    self.assertTrue(any(entry["path"] == "sub/n.txt" for entry in manifest["entries"]))

    def test_check_ignore_short_failure_oversize_admit_nothing(self) -> None:
        for fault in ("short", "failed", "oversize", "write_failed"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(prefix="recovery-rule-fault-") as temporary:
                f = Fixture(Path(temporary))
                if fault == "oversize":
                    f.write("hidden/.gitignore", CANARY + b"x" * (1 << 20))
                f.stash(hidden=fault != "oversize")
                original = git_module._spawn

                def fail(*args: Any, fault: str = fault, original: Any = original, **kwargs: Any) -> git_module.GitResult:
                    if "check-ignore" in args[1] and fault in {"short", "failed"}:
                        return git_module.GitResult(0 if fault == "short" else 128, b"short\0")
                    return original(*args, **kwargs)

                with patch.object(git_module, "_spawn", side_effect=fail), contextlib.ExitStack() as stack:
                    if fault == "write_failed":
                        stack.enter_context(patch.object(ref_module, "write_rule", side_effect=OSError(errno_value("EACCES"), "fixture")))
                    result = f.handle(f.event(command="git stash drop"))
                self.assertTrue(result.saves, result)
                manifest = f.manifest(result.saves[0])
                self.assertFalse(any(entry["layer"] == "ref_untracked" for entry in manifest["entries"]), manifest)
                self.assertGreater(manifest["unsaved_counts"].get("unclassified", 0), 0)
                self.assertEqual(list(f.rules.iterdir()), [])
                f.assert_no_canaries()

    def test_rules_dir_never_inside_store_and_sweeps_only_dead(self) -> None:
        f = self.fixture
        f.stash(hidden=True)
        inside = f.store / "tmp"
        inside.mkdir()
        alias = f.root / "alias"
        alias.symlink_to(inside, target_is_directory=True)
        original_spawn = git_module._spawn

        def during(*args: Any, **kwargs: Any) -> git_module.GitResult:
            if "check-ignore" in args[1]:
                f.assert_no_canaries()
            return original_spawn(*args, **kwargs)

        variants = [inside, alias, f.rules]
        case_alias = Path(str(f.store).replace("store", "STORE")) / "tmp"
        if case_alias.exists():
            variants.append(case_alias)
        for candidate in variants:
            with self.subTest(candidate=candidate), patch.dict(os.environ, {"TMPDIR": str(candidate)}), patch.object(git_module, "_spawn", side_effect=during), patch.object(rules_module.RuleWorkspace, "rule_candidates", return_value=(candidate, f.rules)):
                result = recovery.handle(f.event(command="git stash drop"), root=f.store)
                self.assertTrue(result.saves, result)
                f.assert_no_canaries()
        with patch.object(rules_module.RuleWorkspace, "rule_candidates", return_value=(inside, alias)):
            result = recovery.handle(f.event(command="git stash drop"), root=f.store)
        self.assertIn("safe place", result.notice)
        self.assertGreater(f.manifest(result.saves[0])["unsaved_counts"].get("unclassified", 0), 0)
        dead = f.rules / "solet-recovery-rules-99999999-dead"
        live = f.rules / f"solet-recovery-rules-{os.getpid()}-live"
        dead.mkdir()
        live.mkdir()
        old = time.time() - 700
        os.utime(dead, (old, old))
        os.utime(live, (old, old))
        f.handle(f.event(command="git stash drop"))
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())
        self.assertEqual(list(inside.iterdir()), [])
        f.assert_no_canaries()

    def test_restore_pruned_stash_p9_index_untouched(self) -> None:
        f = self.fixture
        f.stash(hidden=True)
        result = f.handle(f.event(command="git stash drop"))
        self.assertTrue(result.saves, result)
        save_id = result.saves[0]
        git(f.repo, "stash", "drop")
        git(f.repo, "reflog", "expire", "--expire-unreachable=now", "--all")
        git(f.repo, "gc", "--prune=now")
        before = (f.repo / ".git/index").read_bytes()
        outcomes = f.cli("restore", save_id, "--refs")
        self.assertTrue(any("rebuilt" in row["outcome"] for row in outcomes), outcomes)
        self.assertEqual(before, (f.repo / ".git/index").read_bytes())
        git(f.repo, "stash", "apply", "--index")
        self.assertEqual(git(f.repo, "show", ":f"), b"staged\n")
        self.assertEqual((f.repo / "f").read_bytes(), b"worktree\n")
        self.assertEqual((f.repo / "newfile").read_bytes(), b"visible\n")
        self.assertFalse((f.repo / ".env").exists())
        self.assertEqual(list((f.store / "tmp").iterdir()), [])
        f.assert_no_canaries()

    def test_restore_exact_refs_branch_collision_and_explicit_stage(self) -> None:
        f = self.fixture
        f.write("f", b"index\n")
        git(f.repo, "add", "f")
        f.write("f", b"disk\n")
        save_id = f.manual()
        git(f.repo, "reset", "--hard")
        f.cli("restore", save_id, "--stage")
        self.assertEqual(git(f.repo, "show", ":f"), b"index\n")
        self.assertEqual((f.repo / "f").read_bytes(), b"disk\n")
        git(f.repo, "reset", "--hard")
        f.stash()
        result = f.handle(f.event(command="git stash drop"))
        outcomes = f.cli("restore", result.saves[0], "--refs")
        self.assertTrue(any("exactly" in row["outcome"] for row in outcomes))
        git(f.repo, "branch", "lost")
        branch = f.handle(f.event(command="git branch -D lost"))
        outcomes = f.cli("restore", branch.saves[0], "--refs")
        self.assertTrue(any("lost-recovered-" in row["path"] for row in outcomes), outcomes)


if __name__ == "__main__":
    raise SystemExit(run_tests(RecoveryTests))
