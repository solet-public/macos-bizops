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
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
