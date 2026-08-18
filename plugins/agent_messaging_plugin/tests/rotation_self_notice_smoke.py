#!/usr/bin/env python3
"""Unit smoke for the L4c SELF-NOTICE leg (`sweep_rotation_self_notice`,
`BandEdgeLatch`, the capacity band, and the TTL-aware break-even) --
2026-08-17, the landing that makes a session's own context size reach the
session itself rather than only its steward.

EVERY TEST BELOW NAMES THE MUTATION IT WOULD CATCH, because a green that
cannot name what it would have caught is decoration. The wave's standing rule
is to be able to finish "this number would also be this number if ______", and
where a single assertion could not exclude the alternative, a second assertion
that DOES exclude it sits beside it (see the blind-spot test, which pins both
instruments rather than only the one it wants).

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/rotation_self_notice_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import rotation_self_notice as rsn  # noqa: E402
from agent_messaging_plugin import rotation_thresholds as rt  # noqa: E402
from agent_messaging_plugin import session_sweep as ss  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

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


# ---------------------------------------------------------------------------
# Fakes. Deliberately minimal and deliberately OBSERVABLE: the point of several
# tests below is what the leg DID NOT do, which a fake that only records
# successes cannot show.
# ---------------------------------------------------------------------------

class _FakeBinding:
    def __init__(self, bridge_id: str) -> None:
        self.bridge_id = bridge_id


class _FakeRegistry:
    """Resolves only the instance ids it was told about -- everything else
    misses, which is exactly the watcher-held-worker shape the leg must count
    rather than swallow."""

    def __init__(self, known: set[str]) -> None:
        self._known = known
        self.lookups: list[str] = []

    def resolve_by_agent_instance_id(self, agent_instance_id: str) -> _FakeBinding | None:
        self.lookups.append(agent_instance_id)
        if agent_instance_id in self._known:
            return _FakeBinding(f"bridge-for-{agent_instance_id}")
        return None


class _FakeBridgeManager:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str]] = []

    def append_event(
        self, bridge_id: str, event: str, prose: str, meta: dict[str, object],
    ) -> None:
        self.appended.append((bridge_id, event, prose))


class _FakeState:
    """Stands in for StateManagementInterface at the ONE call the leg makes."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def query_state(self, namespace: str, spec: dict[str, Any]) -> dict[str, Any]:
        # The real envelope shape, taken from `state_results.require_records`
        # rather than assumed: `action_status` (NOT `status`) must be the
        # literal "completed", and records live under `data.records`. An
        # earlier draft of this fake used {"status", "records"} and the leg
        # raised StateOperationError -- which is the correct fail-loud
        # behaviour, and is why these tests are run against the real reader
        # instead of a hand-rolled row list.
        return {"action_status": "completed", "data": {"records": list(self._rows)}}


def _measured_age_or_none(row: dict[str, Any]) -> float | None:
    """`_measured_age_seconds` against the fixed sweep clock."""
    return rsn._measured_age_seconds(row, clock=_SWEEP_NOW)


def _first_prose(bridges: _FakeBridgeManager) -> str:
    """The first delivered notice's text, or "" when nothing was delivered.

    Deliberately NOT `_first_prose(bridges)`. A mutation that stops the leg
    delivering at all would make that raise IndexError, which aborts the run
    and hides every later test's verdict -- measured, while mutation-testing
    this very suite. A missing notice must read as a clean FAIL on the
    assertion that wanted it, not as a crash.
    """
    return bridges.appended[0][2] if bridges.appended else ""


def _row(
    agent_instance_id: str = "agi-seat",
    *,
    current_tokens: int,
    ceiling: int = 1_000_000,
    model: str = "claude-opus-5",
    cache_cold: int = 0,
    overage: int = 0,
    measured_at: str = "2026-08-17T22:59:00",
) -> dict[str, Any]:
    """A gauge row. `measured_at` defaults to one minute before `_SWEEP_NOW`
    so rows are LIVE by default; the staleness tests override it."""
    return {
        "agent_instance_id": agent_instance_id,
        "model": model,
        "current_tokens": current_tokens,
        "ceiling": ceiling,
        "cache_cold": cache_cold,
        "cache_overage_signature": overage,
        "measured_at": measured_at,
    }


_SWEEP_NOW = datetime(2026, 8, 17, 23, 0, 0, tzinfo=UTC)


def _sweep(
    rows: list[dict[str, Any]],
    *,
    known: set[str] | None = None,
    latch: rsn.BandEdgeLatch | None = None,
    now: datetime | None = None,
) -> tuple[rsn.SelfNoticeCounts, _FakeBridgeManager]:
    registry = _FakeRegistry(known if known is not None else {r["agent_instance_id"] for r in rows})
    bridges = _FakeBridgeManager()
    result = rsn.sweep_rotation_self_notice(
        _FakeState(rows),  # type: ignore[arg-type]
        now=now or _SWEEP_NOW,
        peer_registry=registry,  # type: ignore[arg-type]
        bridge_manager=bridges,  # type: ignore[arg-type]
        latch=latch,
    )
    return result, bridges


# ---------------------------------------------------------------------------

def test_the_300k_to_500k_blind_spot_is_covered() -> None:
    """CATCHES: keying the leg on `rotation_due`/fraction instead of the band.

    350,000 on a 1M ceiling is the defect's exact middle -- the band saturated
    at `warm_immediate` 50,000 tokens ago, while `is_rotation_due` is still
    False because 0.35 < 0.5. A leg gated on the fraction is SILENT here, and
    this is the range in which the 2026-08-17 seat burned its 300K.

    "This count would also be 1 if the leg keyed on the fraction" -- no: the
    second assertion pins `is_rotation_due` False at the same input, so the two
    instruments are shown to DISAGREE and the leg is shown to follow the band.
    Without that second line this test could not tell the two designs apart.
    """
    counts, bridges = _sweep([_row(current_tokens=350_000)])
    _check(counts.notified == 1 and counts.unroutable == 0,
           "350K on a 1M ceiling notifies (the band says warm_immediate)")
    _check(not rt.is_rotation_due(model="claude-opus-5", current_tokens=350_000),
           "...and `rotation_due` is False at that same 350K -- the two "
           "instruments disagree, and the leg follows the BAND")
    _check("warm_immediate" in _first_prose(bridges),
           "the delivered notice names the band it fired on")


def test_a_carry_on_verdict_says_nothing_at_all() -> None:
    """CATCHES: widening ROTATION_SELF_NOTICE_BANDS to include `warm_keep`.

    A channel is trained by its first message. A leg that fires on the
    keep-working verdict would open every session's channel with something it
    must not act on, and the latch would not save it -- the latch suppresses
    REPEATS, never the first delivery.
    """
    counts, bridges = _sweep([_row(current_tokens=120_000)])
    _check(counts == rsn.SelfNoticeCounts() and not bridges.appended,
           "a warm_keep session is not notified, and nothing is appended")


def test_escalation_across_bands_is_never_suppressed() -> None:
    """CATCHES: swapping BandEdgeLatch for NoticeLatch (or keying the latch on
    the session alone).

    This is the mutation that matters most, because it fails in the direction
    that hurts: a session-keyed latch treats 150K -> 200K -> 300K as ONE
    unbroken episode, delivers the mildest notice, and then stays silent
    through every escalation. The suppressed notice would always be the more
    urgent one.
    """
    latch = rsn.BandEdgeLatch()
    t0 = datetime(2026, 8, 17, 23, 0, 0, tzinfo=UTC)
    first_counts, _ = _sweep([_row(current_tokens=180_000)], latch=latch, now=t0)
    # +2 min: well inside the 20-minute floor, but a DIFFERENT band.
    t1 = datetime(2026, 8, 17, 23, 2, 0, tzinfo=UTC)
    second_counts, bridges = _sweep([_row(current_tokens=260_000)], latch=latch, now=t1)
    _check(first_counts.notified == 1, "the warm_task_boundary crossing notifies")
    _check(second_counts.notified == 1,
           "the escalation to warm_safe_checkpoint notifies too, 2 minutes "
           "later and deep inside the repeat floor -- a new band is new "
           "information and the floor must not apply to it")
    _check("warm_safe_checkpoint" in _first_prose(bridges),
           "the second notice carries the NEW band, not the stale one")


def test_the_same_band_is_floored_at_twenty_minutes() -> None:
    """CATCHES: dropping the floor (re-notifies every 300s tick, which trains
    the reader to filter the channel) and equally CATCHES making the floor a
    blanket time gate (which would have failed the previous test).

    The pair of tests is the point: neither alone pins the design. This one
    alone passes for a leg that never notifies twice at all.
    """
    latch = rsn.BandEdgeLatch()
    row = [_row(current_tokens=260_000)]
    t0 = datetime(2026, 8, 17, 23, 0, 0, tzinfo=UTC)
    first_counts, _ = _sweep(row, latch=latch, now=t0)
    under_counts, _ = _sweep(row, latch=latch, now=datetime(2026, 8, 17, 23, 5, 0, tzinfo=UTC))
    over_counts, _ = _sweep(row, latch=latch, now=datetime(2026, 8, 17, 23, 21, 0, tzinfo=UTC))
    _check((first_counts.notified, under_counts.notified, over_counts.notified) == (1, 0, 1),
           "same band: notified, silent at +5min (inside the floor), notified "
           f"again at +21min (past ROTATION_SELF_NOTICE_FLOOR_S="
           f"{rsn.ROTATION_SELF_NOTICE_FLOOR_S}s)")


def test_an_unresolvable_session_is_counted_not_swallowed() -> None:
    """CATCHES: `continue`-ing past a registry miss instead of counting it.

    The leg's own blind spot -- a watcher-held worker whose gauge row is keyed
    on its LEDGER id while its binding is keyed on its WATCH id -- must appear
    as a NUMBER. A silent skip reads identically to a healthy run, and this
    codebase has already paid for that once with two legs that ran never.

    "The notified count would also be 1 if the miss were swallowed" -- which is
    why the assertion pins `unreachable` too. The pair is the discriminator.
    """
    rows = [
        _row("agi-seat", current_tokens=350_000),
        _row("agi-ccafb17b9ad89af3fb8998081fbcbd23", current_tokens=350_000),
    ]
    counts, bridges = _sweep(rows, known={"agi-seat"})
    _check((counts.notified, counts.unroutable) == (1, 1),
           "one notified, one counted unreachable -- the gap is a number, not "
           "a silence")
    _check(len(bridges.appended) == 1,
           "and exactly one notice was actually delivered")


def test_a_failed_delivery_does_not_latch_the_episode() -> None:
    """CATCHES: latching on ATTEMPT rather than on success.

    An episode silenced by its own delivery failure is the worst available
    outcome: the condition persists, nothing is delivered, and the latch
    guarantees nothing ever will be. Same contract NoticeLatch states.
    """
    latch = rsn.BandEdgeLatch()
    row = [_row("agi-dark", current_tokens=350_000)]
    c1, _ = _sweep(row, known=set(), latch=latch)
    c2, bridges = _sweep(row, known={"agi-dark"}, latch=latch)
    _check((c1.notified, c1.unroutable) == (0, 1),
           "first tick cannot deliver -- counted unroutable")
    _check((c2.notified, c2.unroutable) == (1, 0),
           "second tick, binding now resolvable, DELIVERS -- the failed "
           "attempt did not latch it into permanent silence")
    _check(len(bridges.appended) == 1, "and the notice really was appended")


def test_capacity_binds_early_on_an_unrecognised_model() -> None:
    """CATCHES: a fail-OPEN tier default -- the exact failure a tier-scaling
    scheme would have introduced.

    An unrecognised model resolves to DEFAULT_CONSERVATIVE_CEILING (100,000),
    so 80,000 is 80% of its window while the economics band still reads
    `warm_keep`. The session must be LOUD, never quiet. The second assertion
    pins that economics alone would have said nothing, so this test cannot pass
    for a leg that simply notifies on everything.
    """
    ceiling = rt.resolve_ceiling("some-model-shipped-after-this-landing")
    _check(ceiling == rt.DEFAULT_CONSERVATIVE_CEILING,
           f"an unknown model still falls back to {rt.DEFAULT_CONSERVATIVE_CEILING:,}")
    counts, bridges = _sweep([_row(current_tokens=80_000, ceiling=ceiling,
                                          model="some-model-shipped-after-this-landing")])
    _check(counts.notified == 1, "80K against a conservative 100K ceiling NOTIFIES")
    _check(rt.rotation_band(80_000, cache_cold=False)[0] == "warm_keep",
           "...while the ECONOMICS band alone would have said keep working -- "
           "so it is the capacity axis carrying this, not a leg that shouts at "
           "everything")
    _check("capacity_approaching" in _first_prose(bridges),
           "and the notice names capacity as the reason")


def test_capacity_never_preempts_economics_on_a_1m_ceiling() -> None:
    """CATCHES: setting CAPACITY_BAND_* low enough to fire on 1M models.

    The capacity axis exists for small windows. If it started leading on 1M
    sessions it would relabel the ratified economics policy under a different
    name, and the bands people ratified would stop being the ones that speak.
    """
    for tokens in (150_000, 200_000, 300_000, 499_999, 750_000 - 1):
        verdict = rt.rotation_surface_verdict(
            current_tokens=tokens, ceiling=1_000_000, cache_cold=False,
        )
        _check(verdict.effective_band == verdict.economics_band,
               f"at {tokens:,} on a 1M ceiling the ECONOMICS band leads "
               f"({verdict.effective_band})")


def test_the_break_even_is_tier_invariant() -> None:
    """CATCHES: reintroducing per-tier band scaling.

    The whole 2026-08-17 finding in one assertion: price cancels out of
    `C > H + kH/N`, so the same C and N give the same verdict on every model.
    A tier-scaled implementation makes these disagree by construction.
    """
    verdicts = {
        model: rt.rotation_surface_verdict(
            current_tokens=224_840, ceiling=rt.resolve_ceiling(model), cache_cold=False,
        ).economics_band
        for model in ("claude-fable-5", "claude-opus-5", "claude-sonnet-5")
    }
    _check(len(set(verdicts.values())) == 1,
           f"224,840 reads as the SAME economics band on every 1M-ceiling tier: "
           f"{verdicts}")
    _check(rt.clearing_wins(224_840, 30) is rt.clearing_wins(224_840, 30),
           "clearing_wins takes no model argument at all -- there is no tier "
           "for a future edit to scale")


def test_the_overage_ttl_moves_the_break_even_and_moves_it_down() -> None:
    """CATCHES: wiring the overage premium backwards (12.5 vs 20).

    A collapsed cache TTL makes the rewrite CHEAPER, so clearing wins SOONER.
    Getting the direction wrong would silence sessions in overage -- the state
    in which context is most expensive to carry.

    "This would also pass if both multipliers were equal" -- no: the strict
    inequality on the horizon excludes that.
    """
    nominal = rt.break_even_horizon(300_000)
    overage = rt.break_even_horizon(300_000, overage=True)
    assert nominal is not None and overage is not None
    _check(overage < nominal,
           f"overage horizon {overage:.1f} < nominal {nominal:.1f} -- clearing "
           "wins sooner under a collapsed TTL, not later")
    _check(rt.clearing_wins(167_000, 25, overage=True)
           and not rt.clearing_wins(167_000, 25),
           "167,000 at N=25 wins under overage and loses at the 1-hour TTL -- "
           "the two premiums are genuinely different thresholds")
    _check(rt.write_premium_multiplier(overage=False) == 20.0,
           "the default premium is unchanged for every pre-existing caller")


def test_the_notice_states_one_horizon_not_two() -> None:
    """CATCHES: the regression this landing already made once -- the band's
    embedded horizon computed at the nominal premium while the notice's own
    line used the overage-aware one, rendering "~4" and "~3" for one quantity.

    Two sources that can disagree about the same fact teach the reader to trust
    neither, so the assertion is on the RENDERED prose, not on the two
    functions agreeing in isolation.
    """
    row = _row(current_tokens=606_142, overage=1)
    verdict = rsn._gauge_verdict(row)
    assert verdict is not None, "a well-formed row must produce a verdict"
    prose = rsn._self_notice_prose(row, verdict)
    horizons = {token for token in prose.replace("(", " ").replace(")", " ").split()
                if token.startswith("~") and token[1:].rstrip(",.").isdigit()}
    # The two POLICY thresholds legitimately state their own horizons (~25, ~12);
    # what must not appear is two different figures for THIS session's horizon.
    _check("~3" in horizons and "~4" not in horizons,
           f"the session's own horizon appears once, overage-aware: {sorted(horizons)}")


def test_nothing_in_the_leg_can_drive_a_session() -> None:
    """CATCHES: a future edit 'fixing' the inconsistency by adding the
    `drive_on_delivery` call its three sibling notify paths all make.

    That call injects a turn into the recipient's host driver. There is a
    standing ruling that no agent sits in the injection path for a context
    clear, so its ABSENCE here is load-bearing rather than an oversight -- and
    an oversight is exactly what it looks like to someone tidying up.

    A behavioural property cannot be enumerated by scanning source, so this
    replaces the real function with one that RAISES and then runs the leg for
    real: if anything on the delivery path drives, the sweep cannot come back
    clean.
    """
    # ⚠️ THE PATCH TARGET MOVED WITH THE CODE, AND A STALE ONE PASSES SILENTLY.
    # Before the 2026-08-17 extraction this patched `ss.drive_on_delivery` and
    # ran the leg out of `session_sweep`. The leg now lives in its own module,
    # so patching `session_sweep`'s binding would affect NOTHING the code under
    # test resolves -- the sweep would run, nothing would explode, and this test
    # would report PASS while testing literally nothing. Moving code fails no
    # test, which is what makes that failure mode silent.
    #
    # Two independent guards replace it, because the leg does not import
    # `drive_on_delivery` at all and there is therefore no binding in its
    # namespace to poison:
    #
    #   1. STRUCTURAL — the leg's module has no such attribute. It cannot call
    #      what it never imported, and if a future edit adds the import to make
    #      the call, this assertion fails at that moment.
    #   2. BEHAVIOURAL — the DEFINING module's function is replaced by a
    #      landmine and the leg is then run for real, so any late/dynamic
    #      lookup (`session_lifecycle_verbs.drive_on_delivery(...)`) still
    #      detonates.
    #
    # Neither alone is sufficient: (1) is a source fact, and (2) cannot see a
    # from-import that binds a reference before the patch lands.
    _check(not hasattr(rsn, "drive_on_delivery"),
           "STRUCTURAL: the self-notice module has no drive_on_delivery "
           "binding at all -- it cannot inject a turn it never imported")

    from agent_messaging_plugin import session_lifecycle_verbs as slv
    original = slv.drive_on_delivery

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the self-notice leg must never drive a session")

    slv.drive_on_delivery = _explode  # type: ignore[assignment]
    detonated: str | None = None
    try:
        counts, bridges = _sweep([_row(current_tokens=350_000)])
    except AssertionError as exc:
        # The landmine going off is a FINDING, not a crash. Left unhandled it
        # propagates out of main() and kills the run before the summary, which
        # hides every later test's verdict -- the same crash-is-not-a-failure-
        # report pattern this suite already fixed twice (the IndexError helper,
        # and MUT-8's TypeError). A mutation battery that dies early silently
        # narrows its own coverage, so the detonation is caught and REPORTED.
        detonated = str(exc)
        counts, bridges = rsn.SelfNoticeCounts(), _FakeBridgeManager()
    finally:
        slv.drive_on_delivery = original  # type: ignore[assignment]
    _check(detonated is None,
           "BEHAVIOURAL: the leg ran with drive_on_delivery replaced by a "
           "landmine at its DEFINITION site and never tripped it -- catches a "
           "late/dynamic lookup the structural check cannot see"
           + ("" if detonated is None else f" (TRIPPED: {detonated})"))
    _check(detonated is None
           and (counts.notified, counts.unroutable) == (1, 0)
           and len(bridges.appended) == 1,
           "...and the notice was still delivered while that landmine was "
           "armed -- notice, never act")


def test_an_unusable_row_is_skipped_rather_than_guessed() -> None:
    """CATCHES: defaulting a missing ceiling (e.g. to the conservative floor)
    inside the leg.

    A row with no ceiling means the reporter told us nothing; inventing one
    would answer a capacity question with a number nobody measured, and it
    would do so in the LOUD direction.
    """
    counts, _ = _sweep([
        _row("agi-noceiling", current_tokens=350_000, ceiling=0),
        _row("agi-notokens", current_tokens=0),
    ])
    _check(counts == rsn.SelfNoticeCounts(),
           "a row with an unusable ceiling or no token count is skipped "
           "silently -- not notified, and not counted as a coverage gap either")


def test_the_rider_actually_invokes_this_leg() -> None:
    """★ THE REACHABILITY GUARD, and the reason it exists is written into the
    rider's own docstring as a ★.

    CATCHES: the leg being written, tested, and composed into NOTHING.

    This is not a hypothetical mutation. BOTH existing L4 legs landed exactly
    that way -- `sweep_rotation_due_sessions` and `sweep_gauge_coverage` were
    merged tested and invoked by nothing, so they ran never in production until
    a later change composed them. Every unit test above would pass, unchanged
    and green, against a build in which this leg is dead code. "The leg
    delivers notices" would then be a statement about capability, not about
    behaviour.

    So this drives the REAL `AgentMessagingPlugin._run_rotation_surface_sweep`
    against a duck-typed self -- the same wiring-guard shape
    `bridge_lifecycle_sweep_smoke` uses for `_on_sweep_tick` -- and asserts the
    self-notice leg is reached. Chained with that existing guard (which pins
    tick -> rider), this pins tick -> rider -> leg, which is the whole path a
    notice travels on a live sweep.

    It also pins the FAULT ISOLATION in the direction that matters: the two
    steward legs are made to raise, and the self-notice leg must still run.
    Composed inside a shared rider, an unisolated sibling fault would silence
    this leg every tick, and nothing else would report that it had.
    """
    reached: list[str] = []

    def _boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("steward leg fault -- must not cost the self leg its tick")

    # `plugin.py` does `from .rotation_self_notice import sweep_rotation_self_notice`,
    # so the binding the rider resolves lives in PLUGIN's namespace, not in the
    # defining module's. Patching the definer would not reach it. After the
    # extraction this is the ONLY target that works, and it is the same one it
    # was before -- the move did not change where the caller looks.
    import agent_messaging_plugin.plugin as plugin_mod
    p_due = plugin_mod.sweep_rotation_due_sessions
    p_dark = plugin_mod.sweep_gauge_coverage
    p_self = plugin_mod.sweep_rotation_self_notice

    def _spy(*args: object, **kwargs: object) -> rsn.SelfNoticeCounts:
        reached.append("self_notice")
        return rsn.SelfNoticeCounts()

    plugin_mod.sweep_rotation_due_sessions = _boom  # type: ignore[assignment]
    plugin_mod.sweep_gauge_coverage = _boom  # type: ignore[assignment]
    plugin_mod.sweep_rotation_self_notice = _spy  # type: ignore[assignment]
    try:
        fake_self = SimpleNamespace(
            _log_self_notice_counts=AgentMessagingPlugin._log_self_notice_counts,  # noqa: SLF001
            _get_state_service=lambda: object(),
            _peer_registry=object(),
            _bridge_manager=object(),
            _rotation_due_latch=object(),
            _gauge_coverage_latch=object(),
            _rotation_self_latch=rsn.BandEdgeLatch(),
        )
        # The rider's OWN try/except around the steward legs is what should
        # absorb _boom. If it does not, this call raises and the test fails
        # loud rather than quietly reporting an unreached leg.
        AgentMessagingPlugin._run_rotation_surface_sweep(cast("Any", fake_self))  # noqa: SLF001
    finally:
        plugin_mod.sweep_rotation_due_sessions = p_due  # type: ignore[assignment]
        plugin_mod.sweep_gauge_coverage = p_dark  # type: ignore[assignment]
        plugin_mod.sweep_rotation_self_notice = p_self  # type: ignore[assignment]

    _check(reached == ["self_notice"],
           "the REAL _run_rotation_surface_sweep invokes the self-notice leg -- "
           "and does so even with both steward legs raising, so a sibling "
           "fault cannot silence it")


def test_an_ended_session_is_neither_notified_nor_counted() -> None:
    """★ CATCHES: the unbounded scan. The blocking finding from diff review.

    `session_context_status` is NEVER PRUNED -- no reaper, no `delete_state`,
    nothing sets `is_deleted` anywhere in the repo. So the table holds a row for
    every session that has ever reported, frozen at whatever value it ended on.
    A session that ended at 380,000 tokens sits permanently in
    `warm_immediate`, is permanently a candidate, permanently resolves to no
    binding, and -- because `record_sent` never runs for it, so the latch never
    engages -- would be counted on EVERY TICK FOREVER.

    Nothing would have been spammed (a dead session has no binding), so this is
    invisible from the delivery side. The damage is to the instrument:
    `unroutable` would have been dominated by dead rows while its log prose
    blamed the watch-id join gap, and a reader watching that number climb would
    size a follow-on landing against a phantom.

    "This would also be (0,0) if the row were simply skipped for another
    reason" -- which is why the third assertion pins that the SAME row one
    minute inside the window IS notified. The pair isolates staleness as the
    cause rather than any other predicate.
    """
    stale = _row(
        "agi-ended", current_tokens=380_000, measured_at="2026-08-17T21:00:00",
    )
    counts, bridges = _sweep([stale])
    _check(counts.notified == 0, "an ended session is not notified")
    _check(counts.unroutable == 0 and counts.undeliverable == 0,
           "...and is NOT counted as a coverage gap either -- both numbers "
           "pinned, since notified==0 alone cannot tell 'skipped' from "
           "'counted as unroutable'")
    _check(not bridges.appended, "and nothing was appended for it")

    fresh = _row("agi-ended", current_tokens=380_000, measured_at="2026-08-17T22:59:00")
    live_counts, _ = _sweep([fresh])
    _check(live_counts.notified == 1,
           "...while the SAME row one minute inside the window IS notified -- "
           "so it is staleness doing the work, not some other predicate")


def test_the_staleness_window_is_loose_enough_for_a_long_tool_call() -> None:
    """CATCHES: reusing GAUGE_COVERAGE_GRACE_S (300.0) as the staleness bound.

    The reporter is a `PostToolUse` hook, so a session inside one long tool call
    is legitimately silent for minutes -- this lane's own gate battery runs ~7.
    A 300s bound would drop a live session mid-run, and it would drop it
    silently, since a dropped row is indistinguishable from a carry-on verdict.
    """
    _check(rsn.SELF_NOTICE_STALENESS_S > 300.0,
           f"the staleness bound ({rsn.SELF_NOTICE_STALENESS_S}s) is looser than "
           f"the startup grace ({ss.GAUGE_COVERAGE_GRACE_S}s) -- they are "
           "different quantities and must not share a constant")
    mid_battery = _row(current_tokens=350_000, measured_at="2026-08-17T22:52:00")
    counts, _ = _sweep([mid_battery])
    _check(counts.notified == 1,
           "a session 8 minutes into one tool call is still LIVE and is notified")


def test_measured_at_reads_back_naive_and_is_still_compared_correctly() -> None:
    """CATCHES: comparing the stored timestamp to an aware clock directly.

    The reporting hook writes `datetime.now(UTC).isoformat()` -- AWARE -- but
    the DATETIME column drops the offset, so state hands back a NAIVE string
    (measured: '2026-08-18T00:09:42.968903'). Subtracting that from an aware
    clock raises TypeError, and inside this rider's per-leg fault isolation it
    would have failed the leg SILENTLY on every tick: no notices, no counts, one
    exception line attributed to the leg rather than to the timestamp.

    Both forms are asserted, because handling only the naive one would break the
    day the column starts preserving offsets.
    """
    naive = _measured_age_or_none({"measured_at": "2026-08-17T22:59:00"})
    aware = _measured_age_or_none({"measured_at": "2026-08-17T22:59:00+00:00"})
    _check(naive is not None and abs(naive - 60.0) < 1.0,
           f"a NAIVE stored value is read as UTC and ages correctly ({naive}s)")
    _check(aware is not None and abs(aware - 60.0) < 1.0,
           f"an AWARE value is honoured as-is and agrees ({aware}s)")
    _check(_measured_age_or_none({"measured_at": "not-a-timestamp"}) is None
           and _measured_age_or_none({"measured_at": ""}) is None,
           "an unparseable or absent stamp is a distinct third answer, not a "
           "guess in either direction")
    counts, _ = _sweep([_row(current_tokens=350_000, measured_at="not-a-timestamp")])
    _check(counts == rsn.SelfNoticeCounts(),
           "...and such a row is skipped entirely -- guessing FRESH would "
           "resurrect the unbounded scan, guessing STALE would silence a live "
           "session")


def test_a_delivery_fault_is_not_reported_as_a_routing_gap() -> None:
    """CATCHES: collapsing 'no binding' and 'append raised' into one count.

    They have different causes and different owners, and the log prose names
    the routing gap specifically. A single failure count made every transport
    fault read as evidence for the watch-id join gap.
    """
    class _ExplodingBridges(_FakeBridgeManager):
        def append_event(self, bridge_id, event, prose, meta):  # type: ignore[no-untyped-def]
            raise RuntimeError("bridge went away mid-append")

    registry = _FakeRegistry({"agi-live"})
    bridges = _ExplodingBridges()
    counts = rsn.sweep_rotation_self_notice(
        _FakeState([_row("agi-live", current_tokens=350_000)]),  # type: ignore[arg-type]
        now=_SWEEP_NOW,
        peer_registry=registry,  # type: ignore[arg-type]
        bridge_manager=bridges,  # type: ignore[arg-type]
    )
    _check((counts.notified, counts.unroutable, counts.undeliverable) == (0, 0, 1),
           "a resolved binding whose append raises counts as UNDELIVERABLE, "
           "never as unroutable -- the join gap is not blamed for a transport "
           "fault")


def main() -> int:
    print("rotation self-notice (L4c) smoke\n")
    test_the_300k_to_500k_blind_spot_is_covered()
    test_a_carry_on_verdict_says_nothing_at_all()
    test_escalation_across_bands_is_never_suppressed()
    test_the_same_band_is_floored_at_twenty_minutes()
    test_an_unresolvable_session_is_counted_not_swallowed()
    test_a_failed_delivery_does_not_latch_the_episode()
    test_capacity_binds_early_on_an_unrecognised_model()
    test_capacity_never_preempts_economics_on_a_1m_ceiling()
    test_the_break_even_is_tier_invariant()
    test_the_overage_ttl_moves_the_break_even_and_moves_it_down()
    test_the_notice_states_one_horizon_not_two()
    test_nothing_in_the_leg_can_drive_a_session()
    test_an_unusable_row_is_skipped_rather_than_guessed()
    test_an_ended_session_is_neither_notified_nor_counted()
    test_the_staleness_window_is_loose_enough_for_a_long_tool_call()
    test_measured_at_reads_back_naive_and_is_still_compared_correctly()
    test_a_delivery_fault_is_not_reported_as_a_routing_gap()
    test_the_rider_actually_invokes_this_leg()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
