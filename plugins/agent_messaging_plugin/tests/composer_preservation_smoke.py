#!/usr/bin/env python3
"""Real-tmux regressions for exact single-line Codex draft preservation.

Every case drives the PRODUCTION ``_CodexTmuxDriverChannel`` through a real
tmux server against the deterministic composer in ``composer_tmux_fixture``.
Each fixture is its own ``composer-test-<uuid>`` session on a private
socket of that name; cleanup kills only that session. Human input and command
failures are injected only through the channel's subprocess and sleep seams.
The fixture's raw key, paste and submission log is the evidence.

This measures tmux buffer, key, cursor and capture behavior, not real Codex UI
compatibility. A host without tmux cannot run any case here, so the smoke
exits the dedicated skip code, 77, with the born-clone skip witness; it is
never a silent pass.
"""

from __future__ import annotations

import inspect
import sys
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT = REPO_ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from composer_tmux_fixture import FixtureSession, report_tmux_unmeasured, tmux_missing  # noqa: E402

import agent_messaging_plugin.delivery_drive as drives  # noqa: E402
import agent_messaging_plugin.session_lifecycle_control as control  # noqa: E402
from agent_messaging_plugin.codex_tmux import _CodexTmuxDriverChannel  # noqa: E402
from agent_messaging_plugin.composer_preservation import (  # noqa: E402
    COMPOSER_RECOVERY_NEEDED,
    DEFERRED_COMPOSER_ADVISORY,
    PLACEHOLDER_TEXT,
    DrainReceipt,
)

NOTICE = drives._pickup_notice({"message_id": "arm-composer", "role_name": ""})  # noqa: SLF001
DRAFT = "human unsent draft"
EXACT_DRAFTS = ("  leading spaces", "trailing spaces  ", "a\ttab", "repeated   spaces", "naïve café 日本 — ok")
_MUTATING = {"send-keys", "paste-buffer", "set-buffer"}


class _Seam:
    """The channel's subprocess seam: real tmux, plus scripted human input and failures."""

    def __init__(self, fixture: FixtureSession) -> None:
        self.fixture = fixture
        self.calls: list[tuple[list[str], float, float]] = []
        self.fail: Callable[[list[str]], bool] = lambda _argv: False
        self.before: Callable[[list[str]], None] = lambda _argv: None
        self.after: Callable[[list[str]], None] = lambda _argv: None

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.before(argv)
        start = time.monotonic()
        failed = self.fail(argv)
        result = SimpleNamespace(returncode=1, stdout="", stderr="injected") if failed else self.fixture.run(argv, **kwargs)
        self.calls.append((list(argv), start, time.monotonic()))
        self.after(argv)
        return result

    def mutations(self) -> list[tuple[list[str], float, float]]:
        return [call for call in self.calls if call[0][1] in _MUTATING]


def _once(predicate: Callable[[list[str]], bool]) -> Callable[[list[str]], bool]:
    fired: list[bool] = []

    def check(argv: list[str]) -> bool:
        if fired or not predicate(argv):
            return False
        fired.append(True)
        return True

    return check


class _HumanBeforeEnter:
    """Types ``text`` in the sleep immediately before the Nth Enter attempt."""

    def __init__(self, fixture: FixtureSession, text: str, attempt: int) -> None:
        self.fixture = fixture
        self.text = text
        self.attempt = attempt
        self.seen = 0
        self.fired = False

    def sleep(self, seconds: float) -> None:
        caller = inspect.currentframe()
        if caller is not None and caller.f_back is not None and caller.f_back.f_code.co_name == "_submit_drain_notice":
            self.seen += 1
            if self.seen == self.attempt:
                self.fixture.human(self.text)
                self.fired = True
        time.sleep(seconds)


def _channel(seam: _Seam, *, sleep: Callable[[float], None] = time.sleep, session: str = "") -> _CodexTmuxDriverChannel:
    return _CodexTmuxDriverChannel(
        tmux_bin=seam.fixture.tmux,
        session=session or seam.fixture.name,
        run_fn=seam,
        sleep_fn=sleep,
        now_fn=time.monotonic,
        poll_interval_seconds=0.05,
        stable_samples=2,
        verify_timeout_seconds=5.0,
    )


def _settle(fixture: FixtureSession) -> None:
    """Wait until any submitted turn has ended, so a late Enter would show."""
    fixture.wait_for(lambda events: not any(event["kind"] == "submit" for event in events) or events[-1]["kind"] == "idle", "turn ended")


class _FixtureCase(unittest.TestCase):
    def _fixture(self, *options: str, width: int = 200) -> FixtureSession:
        fixture = FixtureSession(width=width, options=("--busy-seconds", "1.0", *options))
        self.addCleanup(fixture.close)
        return fixture

    def _own_buffers(self, fixture: FixtureSession) -> list[str]:
        return [name for name in fixture.buffers() if name.startswith(("solet-composer-", "solet-doorbell-"))]

    def _assert_alarm(self, receipt: DrainReceipt, fixture: FixtureSession, draft: str, phase: str) -> None:
        self.assertEqual(receipt.composer, "recovery_needed", "COMPOSER_RESTORE_ALARM_TEXT")
        for part in (COMPOSER_RECOVERY_NEEDED, f"saved_draft={draft!r}", f"pane={fixture.name}", f"phase={phase}", "runtime=codex"):
            self.assertIn(part, receipt.alarm, "COMPOSER_RESTORE_ALARM_TEXT")
        buffer = receipt.alarm.split("recovery_buffer=", 1)[1].split(" ", 1)[0]
        self.assertEqual(fixture.buffer(buffer), draft, "COMPOSER_RESTORE_ALARM_TEXT recovery buffer keeps the exact draft")


class RoundTripTests(_FixtureCase):
    def test_single_line_round_trip(self) -> None:
        fixture = self._fixture()
        fixture.human(DRAFT)
        receipt = _channel(_Seam(fixture)).send_drain_notice(NOTICE)
        _settle(fixture)
        submissions = fixture.submissions()
        self.assertEqual(len(submissions), 1, f"COMPOSER_DOORBELL_SUBMITTED {receipt}")
        self.assertEqual(submissions, [NOTICE], "COMPOSER_DOORBELL_SUBMITTED owned notice only")
        self.assertEqual(fixture.state()["text"], DRAFT, "COMPOSER_DRAFT_RESTORED_EXACT")
        events = fixture.events()
        restored = max(event["seq"] for event in events if event["kind"] == "paste" and event["pasted"] == DRAFT)
        self.assertFalse([event for event in events if event["seq"] > restored and event["kind"] in {"submit", "enter_lost"}], "COMPOSER_DRAFT_RESTORED_EXACT no Enter after restore")
        self.assertEqual(receipt, DrainReceipt("turn_observed", "restored_exact", "", ("captured", "cleared", "owned_composed", "turn_observed", "restored")))
        self.assertEqual(self._own_buffers(fixture), [], "terminal cleanup retires both private buffers")
        self.assertNotIn(DRAFT, receipt.detail(), "a routine detail never carries the draft")

    def test_exact_drafts_and_placeholder(self) -> None:
        fixture = self._fixture()
        receipt = _channel(_Seam(fixture)).send_drain_notice(NOTICE)
        _settle(fixture)
        self.assertEqual((receipt.evidence, receipt.composer, fixture.state()["text"]), ("turn_observed", "", ""), "COMPOSER_PLACEHOLDER_DISTINCT dim placeholder is empty")
        for draft in (*EXACT_DRAFTS, PLACEHOLDER_TEXT):
            with self.subTest(draft=draft):
                fixture.human(draft)
                try:
                    receipt = _channel(_Seam(fixture)).send_drain_notice(NOTICE)
                    _settle(fixture)
                    self.assertNotIn(draft, fixture.submissions(), "COMPOSER_NO_HUMAN_SUBMISSION")
                    self.assertEqual(set(fixture.submissions()), {NOTICE}, "COMPOSER_NO_HUMAN_SUBMISSION")
                    self.assertEqual(fixture.state()["text"], draft, "COMPOSER_DRAFT_RESTORED_EXACT")
                    self.assertEqual(receipt.composer, "restored_exact", f"COMPOSER_PLACEHOLDER_DISTINCT literal text is a draft {receipt}")
                finally:
                    fixture.human_key("End")
                    fixture.human_key("C-u")

    def test_unstyled_placeholder_is_proved_by_cursor(self) -> None:
        fixture = self._fixture("--placeholder-style", "plain")
        receipt = _channel(_Seam(fixture)).send_drain_notice(NOTICE)
        _settle(fixture)
        self.assertEqual((receipt.composer, fixture.state()["text"], fixture.submissions()), ("", "", [NOTICE]), "COMPOSER_PLACEHOLDER_DISTINCT")
        fixture.human(PLACEHOLDER_TEXT)
        receipt = _channel(_Seam(fixture)).send_drain_notice(NOTICE)
        _settle(fixture)
        self.assertEqual((receipt.composer, fixture.state()["text"]), ("restored_exact", PLACEHOLDER_TEXT), "COMPOSER_PLACEHOLDER_DISTINCT")


class IntegrityTests(_FixtureCase):
    def test_only_own_enter_first_and_retry(self) -> None:
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                fixture = self._fixture("--lose-enters", str(attempt - 1))
                fixture.human(DRAFT)
                human = _HumanBeforeEnter(fixture, " ", attempt)
                receipt = _channel(_Seam(fixture), sleep=human.sleep).send_drain_notice(NOTICE)
                self.assertTrue(human.fired)
                self.assertEqual(fixture.submissions(), [], "COMPOSER_ONLY_OWN_ENTER")
                self.assertEqual(fixture.state()["text"], NOTICE + " ", "COMPOSER_ONLY_OWN_ENTER foreign edit untouched")
                self._assert_alarm(receipt, fixture, DRAFT, "owned_composed")

    def test_restore_alarm_text(self) -> None:
        fixture = self._fixture()
        fixture.human(DRAFT)
        seam = _Seam(fixture)
        typed: list[bool] = []

        def human_during_turn(argv: list[str]) -> None:
            if not typed and "-N" in argv and fixture.submissions():
                typed.append(True)
                fixture.human("Y")

        seam.before = human_during_turn
        receipt = _channel(seam).send_drain_notice(NOTICE)
        self.assertEqual((fixture.submissions(), fixture.state()["text"]), ([NOTICE], "Y"), "newer human text is never overwritten")
        self._assert_alarm(receipt, fixture, DRAFT, "turn_observed")

    def test_restore_paste_failure_alarms_through_delivery(self) -> None:
        fixture = self._fixture()
        fixture.human(DRAFT)
        seam = _Seam(fixture)
        seam.fail = _once(lambda argv: argv[1] == "paste-buffer" and "-d" not in argv)
        details: list[str] = []
        outcome = control._send_codex_drain_notice(_channel(seam), NOTICE, details.append)  # noqa: SLF001
        self.assertEqual(outcome, "driver_turn_observed")
        self.assertIn(f"saved_draft={DRAFT!r}", details[0], "COMPOSER_RESTORE_ALARM_TEXT")
        self.assertIn("composer=recovery_needed", details[0])
        row = {"message_id": "arm-composer", "role_name": "", "recipient_instance": "agi-a", "recipient_session": "ases-a"}
        with self.assertLogs(drives.logger, level="ERROR") as captured:
            drives._alarm_composer_recovery(cast(StateManagementInterface, None), row, details[0])  # noqa: SLF001
        self.assertTrue(all(part in captured.output[0] for part in ("arm-composer", "agi-a", "ases-a", repr(DRAFT))), "COMPOSER_RESTORE_ALARM_TEXT")
        self.assertEqual(fixture.state()["text"], "", "a failed restore is never retried blindly")

    def test_pane_serialization(self) -> None:
        fixture = self._fixture()
        fixture.human(DRAFT)
        first, second = _Seam(fixture), _Seam(fixture)
        paused, release = threading.Event(), threading.Event()

        def pause_before_clear(argv: list[str]) -> None:
            if argv[-1] == "C-u" and not paused.is_set():
                paused.set()
                release.wait(2.0)

        first.before = pause_before_clear
        receipts: dict[str, DrainReceipt] = {}
        threads = [threading.Thread(target=lambda seam=seam, key=key: receipts.__setitem__(key, _channel(seam).send_drain_notice(NOTICE))) for key, seam in (("first", first), ("second", second))]
        threads[0].start()
        self.assertTrue(paused.wait(5.0))
        threads[1].start()
        time.sleep(0.5)
        release.set()
        for thread in threads:
            thread.join(20.0)
        first_end = max(end for _argv, _start, end in first.mutations())
        second_starts = [start for _argv, start, _end in second.mutations()]
        self.assertTrue(all(start >= first_end for start in second_starts), "COMPOSER_PANE_SERIALIZATION")
        _settle(fixture)
        self.assertEqual(fixture.state()["text"], DRAFT, "COMPOSER_PANE_SERIALIZATION")
        self.assertEqual(set(fixture.submissions()), {NOTICE})
        self.assertEqual(receipts["first"].composer, "restored_exact")


class DeferredAndLifecycleTests(_FixtureCase):
    def test_deferred_states_get_zero_keys(self) -> None:
        cases: list[tuple[str, tuple[str, ...], int, Callable[[FixtureSession], object], str]] = [
            ("busy", ("--busy-seconds", "5"), 200, lambda f: (f.human("warm"), f.human_key("Enter"), f.human(DRAFT)), "pending_busy"),
            ("multiline", (), 40, lambda f: f.human("a draft long enough to wrap onto a second composer row"), "pending_composer_shape"),
            ("popup", ("--popup",), 200, lambda f: f.human("/sta"), "pending_composer_shape"),
            ("mode", (), 200, lambda f: (f.human(DRAFT), f.run([f.tmux, "copy-mode", "-t", f.name])), "pending_composer_unknown"),
        ]
        for name, options, width, prepare, evidence in cases:
            with self.subTest(state=name):
                fixture = self._fixture(*options, width=width)
                prepare(fixture)
                before = fixture.events()
                seam = _Seam(fixture)
                receipt = _channel(seam).send_drain_notice(NOTICE)
                self.assertEqual(receipt.evidence, evidence, "COMPOSER_DEFERRED_ZERO_KEYS")
                self.assertEqual(seam.mutations(), [], "COMPOSER_DEFERRED_ZERO_KEYS")
                self.assertEqual(fixture.events(), before, "COMPOSER_DEFERRED_ZERO_KEYS")
                self.assertEqual(DEFERRED_COMPOSER_ADVISORY in receipt.detail(), evidence != "pending_busy")
        fixture = self._fixture()
        seam = _Seam(fixture)
        self.assertEqual(_channel(seam, session=f"{fixture.name}-absent").send_drain_notice(NOTICE).evidence, "pending_unknown_pane")
        self.assertEqual(seam.mutations(), [], "COMPOSER_DEFERRED_ZERO_KEYS")

    def test_failure_at_each_phase_reverses_or_alarms(self) -> None:
        cases: list[tuple[str, Callable[[list[str]], bool], str, str]] = [
            ("end", lambda argv: argv[-1] == "End", "pending_end_failed", ""),
            ("save", lambda argv: argv[1] == "set-buffer" and argv[3].startswith("solet-composer-"), "pending_save_failed", "unchanged"),
            ("clear", lambda argv: argv[-1] == "C-u", "pending_clear_failed", "unchanged"),
            ("paste", lambda argv: argv[1] == "paste-buffer" and "-d" in argv, "pending_paste_failed", "restored_exact"),
            ("enter", lambda argv: argv[-1] == "Enter", "pending_enter_failed", "restored_exact"),
        ]
        for name, failure, evidence, composer in cases:
            with self.subTest(phase=name):
                fixture = self._fixture()
                fixture.human(DRAFT)
                seam = _Seam(fixture)
                seam.fail = _once(failure)
                receipt = _channel(seam).send_drain_notice(NOTICE)
                self.assertEqual((receipt.evidence, receipt.composer, receipt.alarm), (evidence, composer, ""))
                self.assertEqual((fixture.state()["text"], fixture.submissions()), (DRAFT, []), "no human text submitted; draft intact or restored")
                self.assertEqual(self._own_buffers(fixture), [], "no recovery buffer left without an alarm")

    def test_stale_snapshot_is_corrected_on_the_next_ring(self) -> None:
        fixture = self._fixture()
        fixture.human(DRAFT)
        seam = _Seam(fixture)
        seam.after = lambda argv: fixture.human("Z") if argv[1] == "show-buffer" and fixture.state()["text"] == DRAFT else None
        receipt = _channel(seam).send_drain_notice(NOTICE)
        self.assertEqual(receipt.evidence, "pending_composer_changed")
        self.assertNotIn("C-u", [argv[-1] for argv, _start, _end in seam.calls], "no blind clear after a human edit")
        self.assertEqual(fixture.state()["text"], DRAFT + "Z")
        self._assert_alarm(receipt, fixture, DRAFT, "captured")
        corrected = _channel(_Seam(fixture)).send_drain_notice(NOTICE)
        _settle(fixture)
        self.assertEqual((corrected.composer, fixture.state()["text"], fixture.submissions()), ("restored_exact", DRAFT + "Z", [NOTICE]))

    def test_duplicate_call_retarget_and_terminal_cleanup(self) -> None:
        fixture = self._fixture()
        fixture.human(DRAFT)
        for _call in range(2):
            self.assertEqual(_channel(_Seam(fixture)).send_drain_notice(NOTICE).composer, "restored_exact")
            _settle(fixture)
        self.assertEqual((fixture.submissions(), fixture.state()["text"]), ([NOTICE, NOTICE], DRAFT), "duplicate driver call keeps the draft")
        self.assertEqual(self._own_buffers(fixture), [], "terminal cleanup")
        seam = _Seam(fixture)
        reads: list[bool] = []
        original = seam.__call__

        def retargeted(argv: list[str], **kwargs: Any) -> Any:
            result = original(argv, **kwargs)
            if argv[1] == "display-message":
                reads.append(True)
                if len(reads) == 2:
                    return SimpleNamespace(returncode=0, stdout=result.stdout.replace("%", "%9"), stderr="")
            return result

        channel = _channel(seam)
        channel._run_fn = retargeted  # noqa: SLF001
        self.assertEqual(channel.send_drain_notice(NOTICE).evidence, "pending_retargeted")
        self.assertEqual(seam.mutations(), [], "retargeted pane receives zero keys")


def run_checks() -> bool:
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in (RoundTripTests, IntegrityTests, DeferredAndLifecycleTests))
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    if tmux_missing():
        sys.exit(report_tmux_unmeasured("every composer-preservation case drives a real tmux pane, so none ran"))
    sys.exit(0 if run_checks() else 1)
