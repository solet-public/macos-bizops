#!/usr/bin/env python3
"""Peer/bridge async routes do no slow blocking work on the request path.

Problem wgr_3ee58d5e: iss_87aa81c4, iss_ab680764, iss_ba4d1c81, iss_5cf6184d,
iss_7009ad9a, iss_59a7f833. ``peer_route_event_loop_offload_smoke.py``
(af96438a5) proves the peer send/claim/register routes left the loop; this
smoke proves the rest of the class, including request-local host qualification:

  A. A peer send whose recipient's drive takes 10 s returns within
     ``SEND_BOUND_S`` reporting ``driver_scheduled``; the drive finishes
     later and its record reads ``driver_sent`` (iss_ab680764).
  B. Drives run on the dedicated executor: never more than
     ``DELIVERY_DRIVE_WORKERS`` at once, on threads named
     ``peer-delivery-drive*``, and a burst of blocked drives leaves asyncio's
     default executor free (iss_ba4d1c81).
  C. Forty concurrent HTTP ``peer/send`` requests whose drives all block:
     every send answers within ``SEND_BOUND_S`` and concurrent ``/events``
     long-polls on six bridges still answer promptly (iss_87aa81c4).
  D. A ``peer/register`` whose autonomic handover notice has a 10 s drive
     does not hold the registration lock across it: an unrelated
     registration issued meanwhile completes promptly (iss_7009ad9a).
  E. The streamable ``peer_register`` relabel and the streamable session
     allocation serialize with the HTTP ``peer/register`` check-then-act on
     one lock (iss_59a7f833).
  F. Every route in the DESIGN route table that touches the state service
     keeps ``/health`` (or the loop) responsive while that call is slow
     (iss_5cf6184d).
  G. The real tmux channel ``send()`` into a busy pane that never stabilizes
     sends Enter after two samples show the landed composer row, well before
     the 10 s fail-open (iss_ab1f0a21).
  H. Twenty drives queued to a recipient that is then parked: only the drives
     already in the pane run; every queued one re-checks the row and records
     ``ineligible_state`` (review D1).
  I. Pending drives are capped: a send beyond the cap gets a recorded
     ``driver_queue_full``, and ``/health`` reports the pending count (D1).
  J. A send after the drive executor shut down records ``driver_unavailable``
     instead of raising (D6).

Mutants (each killed by the named checks; see the lane's DESIGN.md):
  M1 drive back on the request path       -> A, C
  M2 default-shaped drive executor        -> B
  M3 register lock held across the drive  -> D
  M5 relabel without the shared lock      -> E (relabel)
  M6 allocate without the shared lock     -> E (allocate)
  M7 /events DB half back on the loop     -> F (events)
  M8 process/search back on the loop      -> F (process/search)
  M9 streamable bearer verify on the loop -> F (streamable GET/POST/DELETE)
  M10 OAuth token grant on the loop       -> F (oauth/token)
  R2 Enter on the first landed sample     -> G (two samples)
  R3 send() drops the probe (old wait)    -> G (Enter time)
  D1a queued drive skips the re-check     -> H
  D1b queue cap removed                   -> I
  D6 shutdown submit raises again         -> J

  K. Codex/Claude host OS qualification leaves the registration lock free.
  L. Deferred probe validation preserves identity, state writes and outcomes.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/async_route_blocking_class_smoke.py
Exits 0 when every check passes, 1 otherwise (all checks run).
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import drive_on_delivery_smoke as drive_smoke  # noqa: E402
from _registration_qualification_cases import RegistrationHarness, run_concurrency, run_outcomes  # noqa: E402
from ananta.llm.agent_messaging.models import TextPart  # noqa: E402
from ananta.llm.agent_messaging.service import AgentMessagingError  # noqa: E402

from agent_messaging_plugin import delivery_drive, http_routes, role_claim  # noqa: E402
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.mcp_streamable import dispatch as streamable_dispatch  # noqa: E402
from agent_messaging_plugin.mcp_streamable import oauth as streamable_oauth  # noqa: E402
from agent_messaging_plugin.mcp_streamable import router as streamable_router  # noqa: E402
from agent_messaging_plugin.mcp_streamable.auth import BearerAuthError, BearerClaim  # noqa: E402
from agent_messaging_plugin.mcp_streamable.session import StreamableSessionManager  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import dispatch_peer_send  # noqa: E402
from agent_messaging_plugin.platform_surface import BridgeError  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_PARKED,
    LIFECYCLE_SPAWNING,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    transition_lifecycle_state,
)
from agent_messaging_plugin.tmux_adapter import _TmuxSendKeysDriverChannel  # noqa: E402

BASE_URL = "http://bridge.test"
DRIVE_S = 10.0
SEND_BOUND_S = 1.0
SLOW_S = 1.0
PROBE_AT_S = 0.2
MAX_LAG_S = 0.4
REGISTER_HOLD_S = 0.4

_failed: list[str] = []
_passed = 0


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


@contextmanager
def _patched(module: Any, name: str, replacement: object) -> Generator[None]:
    original = getattr(module, name)
    setattr(module, name, replacement)
    try:
        yield
    finally:
        setattr(module, name, original)


# ---------------------------------------------------------------------------
# Drive fixtures: a managed, live recipient whose driver channel blocks.
# ---------------------------------------------------------------------------


class _BlockingDrives:
    """A fake host whose channel ``send`` blocks until released (<= DRIVE_S)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.started = 0
        self.thread_names: set[str] = set()
        self.driver = drive_smoke._install_fake_host()  # noqa: SLF001 -- shared fixture
        self.driver.channel.send = self._send  # type: ignore[method-assign]

    def _send(self, _notice: str) -> None:
        with self.lock:
            self.running += 1
            self.started += 1
            self.peak = max(self.peak, self.running)
            self.thread_names.add(threading.current_thread().name)
        self.release.wait(DRIVE_S)
        with self.lock:
            self.running -= 1

    def wait_started(self, count: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self.lock:
                if self.started >= count:
                    return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        self.release.set()
        drive_smoke._remove_fake_host()  # noqa: SLF001


def _managed_state() -> Any:
    state = drive_smoke._state()  # noqa: SLF001
    drive_smoke._insert(state)  # noqa: SLF001
    transition_lifecycle_state(
        state,
        agent_instance_id=drive_smoke._RECIPIENT_AGI,  # noqa: SLF001
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="test:async-route-blocking",
    )
    return state


class _MessagingService:
    """``peer_send`` stand-in that mints a distinct message id per send."""

    def __init__(self) -> None:
        self._counter = 0
        self._lock = threading.Lock()

    def peer_send(self, request: Any) -> Any:  # noqa: ANN401, ARG002
        with self._lock:
            self._counter += 1
            number = self._counter
        return SimpleNamespace(thread_id="agt-arb", message_id=f"agm-arb-{number}", cursor=number)


def _dispatch_send(state: Any, service: _MessagingService) -> Any:
    return dispatch_peer_send(
        bridge_manager=drive_smoke._FakeBridgeManager(),  # type: ignore[arg-type]  # noqa: SLF001
        peer_registry=drive_smoke._FakePeerRegistry(),  # type: ignore[arg-type]  # noqa: SLF001
        agent_messaging_service=service,
        state_service=state,
        sender_bridge_id="agc-sender",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        sender_parent_pid=None,
        peer_id="claude_code",
        peer_agent_instance_id=drive_smoke._RECIPIENT_AGI,  # noqa: SLF001
        content=[TextPart(type="text", text="ping")],
    )


def case_a_send_returns_before_its_drive() -> None:
    drives = _BlockingDrives()
    try:
        state = _managed_state()
        started = time.monotonic()
        outcome = _dispatch_send(state, _MessagingService())
        elapsed = time.monotonic() - started
        payload = outcome.to_payload()
        _check(
            elapsed < SEND_BOUND_S,
            f"A: a send with a {DRIVE_S:.0f}s drive returned in {elapsed:.3f}s "
            f"(< {SEND_BOUND_S}s)",
        )
        _check(
            payload["drive_on_delivery"] == "driver_scheduled",
            f"A: the send reports driver_scheduled (got {payload['drive_on_delivery']!r})",
        )
        _check(drives.wait_started(1, 2.0), "A: the drive itself started on the executor")
        pending = delivery_drive.finished_drive_outcome(outcome.message_id, timeout_s=0.05)
        _check(pending is None, "A: the drive had not finished when the send returned")
        drives.release.set()
        finished = delivery_drive.finished_drive_outcome(outcome.message_id, timeout_s=5.0)
        _check(
            finished is not None and finished[0] == "driver_sent",
            f"A: the finished drive is recorded as driver_sent (got {finished!r})",
        )
    finally:
        drives.close()


async def case_b_executor_is_bounded_and_named() -> None:
    drives = _BlockingDrives()
    burst = delivery_drive.DELIVERY_DRIVE_WORKERS * 3
    try:
        state = _managed_state()
        service = _MessagingService()
        burst_sends = asyncio.ensure_future(
            asyncio.to_thread(lambda: [_dispatch_send(state, service) for _ in range(burst)]),
        )
        sent, _ = await asyncio.wait([burst_sends], timeout=SEND_BOUND_S)
        _check(
            bool(sent),
            f"B: a burst of {burst} sends with blocked drives returned within {SEND_BOUND_S}s",
        )
        if not sent:
            drives.release.set()
        outcomes = await burst_sends
        drives.wait_started(delivery_drive.DELIVERY_DRIVE_WORKERS, 2.0)
        await asyncio.sleep(0.3)
        probe_started = time.monotonic()
        try:
            await asyncio.wait_for(asyncio.to_thread(lambda: None), timeout=MAX_LAG_S)
            default_free = True
        except TimeoutError:
            default_free = False
        probe_s = time.monotonic() - probe_started
        with drives.lock:
            peak, names = drives.peak, set(drives.thread_names)
        _check(
            peak == delivery_drive.DELIVERY_DRIVE_WORKERS,
            f"B: {burst} blocked drives ran at most {peak} at once "
            f"(bound {delivery_drive.DELIVERY_DRIVE_WORKERS})",
        )
        _check(
            bool(names)
            and all(n.startswith(delivery_drive.DELIVERY_DRIVE_THREAD_PREFIX) for n in names),
            f"B: drives ran only on named delivery-drive threads ({sorted(names)})",
        )
        _check(
            default_free,
            f"B: asyncio's default executor stayed free during the burst ({probe_s:.3f}s)",
        )
        drives.release.set()
        finished = [
            delivery_drive.finished_drive_outcome(o.message_id, timeout_s=10.0) for o in outcomes
        ]
        _check(
            all(f is not None and f[0] == "driver_sent" for f in finished),
            f"B: all {burst} queued drives finished driver_sent once released",
        )
    finally:
        drives.close()


# ---------------------------------------------------------------------------
# Route fixtures: a real bridge manager, a fake registry/service/surface.
# ---------------------------------------------------------------------------


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _solet: "sess-arb",
        idle_timeout_s=600,
        max_pending_events=100,
        long_poll_timeout_s=5,
    )


class _Registry:
    """Peer-registry stand-in; ``slow_s`` makes every DB-shaped call slow."""

    def __init__(self, *, slow_s: float = 0.0) -> None:
        self.slow_s = slow_s
        self.bindings: dict[str, BridgeBinding] = {}
        self.events: list[tuple[str, str, float]] = []
        self._lock = threading.Lock()

    def add(self, bridge_id: str, agent_instance_id: str) -> None:
        self.bindings[bridge_id] = BridgeBinding(
            bridge_id=bridge_id,
            agent_id="claude_code",
            agent_instance_id=agent_instance_id,
            session_label=agent_instance_id,
            parent_pid=None,
        )

    def record(self, what: str, who: str) -> None:
        with self._lock:
            self.events.append((what, who, time.monotonic()))

    def list_agent_ids(self) -> dict[str, list[BridgeBinding]]:
        time.sleep(self.slow_s)
        return {"claude_code": list(self.bindings.values())}

    def touch_binding(self, agent_instance_id: str) -> int:
        del agent_instance_id
        return 1

    def agent_session_id_for_instance(self, agent_instance_id: str) -> str:
        del agent_instance_id
        return ""

    def unregister(self, bridge_id: str) -> int:
        del bridge_id
        time.sleep(self.slow_s)
        return 0

    def register(self, binding: BridgeBinding, *, is_live: object = None) -> str:
        del is_live
        self.record("register", binding.agent_instance_id)
        return binding.session_label


class _SlowService:
    def __init__(self, slow_s: float) -> None:
        self.slow_s = slow_s

    def peer_inbox(self, request: Any) -> Any:  # noqa: ANN401, ARG002
        time.sleep(self.slow_s)
        raise AgentMessagingError("slow inbox stand-in")

    def list_undelivered_for_instance(self, **_kwargs: object) -> list[object]:
        time.sleep(self.slow_s)
        return []

    def mark_delivered_for_instance(self, **_kwargs: object) -> bool:
        time.sleep(self.slow_s)
        return True


class _SlowSurface:
    def __init__(self, slow_s: float) -> None:
        self.slow_s = slow_s

    def process_search(self, **_kwargs: object) -> dict[str, object]:
        time.sleep(self.slow_s)
        return {"results": []}

    def process_schema(self, **_kwargs: object) -> dict[str, object]:
        time.sleep(self.slow_s)
        return {}

    def process_call(self, **_kwargs: object) -> dict[str, object]:
        time.sleep(self.slow_s)
        return {}

    def process_result(self, **_kwargs: object) -> dict[str, object]:
        time.sleep(self.slow_s)
        return {}

    def download(self, **_kwargs: object) -> object:
        time.sleep(self.slow_s)
        raise BridgeError("bridge.blob_not_found", "slow download stand-in")


def _build_app(
    bridge_manager: BridgeSessionManager,
    registry: _Registry,
    *,
    slow_s: float = 0.0,
    autonomic_on_register: Callable[..., str] | None = None,
    state_service: Any | None = None,
) -> FastAPI:
    app = FastAPI()
    http_routes.register_routes(
        app,
        bridge_manager=bridge_manager,
        peer_registry=registry,  # type: ignore[arg-type]
        platform_surface=_SlowSurface(slow_s),  # type: ignore[arg-type]
        agent_messaging_service=_SlowService(slow_s),
        config={"long_poll_timeout_seconds": 5},
        autonomic_on_register=autonomic_on_register,
        state_service=state_service,
    )
    return app


async def case_c_long_polls_not_starved_by_send_burst() -> None:
    sends = 40
    drives = _BlockingDrives()
    service = _MessagingService()
    bridge_manager = _bridge_manager()
    registry = _Registry()
    app = _build_app(bridge_manager, registry)
    pollers = [bridge_manager.open(solet_name="") for _ in range(6)]
    for index, bridge in enumerate(pollers):
        registry.add(bridge.bridge_id, f"agi-poller-{index}")

    def _send_impl(**_kwargs: object) -> JSONResponse:
        outcome = _dispatch_send(_managed_state(), service)
        return JSONResponse(content=outcome.to_payload(), status_code=200)

    try:
        with _patched(http_routes, "_peer_send_impl", _send_impl):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL, timeout=30.0,
            ) as client:
                started = time.monotonic()
                send_tasks = [
                    asyncio.ensure_future(
                        client.post(
                            "/api/v1/bridge/agc-any/peer/send",
                            json={"peer_id": "claude_code", "content": []},
                        ),
                    )
                    for _ in range(sends)
                ]
                done, _pending = await asyncio.wait(send_tasks, timeout=SEND_BOUND_S * 3)
                send_s = time.monotonic() - started
                _check(
                    len(done) == sends,
                    f"C: {len(done)}/{sends} concurrent sends with blocked drives answered "
                    f"within {send_s:.2f}s",
                )
                poll_tasks = [
                    asyncio.ensure_future(
                        client.get(f"/api/v1/bridge/{bridge.bridge_id}/events?after=-1"),
                    )
                    for bridge in pollers
                ]
                await asyncio.sleep(PROBE_AT_S)
                appended = time.monotonic()
                for bridge in pollers:
                    await asyncio.to_thread(
                        bridge_manager.append_event, bridge.bridge_id, "post_message", "hi",
                    )
                polls_done, _ = await asyncio.wait(poll_tasks, timeout=MAX_LAG_S)
                lag = time.monotonic() - appended
                # The events are appended through ``asyncio.to_thread`` on
                # purpose: in production a send's append_event runs on the
                # route's request-offload thread, so a default executor
                # saturated by blocked drives starves exactly this path.
                _check(
                    len(polls_done) == len(pollers)
                    and all(t.result().status_code == 200 for t in polls_done)
                    and lag < MAX_LAG_S,
                    f"C: {len(polls_done)}/{len(pollers)} /events long-polls answered "
                    f"{lag:.3f}s (< {MAX_LAG_S}s) after their events, while {sends} "
                    "drives were blocked",
                )
                drives.release.set()
                await asyncio.wait(send_tasks + poll_tasks, timeout=DRIVE_S + 5)
    finally:
        drives.close()


async def case_d_register_lock_not_held_across_handover_drive() -> None:
    drives = _BlockingDrives()
    bridge_manager = _bridge_manager()
    registry = _Registry()
    state = _managed_state()

    def _autonomic(**kwargs: object) -> str:
        if kwargs.get("agent_instance_id") != "agi-reg-a":
            return "held"
        role_claim.send_handover_notice(
            bridge_manager=drive_smoke._FakeBridgeManager(),  # type: ignore[arg-type]  # noqa: SLF001
            peer_registry=drive_smoke._FakePeerRegistry(),  # type: ignore[arg-type]  # noqa: SLF001
            agent_messaging_service=_MessagingService(),
            state_service=state,
            peer_id="claude_code",
            peer_agent_instance_id=drive_smoke._RECIPIENT_AGI,  # noqa: SLF001
            prose="role handover",
            kind="displaced",
        )
        return "vacancy_fill"

    app = _build_app(bridge_manager, registry, autonomic_on_register=_autonomic)
    bridge_a = bridge_manager.open(solet_name="")
    bridge_b = bridge_manager.open(solet_name="")

    def _body(agi: str) -> dict[str, object]:
        return {"agent_id": "codex", "agent_instance_id": agi, "provides_inference": True}

    try:
        with (
            _patched(http_routes, "_session_id_conflict", lambda **_k: None),
            _patched(http_routes, "_orphan_session_id_downgrade", lambda **_k: False),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL, timeout=30.0,
            ) as client:
                task_a = asyncio.ensure_future(
                    client.post(
                        f"/api/v1/bridge/{bridge_a.bridge_id}/peer/register",
                        json=_body("agi-reg-a"),
                    ),
                )
                await asyncio.to_thread(drives.wait_started, 1, 2.0)
                started_b = time.monotonic()
                task_b = asyncio.ensure_future(
                    client.post(
                        f"/api/v1/bridge/{bridge_b.bridge_id}/peer/register",
                        json=_body("agi-reg-b"),
                    ),
                )
                done, _ = await asyncio.wait([task_a, task_b], timeout=SEND_BOUND_S)
                b_s = time.monotonic() - started_b
                _check(
                    task_b in done and task_b.result().status_code == 200,
                    f"D: an unrelated registration completed {b_s:.3f}s after it was "
                    "issued, while the first one's handover drive was still blocked",
                )
                _check(
                    task_a in done and task_a.result().json().get("autonomic") == "vacancy_fill",
                    "D: the registration whose handover scheduled a 10s drive also returned",
                )
                drives.release.set()
                await asyncio.wait([task_a, task_b], timeout=DRIVE_S + 5)
    finally:
        drives.close()


async def _register_racing(
    writer_for: Callable[[_Registry], Callable[[], None]],
) -> list[tuple[str, str, float]]:
    """HTTP register (check held REGISTER_HOLD_S) racing one streamable write."""
    bridge_manager = _bridge_manager()
    registry = _Registry()
    app = _build_app(bridge_manager, registry)
    bridge = bridge_manager.open(solet_name="")

    def _slow_check(**_kwargs: object) -> None:
        registry.record("check_start", "http")
        time.sleep(REGISTER_HOLD_S)
        registry.record("check_end", "http")

    with (
        _patched(http_routes, "_session_id_conflict", _slow_check),
        _patched(http_routes, "_orphan_session_id_downgrade", lambda **_k: False),
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            http = asyncio.ensure_future(
                client.post(
                    f"/api/v1/bridge/{bridge.bridge_id}/peer/register",
                    json={"agent_id": "codex", "agent_instance_id": "agi-http"},
                ),
            )
            await asyncio.sleep(0.1)
            await asyncio.gather(http, asyncio.to_thread(writer_for(registry)))
    return registry.events


def _relabel_writer(registry: _Registry) -> Callable[[], None]:
    session = SimpleNamespace(
        authenticated_agent_id="claude_code",
        agent_id="claude_code",
        session_label="Phone",
        authenticated_session_label="Phone",
        bridge_id="agc-streamable",
        agent_instance_id="agi-relabel",
        binding=None,
    )
    context = SimpleNamespace(peer_registry=registry)

    def _write() -> None:
        streamable_dispatch._tool_peer_register(  # noqa: SLF001 -- the relabel under test
            {"agent_id": "claude_code"},
            session=session,  # type: ignore[arg-type]
            context=context,  # type: ignore[arg-type]
        )

    return _write


def _allocate_writer(registry: _Registry) -> Callable[[], None]:
    manager = StreamableSessionManager(
        bridge_manager=_bridge_manager(),
        peer_registry=registry,  # type: ignore[arg-type]
    )
    claim = BearerClaim(
        agent_id="claude_code",
        agent_instance_id="agi-allocate",
        issued_at=datetime.now(UTC),
    )

    def _write() -> None:
        manager.allocate(claim)

    return _write


def _ordered_after_http_register(events: list[tuple[str, str, float]], who: str) -> bool:
    by_key = {(what, agent): at for what, agent, at in events}
    http_register = by_key.get(("register", "agi-http"))
    other = by_key.get(("register", who))
    check_end = by_key.get(("check_end", "http"))
    if http_register is None or other is None or check_end is None:
        return False
    return other >= http_register >= check_end


async def case_e_streamable_writes_share_the_register_lock() -> None:
    relabel = await _register_racing(_relabel_writer)
    _check(
        _ordered_after_http_register(relabel, "agi-relabel"),
        "E: a streamable peer_register relabel waits for the HTTP register's "
        f"check-then-act ({[(w, a) for w, a, _ in relabel]})",
    )
    allocate = await _register_racing(_allocate_writer)
    _check(
        _ordered_after_http_register(allocate, "agi-allocate"),
        "E: a streamable session allocation waits for the HTTP register's "
        f"check-then-act ({[(w, a) for w, a, _ in allocate]})",
    )


# ---------------------------------------------------------------------------
# F: the route table under a slow database.
# ---------------------------------------------------------------------------


async def _health_lag_during(
    client: httpx.AsyncClient,
    slow_request: Awaitable[httpx.Response],
    probe_path: str,
) -> float:
    started = time.perf_counter()
    slow_task = asyncio.ensure_future(slow_request)
    await asyncio.sleep(PROBE_AT_S)
    response = await client.get(probe_path)
    lag = time.perf_counter() - (started + PROBE_AT_S)
    if response.status_code >= 500:
        raise AssertionError(f"{probe_path} answered {response.status_code}")
    await slow_task
    return lag


async def case_f_bridge_routes_keep_the_loop_free() -> None:
    bridge_manager = _bridge_manager()
    registry = _Registry(slow_s=SLOW_S)
    app = _build_app(bridge_manager, registry, slow_s=SLOW_S)
    bridges = {name: bridge_manager.open(solet_name="") for name in _BRIDGE_ROUTES}
    for name, bridge in bridges.items():
        registry.add(bridge.bridge_id, f"agi-{name.replace('/', '-')}")
    bridge_manager.append_event(bridges["events"].bridge_id, "post_message", "queued")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL, timeout=30.0) as client:
        for name, (method, suffix, body) in _BRIDGE_ROUTES.items():
            path = f"/api/v1/bridge/{bridges[name].bridge_id}/{suffix}"
            request = client.post(path, json=body) if method == "POST" else client.get(path)
            lag = await _health_lag_during(client, request, "/api/v1/bridge/health")
            _check(
                lag < MAX_LAG_S,
                f"F: {method} {suffix.split('?')[0]} — /health lag {lag:.3f}s "
                f"during a {SLOW_S}s DB call",
            )


_BRIDGE_ROUTES: dict[str, tuple[str, str, dict[str, object] | None]] = {
    "events": ("GET", "events?after=-1", None),
    "process/search": ("POST", "process/search", {"query": "x"}),
    "process/schema": ("POST", "process/schema", {"process_key": "a::b::c"}),
    "process/call": ("POST", "process/call", {"process_key": "a::b::c", "arguments": {}}),
    "process/result": ("GET", "process/result/act-1", None),
    "download": ("GET", "download/blob-1", None),
    "peer/list": ("GET", "peer/list", None),
    "current_identity": ("GET", "current_identity", None),
    "peer/inbox": ("GET", "peer/inbox", None),
    "peer/drain": ("POST", "peer/drain", {}),
    "peer/delivered": ("POST", "peer/delivered", {"external_id": "x", "recipient_key": "r"}),
    "close": ("POST", "close", {}),
}


class _SlowVerifier:
    def verify(self, header: str | None) -> object:
        del header
        time.sleep(SLOW_S)
        raise BearerAuthError("bearer.missing", "slow verifier stand-in")


async def _loop_lag_during(slow: Awaitable[object]) -> float:
    started = time.perf_counter()
    slow_task = asyncio.ensure_future(slow)
    await asyncio.sleep(PROBE_AT_S)
    lag = time.perf_counter() - (started + PROBE_AT_S)
    await slow_task
    return lag


async def case_f_streamable_verify_keeps_the_loop_free() -> None:
    request = SimpleNamespace(
        headers={},
        url=SimpleNamespace(path="/mcp"),
        base_url="http://bridge.test/",
    )
    common: dict[str, Any] = {
        "bearer_verifier": _SlowVerifier(),
        "allowed_origins": (),
        "resource_metadata_url": "http://bridge.test/.well-known/x",
    }
    handlers: dict[str, Callable[[], Awaitable[object]]] = {
        "GET": lambda: streamable_router._handle_get(  # noqa: SLF001
            request, session_manager=None, bridge_manager=None, **common,  # type: ignore[arg-type]
        ),
        "POST": lambda: streamable_router._handle_post(  # noqa: SLF001
            request, session_manager=None, dispatch_ctx=None, **common,  # type: ignore[arg-type]
        ),
        "DELETE": lambda: streamable_router._handle_delete(  # noqa: SLF001
            request, session_manager=None, **common,  # type: ignore[arg-type]
        ),
    }
    for method, handler in handlers.items():
        lag = await _loop_lag_during(handler())
        _check(
            lag < MAX_LAG_S,
            f"F: streamable {method} bearer verify — loop lag {lag:.3f}s "
            f"during a {SLOW_S}s client-registry read",
        )


class _SlowClientStore:
    def lookup_oauth_client(self, client_id: str) -> None:
        del client_id
        time.sleep(SLOW_S)

    def verify_oauth_client_credentials(self, client_id: str, client_secret: str) -> None:
        del client_id, client_secret
        time.sleep(SLOW_S)

    def record_oauth_client_token_use(self, client_id: str, **_kwargs: object) -> bool:
        del client_id
        return True


async def case_f_oauth_keeps_the_loop_free() -> None:
    endpoints = streamable_oauth.build_endpoints(issuer=BASE_URL, streamable_path="/mcp")
    app = FastAPI()
    app.include_router(
        streamable_oauth.build_oauth_router(
            endpoints=endpoints,
            client_store=_SlowClientStore(),  # type: ignore[arg-type]
            hmac_key=b"k" * 32,
        ),
    )
    probe = streamable_oauth.OAUTH_AUTHORIZATION_SERVER_PATH
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        authorize = client.get(
            streamable_oauth.OAUTH_AUTHORIZE_PATH,
            params={
                "client_id": "c",
                "response_type": "code",
                "redirect_uri": "http://x/cb",
                "code_challenge": "y" * 43,
                "code_challenge_method": "S256",
            },
        )
        lag = await _health_lag_during(client, authorize, probe)
        _check(lag < MAX_LAG_S, f"F: GET /authorize — metadata lag {lag:.3f}s during a slow lookup")
        token = client.post(
            streamable_oauth.OAUTH_TOKEN_PATH,
            data={"grant_type": "client_credentials", "client_id": "c", "client_secret": "s"},
        )
        lag = await _health_lag_during(client, token, probe)
        _check(lag < MAX_LAG_S, f"F: POST /oauth/token — metadata lag {lag:.3f}s during a slow verify")


async def case_g_paste_wait_ends_on_landed_composer_row() -> None:
    """iss_ab1f0a21: a BUSY pane (spinner changes every sample) whose composer
    row shows the pasted notice gets its Enter after two matching samples,
    long before the 10 s fail-open — driven through the real ``send()``."""
    clock = [0.0]
    pasted = [False]
    samples_after_paste = [0]
    matching_before_enter: list[int] = []
    enter_at: list[float] = []
    notice = "delivery waiting from Sender — drain peer_inbox"

    def _run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        verb = argv[1]
        if verb == "paste-buffer":
            pasted[0] = True
        if verb == "capture-pane":
            row = ""
            if pasted[0]:
                samples_after_paste[0] += 1
                # Rendered from the second post-paste sample on (render lag).
                row = notice if samples_after_paste[0] >= 2 else ""
            spinner = f"✶ Working… ({clock[0]:.1f}s · ↓ {samples_after_paste[0]} tokens)"
            return SimpleNamespace(returncode=0, stdout=f"{spinner}\n\n❯\xa0{row}\n")
        if verb == "send-keys":
            enter_at.append(clock[0])
            matching_before_enter.append(max(0, samples_after_paste[0] - 1))
        return SimpleNamespace(returncode=0, stdout="")

    def _sleep(seconds: float) -> None:
        clock[0] += seconds

    channel = _TmuxSendKeysDriverChannel(
        tmux_bin="tmux", session="fake-busy-pane", run_fn=_run,
        sleep_fn=_sleep, now_fn=lambda: clock[0],
    )
    channel.send(notice)
    _check(len(enter_at) == 1, f"G: exactly one Enter was sent ({len(enter_at)})")
    _check(
        bool(enter_at) and enter_at[0] < 2.0,
        f"G: Enter came {enter_at[0] if enter_at else -1:.1f}s (virtual) after the "
        "paste on a pane that never stabilizes (< 2.0s, timeout 10s)",
    )
    _check(
        bool(matching_before_enter) and matching_before_enter[0] >= 2,
        "G: Enter waited for >= 2 samples showing the landed composer row "
        f"({matching_before_enter[0] if matching_before_enter else 0})",
    )


def _check_recheck_outcomes(
    finished: list[tuple[str, str | None] | None], *, sends: int, workers: int, pane_sends: int,
) -> None:
    kinds = [f[0] if f is not None else "unfinished" for f in finished]
    _check(
        kinds.count("driver_sent") == workers and pane_sends == workers,
        f"H: only the {workers} drives already in the pane reached it "
        f"(driver_sent={kinds.count('driver_sent')}, pane sends={pane_sends})",
    )
    _check(
        kinds.count("ineligible_state") == sends - workers,
        f"H: the {sends - workers} drives queued across the park recorded "
        f"ineligible_state ({kinds.count('ineligible_state')}; {sorted(set(kinds))})",
    )
    stale = [f for f in finished if f is not None and f[0] == "ineligible_state"]
    _check(
        bool(stale) and all("re-check" in (f[1] or "") for f in stale),
        "H: each refused queued drive records the run-time re-check as its detail",
    )


async def case_h_queued_drive_rechecks_lifecycle() -> None:
    """Review D1: 20 drives queued to one live recipient, which is then
    parked. Drives already in the pane finish; every drive still queued
    re-checks the row and records ineligible_state, never driver_sent."""
    drives = _BlockingDrives()
    sends = 20
    workers = delivery_drive.DELIVERY_DRIVE_WORKERS
    try:
        state = _managed_state()
        service = _MessagingService()
        outcomes = [_dispatch_send(state, service) for _ in range(sends)]
        drives.wait_started(workers, 2.0)
        transition_lifecycle_state(
            state,
            agent_instance_id=drive_smoke._RECIPIENT_AGI,  # noqa: SLF001
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_PARKED,
            directed_by="test:async-route-blocking",
        )
        drives.release.set()
        finished = [
            delivery_drive.finished_drive_outcome(o.message_id, timeout_s=10.0)
            for o in outcomes
        ]
        _check_recheck_outcomes(finished, sends=sends, workers=workers, pane_sends=drives.started)
    finally:
        drives.close()


def _check_queue_refusals(outcomes: list[Any], *, limit: int, extra: int) -> None:
    kinds = [o.to_payload()["drive_on_delivery"] for o in outcomes]
    _check(
        kinds.count("driver_scheduled") == limit and kinds.count("driver_queue_full") == extra,
        f"I: {limit + extra} sends -> {kinds.count('driver_scheduled')} scheduled, "
        f"{kinds.count('driver_queue_full')} driver_queue_full (cap {limit})",
    )
    refused = [o for o in outcomes if o.to_payload()["drive_on_delivery"] == "driver_queue_full"]
    record = (
        delivery_drive.finished_drive_outcome(refused[0].message_id, timeout_s=0.1)
        if refused
        else None
    )
    _check(
        record is not None and record[0] == "driver_queue_full" and "limit" in (record[1] or ""),
        f"I: a refused drive is recorded with its typed outcome ({record!r})",
    )


async def case_i_queue_is_capped_and_visible() -> None:
    """Review D1: beyond DELIVERY_DRIVE_QUEUE_LIMIT pending drives a send is
    refused with a recorded driver_queue_full; /health shows the count."""
    drives = _BlockingDrives()
    limit = delivery_drive.DELIVERY_DRIVE_QUEUE_LIMIT
    extra = 5
    bridge_manager = _bridge_manager()
    app = _build_app(bridge_manager, _Registry())
    try:
        state = _managed_state()
        service = _MessagingService()
        outcomes = [_dispatch_send(state, service) for _ in range(limit + extra)]
        _check_queue_refusals(outcomes, limit=limit, extra=extra)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            health = (await client.get("/api/v1/bridge/health")).json()
            busy = health.get("delivery_drives", {})
            drives.release.set()
            for o in outcomes:
                delivery_drive.finished_drive_outcome(o.message_id, timeout_s=10.0)
            idle = (await client.get("/api/v1/bridge/health")).json().get("delivery_drives", {})
        _check(
            busy.get("pending") == limit and busy.get("limit") == limit,
            f"I: /health reports the pending drives at the cap ({busy})",
        )
        _check(idle.get("pending") == 0, f"I: /health reports 0 pending once drained ({idle})")
    finally:
        drives.close()


async def case_j_shut_down_executor_is_a_recorded_outcome() -> None:
    """Review D6: a send after the drive executor shut down (interpreter exit)
    returns a recorded driver_unavailable instead of raising."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    drive_smoke._install_fake_host()  # noqa: SLF001
    closed = ThreadPoolExecutor(max_workers=1)
    closed.shutdown()
    try:
        state = _managed_state()
        with _patched(delivery_drive, "DELIVERY_DRIVE_EXECUTOR", closed):
            try:
                outcome = _dispatch_send(state, _MessagingService())
                raised = ""
            except Exception as exc:  # noqa: BLE001 — the point is that it must not raise
                outcome, raised = None, f"{type(exc).__name__}: {exc}"
        _check(not raised, f"J: a send after executor shutdown does not raise ({raised})")
        payload = outcome.to_payload() if outcome is not None else {}
        record = (
            delivery_drive.finished_drive_outcome(outcome.message_id, timeout_s=0.1)
            if outcome is not None
            else None
        )
        _check(
            payload.get("drive_on_delivery") == "driver_unavailable"
            and record is not None
            and "shut down" in (record[1] or ""),
            f"J: it reports and records driver_unavailable ({payload.get('drive_on_delivery')!r}, "
            f"{record!r})",
        )
        _check(
            delivery_drive.drive_queue_snapshot()["pending"] == 0,
            "J: the refused drive does not leak a pending slot",
        )
    finally:
        drive_smoke._remove_fake_host()  # noqa: SLF001



def _registration_harness() -> RegistrationHarness:
    return RegistrationHarness(_check, _bridge_manager, _Registry, _build_app, _patched)


async def case_k_host_qualification_does_not_hold_registration_lock() -> None:
    """OS qualification leaves the shared lock free for unrelated requests."""
    await run_concurrency(_registration_harness())


async def case_l_host_qualification_preserves_registration_outcomes() -> None:
    """Effective identity, error precedence and writes keep their contracts."""
    await run_outcomes(_registration_harness())


async def _a() -> None:
    case_a_send_returns_before_its_drive()


async def _main() -> None:
    cases: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("A", _a),
        ("B", case_b_executor_is_bounded_and_named),
        ("C", case_c_long_polls_not_starved_by_send_burst),
        ("D", case_d_register_lock_not_held_across_handover_drive),
        ("E", case_e_streamable_writes_share_the_register_lock),
        ("F", case_f_bridge_routes_keep_the_loop_free),
        ("F", case_f_streamable_verify_keeps_the_loop_free),
        ("F", case_f_oauth_keeps_the_loop_free),
        ("G", case_g_paste_wait_ends_on_landed_composer_row),
        ("H", case_h_queued_drive_rechecks_lifecycle),
        ("I", case_i_queue_is_capped_and_visible),
        ("J", case_j_shut_down_executor_is_a_recorded_outcome),
        ("K", case_k_host_qualification_does_not_hold_registration_lock),
        ("L", case_l_host_qualification_preserves_registration_outcomes),
    ]
    for label, case in cases:
        try:
            await case()
        except Exception as exc:  # noqa: BLE001 — a case that raises is a named failure
            _check(False, f"{label}: {case.__name__} raised {type(exc).__name__}: {exc}")


def main() -> int:
    print("=== async route blocking class smoke (wgr_3ee58d5e) ===")
    asyncio.run(_main())
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
