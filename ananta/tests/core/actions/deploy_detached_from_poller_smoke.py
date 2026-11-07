#!/usr/bin/env python3
"""Smoke: a blue-green deploy must not hold the serial action poller (wgr_5de9baf3).

``apply_manifest`` -> restart -> swap ran inline in ``ActionQueuePoller._poll_once``
(measured ``SLOW_ACTION`` 525 s to 5345 s), so every queued action waited behind
it. The fix starts a deploy row as a background task after the normal claim, with
the unchanged dispatch semantics. This smoke drives the REAL ``_poll_once`` with a
deploy-shaped handler (blocks until released, standing in for build + swap)
queued AHEAD of fast actions, replacing only the I/O seams.

Asserts:

  (1) the fast actions behind a deploy dispatch while the deploy is still in
      flight, and ``_poll_once`` returns instead of awaiting it;
  (2) a deploy-shaped action that runs longer than the slow threshold logs no
      ``SLOW_ACTION`` (the loop was not held) and logs ``DEPLOY_DETACHED`` when it
      finishes, while a slow NON-deploy action still logs ``SLOW_ACTION``;
  (3) exactly one deploy in flight: a second deploy row is not claimed while the
      first runs, and is claimed and run on a later cycle after it finishes;
  (4) a deploy that raises still marks its row failed (same failure semantics),
      and the actions behind it are unaffected;
  (5) the orphan reap pass run while the deploy is in flight does not fail the
      deploy's own row, and still fails a stale row that is not live (control);
  (6) a hung deploy stays visible: /health liveness names it in flight with its
      age, past the stall threshold it is stalled (action_path_stalled) and
      ``DEPLOY_STALLED`` is logged at ERROR once per interval;
  (7) a deploy row held back behind the in-flight one is logged once;
  (8) the reaper exemption has a ceiling: past it the deploy's row is failed;
 (9) held-back deploy rows take no dispatch slot (12 queued deploys do not
      starve the one fast row behind them), through the real fetch;
 (10) ``stop()`` cancels the detached deploy as well as the poll task;
 (11) a colour gated off mid-batch claims nothing more (no self-claimed
      ``complete_swap``);
 (12) every detached process key is a REAL shipped process definition: the
      macOS plugin is a bound ServiceProvider, so a ``plugin::`` key for its
      verbs never matches a dispatched row and would silently detach nothing.

The measured embedding/search slot also frees messaging in a fetched batch.
Its FIFO barrier lasts one cycle; uncertain rows retain protection separately
from admission. Stop drains selected work; explicit external cancellation retains uncertainty protection.
Busy reads scan one <=100-row page per poll; queued depth counts eligible page
rows, not total queue size. Mixed pages retain their boundary until drained.
Finite selected backlogs progress; infinite lower-key insertion and ordinary
or pin-only saturation have no bound. Protected ids alone use the idle query.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 ananta/tests/core/actions/deploy_detached_from_poller_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions import action_path_liveness as liveness_module  # noqa: E402
from ananta.core.actions import action_queue_poller as poller_module  # noqa: E402
from ananta.core.actions.action_path_liveness import ACTION_PATH_LIVENESS  # noqa: E402
from ananta.core.actions.action_queue_poller import (  # noqa: E402
    ActionQueuePoller,
    QueuedAction,
)
from ananta.services.state_service.ordered_query import (  # noqa: E402
    apply_ordered_query_in_memory,
    parse_ordered_query,
)
from candidate_starting_actions_pin_smoke import _SharedQueue  # noqa: E402

APPLY = "service_interface::lifecycle_management_service::apply_manifest"
RESTART = "service_interface::self_deployment_service::restart_with_manifest"
FAST = "plugin::agent_messaging_plugin::peer_inbox"
MEASURED = (
    "service_interface::embedding_service::generate_embeddings",
    "service_interface::knowledge_service::search",
)
INGEST = "service_interface::session_ledger_service::ingest_raw_chunk"
SLOW_NON_DEPLOY = "service_interface::knowledge_service::audit_retrieval_corpus"

_failures: list[str] = []


def _check(condition: object, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL {label}")


def _action(action_id: str, process_key: str) -> QueuedAction:
    return QueuedAction(
        id=action_id,
        process_key=process_key,
        parameters="{}",
        notes="",
        created_at="",
    )


class _Harness:
    """A real ``ActionQueuePoller`` with only its I/O seams replaced."""

    def __init__(self, queue: list[QueuedAction], handler: Any) -> None:
        self.queue = queue
        self.claimed: list[str] = []
        self.failed: list[str] = []
        poller = ActionQueuePoller.__new__(ActionQueuePoller)

        async def get_queued() -> list[QueuedAction]:
            return list(self.queue)

        def claim(action_id: str) -> bool:
            self.claimed.append(action_id)
            self.queue = [a for a in self.queue if a.id != action_id]
            return True

        poller._get_queued_actions = get_queued  # type: ignore[method-assign]
        poller._mark_action_processing = claim  # type: ignore[method-assign]
        poller._process_action = handler  # type: ignore[method-assign]
        poller._mark_action_failed = (  # type: ignore[method-assign]
            lambda action_id, _msg, error_detail=None: self.failed.append(action_id)
        )
        poller.total_actions_processed = 0
        poller.total_poll_cycles = 0
        poller._last_observed_queue_depth = len(queue)
        self.poller = poller

    async def poll(self, timeout: float = 2.0) -> bool:
        """One cycle; False when it did not return in time (it awaited a deploy)."""
        try:
            await asyncio.wait_for(self.poller._poll_once(), timeout)
        except TimeoutError:
            return False
        return True


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []
        self.errors: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())
        if record.levelno >= logging.ERROR:
            self.errors.append(record.getMessage())

    def starting(self, prefix: str) -> list[str]:
        return [m for m in self.messages if m.startswith(prefix)]


async def _drain(harness: _Harness, release: asyncio.Event) -> None:
    """Let any deploy task finish so the loop closes cleanly."""
    release.set()
    deploy = harness.poller._detached_deploy
    if deploy is not None:
        await asyncio.wait_for(deploy.task, 2.0)


def test_fast_actions_dispatch_during_deploy() -> None:
    print("\n[1] actions queued behind a deploy dispatch while it is still in flight")

    async def scenario() -> None:
        release = asyncio.Event()
        ran: list[str] = []

        async def handler(action: QueuedAction) -> None:
            if action.process_key == APPLY:
                await release.wait()
            ran.append(action.id)

        h = _Harness(
            [_action("ae-deploy", APPLY), _action("ae-f1", FAST), _action("ae-f2", FAST)],
            handler,
        )
        returned = await h.poll()
        _check(returned, "_poll_once returned while the deploy was still running")
        await asyncio.sleep(0)
        _check(ran == ["ae-f1", "ae-f2"], f"both fast actions ran before the deploy ({ran})")
        deploy = h.poller._detached_deploy
        _check(
            deploy is not None and deploy.action_id == "ae-deploy" and not deploy.task.done(),
            "the deploy is still in flight and named by id",
        )
        await _drain(h, release)
        _check(ran[-1] == "ae-deploy", "the deploy then completed through the same handler")
        _check(
            h.poller.total_actions_processed == 3,
            "all three counted as processed",
        )

    asyncio.run(scenario())


def test_slow_action_before_after() -> None:
    print("\n[2] deploy-shaped action: no SLOW_ACTION on the loop; DEPLOY_DETACHED instead")
    capture = _Capture()
    poller_module.logger.addHandler(capture)
    previous_level = poller_module.logger.level
    poller_module.logger.setLevel(logging.INFO)
    poller_module.SLOW_ACTION_THRESHOLD_SECONDS = 0.2
    try:

        async def scenario() -> None:
            async def handler(action: QueuedAction) -> None:
                if action.process_key in (APPLY, SLOW_NON_DEPLOY):
                    await asyncio.sleep(0.5)

            h = _Harness([_action("ae-deploy", APPLY), _action("ae-f1", FAST)], handler)
            await h.poll()
            _check(not capture.starting("SLOW_ACTION"), "a 0.5 s deploy logs no SLOW_ACTION")
            deploy = h.poller._detached_deploy
            assert deploy is not None
            await asyncio.wait_for(deploy.task, 2.0)
            done = capture.starting("DEPLOY_DETACHED")
            _check(
                len(done) == 1 and "ae-deploy" in done[0] and APPLY in done[0],
                f"its duration is logged once as DEPLOY_DETACHED ({done})",
            )
            control = _Harness([_action("ae-audit", SLOW_NON_DEPLOY)], handler)
            await control.poll()
            slow = capture.starting("SLOW_ACTION")
            _check(
                len(slow) == 1 and "ae-audit" in slow[0],
                f"control: a slow non-deploy action still logs SLOW_ACTION ({slow})",
            )

        asyncio.run(scenario())
    finally:
        poller_module.SLOW_ACTION_THRESHOLD_SECONDS = 10.0
        poller_module.logger.setLevel(previous_level)
        poller_module.logger.removeHandler(capture)


def test_single_flight() -> None:
    print("\n[3] exactly one deploy in flight; a second deploy row waits, unclaimed")

    async def scenario() -> None:
        release = asyncio.Event()
        ran: list[str] = []

        async def handler(action: QueuedAction) -> None:
            ran.append(action.id)
            if action.process_key in (APPLY, RESTART):
                await release.wait()

        h = _Harness(
            [_action("ae-d1", APPLY), _action("ae-d2", RESTART), _action("ae-f1", FAST)],
            handler,
        )
        await h.poll()
        await asyncio.sleep(0)
        _check("ae-d2" not in h.claimed, f"the second deploy was not claimed ({h.claimed})")
        _check(
            any(a.id == "ae-d2" for a in h.queue),
            "the second deploy row is still queued",
        )
        _check("ae-f1" in ran, "the fast action behind both still ran")
        await h.poll()
        _check("ae-d2" not in h.claimed, "still not claimed on a later cycle while d1 runs")
        release.set()
        first = h.poller._detached_deploy
        assert first is not None
        await asyncio.wait_for(first.task, 2.0)
        await h.poll()
        second = h.poller._detached_deploy
        _check(
            second is not None and second.action_id == "ae-d2" and "ae-d2" in h.claimed,
            "after the first finished, the second was claimed and started",
        )
        assert second is not None
        await asyncio.wait_for(second.task, 2.0)

    asyncio.run(scenario())


def test_deploy_failure_marks_row_failed() -> None:
    print("\n[4] a deploy that raises is marked failed; the actions behind it are unaffected")

    async def scenario() -> None:
        ran: list[str] = []

        async def handler(action: QueuedAction) -> None:
            ran.append(action.id)
            if action.process_key == APPLY:
                raise RuntimeError("swap refused")

        h = _Harness([_action("ae-deploy", APPLY), _action("ae-f1", FAST)], handler)
        await h.poll()
        deploy = h.poller._detached_deploy
        assert deploy is not None
        await asyncio.wait_for(deploy.task, 2.0)
        _check(h.failed == ["ae-deploy"], f"the deploy row was marked failed ({h.failed})")
        _check("ae-f1" in ran, "the fast action behind it ran")
        _check(
            h.poller.total_actions_processed == 1,
            "only the action that dispatched is counted as processed",
        )
        _check(h.poller._running_deploy() is None, "the finished deploy no longer counts as in flight")

    asyncio.run(scenario())


class _ReaperSpy:
    """Serves one stale processing row and records which rows the reap failed."""

    def __init__(self, action_id: str) -> None:
        stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        self._row = {
            "id": action_id,
            "process_key": APPLY,
            "parameters": "{}",
            "updated_at": stale,
        }
        self.failed: list[str] = []

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        _ = (namespace, data)
        return {"data": {"records": [self._row]}}

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> dict[str, object]:
        _ = (namespace, updates)
        filters = query["filters"]
        assert isinstance(filters, dict)
        self.failed.append(str(filters["id"]))
        return {"action_status": "completed", "data": {"result": {"updated": 1}}}


def test_reaper_skips_live_deploy() -> None:
    print("\n[5] the orphan reap does not fail the deploy this process is running")

    async def scenario() -> None:
        release = asyncio.Event()

        async def handler(action: QueuedAction) -> None:
            if action.process_key == APPLY:
                await release.wait()

        h = _Harness([_action("ae-deploy", APPLY)], handler)
        await h.poll()
        spy = _ReaperSpy("ae-deploy")
        h.poller.state_service = spy  # type: ignore[assignment]
        h.poller._maybe_reap_orphans()
        _check(spy.failed == [], f"the live deploy's own stale-looking row was skipped ({spy.failed})")
        stranger = _ReaperSpy("ae-other")
        h.poller.state_service = stranger  # type: ignore[assignment]
        h.poller._maybe_reap_orphans()
        _check(
            stranger.failed == ["ae-other"],
            f"control: a stale row that is not live is still failed ({stranger.failed})",
        )
        await _drain(h, release)
        after = _ReaperSpy("ae-deploy")
        h.poller.state_service = after  # type: ignore[assignment]
        h.poller._maybe_reap_orphans()
        _check(
            after.failed == ["ae-deploy"],
            "once the deploy finished, its id is no longer exempt",
        )

    asyncio.run(scenario())


def _backdate_deploy(seconds: float) -> None:
    """Make the in-flight deploy look ``seconds`` older (the age has one source)."""
    started = ACTION_PATH_LIVENESS.detached_deploy_started_monotonic
    assert started is not None
    ACTION_PATH_LIVENESS.detached_deploy_started_monotonic = started - seconds


def test_hung_deploy_is_visible_and_alarmed() -> None:
    print("\n[6] a hung deploy is named in /health, stalls past the threshold, logs ERROR once")
    capture = _Capture()
    poller_module.logger.addHandler(capture)
    previous_level = poller_module.logger.level
    poller_module.logger.setLevel(logging.INFO)

    async def scenario() -> None:
        release = asyncio.Event()

        async def handler(action: QueuedAction) -> None:
            if action.process_key == APPLY:
                await release.wait()

        h = _Harness([_action("ae-hung", APPLY)], handler)
        await h.poll()
        live = ACTION_PATH_LIVENESS.snapshot()
        _check(
            live["detached_deploy_action_id"] == "ae-hung" and live["detached_deploy_process_key"] == APPLY and isinstance(live["detached_deploy_age_seconds"], float) and isinstance(live["detached_deploy_started_at"], str),
            f"liveness names the in-flight deploy with its age ({live})",
        )
        _check(
            live["detached_deploy_stalled"] is False and live["action_path_stalled"] is False,
            "a deploy inside the threshold is in flight, not stalled",
        )
        _check(not capture.starting("DEPLOY_STALLED"), "nothing is logged inside the threshold")
        _backdate_deploy(liveness_module.DEPLOY_STALL_THRESHOLD_SECONDS + 5)
        stalled = ACTION_PATH_LIVENESS.snapshot()
        _check(
            stalled["detached_deploy_stalled"] is True and stalled["action_path_stalled"] is True,
            "past the threshold the deploy is stalled and the action path reports stalled",
        )
        await h.poll()
        await h.poll()
        errors = capture.starting("DEPLOY_STALLED")
        _check(
            len(errors) == 1 and "ae-hung" in errors[0] and APPLY in errors[0],
            f"DEPLOY_STALLED is logged once per interval, naming the deploy ({errors})",
        )
        _check(
            bool(errors) and f"stall threshold {liveness_module.DEPLOY_STALL_THRESHOLD_SECONDS:.0f}s" in errors[0] and f"at {liveness_module.DEPLOY_REAP_EXEMPTION_CEILING_SECONDS:.0f}s" in errors[0],
            "DEPLOY_STALLED names the current threshold and exemption ceiling",
        )
        _check(
            any("process restart" in message.lower() and "hung" in message.lower() for message in errors),
            "deploy_restart_recovery_required: a hung deploy needs process restart to release its slot",
        )
        await _drain(h, release)
        after = ACTION_PATH_LIVENESS.snapshot()
        _check(
            after["detached_deploy_action_id"] is None and after["detached_deploy_age_seconds"] is None and after["detached_deploy_stalled"] is False and after["action_path_stalled"] is False,
            "the finished deploy is cleared from liveness",
        )

    try:
        asyncio.run(scenario())
    finally:
        poller_module.logger.setLevel(previous_level)
        poller_module.logger.removeHandler(capture)


def test_held_back_row_logged_once() -> None:
    print("\n[7] a deploy row held back behind the in-flight deploy is logged once")
    capture = _Capture()
    poller_module.logger.addHandler(capture)
    previous_level = poller_module.logger.level
    poller_module.logger.setLevel(logging.INFO)

    async def scenario() -> None:
        release = asyncio.Event()

        async def handler(action: QueuedAction) -> None:
            if action.process_key in (APPLY, RESTART):
                await release.wait()

        h = _Harness([_action("ae-d1", APPLY), _action("ae-d2", RESTART)], handler)
        await h.poll()
        await h.poll()
        await h.poll()
        held = capture.starting("DEPLOY_HELD_BACK")
        _check(
            len(held) == 1 and "ae-d2" in held[0] and "ae-d1" in held[0],
            f"one DEPLOY_HELD_BACK line, naming the held row and the running deploy ({held})",
        )
        await _drain(h, release)

    try:
        asyncio.run(scenario())
    finally:
        poller_module.logger.setLevel(previous_level)
        poller_module.logger.removeHandler(capture)


def test_reaper_exemption_has_a_ceiling() -> None:
    print("\n[8] the reaper exemption ends at the ceiling: a deploy that far over is failed")

    async def scenario() -> None:
        release = asyncio.Event()

        async def handler(action: QueuedAction) -> None:
            if action.process_key == APPLY:
                await release.wait()

        h = _Harness([_action("ae-hung", APPLY)], handler)
        await h.poll()
        _backdate_deploy(liveness_module.DEPLOY_REAP_EXEMPTION_CEILING_SECONDS - 100)
        _check(
            h.poller._live_detached_action_ids() == frozenset({"ae-hung"}),
            "just inside the ceiling the deploy is still exempt",
        )
        _backdate_deploy(200)
        _check(
            h.poller._live_detached_action_ids() == frozenset(),
            "past the ceiling it is no longer exempt",
        )
        spy = _ReaperSpy("ae-hung")
        h.poller.state_service = spy  # type: ignore[assignment]
        h.poller._maybe_reap_orphans()
        _check(spy.failed == ["ae-hung"], f"the reaper fails the over-ceiling deploy ({spy.failed})")
        _check(
            liveness_module.DEPLOY_REAP_EXEMPTION_CEILING_SECONDS > 5345.7,
            "the ceiling is above the longest measured deploy (5345.7 s)",
        )
        await _drain(h, release)

    asyncio.run(scenario())


def test_held_back_rows_take_no_slot() -> None:
    print("\n[9] 12 queued deploy rows behind a running deploy do not starve the fast row")

    async def scenario() -> None:
        release = asyncio.Event()

        async def handler(action: QueuedAction) -> None:
            if action.process_key == APPLY:
                await release.wait()

        h = _Harness([_action("ae-run", APPLY)], handler)
        await h.poll()
        rows: list[dict[str, object]] = [{"id": f"ae-retry{i}", "process_key": APPLY, "excluded_versions": None} for i in range(12)]
        rows.append({"id": "ae-fast", "process_key": FAST, "excluded_versions": None})

        class _State:
            def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
                _ = (namespace, data)
                return {"action_status": "completed", "data": {"records": rows}}

        real = ActionQueuePoller.__new__(ActionQueuePoller)
        real.state_service = _State()  # type: ignore[assignment]
        real.max_actions_per_poll = 10
        real._solet_version = "local"
        real._last_observed_queue_depth = 0
        real._detached_deploy = h.poller._detached_deploy
        real._fetch_session_namespaces = lambda _taken: {}  # type: ignore[method-assign]
        real._build_queued_action = (  # type: ignore[method-assign]
            lambda row, _ns: _action(str(row["id"]), str(row["process_key"]))
        )
        got = [a.id for a in await real._get_queued_actions()]
        _check(got == ["ae-fast"], f"only the fast row is fetched while a deploy runs ({got})")
        await _drain(h, release)
        got_after = [a.id for a in await real._get_queued_actions()]
        _check(
            len(got_after) == 10 and "ae-fast" not in got_after,
            "control: with no deploy running the deploy rows are fetched as before",
        )

    asyncio.run(scenario())


def test_dispatch_tripwire_explains_page_causes() -> None:
    print("\n[10] a full dispatch page reports eligibility and deploy holdbacks separately")
    capture = _Capture()
    poller_module.logger.addHandler(capture)
    previous_level = poller_module.logger.level
    poller_module.logger.setLevel(logging.WARNING)

    async def scenario() -> None:
        release = asyncio.Event()

        async def handler(action: QueuedAction) -> None:
            if action.process_key == APPLY:
                await release.wait()

        h = _Harness([_action("ae-run", APPLY)], handler)
        await h.poll()
        real = ActionQueuePoller.__new__(ActionQueuePoller)
        real.max_actions_per_poll = 10
        real._solet_version = "local"
        real._last_observed_queue_depth = 0
        real._detached_deploy = h.poller._detached_deploy
        real._fetch_session_namespaces = lambda _taken: {}  # type: ignore[method-assign]
        real._build_queued_action = lambda row, _ns: _action(  # type: ignore[method-assign]
            str(row["id"]), str(row["process_key"])
        )

        class _State:
            def __init__(self) -> None:
                self.queries: list[dict[str, object]] = []
                self.writes = 0

            def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
                _ = namespace
                self.queries.append(data)
                return {"action_status": "completed", "data": {"records": rows}}

        state = _State()
        real.state_service = state  # type: ignore[assignment]

        def held(count: int) -> list[dict[str, object]]:
            return [{"id": f"held-{i}", "process_key": APPLY, "excluded_versions": None} for i in range(count)]

        def rejected(count: int) -> list[dict[str, object]]:
            return [{"id": f"excluded-{i}", "process_key": FAST, "excluded_versions": ["local"]} for i in range(count)]

        def fast(count: int) -> list[dict[str, object]]:
            return [{"id": f"fast-{i}", "process_key": FAST, "excluded_versions": None} for i in range(count)]

        pinned_wrong: dict[str, object] = {
            "id": "pin-ineligible",
            "process_key": FAST,
            "excluded_versions": ["local", "instance:other-instance"],
        }

        async def fetch(page: list[dict[str, object]]) -> list[str]:
            nonlocal rows
            rows = page
            result = await real._get_queued_actions()
            return [action.id for action in result]

        rows: list[dict[str, object]] = []
        held_page = held(91) + fast(9)
        ids = await fetch(held_page)
        warnings = capture.starting("DISPATCH-TRIPWIRE")
        _check(
            ids == [f"fast-{i}" for i in range(9)],
            f"91 held-back rows preserve the 9 fast IDs ({ids})",
        )
        _check(
            len(warnings) == 1 and "eligibility_rejected=0" in warnings[0] and "deploy_held_back=91" in warnings[0],
            f"held-back cause is reported separately from eligibility ({warnings})",
        )

        capture.messages.clear()
        mixed_page = rejected(44) + [pinned_wrong] + held(46) + fast(9)
        ids = await fetch(mixed_page)
        warnings = capture.starting("DISPATCH-TRIPWIRE")
        _check(
            ids == [f"fast-{i}" for i in range(9)],
            f"mixed page preserves eligible action order ({ids})",
        )
        _check(
            len(warnings) == 1 and "eligibility_rejected=45" in warnings[0] and "deploy_held_back=46" in warnings[0],
            f"mixed causes include version and pin eligibility without folding in deploy holdback ({warnings})",
        )

        capture.messages.clear()
        excluded_page = rejected(90) + [pinned_wrong] + fast(9)
        await fetch(excluded_page)
        warnings = capture.starting("DISPATCH-TRIPWIRE")
        _check(
            len(warnings) == 1 and "eligibility_rejected=91" in warnings[0] and "deploy_held_back=0" in warnings[0],
            f"exclusion-only page reports eligibility cause ({warnings})",
        )

        capture.messages.clear()
        await fetch(held(90) + fast(10))
        _check(not capture.starting("DISPATCH-TRIPWIRE"), "a full set of slots stays quiet")
        capture.messages.clear()
        await fetch(held(5) + fast(4))
        _check(not capture.starting("DISPATCH-TRIPWIRE"), "a below-cap page stays quiet")
        _check(
            len(state.queries) == 5
            and all(
                query
                == {
                    "table": "action_events",
                    "filters": {"status": "queued"},
                    "order_by": [["sequence", "asc"], ["id", "asc"]],
                    "limit": 100,
                    "include_deleted": True,
                }
                for query in state.queries
            )
            and state.writes == 0,
            "all fixtures keep the exact bounded ordered read and perform no state writes",
        )
        await _drain(h, release)

    try:
        asyncio.run(scenario())
    finally:
        poller_module.logger.setLevel(previous_level)
        poller_module.logger.removeHandler(capture)


def test_stop_cancels_the_detached_deploy() -> None:
    print("\n[11] stop() cancels the detached deploy as well as the poll task")

    async def scenario() -> None:
        release = asyncio.Event()

        async def handler(action: QueuedAction) -> None:
            if action.process_key == APPLY:
                await release.wait()

        h = _Harness([_action("ae-deploy", APPLY)], handler)
        await h.poll()
        deploy = h.poller._detached_deploy
        assert deploy is not None
        poll_task = asyncio.create_task(asyncio.sleep(3600))
        h.poller.running = True
        h.poller.poller_task = poll_task
        await h.poller.stop()
        _check(poll_task.cancelled(), "the poll task is cancelled")
        _check(deploy.task.cancelled(), "the detached deploy task is cancelled too")
        _check(h.poller._running_deploy() is None, "no deploy counts as in flight after stop")

    asyncio.run(scenario())


def test_gated_off_colour_claims_nothing_more() -> None:
    print("\n[12] a colour gated off mid-batch claims no more rows (no self-claimed finisher)")

    async def scenario() -> None:
        active = [True]

        async def handler(action: QueuedAction) -> None:
            if action.id == "ae-f1":
                active[0] = False  # the swap gated this colour off

        h = _Harness(
            [_action("ae-f1", FAST), _action("ae-complete-swap", FAST), _action("ae-f3", FAST)],
            handler,
        )
        h.poller._is_active_color_getter = lambda: active[0]
        await h.poll()
        _check(h.claimed == ["ae-f1"], f"only the row before the gate-off was claimed ({h.claimed})")
        _check(
            [a.id for a in h.queue] == ["ae-complete-swap", "ae-f3"],
            "the rest stays queued for the new colour",
        )

    asyncio.run(scenario())


def test_detached_keys_are_real_process_keys() -> None:
    print("\n[13] every detached process key is a shipped process definition")
    processes = REPO_ROOT / "ananta" / "knowledge_base" / "processes"
    declared = {json.loads(path.read_text(encoding="utf-8")).get("process_key") for path in processes.glob("*/*.json")}
    detached = poller_module.DETACHED_DEPLOY_PROCESS_KEYS
    for key in sorted(detached):
        _check(key in declared, f"{key} is declared under ananta/knowledge_base/processes")
    _check(
        {APPLY, RESTART} <= detached,
        "the deploy keys this smoke drives are the detached ones",
    )


def _selected_task(h: _Harness, lane_name: str = "measured") -> asyncio.Task[None]:
    from ananta.core.actions import _measured_action_lane as lane

    task = lane._states[h.poller].slots[lane_name].task
    assert task is not None
    return task


@contextmanager
def _measured_logs() -> Generator[_Capture]:
    from ananta.core.actions import _measured_action_lane as lane

    capture = _Capture()
    level = lane.logger.level
    lane.logger.setLevel(logging.INFO)
    lane.logger.addHandler(capture)
    try:
        yield capture
    finally:
        lane.logger.removeHandler(capture)
        lane.logger.setLevel(level)


def test_measured_classes_do_not_block_messaging() -> None:
    async def scenario(key: str) -> None:
        release, entered = asyncio.Event(), asyncio.Event()
        ran: list[str] = []
        task: asyncio.Task[None] | None = None

        async def handler(action: QueuedAction) -> None:
            if action.id == "ae-long":
                print(f"MEASURED_HANDLER_ENTERED: {action.id} {key}")
                entered.set()
                await release.wait()
            ran.append(action.id)

        h = _Harness([_action("ae-long", key), _action("ae-message", FAST)], handler)
        before = set(vars(h.poller))
        try:
            returned = await h.poll(timeout=0.2)
            await asyncio.sleep(0)
            assert "ae-message" in ran and not release.is_set(), (
                "MESSAGING_PROGRESS_BEFORE_MEASURED_RELEASE",
                key,
                returned,
                ran,
                h.claimed,
            )
            task = _selected_task(h)
            assert entered.is_set() and not task.done()
            assert ran == ["ae-message"] and h.claimed == ["ae-long", "ae-message"]
            assert h.failed == [] and h.poller.total_actions_processed == 1
            assert set(vars(h.poller)) == before, "NO_NEW_POLLER_ATTRIBUTES"
        finally:
            release.set()
            if task is not None:
                await task
        assert ran == ["ae-message", "ae-long"] and h.poller.total_actions_processed == 2

    for key in MEASURED:
        asyncio.run(scenario(key))


def test_ingest_and_unknown_classes_stay_serial() -> None:
    from ananta.core.actions import _measured_action_lane as lane

    definitions = [
        *(REPO_ROOT / "ananta/knowledge_base/processes").glob("*/*.json"),
        *(REPO_ROOT / "plugins").glob("*/knowledge_base/processes/*.json"),
    ]
    declared = {json.loads(p.read_text()).get("process_key") for p in definitions}
    # A seed omits origin-only plugins (seed_factory_plugin) but ships this lane
    # module, so only a key whose owner ships in this tree needs its definition
    # here; the origin tree, which ships every plugin, still checks them all.
    shipped = {key for key in lane.PROCESS_KEYS if not key.startswith("plugin::") or (REPO_ROOT / "plugins" / key.split("::")[1]).is_dir()}
    assert lane.MEASURED_PROCESS_KEYS == frozenset(MEASURED) | lane.HEAVY_PROCESS_KEYS, sorted(lane.MEASURED_PROCESS_KEYS)
    assert lane.PROCESS_KEYS == lane.MEASURED_PROCESS_KEYS | lane.SESSION_LIFECYCLE_PROCESS_KEYS, sorted(lane.PROCESS_KEYS)
    assert frozenset(MEASURED) <= shipped and shipped <= declared, sorted(shipped - declared)

    async def scenario(key: str) -> None:
        release, entered = asyncio.Event(), asyncio.Event()
        ran: list[str] = []
        active, peak = 0, 0

        async def handler(action: QueuedAction) -> None:
            nonlocal active, peak
            if action.process_key == key:
                active += 1
                peak = max(peak, active)
                entered.set()
                await release.wait()
                active -= 1
            ran.append(action.id)

        h = _Harness([_action("ae-1", key), _action("ae-2", key), _action("ae-message", FAST)], handler)
        task = asyncio.create_task(h.poller._poll_once())
        try:
            await asyncio.wait_for(entered.wait(), 0.2)
            assert ran == [] and h.claimed == ["ae-1"] and not task.done()
        finally:
            release.set()
            await task
        assert ran == ["ae-1", "ae-2", "ae-message"] and peak == 1

    for key in (INGEST, SLOW_NON_DEPLOY):
        asyncio.run(scenario(key))


def _reaper_control(h: _Harness, protected_id: str) -> None:
    spy = _ReaperSpy(protected_id)
    h.poller.state_service = spy  # type: ignore[assignment]
    h.poller._maybe_reap_orphans()
    assert spy.failed == []
    stranger = _ReaperSpy("ae-stranger")
    h.poller.state_service = stranger  # type: ignore[assignment]
    h.poller._maybe_reap_orphans()
    assert stranger.failed == ["ae-stranger"]


async def _measured_fifo() -> None:
    release = asyncio.Event()
    ran: list[str] = []

    async def handler(action: QueuedAction) -> None:
        if action.id == "ae-message":
            release.set()
            await _selected_task(h)
        if action.id == "ae-1":
            await release.wait()
        ran.append(action.id)

    h = _Harness(
        [
            _action("ae-1", MEASURED[0]),
            _action("ae-2", MEASURED[1]),
            _action("ae-message", FAST),
            _action("ae-3", MEASURED[0]),
        ],
        handler,
    )
    try:
        await h.poll()
        assert h.claimed == ["ae-1", "ae-message"]
        assert [a.id for a in h.queue] == ["ae-2", "ae-3"], "HELD_PREDECESSOR_CYCLE_BARRIER"
        await h.poll()
        await _selected_task(h)
        await h.poll()
        await _selected_task(h)
        assert ran == ["ae-1", "ae-message", "ae-2", "ae-3"]
    finally:
        release.set()


async def _measured_failure(uncertain: bool, capture: _Capture) -> None:
    from ananta.core.actions import _measured_action_lane as lane

    ran: list[str] = []

    async def handler(action: QueuedAction) -> None:
        if action.id == "ae-fail":
            raise RuntimeError("handler refused")
        ran.append(action.id)

    h = _Harness([_action("ae-fail", MEASURED[0]), _action("ae-message", FAST)], handler)
    if uncertain:

        def failed_write(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("terminal write refused")

        h.poller._mark_action_failed = failed_write  # type: ignore[method-assign]
    await h.poll()
    await _selected_task(h)
    assert ran == ["ae-message"] and h.poller.total_actions_processed == 1
    if uncertain:
        h.queue.append(_action("ae-next", MEASURED[1]))
        await h.poll()
        await _selected_task(h)
        assert ran == ["ae-message", "ae-next"] and lane.live_ids(h.poller) == {"ae-fail"}, (
            "UNCERTAIN_ROW_PROTECTED_NEXT_SELECTED_ADMITTED",
            ran,
            h.claimed,
        )
        _reaper_control(h, "ae-fail")
        assert any("ae-fail" in m and "terminal recording" in m for m in capture.errors)
    else:
        assert h.failed == ["ae-fail"] and lane.live_ids(h.poller) == set()
        h.queue.append(_action("ae-lost", MEASURED[1]))
        h.poller._mark_action_processing = lambda _id: False  # type: ignore[method-assign]
        await h.poll()
        assert ran == ["ae-message"] and "ae-lost" not in h.claimed, "CAS_LOSER_NEVER_EXECUTES"


def test_measured_slot_preserves_order_and_failure() -> None:
    with _measured_logs() as capture:
        asyncio.run(_measured_fifo())
        asyncio.run(_measured_failure(False, capture))
        asyncio.run(_measured_failure(True, capture))


async def _measured_reaper_and_advisory(capture: _Capture) -> None:
    from ananta.core.actions import _measured_action_lane as lane

    release = asyncio.Event()

    async def handler(_action: QueuedAction) -> None:
        await release.wait()

    h = _Harness([_action("ae-live", MEASURED[0]), _action("ae-deploy", APPLY)], handler)
    await h.poll()
    task = _selected_task(h)
    try:
        assert h.poller._live_detached_action_ids() == {"ae-live", "ae-deploy"}
        _reaper_control(h, "ae-live")
        state = lane._states[h.poller]
        with patch.object(lane.time, "monotonic", return_value=state.slots["measured"].started + 121):
            lane.report(h.poller)
            lane.report(h.poller)
        assert len([m for m in capture.errors if m.startswith("MEASURED_STALLED")]) == 1
        other = _Harness([], handler)
        assert not lane.held(other.poller, MEASURED[0]) and lane.live_ids(other.poller) == set()
    finally:
        release.set()
        await task
        await _drain(h, release)
    assert lane.live_ids(h.poller) == set()
    after = _ReaperSpy("ae-live")
    h.poller.state_service = after  # type: ignore[assignment]
    h.poller._maybe_reap_orphans()
    assert after.failed == ["ae-live"]


async def _measured_shutdown(capture: _Capture) -> None:
    from ananta.core.actions import _measured_action_lane as lane

    never_release, entered = asyncio.Event(), asyncio.Event()
    terminal: list[str] = []

    async def handler(action: QueuedAction) -> None:
        entered.set()
        await never_release.wait()
        terminal.append(action.id)

    h = _Harness([_action("ae-cancel", MEASURED[0])], handler)
    try:
        await h.poll()
        await asyncio.wait_for(entered.wait(), 0.2)
        task = _selected_task(h)
        h.poller.running = True
        h.poller.poller_task = asyncio.create_task(asyncio.sleep(3600))
        stopping = asyncio.create_task(h.poller.stop())
        await asyncio.sleep(0.01)
        assert not stopping.done() and not task.cancelled(), "STOP_DRAINS_SELECTED_HANDLER"
        never_release.set()
        await asyncio.wait_for(stopping, 0.2)
        assert task.done() and not task.cancelled() and h.poller.poller_task.cancelled()
        assert lane.live_ids(h.poller) == frozenset() and terminal == ["ae-cancel"]
        assert h.failed == [] and h.poller.total_actions_processed == 1 and h.claimed == ["ae-cancel"]
    finally:
        never_release.set()
        await lane.stop(h.poller)
    early = _Harness([_action("ae-before-entry", MEASURED[1])], handler)
    await early.poller._poll_once()
    early_task = _selected_task(early)
    early_task.cancel()  # External cancellation before entry must still protect the CAS-owned row.
    await lane.stop(early.poller)
    assert early_task.cancelled() and lane.live_ids(early.poller) == {"ae-before-entry"}
    assert any("ae-before-entry" in m and "cancelled" in m for m in capture.errors)


def test_measured_slot_shutdown_and_reaper() -> None:
    with _measured_logs() as capture:
        asyncio.run(_measured_reaper_and_advisory(capture))
        asyncio.run(_measured_shutdown(capture))


class _ReadQueue(_SharedQueue):
    """Real ordered grammar and queued-status CAS; only storage is in memory."""

    def __init__(self, claimed: list[str]) -> None:
        super().__init__()
        self.claimed, self.queries = claimed, []
        self.reply: Any = None

    def query_ordered(self, namespace: str, data: dict[str, object]) -> Any:
        spec = parse_ordered_query(data)
        rows = apply_ordered_query_in_memory(self.rows, spec)
        self.queries.append((namespace, dict(data), len(rows)))
        print("MEASURED_RAW_WINDOW", namespace, data, len(rows))
        if isinstance(self.reply, Exception):
            raise self.reply
        return (
            self.reply
            if self.reply is not None
            else {
                "action_status": "completed",
                "data": {"records": rows},
                "actions": [],
            }
        )

    def update_state(self, namespace: str, query: Any, updates: Any) -> Any:
        result: Any = super().update_state(namespace, query, updates)
        if result["data"]["result"]["updated"]:
            self.claimed.append(query["filters"]["id"])
        return result


def _real_fetch(h: _Harness, queue: _ReadQueue) -> None:
    del h.poller._get_queued_actions
    del h.poller._mark_action_processing
    h.poller.state_service = queue
    h.poller.max_actions_per_poll, h.poller._solet_version = 10, "local"
    h.poller._claim_instance_id = "own"


def _assert_backlog_page_contract(h: _Harness, queue: _ReadQueue, ran: list[str]) -> None:
    queries = queue.queries
    assert len(queries) == 4 and [q[2] for q in queries] == [100, 100, 16, 6]
    assert [q[1].get("after") for q in queries] == [
        None,
        [100, "ae-held-99"],
        [200, "ae-held-199"],
        [200, "ae-held-199"],
    ]
    assert ran == [f"ae-message-{i}" for i in range(15)]
    assert h.claimed == ["ae-active", *ran] and h.poller._last_observed_queue_depth == 5
    assert all(r["status"] == "queued" for r in queue.rows[:201])


def test_measured_backlog_paginates_without_claiming_held_rows() -> None:
    async def scenario() -> None:
        from ananta.core.actions import _measured_action_lane as lane

        release, ran = asyncio.Event(), []

        async def handler(action: QueuedAction) -> None:
            if action.id == "ae-active":
                await release.wait()
            ran.append(action.id)

        h = _Harness([_action("ae-active", MEASURED[0])], handler)
        queue = _ReadQueue(h.claimed)
        queries = queue.queries
        queue.rows = [
            {
                "id": f"ae-held-{i}",
                "sequence": i + 1,
                "process_key": MEASURED[i % 2],
                "status": "queued",
            }
            for i in range(201)
        ]
        queue.rows += [{"id": f"ae-message-{i}", "sequence": i + 202, "process_key": FAST, "status": "queued"} for i in range(15)]
        queue.rows.append({"id": "ae-active", "sequence": 0, "status": "processing"})
        try:
            await h.poll()
            await asyncio.sleep(0)
            _real_fetch(h, queue)
            for _ in range(4):
                await h.poll()
            assert all(f"ae-message-{i}" in ran for i in range(15)) and not release.is_set(), (
                "MESSAGING_PROGRESS_PAST_BOUNDED_READ",
                ran,
                h.claimed,
                queries,
            )
            _assert_backlog_page_contract(h, queue, ran)
            release.set()
            await _selected_task(h)
            await h.poll()
            await _selected_task(h)
            assert "after" not in queries[-1][1] and h.claimed[-1] == "ae-held-0"
        finally:
            release.set()
            await lane.stop(h.poller)

    asyncio.run(scenario())


async def _busy_read_contract(h: _Harness, queue: _ReadQueue, expected: dict[str, object]) -> None:
    from ananta.core.actions import _measured_action_lane as lane

    state = lane._states[h.poller]
    for reply in (
        {"action_status": "failed", "error": {"message": "provider"}},
        {"action_status": "completed", "data": {"records": "malformed"}},
    ):
        queue.reply = reply
        assert await lane.query_ordered(h.poller, "core", expected) is reply and state.cursor == (
            7,
            "boundary",
        )
    queue.reply = RuntimeError("provider raised")
    try:
        await lane.query_ordered(h.poller, "core", expected)
    except RuntimeError as exc:
        assert str(exc) == "provider raised" and state.cursor == (7, "boundary")
    else:
        raise AssertionError("PROVIDER_RAISE_PRESERVED")
    queue.reply = {
        "action_status": "completed",
        "data": {"records": [{"process_key": MEASURED[0]}]},
    }
    await lane.query_ordered(h.poller, "core", expected)
    assert state.cursor == (7, "boundary"), "MISSING_CURSOR_IS_ADVISORY"
    queue.reply = {"action_status": "completed", "data": {"records": []}}
    result = await lane.query_ordered(h.poller, "core", expected)
    assert result == queue.reply and result is not queue.reply and state.cursor is None


def test_idle_queue_and_ingest_keep_original_read_contract() -> None:
    async def scenario() -> None:
        from ananta.core.actions import _measured_action_lane as lane

        release, ran = asyncio.Event(), []

        async def handler(action: QueuedAction) -> None:
            if action.id == "ae-fail":
                raise RuntimeError("handler refused")
            if action.id == "ae-next":
                await release.wait()
            ran.append(action.id)

        h = _Harness([], handler)
        queue = _ReadQueue(h.claimed)
        _real_fetch(h, queue)
        queue.rows = [
            {
                "id": name,
                "sequence": i,
                "status": "queued",
                "process_key": key,
                "excluded_versions": excluded,
            }
            for i, (name, key, excluded) in enumerate(
                [
                    ("own", FAST, ["local", "instance:own"]),
                    ("other", FAST, ["instance:other"]),
                    ("excluded", FAST, ["local"]),
                    ("ingest", INGEST, None),
                    ("ordinary", FAST, None),
                ]
            )
        ]
        expected: dict[str, object] = {
            "table": "action_events",
            "filters": {"status": "queued"},
            "order_by": [["sequence", "asc"], ["id", "asc"]],
            "limit": 100,
            "include_deleted": True,
        }
        h.poller._own_rows_only_getter = lambda: True
        await h.poll()
        assert ran == ["own"] and queue.queries[-1][1] == {
            **expected,
            "filters": {"status": "queued", "excluded_versions": {"op": "is_not_null"}},
        }
        h.poller._own_rows_only_getter = lambda: False
        await h.poll()
        assert ran == ["own", "ingest", "ordinary"] and queue.queries[-1][1] == expected
        queue.reply = {
            "action_status": "completed",
            "data": {"records": [{"id": "exact"}]},
            "actions": [],
        }
        assert await lane.query_ordered(h.poller, "core", expected) is queue.reply
        queue.reply = None
        queue.rows = [{"id": "ae-fail", "sequence": 1, "process_key": MEASURED[0], "status": "queued"}]

        def failed_write(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("terminal write refused")

        h.poller._mark_action_failed = failed_write
        await h.poll()
        await _selected_task(h)
        state = lane._states[h.poller]
        state.cursor = (999, "stale")
        queue.rows.append({"id": "ae-next", "sequence": 2, "process_key": MEASURED[1], "status": "queued"})
        try:
            await h.poll()
            assert h.claimed[-1] == "ae-next" and queue.queries[-1][1] == expected, "PROTECTED_ROW_DOES_NOT_ACTIVATE_BUSY_PAGING"
            assert "ae-fail" in lane.live_ids(h.poller) and state.cursor is None
            _reaper_control(h, "ae-fail")
            h.poller.state_service = queue
            state.cursor = (7, "boundary")
            await _busy_read_contract(h, queue, expected)
            release.set()
            await _selected_task(h)
            assert lane.live_ids(h.poller) == {"ae-fail"}
            state.cursor = (9, "stale")
            assert await lane.query_ordered(h.poller, "core", expected) is queue.reply
            assert state.cursor is None and queue.queries[-1][1] == expected
        finally:
            release.set()
            await lane.stop(h.poller)

    asyncio.run(scenario())


def main() -> int:
    print("Deploy-detached-from-poller smoke (wgr_5de9baf3)")
    test_measured_backlog_paginates_without_claiming_held_rows()
    test_idle_queue_and_ingest_keep_original_read_contract()
    test_measured_classes_do_not_block_messaging()
    test_ingest_and_unknown_classes_stay_serial()
    test_measured_slot_preserves_order_and_failure()
    test_measured_slot_shutdown_and_reaper()
    test_fast_actions_dispatch_during_deploy()
    test_slow_action_before_after()
    test_single_flight()
    test_deploy_failure_marks_row_failed()
    test_reaper_skips_live_deploy()
    test_hung_deploy_is_visible_and_alarmed()
    test_held_back_row_logged_once()
    test_reaper_exemption_has_a_ceiling()
    test_held_back_rows_take_no_slot()
    test_dispatch_tripwire_explains_page_causes()
    test_stop_cancels_the_detached_deploy()
    test_gated_off_colour_claims_nothing_more()
    test_detached_keys_are_real_process_keys()
    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: a deploy runs off the serial poller, one at a time, with failure and reap semantics intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
