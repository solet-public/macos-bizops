#!/usr/bin/env python3
"""Native registered-brief launch regressions (historical gate filename)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from _register_unit_double import RegisterUnitDouble, fixture_assignment  # noqa: E402
from session_lifecycle_verbs_smoke import (  # noqa: E402
    _TEST_HOST,
    _install_fake_hosts,
    _remove_fake_hosts,
    _spawn_req,
    _state,
)

import agent_messaging_plugin.session_hosts as hosts  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as lifecycle  # noqa: E402
from agent_messaging_plugin.register_unit_client import (  # noqa: E402
    RegisterActor,  # noqa: E402
    RegisterUnitError,  # noqa: E402
)
from agent_messaging_plugin.session_lifecycle_verbs import VerbError  # noqa: E402


class RegisteredBriefLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_fake_hosts()
        self.addCleanup(_remove_fake_hosts)

    def test_empty_directory_delivers_exact_registered_bytes_once(self) -> None:
        text = "First λ line\r\nLast line without a newline"
        assignment = fixture_assignment(text=text)
        state = _state()
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                with patch.object(lifecycle, "_provision_worktree_for_request", return_value=None):
                    result = lifecycle.spawn_session(state, _spawn_req(
                        host=_TEST_HOST, assignment=assignment,
                    ), register=RegisterUnitDouble(), actor=RegisterActor("agi-coordinator", "ases-agi-coordinator", "Coordinator-Main", "fixture-lane"))
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                os.chdir(previous)
        driver = hosts._REGISTRY[_TEST_HOST]
        self.assertTrue(result["first_turn_delivered"])
        self.assertEqual(len(driver.channel.sent), 1)
        self.assertIn(text, driver.channel.sent[0])
        self.assertIn(assignment.revision.sha256, driver.channel.sent[0])
        self.assertIn(assignment.revision.revision_id, driver.channel.sent[0])

    def test_missing_assignment_refuses_before_provision(self) -> None:
        with patch.object(lifecycle, "_provision_worktree_for_request") as provision:
            with self.assertRaises(VerbError) as caught:
                lifecycle.spawn_session(_state(), _spawn_req(host=_TEST_HOST, assignment=None), register=RegisterUnitDouble(), actor=RegisterActor("agi-coordinator", "ases-agi-coordinator", "Coordinator-Main", "fixture-lane"))
            self.assertEqual(caught.exception.code, "unit_assignment_required")
            provision.assert_not_called()

    def test_mismatched_unit_refuses_before_provision(self) -> None:
        with patch.object(lifecycle, "_provision_worktree_for_request") as provision:
            with self.assertRaises(VerbError):
                lifecycle.spawn_session(_state(), _spawn_req(host=_TEST_HOST, unit_id="unt_other"), register=RegisterUnitDouble(), actor=RegisterActor("agi-coordinator", "ases-agi-coordinator", "Coordinator-Main", "fixture-lane"))
            provision.assert_not_called()

    def test_corrupt_registered_bytes_refuse_before_provision(self) -> None:
        original = fixture_assignment()
        changed = replace(original, revision=replace(original.revision, text="changed"))
        with patch.object(lifecycle, "_provision_worktree_for_request") as provision:
            with self.assertRaises(RegisterUnitError):
                lifecycle.spawn_session(_state(), _spawn_req(host=_TEST_HOST, assignment=changed), register=RegisterUnitDouble(), actor=RegisterActor("agi-coordinator", "ases-agi-coordinator", "Coordinator-Main", "fixture-lane"))
            provision.assert_not_called()

    def test_historical_path_session_is_read_only(self) -> None:
        from agent_messaging_plugin.session_lifecycle_control import _resolve_driver_channel
        with self.assertRaises(VerbError) as caught:
            _resolve_driver_channel({"brief_ref": "/old/brief.md", "host": _TEST_HOST})
        self.assertEqual(caught.exception.code, "unit_assignment_historical")

    def test_path_arguments_are_not_a_public_launch_mode(self) -> None:
        from agent_messaging_plugin.session_launch_adapters import reject_retired_launch_fields
        for field in ("brief_ref", "brief_sha256", "brief_repository_root"):
            with self.subTest(field=field), self.assertRaises(VerbError) as caught:
                reject_retired_launch_fields({field: "obsolete", "unit_id": fixture_assignment().unit_id})
            self.assertEqual(caught.exception.code, "retired_launch_argument")


if __name__ == "__main__":
    unittest.main()
