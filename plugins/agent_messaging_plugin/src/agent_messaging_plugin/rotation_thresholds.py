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

from dataclasses import dataclass
from datetime import datetime

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

# ---------------------------------------------------------------------------
# ECONOMIC ROTATION POLICY (operator-ratified 2026-08-16). These supersede the
# fraction above AS POLICY. ROTATION_THRESHOLD_FRACTION is retained as the
# coarse `rotation_due` HINT the verb has always emitted -- it is not the
# policy, and the context-gauge card says so explicitly.
#
# The bands are ABSOLUTE token counts, not fractions of a ceiling, because the
# cost being managed is the carriage re-read on every model CALL -- which is a
# function of how many tokens are in the window, not of how full the window is.
# The unit is the MODEL CALL, not the operator prompt: the superseded
# "~80k token-equivalents per prompt" arithmetic priced the wrong event.
# KNOWN LIMITATION, recorded 2026-08-16 rather than left for a reader to
# discover: THESE BANDS ARE MODEL-BLIND, and the numbers were derived from
# Fable-tier input economics. A cheaper tier carries the same context for
# roughly an order of magnitude less, so applying these bands to it reports
# urgency that the actual cost does not justify -- observed live, when a
# sonnet session at 224,840 tokens was read as past-200K-rotate-now.
# `rotation_band()` takes no model argument today. The stored row DOES carry
# `model`, so a model-aware refinement is available and is deliberately NOT
# in this landing: either scale the bands by tier, or state these as
# Fable-seat policy and give cheaper tiers their own rule. Until then, read
# a band on a cheap-tier session as hygiene rather than urgency.
WARM_BAND_KEEP_WORKING_TOKENS: int = 150_000      # under this: keep working
WARM_BAND_TASK_BOUNDARY_TOKENS: int = 200_000     # 150-200K: next natural boundary
WARM_BAND_SAFE_CHECKPOINT_TOKENS: int = 300_000   # over 200K: first safe checkpoint
                                                  # over 300K: immediately

# H -- the post-rotation prefix a `/clear` re-writes, and the quantity the
# cold-cache rule compares against. MEASURED 2026-08-16
# (workbench/2026-08-16_h_measurement_lane_al.md), replacing a 74K ESTIMATE
# that measurement put roughly 50% low.
#
# H is boot payload PLUS incremental rehydration: the boot payload is re-paid
# on every clear, so it belongs inside H rather than beside it.
#
# THIS CONSTANT HAS A SHELF LIFE. It drifts as the boot payload changes, so
# re-measuring it belongs in the seed-update runbook rather than in anyone's
# memory. Measured 2026-08-16; re-measure when CLAUDE.md, MEMORY.md or the
# hook set changes materially.
POLICY_H_BOOT_TOKENS: int = 42_873
POLICY_H_REHYDRATION_TOKENS: int = 67_829
POLICY_H_TOKENS: int = POLICY_H_BOOT_TOKENS + POLICY_H_REHYDRATION_TOKENS  # 110,702

# Break-even multiplier for `C > H + (CACHE_WRITE_PREMIUM_MULTIPLIER * H) / N`.
# A clear re-writes the whole prefix at roughly 2x base input under the 1-hour
# cache-write premium, and that write is amortised over the N calls that follow
# it -- so a rotation pays for itself only when the carried context C is large
# enough relative to what the rewrite costs.
CACHE_WRITE_PREMIUM_MULTIPLIER: int = 20

# Prompt-cache lifetimes. The policy's words are "idle" and "in usage overage";
# the code measures CACHE EXPIRY, because overage MANIFESTS as cache expiry and
# the account state itself is not observable to this platform (searched
# 2026-08-16: no signal found -- which is not the same claim as none existing).
CACHE_TTL_SECONDS: int = 3_600        # nominal 1-hour prompt-cache TTL
OVERAGE_TTL_SECONDS: int = 300        # TTL collapses to ~5 min while in overage
OVERAGE_MIN_COLD_CALLS: int = 2       # "repeated", never a single cold call

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


def rotation_band(current_tokens: int, *, cache_cold: bool) -> tuple[str, str]:
    """``(band, guidance)`` for a measured context size and cache state.

    The COLD branch is not a stricter version of the warm bands, it is a
    different question. Warm, the carried context is cheap to re-read and the
    only cost of continuing is the per-call carriage, so the bands are about
    when that carriage stops being worth it. Cold, the context will be re-paid
    at full price on the very next call whether you rotate or not -- so the
    reason to keep a long context has gone, and the comparison is against H,
    the prefix a clear would rewrite.
    """
    if cache_cold:
        if current_tokens > POLICY_H_TOKENS:
            return ("cold_above_h", (
                f"cache is cold and context ({current_tokens:,}) exceeds H "
                f"({POLICY_H_TOKENS:,}) -- rotate: the carried context will be "
                "re-paid at full price either way"))
        return ("cold_below_h", (
            f"cache is cold but context ({current_tokens:,}) is under H "
            f"({POLICY_H_TOKENS:,}) -- keep working, a clear would cost more "
            "than it saves"))
    if current_tokens < WARM_BAND_KEEP_WORKING_TOKENS:
        return ("warm_keep", "keep working")
    if current_tokens < WARM_BAND_TASK_BOUNDARY_TOKENS:
        return ("warm_task_boundary", "rotate at the next natural task boundary")
    if current_tokens < WARM_BAND_SAFE_CHECKPOINT_TOKENS:
        return ("warm_safe_checkpoint", "rotate at the first safe checkpoint")
    return ("warm_immediate",
            "rotate immediately -- finish only the in-flight tool action")


def clearing_wins(current_tokens: int, expected_calls_after: int) -> bool:
    """``C > H + 20H/N`` -- whether a clear pays for itself.

    ``expected_calls_after`` is N, the calls the rewritten prefix will be
    amortised over. N <= 0 means the rewrite is never amortised, so a clear
    cannot win regardless of how large C is.
    """
    if expected_calls_after <= 0:
        return False
    threshold = POLICY_H_TOKENS + (
        CACHE_WRITE_PREMIUM_MULTIPLIER * POLICY_H_TOKENS
    ) / expected_calls_after
    return current_tokens > threshold


class TimestampAwarenessError(ValueError):
    """A transcript timestamp arrived naive. Refuse rather than assume UTC.

    The peer registry's ``updated_at`` IS naive-UTC and the rotation hook
    deliberately attaches UTC to it. Doing the same here would be a coincidence
    of behaviour rather than a shared rule, and the next person to reuse one
    helper for the other pays the whole UTC offset -- 25,200s on a PDT host,
    two orders of magnitude larger than any bound either side compares against.
    So each parser states its own contract and refuses input that does not meet
    it.
    """


def parse_transcript_timestamp(raw: str) -> datetime:
    """Parse a transcript timestamp, which is tz-AWARE (``...Z``)."""
    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise TimestampAwarenessError(f"transcript timestamp is naive: {raw!r}")
    return stamp


@dataclass(frozen=True)
class AssistantCall:
    """One assistant usage block, reduced to what the cache decision needs."""

    at: datetime
    cache_read_tokens: int

    @property
    def was_cold(self) -> bool:
        """True when this call read NOTHING from cache -- it paid full price."""
        return self.cache_read_tokens == 0


@dataclass(frozen=True)
class CacheState:
    cold: bool
    overage_signature: bool
    reason: str


def _scored_calls(
    calls: list[AssistantCall], cleared_at: datetime | None,
) -> list[AssistantCall]:
    """Calls that count as evidence of cache behaviour.

    THE FIRST CALL AFTER A CLEAR IS COLD BY CONSTRUCTION -- the clear rewrites
    the prefix -- so counting it would make every rotation immediately
    recommend another rotation. This exclusion is coupled to the counting in
    :func:`classify_cache_state`: drop it and the excluded rewrite becomes the
    second cold call that trips the overage threshold. Proven by mutation, so
    it lives in its own function with its own assertion rather than inline.
    """
    scored = [c for c in calls if cleared_at is None or c.at > cleared_at]
    if cleared_at is not None and scored:
        return scored[1:]
    return scored


def _short_gap_cold_count(scored: list[AssistantCall]) -> int:
    """Cold calls that follow their predecessor by LESS than a full TTL.

    A cold call after a long gap is ordinary expiry. A cold call soon after
    another call means the cache did not survive its nominal window, which is
    what usage overage looks like from outside -- the account state is not
    observable to this platform, but its consequence is.
    """
    return sum(
        1
        for older, newer in zip(scored, scored[1:], strict=False)
        if newer.was_cold and (newer.at - older.at).total_seconds() <= CACHE_TTL_SECONDS
    )


def classify_cache_state(
    calls: list[AssistantCall], now: datetime, *, cleared_at: datetime | None,
) -> CacheState:
    """Prompt-cache state, from the transcript's own usage blocks.

    An ISOLATED cold call after a long gap is the idle case, already resolved:
    the rewrite happened, the cache is warm again, warm bands apply. REPEATED
    cold calls across short gaps are the overage signature.
    """
    scored = _scored_calls(calls, cleared_at)
    if not scored:
        return CacheState(False, False, "no scoreable assistant calls -- nothing measured")
    idle_seconds = (now - scored[-1].at).total_seconds()
    if idle_seconds > CACHE_TTL_SECONDS:
        return CacheState(
            True, False,
            f"last call {idle_seconds:.0f}s ago, past the {CACHE_TTL_SECONDS}s TTL",
        )
    short_gap_colds = _short_gap_cold_count(scored)
    if short_gap_colds >= OVERAGE_MIN_COLD_CALLS:
        return CacheState(
            True, True,
            f"{short_gap_colds} cold calls across sub-TTL gaps -- the cache is not "
            "surviving its nominal window (the overage signature)",
        )
    if scored[-1].was_cold:
        return CacheState(False, False, "one cold call, cache re-warmed since -- warm bands apply")
    return CacheState(False, False, "cache is warm")
