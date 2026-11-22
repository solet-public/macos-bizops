#!/usr/bin/env python3
"""The set and list session limit verbs, offline over in-memory state.

One test method per check V0-V5. The plugin is a subclass whose state service is an in-memory fake; no test creates a file.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT / "ananta/src"))
sys.path.insert(0, str(_ROOT / "plugins/agent_messaging_plugin/src"))

from _real_state_fake import _STANDARDIZER_COLUMNS, RealShapeState  # noqa: E402
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402

from agent_messaging_plugin import plugin as plugin_module  # noqa: E402
from agent_messaging_plugin import session_limits as limits  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

PROBE = {"selector": {"runtime": "codex"}, "sessions": 2, "words": "two codex at once", "reason": "probe"}
CONTEXT = {"call_context": CallContext(calling_plugin=None, principal_kind="external", principal_id="agi-probe")}


class LimitPlugin(AgentMessagingPlugin):
    fixture_state: StateManagementInterface | None

    def _get_state_service(self) -> StateManagementInterface | None:
        return self.fixture_state


def make_plugin(*, bound: bool = True) -> LimitPlugin:
    state = RealShapeState()
    table = limits.get_session_limit_schema_definition().tables[limits.TABLE_SESSION_LIMIT]
    state._enforced_columns[table.table_name] = frozenset(table.columns) | _STANDARDIZER_COLUMNS
    plugin = object.__new__(LimitPlugin)
    plugin.fixture_state = cast(StateManagementInterface, state) if bound else None
    return plugin


def completed(answer: dict[str, Any]) -> dict[str, Any]:
    if answer["action_status"] != "completed":
        raise AssertionError(f"the verb did not complete: {answer!r}")
    return cast(dict[str, Any], answer["data"])


def failed_code(answer: dict[str, Any]) -> str:
    if answer["action_status"] != "failed":
        raise AssertionError(f"the verb did not fail: {answer!r}")
    return str(answer["error"]["code"])


class SessionLimitVerbsSmoke(unittest.TestCase):
    def test_v0_the_modules_are_this_worktrees(self) -> None:
        self.assertTrue(Path(plugin_module.__file__).resolve().is_relative_to(_ROOT), plugin_module.__file__)
        self.assertTrue(Path(limits.__file__).resolve().is_relative_to(_ROOT), limits.__file__)

    def test_v1_the_table_and_both_verbs_are_declared(self) -> None:
        plugin = make_plugin()
        tables = {name for definition in plugin.get_schema_definitions() for name in definition.tables}
        self.assertIn(limits.TABLE_SESSION_LIMIT, tables)
        edges = plugin.get_edge_process_definitions()
        self.assertIn("set_session_limit", edges)
        self.assertIn("list_session_limits", edges)

    def test_v2_set_records_who_called_and_a_call_without_context_records_nobody(self) -> None:
        plugin = make_plugin()
        named = completed(plugin.set_session_limit(dict(PROBE), CONTEXT))
        self.assertEqual((named["limit"]["revision"], named["limit"]["set_by"]), (1, "external:agi-probe"))
        anonymous = completed(plugin.set_session_limit(dict(PROBE), {}))
        self.assertEqual(anonymous["limit"]["set_by"], "")
        self.assertEqual(len(anonymous["limits"]), 2)

    def test_v3_list_shows_the_limit_not_lapsed_and_history_newest_first(self) -> None:
        plugin = make_plugin()
        first = completed(plugin.set_session_limit(dict(PROBE), CONTEXT))["limit"]
        second = completed(plugin.set_session_limit({**PROBE, "words": "one more"}, CONTEXT))["limit"]
        listed = completed(plugin.list_session_limits({}, CONTEXT))
        self.assertEqual({row["limit_id"] for row in listed["limits"]}, {first["limit_id"], second["limit_id"]})
        self.assertTrue(all(row["lapsed"] is False for row in listed["limits"]))
        self.assertEqual([row["limit_id"] for row in listed["history"]], [second["limit_id"], first["limit_id"]])

    def test_v4_correct_retire_stale_and_invalid_parameters(self) -> None:
        plugin = make_plugin()
        limit_id = completed(plugin.set_session_limit(dict(PROBE), CONTEXT))["limit"]["limit_id"]
        target = {"limit_id": limit_id, "reason": "probe"}
        corrected = completed(plugin.set_session_limit({**PROBE, **target, "action": "correct", "expected_revision": 1, "sessions": 1}, CONTEXT))
        self.assertEqual((corrected["limit"]["revision"], corrected["limit"]["sessions"]), (2, 1))
        retired = completed(plugin.set_session_limit({**target, "action": "retire", "expected_revision": 2}, CONTEXT))
        self.assertEqual(retired["limits"], [])
        self.assertEqual(failed_code(plugin.set_session_limit({**target, "action": "retire", "expected_revision": 2}, CONTEXT)), "stale_limit")
        self.assertEqual(failed_code(plugin.set_session_limit({**PROBE, "selector": {"project": "x"}}, CONTEXT)), "parameter_invalid")
        self.assertEqual(failed_code(plugin.list_session_limits({"history_limit": 0}, CONTEXT)), "parameter_invalid")

    def test_v5_both_verbs_fail_when_no_state_service_is_bound(self) -> None:
        plugin = make_plugin(bound=False)
        self.assertEqual(failed_code(plugin.set_session_limit(dict(PROBE), CONTEXT)), "state_service_unavailable")
        self.assertEqual(failed_code(plugin.list_session_limits({}, CONTEXT)), "state_service_unavailable")


if __name__ == "__main__":
    unittest.main()
