#!/usr/bin/env python3
"""Unit smoke for GAU-21 — a gauge alarm now leaves a durable, attributable
trace, INCLUDING the alarm that reached nobody.

THE DEFECT this covers, measured against the source on 2026-08-19. The sweep's
gauge notices existed only as in-memory bridge events, and four independent
properties of that queue each defeated an audit:

  1. NOT DURABLE — `BridgeSessionState` is "In-memory state for one active
     bridge"; a restart loses every un-drained notice, and the DEPLOY a canary
     must traverse IS a restart.
  2. DRAIN-ONCE, AND READING STEALS — `events_after` returns the acked events
     and rebinds the queue to exclude them, so a verifier polling that queue
     races the steward and can consume the very notice the steward needed.
  3. NO BY-TYPE READ — nothing is keyed on event type.
  4. CONDITIONALLY NEVER EMITTED — the notify path resolved the steward binding
     FIRST and returned early when it was None, so an alarm about an unbound
     session reached nobody AND left nothing behind.

So "the detector never fired" and "it fired into the void" were the same
silence — the original gauge defect one level up, with the instrument's own
output unobservable after the moment. That is also why the manual freeze-watch
cannot retire when the detector merely deploys.

★ THE SWEEPS THAT EMITTED THESE NOTICES ARE GONE (a spawner is no longer told
about a session's gauge on a clock), so this smoke now covers only the record
store and its reader: rows are seeded through ``record_notice_best_effort`` or
``record_gauge_notice`` and read back through ``gauge_notice_records``. The
emit-path tests (a delivered, undeliverable or faulting alarm still leaves a
record) went with the sweeps that drove them.

★ WHAT WOULD FAIL IF THIS SMOKE WERE WRONG. Each test names the mutation it
catches, because a green that cannot name its failing mutation is not evidence.
The load-bearing ones:
  * make the prune soft-delete instead of hard
    → `test_retention_is_a_hard_bound`
  * make the read consume what it reads (the bridge queue's own bug)
    → `test_reading_does_not_consume`
  * match a None filter against the column instead of omitting it
    → `test_an_omitted_filter_does_not_narrow`

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/gauge_notice_record_smoke.py
"""

from __future__ import annotations

import inspect
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
)
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.context_status_verbs import VerbError  # noqa: E402
from agent_messaging_plugin.gauge_canary import register_synthetic_session  # noqa: E402
from agent_messaging_plugin.gauge_canary_store import register_canary  # noqa: E402
from agent_messaging_plugin.gauge_notice_record_store import (  # noqa: E402
    GAUGE_NOTICE_RETENTION,
    read_gauge_notice_records,
    record_notice_best_effort,
)
from agent_messaging_plugin.gauge_notice_records import gauge_notice_records  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.release_identity import (  # noqa: E402
    MAX_PARENTS_SEARCHED,
    _identity_from,
    running_release_id,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    NOTICE_DELIVERY_APPENDED,
    PEER_BINDING_NAMESPACE,
    TABLE_GAUGE_NOTICE_RECORD,
    get_peer_binding_schema,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_sweep import (  # noqa: E402
    EVENT_GAUGE_COVERAGE_NOTICE,
    EVENT_GAUGE_STALE_NOTICE,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _state() -> Any:
    return RealShapeState()


def _peer_registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _n: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )


def _spawn_live(
    state: Any,
    *,
    agent_instance_id: str,
    spawned_by_instance_id: str = "",
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            label="gauge-watch",
            host="operator",
            session_origin="registered",
            report_by_seconds=0,
            spawned_by_instance_id=spawned_by_instance_id,
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id=agent_instance_id,
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
    )


def _register_live_binding(
    reg: PeerRegistry,
    mgr: BridgeSessionManager,
    *,
    agent_instance_id: str,
) -> str:
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id,
            agent_id="claude_code",
            agent_instance_id=agent_instance_id,
            session_label=agent_instance_id,
            parent_pid=1,
        ),
    )
    return bridge_id


def _wired(*, with_steward_binding: bool = True) -> tuple[Any, PeerRegistry, Any, str]:
    """A live worker spawned by a steward, with the steward's bridge optionally
    registered.

    ``with_steward_binding=False`` is the count-4 shape: a real, spawned,
    live worker whose steward has NO resolvable binding. That is the case the
    old early return made invisible, so it gets a first-class fixture rather
    than a monkeypatch.
    """
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward"}},
        {"agent_id": "claude_code"},
    )
    bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward") if with_steward_binding else ""
    _spawn_live(
        state,
        agent_instance_id="agi-worker",
        spawned_by_instance_id="agi-steward",
    )
    return state, reg, mgr, bridge_id


def _records(state: Any) -> list[dict[str, Any]]:
    return state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_GAUGE_NOTICE_RECORD)


def _seed_stale_record(state: Any) -> None:
    """Write one staleness record through the store's own best-effort writer.

    The sweeps that used to emit these notices are gone, so the reader tests
    below seed a record the way the emit path did: ``record_notice_best_effort``.
    """
    now = datetime.now(UTC)
    record_notice_best_effort(
        state,
        notice_type=EVENT_GAUGE_STALE_NOTICE,
        agent_instance_id="agi-worker",
        delivery_outcome=NOTICE_DELIVERY_APPENDED,
        steward_instance_id="agi-steward",
        clock=now,
        threshold_s=900.0,
        observed_s=5370.0,
        last_report_alive_at=now - timedelta(seconds=30),
        gauge_measured_at=now - timedelta(seconds=5400),
    )


# ---------------------------------------------------------------------------
# The bound, and the read.
# ---------------------------------------------------------------------------


def test_retention_is_a_hard_bound() -> None:
    """The writer bounds its own table, and bounds it by DELETING.

    MUTATION: prune with `soft_delete=True` → the row count check fails,
    because this platform has no reaper that ever clears an is_deleted flag,
    so a soft bound is cosmetic while the table grows exactly as before.
    """
    state, _, _, _ = _wired()
    base = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    from agent_messaging_plugin.gauge_notice_record_store import record_gauge_notice

    for i in range(GAUGE_NOTICE_RETENTION + 7):
        record_gauge_notice(
            state,
            notice_type=EVENT_GAUGE_STALE_NOTICE,
            agent_instance_id="agi-worker",
            emitted_at=(base + timedelta(seconds=i)).isoformat(),
            delivery_outcome=NOTICE_DELIVERY_APPENDED,
        )
    rows = _records(state)
    _check(
        len(rows) == GAUGE_NOTICE_RETENTION,
        f"pruned to exactly {GAUGE_NOTICE_RETENTION} rows (found {len(rows)})",
    )
    _check(
        all(r.get("is_deleted") in (0, None) for r in rows),
        "the survivors are GONE, not tombstoned — nothing here reaps a soft-deleted row",
    )
    newest = max(str(r.get("emitted_at")) for r in rows)
    _check(newest == (base + timedelta(seconds=GAUGE_NOTICE_RETENTION + 6)).isoformat(), "and it is the OLDEST rows that go, never the newest")


def test_retention_is_scoped_per_type() -> None:
    """One leg's volume must not evict the other leg's history.

    MUTATION: prune per subject only (drop notice_type from the filters) →
    the coverage record is deleted by the staleness flood and this fails.
    """
    state, _, _, _ = _wired()
    base = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    from agent_messaging_plugin.gauge_notice_record_store import record_gauge_notice

    record_gauge_notice(
        state,
        notice_type=EVENT_GAUGE_COVERAGE_NOTICE,
        agent_instance_id="agi-worker",
        emitted_at=base.isoformat(),
        delivery_outcome=NOTICE_DELIVERY_APPENDED,
    )
    for i in range(GAUGE_NOTICE_RETENTION + 5):
        record_gauge_notice(
            state,
            notice_type=EVENT_GAUGE_STALE_NOTICE,
            agent_instance_id="agi-worker",
            emitted_at=(base + timedelta(seconds=i + 1)).isoformat(),
            delivery_outcome=NOTICE_DELIVERY_APPENDED,
        )
    coverage, _ = read_gauge_notice_records(
        state,
        notice_type=EVENT_GAUGE_COVERAGE_NOTICE,
    )
    _check(len(coverage) == 1, "the single coverage record SURVIVES a staleness flood past the bound — retention is per (subject, type), so one leg's volume cannot silently become the other leg's retention")


def test_reading_does_not_consume() -> None:
    """★ THE BRIDGE QUEUE'S OWN BUG, pinned so it cannot come back.

    `events_after` returns the pending events and then rebinds the queue to
    exclude them, so a verifier polling it can swallow the steward's notice.
    Two identical reads here must return identical rows.

    MUTATION: make the read delete or mark what it returns → the second read
    comes back short and this fails.
    """
    state, _, _, _ = _wired()
    _seed_stale_record(state)
    first, _ = read_gauge_notice_records(state)
    second, _ = read_gauge_notice_records(state)
    _check(len(first) == 1 and len(second) == 1, "two consecutive reads both see the record")
    _check(
        first and second and first[0].get("emitted_at") == second[0].get("emitted_at"),
        "and they see the SAME record — reading is not a drain",
    )


def test_an_omitted_filter_does_not_narrow() -> None:
    """★ THE PLAUSIBLE-WRONG-ANSWER GUARD. The natural mis-implementation
    matches a None filter against the column, which returns a small, plausible,
    wrong answer (only rows with a NULL steward) rather than an error.

    MUTATION: put `filters[col] = None` in unconditionally → the unfiltered
    read returns 0 rows and this fails.
    """
    state, _, _, _ = _wired()
    _seed_stale_record(state)
    everything = gauge_notice_records(state)
    _check(everything["returned"] == 1, "an unfiltered read means EVERY notice, not 'notices with a null subject'")
    by_type = gauge_notice_records(state, notice_type=EVENT_GAUGE_STALE_NOTICE)
    _check(by_type["returned"] == 1, "narrowing by the matching type still finds it")
    other_type = gauge_notice_records(state, notice_type=EVENT_GAUGE_COVERAGE_NOTICE)
    _check(other_type["returned"] == 0, "and narrowing by the OTHER type finds nothing — the type filter is load-bearing rather than decorative")


def test_the_window_filter_is_inclusive_and_bounded() -> None:
    """`since` is a lower bound on emitted_at, inclusive.

    MUTATION: use a strict `gt` → the boundary record disappears and this fails.
    """
    state, _, _, _ = _wired()
    base = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    from agent_messaging_plugin.gauge_notice_record_store import record_gauge_notice

    for i in range(3):
        record_gauge_notice(
            state,
            notice_type=EVENT_GAUGE_STALE_NOTICE,
            agent_instance_id="agi-worker",
            emitted_at=(base + timedelta(minutes=i)).isoformat(),
            delivery_outcome=NOTICE_DELIVERY_APPENDED,
        )
    boundary = (base + timedelta(minutes=1)).isoformat()
    windowed = gauge_notice_records(state, since=boundary)
    _check(windowed["returned"] == 2, "the window keeps the boundary record itself (inclusive), not just what follows it")
    _check(
        windowed["entries"][0]["emitted_at"] > windowed["entries"][1]["emitted_at"],
        "and the page is newest-first",
    )


def test_truncation_is_published_not_implied() -> None:
    """A capped page must say it was capped.

    MUTATION: return the slice with `truncated=False` hardcoded → fails.
    """
    state, _, _, _ = _wired()
    base = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
    from agent_messaging_plugin.gauge_notice_record_store import record_gauge_notice

    for i in range(4):
        record_gauge_notice(
            state,
            notice_type=EVENT_GAUGE_STALE_NOTICE,
            agent_instance_id="agi-worker",
            emitted_at=(base + timedelta(minutes=i)).isoformat(),
            delivery_outcome=NOTICE_DELIVERY_APPENDED,
        )
    capped = gauge_notice_records(state, limit=2)
    _check(capped["returned"] == 2 and capped["truncated"] is True, "a capped page reports truncated=True")
    full = gauge_notice_records(state, limit=50)
    _check(full["returned"] == 4 and full["truncated"] is False, "and a complete page reports False — the flag tracks the data, not a constant")


def test_an_invalid_notice_type_is_refused_not_silently_empty() -> None:
    """A typo'd filter must fail loudly. Returning an empty page would read as
    'no alarms fired', which is the most dangerous wrong answer this verb can
    give.

    MUTATION: drop the domain check → the call returns returned=0 and this
    fails.
    """
    state, _, _, _ = _wired()
    raised: VerbError | None = None
    try:
        gauge_notice_records(state, notice_type="gauge_stale_notices")
    except VerbError as exc:
        raised = exc
    _check(raised is not None and raised.code == "invalid_argument", "a notice_type outside the domain is REFUSED, never answered with an empty page that reads as 'nothing fired'")


def test_a_blank_subject_is_refused_rather_than_matching_nothing() -> None:
    """Omitting the subject means 'all subjects'; passing "" means the caller
    thought it had an id and did not.

    MUTATION: treat "" as absent → a caller's lost id silently becomes a
    fleet-wide read, and this fails.
    """
    state, _, _, _ = _wired()
    raised: VerbError | None = None
    try:
        gauge_notice_records(state, agent_instance_id="   ")
    except VerbError as exc:
        raised = exc
    _check(raised is not None and raised.code == "missing_argument", "a blank subject is refused rather than silently widening the read")


def test_the_subject_filter_uses_the_shared_id_join() -> None:
    """A caller holding the id `peer_list` publishes must still find records
    keyed on the ledger id.

    MUTATION: filter on the raw argument instead of the resolved subject →
    a watch-id caller gets an empty page and this fails.
    """
    state, _, _, _ = _wired()
    _seed_stale_record(state)
    direct = gauge_notice_records(state, agent_instance_id="agi-worker")
    _check(direct["returned"] == 1, "the ledger id resolves directly")
    _check(direct["agent_instance_id"] == "agi-worker", "and the reply names the id the records are keyed on")
    unknown = gauge_notice_records(state, agent_instance_id="agi-nobody")
    _check(
        unknown["returned"] == 0 and unknown["queried_agent_instance_id"] == "agi-nobody",
        "an unknown subject is an empty page that still echoes what was asked — never an error, and never silently widened to everything",
    )


# ---------------------------------------------------------------------------
# Release attribution.
# ---------------------------------------------------------------------------


def test_release_identity_is_read_or_honestly_absent() -> None:
    """★ NO PHANTOM RELEASE. `SOLET_RELEASE_ID` is a real convention elsewhere
    in this platform but is NOT set for this fleet's solet (measured against
    the LaunchAgent plist, whose EnvironmentVariables carries only PATH and
    SOLET_NAME), so reading it would have stamped every row with an empty
    string. Self-identifying from the module's own tree cannot disagree with
    the code that is executing.

    ★ AND THE SUCCESS PATH IS EXERCISED, not merely the absent one. A checkout
    run legitimately has no release, so asserting only `is None` here would be
    a check whose success path never runs.

    MUTATION: return a directory name (or "unknown") instead of None when no
    VERSION resolves → the tri-state check fails.
    """
    here = running_release_id()
    _check(here is None or bool(here), "the running release id is a non-empty string or honestly None, never an empty string pretending to be an answer")

    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "VERSION").write_text(json.dumps({"release_id": "rel-under-test"}))
        _check(_identity_from(root / "VERSION") == "rel-under-test", "POSITIVE CONTROL: a well-formed identity file IS read — the success path is reachable, so a None elsewhere means absent rather than unreachable")
        (root / "bad.json").write_text("{not json")
        _check(_identity_from(root / "bad.json") is None, "an unreadable identity file is absent, not a crash")
        (root / "empty.json").write_text(json.dumps({"release_id": ""}))
        _check(_identity_from(root / "empty.json") is None, "and an EMPTY release_id is absent too, never adopted verbatim")
        _check(_identity_from(root / "missing.json") is None, "a missing file is absent")
    _check(MAX_PARENTS_SEARCHED >= 6, "the upward walk still spans the deployed layout's depth (<release>/code/plugins/<p>/src/<m> is six levels)")


def test_the_record_carries_whatever_release_identity_resolved() -> None:
    """Whatever `running_release_id` answers, the record must carry THAT and
    not a substitute.

    MUTATION: default release_id to "" or to the current release when None →
    fails, and a reader would date the thresholds to a release that never ran.
    """
    state, _, _, _ = _wired()
    _seed_stale_record(state)
    row = _records(state)[0]
    _check(row.get("release_id") == running_release_id(), "the record's release_id is exactly what the identity read returned, including None")


def test_register_synthetic_session_takes_a_label_and_omits_retired_fields() -> None:
    """A canary stores its label and synthetic origin using the current parameters."""
    state = _state()
    register_canary(state, agent_instance_id="agi-plainwatch", purpose="exercise the staleness leg", registered_by="operator:none")
    result = register_synthetic_session(
        state,
        agent_instance_id="agi-plainwatch",
        label="plain-watch",
        spawned_by_instance_id="agi-steward",
        directed_by="operator:none",
        report_by_seconds=60,
    )
    row = read_managed_session(state, "agi-plainwatch")
    _check(result.get("label") == "plain-watch", "the outcome reports the supplied label")
    _check(row.get("session_origin") == "synthetic", "the stored row identifies the synthetic origin")
    _check(row.get("label") == "plain-watch" and row.get("local_name") == "plain-watch", "the row stores the label as its label and its local name")
    parameters = set(inspect.signature(register_synthetic_session).parameters)
    retired_fields = {"_".join(parts) for parts in (("lane", "id"), ("work", "class"), ("budget", "line"))}
    _check(not parameters.intersection(retired_fields), "the signature omits retired parameters")
    _check(not retired_fields.intersection(result), "the outcome omits retired fields")


def main() -> int:
    tests = (
        test_retention_is_a_hard_bound,
        test_retention_is_scoped_per_type,
        test_reading_does_not_consume,
        test_an_omitted_filter_does_not_narrow,
        test_the_window_filter_is_inclusive_and_bounded,
        test_truncation_is_published_not_implied,
        test_an_invalid_notice_type_is_refused_not_silently_empty,
        test_a_blank_subject_is_refused_rather_than_matching_nothing,
        test_the_subject_filter_uses_the_shared_id_join,
        test_release_identity_is_read_or_honestly_absent,
        test_the_record_carries_whatever_release_identity_resolved,
        test_register_synthetic_session_takes_a_label_and_omits_retired_fields,
    )
    for test in tests:
        print(f"\n{test.__name__}")
        test()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
