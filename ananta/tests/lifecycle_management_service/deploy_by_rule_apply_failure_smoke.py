"""Fake-only proof: a definite refusal of the committing apply ends the run at once, and says what it is.

A refusal by error fails as apply_rejected. A cutover that refuses the release's schema change
defers to the written hand route, sends one notice, and every later run defers without building
until the serving release changes.

Offline: every external effect comes from the shipped fixture support, loaded by its exact path.

Run from the repository root:
    .venv/bin/python3 -B ananta/tests/lifecycle_management_service/deploy_by_rule_apply_failure_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deploy_by_rule_fixture_support import (  # noqa: E402
    BASELINE_AT,
    CLOCK,
    RELEASE,
    TARGET,
    Fixture,
    calls,
    notices,
    writes,
)

ROUTE = "plugins/default_thinking_plugin/knowledge_base_joseki/deploy_local_blue_green_release.md#Installing a release the cutover refuses"


def refused_marker(current_release: str) -> bytes:
    return json.dumps({"target": TARGET, "current_release": current_release, "refused_at": BASELINE_AT}).encode()


def test_error_answer_to_the_committing_apply_fails_at_once() -> None:
    f = Fixture()
    orig = f.apply

    def apply(key: str, data: dict[str, Any]) -> str:
        if data["dry_run"]:
            return orig(key, data)
        return json.dumps({"status": "completed", "result": {"action_status": "error", "error": "fixture apply failed", "data": {}}})

    f.apply = apply
    result = f.run()
    assert str(result["error"]).startswith("apply_rejected"), "APPLY_REJECTED_NAMED"
    assert f.s.ticks == 0, "NO_WAIT_AFTER_ERROR_ANSWER"
    assert result["decision"] == "failed", "FAILED_DECISION"
    assert not f.s.committed and not f.s.recorded, "NOTHING_COMMITTED_OR_RECORDED"
    assert f.log.count("REACHED_NOTICE") == 1, "ONE_NOTICE"
    assert f.s.marker is None, "AN_ORDINARY_REJECTION_LEAVES_NO_MARKER"


def test_the_named_route_is_a_section_of_the_joseki() -> None:
    path, section = ROUTE.split("#")
    assert f"\n## {section}\n" in (_ROOT / path).read_text(encoding="utf-8"), "ROUTE_SECTION_EXISTS"


def test_a_schema_refusal_defers_to_the_hand_route() -> None:
    """Leg A: the cutover's refusal ends the run as the route, with one notice and a marker, and no wait."""
    f = Fixture()
    f.refuse_schema()
    result = f.run()
    assert result["decision"] == "deferred_schema_route", f"DEFERRED_SCHEMA_ROUTE: {result}"
    assert (result["refused"], result["built"], result["route"]) == ("schema_preflight_refused", True, ROUTE), "ROUTE_NAMED_AND_BUILT"
    assert "schema preflight refused deploy" in result["detail"], "BOUNDED_RESTART_MESSAGE_CARRIED"
    assert f.s.ticks == 0, "NO_DRAIN_WAIT_AFTER_REFUSAL"
    assert not f.s.committed and not f.s.recorded and writes(f) == [], "NOTHING_RECORDED"
    (notice,) = notices(f)
    assert notice["error"].startswith("schema_preflight_refused:") and ROUTE in notice["error"], "ONE_NOTICE_NAMING_THE_ROUTE"
    assert json.loads(f.s.marker) == {"target": TARGET, "current_release": RELEASE, "refused_at": CLOCK}, "MARKER_WRITTEN"


def test_a_later_run_defers_without_building() -> None:
    """Leg B: the same serving release defers before the gate check, the swap read and any APPLY, and sends no notice."""
    f = Fixture()
    f.refuse_schema()
    f.run()
    applies, swaps = calls(f, f.m.APPLY), calls(f, f.m.SWAP)
    f.s.receipt, f.s.receipt_lock_held = {"result": "running", "pid": 7}, True  # a live gate would defer a run that got as far as busy()
    second = f.run()
    assert second["decision"] == "deferred_schema_route", f"SECOND_RUN_DEFERS_ON_THE_MARKER: {second}"
    assert (second["refused"], second["built"], second["route"]) == ("schema_preflight_refused", False, ROUTE), "ROUTE_NAMED_AND_NOT_BUILT"
    assert calls(f, f.m.APPLY) == applies, "NO_APPLY_DRY_RUN_INCLUDED"
    assert calls(f, f.m.SWAP) == swaps, "NO_SWAP_STATE_READ"
    assert len(notices(f)) == 1 and "notice" not in second, "NO_SECOND_NOTICE"
    assert f.s.ticks == 0, "NO_WAIT"


def test_the_marker_is_ignored_once_current_names_another_release() -> None:
    """Leg C: after the route moved current, the run reaches its ordinary outcome."""
    f = Fixture()
    f.s.marker = refused_marker("rel-before-the-route")
    result = f.run()
    assert result["decision"] == "deployed" and writes(f) == ["REACHED_APPLY_COMMIT", "REACHED_RECORD"], f"ORDINARY_OUTCOME_AFTER_THE_ROUTE: {result}"
    assert f.s.marker == refused_marker("rel-before-the-route"), "STALE_MARKER_IS_IGNORED_NOT_REWRITTEN"


def test_an_unreadable_marker_advises_and_the_run_proceeds() -> None:
    """Missing or unknown data is an advisory, never a refusal."""
    f = Fixture()
    f.s.marker = b"{not json"
    result = f.run()
    assert result["decision"] == "deployed", f"UNREADABLE_MARKER_DOES_NOT_BLOCK: {result}"
    assert any(str(a).startswith("schema_route_marker_unreadable") for a in result["advisories"]), "UNREADABLE_MARKER_ADVISED"


def test_a_marker_that_cannot_be_written_advises_and_the_run_still_defers() -> None:
    f = Fixture()
    f.refuse_schema()
    f.s.marker_write_fails = True
    result = f.run()
    assert result["decision"] == "deferred_schema_route" and result["built"] is True, f"REFUSAL_STANDS_WITHOUT_THE_MARKER: {result}"
    assert any(str(a).startswith("schema_route_marker_unwritten") for a in result["advisories"]), "UNWRITTEN_MARKER_ADVISED"
    assert f.s.marker is None and len(notices(f)) == 1, "NO_MARKER_ONE_NOTICE"


def main() -> None:
    test_error_answer_to_the_committing_apply_fails_at_once()
    test_the_named_route_is_a_section_of_the_joseki()
    test_a_schema_refusal_defers_to_the_hand_route()
    test_a_later_run_defers_without_building()
    test_the_marker_is_ignored_once_current_names_another_release()
    test_an_unreadable_marker_advises_and_the_run_proceeds()
    test_a_marker_that_cannot_be_written_advises_and_the_run_still_defers()
    print("deploy_by_rule_apply_failure_smoke: ok")


if __name__ == "__main__":
    main()
