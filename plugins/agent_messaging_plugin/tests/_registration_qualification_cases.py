"""Request-local host qualification cases for the async route smoke.

OS facts and state-provider envelopes are fixtures. The HTTP route, worker
thread, shared registration lock, qualifiers, identity resolution and state
writers are real. No fixture probes or mutates a live operator registration.
The parent smoke supplies its counters and app fixtures explicitly; importing
this module never imports the parent smoke or initializes a second counter.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from typing import Any

import httpx
import operator_codex_launcher_smoke as host_smoke
from _real_state_fake import RealShapeState
from fastapi.responses import JSONResponse

from agent_messaging_plugin import http_routes
from agent_messaging_plugin import operator_codex_registration as registration
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager
from agent_messaging_plugin.models import BridgeBinding


@dataclass(frozen=True, slots=True)
class RegistrationHarness:
    """Borrow the registered smoke's fixtures and named assertion recorder."""

    check: Callable[[object, str], None]
    manager: Callable[[], BridgeSessionManager]
    registry: Callable[[], Any]
    build_app: Callable[..., Any]
    patched: Callable[..., AbstractContextManager[None]]


# Host qualification fixtures substitute OS observations, never live registrations.


def _host_body(runtime: str) -> dict[str, object]:
    body = host_smoke._body() if runtime == "codex" else host_smoke._claude_body()  # noqa: SLF001
    if runtime == "implicit":
        body["operator_tmux_host"] = ""
        body["watcher_declared"] = False
    return body


def _host_probe(runtime: str, argv: list[str]) -> str:
    probe = host_smoke._probe if runtime == "codex" else host_smoke._claude_probe  # noqa: SLF001
    return probe(argv)


def _host_patches(harness: RegistrationHarness, stack: ExitStack, probe: Callable[[list[str]], str]) -> None:
    stack.enter_context(harness.patched(registration, "_output", probe))
    stack.enter_context(harness.patched(registration.shutil, "which", lambda _name: "/bin/tmux"))
    stack.enter_context(harness.patched(http_routes, "claude_operator_tmux_host_ref", lambda _label: host_smoke.CLAUDE_HOST))


async def _blocked_host_registration(harness: RegistrationHarness, runtime: str) -> None:
    manager, registry, state = harness.manager(), harness.registry(), RealShapeState()
    app = harness.build_app(manager, registry, state_service=state)
    bridges = [manager.open(solet_name="") for _ in range(2)]
    entered, release = threading.Event(), threading.Event()

    def probe(argv: list[str]) -> str:
        if "list-panes" in argv and not entered.is_set():
            entered.set()
            release.wait(10.0)
        return _host_probe(runtime, argv)

    with ExitStack() as stack:
        _host_patches(harness, stack, probe)
        stack.enter_context(harness.patched(http_routes, "_session_id_conflict", lambda **_kw: None))
        stack.enter_context(harness.patched(http_routes, "_orphan_session_id_downgrade", lambda **_kw: False))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge.test") as client:
            task_a = asyncio.create_task(client.post(
                f"/api/v1/bridge/{bridges[0].bridge_id}/peer/register", json=_host_body(runtime),
            ))
            tasks = [task_a]
            try:
                reached = await asyncio.to_thread(entered.wait, 2.0)
                harness.check(reached, f"K: {runtime} list-panes entered before unrelated registration")
                if not reached:
                    raise AssertionError("INVALID K: qualification probe was not reached")
                task_b = asyncio.create_task(client.post(
                    f"/api/v1/bridge/{bridges[1].bridge_id}/peer/register",
                    json={"agent_id": "codex", "agent_instance_id": "agi-" + "c" * 32},
                ))
                tasks.append(task_b)
                started = time.monotonic()
                done, _ = await asyncio.wait([task_b], timeout=1.0)
                print(f"  K reached={reached} runtime={runtime} elapsed={time.monotonic() - started:.3f}s release={release.is_set()}")
                harness.check(task_b in done and task_b.result().status_code == 200,
                       'K: unrelated registration completes while host qualification is blocked')
                harness.check(not release.is_set(), f"K: {runtime} qualification remained blocked at check")
            finally:
                release.set()
                await asyncio.gather(*tasks)
            result = task_a.result()
            harness.check(result.status_code == 200, f"K: {runtime} released registration succeeds ({result.text})")
            key = "operator_codex_host" if runtime == "codex" else "operator_claude_host"
            expected_host = host_smoke.HOST if runtime == "codex" else host_smoke.CLAUDE_HOST
            harness.check(result.json().get(key, {}).get("host_ref") == expected_host,
                   f"K: {runtime} response preserves qualified host")
            row = registration.read_managed_session(state, host_smoke.INSTANCE)  # type: ignore[arg-type]
            harness.check(row["host"] == "tmux" and row["agent_session_id"] == host_smoke.SESSION,
                   f"K: {runtime} state preserves host and session")


async def run_concurrency(harness: RegistrationHarness) -> None:
    """A delayed Codex/Claude OS probe cannot block an unrelated registration."""
    for runtime in ("codex", "claude", "implicit"):
        await _blocked_host_registration(harness, runtime)


def _real_host_registry() -> Any:
    return host_smoke.PeerRegistry(bindings_store=host_smoke.open_store(
        host_smoke.get_peer_binding_schema(), namespace=host_smoke.PEER_BINDING_NAMESPACE,
        backend="in_memory",
    ))


def _seed_host_state(state: RealShapeState, runtime: str, variant: str) -> None:
    if variant not in {"worker", "noneligible"}:
        return
    host_smoke.insert_managed_session(state, host_smoke.ManagedSessionSpec(  # type: ignore[arg-type]
        agent_instance_id=host_smoke.INSTANCE, lane_id="worker" if variant == "worker" else "",
        assignment=host_smoke.fixture_assignment() if variant == "worker" else None,
        work_class="analysis_deliverable", budget_line="budget",
        host="tmux" if variant == "worker" else "operator",
        provisioning_mode="worktree" if variant == "worker" else registration.OPERATOR_CHECKOUT_MODE,
        agent_runtime="codex" if runtime == "codex" else "claude_code",
    ))
    registration.backfill_registration(state, agent_instance_id=host_smoke.INSTANCE,  # type: ignore[arg-type]
                                       agent_id="codex", agent_session_id=host_smoke.SESSION)
    if variant == "noneligible":
        state.update_state(registration.AGENT_ROLE_BINDING_NAMESPACE,
                           {"table": registration.TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": host_smoke.INSTANCE}},
                           {"lifecycle_state": "terminated"})


def _outcome_body(runtime: str, variant: str) -> dict[str, object]:
    body = _host_body(runtime)
    if variant == "empty":
        body["agent_session_id"] = ""
    if variant == "unclaimed":
        body["agent_session_id"] = http_routes.UNCLAIMED_SESSION_ID
    return body


def _outcome_probe(runtime: str, variant: str, argv: list[str]) -> str:
    if variant in {"probe", "conflict", "bridge", "declaration"} and "list-panes" in argv:
        raise registration.OperatorHostQualificationError("fixture host probe failed")
    if variant in {"session", "session_later"} and argv[-1] == "AGENT_SESSION_ID":
        return "AGENT_SESSION_ID=ases-wrong"
    if variant == "session_later" and argv[-1] == "SOLET_NAME":
        raise registration.OperatorHostQualificationError("fixture later probe failed")
    return _host_probe(runtime, argv)


def _observe_state_writes(harness: RegistrationHarness, stack: ExitStack, state: RealShapeState, variant: str, locks: list[bool]) -> None:
    original_update = state.update_state

    def update(namespace: str, query: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        locks.append(http_routes.PEER_REGISTRATION_LOCK.locked())
        if variant == "cas" and values.get("host") == "tmux":
            return {"action_status": "completed", "data": {"result": {"updated": 0}}}
        result = original_update(namespace, query, values)
        if variant == "readback" and values.get("host") == "tmux":
            original_update(namespace, {"table": query["table"], "filters": {"agent_instance_id": host_smoke.INSTANCE}}, {"host": "operator"})
        return result

    original_upsert = state.upsert_state

    def upsert(namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        locks.append(http_routes.PEER_REGISTRATION_LOCK.locked())
        return original_upsert(namespace, query)

    stack.enter_context(harness.patched(state, "update_state", update))
    stack.enter_context(harness.patched(state, "upsert_state", upsert))


def _expected_host_metadata(runtime: str, degraded: bool) -> dict[str, str]:
    if degraded:
        return {}
    metadata = host_smoke.METADATA if runtime == "codex" else {"model": "", "effort": "", "lane_repo_root": "/tmp"}
    return {"host": "tmux", "host_ref": host_smoke.HOST if runtime == "codex" else host_smoke.CLAUDE_HOST,
            "agent_runtime": "codex" if runtime == "codex" else "claude_code",
            "qualification": "verified_local_watcher", **metadata}


def _check_host_success(harness: RegistrationHarness, result: httpx.Response, runtime: str, state: RealShapeState, registry: Any, bridge_id: str, variant: str) -> None:
    payload = result.json()
    harness.check(result.status_code == 200, f"L: {runtime}/{variant} HTTP 200 ({result.text})")
    key = "operator_codex_host" if runtime == "codex" else "operator_claude_host"
    harness.check(set(payload) == {"agent_id", "agent_instance_id", "agent_session_id", "session_label", "parent_pid",
                            "bridge_id", "status", "operator_codex_host", "operator_claude_host", "self_refresh",
                            "autonomic", "session_role_held", "session_id_downgrade"},
           f"L: {runtime}/{variant} exact response keys")
    degraded = variant in {"probe", "orphan"}
    expected_session = "" if variant == "orphan" else host_smoke.SESSION
    expected = _expected_host_metadata(runtime, degraded)
    harness.check(payload.get(key) == expected, f"L: {runtime}/{variant} exact host metadata")
    row = registration.read_managed_session(state, host_smoke.INSTANCE)  # type: ignore[arg-type]
    harness.check(row["host"] == ("operator" if degraded else "tmux") and row["agent_session_id"] == expected_session,
           f"L: {runtime}/{variant} exact state identity")
    binding = registry.list_by_bridge(bridge_id)[0]
    harness.check(binding.agent_session_id == expected_session and binding.agent_instance_id == host_smoke.INSTANCE,
           f"L: {runtime}/{variant} exact registry identity")
    harness.check(payload["agent_session_id"] == expected_session and payload["status"] == "registered"
           and payload["bridge_id"] == bridge_id and payload["session_label"] == binding.session_label,
           f"L: {runtime}/{variant} response identity agrees with registry")


def _patch_host_precedence(harness: RegistrationHarness, stack: ExitStack, variant: str, body: dict[str, object]) -> None:
    if variant == "orphan":
        stack.enter_context(harness.patched(http_routes, "_orphan_session_id_downgrade", lambda **_kw: True))
    if variant == "conflict":
        stack.enter_context(harness.patched(http_routes, "_session_id_conflict", lambda **_kw: JSONResponse(
            status_code=409, content={"code": http_routes.SESSION_ID_BOUND_TO_LIVE_SESSION, "message": "fixture live conflict"})))
    if variant == "declaration":
        body["agent_session_id"] = ""


async def _host_outcome(harness: RegistrationHarness, runtime: str, variant: str, expected_error: str) -> None:
    manager, registry, state = harness.manager(), _real_host_registry(), RealShapeState()
    _seed_host_state(state, runtime, variant)
    app = harness.build_app(manager, registry, state_service=None if variant == "state" else state)
    bridge = manager.open(solet_name="")
    bridge_id = "agc-missing" if variant == "bridge" else bridge.bridge_id
    body = _outcome_body(runtime, variant)
    if variant in {"empty", "unclaimed"}:
        registry.register(BridgeBinding(bridge_id=bridge_id, agent_id=str(body["agent_id"]),
                                        agent_instance_id=host_smoke.INSTANCE, agent_session_id=host_smoke.SESSION,
                                        session_label=str(body["session_label"]), parent_pid=701))
    probes: list[bool] = []
    writes: list[bool] = []
    warnings: list[str] = []

    def probe(argv: list[str]) -> str:
        probes.append(http_routes.PEER_REGISTRATION_LOCK.locked())
        return _outcome_probe(runtime, variant, argv)

    def warning(message: str, *args: object, **_kwargs: object) -> None:
        warnings.append(message % args)

    original_register = registry.register

    def register(*args: Any, **kwargs: Any) -> str:
        writes.append(http_routes.PEER_REGISTRATION_LOCK.locked())
        return original_register(*args, **kwargs)

    with ExitStack() as stack:
        _host_patches(harness, stack, probe)
        _observe_state_writes(harness, stack, state, variant, writes)
        stack.enter_context(harness.patched(registry, "register", register))
        stack.enter_context(harness.patched(http_routes.logger, "warning", warning))
        _patch_host_precedence(harness, stack, variant, body)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge.test") as client:
            await _measure_host_outcome(harness, client, runtime, variant, body, expected_error, state, registry, bridge_id)
    harness.check(bool(probes) and not any(probes), f"L: {runtime}/{variant} OS probes outside registration lock")
    harness.check(all(writes), f"L: {runtime}/{variant} all {len(writes)} state/registry writes under lock")
    if runtime == "implicit" and variant in {"probe", "orphan"}:
        expected_message = "fixture host probe failed" if variant == "probe" else "incomplete operator Claude watcher declaration"
        expected_warning = ("peer/register: Claude operator-host qualification failed; continuing ordinary registration "
                            f"(agi={host_smoke.INSTANCE}): {expected_message}")
        harness.check(expected_warning in warnings, f"L: {runtime}/{variant} exact fallback warning")


async def _measure_host_outcome(harness: RegistrationHarness, client: httpx.AsyncClient, runtime: str, variant: str, body: dict[str, object],
                                error: str, state: RealShapeState, registry: Any, bridge_id: str) -> None:
    path = f"/api/v1/bridge/{bridge_id}/peer/register"
    result = await client.post(path, json=body)
    if not error:
        _check_host_success(harness, result, runtime, state, registry, bridge_id, variant)
        if variant == "success":
            before = registration.read_managed_session(state, host_smoke.INSTANCE)  # type: ignore[arg-type]
            reconnect = await client.post(path, json=body)
            _check_host_success(harness, reconnect, runtime, state, registry, bridge_id, "reconnect")
            after = registration.read_managed_session(state, host_smoke.INSTANCE)  # type: ignore[arg-type]
            # Registration touches timestamps; all persistent identity/driver fields agree.
            keys = ("host", "host_ref", "agent_runtime", "agent_session_id", "agent_instance_id", "model", "effort", "lane_repo_root")
            harness.check({k: before.get(k) for k in keys} == {k: after.get(k) for k in keys},
                   f"L: {runtime} reconnect preserves exact driver row")
        return
    status, code = {"bridge": (404, "bridge_not_found"), "conflict": (409, http_routes.SESSION_ID_BOUND_TO_LIVE_SESSION)}.get(
        variant, (400, "operator_host_unqualified"),
    )
    harness.check(result.status_code == status and result.json() == {"code": code, "message": error},
           f"L: {runtime}/{variant} exact failure ({result.status_code}: {result.text})")
    harness.check(not registry.list_by_bridge(bridge_id), f"L: {runtime}/{variant} refused registration has no binding")


async def run_outcomes(harness: RegistrationHarness) -> None:
    """Pure snapshot validation retains locked effective identity and outcomes."""
    for runtime in ("codex", "claude"):
        declaration = f"incomplete operator {'Codex' if runtime == 'codex' else 'Claude'} watcher declaration"
        cases = (
            ("success", ""), ("empty", ""), ("unclaimed", ""),
            ("probe", "fixture host probe failed"), ("state", "state service is unavailable"),
            ("worker", "refusing to adopt an existing or differently bound session"),
            ("noneligible", "operator session is no longer eligible for registration"),
            ("cas", "operator host registration lost its state predicate"),
            ("readback", "operator host registration readback mismatch"),
            ("session", "operator host identity mismatch: AGENT_SESSION_ID"),
            ("session_later", "operator host identity mismatch: AGENT_SESSION_ID"),
            ("declaration", declaration), ("orphan", declaration),
            ("conflict", "fixture live conflict"),
            ("bridge", "bridge agc-missing not found or closed"),
        )
        for variant, error in cases:
            await _host_outcome(harness, runtime, variant, error)
    for variant in ("success", "probe", "orphan"):
        await _host_outcome(harness, "implicit", variant, "")


