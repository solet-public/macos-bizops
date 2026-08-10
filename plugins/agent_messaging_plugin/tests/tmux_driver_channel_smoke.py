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

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import tmux_adapter as tmux_module  # noqa: E402

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


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class _FakeTmuxRunner:
    """Records every invocation's argv; ``capture_pane_sequence`` is popped
    one value per ``capture-pane`` call (the last value repeats once
    exhausted, simulating a pane that settles and stays settled)."""

    def __init__(
        self, *, capture_pane_sequence: list[str] | None = None,
        fail_literal_send: bool = False, fail_enter_send: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self._capture_pane_sequence = list(capture_pane_sequence or [])
        self._last_capture_value = ""
        self._fail_literal_send = fail_literal_send
        self._fail_enter_send = fail_enter_send

    def __call__(self, argv: list[str], **_kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append(argv)
        if "capture-pane" in argv:
            if self._capture_pane_sequence:
                self._last_capture_value = self._capture_pane_sequence.pop(0)
            return _FakeCompletedProcess(stdout=self._last_capture_value)
        if "send-keys" in argv and "-l" in argv and self._fail_literal_send:
            raise OSError("literal send failed")
        if "send-keys" in argv and "Enter" in argv and self._fail_enter_send:
            raise OSError("enter send failed")
        return _FakeCompletedProcess()

    def capture_pane_call_count(self) -> int:
        return sum(1 for c in self.calls if "capture-pane" in c)

    def enter_sent(self) -> bool:
        return any("send-keys" in c and "Enter" in c for c in self.calls)

    def literal_text_sent(self) -> bool:
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
    """Matches ``wait_for_screen_stable``'s own established semantics
    exactly: the FIRST sample has no predecessor to match against (it only
    establishes the baseline), so a streak of N consecutive matches always
    needs N+1 raw captures from a cold start -- this is the same off-by-one
    shape the iTerm2 precedent has, not a defect."""
    runner = _FakeTmuxRunner(capture_pane_sequence=["A", "A", "A", "A", "A"])
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    )
    channel.send("hello")
    _check(runner.literal_text_sent(), "the literal text is sent")
    _check(
        runner.capture_pane_call_count() == 4,
        f"4 capture-pane reads for a 3-sample streak (1 baseline + 3 confirmations), "
        f"got {runner.capture_pane_call_count()}",
    )
    _check(runner.enter_sent(), "Enter is sent once the pane stabilizes")
    literal_idx = next(i for i, c in enumerate(runner.calls) if "-l" in c)
    enter_idx = next(i for i, c in enumerate(runner.calls) if "Enter" in c)
    _check(enter_idx > literal_idx, "Enter is sent strictly AFTER the literal text send")
    capture_indices = [i for i, c in enumerate(runner.calls) if "capture-pane" in c]
    _check(
        all(literal_idx < i < enter_idx for i in capture_indices),
        "every capture-pane read happens strictly between the text send and the Enter",
    )


def test_non_matching_sample_resets_the_consecutive_counter() -> None:
    """RED-FIRST regression against an early-settle mutation: a stray
    non-matching sample (e.g. mid-render) must not count toward the streak
    -- kills a mutation that forgets to reset on mismatch."""
    runner = _FakeTmuxRunner(capture_pane_sequence=["A", "B", "B", "B"])
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    )
    channel.send("hello")
    _check(
        runner.capture_pane_call_count() == 5,
        f"a mismatch resets the streak -- needs 5 samples (baseline, then a mismatch that "
        f"becomes the new baseline, then 3 confirmations), got {runner.capture_pane_call_count()}",
    )
    _check(runner.enter_sent(), "Enter is still eventually sent once the pane genuinely stabilizes")


def test_timeout_still_sends_enter_fail_open() -> None:
    """The channel's own established contract is fire-and-forget best-
    effort (never silently do nothing) -- a pane that never stabilizes must
    still get the Enter, not be abandoned mid-composer forever."""
    ever_changing = [f"frame-{i}" for i in range(1000)]
    runner = _FakeTmuxRunner(capture_pane_sequence=ever_changing)
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=2.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=1.0),
    )
    channel.send("hello")
    _check(runner.enter_sent(), "Enter is still sent (fail-open) after a stability timeout")


def test_literal_send_failure_never_attempts_enter() -> None:
    runner = _FakeTmuxRunner(fail_literal_send=True)
    channel = tmux_module._TmuxSendKeysDriverChannel(  # noqa: SLF001
        tmux_bin="tmux", session="s1", run_fn=runner,
        stable_samples_required=3, stable_timeout_seconds=100.0,
        sleep_fn=_fake_sleep, now_fn=_FakeClock(step=0.1),
    )
    channel.send("hello")  # must not raise -- fire-and-forget contract
    _check(not runner.enter_sent(), "Enter is NEVER attempted when the literal text send itself failed")
    _check(runner.capture_pane_call_count() == 0, "no stability wait is even attempted after a failed text send")


def main() -> int:
    print("=== tmux driver channel smoke ===")
    test_enter_sent_only_after_n_consecutive_stable_samples()
    test_non_matching_sample_resets_the_consecutive_counter()
    test_timeout_still_sends_enter_fail_open()
    test_literal_send_failure_never_attempts_enter()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
