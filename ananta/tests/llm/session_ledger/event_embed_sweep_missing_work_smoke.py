#!/usr/bin/env python3
"""Smoke: an event-embedding sweep embeds only MISSING work and never holds new arrivals back (prb_f6964132).

Live defect (Operator ordinals 63-65, 2026-11-07): from the 2026-10-18 checkpoint landing until
c39883889 every drain fire decoded its own JSON-string sweep checkpoint as corrupt and minted a new
generation with ``previous='unknown:corrupt'``.  ``policy_backfill_chunks`` treats that previous as
"every event stale", so the surviving generation re-embedded the whole ~1M-event corpus on one CPU
core although every chunk already held a vector for the same model and target, and events newer
than the incremental cursor waited behind the re-walk.  It proves:

E1. a corrupt checkpoint restarts only the missing work;
E2. a running generation carrying the legacy unknown previous is corrected in place to the recorded
    policy, resumes from its checkpoint and re-embeds nothing already embedded;
E3. under a reproducible-policy backfill an event already holding the new cut is skipped as current
    while an event still holding the old cut is re-embedded;
E4. new arrivals past the cursor are embedded before the sweep re-walks the corpus.

It drives the REAL drain over the REAL repository KV and PGVectorProvider through the harness of
``event_embed_drain_progress_smoke``.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/event_embed_sweep_missing_work_smoke.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from ananta.llm.session_ledger import event_embeddings as event_embeddings_module  # noqa: E402
from ananta.llm.session_ledger.event_embedding_checkpoint import UNKNOWN_PREVIOUS, SweepCheckpoint  # noqa: E402
from event_embed_drain_progress_smoke import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    _SINK,
    _big_text,
    _event,
    _restart,
    _World,
)

_LOGGERS = (
    "ananta.llm.session_ledger.event_embeddings",
    "ananta.llm.session_ledger.search",
    "ananta.llm.session_ledger.event_embedding_checkpoint",
)
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failed.append(label)


_CHECKPOINT_KEY = ("session_ledger", "event_embed_sweep_checkpoint", "GLOBAL")
_POLICY_KEY = ("session_ledger", "event_embed_chunk_policy", "GLOBAL")


def _embedded_world(corpus: list[dict[str, object]], *, policy_recorded: bool = True) -> _World:
    """A world whose corpus is fully embedded once under the current policy."""
    world = _World(corpus, policy_recorded=policy_recorded)
    outcome = world.writer.drain_missing_events(page_size=2)
    assert not outcome["halted_on_error"], outcome
    return world


def scenario_corrupt_checkpoint_embeds_only_missing() -> None:
    """[E1] A corrupt checkpoint restarts only MISSING work (prb_f6964132, Operator ord 64 check 1).

    Live defect: corrupt progress minted ``previous='unknown:corrupt'``, which marked every event stale,
    so the sweep re-embedded the whole corpus although every chunk held a vector for the same target.
    """
    world = _embedded_world([_event(f"evt_{hour}", hour, f"message {hour}") for hour in range(1, 5)])
    world.repo.corpus.append(_event("evt_9", 9, "new arrival"))
    world.state.key_values[_CHECKPOINT_KEY] = {"broken": True}
    _restart(world)
    sent = len(world.embedder.sent_token_counts)
    outcome = world.writer.drain_missing_events(page_size=2)
    _check(not outcome["halted_on_error"] and outcome["events_embedded"] == 1, "[E1] a corrupt checkpoint embeds only the one missing event")
    _check(len(world.embedder.sent_token_counts) - sent == 1, "[E1] no chunk already holding a vector for the same target is re-embedded")
    _check(world.repo.get_event_embed_checkpoint() is None, "[E1] the missing-work sweep completes and retires its checkpoint")


def scenario_legacy_unknown_previous_is_corrected_in_place() -> None:
    """[E2] The live generation carrying ``unknown:corrupt`` is corrected in place; progress kept, nothing re-embedded."""
    corpus = [_event(f"evt_{hour}", hour, f"message {hour}") for hour in range(1, 6)]
    world = _embedded_world(corpus)
    policy = world.repo.get_event_embed_chunk_policy()
    after = ("2026-10-01T02:00:00", "evt_2")
    legacy = SweepCheckpoint("gen_live", str(policy), UNKNOWN_PREVIOUS, after)
    world.state.key_values[_CHECKPOINT_KEY] = json.dumps(legacy.payload())
    _restart(world)
    sent = len(world.embedder.sent_token_counts)
    seen = len(_SINK.messages)
    outcome = world.writer.drain_missing_events(page_size=2)
    logs = _SINK.messages[seen:]
    _check(any("generation=gen_live" in m and "corrected to recorded policy" in m for m in logs), "[E2] the legacy unknown previous is corrected to the recorded policy, same generation")
    _check(after in world.repo.reads and None not in world.repo.reads, "[E2] the corrected sweep resumes from its checkpoint, never from the corpus head")
    _check(outcome["events_embedded"] == 0 and len(world.embedder.sent_token_counts) == sent, "[E2] the corrected sweep re-embeds nothing already embedded")
    _check(not outcome["policy_backfill"] and world.repo.get_event_embed_checkpoint() is None, "[E2] the corrected sweep completes as a missing-work sweep")


def scenario_backfill_skips_events_already_holding_the_new_cut() -> None:
    """[E3] Under a reproducible-policy backfill, an event whose new-policy chunks exist is not re-embedded."""
    over_budget = _big_text(2500)  # one 8192-char window, two 2048-token chunks
    world = _embedded_world([_event("evt_new_cut", 1, over_budget)], policy_recorded=False)
    _check(world.table.active_ids() >= {"evt_new_cut:0", "evt_new_cut:1"}, "[E3] setup: the event already holds the token-budget cut")
    world.repo.corpus.append(_event("evt_old_cut", 2, over_budget + " cd"))
    world.table.seed("evt_old_cut:0")  # only the old single character-window chunk
    world.state.key_values.pop(_POLICY_KEY, None)
    world.state.key_values[_CHECKPOINT_KEY] = {"broken": True}
    _restart(world)
    outcome = world.writer.drain_missing_events(page_size=2)
    _check(outcome["policy_backfill"] and outcome.get("events_skipped_current") == 1, "[E3] the event already holding the new cut is skipped as current")
    _check(outcome["events_embedded"] == 1 and "evt_old_cut:1" in world.table.active_ids(), "[E3] the event still holding the old cut is re-embedded")


def scenario_new_arrivals_are_embedded_before_the_sweep() -> None:
    """[E4] New arrivals past the cursor are embedded before a sweep re-walks the corpus (Operator ord 65)."""
    world = _embedded_world([_event(f"evt_{hour}", hour, f"message {hour}") for hour in range(1, 4)])
    world.repo.corpus.append(_event("evt_7", 7, "fresh arrival"))
    policy = str(world.repo.get_event_embed_chunk_policy())
    world.state.key_values[_CHECKPOINT_KEY] = json.dumps(SweepCheckpoint("gen_walk", policy, policy).payload())
    _restart(world)
    outcome = world.writer.drain_missing_events(page_size=2)
    first_sweep_read = world.repo.reads.index(None)
    _check(("2026-10-01T03:00:00", "") in world.repo.reads[:first_sweep_read], "[E4] the incremental frontier is read before the sweep walks from the corpus head")
    _check(outcome["events_embedded"] == 1 and world.repo.get_event_embed_cursor() == "2026-10-01T07:00:00", "[E4] the new arrival is embedded and the cursor reaches it")


_SCENARIOS = (
    scenario_corrupt_checkpoint_embeds_only_missing,
    scenario_legacy_unknown_previous_is_corrected_in_place,
    scenario_backfill_skips_events_already_holding_the_new_cut,
    scenario_new_arrivals_are_embedded_before_the_sweep,
)


def main() -> int:
    print("=== event_embed_sweep_missing_work_smoke ===")
    print(f"module under test: {event_embeddings_module.__file__}")
    loggers = [logging.getLogger(name) for name in _LOGGERS]
    for logger in loggers:
        logger.setLevel(logging.INFO)
        logger.addHandler(_SINK)
        logger.propagate = False
    try:
        for scenario in _SCENARIOS:
            scenario()
    finally:
        for logger in loggers:
            logger.removeHandler(_SINK)
            logger.propagate = True
    print(f"=== {'OK' if not _failed else 'FAILED'}: failing_checks={len(_failed)} ===")
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
