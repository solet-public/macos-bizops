"""Public recovery probes; plugins/github_midwife_plugin/tests/working_tree_recovery_smoke.py.

Self-contained stdlib smoke with disposable repositories and one shared fixture.
"""

from __future__ import annotations

import importlib
import os
import stat
import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from recovery_test_support import SECRET, RecoveryBase, errno_value, git, git_module, layout, recovery, run_tests  # pyright: ignore[reportMissingImports] - retained sibling fixtures

restore_module = importlib.import_module("recovery_restore")
_WRITER_WAIT_SECONDS = 10.0


def _concurrent_edit(target: Path, content: bytes, ready: threading.Event, finished: threading.Event, errors: list[str]) -> None:
    try:
        if not ready.wait(_WRITER_WAIT_SECONDS):
            raise RuntimeError("restore did not reach its final identity read")
        with target.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException as exc:
        errors.append(repr(exc))
    finally:
        finished.set()


class RecoveryTests(RecoveryBase):
    def test_readback_without_git_and_session_start_store_failure(self) -> None:
        f = self.fixture
        f.write("f", b"saved\n")
        save_id = f.manual()
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(f.cli("show", save_id)["save_id"], save_id)
            self.assertTrue(f.cli("list"))
        with patch.object(recovery, "prepare_root", side_effect=OSError(errno_value("EACCES"), "store root")):
            result = f.handle(f.event("SessionStart"))
        self.assertIn("Recovery is inactive", result.context)

    def test_capture_layers_custody_git_unchanged_and_safe_output(self) -> None:
        f = self.fixture
        f.write("f", b"index\n")
        git(f.repo, "add", "f")
        f.write("f", b"disk\n")
        f.write("untracked", b"new\n")
        f.write("hidden/secret", SECRET)
        (f.repo / "link").symlink_to("/outside/never/follow")
        before = layout(f.repo / ".git")
        result = f.handle()
        self.assertTrue(result.saves, result)
        manifest = f.manifest(result.saves[0])
        entries = {(entry["layer"], entry["path"]): entry for entry in manifest["entries"]}
        self.assertNotEqual(entries[("index", "f")]["sha256"], entries[("worktree", "f")]["sha256"])
        self.assertIn(("worktree", "untracked"), entries)
        self.assertEqual(entries[("worktree", "link")]["mode"], 0o120000)
        self.assertEqual(before, layout(f.repo / ".git"))
        self.assertIn("outside this session", result.notice)
        self.assertNotIn("decision", asdict(result))
        self.assertNotIn("permissionDecision", asdict(result))
        f.assert_no_canaries()
        packet = next((f.store / "packets").glob("*/*"))
        payload = next((packet / "payload").iterdir())
        self.assertEqual(stat.S_IMODE(packet.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(payload.stat().st_mode), 0o400)
        self.assertEqual(payload.stat().st_ino, (f.store / "pool" / payload.name).stat().st_ino)

    def test_restore_preserves_newer_file_type_even_when_oid_matches_base(self) -> None:
        f = self.fixture
        f.write("f", b"saved\n")
        save_id = f.manual()
        (f.repo / "f").unlink()
        (f.repo / "f").symlink_to("base\n")
        outcome = f.cli("restore", save_id)
        self.assertTrue((f.repo / "f").is_symlink())
        self.assertEqual(os.readlink(f.repo / "f"), "base\n")
        self.assertTrue(any(row.get("saved_copy") for row in outcome))

    def test_restore_atomic_publish_preserves_completed_writer(self) -> None:
        f = self.fixture
        saved, newer = b"B1_OLD_SAVED_738bc97c\n", b"B1_COMPLETED_WRITER_d485fbb8\n"
        f.write("f", saved)
        save_id = f.manual()
        f.write("f", b"base\n")
        before = layout(f.repo / ".git")
        ready, finished = threading.Event(), threading.Event()
        errors: list[str] = []
        original = restore_module._leaf
        info = f.repo.stat()
        folder_identity = info.st_dev, info.st_ino
        reads = 0

        def verified_then_write(parent: int, name: str, object_format: str) -> Any:
            nonlocal reads
            result = original(parent, name, object_format)
            current = os.fstat(parent)
            if name == "f" and (current.st_dev, current.st_ino) == folder_identity:
                reads += 1
                if reads == 2:
                    ready.set()
                    if not finished.wait(_WRITER_WAIT_SECONDS):
                        raise RuntimeError("writer did not finish before publication")
            return result

        thread = threading.Thread(target=_concurrent_edit, args=(f.repo / "f", newer, ready, finished, errors), daemon=True)
        thread.start()
        try:
            with patch.object(restore_module, "_leaf", side_effect=verified_then_write):
                outcomes = f.cli("restore", save_id)
        finally:
            ready.set()
            thread.join(_WRITER_WAIT_SECONDS)
        self.assertFalse(thread.is_alive(), "fixture writer stalled")
        self.assertFalse(errors, errors)
        self.assertEqual(reads, 2)
        self.assertEqual((f.repo / "f").read_bytes(), newer, "restore lost a write completed after the final identity read")
        saved_copy = next(row["saved_copy"] for row in outcomes if row["path"] == "f")
        self.assertEqual(Path(saved_copy).read_bytes(), saved)
        self.assertEqual(layout(f.repo / ".git"), before)

    def test_restore_no_replace_keeps_recreated_target(self) -> None:
        f = self.fixture
        saved, newer = b"saved before recreation\n", b"created after atomic displacement\n"
        f.write("f", saved)
        save_id = f.manual()
        f.write("f", b"base\n")
        original = os.link
        scheduled: list[bool] = []

        def recreate(src: Any, dst: Any, **kwargs: Any) -> None:
            if str(src).startswith(".recovery-") and dst == "f" and not scheduled:
                scheduled.append(True)
                with (f.repo / "f").open("wb") as stream:
                    stream.write(newer)
                    stream.flush()
                    os.fsync(stream.fileno())
            original(src, dst, **kwargs)

        with patch.object(os, "link", side_effect=recreate):
            outcomes = f.cli("restore", save_id)
        self.assertEqual(scheduled, [True])
        self.assertEqual((f.repo / "f").read_bytes(), newer)
        self.assertEqual(Path(outcomes[0]["saved_copy"]).read_bytes(), saved)
        self.assertEqual(Path(outcomes[0]["preserved_original"]).read_bytes(), b"base\n")

    def test_restore_retains_inode_for_already_open_writer(self) -> None:
        f = self.fixture
        saved, newer = b"saved before open-writer race\n", b"writer still holds displaced inode\n"
        f.write("f", saved)
        save_id = f.manual()
        f.write("f", b"base\n")
        original = os.link
        scheduled: list[bool] = []
        with (f.repo / "f").open("r+b") as writer:

            def late_write(src: Any, dst: Any, **kwargs: Any) -> None:
                if str(src).startswith(".recovery-") and dst == "f" and not scheduled:
                    scheduled.append(True)
                    writer.write(newer)
                    writer.truncate()
                    writer.flush()
                    os.fsync(writer.fileno())
                original(src, dst, **kwargs)

            with patch.object(os, "link", side_effect=late_write):
                outcomes = f.cli("restore", save_id)
        self.assertEqual(scheduled, [True])
        self.assertEqual((f.repo / "f").read_bytes(), saved)
        preserved = Path(outcomes[0]["preserved_original"])
        self.assertEqual(preserved.read_bytes(), newer)
        self.assertEqual(stat.S_IMODE(preserved.parent.stat().st_mode), 0o700)

    def test_restore_never_overwrites_newer_edit(self) -> None:
        f = self.fixture
        f.write("f", b"saved\n")
        save_id = f.manual()
        f.write("f", b"newer\n")
        result = f.cli("restore", save_id)
        self.assertEqual((f.repo / "f").read_bytes(), b"newer\n")
        saved = next(row["saved_copy"] for row in result if row["path"] == "f")
        self.assertEqual(Path(saved).read_bytes(), b"saved\n")
        f.write("f", b"base\n")
        f.cli("restore", save_id)
        self.assertEqual((f.repo / "f").read_bytes(), b"saved\n")
        into = f.root / "restored-folder"
        f.cli("restore", save_id, "--into", str(into))
        self.assertEqual((into / "f").read_bytes(), b"saved\n")

    def test_restore_symlinked_parent_never_followed(self) -> None:
        f = self.fixture
        f.write("dir/a", b"saved\n")
        save_id = f.manual()
        (f.repo / "dir/a").unlink()
        (f.repo / "dir").rmdir()
        outside = f.root / "outside"
        outside.mkdir()
        (f.repo / "dir").symlink_to(outside, target_is_directory=True)
        result = f.cli("restore", save_id)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(any(row.get("saved_copy") for row in result))

    def test_failure_rows_root_errors_git_missing_unreadable_no_decisions(self) -> None:
        f = self.fixture
        for error in ("ENOSPC", "EROFS", "EACCES"):
            with self.subTest(error=error), patch.object(recovery.store, "publish_packet", side_effect=OSError(errno_value(error), error)):
                result = f.handle()
            self.assertIn("command will run", result.notice)
            self.assertFalse(result.saves)
        with patch.object(git_module, "_spawn", side_effect=recovery.RecoveryError("git_missing")):
            self.assertEqual(f.handle(), recovery.HookResult())
        with patch.object(recovery.Git, "read", side_effect=recovery.RecoveryError("git_read_failed", "safe.directory")):
            result = f.handle(f.event(command="git restore ."))
        self.assertIn("command will run", result.context)
        for event in [recovery.HookEvent(), f.event("Unknown"), replace(f.event(), event="Stop"), f.event(command=""), f.event("PreToolUse", tool_name="Write"), f.event("Stop"), f.event("SessionEnd"), f.event("PostToolUse")]:
            result = f.handle(event)
            keys = set(asdict(result))
            self.assertFalse(keys & {"decision", "permissionDecision", "continue", "stopReason"})
            if event.event in {"Stop", "SessionEnd", "PostToolUse"}:
                self.assertEqual((result.notice, result.context), ("", ""))

    def test_clean_c_locale_quoted_names_and_non_git_journal_only(self) -> None:
        f = self.fixture
        f.write('späce "name\n', b"visible\n")
        f.write("hidden/secret", SECRET)
        with patch.dict(os.environ, {"LANG": "de_DE.UTF-8"}):
            result = f.handle(f.event(command="git clean -fdx"))
        self.assertTrue(result.saves, result)
        manifest = f.manifest(result.saves[0])
        self.assertTrue(any(entry["path"] == 'späce "name\n' for entry in manifest["entries"]))
        self.assertTrue(manifest["unsaved_counts"].get("ignored_victim"))
        f.assert_no_canaries()
        folder = f.root / "plain"
        folder.mkdir()
        (folder / "mine").write_bytes(b"own non-git\n")
        (folder / "other").write_bytes(b"foreign non-git\n")
        event = recovery.HookEvent("PostToolUse", "plain", cwd=folder, tool_name="Write", edited_paths=("mine",))
        f.handle(event)
        result = f.handle(replace(event, event="Stop"))
        self.assertTrue(result.saves, result)
        self.assertEqual([entry["path"] for entry in f.manifest(result.saves[0])["entries"]], ["mine"])


if __name__ == "__main__":
    raise SystemExit(run_tests(RecoveryTests))
