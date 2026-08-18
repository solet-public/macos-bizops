"""L4c -- the rotation SELF-NOTICE leg: the half of the context-rotation
surface that reaches the MEASURED SESSION ITSELF rather than its steward.

Its two sibling legs (`sweep_rotation_due_sessions`, `sweep_gauge_coverage`)
live in `session_sweep.py` and enumerate the `managed_session` lifecycle
ledger, which structurally has no row for an operator-launched seat. This leg
scans `session_context_status` directly -- that table has no FK to the ledger,
so a `host=operator` row is representable -- and appends to the measured
session's own bridge.

WHY ITS OWN MODULE, given that all three legs share one rider. It was written
inside `session_sweep.py` and moved out on 2026-08-17 for a measured reason:
at ~474 lines it took that module's maintainability index from 18.59 to 8.88,
across the gate's B/C boundary at 10.00. The pre-move landing had passed at
10.86 -- a TRUE green with 0.86 of headroom, which is the least informative
kind of pass: nothing in a pass/fail gate distinguishes 10.86 from 18.59, so
"this file cannot absorb another fifteen lines" was invisible in a green
report. The next edit by ANY lane would have blown it.

Extracting rather than allowlisting fixes the metric on its own merits instead
of silencing it. The leg shares a rider with its siblings but shares no state,
no helpers and no data with them -- it was already a module, it just was not in
a file yet.

★ NOTICE, NEVER ACT. Nothing here clears anything. See
:data:`EVENT_ROTATION_SELF_NOTICE` and :func:`_notify_rotation_self` for the
standing ruling that no agent sits in the injection path for a context clear,
and for why this module deliberately does NOT import `drive_on_delivery`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from . import rotation_thresholds
from .session_context_status_store import list_session_context_statuses

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .models import BridgeBinding
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)


# L4c -- THE SELF-NOTICE LEG (2026-08-17). The half of the rotation surface
# that reaches the session ITSELF rather than its steward.
# ---------------------------------------------------------------------------

EVENT_ROTATION_SELF_NOTICE = "rotation_self_notice"
"""Notice delivered to a session's OWN bridge saying how big its context has
got and what the ratified bands say about it.

WHY A THIRD ROTATION EVENT RATHER THAN REUSING `rotation_due_notice`: that
event is addressed to a STEWARD and is written in the third person about
someone else's session ("<id> is at N tokens"). This one is addressed to the
session in the second person about itself. Same measurement, different
recipient and different grammar; sharing the name would make a reader who
filters on it unable to tell whether a given notice was about them or about a
worker they are responsible for.

NOTICE, NEVER ACT -- the same contract as `EVENT_TTL_OVERDUE_NOTICE`, and
here it is load-bearing rather than stylistic. This leg does NOT call
`drive_on_delivery`, unlike every other notify path in this module. That is
deliberate and must stay: driving a session's host driver INJECTS a turn, and
there is a standing ruling that no agent sits in the injection path for a
context clear. `append_event` alone lands the notice on the session's own
bridge, where it surfaces at the session's next natural boundary without
interrupting in-flight work -- which is exactly the non-interrupting surface
the operator asked for, and is already proven in production by the TTL leg.
"""

ROTATION_SELF_NOTICE_FLOOR_S: int = 1200
"""Minimum seconds before the SAME band is re-notified to the SAME session.

The operator's ask was "something scheduled to run once every 20 minutes".
This is that 20 minutes -- and it is a FLOOR ON REPETITION, not a period. No
scheduler is created: the sweeper already ticks every
`bridge_sweep_interval_seconds` (300s), so this leg runs on a tick that exists
and this constant governs only how often it may say the same thing twice.

Crossing into a NEW band notifies on the very next tick regardless of this
floor -- a band change is new information and delaying it by up to 20 minutes
would reintroduce, in miniature, the delivery lag this whole leg exists to
remove.
"""

SELF_NOTICE_STALENESS_S: float = 3600.0
"""How old a gauge row may be and still be treated as a LIVE session.

★ THIS IS A LIVENESS PROXY, NOT A FRESHNESS REQUIREMENT, and it is loose on
purpose. `session_context_status` is never pruned -- there is no reaper, no
`delete_state` against it, and nothing anywhere sets `is_deleted` (verified
across the repo 2026-08-17). So the table holds a row for EVERY
`agent_instance_id` that has ever reported, frozen at whatever value that
session ended on. Without this bound the leg does not enumerate live sessions,
it enumerates the entire history of sessions: a session that ended at 380,000
tokens sits permanently in `warm_immediate`, is permanently a candidate,
permanently resolves to no binding, and is therefore permanently counted -- on
every tick, forever, since `record_sent` never runs for it and the latch never
engages.

The delivery path was never at risk (a dead session has no binding, so nothing
is spammed). The damage was to the INSTRUMENT: `unreachable` would have been
dominated by dead sessions while its log prose named the watch-id join gap as
the cause, so a reader watching that number climb would size a follow-on
landing against a phantom. A true number attached to the wrong noun, in the
one field added specifically so a gap would surface as a number rather than a
silence.

WHY 3600s AND NOT `GAUGE_COVERAGE_GRACE_S` (300.0). That constant is a STARTUP
grace for a session that has not reported YET -- a different quantity that
merely happens to be a duration, and reusing it would couple two unrelated
policies through one symbol. 300s is also far too tight here: the reporter is a
`PostToolUse` hook, so a session inside a single long tool call is legitimately
silent for longer than that (this lane's own gate battery runs ~7 minutes) and
must not be dropped mid-run. An hour is generous against every observed
reporting cadence while still excluding sessions that ended, which is the only
distinction this constant has to make.
"""

ROTATION_SELF_NOTICE_BANDS: frozenset[str] = frozenset({
    "warm_task_boundary",
    "warm_safe_checkpoint",
    "warm_immediate",
    "cold_above_h",
    "capacity_approaching",
    "capacity_critical",
})
"""The effective bands worth interrupting nobody for.

`warm_keep`, `cold_below_h` and `capacity_ok` are the "carry on" verdicts, and
a notice that fires on them would deliver a message on every tick of every
session's entire life. The latch would suppress the repeats, but the FIRST one
would still arrive -- teaching every session that this channel opens with
something it does not need to act on. A channel is trained on its first
message.
"""


@dataclass(frozen=True)
class SelfNoticeCounts:
    """What one self-notice sweep actually did, as three distinguishable numbers.

    A single "failed" count would conflate a ROUTING gap (neither the row's
    own key nor its stored session-id join resolves to a live binding) with a
    DELIVERY fault (the binding resolved and `append_event` raised). Those have
    different causes, different owners and different fixes, and one of this
    leg's own arguments is that a number which cannot discriminate is the thing
    this codebase keeps paying for. Splitting them costs one field.

    Since 2026-08-18 `unroutable` counts a REPORTER-VERSION gap (a row predating
    the `agent_session_id` join column, which carries NULL), not the structural
    watch-id gap it originally counted. It should decay to zero as reporters
    upgrade; one that does not is a new fault, not the old known one.
    """

    notified: int = 0
    unroutable: int = 0
    undeliverable: int = 0


class BandEdgeLatch:
    """One-notice-per-BAND-EDGE gate, with a floor on repeating the same band.

    :class:`NoticeLatch` is not enough here and the difference is not a
    refinement. That latch answers "has this session been told about this
    EPISODE", keyed on the session alone, and it releases only when the
    condition clears entirely. A session that crosses 150K, then 200K, then
    300K is ONE unbroken episode by that definition -- so a `NoticeLatch` would
    deliver the 150K notice and then stay silent through both escalations,
    which is the failure mode that matters most: the band that gets suppressed
    is always the more urgent one.

    Keying on ``(session, band)`` instead makes each crossing its own edge. The
    floor then handles the opposite problem: a session that sits in
    `warm_immediate` for three hours is still in one band, and without a
    time bound it would be re-notified only never, or (if released) every 300s.
    Neither is what the operator asked for.

    So the rule is exactly two lines:
      * band CHANGED  -> notify now, floor does not apply (new information).
      * band SAME     -> notify only if `floor_seconds` have passed.

    In-memory and process-lifetime bounded, the same stated trade
    :class:`NoticeLatch` makes and for the same reason: a solet restart re-arms
    every session and each gets at most one extra notice. Restarts are rare and
    ticks are every five minutes, so the error is self-limiting in the
    direction that matters.
    """

    def __init__(self, floor_seconds: int = ROTATION_SELF_NOTICE_FLOOR_S) -> None:
        self._floor_seconds = floor_seconds
        self._sent: dict[str, tuple[str, datetime]] = {}

    def suppressed(self, key: str, band: str, *, now: datetime) -> bool:
        """True when ``key`` should NOT be notified about ``band`` right now."""
        previous = self._sent.get(key)
        if previous is None:
            return False
        last_band, last_at = previous
        if last_band != band:
            return False
        return (now - last_at).total_seconds() < self._floor_seconds

    def record_sent(self, key: str, band: str, *, now: datetime) -> None:
        """Latch ``key`` at ``band`` -- call only after delivery succeeded.

        Same posture as :meth:`NoticeLatch.record_sent`: a failed delivery
        leaves the entry untouched so the next tick retries. An episode must
        never be silenced by its own delivery failure.
        """
        self._sent[key] = (band, now)

    def retain_active(self, active: set[str]) -> None:
        """Forget every session that is no longer in a notifiable band.

        A session that rotates back down to `warm_keep` drops out here, so if
        it later climbs back into the same band that is a NEW episode and
        notifies immediately rather than waiting out a floor it started before
        it rotated.
        """
        self._sent = {key: value for key, value in self._sent.items() if key in active}


def _measured_age_seconds(row: dict[str, Any], *, clock: datetime) -> float | None:
    """Seconds since this row's `measured_at`, or None when it cannot be established.

    ⚠️ `measured_at` IS WRITTEN AWARE AND READS BACK NAIVE. The reporting hook
    writes `datetime.now(UTC).isoformat()`, but the `DATETIME` column drops the
    offset on the round-trip, so what comes out of state is naive-UTC
    (measured, not assumed: a stored value read back as
    '2026-08-18T00:09:42.968903'). Subtracting that from an aware `clock`
    raises `TypeError` -- which, inside this rider's per-leg fault isolation,
    would have failed the leg SILENTLY on every tick.

    So the naive case is handled explicitly and its contract is stated here
    rather than inherited by coincidence, exactly as
    :class:`rotation_thresholds.TimestampAwarenessError` argues each parser
    should. An already-aware value is honoured as-is so this keeps working if
    the column ever preserves the offset.

    None (unparseable or absent) is a genuine third answer, not an error to
    swallow: `measured_at` is `not_null` and written by our own hook, so a value
    that will not parse is a reporting-path defect. It is surfaced by the
    caller as its own skip rather than being guessed in either direction --
    guessing FRESH resurrects the unbounded-scan bug for malformed rows, and
    guessing STALE silences a live session.
    """
    raw = str(row.get("measured_at") or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (clock - stamp).total_seconds()


def _gauge_verdict(row: dict[str, Any]) -> rotation_thresholds.RotationVerdict | None:
    """The two-axis verdict for one gauge row, or ``None`` when the row cannot
    support one.

    Every ``None`` is a deliberate skip of a row this leg has nothing true to
    say about, never a swallowed failure: an unusable ceiling (the same guard
    :func:`_rotation_due_row` applies) or a missing token count. Notably it
    does NOT skip on a missing `cache_cold` -- that column is nullable and a
    NULL means NOT REPORTED, so it is passed through as the warm default and
    the notice says so rather than silently asserting a measurement.
    """
    ceiling = int(row.get("ceiling") or 0)
    current = int(row.get("current_tokens") or 0)
    if ceiling <= 0 or current <= 0:
        return None
    return rotation_thresholds.rotation_surface_verdict(
        current_tokens=current,
        ceiling=ceiling,
        cache_cold=bool(row.get("cache_cold")),
        overage=bool(row.get("cache_overage_signature")),
    )


def _self_notice_prose(row: dict[str, Any], verdict: rotation_thresholds.RotationVerdict) -> str:
    """The notice text, in the shape the operator asked for.

    The operator's own words were: *"context has reached xxx tokens, clearing
    is recommended after XXX tokens and strongly recommended after XXX
    tokens"* -- a measured number and the two thresholds ahead of it. This
    carries that, plus the thing that makes a threshold arguable rather than
    arbitrary: the HORIZON each one was derived from. "Rotate at 300,000" is a
    rule to be obeyed or ignored; "at 300,000 a clear pays for itself with as
    few as ~12 calls left" is a claim a reader can check.

    Composed OUTSIDE its caller's delivery `try` for the reason
    :func:`_notify_rotation_due` records: a notice family whose whole purpose
    is to be the thing that speaks up must not be able to eat its own message
    bug and log it as a delivery failure.
    """
    current = int(row["current_tokens"])
    ceiling = int(row["ceiling"])
    model = row.get("model") or "unknown model"
    # The horizon is stated ONCE, inside the band's own guidance, which
    # `rotation_surface_verdict` computes against the SAME overage flag as
    # `verdict.horizon_calls`. An earlier draft printed it a second time from
    # `horizon_calls` directly and the two disagreed under overage (~4 vs ~3),
    # because the guidance was still using the nominal premium. One quantity,
    # one number, one source.
    ttl_note = (
        " Note: the prompt-cache TTL is showing the overage signature (~5 min "
        "rather than 1 hour), so the horizon above is computed against the "
        f"cheaper {rotation_thresholds.CACHE_WRITE_PREMIUM_MULTIPLIER_OVERAGE:g}x "
        "rewrite premium -- clearing wins SOONER than the nominal thresholds "
        "below suggest."
        if verdict.overage else ""
    )
    # The two POLICY thresholds' own horizons, bound to locals so the f-string
    # below stays inside the line limit AND so the reader can see that both are
    # derived from the same function the session's own horizon came from --
    # never transcribed.
    recommended = rotation_thresholds.WARM_BAND_TASK_BOUNDARY_TOKENS
    strongly = rotation_thresholds.WARM_BAND_SAFE_CHECKPOINT_TOKENS
    recommended_n = rotation_thresholds.break_even_horizon(recommended)
    strongly_n = rotation_thresholds.break_even_horizon(strongly)
    if recommended_n is None or strongly_n is None:  # pragma: no cover - both are > H
        raise ValueError(
            "the ratified bands must sit above H for their horizons to exist; "
            f"got recommended={recommended}, strongly={strongly}, "
            f"H={rotation_thresholds.POLICY_H_TOKENS}",
        )
    return (
        f"rotation_self_notice: YOUR context has reached {current:,} tokens on "
        f"{model} ({current / ceiling:.1%} of its {ceiling:,} ceiling).\n"
        f"Band: {verdict.effective_band} -- {verdict.headline}.{ttl_note}\n"
        f"The ratified thresholds and the horizons they came from: clearing is "
        f"RECOMMENDED past {recommended:,} (pays off with ~{recommended_n:.0f} "
        f"calls left) and STRONGLY RECOMMENDED past {strongly:,} (pays off "
        f"with ~{strongly_n:.0f} calls left).\n"
        f"Economics axis: {verdict.economics_band}. Capacity axis: "
        f"{verdict.capacity_band} -- {verdict.capacity_guidance}\n"
        f"Measured at {row.get('measured_at')}. NOTHING HAS BEEN CLEARED AND "
        "NOTHING WILL BE: this is a notice, the decision and the action are "
        "yours."
    )


def _resolve_self_binding(
    peer_registry: PeerRegistry,
    *,
    row: dict[str, Any],
    agent_instance_id: str,
) -> BridgeBinding | None:
    """The measured session's LIVE bridge binding, or None if nothing routes.

    TWO KEYS, TRIED IN ORDER, because a session's gauge row and its live
    binding are not always keyed on the same id:

    1. ``agent_instance_id`` -- this row's own key. Correct and sufficient for
       a bridge-held session (every operator-launched seat), where the ledger
       id and the binding key are the same string.
    2. ``agent_session_id`` -- the stored stable session id, reverse-resolved
       through the registry. This is the watcher-held-worker case: the gauge
       row keys on the LEDGER id (``agi-<hash>``, what ``$AGENT_INSTANCE_ID``
       carries) while the live binding keys on the WATCH id
       (``agi-watch-<hash>``). Different strings, same session, and before this
       column existed nothing related them -- so a worker could report its own
       context every two minutes and remain permanently unreachable from the
       row it had just written. Measured 2026-08-17 on the live table: 3 of 4
       lanes unroutable, and the one that resolved was the bridge-held seat.

    Order matters and this order is the cheap one: the instance lookup is a
    single indexed read that succeeds for the common case, so the join is only
    attempted for rows it can actually help.

    ★ THE JOIN IS RESOLVED THROUGH THE REGISTRY, NEVER DERIVED FROM THE ID.
    The stored value currently looks like ``"ases-" + agent_instance_id``, and
    reconstructing it from the ledger id would pass every test that exists,
    including a live one, for exactly as long as that convention holds. It is
    one launcher's formatting choice, not a join. A test asserting the string
    shape would verify the convention and never once exercise the routing, and
    the failure -- routing to nowhere, or to the wrong session -- would surface
    only once some other launcher minted a session id another way. So: no
    ``startswith``, no prefix slicing, no reconstruction. If the registry
    cannot resolve it, it does not resolve.

    A NULL join column is NOT REPORTED (a reporter predating the widening),
    which is why a miss here is counted rather than raised: it is a coverage
    gap that heals when the reporter deploys, not a fault.

    ``PeerSessionAmbiguousError`` is deliberately NOT caught. A session holds
    at most one live bridge, so two bindings for one session id is a corrupt
    registry, and this leg's whole job is delivering a message to the right
    session. Guessing between two candidates could deliver a private context
    measurement to the wrong one. The rider's per-leg ``try/except`` turns this
    into a logged leg fault that costs the other legs nothing -- loud, bounded,
    and recoverable -- which is the correct handling for "the data says
    something impossible".
    """
    binding = peer_registry.resolve_by_agent_instance_id(agent_instance_id)
    if binding is not None:
        return binding
    agent_session_id = row.get("agent_session_id")
    if not agent_session_id:
        return None
    return peer_registry.resolve_by_agent_session_id(str(agent_session_id))


def _notify_rotation_self(
    *,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
    agent_instance_id: str,
    verdict: rotation_thresholds.RotationVerdict,
) -> Literal["notified", "unroutable", "undeliverable"]:
    """Best-effort notice to the session's OWN bridge.

    Returns WHICH outcome, not merely whether it worked. `unroutable` (no
    binding for this instance id) and `undeliverable` (binding resolved,
    `append_event` raised) have different causes and different owners, and a
    single False collapsed them under log prose that named only the first.

    Resolution is :func:`_resolve_self_binding` -- the row's own key first,
    then the stored session-id join -- and a miss is a COUNTED coverage gap
    rather than a silent skip; see :func:`sweep_rotation_self_notice` for what
    the gap now is and why it is reported as a number.

    ★ NO `drive_on_delivery` CALL HERE, unlike every sibling notify in this
    module. Driving the host driver injects a turn; the standing ruling is that
    no agent sits in the injection path for a context clear. Appending to the
    session's own bridge is enough -- it surfaces at the next natural boundary
    without interrupting in-flight work. If a future edit "fixes" the
    inconsistency by adding the drive call, it will have converted a notice
    into an interruption and broken the ruling. It is absent on purpose.
    """
    binding = _resolve_self_binding(
        peer_registry, row=row, agent_instance_id=agent_instance_id,
    )
    if binding is None:
        return "unroutable"
    prose = _self_notice_prose(row, verdict)
    try:
        bridge_manager.append_event(
            binding.bridge_id,
            EVENT_ROTATION_SELF_NOTICE,
            prose,
            {"flow_id": f"rotation-self-{agent_instance_id}"},
        )
    except Exception:  # noqa: BLE001 — best-effort notify, never fails the sweep
        logger.warning(
            "session %s rotation self-notice append failed",
            agent_instance_id, exc_info=True,
        )
        return "undeliverable"
    return "notified"


def _self_notice_candidate(
    row: dict[str, Any], *, clock: datetime,
) -> tuple[str, rotation_thresholds.RotationVerdict] | None:
    """``(agent_instance_id, verdict)`` for a row worth notifying, else None.

    Split out of :func:`sweep_rotation_self_notice`'s loop so that loop reads
    as "for each notifiable session, deliver unless latched" and the decision
    of what counts as NOTIFIABLE lives in one place -- the same separation
    :func:`_rotation_due_row` makes for the steward leg, and the same reason:
    a sweep loop that also adjudicates eligibility is a loop whose delivery
    policy cannot be read without reading its threshold policy too.

    Each ``None`` is a deliberate skip, never a swallowed failure: a row with
    no instance id (nothing to route to), a row too STALE to be a live session
    (see :data:`SELF_NOTICE_STALENESS_S` -- the gauge table is never pruned, so
    without this the scan walks the entire history of sessions), a row whose
    age cannot be established at all, a row that cannot support a verdict (see
    :func:`_gauge_verdict`), or a session whose effective band is one of the
    carry-on verdicts.

    The staleness gate lives HERE rather than in
    :func:`list_session_context_statuses` on purpose: that function's docstring
    promises it filters nothing beyond ``is_deleted`` so a future consumer
    asking a different question of the same rows does not have to defeat its
    opinion first -- and keeping every "reason a row is not a candidate" in one
    place is what lets a reader check the list is complete.
    """
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return None
    age_seconds = _measured_age_seconds(row, clock=clock)
    if age_seconds is None or age_seconds > SELF_NOTICE_STALENESS_S:
        return None
    verdict = _gauge_verdict(row)
    if verdict is None:
        return None
    if verdict.effective_band not in ROTATION_SELF_NOTICE_BANDS:
        return None
    return (agent_instance_id, verdict)


def sweep_rotation_self_notice(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    latch: BandEdgeLatch | None = None,
) -> SelfNoticeCounts:
    """Tell each session ITSELF how big its context has got.

    ★ THE GAP THIS CLOSES, stated plainly because two sessions took personal
    responsibility for it before it was understood as a wiring fact. The
    context gauge is MEASURED by a `PostToolUse` hook and SURFACED to the seat
    by a `UserPromptSubmit` hook. So detection is continuous while delivery is
    gated on the operator typing -- and a session running autonomously for
    hours, which is precisely the condition that runs context up, is never
    shown its own number. The only surface that would tell it is silent exactly
    when the problem occurs, because it fires on the event whose ABSENCE causes
    the problem. Two context overruns (300K->559K on 2026-08-16, and a rotation
    at 606,142 on 2026-08-17) were both recorded as discipline failures. They
    were delivery failures. This leg is the delivery path that does not depend
    on anyone typing.

    ★ KEYED ON THE BAND, NEVER ON `rotation_due`, and this is not a preference.
    `rotation_due` is `fraction >= ROTATION_THRESHOLD_FRACTION` (0.5), which on
    a 1M-ceiling model is 500,000, while `rotation_band` saturates at 300,000.
    Every 1M-ceiling session therefore has a permanent 300K-500K window in
    which the band says "rotate immediately" and the fraction gate says
    nothing -- and `sweep_rotation_due_sessions` gates on the fraction. A leg
    built on `rotation_due` would have stayed silent through the ENTIRE range
    in which the 2026-08-17 seat burned its 300K. Measured live while this leg
    was being written: the dispatching seat sat at 184,680 tokens in band
    `warm_task_boundary` with `rotation_due` False.

    ★ IT SCANS `session_context_status`, NOT `managed_session`. Its two sibling
    legs enumerate the lifecycle ledger, which structurally has no row for a
    `host=operator` seat -- so no amount of fixing their thresholds could ever
    have reached one. See :func:`list_session_context_statuses`.

    ★ THE WATCH-ID COVERAGE GAP IS CLOSED (2026-08-18). It was this leg's
    largest known blind spot and is worth keeping the shape of, because the
    number it produced was itself misleading in an instructive way. A gauge row
    is keyed on the session's LEDGER instance id (what `$AGENT_INSTANCE_ID`
    carries), while a watcher-held session's live peer binding is keyed on its
    WATCH instance id (`agi-watch-<hash>`). For a bridge-held session --
    including every operator-launched seat, the case this leg was built for --
    those are the same string and resolution always succeeded. For a
    watcher-held worker they differ, and with no stored join the leg could not
    route to a session that had written the very row it was reading. Measured
    live 2026-08-17: 3 of 4 lanes unroutable, the sole success being the
    bridge-held seat. :func:`_resolve_self_binding` now falls back to the
    stored `agent_session_id` through `resolve_by_agent_session_id`, so a
    watcher-held worker is reachable from its own gauge row.

    WHAT `unroutable` MEANS NOW, which is NOT what it meant before. It is no
    longer a structural gap that no amount of correct behaviour could close; it
    is a REPORTER-VERSION gap. A row written before the join column shipped
    carries NULL, and NULL is NOT REPORTED -- so the count decays to zero on
    its own as reporters upgrade, and a count that STAYS non-zero after a full
    deploy cycle means something new is wrong. That is a different signal with
    a different response, and the two must not be read as the same number.
    Every count is logged by the caller together: an unresolved count on its
    own reads identically to a healthy run, which is the discriminator failure
    this codebase keeps paying for.

    The count is trustworthy ONLY because :data:`SELF_NOTICE_STALENESS_S`
    excludes ended sessions first. Before that bound existed the same field
    read 27, of which 24 were dead sessions and 3 were the join gap -- a true
    number attached to the wrong noun, overstating the cause the prose named by
    ~9x, in the one field added so a gap would surface as a number rather than
    a silence.

    ★ THE JOIN IS RESOLVED, NEVER DERIVED. See :func:`_resolve_self_binding`:
    the stored value currently looks like `"ases-" + ledger id`, and that is
    one launcher's convention, not a join. Reconstructing it would pass every
    test including a live one, right up until a session id is minted some other
    way.

    ``latch`` gates repetition per BAND EDGE (see :class:`BandEdgeLatch`).
    Passing None means "notify every call", right for a one-shot or a test and
    WRONG for a repeating tick -- the composed production caller always
    supplies one.
    """
    if peer_registry is None or bridge_manager is None:
        return SelfNoticeCounts()
    clock = now or datetime.now(UTC)
    gate = latch if latch is not None else BandEdgeLatch()
    tally = {"notified": 0, "unroutable": 0, "undeliverable": 0}
    notifiable: set[str] = set()
    for row in list_session_context_statuses(state):
        candidate = _self_notice_candidate(row, clock=clock)
        if candidate is None:
            continue
        agent_instance_id, verdict = candidate
        notifiable.add(agent_instance_id)
        if gate.suppressed(agent_instance_id, verdict.effective_band, now=clock):
            continue
        outcome = _notify_rotation_self(
            peer_registry=peer_registry, bridge_manager=bridge_manager,
            row=row, agent_instance_id=agent_instance_id, verdict=verdict,
        )
        tally[outcome] += 1
        if outcome == "notified":
            gate.record_sent(agent_instance_id, verdict.effective_band, now=clock)
    gate.retain_active(notifiable)
    return SelfNoticeCounts(**tally)


__all__ = [
    "EVENT_ROTATION_SELF_NOTICE",
    "ROTATION_SELF_NOTICE_BANDS",
    "ROTATION_SELF_NOTICE_FLOOR_S",
    "SELF_NOTICE_STALENESS_S",
    "BandEdgeLatch",
    "SelfNoticeCounts",
    "sweep_rotation_self_notice",
]
