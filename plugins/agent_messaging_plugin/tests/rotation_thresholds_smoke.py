#!/usr/bin/env python3
"""Unit smoke for ``rotation_thresholds.py`` (rotation-systematization P2
slice A/B, ruling 3, ratified 2026-08-07; ceiling table populated 2026-08-07
at the seat's closing-slice package review) -- the declared context-ceiling
table + conservative fallback + 50% threshold comparison.

Proves: an unknown model resolves to DEFAULT_CONSERVATIVE_CEILING, never a
KeyError or a silently-invented value; a known model resolves to ITS OWN
ceiling, not the fallback; is_rotation_due is false strictly below the
threshold fraction and true at/above it (the exact boundary, since a
>-only comparison would miss the crossing tick); MODEL_CONTEXT_CEILINGS
carries EXACTLY the seat-confirmed alias set (a structural drift guard --
neither a silent addition nor a silent removal passes); every REAL populated
alias resolves to its measured ceiling against the production data itself,
not a fixture; and a plausible-but-not-yet-added future model name still
falls back to the conservative ceiling rather than being silently exempted.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/rotation_thresholds_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import rotation_thresholds as rt  # noqa: E402

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


def test_table_carries_exactly_the_seat_confirmed_keys() -> None:
    _check(
        set(rt.MODEL_CONTEXT_CEILINGS.keys()) == {
            "claude-fable-5", "claude-sonnet-5", "claude-opus-5",
            "claude-haiku-4-5", "claude-haiku-4-5-20251001",
        },
        "MODEL_CONTEXT_CEILINGS carries exactly the seat-confirmed alias set, "
        "no silent additions or removals",
    )


def test_real_aliases_resolve_to_their_measured_ceilings() -> None:
    """Distinct from the fixture-injection test below (which proves the
    MECHANISM works) -- this proves the PRODUCTION DATA itself is correct,
    a direct assertion against the real table, not a stubbed value."""
    _check(
        rt.resolve_ceiling("claude-sonnet-5") == 1_000_000,
        "claude-sonnet-5 resolves to its measured 1M ceiling, not the fallback",
    )
    _check(
        rt.resolve_ceiling("claude-fable-5") == 1_000_000,
        "claude-fable-5 resolves to its measured 1M ceiling",
    )
    _check(
        rt.resolve_ceiling("claude-opus-5") == 1_000_000,
        "claude-opus-5 resolves to its measured 1M ceiling",
    )
    _check(
        rt.resolve_ceiling("claude-haiku-4-5") == 200_000,
        "claude-haiku-4-5 resolves to its measured 200K ceiling",
    )
    _check(
        rt.resolve_ceiling("claude-haiku-4-5-20251001") == 200_000,
        "the dated full-ID form also resolves to 200K, defensively",
    )


def test_plausible_future_model_still_falls_back_conservative() -> None:
    """RED-FIRST regression against silent scope-creep exemptions: a
    plausible-looking future model name (not an obviously-fake fixture
    string) must still fall back to the conservative ceiling -- kills a
    mutation that special-cases "looks like a claude model" instead of
    exact-key lookup."""
    _check(
        rt.resolve_ceiling("claude-opus-6") == rt.DEFAULT_CONSERVATIVE_CEILING,
        "a plausible-but-not-yet-added future model name still falls back "
        "to the conservative ceiling, never silently exempted",
    )


def test_unknown_model_resolves_to_conservative_fallback() -> None:
    _check(
        rt.resolve_ceiling("some-model-nobody-populated-yet") == rt.DEFAULT_CONSERVATIVE_CEILING,
        "unknown model resolves to DEFAULT_CONSERVATIVE_CEILING, never a guess or KeyError",
    )


def test_known_model_resolves_to_its_own_ceiling_not_the_fallback() -> None:
    original = dict(rt.MODEL_CONTEXT_CEILINGS)
    try:
        rt.MODEL_CONTEXT_CEILINGS["test-model-x"] = 250_000
        _check(
            rt.resolve_ceiling("test-model-x") == 250_000,
            "a populated model resolves to its own ceiling, not DEFAULT_CONSERVATIVE_CEILING",
        )
    finally:
        rt.MODEL_CONTEXT_CEILINGS.clear()
        rt.MODEL_CONTEXT_CEILINGS.update(original)


def test_is_rotation_due_false_strictly_below_threshold() -> None:
    ceiling = rt.DEFAULT_CONSERVATIVE_CEILING
    just_below = int(ceiling * rt.ROTATION_THRESHOLD_FRACTION) - 1
    _check(
        rt.is_rotation_due(model="unknown", current_tokens=just_below) is False,
        "one token below the threshold fraction -> not due",
    )


def test_is_rotation_due_true_at_exact_threshold_boundary() -> None:
    ceiling = rt.DEFAULT_CONSERVATIVE_CEILING
    at_threshold = int(ceiling * rt.ROTATION_THRESHOLD_FRACTION)
    _check(
        rt.is_rotation_due(model="unknown", current_tokens=at_threshold) is True,
        "exactly at the threshold fraction -> due (kills a strict-greater-than mutation)",
    )


def test_watch_threshold_constants_match_ratified_values() -> None:
    """Structural drift guard, same pattern as the ceiling-table key-set test
    above -- these four values were individually ratified (P4(a) ratification
    condition 4); a silent edit to any of them should fail this smoke, not
    slip through unnoticed."""
    _check(
        rt.WATCH_POLL_INTERVAL_SECONDS == 60,
        "WATCH_POLL_INTERVAL_SECONDS matches its ratified value (60s)",
    )
    _check(
        rt.IDLE_POKE_THRESHOLD_SECONDS == 300,
        "IDLE_POKE_THRESHOLD_SECONDS matches its ratified value (300s)",
    )
    _check(
        rt.IDLE_ROTATE_THRESHOLD_SECONDS == 2100,
        "IDLE_ROTATE_THRESHOLD_SECONDS matches its ratified value (2100s)",
    )
    _check(
        rt.POKE_COOLDOWN_SECONDS == 1800,
        "POKE_COOLDOWN_SECONDS matches its ratified value (1800s)",
    )


def test_poke_threshold_is_strictly_below_rotate_threshold() -> None:
    """Load-bearing ordering, not just individual values: the poke path is
    supposed to have a chance to fire and drain pending mail well before the
    rotate path would otherwise trigger (P4(a).4 precedence design -- poke
    evaluated first, and only wins the race if its threshold is the smaller
    one). If a future edit ever inverted this ordering, poke would never have
    a window to act before rotate did instead."""
    _check(
        rt.IDLE_POKE_THRESHOLD_SECONDS < rt.IDLE_ROTATE_THRESHOLD_SECONDS,
        "poke threshold is strictly below rotate threshold, preserving the "
        "poke-fires-first design window",
    )


def test_rotate_threshold_is_margined_below_cache_ttl_floor() -> None:
    """The trade-off documented on IDLE_ROTATE_THRESHOLD_SECONDS is only real
    if the number itself actually sits below the fleet's conservative 45-min
    cache-TTL floor -- this pins that margin so it can't silently drift past
    the boundary it exists to stay ahead of."""
    conservative_ttl_floor_seconds = 45 * 60
    _check(
        rt.IDLE_ROTATE_THRESHOLD_SECONDS < conservative_ttl_floor_seconds,
        "rotate threshold fires strictly before the conservative 45-minute "
        "cache-TTL floor, not at or after it",
    )


def test_h_is_the_sum_of_its_measured_parts() -> None:
    """H is composed, not asserted. If someone edits one part and not the
    total, this catches it -- the failure mode for a constant whose value is
    a measurement rather than a choice."""
    _check(
        rt.POLICY_H_TOKENS == rt.POLICY_H_BOOT_TOKENS + rt.POLICY_H_REHYDRATION_TOKENS,
        "H equals boot payload + incremental rehydration, not an independent number",
    )
    _check(rt.POLICY_H_TOKENS == 110_702, "H is the measured 110,702 (2026-08-16)")


def test_warm_bands_are_ordered_and_exhaustive() -> None:
    """Every warm size lands in exactly one band, and the boundaries ascend.
    An unordered band table silently swallows a whole range."""
    _check(
        rt.WARM_BAND_KEEP_WORKING_TOKENS
        < rt.WARM_BAND_TASK_BOUNDARY_TOKENS
        < rt.WARM_BAND_SAFE_CHECKPOINT_TOKENS,
        "warm band boundaries ascend",
    )
    seen = {rt.rotation_band(n, cache_cold=False)[0]
            for n in (0, 149_999, 150_000, 199_999, 200_000, 299_999, 300_000, 10**7)}
    _check(
        seen == {"warm_keep", "warm_task_boundary", "warm_safe_checkpoint", "warm_immediate"},
        "every warm size resolves to exactly one of the four bands",
    )


def test_band_boundaries_are_inclusive_where_the_policy_says_so() -> None:
    """The crossing tick is the one that matters: a >-only comparison misses
    the exact boundary, which is the same defect this file already guards for
    is_rotation_due."""
    _check(rt.rotation_band(149_999, cache_cold=False)[0] == "warm_keep",
           "just under 150K still keeps working")
    _check(rt.rotation_band(150_000, cache_cold=False)[0] == "warm_task_boundary",
           "exactly 150K has crossed into the task-boundary band")
    _check(rt.rotation_band(300_000, cache_cold=False)[0] == "warm_immediate",
           "exactly 300K rotates immediately, not at the next checkpoint")


def test_cold_is_a_different_question_not_a_stricter_band() -> None:
    """Cold compares against H, not against the warm bands. A context that
    would 'keep working' warm can still be worth rotating cold, and one that
    would rotate warm can be NOT worth rotating cold -- the asymmetry is the
    point, and collapsing cold into 'a stricter warm' loses it."""
    _check(rt.rotation_band(90_000, cache_cold=True)[0] == "cold_below_h",
           "cold and under H -> keep working; a clear would cost more than it saves")
    _check(rt.rotation_band(250_000, cache_cold=True)[0] == "cold_above_h",
           "cold and over H -> rotate")
    _check(rt.rotation_band(90_000, cache_cold=False)[0] == "warm_keep",
           "the same 90K warm is also keep -- but for a different reason")
    _check(rt.rotation_band(250_000, cache_cold=False)[0] == "warm_safe_checkpoint",
           "the same 250K warm waits for a safe checkpoint rather than rotating now")
    # THE DISCRIMINATING VALUE. 90K and 250K cannot tell "compares against H"
    # apart from "compares against the 150K warm band" -- both thresholds sort
    # them identically, so those assertions pass under a cold-is-just-stricter-
    # warm implementation. Only a value BETWEEN H (110,702) and 150,000
    # separates the two, which is the whole claim this test exists to make.
    # Found by mutation: without this line the test was blind to exactly the
    # collapse it is named after.
    _check(rt.WARM_BAND_KEEP_WORKING_TOKENS > rt.POLICY_H_TOKENS,
           "H sits BELOW the first warm band -- there is a range that separates them")
    _check(rt.rotation_band(120_000, cache_cold=True)[0] == "cold_above_h",
           "120K cold is ABOVE H -> rotate, even though 120K warm would keep working")
    _check(rt.rotation_band(120_000, cache_cold=False)[0] == "warm_keep",
           "120K warm keeps working -- the same size, opposite verdicts by cache state")


def test_clearing_wins_needs_calls_to_amortise_over() -> None:
    """C > H + 20H/N. With no calls after the clear the rewrite is never
    amortised, so a clear cannot win no matter how large C is -- the guard
    against 'rotate at the very end of the work', which pays the rewrite and
    then throws it away."""
    _check(not rt.clearing_wins(10_000_000, 0),
           "N=0 -> clearing never wins, however large the context")
    _check(not rt.clearing_wins(10_000_000, -3), "negative N is refused too")
    _check(rt.clearing_wins(559_000, 50),
           "the 559K case with 50 calls ahead: clearing wins")
    _check(not rt.clearing_wins(120_000, 2),
           "modest context with only 2 calls ahead: clearing loses")


def test_the_superseded_fraction_survives_as_a_hint_only() -> None:
    """ROTATION_THRESHOLD_FRACTION is deliberately KEPT -- the verb still emits
    it. This pins that it was not silently repurposed as the policy: it is a
    fraction, the policy is absolute token counts, and the two must not drift
    into each other."""
    _check(isinstance(rt.ROTATION_THRESHOLD_FRACTION, float),
           "the legacy hint is still a FRACTION, not a token count")
    _check(rt.ROTATION_THRESHOLD_FRACTION == 0.5, "the hint is unchanged at 0.5")
    _check(isinstance(rt.WARM_BAND_KEEP_WORKING_TOKENS, int)
           and rt.WARM_BAND_KEEP_WORKING_TOKENS > 1_000,
           "the policy bands are absolute token counts, not fractions")


def _call(offset_s: float, *, cold: bool, base: datetime) -> rt.AssistantCall:
    return rt.AssistantCall(
        at=base + timedelta(seconds=offset_s),
        cache_read_tokens=0 if cold else 150_000,
    )


def test_post_clear_rewrite_is_not_evidence_of_a_cold_cache() -> None:
    """The dangerous one. A clear rewrites the prefix, so the first call after
    it is cold BY CONSTRUCTION. Counting it makes every rotation immediately
    recommend another rotation."""
    base = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    st = rt.classify_cache_state(
        [_call(1, cold=True, base=base)], base + timedelta(seconds=2), cleared_at=base)
    _check(not st.cold, "the post-clear rewrite alone is NOT a cold cache")
    st = rt.classify_cache_state(
        [_call(1, cold=True, base=base), _call(2, cold=False, base=base)],
        base + timedelta(seconds=3), cleared_at=base)
    _check(not st.cold, "post-clear rewrite then a warm call -> warm")


def test_isolated_cold_call_is_the_resolved_idle_case() -> None:
    base = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    st = rt.classify_cache_state(
        [_call(0, cold=False, base=base), _call(7200, cold=True, base=base)],
        base + timedelta(seconds=7210), cleared_at=None)
    _check(not st.cold and not st.overage_signature,
           "ONE cold call after a long gap = idle case resolved, warm bands apply")


def test_repeated_cold_calls_across_short_gaps_are_the_overage_signature() -> None:
    base = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    st = rt.classify_cache_state(
        [_call(0, cold=False, base=base), _call(60, cold=True, base=base),
         _call(120, cold=True, base=base)],
        base + timedelta(seconds=130), cleared_at=None)
    _check(st.cold and st.overage_signature,
           "TWO cold calls across sub-TTL gaps = overage signature")


def test_ttl_lapse_is_caught_even_when_the_last_call_read_cache() -> None:
    """The robustness half. cache_read==0 is structurally blind to a PARTIAL
    cache read after the TTL lapsed; the age comparison is not."""
    base = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    st = rt.classify_cache_state(
        [_call(0, cold=False, base=base)], base + timedelta(seconds=4000), cleared_at=None)
    _check(st.cold and not st.overage_signature,
           "past the TTL -> cold, even though that call read cache fine")


def test_transcript_parser_refuses_a_naive_timestamp() -> None:
    """Transcript stamps are AWARE (...Z); the peer registry's are NAIVE UTC.
    One helper for both is the 25,200-second failure, so this parser refuses
    rather than assuming."""
    try:
        rt.parse_transcript_timestamp("2026-08-16T19:12:13.529245")  # the REGISTRY shape
        _check(False, "a naive timestamp is REFUSED, never assumed to be UTC")
    except rt.TimestampAwarenessError:
        _check(True, "a naive timestamp is REFUSED, never assumed to be UTC")
    _check(rt.parse_transcript_timestamp("2026-08-16T20:04:13.422Z").tzinfo is not None,
           "an aware Z timestamp parses")


def main() -> int:
    print("=== rotation_thresholds smoke ===")
    test_table_carries_exactly_the_seat_confirmed_keys()
    test_real_aliases_resolve_to_their_measured_ceilings()
    test_plausible_future_model_still_falls_back_conservative()
    test_unknown_model_resolves_to_conservative_fallback()
    test_known_model_resolves_to_its_own_ceiling_not_the_fallback()
    test_is_rotation_due_false_strictly_below_threshold()
    test_is_rotation_due_true_at_exact_threshold_boundary()
    test_watch_threshold_constants_match_ratified_values()
    test_poke_threshold_is_strictly_below_rotate_threshold()
    test_rotate_threshold_is_margined_below_cache_ttl_floor()
    test_h_is_the_sum_of_its_measured_parts()
    test_warm_bands_are_ordered_and_exhaustive()
    test_band_boundaries_are_inclusive_where_the_policy_says_so()
    test_cold_is_a_different_question_not_a_stricter_band()
    test_clearing_wins_needs_calls_to_amortise_over()
    test_the_superseded_fraction_survives_as_a_hint_only()
    test_post_clear_rewrite_is_not_evidence_of_a_cold_cache()
    test_isolated_cold_call_is_the_resolved_idle_case()
    test_repeated_cold_calls_across_short_gaps_are_the_overage_signature()
    test_ttl_lapse_is_caught_even_when_the_last_call_read_cache()
    test_transcript_parser_refuses_a_naive_timestamp()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
