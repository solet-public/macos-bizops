#!/usr/bin/env python3
"""A session whose row names a historical brief path stays read-only (historical gate filename)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from session_lifecycle_verbs_smoke import _state  # noqa: E402
from session_origin_backfill_smoke import seed_legacy  # noqa: E402

from agent_messaging_plugin import session_lifecycle_control  # noqa: E402
from agent_messaging_plugin.session_lifecycle_verbs import VerbError, drive_session  # noqa: E402
from agent_messaging_plugin.session_origin_backfill import backfill_session_origins  # noqa: E402


class HistoricalBriefPathTests(unittest.TestCase):
    def test_migrated_historical_and_unknown_origins_refuse_driver_work(self) -> None:
        state = _state()
        seed_legacy(state, with_default=True)
        self.assertEqual(backfill_session_origins(state)["updated"], 5)
        with patch.object(session_lifecycle_control, "resolve_host_driver", side_effect=AssertionError("driver lookup reached for a read-only or unknown session")) as resolve:
            for identity, code in (("old-path", "session_read_only"), ("old-ambiguous", "session_origin_unknown")):
                with self.subTest(identity=identity):
                    with self.assertRaises(VerbError) as caught:
                        drive_session(state, agent_instance_id=identity, text="hello", directed_by="fixture")
                    self.assertEqual(caught.exception.code, code)
            resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
