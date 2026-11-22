"""Fake-only proof: a running full-campaign receipt is noted, not a hold.

Loads the shipped deploy-by-rule module through the shared fixture. Every
receipt, lock and bridge reply is synthetic, except the real lock port case,
which takes a real flock on a temporary receipt lock.
"""

from __future__ import annotations

import fcntl
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deploy_by_rule_fixture_support import Fixture, writes  # noqa: E402

CAMPAIGN = "scheduled:" + "c" * 64 + ":1"


def _running(campaign: str | None, held: bool) -> Fixture:
    f = Fixture()
    receipt: dict[str, object] = {"result": "running", "pid": 1}
    if campaign is not None:
        receipt["campaign"] = campaign
    f.s.receipt = receipt
    f.s.receipt_lock_held = held
    return f


def test_full_campaign_receipt_is_not_a_hold() -> None:
    f = _running(CAMPAIGN, held=True)
    result = f.run()
    assert result["decision"] == "deployed" and writes(f) == ["REACHED_APPLY_COMMIT", "REACHED_RECORD"], "CAMPAIGN_RECEIPT_NOT_A_HOLD"
    assert "full_campaign_running_not_a_hold:/fixture/receipt.json" in result["advisories"], "CAMPAIGN_RECEIPT_IS_NOTED"


def test_other_campaign_receipt_still_defers() -> None:
    f = _running("default", held=True)
    result = f.run()
    assert result["decision"] == "deferred_gate" and result["receipt"] == "/fixture/receipt.json", "NON_SCHEDULED_RECEIPT_DEFERS"


def test_receipt_without_campaign_still_defers() -> None:
    f = _running(None, held=True)
    result = f.run()
    assert result["decision"] == "deferred_gate", "RECEIPT_WITHOUT_CAMPAIGN_DEFERS"


def test_later_held_receipt_defers_after_campaign_receipt() -> None:
    f = Fixture()
    f.s.receipt_lock_held = True
    files = {
        "/fixture/campaign.json": {"result": "running", "pid": 1, "campaign": CAMPAIGN},
        "/fixture/hand.json": {"result": "running", "pid": 2, "campaign": "default"},
    }

    def read(path: Path) -> bytes:
        return json.dumps(files[str(path)]).encode() if str(path) in files else f.read(path)

    ports = replace(f.ports, receipts=lambda: [Path(name) for name in files], read=read)
    result = f.m.deploy(ports, f.args)
    assert result["decision"] == "deferred_gate" and result["receipt"] == "/fixture/hand.json", "LATER_HELD_RECEIPT_DEFERS"


def test_unheld_campaign_receipt_continues() -> None:
    f = _running(CAMPAIGN, held=False)
    result = f.run()
    assert result["decision"] != "deferred_gate", "UNHELD_CAMPAIGN_RECEIPT_DOES_NOT_DEFER"
    assert "receipt_stale_running:/fixture/receipt.json" in result["advisories"], "UNHELD_CAMPAIGN_RECEIPT_IS_STALE"
    assert not any(note.startswith("full_campaign_running_not_a_hold") for note in result["advisories"]), "UNHELD_CAMPAIGN_RECEIPT_IS_NOT_NOTED_AS_HELD"


def test_passed_campaign_receipt_is_ignored() -> None:
    f = Fixture()
    f.s.receipt = {"result": "passed", "pid": 1, "campaign": CAMPAIGN}
    f.s.receipt_lock_held = True
    result = f.run()
    assert result["decision"] == "deployed" and not any("full_campaign" in note for note in result["advisories"]), "PASSED_CAMPAIGN_RECEIPT_IS_IGNORED"


def test_real_lock_port_with_campaign_receipt() -> None:
    f = Fixture()
    f.s.receipt = {"result": "running", "pid": 1, "campaign": CAMPAIGN}
    with tempfile.TemporaryDirectory() as temp:
        receipt_path = Path(temp) / "receipt.json"
        lock_path = receipt_path.with_suffix(".lock")
        receipt_path.write_text(json.dumps(f.s.receipt))
        lock_path.touch()
        ports = replace(f.ports, receipts=lambda: [receipt_path], receipt_lock_held=f.m.receipt_lock_held)
        with lock_path.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = f.m.deploy(ports, f.args)
    assert result["decision"] == "deployed", "PRODUCTION_CAMPAIGN_RECEIPT_IS_NOT_A_HOLD"


def main() -> None:
    failures: list[AssertionError] = []
    for test in (
        test_full_campaign_receipt_is_not_a_hold,
        test_other_campaign_receipt_still_defers,
        test_receipt_without_campaign_still_defers,
        test_later_held_receipt_defers_after_campaign_receipt,
        test_unheld_campaign_receipt_continues,
        test_passed_campaign_receipt_is_ignored,
        test_real_lock_port_with_campaign_receipt,
    ):
        try:
            test()
        except AssertionError as error:
            failures.append(error)
    if failures:
        raise AssertionError("; ".join(map(str, failures)))


if __name__ == "__main__":
    main()
