#!/usr/bin/env python3
"""Unit smoke for ``_TmuxSendKeysDriverChannel`` (spawn/registration-gaps fix,
2026-08-08 — ``workbench/2026-08-08_spawn_registration_gaps_findings_
rotation-impl.md``). Offline, stub-driven — a fake ``run_fn`` records every
``tmux`` invocation and simulates pane content across successive
``capture-pane`` calls; no real tmux process. The claim that this actually
fixes the real Claude Code paste-coalescing race is proven LIVE, against a
real disposable spawn (recorded in the findings file), not by this offline
smoke — this file's job is to prove the SHAPE of the fix (ordering, the
stability-poll contract, the fail-open timeout, and that a send-primitive
failure never attempts the Enter), matching this repo's own established
split between offline-shape smokes and live-measurement legs
(``seat_rotation_helper_smoke.py`` / rotation-systematization fix loop #2).

Proves: the submitting Enter is sent ONLY after
``stable_samples_required`` CONSECUTIVE identical ``capture-pane`` reads
(kills an early-Enter mutation); any non-matching sample resets the
consecutive counter to zero (kills an off-by-one/no-reset mutation); a
timeout still sends the Enter (fail-open, not fail-closed — this channel's
own established fire-and-forget contract); and a failure sending the
literal text NEVER attempts the Enter at all.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/tmux_driver_channel_smoke.py
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import codex_eventual_pickup_smoke as pickup  # noqa: E402
from composer_tmux_fixture import FixtureSession, report_tmux_unmeasured, tmux_missing  # noqa: E402

from agent_messaging_plugin import tmux_adapter as tmux_module  # noqa: E402
from agent_messaging_plugin.codex_tmux import _CodexTmuxDriverChannel  # noqa: E402
from agent_messaging_plugin.park_drive import send_delivery_notice  # noqa: E402
from agent_messaging_plugin.session_hosts import DriverChannelSendError  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeCompletedProcess(SimpleNamespace):
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        super().__init__(stdout=stdout, returncode=returncode)


class _FakeTmuxRunner:
    """Records every invocation's argv; ``capture_pane_sequence`` is popped
    one value per ``capture-pane`` call (the last value repeats once
    exhausted, simulating a pane that settles and stays settled)."""

    def __init__(
        self,
        *,
        capture_pane_sequence: list[str] | None = None,
        fail_payload_insert: bool = False,
        fail_enter_send: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.payloads: list[str] = []
        self._capture_pane_sequence = list(capture_pane_sequence or [])
        self._last_capture_value = ""
        self._fail_payload_insert = fail_payload_insert
        self._fail_enter_send = fail_enter_send

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append(argv)
        if "capture-pane" in argv:
            if self._capture_pane_sequence:
                self._last_capture_value = self._capture_pane_sequence.pop(0)
            return _FakeCompletedProcess(stdout=self._last_capture_value)
        if "load-buffer" in argv:
            if self._fail_payload_insert:
                raise OSError("payload buffer load failed")
            self.payloads.append(str(kwargs.get("input", "")))
        if "send-keys" in argv and "Enter" in argv and self._fail_enter_send:
            raise OSError("enter send failed")
        return _FakeCompletedProcess()

    def capture_pane_call_count(self) -> int:
        return sum(1 for c in self.calls if "capture-pane" in c)

    def enter_sent(self) -> bool:
        return any("send-keys" in c and "Enter" in c for c in self.calls)

    def payload_inserted(self) -> bool:
        return any("paste-buffer" in c for c in self.calls)

    def payload_insert_index(self) -> int:
        return next(i for i, c in enumerate(self.calls) if "paste-buffer" in c)

    def send_keys_literal_used(self) -> bool:
        """Whether the OLD, head-truncating ``send-keys -l`` payload path ran.

        Kept as a named probe rather than deleted with the mechanism: this is
        the assertion that pins the 2026-09-07 fix, and a future edit that
        reintroduces ``send-keys -l`` for the payload must fail a test that
        says so in as many words."""
        return any("send-keys" in c and "-l" in c for c in self.calls)


def _fake_sleep(_seconds: float) -> None:
    return None


class _FakeClock:
    """Deterministic monotonic clock — each call advances by a fixed step,
    so a timeout test needs no real wall-clock wait."""

    def __init__(self, step: float = 1.0) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value


def test_enter_sent_only_after_n_consecutive_stable_samples() -> None:
    """Baseline-gate fix (driver-channel strand fix, 2026-08-14): the FIRST
    capture-pane read establishes the BASELINE (the pre-render screen) as
    well as seeding the cold-start ``prev`` — a value is only eligible to
    start the stability streak once it genuinely differs from that first
    look. Updated from the pre-fix fixture (five IDENTICAL samples): a
    pane that never visibly differs from its own baseline is now
    indistinguishable from the un-rendered-paste defect this fix closes
    and correctly falls through to the timeout/fail-open path instead of
    declaring quick stability (covered separately by
    ``test_timeout_still_sends_enter_fail_open``) — so demonstrating the
    N-consecutive-match counting shape now needs a fixture with a genuine
    pre/post transition: 1 baseline + 1 sample establishing the new value
    (no count, off-by-one, same shape ``wait_for_screen_stable`` has) + 3
    matching confirmations = 5 raw captures for a 3-sample streak. (This
    fixture's baseline already differs from its stable value on the very
    first transition, so it does not itself distinguish the baseline-gated
    algorithm from the pre-fix one — that discriminating coverage lives in
    ``tmux_adapter_smoke.py``'s ``test_driver_channel_enter_waits_for_
    baseline_change``, whose fixture holds the PRE-send screen steady across
    the whole stability window before switching. This test's own job stays
    the N-consecutive-match counting shape and the call-ordering invariants
    below.)
    """
    runner = _FakeTmuxRunner(capture_pane_sequence=["A", "B", "B", "B", "B"])
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux",
        session="s1",
        run_fn=runner,
        stable_samples_required=3,
        stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
    )
    channel.send("hello")
    _check(runner.payload_inserted(), "the payload is inserted into the composer")
    _check(
        runner.capture_pane_call_count() == 5,
        f"5 capture-pane reads for a 3-sample streak (1 baseline + 1 transition sample + 3 confirmations), got {runner.capture_pane_call_count()}",
    )
    _check(runner.enter_sent(), "Enter is sent once the pane stabilizes")
    literal_idx = runner.payload_insert_index()
    enter_idx = next(i for i, c in enumerate(runner.calls) if "Enter" in c)
    _check(enter_idx > literal_idx, "Enter is sent strictly AFTER the payload insert")
    capture_indices = [i for i, c in enumerate(runner.calls) if "capture-pane" in c]
    _check(
        all(literal_idx < i < enter_idx for i in capture_indices),
        "every capture-pane read happens strictly between the payload insert and the Enter",
    )


def test_non_matching_sample_resets_the_consecutive_counter() -> None:
    """RED-FIRST regression against an early-settle mutation: a stray
    non-matching sample (e.g. mid-render) must not count toward the streak
    -- kills a mutation that forgets to reset on mismatch."""
    runner = _FakeTmuxRunner(capture_pane_sequence=["A", "B", "B", "B"])
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux",
        session="s1",
        run_fn=runner,
        stable_samples_required=3,
        stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
    )
    channel.send("hello")
    _check(
        runner.capture_pane_call_count() == 5,
        f"a mismatch resets the streak -- needs 5 samples (baseline, then a mismatch that becomes the new baseline, then 3 confirmations), got {runner.capture_pane_call_count()}",
    )
    _check(runner.enter_sent(), "Enter is still eventually sent once the pane genuinely stabilizes")


def test_timeout_still_sends_enter_fail_open() -> None:
    """The channel's own established contract is fire-and-forget best-
    effort (never silently do nothing) -- a pane that never stabilizes must
    still get the Enter, not be abandoned mid-composer forever."""
    ever_changing = [f"frame-{i}" for i in range(1000)]
    runner = _FakeTmuxRunner(capture_pane_sequence=ever_changing)
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux",
        session="s1",
        run_fn=runner,
        stable_samples_required=3,
        stable_timeout_seconds=2.0,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=1.0),
    )
    channel.send("hello")
    _check(runner.enter_sent(), "Enter is still sent (fail-open) after a stability timeout")


def test_payload_insert_failure_never_attempts_enter() -> None:
    runner = _FakeTmuxRunner(fail_payload_insert=True)
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux",
        session="s1",
        run_fn=runner,
        stable_samples_required=3,
        stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
    )
    channel.send("hello")  # must not raise -- fire-and-forget contract
    _check(
        not runner.enter_sent(),
        "Enter is NEVER attempted when the payload insert itself failed",
    )
    _check(
        runner.capture_pane_call_count() == 0,
        "no stability wait is even attempted after a failed payload insert",
    )


def test_submit_after_settling_insert_never_reinserts_text() -> None:
    """Red-first: a later submit must not put a second copy in the composer."""
    runner = _FakeTmuxRunner(
        capture_pane_sequence=["before", "after", "after", "after", "after"],
    )
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux",
        session="s1",
        run_fn=runner,
        stable_samples_required=3,
        stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
    )
    channel.insert("large\\npaste")
    channel.submit()
    payload_inserts = [call for call in runner.calls if "paste-buffer" in call]
    _check(len(payload_inserts) == 1, "submit() cannot reinsert settling text")
    _check(runner.enter_sent(), "submit() sends Enter after the paste settles")


def test_payload_insert_uses_bracketed_paste_never_send_keys_literal() -> None:
    """★ THE KILLING TEST for the 2026-09-07 head-truncation defect.

    ``send-keys -l`` was measured DROPPING THE HEAD of a large payload
    against a real Claude Code TUI pane: a 10.8KB / 142-line message
    beginning ``HEADMARKERZZ1`` and ending ``TAILMARKERZZ2`` was sent, and
    the receiving model — asked for the first and last 25 characters of what
    it had actually received — reported filler from the MIDDLE of the
    payload plus the intact tail, with the head marker appearing nowhere in
    the pane's entire scrollback. The identical payload delivered through
    ``load-buffer`` + ``paste-buffer -p`` arrived head-intact. Nothing
    raised, logged, or reported a failure on the truncating path: dispatch
    briefs were arriving with their opening scope and authority sections
    silently missing.

    This pins BOTH halves, because either alone is satisfiable by a
    mutation that keeps the defect: the payload must go through the buffer
    path (with ``-p``, so the payload's own newlines stay literal instead of
    racing the submitting Enter), AND ``send-keys -l`` must not carry it.
    """
    runner = _FakeTmuxRunner(capture_pane_sequence=["A", "B", "B", "B", "B"])
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux",
        session="s1",
        run_fn=runner,
        stable_samples_required=3,
        stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
    )
    payload = "line one\nline two\nline three"
    channel.send(payload)
    _check(
        not runner.send_keys_literal_used(),
        "the payload NEVER goes through send-keys -l (the head-truncating path)",
    )
    _check(
        any("load-buffer" in c for c in runner.calls),
        "the payload is loaded into a tmux buffer",
    )
    paste_calls = [c for c in runner.calls if "paste-buffer" in c]
    _check(len(paste_calls) == 1, "the payload is pasted exactly once")
    _check("-p" in paste_calls[0], "the paste is BRACKETED (-p), keeping newlines literal")
    _check("-d" in paste_calls[0], "the payload buffer is deleted after the paste")
    _check(
        runner.payloads == [payload],
        f"the buffer carries the payload verbatim, got {runner.payloads!r}",
    )
    load_call = next(c for c in runner.calls if "load-buffer" in c)
    paste_call = paste_calls[0]
    load_buffer_name = load_call[load_call.index("-b") + 1]
    paste_buffer_name = paste_call[paste_call.index("-b") + 1]
    _check(
        load_buffer_name == paste_buffer_name,
        "the paste reads back the buffer this call loaded",
    )
    other = _FakeTmuxRunner(capture_pane_sequence=["A", "B", "B", "B", "B"])
    tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux",
        session="s2",
        run_fn=other,
        stable_samples_required=3,
        stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
    ).send("a different lane's brief")
    other_load = next(c for c in other.calls if "load-buffer" in c)
    _check(
        other_load[other_load.index("-b") + 1] != load_buffer_name,
        "two concurrent drives never share a buffer name (one lane cannot paste another lane's payload)",
    )


def test_insert_raises_when_the_payload_buffer_load_fails() -> None:
    """``insert()`` used to call ``_insert`` and DISCARD its boolean, so a
    caller using the declared insert/submit split (``session_hosts.
    DriverChannel``) got a silent no-op followed by a bare ``Enter`` into
    whatever the composer already held. The Codex twin raises here; this one
    now does too.

    ``send()`` is deliberately NOT covered by this change and is asserted
    alongside: it keeps consuming ``_insert``'s boolean, so its established
    fire-and-forget contract (a dead pane is a logged warning, never an
    exception through the verb layer) is unchanged.
    """
    runner = _FakeTmuxRunner(fail_payload_insert=True)
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux",
        session="s1",
        run_fn=runner,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
    )
    raised = False
    try:
        channel.insert("some work")
    except DriverChannelSendError:
        raised = True
    _check(raised, "insert() RAISES when the payload never reached the composer")
    _check(not runner.enter_sent(), "a failed insert never leaves a bare Enter behind")

    fire_and_forget = _FakeTmuxRunner(fail_payload_insert=True)
    tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux",
        session="s1",
        run_fn=fire_and_forget,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
    ).send("some work")  # must not raise
    _check(
        not fire_and_forget.enter_sent(),
        "send() stays fire-and-forget and still refuses the Enter",
    )


class _CodexWedgeRunner:
    """Hermetic Codex composer model for the stable-gate rollback proofs."""

    def __init__(self, *, stranded_text: str) -> None:
        self.calls: list[list[str]] = []
        self._stranded_text = stranded_text
        self.composer = ""
        self._mode = "idle"
        self._unstable_captures = 0

    def __call__(self, argv: list[str], **_kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append(argv)
        if "capture-pane" in argv:
            return _FakeCompletedProcess(stdout=self._capture())
        if "send-keys" in argv and "-l" in argv:
            text = argv[-1]
            self.composer = text
            self._mode = "unstable" if text == self._stranded_text else "stable"
        elif "send-keys" in argv and argv[-1] == "C-u":
            self.composer = ""
            self._mode = "idle"
        return _FakeCompletedProcess()

    def _capture(self) -> str:
        if not self.composer:
            return "› Ask Codex to do anything"
        if self._mode == "unstable":
            self._unstable_captures += 1
            if self._unstable_captures < 3:
                return f"› {self.composer} rendering-{self._unstable_captures}"
        return f"› {self.composer}"

    def literal_texts(self) -> list[str]:
        return [call[-1] for call in self.calls if "send-keys" in call and "-l" in call]

    def clear_sent(self) -> bool:
        return any(call[-1:] == ["C-u"] for call in self.calls)

    def enter_sent(self) -> bool:
        return any(call[-1:] == ["Enter"] for call in self.calls)


def _codex_wedge_channel(runner: _CodexWedgeRunner) -> _CodexTmuxDriverChannel:
    return _CodexTmuxDriverChannel(
        tmux_bin="tmux",
        session="codex-wedge-proof",
        run_fn=runner,
        sleep_fn=_fake_sleep,
        now_fn=_FakeClock(step=0.1),
        stable_samples=2,
        verify_timeout_seconds=0.4,
    )


def test_stable_failure_rolls_back_to_verified_idle_composer() -> None:
    """RED FIRST: a post-literal stable timeout must not strand the text.

    Failing mutation: delete the failed-insert rollback. The pre-fix channel
    raises the same stable-gate error but leaves ``stranded_text`` in the fake
    composer; this assertion fails before the repair and proves the damaging
    path rather than the benign ready-gate miss.
    """
    stranded_text = "delivery waiting from Coordinator — drain peer_inbox"
    runner = _CodexWedgeRunner(stranded_text=stranded_text)
    channel = _codex_wedge_channel(runner)
    failure = ""
    try:
        channel.insert(stranded_text)
    except DriverChannelSendError as exc:
        failure = str(exc)
    _check(
        failure == ("Codex tmux pane 'codex-wedge-proof' did not stabilize before Enter; submission was not attempted."),
        "stable-gate failure retains its original DriverChannelSendError message",
    )
    _check(
        runner.composer == "" and "Ask Codex to do anything" in runner._capture(),
        "RED proof 1: failed post-literal insert leaves the composer empty with its idle placeholder",
    )
    _check(runner.clear_sent(), "rollback clears only the failed call's confirmed composer text")
    _check(not runner.enter_sent(), "rollback never sends Enter")


def test_stable_failure_does_not_wedge_the_next_ready_gate() -> None:
    """RED FIRST: without rollback, stranded text erases the next idle prompt.

    Failing mutation: remove rollback or change it to a no-op. The second
    insert then fails at ``_wait_until_ready`` before it can send anything,
    exactly the permanent wedge this repair prevents.
    """
    stranded_text = "delivery waiting from Coordinator — drain peer_inbox"
    follow_up_text = "follow-up"
    runner = _CodexWedgeRunner(stranded_text=stranded_text)
    channel = _codex_wedge_channel(runner)
    with contextlib.suppress(DriverChannelSendError):
        channel.insert(stranded_text)
    next_failure = ""
    try:
        channel.insert(follow_up_text)
    except DriverChannelSendError as exc:
        next_failure = str(exc)
    _check(
        not next_failure,
        "RED proof 2: the next ready gate sees the restored idle placeholder, not stranded text",
    )
    _check(
        runner.literal_texts() == [stranded_text, follow_up_text],
        "the next insert reaches its literal send after rollback",
    )


def test_ready_gate_busy_pane_remains_a_benign_no_paste_failure() -> None:
    """Control: deleting the ready check makes this red by pasting into busy UI."""
    runner = _CodexWedgeRunner(stranded_text="unused")
    runner.composer = "busy human draft"
    channel = _codex_wedge_channel(runner)
    failure = ""
    try:
        channel.insert("must not paste")
    except DriverChannelSendError as exc:
        failure = str(exc)
    _check("never reached an idle prompt" in failure, "busy pane still fails at the ready gate")
    _check(runner.literal_texts() == [], "busy ready-gate failure pastes nothing")
    _check(
        not runner.clear_sent(),
        "busy ready-gate failure never clears a composer it does not own",
    )


class _DrainFailurePane(pickup._Pane):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode, self.idle, self.notice = mode, self.visible, ""
        self.literal_attempts = 0
        self.cleared = False
        self.capture_trace: list[str] = []

    def run(self, argv: list[str], **_kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append(list(argv))
        if "display-message" in argv:  # act-time pane identity for the per-pane doorbell lock
            return _FakeCompletedProcess(stdout=f"1:$1:%{id(self)} 0 99 200 0\n")
        if "capture-pane" in argv:
            if (bool(self.literal_attempts), self.mode, self.cleared) in {
                (True, "unknown", False),
                (True, "unknown", True),
                (True, "unknown_after_clear", True),
            }:
                self.capture_trace.append("unavailable")
                return _FakeCompletedProcess(returncode=1)
            if self.literal_attempts:
                frame = len(self.capture_trace)
                self.visible = {
                    (True, False): f"{self.idle}\nframe-{frame}\n› foreign draft",
                    (True, True): f"{self.idle}\nframe-{frame}\n› foreign draft",
                    (False, True): self.idle,
                    (False, False): f"frame-{frame}\n› {self.notice}",
                }[(self.mode == "foreign", self.cleared)]
            current = self.visible
            self.capture_trace.append(current)
            return _FakeCompletedProcess(stdout=current)
        if "send-keys" in argv and "-l" in argv:
            self.literal_attempts += 1
            self.notice = argv[-1]
            self.visible = f"› {self.notice}"
            return _FakeCompletedProcess(returncode=int(self.mode == "literal_nonzero"))
        if argv[-1] == "C-u":
            self.cleared = self.mode != "clear_failure"
            self.visible = (self.visible, self.idle)[self.cleared]
            return _FakeCompletedProcess(returncode=int(not self.cleared))
        return _FakeCompletedProcess()


def test_drain_failed_capture_records_stranded_possible() -> None:
    fixture, pane = pickup._PickupFixture(), _DrainFailurePane("unknown")
    try:
        fixture.setUp()
        fixture.addCleanup(fixture.tearDown)
        fixture.panes["pane-a"] = pane
        fixture._send("arm-partial")
        fixture.executor.flush()
        row, evidence = fixture._read("arm-partial"), fixture.store.history("arm-partial")[-1]["evidence"]
        print("TRACE", pane.literal_attempts, pane.capture_trace, row["reason"])
        _check("pending_partial:stranded_possible" in str(row["reason"]), "DRAIN_FAILED_CAPTURE_HAS_STRANDED_POSSIBLE_RECEIPT")
        _check(row["pending"] is True and row["phase"] == "driver_error", "unknown failed capture remains pending driver_error")
        _check(pane.literal_attempts == 1 and not any(k[-1] in {"C-u", "Enter"} for k in pane.keys()), "unknown capture never clears or submits")
        _check(isinstance(evidence, dict) and evidence.get("reason") == row["reason"] and evidence.get("attempts") == 1, "failed-capture receipt is in immutable history")
    finally:
        fixture.doCleanups()


def test_drain_owned_cleanup_and_foreign_preservation() -> None:
    cases = [("owned", "cleanup_verified", "empty", 1), ("foreign", "stranded_possible", "foreign", 0), ("unknown_after_clear", "stranded_possible", "empty", 1), ("clear_failure", "stranded_possible", "own", 1), ("literal_nonzero", "cleanup_verified", "empty", 1)]
    fixture = pickup._PickupFixture()
    try:
        fixture.setUp()
        fixture.addCleanup(fixture.tearDown)
        for mode, receipt, composer, clears in cases:
            message, pane = f"arm-{mode}", _DrainFailurePane(mode)
            fixture.panes["pane-a"] = pane
            fixture._send(message)
            fixture.executor.flush()
            row, evidence = fixture._read(message), fixture.store.history(message)[-1]["evidence"]
            visible = pane.visible.endswith({"empty": pane.idle, "foreign": "› foreign draft", "own": f"› {pane.notice}"}[composer])
            _check(f"pending_partial:{receipt}" in str(row["reason"]), f"{message} records {receipt}: {row['reason']}")
            _check(visible, f"{message} preserves or verifies expected composer: {pane.visible!r}")
            _check(sum(k[-1] == "C-u" for k in pane.keys()) == clears and not any(k[-1] == "Enter" for k in pane.keys()), f"{message} clear/Enter counts are safe")
            _check(row["pending"] is True and row["phase"] == "driver_error" and isinstance(evidence, dict) and evidence.get("reason") == row["reason"], f"{message} history retains pending driver_error")
    finally:
        fixture.doCleanups()


def test_drain_receptive_delivers_once() -> None:
    fixture = pickup._PickupFixture()
    try:
        fixture.setUp()
        fixture.addCleanup(fixture.tearDown)
        message, pane = "arm-receptive", fixture.panes["pane-a"]
        fixture._send(message)
        fixture.executor.flush()
        row = fixture._read(message)
        _check(sum("-l" in keys for keys in pane.keys()) == 1 and pane.enters == 1 and row["phase"] == "driver_turn_observed", "receptive drain pastes and submits once")
        page = fixture._service([fixture._message(message)]).peer_inbox(pickup.PeerInboxRequest(recipient_agent_id="codex", recipient_agent_instance_id="agi-a"))
        _check(row["pending"] is True and [entry.message.id for entry in page.entries] == [message] and fixture._read(message)["phase"] == "inbox_returned" and fixture._read(message)["pending"] is False, "only exact recipient inbox return settles pickup")
        keys = pane.keys()
        pickup.drives.sweep_codex_pickups(fixture.state)
        fixture.executor.flush()
        _check(pane.keys() == keys and fixture._read(message)["phase"] == "inbox_returned", "terminal inbox return is not redriven")
    finally:
        fixture.doCleanups()


_NOTICE = "delivery waiting from Coordinator \u2014 drain peer_inbox"
_DRAFT = "my half-typed draft"


@contextlib.contextmanager
def _claude_like_pane(*options: str) -> Any:
    """A REAL tmux pane on a private socket drawing a Claude-shaped composer."""
    fixture = FixtureSession(options=("--claude-like", "--busy-seconds", "0.2", *options))
    try:
        yield fixture
    finally:
        fixture.close()


def _real_channel(fixture: FixtureSession, *, boundary_seconds: float | None = None, **kwargs: Any) -> tmux_module._TmuxSendKeysDriverChannel:  # noqa: SLF001
    """The base (blind-send) channel, or the shipped boundary channel with an explicit bound."""
    if boundary_seconds is None:
        return tmux_module._TmuxSendKeysDriverChannel(tmux_bin=fixture.tmux, session=fixture.name, run_fn=fixture.run, **kwargs)  # noqa: SLF001
    channel = tmux_module._BoundarySendKeysDriverChannel(tmux_bin=fixture.tmux, session=fixture.name, run_fn=fixture.run, **kwargs)  # noqa: SLF001
    channel._send_boundary_timeout_seconds = boundary_seconds  # noqa: SLF001
    return channel


def _pastes(fixture: FixtureSession) -> list[str]:
    return [str(event["pasted"]) for event in fixture.events() if event["kind"] == "paste"]


def _screen(fixture: FixtureSession) -> str:
    return str(fixture.run([fixture.tmux, "capture-pane", "-t", fixture.name, "-p"]).stdout)


def test_real_pane_send_waits_for_the_composer_to_empty_then_submits_exactly_once() -> None:
    """Acceptance (a)+(d): the production adapter's ``driver_channel()`` (which
    carries the shipped bound) drives a REAL pane whose composer holds a human
    draft; nothing is inserted while it is occupied; once the composer is
    emptied inside the bound the text is inserted and submitted exactly once."""
    with _claude_like_pane() as fixture:
        driver = tmux_module.TmuxHostDriver(tmux_bin=fixture.tmux, run_fn=fixture.run)
        channel = driver.driver_channel(fixture.name)
        _check(channel is not None, "driver_channel() resolves the real pane")
        assert channel is not None
        fixture.human(_DRAFT)
        errors: list[BaseException] = []

        def _drive() -> None:
            try:
                channel.send(_NOTICE)
            except BaseException as exc:  # noqa: BLE001 -- surfaced to the main thread
                errors.append(exc)

        worker = threading.Thread(target=_drive)
        worker.start()
        time.sleep(1.5)
        _check(worker.is_alive(), "send() is still waiting while the composer is occupied")
        _check(_pastes(fixture) == [] and fixture.submissions() == [], "nothing was pasted or submitted into the occupied composer")
        _check(fixture.state()["text"] == _DRAFT, "the human draft is untouched while send() waits")
        fixture.human_key("C-u")
        worker.join(30)
        _check(not worker.is_alive() and not errors, f"send() returned without error once the composer emptied ({errors})")
        _check(_pastes(fixture) == [_NOTICE], f"exactly one copy reached the pane ({_pastes(fixture)})")
        _check(fixture.submissions() == [_NOTICE], f"the text was submitted exactly once ({fixture.submissions()})")
        _check(fixture.state()["text"] == "", "the composer is empty afterwards")


def test_real_pane_send_into_an_already_empty_composer_does_not_wait() -> None:
    """Already-idle case: the boundary is found on the first sample."""
    with _claude_like_pane() as fixture:
        channel = _real_channel(fixture, boundary_seconds=10.0)
        started = time.monotonic()
        channel.send(_NOTICE)
        elapsed = time.monotonic() - started
        _check(_pastes(fixture) == [_NOTICE] and fixture.submissions() == [_NOTICE], "an empty composer gets the text pasted and submitted exactly once")
        _check(elapsed < 8.0, f"no boundary wait was spent on an empty composer ({elapsed:.1f}s)")


def test_real_pane_deadline_sends_nothing_and_reports_not_delivered() -> None:
    """Acceptance (b): an occupied composer for longer than the bound gets NO
    paste and NO key; the pane is byte-for-byte what it was, and the existing
    delivery-notice mapping reports ``driver_error`` -- never sent/scheduled."""
    with _claude_like_pane() as fixture:
        channel = _real_channel(fixture, boundary_seconds=1.5)
        fixture.human(_DRAFT)
        screen_before, events_before = _screen(fixture), fixture.events()
        raised: list[Exception] = []
        try:
            channel.send(_NOTICE)
        except DriverChannelSendError as exc:
            raised.append(exc)
        _check(len(raised) == 1 and "nothing was sent" in str(raised[0]), f"send() raises the existing DriverChannelSendError ({raised})")
        _check(fixture.events() == events_before, "the pane received no paste and no key (event log unchanged)")
        _check(_screen(fixture) == screen_before, "the rendered pane is byte-for-byte unchanged")
        _check(fixture.state()["text"] == _DRAFT and fixture.buffers() == [], "the draft is intact and no tmux buffer was even loaded")
        details: list[str] = []
        outcome = send_delivery_notice(
            channel,
            row={"agent_instance_id": "agi-real-pane"},
            recipient_agent_instance_id="agi-real-pane",
            notice=_NOTICE,
            detail_sink=details.append,
            park_detail=None,
            driver_sent="driver_sent",
            driver_error="driver_error",
            record_error_detail=lambda sink, exc: sink and sink(str(exc)),
            logger=tmux_module.logger,
        )
        _check(outcome == "driver_error" and fixture.events() == events_before, f"the delivery layer reports driver_error and the pane is still untouched ({outcome})")


def test_driver_channel_carries_the_shipped_bound_and_direct_channels_do_not_wait() -> None:
    """The wiring: only ``driver_channel()`` enables the wait; a directly
    constructed channel keeps the legacy blind send (bound 0)."""
    with _claude_like_pane() as fixture:
        driver = tmux_module.TmuxHostDriver(tmux_bin=fixture.tmux, run_fn=fixture.run)
        shipped = driver.driver_channel(fixture.name)
        assert shipped is not None
        _check(type(shipped) is tmux_module._BoundarySendKeysDriverChannel and shipped._send_boundary_timeout_seconds == tmux_module.DEFAULT_SEND_BOUNDARY_TIMEOUT_SECONDS > 0, "driver_channel() returns the boundary channel with DEFAULT_SEND_BOUNDARY_TIMEOUT_SECONDS")  # noqa: SLF001
        _check(not isinstance(_real_channel(fixture), tmux_module._BoundarySendKeysDriverChannel), "a directly constructed base channel keeps the legacy blind send (no wait)")  # noqa: SLF001


def test_real_pane_no_resend_invariant_and_drive_classification_are_unchanged() -> None:
    """Acceptance (c) on a REAL pane: a positively stranded composer is still
    ``False`` (distinct from a could-not-determine ``None``), a confirmed
    submit is still ``True``, and verification never sends another key."""
    with _claude_like_pane("--lose-enters", "100") as fixture:
        channel = _real_channel(fixture, boundary_seconds=5.0, drive_verify_timeout_seconds=10.0)
        channel.send(_NOTICE)
        _check(channel.verify_driven(_NOTICE) is False, "text left in the composer is POSITIVELY stranded (False)")
        _check(_pastes(fixture) == [_NOTICE] and sum(e["kind"] == "enter_lost" for e in fixture.events()) == 1, "verification re-sent nothing: one paste, one Enter attempt")
    with _claude_like_pane() as fixture:
        channel = _real_channel(fixture, drive_verify_timeout_seconds=2.0)
        events_before = fixture.events()
        _check(channel.verify_driven(_NOTICE) is None, "idle pane where the text never landed is could-not-determine (None), not False")
        _check(fixture.events() == events_before, "the UNVERIFIED path sent no key")
    with _claude_like_pane("--claude-text-sgr", "0") as fixture:
        channel = _real_channel(fixture, drive_verify_timeout_seconds=20.0)
        _check(channel._insert(_NOTICE), "the text lands in the composer")  # noqa: SLF001
        result: list[bool | None] = []
        worker = threading.Thread(target=lambda: result.append(channel.verify_driven(_NOTICE)))
        worker.start()
        time.sleep(1.2)
        fixture.human_key("Enter")
        worker.join(30)
        _check(result == [True] and fixture.submissions() == [_NOTICE], f"landed-then-gone is a confirmed submit (True) ({result})")


def main() -> int:
    from codex_eventual_pickup_smoke import run_checks  # noqa: PLC0415

    print("=== tmux driver channel smoke ===")
    test_drain_failed_capture_records_stranded_possible()
    test_drain_owned_cleanup_and_foreign_preservation()
    test_drain_receptive_delivers_once()
    test_enter_sent_only_after_n_consecutive_stable_samples()
    test_non_matching_sample_resets_the_consecutive_counter()
    test_timeout_still_sends_enter_fail_open()
    test_payload_insert_failure_never_attempts_enter()
    test_payload_insert_uses_bracketed_paste_never_send_keys_literal()
    test_insert_raises_when_the_payload_buffer_load_fails()
    test_submit_after_settling_insert_never_reinserts_text()
    test_stable_failure_rolls_back_to_verified_idle_composer()
    test_stable_failure_does_not_wedge_the_next_ready_gate()
    test_ready_gate_busy_pane_remains_a_benign_no_paste_failure()
    real_pane_unmeasured = tmux_missing()
    if not real_pane_unmeasured:
        test_real_pane_send_waits_for_the_composer_to_empty_then_submits_exactly_once()
        test_real_pane_send_into_an_already_empty_composer_does_not_wait()
        test_real_pane_deadline_sends_nothing_and_reports_not_delivered()
        test_driver_channel_carries_the_shipped_bound_and_direct_channels_do_not_wait()
        test_real_pane_no_resend_invariant_and_drive_classification_are_unchanged()
    _check(run_checks(), "Codex eventual-pickup driver and sweep regressions")
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    if real_pane_unmeasured:
        return report_tmux_unmeasured(f"the five real-pane tests did not run; the {_passed} stub-driven checks ran and passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
