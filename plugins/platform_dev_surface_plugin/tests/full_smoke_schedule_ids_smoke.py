#!/usr/bin/env python3
"""Tier U: the scheduled full battery's flow and session ids pass the action factory's normalizers."""

# ruff: noqa: E402
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/platform_dev_surface_plugin/src"):
    sys.path.insert(0, str(source))
from ananta.core.contexts.normalization import normalize_flow_id, normalize_session_id
from platform_dev_surface_plugin.quality.full_battery import SYSTEM_STATE


class ScheduleStateIdTests(unittest.TestCase):
    def test_flow_id_is_one_the_action_factory_accepts(self) -> None:
        self.assertEqual(normalize_flow_id(SYSTEM_STATE["flow_id"]), SYSTEM_STATE["flow_id"])

    def test_session_id_is_one_the_action_factory_accepts(self) -> None:
        self.assertEqual(normalize_session_id(SYSTEM_STATE["session_id"]), SYSTEM_STATE["session_id"])


if __name__ == "__main__":
    unittest.main()
