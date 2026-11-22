"""A deployed or skipped landing asks for one full battery run; other decisions ask for none."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deploy_by_rule_fixture_support import (  # noqa: E402
    FULL_BATTERY,
    TARGET,
    Fixture,
)


def _asks(f: Fixture) -> list[list[str]]:
    """Every bridge argv that called the full battery verb, in order."""
    return [a for a in f.log if isinstance(a, list) and a[1:3] == ["call", FULL_BATTERY]]


def test_production_deploy_asks_once_after_the_record() -> None:
    f = Fixture()
    result = f.run()
    assert result["decision"] == "deployed", "DEPLOYED_DECISION"
    assert f.log.count("REACHED_FULL_BATTERY_REQUEST") == 1, "ONE_REQUEST_PER_DEPLOYED_COMMIT"
    assert f.log.index("REACHED_RECORD") < f.log.index("REACHED_FULL_BATTERY_REQUEST"), "REQUEST_FOLLOWS_THE_RECORD"
    assert f.requests == [{"trigger": "deploy"}], "REQUEST_DATA_IS_THE_DEPLOY_TRIGGER"
    assert result["full_battery"]["campaign_id"] == "c" * 64, "REPLY_IS_IN_THE_TERMINAL_RESULT"
    assert not result["advisories"], "A_STARTED_REQUEST_ADDS_NO_ADVISORY"


def test_request_argv_goes_through_the_bridge_and_waits() -> None:
    f = Fixture()
    f.run()
    assert _asks(f) == [[f.args.bridge, "call", FULL_BATTERY, '{"trigger": "deploy"}', "--wait"]], "REQUEST_ARGV"


def _assert_asks_none(setup: Callable[[SimpleNamespace], None], decision: str, name: str) -> None:
    f = Fixture()
    setup(f.s)
    result = f.run()
    assert result["decision"] == decision, f"{name}_DECISION: {result['decision']}"
    assert not _asks(f) and not f.requests and "full_battery" not in result, f"{name}_ASKS_NONE"


def _hold_receipt(s: SimpleNamespace) -> None:
    s.receipt = {"result": "running", "pid": 1}
    s.receipt_lock_held = True


def _recorded_already(s: SimpleNamespace) -> None:
    s.serving, s.recorded, s.stale_first = TARGET, True, True


def test_every_other_decision_asks_none() -> None:
    _assert_asks_none(_recorded_already, "already_recorded", "ALREADY_RECORDED")
    _assert_asks_none(lambda s: setattr(s, "baseline", TARGET), "already_current", "ALREADY_CURRENT")
    _assert_asks_none(_hold_receipt, "deferred_gate", "DEFERRED_GATE")
    _assert_asks_none(lambda s: setattr(s, "reject", True), "failed", "FAILED")


def test_skipped_landing_asks_once() -> None:
    f = Fixture()
    f.s.delta = "M\0docs/x\0"
    result = f.run()
    assert result["decision"] == "skipped_nonproduction", "SKIPPED_NONPRODUCTION_DECISION"
    assert f.log.count("REACHED_FULL_BATTERY_REQUEST") == 1, "SKIPPED_NONPRODUCTION_ONE_REQUEST"
    assert f.requests == [{"trigger": "deploy"}], "SKIPPED_NONPRODUCTION_REQUEST_DATA"
    assert result["full_battery"] == {"status": "started", "campaign_id": "c" * 64}, "SKIPPED_NONPRODUCTION_REPLY"


def test_a_failed_request_is_one_advisory_and_keeps_the_deployment() -> None:
    for knob in ("full_battery_timeout", "full_battery_reject"):
        f = Fixture()
        setattr(f.s, knob, True)
        result = f.run()
        assert result["decision"] == "deployed", f"{knob}_KEEPS_THE_DECISION"
        assert any(note.startswith("full_battery_request_unresolved") for note in result["advisories"]), f"{knob}_IS_AN_ADVISORY"
        assert result["full_battery"] == {"status": "unresolved"}, f"{knob}_REPLY_IS_UNRESOLVED"
        assert f.log.count("REACHED_FULL_BATTERY_REQUEST") == 1, f"{knob}_IS_NOT_REPEATED"
        assert "notice" not in result and f.store.row_id(TARGET), f"{knob}_DEPLOYMENT_STAYS_RECORDED"


def test_skipped_landing_failed_request_is_one_advisory() -> None:
    for knob in ("full_battery_timeout", "full_battery_reject"):
        f = Fixture()
        f.s.delta = "M\0docs/x\0"
        setattr(f.s, knob, True)
        result = f.run()
        assert result["decision"] == "skipped_nonproduction", f"{knob}_SKIP_DECISION"
        assert any(note.startswith("full_battery_request_unresolved") for note in result["advisories"]), f"{knob}_SKIP_ADVISORY"
        assert result["full_battery"] == {"status": "unresolved"}, f"{knob}_SKIP_UNRESOLVED"
        assert f.log.count("REACHED_FULL_BATTERY_REQUEST") == 1, f"{knob}_SKIP_NOT_REPEATED"
        assert "notice" not in result, f"{knob}_SKIP_NO_NOTICE"


def main() -> None:
    failures: list[AssertionError] = []
    for test in (
        test_production_deploy_asks_once_after_the_record,
        test_request_argv_goes_through_the_bridge_and_waits,
        test_every_other_decision_asks_none,
        test_skipped_landing_asks_once,
        test_a_failed_request_is_one_advisory_and_keeps_the_deployment,
        test_skipped_landing_failed_request_is_one_advisory,
    ):
        try:
            test()
        except AssertionError as error:
            failures.append(error)
    if failures:
        raise AssertionError("; ".join(map(str, failures)))


if __name__ == "__main__":
    main()
