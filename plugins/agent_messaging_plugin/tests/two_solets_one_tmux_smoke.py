#!/usr/bin/env python3
"""Exercise recorded session references on one private tmux server.

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/agent_messaging_plugin/tests/two_solets_one_tmux_smoke.py
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin.codex_tmux import CodexTmuxHostDriver  # noqa: E402
from agent_messaging_plugin.ledger_tmux_reconciliation import collect_ledger_tmux_report  # noqa: E402
from agent_messaging_plugin.schema import LIFECYCLE_LIVE, LIFECYCLE_SPAWNING, TABLE_MANAGED_SESSION  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.tmux_adapter import TmuxHostDriver  # noqa: E402

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

REF_A = "fleet-solet-a-shared-label-aaaa1111"
REF_B = "fleet-solet-b-shared-label-bbbb2222"
HAND = "mine"
SIBLING = "mine-extra"


class TwoSoletsOneTmuxSmoke(unittest.TestCase):
    """Each driver and ledger observes only the references it receives."""

    def setUp(self) -> None:
        tmux = shutil.which("tmux")
        if tmux is None:
            self.skipTest("tmux is not installed")
        temporary = tempfile.TemporaryDirectory(prefix="two-solets-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.wrapper = root / "tmux"
        self.wrapper.write_text(
            f'#!/bin/sh\nunset TMUX\nexec {shlex.quote(tmux)} -S {shlex.quote(str(root / "socket"))} "$@"\n',
            encoding="utf-8",
        )
        self.wrapper.chmod(0o755)
        self.calls: list[list[str]] = []
        self.addCleanup(self._kill_server)
        for ref in (REF_A, REF_B, HAND, SIBLING):
            self._start(ref)

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.wrapper), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def _kill_server(self) -> None:
        self._tmux("kill-server")

    def _start(self, ref: str) -> None:
        result = self._tmux("new-session", "-d", "-s", ref, "sleep 60")
        self.assertEqual(result.returncode, 0, f"new-session {ref}: {result.stderr}")

    def _sessions(self) -> set[str]:
        result = self._tmux("list-sessions", "-F", "#{session_name}")
        self.assertEqual(result.returncode, 0, result.stderr)
        return set(result.stdout.splitlines())

    def _restore_sessions(self) -> None:
        remaining = self._sessions()
        for ref in (REF_A, REF_B, HAND, SIBLING):
            if ref not in remaining:
                self._start(ref)

    def _driver_run(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(argv))
        return subprocess.run(argv, **kwargs)

    def _state(self, ref: str) -> StateManagementInterface:
        state = cast("StateManagementInterface", RealShapeState())
        instance = f"agi-{ref}"
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id=instance,
                label="shared-label",
                session_origin="spawned",
                host="tmux",
            ),
        )
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": instance}},
            {"host_ref": ref},
        )
        transition_lifecycle_state(
            state,
            agent_instance_id=instance,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="fixture",
        )
        return state

    def _metadata(self, argv: list[str]) -> tuple[int, bytes, str]:
        self.calls.append(list(argv))
        result = subprocess.run(
            [str(self.wrapper), *argv[1:]],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr.decode("utf-8", errors="replace")

    def test_c1_each_driver_stops_only_its_recorded_session(self) -> None:
        for driver_type in (TmuxHostDriver, CodexTmuxHostDriver):
            with self.subTest(driver=driver_type.__name__):
                driver_a = driver_type(tmux_bin=str(self.wrapper), solet_name="solet-a", run_fn=self._driver_run)
                driver_b = driver_type(tmux_bin=str(self.wrapper), solet_name="solet-b", run_fn=self._driver_run)
                try:
                    self.assertTrue(driver_a.alive(REF_A), self.calls)
                    self.assertTrue(driver_b.alive(REF_B), self.calls)
                    driver_b.terminate(REF_B, grace_seconds=1)
                    self.assertEqual(self._sessions(), {REF_A, HAND, SIBLING}, self.calls)
                finally:
                    self._restore_sessions()

    def test_c2_reconcile_probes_only_its_ledger_references(self) -> None:
        for own, foreign in ((REF_A, REF_B), (REF_B, REF_A)):
            with self.subTest(own=own):
                self.calls.clear()
                report = collect_ledger_tmux_report(self._state(own), runner=self._metadata)
                observations = report["observations"]
                self.assertTrue(report["complete"], report)
                self.assertEqual(len(observations), 1, report)
                self.assertEqual(observations[0]["reason"], "matched_process_present", report)
                self.assertEqual(observations[0]["ledger_row"]["host_ref"], own, report)
                self.assertEqual([pane["session_name"] for pane in observations[0]["panes"]], [own], report)
                self.assertEqual([call[3] for call in self.calls], [f"={own}:"], self.calls)
                self.assertNotIn(foreign, str(report), self.calls)
                self.assertNotIn(HAND, str(report), self.calls)

    def test_c3_hand_termination_preserves_prefix_sibling(self) -> None:
        for driver_type in (TmuxHostDriver, CodexTmuxHostDriver):
            with self.subTest(driver=driver_type.__name__):
                driver = driver_type(tmux_bin=str(self.wrapper), solet_name="solet-a", run_fn=self._driver_run)
                try:
                    driver.terminate(HAND, grace_seconds=1)
                    self.assertEqual(self._sessions(), {REF_A, REF_B, SIBLING}, self.calls)
                    driver.terminate(HAND, grace_seconds=1)
                    self.assertEqual(self._sessions(), {REF_A, REF_B, SIBLING}, self.calls)
                finally:
                    self._restore_sessions()

    def test_c4_hand_session_is_reachable_by_its_reference(self) -> None:
        driver = TmuxHostDriver(tmux_bin=str(self.wrapper), solet_name="solet-a", run_fn=self._driver_run)
        self.assertTrue(driver.alive(HAND), self.calls)
        seeded = self._tmux("send-keys", "-t", f"={HAND}:", "-l", "--", "hand-session-marker")
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        self._wait_for_text(driver, HAND, "hand-session-marker")
        self.assertEqual(self._sessions(), {REF_A, REF_B, HAND, SIBLING})

    def _wait_for_text(self, driver: TmuxHostDriver, ref: str, text: str) -> str:
        deadline = time.monotonic() + 3.0
        captured = driver.capture_pane_text(ref)
        while text not in captured and time.monotonic() < deadline:
            time.sleep(0.05)
            captured = driver.capture_pane_text(ref)
        self.assertIn(text, captured, self.calls)
        return captured

    def test_c5_send_to_b_is_not_visible_in_a(self) -> None:
        driver = TmuxHostDriver(tmux_bin=str(self.wrapper), solet_name="solet-b", run_fn=self._driver_run)
        seeded = self._tmux("send-keys", "-t", f"={REF_B}:", "-l", "--", "❯ ")
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        self._wait_for_text(driver, REF_B, "❯")
        channel = driver.driver_channel(REF_B)
        self.assertIsNotNone(channel, self.calls)
        assert channel is not None
        marker = "isolation-marker-b"
        channel.send(marker)
        self._wait_for_text(driver, REF_B, marker)
        self.assertTrue(any(call[1:4] == ["send-keys", "-t", f"={REF_B}:"] for call in self.calls), self.calls)
        self.assertNotIn(marker, driver.capture_pane_text(REF_A), self.calls)


if __name__ == "__main__":
    unittest.main()
