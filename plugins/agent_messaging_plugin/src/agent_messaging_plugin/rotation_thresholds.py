"""Declared context-window ceilings for the rotation-due trigger (P2 slice B,
ruling 3, P1 ratification 2026-08-07 --
`workbench/2026-08-07_rotation_systematization_findings_rotation-impl.md`).

**Populated 2026-08-07** (seat-supplied at package review, per ruling 3's
"ship empty-or-conservative, confirm at review" contract) -- any model NOT
listed below (e.g. a future release not yet added here) still falls back to
``DEFAULT_CONSERVATIVE_CEILING``, which any threshold check must treat as
the fail-safe floor (a conservative UNDER-estimate, so an unrecognized model
reports "rotation due" EARLIER than it would with its real, larger ceiling
-- never later).
"""

from __future__ import annotations

# Model identifier -> total context-window token ceiling. Populated 2026-08-07
# (seat-supplied, ruling 3 review). Citable sources:
#   1. Anthropic model catalog: platform.claude.com/docs/en/about-claude/models/overview.md
#   2. claude-api reference skill's catalog, cached 2026-06-24
#   3. Runtime-confirmable via the Models API's `max_input_tokens` field
# Key format: the transcript `message.model` field carries the BARE ALIAS (not a
# dated full-ID) on this fleet today -- seat-measured directly against 4 recent
# transcripts (2 seat-class sessions on claude-fable-5, 2 worker-class sessions on
# claude-sonnet-5), all exact on the alias with no dated suffix. A dated full-ID
# form is included defensively for claude-haiku-4-5, whose alias can also resolve
# that way. Any model not listed here falls through to DEFAULT_CONSERVATIVE_CEILING
# below -- never a guess.
# Corroborating field evidence: the 2026-08-04 operator-flagged "774k ≈ 77% context"
# catch is arithmetically consistent with a 1,000,000-token window -- the live
# fleet was already operating against these exact ceilings before this table
# existed to name them.
MODEL_CONTEXT_CEILINGS: dict[str, int] = {
    "claude-fable-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}

# Conservative fallback for any model NOT present in MODEL_CONTEXT_CEILINGS
# above (e.g. a future release not yet added). Chosen deliberately small so
# an unrecognized model trips the rotation-due threshold EARLY rather than
# late -- a false-early rotation-due signal is far cheaper than a missed one
# (the brief's own 2026-08-04 catch: 77% context before anyone noticed).
# Unchanged by the 2026-08-07 ceiling-table population (seat's own
# instruction) -- not itself a measured per-model value, a deliberate floor.
DEFAULT_CONSERVATIVE_CEILING: int = 100_000

# Fraction of the resolved ceiling (MODEL_CONTEXT_CEILINGS[model] or the
# DEFAULT_CONSERVATIVE_CEILING fallback) at which the rotation-due hook
# (P2 slice B) reports to the steward. 0.5 per the brief's own standing
# framing ("first slice boundary past ~50% of context").
ROTATION_THRESHOLD_FRACTION: float = 0.5

# P4 -- idle-boundary seat watcher thresholds (P4(a) ratification, condition 4,
# 2026-08-07). The watcher polls the seat's idle state out-of-process; these
# four constants are its only tunable surface.
#
# Early-vs-late rotation trade-off (documentation REQUIRED by ratification
# condition 4, not optional commentary): IDLE_ROTATE_THRESHOLD_SECONDS fires
# BEFORE the measured ~45-60 minute prompt-cache TTL, not at or after it.
#   - Rotating EARLY (pre-expiry) spends some of the still-warm cache on a
#     rotation that technically didn't NEED to happen yet -- the session was
#     still inside its warm window. The payoff: it GUARANTEES that any poke
#     fired after this point lands in a freshly-cleared, cheap context rather
#     than risking a poke landing just past the expiry boundary and paying a
#     full cold re-warm on the very message that was time-sensitive enough to
#     need a poke in the first place.
#   - Rotating LATE (at/after expiry) preserves the warm window for longer
#     (more turns before any rotation cost is paid at all), but risks exactly
#     the 2026-08-07 incident this watcher exists to prevent: a poke or an
#     operator turn arriving just after expiry pays the full re-warm cost
#     regardless, with no advance warning and no cheaper alternative available.
# The chosen threshold (2100s = 35 min) trades a small amount of warm-window
# time for eliminating that late-poke worst case -- deliberate, not a rounding
# choice. See the per-constant comment below for the specific margin math.
WATCH_POLL_INTERVAL_SECONDS = 60
"""How often the watcher wakes to check idle state. Cheap per tick (one
`ps eww`, one file stat, occasionally one iTerm2 read or one `peer_inbox`
call) -- 60s balances promptness against the watcher's own steady-state cost,
never spending a model/inference call at any interval."""

IDLE_POKE_THRESHOLD_SECONDS = 300
"""Idle duration (5 min) past which, IF the seat's role inbox has pending
entries, the watcher injects a short wake turn (poke) rather than waiting for
the rotate threshold. Deliberately much shorter than the rotate threshold --
draining real pending mail promptly is the point of the poke path; a poke
also naturally resets the idle clock, deferring the rotate question."""

IDLE_ROTATE_THRESHOLD_SECONDS = 2100
"""Idle duration (35 min) past which the watcher fires the full rotation
helper, with no pending-inbox precondition. Margined below the fleet's own
measured ~45-60 minute prompt-cache TTL (memory:
no-session-idles-past-cache-expiry) by roughly a 10-minute floor under the
conservative 45-minute figure -- room for the watcher's own poll latency
(<=WATCH_POLL_INTERVAL_SECONDS) plus the rotation sequence's own worst-case
run time (settle timeout up to 120s + the submit-confirm step), so the
rotation completes and lands the seat in a fresh session BEFORE the cache
would have expired anyway. See the trade-off note above for why firing
before expiry, not at it, is the entire point of this threshold existing at
all. Not live-measured against a real cache-expiry event this pass --
proposed with rationale (P4(a).4), ratified as-is (condition 4)."""

POKE_COOLDOWN_SECONDS = 1800
"""After a poke fires, the watcher holds off re-poking for 30 min even if the
inbox still shows pending entries -- prevents spamming a seat that is idle
for a reason (e.g. the operator stepped away) with repeated pokes for the
same still-unread mail. A successful poke that produces activity naturally
resets the idle clock anyway; this cooldown covers the case where the poke
lands but the seat stays idle regardless."""


def resolve_ceiling(model: str) -> int:
    """The declared ceiling for ``model``, or the conservative fallback when
    ``model`` has no entry in :data:`MODEL_CONTEXT_CEILINGS` -- e.g. a future
    model release not yet added here."""
    return MODEL_CONTEXT_CEILINGS.get(model, DEFAULT_CONSERVATIVE_CEILING)


def is_rotation_due(*, model: str, current_tokens: int) -> bool:
    """``True`` once ``current_tokens`` crosses ``ROTATION_THRESHOLD_
    FRACTION`` of ``model``'s resolved ceiling (:func:`resolve_ceiling`).
    Pure comparison -- the caller (P2 slice B's hook) supplies
    ``current_tokens`` from its own live transcript-usage read; this
    function makes no I/O call of its own."""
    ceiling = resolve_ceiling(model)
    return current_tokens >= ceiling * ROTATION_THRESHOLD_FRACTION


__all__ = [
    "DEFAULT_CONSERVATIVE_CEILING",
    "IDLE_POKE_THRESHOLD_SECONDS",
    "IDLE_ROTATE_THRESHOLD_SECONDS",
    "MODEL_CONTEXT_CEILINGS",
    "POKE_COOLDOWN_SECONDS",
    "ROTATION_THRESHOLD_FRACTION",
    "WATCH_POLL_INTERVAL_SECONDS",
    "is_rotation_due",
    "resolve_ceiling",
]
