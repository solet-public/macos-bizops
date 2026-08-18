"""maintenance-verbs M1 (workbench
2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3; coordinator-seat ruling
on Q3, 2026-08-09) — `report_context_status` / `session_context_status`.

Shape (a), ratified: a hook (`rotation_due_watch.py`) already computes
current-context-window occupancy client-side every tick; `report_context_status`
is a plain state upsert of that measurement (no file/subprocess I/O in this
handler — the file read already happened in the caller, sanctioned ms-scale
state work per D0.3 §1), and `session_context_status` is a trivial state read
of the cached row. Neither verb resolves a transcript path or reads a file
itself; both are pure over `session_context_status_store`.

Fraction / rotation-due / per-prompt-carriage are derived at READ time from
the live `rotation_thresholds` constants, never stored — a future change to
`ROTATION_THRESHOLD_FRACTION` must never require a backfill of stored rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import rotation_thresholds
from .session_context_status_store import read_session_context_status, upsert_session_context_status
from .session_lifecycle_verbs import VerbError

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

# The per-prompt carriage-cost heuristic named in the operator's 2026-08-09
# ruling (feedback_rotation_economics_and_context_gauge): a cached-context
# read bills roughly this fraction of base input, EVERY turn, before any new
# work. A declared constant, not a guess dressed as one — matches the
# operator's own "doesn't 800k context result in 80k tokens used per prompt?"
# arithmetic exactly (800_000 * 0.1 = 80_000).
CACHE_READ_COST_FRACTION = 0.1

# The path CLASSES a reporting hook can identify itself as (2026-08-16). A
# CLASS, deliberately, not a path: the stored row must stay meaningful across
# machines, and an absolute path would encode one host's layout into shared
# state. 'unknown' is a first-class member rather than an error case — a hook
# that cannot classify its own location must be able to say exactly that,
# because the alternative is guessing, and a guessed surface is worse than an
# admitted unknown for the one job these columns have.
REPORTER_SURFACE_CHECKOUT = "checkout"
REPORTER_SURFACE_PLUGIN_CACHE = "plugin_cache"
REPORTER_SURFACE_UNKNOWN = "unknown"
# WIDENED 2026-08-17 (phase 1 of 2). The original three collapsed at least
# three genuinely distinct surfaces into `unknown`: the vendored source copy
# in the repo, the copy of it inside a deployed release tree, and -- not
# anticipated when the field shipped -- any checkout hook living in a
# SUBDIRECTORY of `.claude/hooks/`, which the reporter's `parents[N]` test
# failed to recognise. Measured consequence: a row's surface was observed
# ALTERNATING between `checkout` and `unknown` tick-by-tick on the same
# session, which is the shared-throttle race between two copies that both
# carry the field -- and the collapsed bucket made it impossible to say which
# copy the other one was.
#
# ★ THIS HALF LANDS AND DEPLOYS ALONE, BEFORE ANY REPORTER EMITS THE NEW
# VALUES. The verb rejects an unrecognised surface BEFORE any write, so a
# reporter that learned the new classes first would have every report refused
# and would write no row at all -- turning an attribution gap into a total
# reporting outage for exactly the sessions the change exists to illuminate.
# Widening is inert on its own: it rejects nothing that previously passed, and
# nothing emits these yet. The reporter half follows only once this is
# CONFIRMED SERVING (checked against the serving release's own identity, not
# master and not a deploy report -- deployed is not serving until the swap is
# confirmed).
REPORTER_SURFACE_VENDORED = "vendored"
REPORTER_SURFACE_RELEASE = "release"
REPORTER_SURFACES = frozenset(
    {
        REPORTER_SURFACE_CHECKOUT,
        REPORTER_SURFACE_PLUGIN_CACHE,
        REPORTER_SURFACE_VENDORED,
        REPORTER_SURFACE_RELEASE,
        REPORTER_SURFACE_UNKNOWN,
    },
)


def report_context_status(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    claude_session_id: str,
    model: str,
    current_tokens: int,
    ceiling: int,
    measured_at: str,
    cache_read_tokens: int | None = None,
    cache_cold: bool | None = None,
    cache_overage_signature: bool | None = None,
    reporter_surface: str | None = None,
    reporter_generation: int | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Overwrite the caller's own latest context-status snapshot. The caller
    (today: `rotation_due_watch.py`, extended) already did the local
    transcript read and the `rotation_thresholds.resolve_ceiling` lookup —
    this verb trusts the reported `ceiling`/`current_tokens` rather than
    recomputing them, so it never touches a file itself.

    The three cache fields are OPTIONAL and describe THE MOST RECENT
    ASSISTANT CALL — the same call `current_tokens` is summed from.
    Omitting them records NOT REPORTED, which the read-back verb surfaces
    as `null` rather than as a warm cache: a reporter that never looked
    and a reporter that looked and found the cache live are different
    facts, and collapsing them would let every un-upgraded hook assert a
    warm cache it never measured.

    `reporter_surface`/`reporter_generation` are OPTIONAL and identify WHICH
    COPY of the reporting hook produced this snapshot. More than one copy can
    be registered on the same event at once, they serialize on a shared
    throttle marker that records nothing about who claimed it, and this
    table keeps only the latest row — so without these a row is
    unattributable, and an absent cache field cannot be told apart from a
    stale copy having served that tick. Omitting them records a
    pre-attribution reporter, which is a positive finding, not missing data.

    `agent_session_id` is OPTIONAL and is the ROUTING JOIN (2026-08-18): the
    reporter's own stable `$AGENT_SESSION_ID`, stored so a consumer holding
    this row can reverse-resolve the session's live bridge binding through
    `peer_registry.resolve_by_agent_session_id`. Without it, a watcher-held
    worker is unreachable from its own gauge row -- the row keys on the LEDGER
    id and the binding keys on the WATCH id. Omitting it records NOT REPORTED,
    never "this session has no bridge".

    ★ UNLIKE `reporter_surface`, THIS FIELD IS NOT VALIDATED AGAINST AN
    ALLOWLIST, and that difference is deliberate. The surface allowlist is why
    that widening had to land and deploy ALONE, ahead of any reporter emitting
    it: a reporter upgrading first would have had every report REFUSED, turning
    an attribution gap into a total reporting outage. This field has no such
    edge -- MEASURED 2026-08-18 against the live pre-change verb, which
    accepted an undeclared `agent_session_id` and returned `recorded` while
    ignoring it. So both deploy orders degrade to NULL and neither loses a row,
    and this landing carries NO ordering constraint. Recorded because the
    precedent sitting a few lines above says the opposite, and a reader is
    entitled to assume it binds here too.

    Errors: `missing_argument` (any of the five required fields empty/absent
    — fast-fail before any write), `negative_tokens` (a reported value that
    cannot be a real token count, catching a caller bug loud rather than
    silently caching garbage), `unknown_reporter_surface` (a surface outside
    the known classes — a typo'd or invented surface would quietly poison
    exactly the attribution these columns exist to provide, so it fails loud
    instead of being stored)."""
    if not agent_instance_id.strip():
        raise VerbError(
            "missing_argument", "report_context_status requires a non-empty agent_instance_id.",
        )
    if not claude_session_id.strip():
        raise VerbError(
            "missing_argument", "report_context_status requires a non-empty claude_session_id.",
        )
    if not model.strip():
        raise VerbError("missing_argument", "report_context_status requires a non-empty model.")
    if not measured_at.strip():
        raise VerbError(
            "missing_argument", "report_context_status requires a non-empty measured_at.",
        )
    if current_tokens < 0 or ceiling <= 0:
        raise VerbError(
            "negative_tokens",
            f"report_context_status got current_tokens={current_tokens!r}, ceiling={ceiling!r} "
            "— neither can be non-positive for a real measurement.",
        )
    if reporter_surface is not None and reporter_surface not in REPORTER_SURFACES:
        raise VerbError(
            "unknown_reporter_surface",
            f"report_context_status got reporter_surface={reporter_surface!r}, which is not one "
            f"of {sorted(REPORTER_SURFACES)}. Report 'unknown' when the hook cannot classify its "
            "own location — an invented surface silently poisons attribution.",
        )
    upsert_session_context_status(
        state,
        agent_instance_id=agent_instance_id,
        claude_session_id=claude_session_id,
        model=model,
        current_tokens=current_tokens,
        ceiling=ceiling,
        measured_at=measured_at,
        cache_read_tokens=cache_read_tokens,
        cache_cold=cache_cold,
        cache_overage_signature=cache_overage_signature,
        reporter_surface=reporter_surface,
        reporter_generation=reporter_generation,
        agent_session_id=agent_session_id,
    )
    return {"status": "recorded"}


def _tri_state(raw: object) -> bool | None:
    """Stored 1/0/NULL -> True/False/None. ``None`` means NOT REPORTED.

    Never coerces NULL to False. A reporter that never looked and a reporter
    that looked and found the cache live are different facts, and this is the
    boundary where collapsing them would become invisible to every caller.
    """
    return None if raw is None else bool(raw)


def _cache_view(row: dict[str, Any], current_tokens: int) -> dict[str, Any]:
    """Cache state plus the economic band it implies.

    The band is DERIVED at read time from the live policy constants, the same
    rule `fraction`/`rotation_due` already follow — a change to the bands must
    never require backfilling stored rows.

    When cache state was not reported the band is still computed, as the WARM
    band, and `cache_cold` is `null` so the caller can see the assumption
    rather than inherit it silently.
    """
    cold = _tri_state(row.get("cache_cold"))
    band, guidance = rotation_thresholds.rotation_band(
        current_tokens, cache_cold=bool(cold),
    )
    return {
        "cache_read_tokens": (
            None if row.get("cache_read_tokens") is None
            else int(row["cache_read_tokens"])
        ),
        "cache_cold": cold,
        "cache_overage_signature": _tri_state(row.get("cache_overage_signature")),
        "rotation_band": band,
        "rotation_guidance": guidance,
    }


def _routing_view(row: dict[str, Any]) -> dict[str, Any]:
    """How to REACH this session, as opposed to how to describe it.

    Extracted rather than inlined beside its neighbours, and rather than
    allowlisted: adding this field inline took `session_context_status` to
    cyclomatic complexity 11, one over the gate. The file already answers that
    shape with `_cache_view`/`_reporter_view`, so this is the existing idiom
    rather than a new one -- and an allowlist entry would have frozen the
    complexity at the ceiling and handed the wall to whoever edits this verb
    next, which is the failure mode the L4c extraction was made to avoid.

    NOT `str(... or "")` like its neighbours in the caller: that idiom maps
    NULL to `""` and would erase the NOT-REPORTED state this column exists to
    carry. `resolve_by_agent_session_id` returns None for an empty string, so
    the collapse is invisible in the outcome -- a reporter that never sent a
    session id would become indistinguishable from a session that genuinely
    could not be routed, which is precisely the discrimination the L4c counts
    depend on.
    """
    agent_session_id = row.get("agent_session_id")
    return {
        "agent_session_id": (
            None if agent_session_id is None else str(agent_session_id)
        ),
    }


def _reporter_view(row: dict[str, Any]) -> dict[str, Any]:
    """Which copy of the reporting hook wrote this row, on two independent axes.

    Both `null` means the tick was served by a reporter predating this
    widening — which is a POSITIVE finding, not missing data: only a stale
    copy can produce it. That matters because this table keeps one row per
    session and the latest write wins, so an absent cache field is otherwise
    ambiguous between "the verbs are not deployed" and "a stale copy served
    this tick". These two fields are what makes those distinguishable, and
    they are deliberately NOT folded into one value — a current-generation
    hook running from the wrong surface and a stale-generation hook running
    from the right one are different failures with different fixes.
    """
    generation = row.get("reporter_generation")
    surface = row.get("reporter_surface")
    return {
        "reporter_surface": None if surface is None else str(surface),
        "reporter_generation": None if generation is None else int(generation),
    }


def session_context_status(
    state: StateManagementInterface, *, agent_instance_id: str,
) -> dict[str, Any]:
    """Read the cached snapshot for `agent_instance_id`. `resolved=False`
    (never a raised `VerbError`) is the expected, stable shape for "no report
    has landed yet for this session" — e.g. a fresh session pre-first-tick,
    or (until the seat-wiring design note is acted on) any `host=operator`
    seat. Callers must treat `resolved=False` as a loud, honest gap, never
    estimate a number in its place (the standing repo rule against silently
    promoting an unknown into a fact).

    Cache fields carry three states, not two: ``true``/``false``/``null``,
    where ``null`` is NOT REPORTED. `cache_cold` reflects the reporter's
    classification, which EXCLUDES the first call after a `/clear` — that call
    is cold by construction, and counting it would make every rotation
    recommend another one. `rotation_band` applies the ratified economic
    policy; with cache state unreported it is the WARM band, and the `null`
    beside it is how you can tell.

    `reporter_surface`/`reporter_generation` say which copy of the reporting
    hook wrote this row. Read them BEFORE concluding anything from an absent
    cache field: several hook copies can be registered at once and only the
    latest write survives here, so `null` cache state next to a stale or
    absent reporter means "a stale copy served this tick", which is a
    different fact — and a different fix — from "the verbs are undeployed".

    `agent_session_id` is the session's stable id, for callers that need to
    REACH the session rather than merely describe it. `null` means the
    reporter predates the column, NOT that the session has no bridge. Those
    must not be collapsed: the first is a coverage gap that heals on the next
    deploy, the second would be a live routing failure worth paging someone
    about.
    """
    if not agent_instance_id.strip():
        raise VerbError(
            "missing_argument", "session_context_status requires a non-empty agent_instance_id.",
        )
    row = read_session_context_status(state, agent_instance_id)
    if row is None:
        return {
            "resolved": False,
            "resolution_error": (
                f"no session_context_status report on file for {agent_instance_id!r} — "
                "either this session has not completed a reporting tick yet, or "
                "(for host=operator sessions) the seat-wiring design note has not "
                "been acted on yet."
            ),
            "agent_instance_id": agent_instance_id,
            "claude_session_id": "",
            "model": "",
            "current_tokens": 0,
            "ceiling": 0,
            "fraction": 0.0,
            "per_prompt_carriage_estimate_tokens": 0,
            "rotation_due": False,
            "measured_at": "",
            "cache_read_tokens": None,
            "cache_cold": None,
            "cache_overage_signature": None,
            "rotation_band": None,
            "rotation_guidance": None,
            "reporter_surface": None,
            "reporter_generation": None,
            "agent_session_id": None,
        }
    current_tokens = int(row.get("current_tokens") or 0)
    ceiling = int(row.get("ceiling") or 0) or rotation_thresholds.DEFAULT_CONSERVATIVE_CEILING
    fraction = current_tokens / ceiling if ceiling else 0.0
    # Derived ONCE and reused for both the verdict and the reported band. The
    # two used to be computed independently — `rotation_due` from the fraction
    # here, the band inside `_cache_view` — which is how this verb came to
    # publish `rotation_due=False` beside `rotation_band=warm_immediate` on the
    # same row (GAU-08). Sharing the derivation makes the two fields agree by
    # construction rather than by both being edited together.
    cache_view = _cache_view(row, current_tokens)
    return {
        "resolved": True,
        "resolution_error": None,
        "agent_instance_id": agent_instance_id,
        "claude_session_id": str(row.get("claude_session_id") or ""),
        "model": str(row.get("model") or ""),
        "current_tokens": current_tokens,
        "ceiling": ceiling,
        "fraction": fraction,
        "per_prompt_carriage_estimate_tokens": round(current_tokens * CACHE_READ_COST_FRACTION),
        # THE UNION (GAU-08): an actionable economics band OR the fraction hint.
        # Decided against the STORED `ceiling` — the same denominator `fraction`
        # above is computed from and that this verb publishes — so the verdict
        # and the number a reader checks it against can never disagree.
        "rotation_due": rotation_thresholds.is_rotation_due_for_ceiling(
            ceiling=ceiling,
            current_tokens=current_tokens,
            cache_cold=cache_view["cache_cold"],
        ),
        "measured_at": str(row.get("measured_at") or ""),
        **_routing_view(row),
        **cache_view,
        **_reporter_view(row),
    }


__all__ = [
    "CACHE_READ_COST_FRACTION",
    "REPORTER_SURFACES",
    "REPORTER_SURFACE_CHECKOUT",
    "REPORTER_SURFACE_PLUGIN_CACHE",
    "REPORTER_SURFACE_RELEASE",
    "REPORTER_SURFACE_UNKNOWN",
    "REPORTER_SURFACE_VENDORED",
    "report_context_status",
    "session_context_status",
]
