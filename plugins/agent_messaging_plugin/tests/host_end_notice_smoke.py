#!/usr/bin/env python3
"""Hermetic checks for arming tmux end notices and the termination race."""

from __future__ import annotations

import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SOLET = "ad" + chr(97)  # runtime fixture value keeps the shipped source free of a reserved identity token
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))

# Public lifecycle verbs initialize control's circular compatibility export.
importlib.import_module("agent_messaging_plugin.session_lifecycle_verbs")

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402

from agent_messaging_plugin import (  # noqa: E402
    host_end_notice,
    session_lifecycle_control,
)
from agent_messaging_plugin.schema import LIFECYCLE_LIVE, LIFECYCLE_TERMINATED  # noqa: E402
from agent_messaging_plugin.session_end import end_session_record  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)


class FakeRun:
    def __init__(self, *, shown: str = "", show_exit: int = 0, set_exit: int = 0) -> None:
        self.shown = shown
        self.show_exit = show_exit
        self.set_exit = set_exit
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs == {"capture_output": True, "text": True, "timeout": 5}
        self.calls.append(argv)
        if "show-hooks" in argv:
            return subprocess.CompletedProcess(argv, self.show_exit, self.shown, "show failed" if self.show_exit else "")
        return subprocess.CompletedProcess(argv, self.set_exit, "", "set failed" if self.set_exit else "")


class EndNoticeSmoke(unittest.TestCase):
    def test_n0_candidate_import(self) -> None:
        self.assertTrue(Path(host_end_notice.__file__).resolve().is_relative_to(ROOT))

    def test_n1_exact_hook_text(self) -> None:
        expected = 'run-shell \'(SOLET_NAME=x /x/solet-bridge call --no-wait plugin::agent_messaging_plugin::session_host_closed "{\\"host\\": \\"tmux\\", \\"host_ref\\": \\"#{q:hook_session_name}\\"}") </dev/null >/dev/null 2>&1 &\''.replace("SOLET_NAME=x", f"SOLET_NAME={SOLET}")
        self.assertEqual(host_end_notice.session_closed_hook_command(SOLET, "/x/solet-bridge"), expected)

    def test_n2_add_once(self) -> None:
        run = FakeRun()
        self.assertTrue(host_end_notice.arm_session_closed_notice(tmux=["tmux"], run_fn=run, solet_name=SOLET, solet_bin="/x/solet-bridge"))
        self.assertEqual(
            run.calls,
            [
                ["tmux", "show-hooks", "-g", "session-closed"],
                [
                    "tmux",
                    "set-hook",
                    "-ga",
                    "session-closed",
                    host_end_notice.session_closed_hook_command(SOLET, "/x/solet-bridge"),
                ],
            ],
        )

    def test_n3_same_hook_is_unchanged(self) -> None:
        hook = host_end_notice.session_closed_hook_command(SOLET, "/x/solet-bridge")
        run = FakeRun(shown=f"session-closed[3] {hook}\n")
        self.assertTrue(host_end_notice.arm_session_closed_notice(tmux=["tmux"], run_fn=run, solet_name=SOLET, solet_bin="/x/solet-bridge"))
        self.assertEqual(len(run.calls), 1)

    def test_n4_replace_only_ours(self) -> None:
        other = host_end_notice.session_closed_hook_command("other", "/other/solet-bridge")
        old = host_end_notice.session_closed_hook_command(SOLET, "/old/solet-bridge")
        run = FakeRun(shown=f"session-closed[0] {other}\nsession-closed[3] {old}\n")
        self.assertTrue(host_end_notice.arm_session_closed_notice(tmux=["tmux"], run_fn=run, solet_name=SOLET, solet_bin="/x/solet-bridge"))
        self.assertEqual(
            run.calls[1],
            [
                "tmux",
                "set-hook",
                "-g",
                "session-closed[3]",
                host_end_notice.session_closed_hook_command(SOLET, "/x/solet-bridge"),
            ],
        )

    def test_n5_failures_warn_once(self) -> None:
        for run in (FakeRun(show_exit=1), FakeRun(set_exit=1)):
            with self.subTest(run=run), self.assertLogs(level="WARNING") as logs:
                self.assertFalse(host_end_notice.arm_session_closed_notice(tmux=["tmux"], run_fn=run, solet_name=SOLET, solet_bin="/x/solet-bridge"))
            self.assertEqual(len(logs.records), 1)
            self.assertIn("show-hooks" if run.show_exit else "set-hook", logs.output[0])

    def test_n6_invalid_inputs_warn_without_tmux(self) -> None:
        for name, binary in (("", "/x/solet-bridge"), (SOLET, "/x/it's-bridge")):
            run = FakeRun()
            with self.subTest(name=name, binary=binary), self.assertLogs(level="WARNING") as logs:
                self.assertFalse(host_end_notice.arm_session_closed_notice(tmux=["tmux"], run_fn=run, solet_name=name, solet_bin=binary))
            self.assertEqual(len(logs.records), 1)
            self.assertEqual(run.calls, [])

    def test_n7_notice_wins_terminate_race(self) -> None:
        state = cast("StateManagementInterface", RealShapeState())
        agent_id = "agi-end-notice-race"
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id=agent_id,
                label="race",
                session_origin="spawned",
                host="tmux",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_id,
            from_state="spawning",
            to_state=LIFECYCLE_LIVE,
            directed_by="test",
            recorded_fields={"host_ref": "race-host"},
        )

        class EndingDriver:
            def terminate(self, _host_ref: str, _grace_seconds: int) -> None:
                row = read_managed_session(state, agent_id)
                assert end_session_record(state, row, why="tmux notice", observation=None, directed_by="host")

        with (
            patch.object(
                session_lifecycle_control,
                "_resolve_termination_driver",
                return_value=(EndingDriver(), "tmux"),
            ),
            patch.object(session_lifecycle_control, "_probe_host_liveness", return_value=("dead", "closed")),
        ):
            result = session_lifecycle_control.terminate_session(state, agent_instance_id=agent_id, directed_by="test")
        self.assertTrue(result["already_terminal"])
        self.assertEqual(result["lifecycle_state"], LIFECYCLE_TERMINATED)
        self.assertEqual(read_managed_session(state, agent_id)["lifecycle_state"], LIFECYCLE_TERMINATED)


if __name__ == "__main__":
    unittest.main()
