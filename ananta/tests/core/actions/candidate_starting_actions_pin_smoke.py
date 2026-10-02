#!/usr/bin/env python3
"""Smoke: two colours over ONE action queue each claim only their own boot rows (iss_faf5802c).

Since the deploy runs detached from the poller (``unt_37b85986``), the old
colour keeps polling the shared queue while a candidate boots. Measured on the
2026-10-01 deploy of 6ebccd2de: the old colour claimed the candidate's
``start_interface`` (``bridge.already_running``), so the candidate never bound a
bridge and the swap ended ``register_timeout``; and the candidate, with no
bridge yet, claimed the live colour's ``peer_inbox`` (``bridge.not_running``).

This smoke runs two REAL ``ActionQueuePoller`` instances, one per colour, over
ONE shared in-memory ``action_events`` table that applies the state grammar's own
filter function. The fetch (``_get_queued_actions``), the conditional claim
(``_mark_action_processing``: id plus ``status='queued'``) and ``_poll_once`` are
the real ones; only the handler is replaced, to record which colour ran what.
Rows are written by the real writer pieces: ``EventOrchestrator._submit_starting_actions``,
``ActionFactory._preserve_action_metadata`` and ``ActionEventRecorder._build_action_data``.

Asserts:

  (1) the writer pins every starting action to this process's instance id, and
      the recorder stores the pin plus the writer's version shield in
      ``excluded_versions``;
  (2) the old colour, active and with a deploy in flight, does NOT claim the
      candidate's pinned starting actions, even when it polls first, and still
      claims unpinned live work;
  (3) the candidate claims its own pinned starting actions;
  (4) direction two: the candidate, restricted until the router names it
      active, does NOT claim unpinned live work, reads only pinned rows in
      SQL, and once released claims unpinned rows (its boot needs no unpinned
      row, so the restriction cannot deadlock it);
  (5) unpinned rows stay claimable by either colour, each exactly once;
  (6) same version, different instance: the pin decides, not ``SOLET_VERSION``;
      and a poller that predates pins (version check only) skips a pinned row
      because of the version shield;
  (7) a row pinned to an instance that never claims it is failed by the
      abandoned-pin reap once past the threshold; own, fresh and unpinned rows
      are left alone;
  (8) wiring: the orchestrator binds ``claims_own_rows_only`` into the poller.
      The deploy plugin's restrict/release is proved by
      ``plugins/macos_self_deployment_plugin/tests/claim_restriction_lifecycle_smoke.py``.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 ananta/tests/core/actions/candidate_starting_actions_pin_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ANANTA_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ANANTA_ROOT / "src"))

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_queue_poller import (  # noqa: E402
    ActionQueuePoller,
    QueuedAction,
)
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.orchestration.managers.action_event_recorder import (  # noqa: E402
    ActionEventRecorder,
)
from ananta.services.state_service.ordered_query import _filter_matches  # noqa: E402

START_INTERFACE = "plugin::agent_messaging_plugin::start_interface"
ENSURE_SCHEDULES = "service_interface::memory_service::ensure_schedules"
PEER_INBOX = "plugin::agent_messaging_plugin::peer_inbox"
DELIVER_RESULT = "plugin::agent_messaging_plugin::deliver_result"
APPLY = "service_interface::lifecycle_management_service::apply_manifest"

BLUE = "solet-blue-0fea479c/111-aaaaaaaa"
GREEN = "solet-green-7edf1bfc/222-bbbbbbbb"

_failures: list[str] = []


def _check(condition: object, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL {label}")


def _naive_utc(delta: timedelta = timedelta()) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - delta


class _SharedQueue:
    """One ``core__action_events`` table both colours read and claim from.

    Filters go through ``ordered_query._filter_matches``, the in-memory mirror
    of the SQL filter grammar, so a filter the real seam would reject or
    evaluate differently is not silently accepted here.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self._next = 0

    def insert(self, record: dict[str, object], *, age: timedelta = timedelta()) -> str:
        self._next += 1
        row = dict(record)
        row["id"] = f"ae-{self._next:03d}"
        row.setdefault("sequence", self._next)
        row.setdefault("status", "queued")
        stamp = _naive_utc(age)
        row["created_at"] = stamp
        row["updated_at"] = stamp
        raw = row.get("excluded_versions")
        # JSONB round trip: the recorder writes a JSON string, Postgres hands
        # back a list.
        row["excluded_versions"] = json.loads(raw) if isinstance(raw, str) else raw
        self.rows.append(row)
        return str(row["id"])

    def _matching(self, filters: dict[str, object]) -> list[dict[str, object]]:
        return [
            row for row in self.rows
            if all(_filter_matches(row.get(col), spec) for col, spec in filters.items())
        ]

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        assert namespace == "core" and data["table"] == "action_events"
        filters = data["filters"]
        assert isinstance(filters, dict)
        self.last_filters = dict(filters)
        rows = self._matching(filters)
        order = data.get("order_by") or []
        assert isinstance(order, list)
        for column, _direction in reversed(order):
            rows.sort(key=lambda r, c=column: r.get(c))  # type: ignore[misc]
        limit = data.get("limit")
        if isinstance(limit, int):
            rows = rows[:limit]
        return {"action_status": "completed", "data": {"records": [dict(r) for r in rows]}}

    def query_state(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        _ = (namespace, data)
        return {"action_status": "completed", "data": {"records": []}}

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> dict[str, object]:
        assert namespace == "core" and query["table"] == "action_events"
        filters = query["filters"]
        assert isinstance(filters, dict)
        hit = self._matching(filters)
        for row in hit:
            row.update(updates)
        return {"action_status": "completed", "data": {"result": {"updated": len(hit)}}}

    def status(self, action_id: str) -> object:
        return next(r["status"] for r in self.rows if r["id"] == action_id)


class _Colour:
    """A REAL poller for one colour; only the handler is a recorder."""

    def __init__(self, name: str, instance_id: str, queue: _SharedQueue) -> None:
        self.name = name
        self.ran: list[str] = []
        self.release_deploy = asyncio.Event()
        self.own_rows_only = False
        poller = ActionQueuePoller.__new__(ActionQueuePoller)
        poller.state_service = queue  # type: ignore[assignment]
        poller.max_actions_per_poll = 10
        poller._solet_version = "local"
        poller._claim_instance_id = instance_id
        poller._last_observed_queue_depth = 0
        poller.total_actions_processed = 0
        poller.total_poll_cycles = 0
        # Assigned, not set through the setter, so the base tree (which has no
        # setter) still runs this harness and fails on behaviour.
        poller._own_rows_only_getter = lambda: self.own_rows_only

        async def handler(action: QueuedAction) -> None:
            if action.process_key == APPLY:
                await self.release_deploy.wait()
            self.ran.append(action.id)

        poller._process_action = handler  # type: ignore[method-assign]
        self.poller = poller

    async def poll(self) -> None:
        await asyncio.wait_for(self.poller._poll_once(), 2.0)
        await asyncio.sleep(0)


def _write_starting_actions(queue: _SharedQueue, process_keys: list[str]) -> list[str]:
    """Submit starting actions through the real writer pieces into ``queue``."""
    written: list[str] = []
    factory = ActionFactory.__new__(ActionFactory)
    recorder = ActionEventRecorder.__new__(ActionEventRecorder)

    def submit(action_def: dict[str, object], context: dict[str, object]) -> str:
        _ = context
        action: dict[str, object] = {
            "process_key": action_def["process_key"],
            "parameters": {},
            "notes": "starting action",
        }
        factory._preserve_action_metadata(action, action_def)
        record = recorder._build_action_data(
            action, 0, 0, str(action_def["name"]), "flow-boot", "sess-boot", None,
        )
        action_id = queue.insert(record)
        written.append(action_id)
        return action_id

    orch = EventOrchestrator.__new__(EventOrchestrator)
    orch.action_factory = SimpleNamespace(  # type: ignore[assignment]
        update_process_registry=lambda _registry: None,
        submit_action_definition=submit,
    )
    orch._process_registry = {}
    orch.current_flow_id = "flow-boot"
    orch.current_session_id = "sess-boot"
    orch._submit_starting_actions(
        [{"name": f"boot_{i}", "process_key": key} for i, key in enumerate(process_keys)], {},
    )
    return written


def _writer_pin(queue: _SharedQueue, action_id: str) -> str:
    """The instance the writer pinned a row to; ``GREEN`` when it pinned none."""
    entries = next(r for r in queue.rows if r["id"] == action_id).get("excluded_versions")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str) and entry.startswith("instance:"):
            return entry.removeprefix("instance:")
    return GREEN


def _unpinned(queue: _SharedQueue, process_key: str, *, age: timedelta = timedelta()) -> str:
    return queue.insert(
        {"process_key": process_key, "parameters": "{}", "notes": "live work"}, age=age,
    )


def _pinned(queue: _SharedQueue, process_key: str, instance_id: str, *, age: timedelta) -> str:
    return queue.insert(
        {
            "process_key": process_key,
            "parameters": "{}",
            "notes": "boot work",
            "excluded_versions": json.dumps(["local", f"instance:{instance_id}"]),
        },
        age=age,
    )


def test_writer_pins_starting_actions() -> None:
    print("\n[1] every starting action is pinned to this process, with the version shield")
    from ananta.core.actions import instance_pin

    queue = _SharedQueue()
    ids = _write_starting_actions(queue, [START_INTERFACE, ENSURE_SCHEDULES])
    me = instance_pin.process_instance_id()
    for action_id in ids:
        row = next(r for r in queue.rows if r["id"] == action_id)
        entries = row["excluded_versions"]
        _check(
            entries == [instance_pin.solet_version(), f"instance:{me}"],
            f"{row['process_key']} stored excluded_versions={entries!r}",
        )
    _check(len(ids) == 2, "both starting actions were written")


def test_old_colour_leaves_candidate_rows() -> None:
    print("\n[2]+[3]+[4] old colour mid-deploy vs booting candidate, one shared queue")

    async def scenario() -> None:
        queue = _SharedQueue()
        blue = _Colour("blue", BLUE, queue)

        deploy = _unpinned(queue, APPLY)
        await blue.poll()
        _check(
            blue.poller._running_deploy() is not None,
            "blue claimed the deploy and it is in flight, detached",
        )
        # Green boots: its starting actions go through the real writer, so the
        # green poller's identity is whatever that writer pinned them to.
        boot = _write_starting_actions(queue, [START_INTERFACE, ENSURE_SCHEDULES])
        green = _Colour("green", _writer_pin(queue, boot[0]), queue)
        green.own_rows_only = True  # router has not named green active yet
        live = _unpinned(queue, PEER_INBOX)

        await blue.poll()  # the old colour polls FIRST
        _check(
            all(queue.status(a) == "queued" for a in boot),
            "blue, active with a deploy in flight, left green's pinned rows queued",
        )
        _check(live in blue.ran, "control: blue still claimed the unpinned live work")

        live2 = _unpinned(queue, DELIVER_RESULT)
        # An unpinned row that carries a version exclusion for another version
        # does reach a restricted poller's narrowed read; the claim filter must
        # still refuse it.
        targeted = queue.insert(
            {
                "process_key": DELIVER_RESULT,
                "parameters": "{}",
                "notes": "version-targeted live work",
                "excluded_versions": json.dumps(["v-other"]),
            },
        )
        await green.poll()
        _check(green.ran == boot, f"green claimed exactly its own pinned rows ({green.ran})")
        _check(
            queue.status(live2) == "queued",
            "green, not yet router-active, left the live colour's deliver_result queued",
        )
        _check(
            queue.status(targeted) == "queued",
            "green, not yet router-active, left an unpinned version-targeted row queued",
        )
        _check(
            queue.last_filters.get("excluded_versions") == {"op": "is_not_null"},
            "a restricted poller reads only pinned rows in SQL",
        )

        green.own_rows_only = False  # the router named green active
        await green.poll()
        _check(
            live2 in green.ran and targeted in green.ran,
            "released, green claims unpinned rows (its boot needed none of them)",
        )
        blue.release_deploy.set()
        await asyncio.sleep(0)
        _check(deploy in blue.ran, "the deploy then completed on blue")

    asyncio.run(scenario())


def test_unpinned_rows_claimable_by_either() -> None:
    print("\n[5] unpinned rows are claimable by either colour, each exactly once")

    async def scenario() -> None:
        queue = _SharedQueue()
        blue = _Colour("blue", BLUE, queue)
        green = _Colour("green", GREEN, queue)
        first = [_unpinned(queue, PEER_INBOX) for _ in range(3)]
        await green.poll()
        _check(green.ran == first, "green (unrestricted) claimed unpinned rows")
        second = [_unpinned(queue, PEER_INBOX) for _ in range(3)]
        await blue.poll()
        await green.poll()
        _check(blue.ran == second and len(green.ran) == 3, "blue claimed the next ones")
        _check(
            all(queue.status(a) == "processing" for a in first + second),
            "every row claimed exactly once",
        )

    asyncio.run(scenario())


def test_pin_not_version_decides() -> None:
    print("\n[6] the pin decides between same-version colours; pre-pin pollers are shielded")
    queue = _SharedQueue()
    blue = _Colour("blue", BLUE, queue)
    green = _Colour("green", GREEN, queue)
    row = {"excluded_versions": ["local", f"instance:{GREEN}"]}
    _check(
        blue.poller._solet_version == green.poller._solet_version,
        "both colours run the same SOLET_VERSION",
    )
    _check(not blue.poller._claimable(row), "blue may not claim green's pinned row")
    _check(green.poller._claimable(row), "green may claim it despite its own version entry")
    _check(
        blue.poller._version_excluded(row["excluded_versions"]),
        "a poller that predates pins (version check only) skips the pinned row",
    )
    _check(
        blue.poller._claimable({"excluded_versions": None}),
        "control: an unpinned row with no exclusion is claimable",
    )


def test_abandoned_pins_are_reaped() -> None:
    print("\n[7] a pinned row its instance never claims is failed after the threshold")
    from ananta.core.actions.orphan_reaper import reap_abandoned_pinned_rows

    queue = _SharedQueue()
    dead = _pinned(queue, START_INTERFACE, "solet-green-deadbeef/9-cccccccc", age=timedelta(hours=2))
    own = _pinned(queue, START_INTERFACE, BLUE, age=timedelta(hours=2))
    fresh = _pinned(queue, START_INTERFACE, GREEN, age=timedelta(minutes=1))
    old_unpinned = _unpinned(queue, PEER_INBOX, age=timedelta(hours=2))
    failed = reap_abandoned_pinned_rows(queue, own_instance_id=BLUE)  # type: ignore[arg-type]
    _check(failed == 1 and queue.status(dead) == "failed", "the dead instance's row was failed")
    _check(queue.status(own) == "queued", "a row pinned to the reaping instance is left alone")
    _check(queue.status(fresh) == "queued", "a fresh row pinned to a live instance is left alone")
    _check(queue.status(old_unpinned) == "queued", "an unpinned row is not this reap's business")
    error = next(r for r in queue.rows if r["id"] == dead).get("error_message")
    _check(
        isinstance(error, str) and "deadbeef" in error,
        "the failure names the instance the row waited for",
    )
    blue = _Colour("blue", BLUE, queue)
    second = _pinned(queue, START_INTERFACE, "solet-blue-gone/8-dddddddd", age=timedelta(hours=3))
    blue.poller._maybe_reap_orphans()
    _check(queue.status(second) == "failed", "the poller's periodic reap runs the pinned-row reap")


def test_orchestrator_wiring() -> None:
    print("\n[8] the orchestrator binds claims_own_rows_only into the poller")
    orch = EventOrchestrator.__new__(EventOrchestrator)
    poller = ActionQueuePoller.__new__(ActionQueuePoller)
    orch.is_active_color = True
    orch.claims_own_rows_only = False
    orch.action_coordinator = SimpleNamespace(  # type: ignore[assignment]
        action_manager=object(),
        action_factory=object(),
        event_processor=object(),
        action_preparation_service=None,
        action_queue_poller=poller,
        _process_registry_manager=None,
        _process_registry={},
        _framework_services_initialized=True,
        _plugins_ready=True,
    )
    orch._delegate_action_attributes()
    orch.claims_own_rows_only = True
    _check(poller._claims_own_rows_only(), "the poller reads the orchestrator's flag (on)")
    orch.claims_own_rows_only = False
    _check(not poller._claims_own_rows_only(), "the poller reads the orchestrator's flag (off)")


def _run(test: Callable[[], None]) -> None:
    """Run one leg; a leg that raises is a named failure and the rest still run."""
    try:
        test()
    except Exception as exc:  # noqa: BLE001 — report every leg, then exit 1
        _check(False, f"{test.__name__} raised {type(exc).__name__}: {exc}")


def main() -> int:
    print("Candidate starting-actions pin smoke (iss_faf5802c)")
    for test in (
        test_writer_pins_starting_actions,
        test_old_colour_leaves_candidate_rows,
        test_unpinned_rows_claimable_by_either,
        test_pin_not_version_decides,
        test_abandoned_pins_are_reaped,
        test_orchestrator_wiring,
    ):
        _run(test)
    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: each colour claims only its own boot rows; unpinned work is shared as before")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
