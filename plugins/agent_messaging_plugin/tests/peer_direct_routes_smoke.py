#!/usr/bin/env python3
"""The landing path's two act-time reads on direct bridge routes (iss_d97f6633).

``peer_holds_role`` and ``peer_inbox`` are platform processes, so a caller with
no registered bridge reached them only through ``process/call`` — the serial
action queue that a deploy (or a slow ``peer_send_by_name``) holds for minutes.
They are now ALSO served on ``GET .../peer/holds_role`` and
``GET .../peer/inbox_for_session``, each calling the SAME function as its verb.
Nothing here needs a database or a live solet.

  * **PARITY (holds_role)** — one parametrised table over BOTH doors (the verb
    and the route): held, displaced, vacant, unregistered-instance and
    duplicate-binding give identical objects; the route adds only
    ``instance_registered`` and ``session_label``. The missing-argument refusal
    agrees too.
  * **PARITY (inbox)** — ``inbox_for_session`` pages equal ``peer_inbox`` pages
    across the instance and role sections (walked page by page with their
    cursors), and every refusal (malformed ``after`` / ``role_after``, unknown or
    ambiguous or missing session) is the same refusal at both doors — never an
    empty page.
  * **ROUTE POLICY** — an unknown or closed bridge is refused 404; an
    OAuth-bound bridge is held to the same per-session allowlist as the process
    key (refused 403 unless it carries the key, or is unrestricted); a stdio
    bridge is allowed, as on ``process/call``.
  * **STALL** — the REAL ``ActionQueuePoller._poll_once`` serial drain is held by
    a test action that outlives the timeout; both new routes answer inside 1 s
    while a ``peer_holds_role`` queued behind it does not.
  * **CLIENT / CLI** — ``solet-bridge holds-role`` prints the
    ``result.success`` / ``result.data`` envelope callers parse and exits
    non-zero on a refusal; ``solet-bridge inbox`` raises, never an empty page.
  * **MUTANTS** — each must be KILLED by the checks above: drop
    ``delivery_route_attached`` (as a forced False, and as an omitted key);
    return an empty page instead of raising (server side, and client side);
    skip bridge resolution.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/peer_direct_routes_smoke.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from _real_state_fake import RealShapeState  # noqa: E402
from ananta.core.actions.action_path_liveness import ACTION_PATH_LIVENESS  # noqa: E402
from ananta.core.actions.action_queue_poller import (  # noqa: E402
    ActionQueuePoller,
    QueuedAction,
)
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.models import (  # noqa: E402
    MessageKind,
    MessageRole,
    TextPart,
)
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    ID_PREFIX_MESSAGE,
    NAMESPACE,
    RECIPIENT_KIND_ROLE,
    TABLE_AGENT_MESSAGE,
    TABLE_AGENT_THREAD,
)
from ananta.llm.agent_messaging.service import AgentMessagingService  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin import http_routes as http_routes_module  # noqa: E402
from agent_messaging_plugin import peer_direct_reads as reads_module  # noqa: E402
from agent_messaging_plugin import plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.bridge_sessions import (  # noqa: E402
    _UNRESTRICTED,
    BridgeSessionManager,
)
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.local_cli import cli as cli_module  # noqa: E402
from agent_messaging_plugin.local_cli import client as client_module  # noqa: E402
from agent_messaging_plugin.local_cli.client import (  # noqa: E402
    BridgeCallError,
    BridgeClient,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_direct_reads import (  # noqa: E402
    PEER_HOLDS_ROLE_PROCESS_KEY,
    PEER_INBOX_PROCESS_KEY,
    PeerInboxReadRefusedError,
)
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.role_binding_store import (  # noqa: E402
    HOLDER_KIND_SESSION,
    HolderClaim,
    claim_role_binding_v4,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

_PREFIX = "/api/v1/bridge"
_ROLE = "Git-Controller"
_T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
_STALL_SECONDS = 2.5
_ANSWER_BUDGET_SECONDS = 1.0


class _Results:
    """Per-scenario check collector, so a MUTANT run can be asked "did anything fail?"."""

    def __init__(self, *, quiet: bool = False) -> None:
        self.quiet = quiet
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
            if not self.quiet:
                print(f"  PASS  {label}")
            return
        self.failed.append(label)
        if not self.quiet:
            print(f"  FAIL  {label}")


class _EnabledConfig:
    enabled = True
    allowed_backends: tuple[str, ...] = ()
    max_message_bytes = 65_536


class _World:
    """One solet's worth of real state, registry, bridge manager and service.

    The verb is driven through a real ``AgentMessagingPlugin`` instance holding
    the SAME collaborators the routes are registered with, so "the two doors" are
    the shipped verb and the shipped route over one state.
    """

    def __init__(self, *, state_service_bound: bool = True) -> None:
        self.state = RealShapeState()
        self.state.now_iso = lambda: _T0.isoformat()
        self.service = AgentMessagingService(
            repository=AgentMessagingRepository(cast(StateManagementInterface, self.state)),
            state_service=cast(StateManagementInterface, self.state),
            config=cast(Any, _EnabledConfig()),
            clock=lambda: _T0,
        )
        store: Store = open_store(
            get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
        )
        self.registry = PeerRegistry(bindings_store=store)
        self.manager = BridgeSessionManager(
            session_id_factory=lambda _n: "ags-direct",
            idle_timeout_s=3600,
            max_pending_events=50,
            long_poll_timeout_s=1,
        )
        app = FastAPI()
        register_routes(
            app,
            bridge_manager=self.manager,
            peer_registry=self.registry,
            platform_surface=cast(Any, object()),
            agent_messaging_service=self.service,
            config={"long_poll_timeout_seconds": 1},
            state_service=cast(StateManagementInterface, self.state) if state_service_bound else None,
        )
        self.http = TestClient(app)
        self.plugin = AgentMessagingPlugin()
        self.plugin._active = True  # noqa: SLF001
        self.plugin._peer_registry = self.registry  # noqa: SLF001
        self.plugin._bridge_manager = self.manager  # noqa: SLF001
        self.plugin._service = cast(Any, self.service)  # noqa: SLF001
        bound = cast(StateManagementInterface, self.state) if state_service_bound else None
        self.plugin._get_state_service = lambda: bound  # type: ignore[method-assign]  # noqa: SLF001
        # The caller: an UNREGISTERED one-shot bridge, exactly what solet-bridge opens.
        self.caller_bridge = self.manager.open(solet_name="", parent_pid=1).bridge_id

    # -- fixtures ------------------------------------------------------------

    def register(
        self,
        agi: str,
        session: str,
        label: str,
        *,
        bridge_open: bool = True,
        bridge_id: str | None = None,
    ) -> str:
        """Register a peer binding; ``bridge_open`` decides whether its bridge is live."""
        bid = bridge_id or self.manager.open(solet_name="", parent_pid=2).bridge_id
        if not bridge_open:
            self.manager.close(bid)
        self.registry.register(
            BridgeBinding(
                bridge_id=bid,
                agent_id="claude_code",
                agent_instance_id=agi,
                session_label=label,
                parent_pid=2,
                agent_session_id=session,
            ),
        )
        return bid

    def claim(self, agi: str, session: str, label: str, name: str = _ROLE) -> None:
        claim_role_binding_v4(
            cast(StateManagementInterface, self.state),
            name=name,
            claim=HolderClaim(
                holder_kind=HOLDER_KIND_SESSION,
                holder_identity={"agent_id": "claude_code"},
                agent_instance_id=agi,
                agent_session_id=session,
                session_label=label,
            ),
        )

    # -- the two doors -------------------------------------------------------

    def holds_verb(self, name: str, agi: str) -> dict[str, Any]:
        return self.plugin.peer_holds_role({"name": name, "agent_instance_id": agi}, {})

    def holds_route(self, name: str, agi: str, *, bridge: str | None = None) -> httpx.Response:
        return self.http.get(
            f"{_PREFIX}/{bridge or self.caller_bridge}/peer/holds_role"
            f"?name={quote(name)}&agent_instance_id={quote(agi)}",
        )

    def inbox_verb(self, **args: object) -> dict[str, Any]:
        return self.plugin.peer_inbox_action(dict(args), {})

    def inbox_route(self, *, bridge: str | None = None, **args: object) -> httpx.Response:
        query = "&".join(
            f"{key}={quote(str(value).lower() if isinstance(value, bool) else str(value), safe='')}"
            for key, value in args.items()
            if value is not None
        )
        return self.http.get(
            f"{_PREFIX}/{bridge or self.caller_bridge}/peer/inbox_for_session?{query}",
        )

    def forwarding_transport(self) -> httpx.MockTransport:
        """An httpx transport that forwards every request into the real route app."""

        def handler(request: httpx.Request) -> httpx.Response:
            forwarded = self.http.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.content,
                headers={"content-type": request.headers.get("content-type", "application/json")},
            )
            return httpx.Response(
                forwarded.status_code,
                content=forwarded.content,
                headers={"content-type": forwarded.headers.get("content-type", "application/json")},
            )

        return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# holds_role parity
# ---------------------------------------------------------------------------


def _held(w: _World) -> tuple[str, str]:
    w.register("agi-A", "ases-A", "Controller-A")
    w.claim("agi-A", "ases-A", "Controller-A")
    return _ROLE, "agi-A"


def _displaced(w: _World) -> tuple[str, str]:
    w.register("agi-A", "ases-A", "Controller-A")
    w.register("agi-B", "ases-B", "Controller-B")
    w.claim("agi-A", "ases-A", "Controller-A")
    w.claim("agi-B", "ases-B", "Controller-B")  # the later claim re-points the role at B
    return _ROLE, "agi-A"


def _vacant(w: _World) -> tuple[str, str]:
    w.register("agi-A", "ases-A", "Controller-A")
    return "Never-Claimed", "agi-A"


def _unregistered_instance(w: _World) -> tuple[str, str]:
    w.register("agi-A", "ases-A", "Controller-A")
    w.claim("agi-A", "ases-A", "Controller-A")
    return _ROLE, "agi-never-registered"


def _duplicate_binding(w: _World) -> tuple[str, str]:
    # Two siblings share the holder's session id, so the ROUTE lookup faults
    # (PeerSessionAmbiguousError) while the ownership comparison stays true.
    w.register("agi-A", "ases-A", "Controller-A")
    w.register("agi-A2", "ases-A", "Controller-A2")
    w.register("agi-A3", "ases-A", "Controller-A3")
    w.claim("agi-A", "ases-A", "Controller-A")
    return _ROLE, "agi-A"


# case -> (builder, expected verb object, expected extras)
_HOLDS_CASES: dict[str, tuple[Callable[[_World], tuple[str, str]], dict[str, object], dict[str, object]]] = {
    "held": (
        _held,
        {"holds": True, "agent_session_id": "ases-A", "delivery_route_attached": True},
        {"instance_registered": True, "session_label": "Controller-A"},
    ),
    "displaced": (
        _displaced,
        {"holds": False, "agent_session_id": "ases-A", "delivery_route_attached": True},
        {"instance_registered": True, "session_label": "Controller-A"},
    ),
    "vacant": (
        _vacant,
        {"holds": False, "agent_session_id": "ases-A", "delivery_route_attached": False},
        {"instance_registered": True, "session_label": "Controller-A"},
    ),
    "unregistered-instance": (
        _unregistered_instance,
        {"holds": False, "agent_session_id": "", "delivery_route_attached": True},
        {"instance_registered": False, "session_label": ""},
    ),
    "duplicate-binding": (
        _duplicate_binding,
        {"holds": True, "agent_session_id": "ases-A", "delivery_route_attached": False},
        {"instance_registered": True, "session_label": "Controller-A"},
    ),
}


def _check_holds_case(
    r: _Results,
    case: str,
    build: Callable[[_World], tuple[str, str]],
    expected: dict[str, object],
    extras: dict[str, object],
) -> None:
    w = _World()
    name, agi = build(w)
    verb = w.holds_verb(name, agi)
    route = w.holds_route(name, agi)
    verb_data = verb.get("data", {})
    route_data = route.json() if route.status_code == 200 else {}
    r.check(
        verb.get("action_status") == "completed" and route.status_code == 200,
        f"[{case}] both doors answer (verb={verb.get('action_status')}, route={route.status_code})",
    )
    r.check(
        {key: route_data.get(key) for key in verb_data} == verb_data
        and set(verb_data) == {"holds", "name", "agent_session_id", "delivery_route_attached"},
        f"[{case}] the route returns exactly the verb's object ({verb_data} vs {route_data})",
    )
    r.check(
        set(route_data) - set(verb_data) == {"instance_registered", "session_label"},
        f"[{case}] the route adds exactly instance_registered and session_label",
    )
    wanted = {**expected, "name": name}
    r.check(
        all(route_data.get(key) == value for key, value in wanted.items())
        and all(verb_data.get(key) == value for key, value in wanted.items()),
        f"[{case}] the shared answer is the expected one ({wanted})",
    )
    r.check(
        all(route_data.get(key) == value for key, value in extras.items()),
        f"[{case}] instance_registered/session_label come from the instance's peer_binding row",
    )


def _check_holds_refusals(r: _Results) -> None:
    w = _World()
    verb = w.holds_verb("", "agi-A")
    route = w.holds_route("", "agi-A")
    r.check(
        verb.get("action_status") == "failed"
        and (verb.get("error") or {}).get("code") == "missing_argument"
        and route.status_code == 400
        and route.json().get("code") == "missing_argument",
        "missing name: both doors refuse with missing_argument",
    )
    unbound = _World(state_service_bound=False)
    verb = unbound.holds_verb(_ROLE, "agi-A")
    route = unbound.holds_route(_ROLE, "agi-A")
    r.check(
        verb.get("action_status") == "failed"
        and (verb.get("error") or {}).get("code") == "state_service_unavailable"
        and route.status_code == 503,
        "no state service: both doors refuse (state_service_unavailable / 503), never 'not held'",
    )


def scenario_holds_role_parity(r: _Results) -> None:
    for case, (build, expected, extras) in _HOLDS_CASES.items():
        _check_holds_case(r, case, build, expected, extras)
    _check_holds_refusals(r)


# ---------------------------------------------------------------------------
# inbox parity
# ---------------------------------------------------------------------------


def _seed_instance_mail(w: _World, *, instance: str, session: str, count: int) -> None:
    thread_id = f"agt-direct-{instance}"
    w.state.rows(NAMESPACE, TABLE_AGENT_THREAD).append(
        {
            "id": thread_id,
            "namespace": "core",
            "target_backend": "peer:claude_code",
            "recipient_agent_instance_id": instance,
            "recipient_agent_session_id": session,
            "is_deleted": 0,
        },
    )
    for index in range(1, count + 1):
        w.state.rows(NAMESPACE, TABLE_AGENT_MESSAGE).append(
            {
                "id": f"{ID_PREFIX_MESSAGE}_direct{index}",
                "namespace": "core",
                "thread_id": thread_id,
                "cursor": index,
                "role": MessageRole.ORIGINATOR.value,
                "kind": MessageKind.MESSAGE.value,
                "content": [{"type": "text", "text": f"instance mail {index}"}],
                "action_id": None,
                "backend_session_id": None,
                "error": None,
                "artifacts": [],
                "metadata": {},
                "important": False,
                "created_at": f"2026-10-01T11:0{index}:00",
                "is_deleted": 0,
            },
        )


def _seed_role_mail(w: _World, *, count: int) -> None:
    for index in range(1, count + 1):
        w.service.persist_role_message(
            recipient_kind=RECIPIENT_KIND_ROLE,
            recipient_key=_ROLE,
            message_id=f"agm-direct-role{index}",
            sender_agent_id="claude_code",
            sender_agent_instance_id="agi-sender",
            sender_session_label="Sender",
            important=True,
            content=[TextPart(type="text", text=f"role mail {index}")],
        )


def _inbox_world() -> _World:
    w = _World()
    w.register("agi-A", "ases-A", "Controller-A")
    w.claim("agi-A", "ases-A", "Controller-A")
    _seed_instance_mail(w, instance="agi-A", session="ases-A", count=3)
    _seed_role_mail(w, count=3)
    return w


def _verb_outcome(result: dict[str, Any]) -> tuple[Any, ...]:
    if result.get("action_status") == "completed":
        return ("ok", result["data"])
    error = result.get("error") or {}
    return ("refused", error.get("code"), error.get("message"))


def _route_outcome(response: httpx.Response) -> tuple[Any, ...]:
    body = response.json()
    if response.status_code == 200:
        return ("ok", body)
    return ("refused", body.get("code"), body.get("message"))


def _mask_receipt(outcome: tuple[Any, ...]) -> tuple[Any, ...]:
    """A non-observer role read issues a fresh opaque receipt token per call."""
    if outcome[0] != "ok":
        return outcome
    data = dict(outcome[1])
    if data.get("role_read_page_token"):
        data["role_read_page_token"] = "<issued>"
    return ("ok", data)


def _check_page_walk(r: _Results, w: _World) -> None:
    """Walk the instance and role sections page by page with their cursors, over both doors."""
    after: str | None = None
    role_after: str | None = None
    for page_no in (1, 2, 3):
        args: dict[str, object] = {"agent_session_id": "ases-A", "observer": True, "limit": 2}
        if after:
            args["after"] = after
        if role_after:
            args["role_after"] = role_after
        verb = _verb_outcome(w.inbox_verb(**args))
        route = _route_outcome(w.inbox_route(**args))
        r.check(verb == route, f"page {page_no}: the route page equals the verb page")
        if verb[0] != "ok":
            r.check(False, f"page {page_no}: the walk reads a page ({verb})")
            return
        page = verb[1]
        if page_no == 1:
            r.check(
                len(page["entries"]) == 2 and len(page["role_entries"]) == 2,
                "page 1 carries BOTH sections (2 instance + 2 role entries), "
                f"so equality is not vacuous (got {len(page['entries'])}/{len(page['role_entries'])})",
            )
        after = page.get("next_after_created_at") or None
        role_after = page.get("next_role_cursor") or None


def _check_non_observer_page(r: _Results, w: _World) -> None:
    args: dict[str, object] = {"agent_session_id": "ases-A", "limit": 5}
    verb = _mask_receipt(_verb_outcome(w.inbox_verb(**args)))
    route = _mask_receipt(_route_outcome(w.inbox_route(**args)))
    r.check(verb == route and verb[0] == "ok", "non-observer page equals the verb page (receipt token masked)")


def _check_forged_role_cursor(r: _Results, w: _World) -> None:
    """A malformed ROLE cursor is not a refusal of the read: the service answers a page whose
    role section says ``error`` (the instance section still reads). Both doors must give the
    SAME page, and it must never look like a clean, empty role section.
    """
    forged: dict[str, object] = {"agent_session_id": "ases-A", "role_after": "forged-token"}
    verb = _verb_outcome(w.inbox_verb(**forged))
    route = _route_outcome(w.inbox_route(**forged))
    page = route[1] if route[0] == "ok" else {}
    r.check(verb == route and route[0] == "ok", "malformed role_after: the route page equals the verb page")
    r.check(
        page.get("role_section_status") == "error"
        and page.get("role_section_error")
        and page.get("role_entries") == []
        and len(page.get("entries", [])) == 3,
        "malformed role_after: role_section_status=error with its message (instance section still "
        f"read), not a clean empty role section ({page.get('role_section_status')!r})",
    )


def _check_refusals(r: _Results, w: _World) -> None:
    refusals: dict[str, dict[str, object]] = {
        "malformed after": {"agent_session_id": "ases-A", "after": "not-a-datetime"},
        "unknown session": {"agent_session_id": "ases-nobody"},
        "missing session": {"agent_session_id": ""},
    }
    for label, args in refusals.items():
        verb = _verb_outcome(w.inbox_verb(**args))
        route_response = w.inbox_route(**args)
        route = _route_outcome(route_response)
        r.check(
            verb[0] == "refused" and route[0] == "refused" and verb == route,
            f"{label}: the same refusal at both doors, never a page ({verb[:2]} vs {route[:2]}, "
            f"http={route_response.status_code})",
        )
        r.check(route_response.status_code >= 400, f"{label}: the route status is an error status")


def _check_ambiguous_session(r: _Results) -> None:
    amb = _World()
    amb.register("agi-A", "ases-A", "Controller-A")
    amb.register("agi-A2", "ases-A", "Controller-A2")
    verb = _verb_outcome(amb.inbox_verb(agent_session_id="ases-A"))
    route_response = amb.inbox_route(agent_session_id="ases-A")
    route = _route_outcome(route_response)
    r.check(
        verb[:2] == ("refused", "peer_session_ambiguous")
        and route == verb
        and route_response.status_code == 409,
        "duplicate session binding: the same peer_session_ambiguous refusal (409), not an empty page",
    )


def _check_read_does_not_register(r: _Results, w: _World) -> None:
    before = {b.agent_instance_id for bs in w.registry.list_agent_ids().values() for b in bs}
    w.inbox_route(agent_session_id="ases-A", observer=True)
    after_ids = {b.agent_instance_id for bs in w.registry.list_agent_ids().values() for b in bs}
    r.check(before == after_ids == {"agi-A"}, "a read never registers the one-shot caller bridge")


def scenario_inbox_parity(r: _Results) -> None:
    w = _inbox_world()
    _check_page_walk(r, w)
    _check_non_observer_page(r, w)
    _check_forged_role_cursor(r, w)
    _check_refusals(r, w)
    _check_ambiguous_session(r)
    _check_read_does_not_register(r, w)


# ---------------------------------------------------------------------------
# include_covered (the role-covered-floor read, wgr_b6007d23)
# ---------------------------------------------------------------------------

_SINCE = "2026-09-01T00:00:00"


def _covered_world() -> _World:
    """An inbox world whose role has a covered mark on its middle row, so the default
    drain is floored and an ``include_covered`` read has something to read past."""
    w = _inbox_world()
    w.service.mark_role_covered(
        recipient_key=_ROLE,
        message_id="agm-direct-role2",
        attested_by_agent_instance_id="agi-A",
        attested_by_agent_session_id="ases-A",
        attested_by_session_label="Controller-A",
    )
    return w


def _check_include_covered_parity(r: _Results, w: _World) -> None:
    covered: dict[str, object] = {"agent_session_id": "ases-A", "observer": True, "include_covered": True}
    default: dict[str, object] = {"agent_session_id": "ases-A", "observer": True}
    verb = _verb_outcome(w.inbox_verb(**covered))
    route = _route_outcome(w.inbox_route(**covered))
    floored = _route_outcome(w.inbox_route(**default))
    r.check(verb == route and route[0] == "ok", "include_covered observer page: the route page equals the verb page")
    page = route[1] if route[0] == "ok" else {}
    plain = floored[1] if floored[0] == "ok" else {}
    r.check(
        page.get("role_floor_applied") is False
        and plain.get("role_floor_applied") is True
        and len(page.get("role_entries", [])) > len(plain.get("role_entries", [])),
        "include_covered reads past the covered mark through the route (the default read is floored: "
        f"{len(plain.get('role_entries', []))} role rows vs {len(page.get('role_entries', []))})",
    )
    bare: dict[str, object] = {"agent_session_id": "ases-A", "include_covered": True}
    verb = _verb_outcome(w.inbox_verb(**bare))
    route = _route_outcome(w.inbox_route(**bare))
    r.check(
        verb == route and route[0] == "ok" and route[1].get("role_section_status") == "error",
        "include_covered without observer: the same role-section error at both doors, never a clean page",
    )


def _check_include_covered_cli(r: _Results, w: _World) -> None:
    args = ["inbox", "--observer", "--include-covered", "--since", _SINCE, "--limit", "2"]
    result = _invoke_cli(w, args, env={"AGENT_SESSION_ID": "ases-A"})
    try:
        payload, _ = json.JSONDecoder().raw_decode(result.output)
    except json.JSONDecodeError:
        payload = {}
    r.check(
        result.exit_code == 0 and payload.get("complete") is True and payload.get("role_count") == 3,
        "inbox --observer --include-covered over the route reads every role row past the mark "
        f"(role_count={payload.get('role_count')}, exit={result.exit_code})",
    )
    with BridgeClient("http://test", transport=w.forwarding_transport()) as client:
        page = client.peer_inbox_for_session(
            agent_session_id="ases-A", limit=5, observer=True, include_covered=True,
        )
    r.check(page.get("role_floor_applied") is False, "BridgeClient.peer_inbox_for_session sends include_covered")


def scenario_include_covered(r: _Results) -> None:
    w = _covered_world()
    _check_include_covered_parity(r, w)
    _check_include_covered_cli(r, w)


def _route_drops_include_covered(original: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def mutated(raw: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return original({key: value for key, value in raw.items() if key != "include_covered"}, **kwargs)

    return mutated


# ---------------------------------------------------------------------------
# route policy
# ---------------------------------------------------------------------------


def scenario_route_policy(r: _Results) -> None:
    w = _inbox_world()
    unknown = "agc-does-not-exist"
    r.check(
        w.holds_route(_ROLE, "agi-A", bridge=unknown).status_code == 404
        and w.inbox_route(bridge=unknown, agent_session_id="ases-A").status_code == 404,
        "an unknown bridge is refused 404 on both routes",
    )
    closed = w.manager.open(solet_name="", parent_pid=3).bridge_id
    w.manager.close(closed)
    r.check(
        w.holds_route(_ROLE, "agi-A", bridge=closed).status_code == 404
        and w.inbox_route(bridge=closed, agent_session_id="ases-A").status_code == 404,
        "a closed bridge is refused 404 on both routes",
    )
    r.check(
        w.holds_route(_ROLE, "agi-A").status_code == 200
        and w.inbox_route(agent_session_id="ases-A").status_code == 200,
        "a stdio bridge (client_id empty) is allowed, as on process/call",
    )

    def oauth_bridge(allowlist: tuple[str, ...]) -> str:
        state = w.manager.open(solet_name="", parent_pid=4)
        state.client_id = "client-x"
        state.process_export_allowlist = allowlist
        return state.bridge_id

    empty = oauth_bridge(())
    r.check(
        w.holds_route(_ROLE, "agi-A", bridge=empty).status_code == 403
        and w.inbox_route(bridge=empty, agent_session_id="ases-A").status_code == 403,
        "an OAuth bridge with an empty allowlist is refused 403 on both routes (fail closed)",
    )
    only_holds = oauth_bridge((PEER_HOLDS_ROLE_PROCESS_KEY,))
    r.check(
        w.holds_route(_ROLE, "agi-A", bridge=only_holds).status_code == 200
        and w.inbox_route(bridge=only_holds, agent_session_id="ases-A").status_code == 403,
        "an OAuth bridge is held to each route's OWN process key (holds_role yes, inbox no)",
    )
    only_inbox = oauth_bridge((PEER_INBOX_PROCESS_KEY,))
    r.check(
        w.holds_route(_ROLE, "agi-A", bridge=only_inbox).status_code == 403
        and w.inbox_route(bridge=only_inbox, agent_session_id="ases-A").status_code == 200,
        "an OAuth bridge carrying only peer_inbox reads the inbox, not the role",
    )
    unrestricted = oauth_bridge(_UNRESTRICTED)
    r.check(
        w.holds_route(_ROLE, "agi-A", bridge=unrestricted).status_code == 200
        and w.inbox_route(bridge=unrestricted, agent_session_id="ases-A").status_code == 200,
        "an operator-equivalent (unrestricted) OAuth bridge is allowed",
    )


# ---------------------------------------------------------------------------
# client and CLI
# ---------------------------------------------------------------------------


def _invoke_cli(w: _World, args: list[str], *, env: dict[str, str] | None = None) -> Any:
    transport = w.forwarding_transport()

    def factory(base_url: str, **kw: Any) -> BridgeClient:
        return BridgeClient(base_url, transport=transport, **kw)

    with (
        patch.object(cli_module, "resolve_base_url", lambda name=None: "http://test"),
        patch.object(cli_module, "BridgeClient", factory),
        patch.object(client_module.time, "sleep", lambda _s: None),
    ):
        return CliRunner().invoke(cli_module.cli, args, env=env, obj={})


def _holds_role_envelope_data(output: str) -> dict[str, Any]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return {}
    data = parsed.get("result", {}).get("data", {})
    return data if isinstance(data, dict) else {}


def _check_holds_role_cli(r: _Results) -> None:
    w = _World()
    _held(w)
    held = _invoke_cli(w, ["holds-role", "--name", _ROLE, "--instance-id", "agi-A"])
    data = _holds_role_envelope_data(held.output)
    envelope = json.loads(held.output) if held.exit_code == 0 else {}
    r.check(
        held.exit_code == 0
        and envelope.get("result", {}).get("success") is True
        and data.get("holds") is True
        and data.get("delivery_route_attached") is True
        and data.get("session_label") == "Controller-A",
        f"holds-role prints the result.success/result.data envelope (exit={held.exit_code}, {held.output[:120]!r})",
    )
    gone = _invoke_cli(w, ["holds-role", "--name", _ROLE, "--instance-id", "agi-never"])
    gone_data = _holds_role_envelope_data(gone.output)
    r.check(
        gone.exit_code == 0 and gone_data.get("holds") is False and gone_data.get("instance_registered") is False,
        "holds-role for an unregistered instance prints holds=false (a fact, not an error)",
    )
    # a refusal (state service unbound) must exit non-zero with NO envelope
    refused = _invoke_cli(
        _World(state_service_bound=False), ["holds-role", "--name", _ROLE, "--instance-id", "agi-A"],
    )
    r.check(
        refused.exit_code != 0 and '"success"' not in refused.output,
        f"holds-role exits non-zero on a refusal and prints no envelope (exit={refused.exit_code})",
    )


def _check_inbox_cli(r: _Results) -> None:
    inbox_world = _inbox_world()
    complete = _invoke_cli(inbox_world, ["inbox", "--observer", "--limit", "2"], env={"AGENT_SESSION_ID": "ases-A"})
    try:
        payload, _ = json.JSONDecoder().raw_decode(complete.output)
    except json.JSONDecodeError:
        payload = {}
    r.check(
        complete.exit_code == 0
        and payload.get("complete") is True
        and payload.get("direct_count") == 3
        and payload.get("role_count") == 3,
        f"solet-bridge inbox drains both sections over the direct route (got {payload.get('direct_count')}/"
        f"{payload.get('role_count')}, exit={complete.exit_code})",
    )
    stranger = _invoke_cli(inbox_world, ["inbox", "--observer"], env={"AGENT_SESSION_ID": "ases-nobody"})
    r.check(
        stranger.exit_code != 0 and '"messages"' not in stranger.output,
        f"solet-bridge inbox for an unregistered session raises, never prints an empty page (exit={stranger.exit_code})",
    )
    with BridgeClient("http://test", transport=inbox_world.forwarding_transport()) as client:
        try:
            client.peer_inbox_for_session(agent_session_id="ases-nobody", limit=5)
        except BridgeCallError:
            raised = True
        else:
            raised = False
    r.check(raised, "BridgeClient.peer_inbox_for_session raises BridgeCallError on a refusal")


def scenario_client_and_cli(r: _Results) -> None:
    _check_holds_role_cli(r)
    _check_inbox_cli(r)


# ---------------------------------------------------------------------------
# stall
# ---------------------------------------------------------------------------


def scenario_stall(r: _Results) -> None:
    """Hold the REAL serial drain; the direct routes answer, a queued verb does not."""
    w = _inbox_world()
    w2 = _World()
    _held(w2)
    finished: dict[str, float] = {}

    async def handler(action: QueuedAction) -> None:
        if action.id == "ae-stall":
            await asyncio.sleep(_STALL_SECONDS)
            finished["stall"] = time.monotonic()
        else:
            w2.holds_verb(_ROLE, "agi-A")
            finished["queued_holds_role"] = time.monotonic()

    poller = ActionQueuePoller.__new__(ActionQueuePoller)
    queue = [
        QueuedAction(id="ae-stall", process_key="held-test-action", parameters="{}", notes="", created_at=""),
        QueuedAction(id="ae-holds", process_key=PEER_HOLDS_ROLE_PROCESS_KEY, parameters="{}", notes="", created_at=""),
    ]

    async def get_queued() -> list[QueuedAction]:
        return list(queue)

    poller._get_queued_actions = get_queued  # type: ignore[method-assign]  # noqa: SLF001
    poller._mark_action_processing = lambda _action_id: True  # type: ignore[method-assign]  # noqa: SLF001
    poller._process_action = handler  # type: ignore[method-assign]  # noqa: SLF001
    poller._mark_action_failed = lambda *_a, **_k: None  # type: ignore[method-assign]  # noqa: SLF001
    poller.total_actions_processed = 0
    poller._last_observed_queue_depth = 2  # noqa: SLF001

    started = time.monotonic()
    drain = threading.Thread(target=lambda: asyncio.run(poller._poll_once()), daemon=True)  # noqa: SLF001
    drain.start()
    deadline = time.monotonic() + 5
    while ACTION_PATH_LIVENESS.snapshot().get("in_flight_action_id") != "ae-stall" and time.monotonic() < deadline:
        time.sleep(0.01)
    r.check(
        ACTION_PATH_LIVENESS.snapshot().get("in_flight_action_id") == "ae-stall",
        "the test action is in flight: the serial drain is held",
    )

    t0 = time.monotonic()
    holds = w2.holds_route(_ROLE, "agi-A")
    holds_elapsed = time.monotonic() - t0
    t1 = time.monotonic()
    inbox = w.inbox_route(agent_session_id="ases-A", observer=True, limit=2)
    inbox_elapsed = time.monotonic() - t1
    r.check(
        holds.status_code == 200 and holds.json().get("holds") is True and holds_elapsed < _ANSWER_BUDGET_SECONDS,
        f"GET peer/holds_role answers inside {_ANSWER_BUDGET_SECONDS}s while the queue is held ({holds_elapsed:.3f}s)",
    )
    r.check(
        inbox.status_code == 200 and inbox_elapsed < _ANSWER_BUDGET_SECONDS,
        f"GET peer/inbox_for_session answers inside {_ANSWER_BUDGET_SECONDS}s while the queue is held ({inbox_elapsed:.3f}s)",
    )
    r.check(
        "queued_holds_role" not in finished,
        "a peer_holds_role queued behind the held action has NOT answered (control: the queue really is held)",
    )
    drain.join(timeout=_STALL_SECONDS + 5)
    r.check(
        "queued_holds_role" in finished
        and finished["queued_holds_role"] - started >= _STALL_SECONDS - 0.05,
        "control: the queued verb answered only after the held action released the loop",
    )


# ---------------------------------------------------------------------------
# mutants
# ---------------------------------------------------------------------------


def _empty_page_instead_of_raising(original: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def mutated(raw: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        try:
            return original(raw, **kwargs)
        except PeerInboxReadRefusedError:
            return {
                "recipient_agent_id": "",
                "entries": [],
                "role_entries": [],
                "next_role_cursor": None,
                "role_section_status": "ok",
            }

    return mutated


def _client_swallows_refusal(original: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def mutated(self: BridgeClient, **kwargs: Any) -> dict[str, Any]:
        try:
            return original(self, **kwargs)
        except BridgeCallError:
            return {"entries": [], "role_entries": [], "role_section_status": "ok"}

    return mutated


def _mutants() -> list[tuple[str, contextlib.AbstractContextManager[Any], Callable[[_Results], None]]]:
    def omit_route_attached(self: reads_module.HoldsRoleRead) -> dict[str, object]:
        data = {
            "holds": self.holds,
            "name": self.name,
            "agent_session_id": self.agent_session_id,
            "instance_registered": self.instance_registered,
            "session_label": self.session_label,
        }
        return data

    return [
        (
            "drop delivery_route_attached (forced False)",
            patch.multiple(reads_module, role_delivery_route_attached=lambda *_a, **_k: False),
            scenario_holds_role_parity,
        ),
        (
            "drop delivery_route_attached (key omitted from the route object)",
            patch.object(reads_module.HoldsRoleRead, "route_data", omit_route_attached),
            scenario_holds_role_parity,
        ),
        (
            "return an empty page instead of raising (server side, both doors)",
            _patch_all(
                patch.object(
                    http_routes_module, "read_peer_inbox_for_session",
                    _empty_page_instead_of_raising(reads_module.read_peer_inbox_for_session),
                ),
                patch.object(
                    plugin_module, "read_peer_inbox_for_session",
                    _empty_page_instead_of_raising(reads_module.read_peer_inbox_for_session),
                ),
            ),
            scenario_inbox_parity,
        ),
        (
            "return an empty page instead of raising (client side)",
            patch.object(
                BridgeClient, "peer_inbox_for_session",
                _client_swallows_refusal(BridgeClient.peer_inbox_for_session),
            ),
            scenario_client_and_cli,
        ),
        (
            "the route drops include_covered (the CLI's floored-page guard must fail loudly)",
            patch.object(
                http_routes_module, "read_peer_inbox_for_session",
                _route_drops_include_covered(reads_module.read_peer_inbox_for_session),
            ),
            scenario_include_covered,
        ),
        (
            "skip bridge resolution",
            patch.object(
                http_routes_module, "_refuse_unresolvable_or_disallowed_bridge",
                lambda *_a, **_k: None,
            ),
            scenario_route_policy,
        ),
    ]


@contextlib.contextmanager
def _patch_all(*patches: contextlib.AbstractContextManager[Any]) -> Iterator[None]:
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _check_guard_catches_dropped_field(r: _Results) -> None:
    """With the route dropping include_covered, the CLI's own floored-page guard is what stops it."""
    w = _covered_world()
    args = ["inbox", "--observer", "--include-covered", "--since", _SINCE, "--limit", "2"]
    with patch.object(
        http_routes_module, "read_peer_inbox_for_session",
        _route_drops_include_covered(reads_module.read_peer_inbox_for_session),
    ):
        result = _invoke_cli(w, args, env={"AGENT_SESSION_ID": "ases-A"})
    try:
        payload, _ = json.JSONDecoder().raw_decode(result.output)
    except json.JSONDecodeError:
        payload = {}
    r.check(
        result.exit_code != 0
        and payload.get("complete") is False
        and payload.get("role_section_status") == "error"
        and "needs a deploy" in str(payload.get("role_section_error")),
        "route drops include_covered: the CLI's floored-page guard fails loudly "
        f"(exit={result.exit_code}, complete={payload.get('complete')}, "
        f"role_section_status={payload.get('role_section_status')!r})",
    )


def scenario_mutants_are_killed(r: _Results) -> None:
    _check_guard_catches_dropped_field(r)
    for name, patcher, scenario in _mutants():
        probe = _Results(quiet=True)
        with patcher:
            scenario(probe)
        r.check(
            probe.failed,
            f"MUTANT KILLED: {name} (the checks failed: {len(probe.failed)}, e.g. "
            f"{probe.failed[0] if probe.failed else 'NONE — it survived'})",
        )


# ---------------------------------------------------------------------------


def main() -> int:
    # The service logs the forged-cursor exception it converts into role_section_status=error;
    # that is the behaviour under test, not noise worth a traceback in the gate output.
    logging.getLogger("ananta.llm.agent_messaging.service").setLevel(logging.CRITICAL)
    r = _Results()
    for title, scenario in (
        ("holds_role parity over both doors", scenario_holds_role_parity),
        ("inbox parity over both doors", scenario_inbox_parity),
        ("include_covered", scenario_include_covered),
        ("route policy", scenario_route_policy),
        ("client and CLI", scenario_client_and_cli),
        ("stall (real serial poller drain)", scenario_stall),
        ("mutants", scenario_mutants_are_killed),
    ):
        print(f"\n[{title}]")
        scenario(r)
    print(f"\n{r.passed} passed, {len(r.failed)} failed")
    for label in r.failed:
        print(f"FAILED: {label}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
