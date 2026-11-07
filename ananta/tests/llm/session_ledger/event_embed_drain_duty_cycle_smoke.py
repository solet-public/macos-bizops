#!/usr/bin/env python3
"""Smoke: the event-embedding drain runs at a bounded duty cycle (prb_f6964132, Operator ordinals 63-64).

Live defect: the drain embedded back to back on one CPU core (~96 percent of a core
for hours).  The drain now sleeps ``worked * (1 - duty) / duty`` after every
embedded event and every page.  It proves:

D1. at the busy duty a unit of work is followed by a proportional pause; when the
    newest in-scope arrival is older than the idle threshold the drain runs at the
    idle duty (full speed by default);
D2. an invalid setting or a failed / unreadable idle read falls back to the slow
    busy duty with an advisory, never a refusal (rul_becb4bed);
D4. health: each page persists the fire's progress, and ``event_embedding_coverage``
    reports the pending sweep, remaining candidate events past the sweep and the cursor
    (a real COUNT filter), the measured rates and an ETA (Operator ordinal 64 check 2);
D3. the pass path: a real drain over a fake embedder whose every call costs two
    seconds sleeps nine times its work at duty 0.1 between events and pages, reports
    ``paused_ms``, and its sweep checkpoint advances and survives a simulated restart.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/event_embed_drain_duty_cycle_smoke.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from ananta.llm.session_ledger import event_embedding_pacing as pacing  # noqa: E402
from ananta.llm.session_ledger import event_embeddings as event_embeddings_module  # noqa: E402
from ananta.llm.session_ledger.event_embedding_checkpoint import SweepCheckpoint  # noqa: E402
from ananta.llm.session_ledger.event_embedding_pacing import DutyCycle, PacingSettings  # noqa: E402
from event_embed_drain_progress_smoke import _DrainRepo, _event, _restart, _World  # noqa: E402  # pyright: ignore[reportMissingImports]

_failed: list[str] = []
_NOW = datetime(2026, 10, 1, 4, 0, 0)


def _check(condition: object, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failed.append(label)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _cycle(clock: _Clock, newest: Any, settings: PacingSettings | None = None, now: datetime = _NOW) -> DutyCycle:
    return DutyCycle(settings or PacingSettings(), newest_arrival=newest, clock=clock, sleep=clock.sleep, now=lambda: now)


def scenario_busy_and_idle_duty() -> None:
    clock = _Clock()
    busy = _cycle(clock, lambda: "2026-10-01T03:59:00")
    clock.t += 2.0
    _check(abs(busy.pace() - 18.0) < 1e-9, "[D1] busy duty 0.1: 2 s of work is followed by an 18 s pause")
    idle = _cycle(clock, lambda: "2026-10-01T03:00:00")
    clock.t += 2.0
    _check(idle.pace() == 0.0, "[D1] newest arrival older than 900 s: idle duty 1.0 runs at full speed")
    _check(_cycle(clock, lambda: None).duty() == 1.0, "[D1] an empty ledger is idle")


def scenario_unknown_is_slow_and_advisory() -> None:
    clock = _Clock()

    def broken() -> object:
        raise RuntimeError("state read failed")

    with patch.object(pacing.logger, "warning") as warned:
        _check(_cycle(clock, broken).duty() == 0.1, "[D2] a failed idle read uses the busy duty")
        _check(_cycle(clock, lambda: "not a time").duty() == 0.1, "[D2] an unreadable arrival uses the busy duty")
        with patch.dict(os.environ, {pacing.BUSY_DUTY_ENV: "7", pacing.IDLE_AFTER_ENV: "-1"}):
            settings = PacingSettings.from_environment()
    _check(settings == PacingSettings(), "[D2] invalid settings fall back to the safe defaults")
    _check(warned.call_count == 4, "[D2] every fallback is logged as an advisory")


def scenario_drain_pass_path_paces_and_checkpoint_survives_restart() -> None:
    corpus = [_event(f"evt_{hour}", hour, f"message {hour}") for hour in range(1, 4)]
    world = _World(corpus, policy_recorded=False)  # an unrecorded policy makes the first fire a checkpointed sweep
    clock = _Clock()
    generate = world.embedder.generate_embeddings

    def costly(*args: Any, **kwargs: Any) -> Any:
        clock.t += 2.0
        return generate(*args, **kwargs)

    world.embedder.generate_embeddings = costly  # type: ignore[method-assign]
    world.vectors.fail_phase, world.vectors.fail_on = "store", "evt_3"

    def pacer(writer: Any) -> DutyCycle:
        # Five minutes after the newest arrival (03:00): the fleet is busy.
        return _cycle(clock, lambda: event_embeddings_module._newest_arrival(writer), now=datetime(2026, 10, 1, 3, 5))

    with patch.object(event_embeddings_module, "_make_pacer", side_effect=pacer):
        first = world.writer.drain_missing_events(page_size=1)
        checkpoint = world.repo.get_event_embed_checkpoint()
        world.vectors.fail_phase = None
        _restart(world)
        second = world.writer.drain_missing_events(page_size=1)
    embedded = first["events_embedded"] + second["events_embedded"]
    _check(first["halted_on_error"] and checkpoint is not None and checkpoint.after == ("2026-10-01T02:00:00", "evt_2"), "[D3] the sweep checkpoint advances page by page before the outage")
    _check(not second["halted_on_error"] and world.repo.get_event_embed_checkpoint() is None, "[D3] after a simulated restart the sweep resumes and completes")
    _check(embedded >= 3 and len(clock.sleeps) == embedded, "[D3] the drain sleeps after every embedded event (one event per page here)")
    _check(all(abs(pause - 18.0) < 1e-9 for pause in clock.sleeps), "[D3] every embed's 2 s of work is followed by an 18 s pause (duty 0.1)")
    _check(first["paused_ms"] > 0 and second["paused_ms"] > 0, "[D3] each fire reports its paused time")


def _count_corpus(repo: Any, imported_at: str | None) -> int:
    return sum(1 for row in repo.corpus if imported_at is None or str(row["imported_at"]) > imported_at)


def scenario_coverage_reports_remaining_work_and_rate() -> None:
    corpus = [_event(f"evt_{hour}", hour, f"message {hour}") for hour in range(1, 4)]
    world = _World(corpus, policy_recorded=True)
    world.writer.drain_missing_events(page_size=1)
    progress = world.repo.get_event_embed_progress()
    _check(progress is not None and progress["pages"] == 3 and progress["events_embedded"] == 3, "[D4] every page persists the fire's progress record")
    world.repo.set_event_embed_progress({"elapsed_seconds": 3600.0, "candidates_scanned": 500, "events_embedded": 100})
    policy = str(world.repo.get_event_embed_chunk_policy())
    sweep = SweepCheckpoint("gen_health", policy, policy, ("2026-10-01T01:00:00", "evt_1"))
    world.repo.set_event_embed_checkpoint(sweep, expected=None)
    with patch.object(_DrainRepo, "count_event_embedding_candidates_after", _count_corpus):
        health = event_embeddings_module.build_event_embedding_coverage(repository=world.repo, vector_service=None)
    _check(health["sweep_checkpoint"] == sweep.payload() and health["remaining_candidate_events_past_sweep"] == 2, "[D4] coverage names the pending sweep and the candidates it has left")
    _check(health["remaining_candidate_events_past_cursor"] == 0, "[D4] coverage counts candidates past the cursor")
    _check(health["candidates_scanned_per_hour"] == 500.0 and health["events_embedded_per_hour"] == 100.0, "[D4] coverage reports the measured rates")
    _check(abs(health["estimated_hours_remaining"] - 2 / 500) < 1e-12, "[D4] coverage reports an ETA from the remaining sweep and the scan rate")
    calls: list[dict[str, Any]] = []

    def count(namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        calls.append(data)
        return {"action_status": "completed", "data": {"result": {"value": 7}}}

    with patch.object(world.state, "count", side_effect=count, create=True):
        counted = world.repo.count_event_embedding_candidates_after("2026-10-01T02:00:00")
    filters = calls[0]["filters"] if calls else {}
    _check(counted == 7 and filters.get("imported_at") == {"op": "gt", "value": "2026-10-01T02:00:00"} and filters.get("is_deleted") == 0, "[D4] the remaining count is a COUNT past the arrival time over live in-scope rows")


_SCENARIOS = (
    scenario_busy_and_idle_duty,
    scenario_unknown_is_slow_and_advisory,
    scenario_drain_pass_path_paces_and_checkpoint_survives_restart,
    scenario_coverage_reports_remaining_work_and_rate,
)


def main() -> int:
    print("=== event_embed_drain_duty_cycle_smoke ===")
    print(f"module under test: {pacing.__file__}")
    logging.getLogger("ananta.llm.session_ledger").setLevel(logging.ERROR)
    for scenario in _SCENARIOS:
        scenario()
    print(f"=== {'OK' if not _failed else 'FAILED'}: failing_checks={len(_failed)} ===")
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
