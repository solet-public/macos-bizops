"""Public recovery probes; plugins/github_midwife_plugin/tests/recovery_lifecycle_smoke.py.

Self-contained stdlib smoke with disposable repositories and one shared fixture.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from recovery_test_support import RecoveryBase, git, recovery, run_tests  # pyright: ignore[reportMissingImports] - retained sibling fixtures


class RecoveryTests(RecoveryBase):
    def test_stash_journal_uses_classifier_identity_and_owns_ref_paths(self) -> None:
        f = self.fixture
        f.stash()
        f.handle(f.event("PostToolUse", command="git stash push --all"))
        self.assertEqual(f.handle(f.event(command="git stash drop")).notice, "")
        f.handle(f.event("PostToolUse", command="echo stash; git reset --hard", session="other"))
        foreign = f.handle(f.event(command="git stash drop", session="other"))
        self.assertIn("outside this session", foreign.notice)

    def test_notices_are_once_per_session_and_old_python_is_informational(self) -> None:
        f = self.fixture
        f.write("f", b"own\n")
        f.handle(f.event("PostToolUse", tool_name="Write", tool_input={"file_path": str(f.repo / "f")}))
        f.configure(disk_floor=1 << 62)
        self.assertIn("disk space is low", f.handle().notice)
        self.assertEqual(f.handle().notice, "")
        f.configure(store_bytes=0)
        self.assertTrue(f.handle(f.event("SessionStart")).context)
        self.assertEqual(f.handle(f.event("SessionStart")).context, "")
        with patch.object(sys, "version_info", (3, 10)):
            self.assertIn("python_below_3_11", f.handle(f.event("SessionStart")).context)
        self.assertEqual(recovery.handle(None), recovery.HookResult())

    def test_attribution_cache_and_stop_idempotence(self) -> None:
        f = self.fixture
        f.write("f", b"own\n")
        edit = f.event("PostToolUse", tool_name="apply_patch", tool_input={"patch": "*** Update File: f\n"})
        self.assertEqual(f.handle(edit).notice, "")
        self.assertEqual(f.handle().notice, "")
        first = f.handle(f.event("Stop"))
        self.assertTrue(first.saves, first)
        self.assertEqual(first.notice, "")
        original = recovery.store.read_entry
        reread: list[str] = []

        def read(root: Path, relative: str, **kwargs: Any) -> Any:
            if root.resolve() == f.repo.resolve() and relative == "f" and kwargs.get("digest"):
                reread.append(relative)
            return original(root, relative, **kwargs)

        with patch.object(recovery.store, "read_entry", side_effect=read):
            second = f.handle(f.event("Stop", turn_id="turn-2"))
        self.assertEqual(first.saves, second.saves)
        self.assertEqual(reread, [])
        f.write("f", b"someone else\n")
        self.assertIn("outside this session", f.handle().notice)

    def test_lifecycle_correction_stale_reversal_terminal_pool(self) -> None:
        f = self.fixture
        f.configure(trash_seconds=0.03)
        f.write("f", b"hold\n")
        original = f.manual()
        successor = f.cli("correct", original, "--note", "corrected label")["save_id"]
        self.assertEqual(f.manifest(successor)["predecessor"], original)
        self.assertEqual(f.cli("show", original)["state"], "present")
        f.cli("forget", original, "--expected-state", "present")
        self.assertIn("recovery_stale_state", f.cli("forget", original, "--expected-state", "present", code=1))
        f.cli("unforget", original, "--expected-state", "trashed")
        f.cli("forget", original, "--expected-state", "present")
        time.sleep(0.04)
        f.cli("purge", original)
        self.assertIn("save_missing", f.cli("show", original, code=1))
        self.assertTrue(any((f.store / "pool").iterdir()), "successor still keeps shared payload")
        f.cli("forget", successor, "--expected-state", "present")
        time.sleep(0.04)
        f.cli("purge", successor)
        self.assertEqual(list((f.store / "pool").iterdir()), [])

    def test_prune_undelivered_never_pruned_and_newest_turn_protected(self) -> None:
        f = self.fixture
        f.configure(store_bytes=0, grace_seconds=0, trash_seconds=0)
        f.write("f", b"undelivered\n")
        undelivered = f.manual()
        f.cli("prune", "--apply")
        self.assertEqual(f.cli("show", undelivered)["state"], "present")
        turn = f.handle(f.event("Stop"))
        self.assertTrue(turn.saves)
        git(f.repo, "add", "f")
        git(f.repo, "commit", "-m", "delivered")
        pruned = f.cli("prune", "--apply")
        self.assertIn(undelivered, pruned["pruned"])
        self.assertEqual(f.cli("show", turn.saves[0])["state"], "present")
        self.assertEqual(f.cli("list", "--undelivered"), [])

    def test_delivery_later_version_propagates_and_grace(self) -> None:
        f = self.fixture
        f.configure(grace_seconds=0.05, trash_seconds=100)
        f.write("f", b"intermediate\n")
        old = f.manual()
        f.write("f", b"final\n")
        final = f.manual()
        git(f.repo, "add", "f")
        git(f.repo, "commit", "-m", "delivered final")
        first = f.cli("prune", "--apply")
        self.assertEqual(first["pruned"], [])
        time.sleep(0.06)
        second = f.cli("prune", "--apply")
        self.assertEqual(set(second["pruned"]), {old, final})


if __name__ == "__main__":
    raise SystemExit(run_tests(RecoveryTests))
