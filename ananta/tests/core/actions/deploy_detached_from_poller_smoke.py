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

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 ananta/tests/core/actions/deploy_detached_from_poller_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions import action_path_liveness as liveness_module  # noqa: E402
from ananta.core.actions import action_queue_poller as poller_module  # noqa: E402
from ananta.core.actions.action_path_liveness import ACTION_PATH_LIVENESS  # noqa: E402
from ananta.core.actions.action_queue_poller import (  # noqa: E402
    ActionQueuePoller,
    QueuedAction,
)

APPLY = "service_interface::lifecycle_management_service::apply_manifest"
RESTART = "service_interface::self_deployment_service::restart_with_manifest"
FAST = "service_interface::knowledge_service::search"
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
        id=action_id, process_key=process_key, parameters="{}", notes="", created_at="",
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

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

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
            h.poller.total_actions_processed == 3, "all three counted as processed",
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
            any(a.id == "ae-d2" for a in h.queue), "the second deploy row is still queued",
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
            "id": action_id, "process_key": APPLY, "parameters": "{}", "updated_at": stale,
        }
        self.failed: list[str] = []

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        _ = (namespace, data)
        return {"data": {"records": [self._row]}}

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
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
            live["detached_deploy_action_id"] == "ae-hung"
            and live["detached_deploy_process_key"] == APPLY
            and isinstance(live["detached_deploy_age_seconds"], float)
            and isinstance(live["detached_deploy_started_at"], str),
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
        await _drain(h, release)
        after = ACTION_PATH_LIVENESS.snapshot()
        _check(
            after["detached_deploy_action_id"] is None
            and after["detached_deploy_age_seconds"] is None
            and after["detached_deploy_stalled"] is False
            and after["action_path_stalled"] is False,
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
        rows: list[dict[str, object]] = [
            {"id": f"ae-retry{i}", "process_key": APPLY, "excluded_versions": None}
            for i in range(12)
        ]
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


def test_stop_cancels_the_detached_deploy() -> None:
    print("\n[10] stop() cancels the detached deploy as well as the poll task")

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
    print("\n[11] a colour gated off mid-batch claims no more rows (no self-claimed finisher)")

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
    print("\n[12] every detached process key is a shipped process definition")
    processes = REPO_ROOT / "ananta" / "knowledge_base" / "processes"
    declared = {
        json.loads(path.read_text(encoding="utf-8")).get("process_key")
        for path in processes.glob("*/*.json")
    }
    detached = poller_module.DETACHED_DEPLOY_PROCESS_KEYS
    for key in sorted(detached):
        _check(key in declared, f"{key} is declared under ananta/knowledge_base/processes")
    _check(
        {APPLY, RESTART} <= detached,
        "the deploy keys this smoke drives are the detached ones",
    )


def main() -> int:
    print("Deploy-detached-from-poller smoke (wgr_5de9baf3)")
    test_fast_actions_dispatch_during_deploy()
    test_slow_action_before_after()
    test_single_flight()
    test_deploy_failure_marks_row_failed()
    test_reaper_skips_live_deploy()
    test_hung_deploy_is_visible_and_alarmed()
    test_held_back_row_logged_once()
    test_reaper_exemption_has_a_ceiling()
    test_held_back_rows_take_no_slot()
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
