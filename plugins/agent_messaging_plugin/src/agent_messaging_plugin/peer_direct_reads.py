"""The two act-time reads Git-Controller's landing path makes, as ONE body each.

``peer_holds_role`` (the role-ownership re-check) and ``peer_inbox`` (the
caller's own mail, by session id) are platform processes, so a caller with no
registered bridge could reach them only through ``process/call``: the serial
action queue. A held queue (a deploy, a slow ``peer_send_by_name``) therefore
froze the landing guard even though both reads are cheap, pure state reads
(iss_d97f6633, wgr_09e4e4ed).

The bridge's HTTP surface stays responsive while the queue is held, so both
reads are now ALSO served on direct bridge routes
(``GET .../peer/holds_role`` and ``GET .../peer/inbox_for_session``). The
verbs and the routes call the functions in this module and nothing else, so
the guarded answer cannot drift between the two doors. Nothing is cached and
nothing is written (the inbox read touches the caller's binding liveness, as
the verb always did).

Each door keeps its own transport shell around the shared body: the verb wraps
it in an ``ActionResult``, the route in a ``JSONResponse``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.models import PeerInboxRequest
from ananta.llm.agent_messaging.service import AgentMessagingError

from .peer_inbox_view import (
    InvalidInboxCursorError,
    parse_since_cursor,
    serialize_peer_inbox_page,
)
from .peer_registry import PeerRegistry, PeerSessionAmbiguousError
from .role_binding_store import (
    RoleBindingMalformedError,
    RoleBindingVacantError,
    holds_role,
    resolve_role_binding,
)

if TYPE_CHECKING:
    from .bridge_sessions import BridgeSessionManager
    from .models import BridgeBinding

PEER_HOLDS_ROLE_PROCESS_KEY: Final[str] = "plugin::agent_messaging_plugin::peer_holds_role"
PEER_INBOX_PROCESS_KEY: Final[str] = "plugin::agent_messaging_plugin::peer_inbox"

# peer_inbox page size. Deliberately far below the route's 50: a freshly
# /clear'd Coordinator-Dawn measured a 422,513-character page at 50 instance +
# 50 role entries on 2026-08-01 — roughly 4KB per entry, because an entry
# carries the whole message. ``limit`` bounds the COUNT, so bytes are the
# caller's arithmetic, not the platform's promise: 5 is a page a session can
# read and still act on, and the two cursors exist to fetch the rest.
PEER_INBOX_DEFAULT_LIMIT: Final[int] = 5
PEER_INBOX_MIN_LIMIT: Final[int] = 1
# Parity with the /peer/inbox route's own clamp — one ceiling, both surfaces.
PEER_INBOX_MAX_LIMIT: Final[int] = 100

_HTTP_BAD_REQUEST: Final[int] = 400
_HTTP_CONFLICT: Final[int] = 409


# ---------------------------------------------------------------------------
# peer_holds_role
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldsRoleRead:
    """One act-time ownership read, with every field either door may report.

    ``holds`` is the safety answer. ``instance_registered`` and
    ``session_label`` come from the SAME ``peer_binding`` row that supplied
    ``agent_session_id`` (one read, so the three cannot disagree); only the
    route reports them, because the verb's declared return schema is fixed.
    """

    holds: bool
    name: str
    agent_session_id: str
    delivery_route_attached: bool
    instance_registered: bool
    session_label: str

    def verb_data(self) -> dict[str, object]:
        """Exactly the object the ``peer_holds_role`` verb has always returned."""
        return {
            "holds": self.holds,
            "name": self.name,
            "agent_session_id": self.agent_session_id,
            "delivery_route_attached": self.delivery_route_attached,
        }

    def route_data(self) -> dict[str, object]:
        """The verb's object plus the two fields the landing guard needs."""
        return {
            **self.verb_data(),
            "instance_registered": self.instance_registered,
            "session_label": self.session_label,
        }


def read_holds_role(
    state_service: Any,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
    *,
    name: str,
    agent_instance_id: str,
) -> HoldsRoleRead:
    """READ-ONLY act-time ownership re-check (§5.0) — does the caller's session
    STILL hold ``name``?

    ``holds_role`` is PULL-TRUTH over the v4 ``role_binding`` table. The
    caller's STABLE session id is sourced from its OWN ``peer_binding`` row (by
    ``agent_instance_id`` — the REL-07 pattern), never from a caller-supplied
    session id, then compared to the live holder's. It NEVER writes: the
    anti-pattern is a self-re-claim (a WRITE that would STEAL the role back from
    a legitimate new holder) — this is a pure read.

    A malformed role row propagates :class:`RoleBindingMalformedError` out of
    ``holds_role`` on purpose: a data fault must surface, not read as "not held".
    """
    binding = (
        peer_registry.resolve_by_agent_instance_id(agent_instance_id)
        if peer_registry is not None
        else None
    )
    agent_session_id = binding.agent_session_id if binding is not None else ""
    return HoldsRoleRead(
        holds=holds_role(state_service, name, agent_session_id),
        name=name,
        agent_session_id=agent_session_id,
        delivery_route_attached=role_delivery_route_attached(
            state_service, peer_registry, bridge_manager, name,
        ),
        instance_registered=binding is not None,
        session_label=binding.session_label if binding is not None else "",
    )


def role_delivery_route_attached(
    state_service: Any,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
    name: str,
) -> bool:
    """Does the role's CURRENT holder have a live bridge bound right now?

    A role binding outlives the session that claimed it, so ``holds=True`` can
    be reported for a role whose holder has no receiver left — the claim is
    durable, the route is not. This measures the route: the holder's stable
    session id resolves to a ``peer_binding`` row, and that row's bridge is open
    (an MCP bridge session or an armed ``watch`` long-poll — both are the same
    kind of attachment here).

    Named for what it measures. NOT ``receiving``: on MCP transport a route can
    be attached while no waker ever fires, so a truthful name is the narrow one.
    False is also the honest answer for a vacant role and for a holder whose
    binding is gone.

    **Total by construction.** Every fault this lookup can raise — a duplicate
    binding for one session id, a malformed role row — is answered ``False``
    rather than propagated. ``holds`` is Git-Controller's Step-9.5 pre-commit
    ownership re-check and is the safety answer: it must survive anything the
    route lookup does. An additive truth-in-reporting field that can convert
    that boolean into an exception would be a regression wearing an addition's
    clothes.

    Caveat, stated rather than engineered away: this reads the role binding a
    second time (``holds_role`` read it first), so a displacement landing
    between the two reads would report ``holds`` for one holder and the route of
    another. Fixing that would mean re-implementing ``holds_role`` inline, and
    changing the Step-9.5 safety computation to improve an advisory field is the
    wrong trade. The window is one state read wide.
    """
    if peer_registry is None or bridge_manager is None:
        return False
    try:
        resolved = resolve_role_binding(state_service, name)
        binding = peer_registry.resolve_by_agent_session_id(resolved.agent_session_id)
    except (
        RoleBindingVacantError,
        RoleBindingMalformedError,
        PeerSessionAmbiguousError,
    ):
        return False
    if binding is None:
        return False
    bridge = bridge_manager.get(binding.bridge_id)
    return bridge is not None and not bridge.closed


# ---------------------------------------------------------------------------
# peer_inbox
# ---------------------------------------------------------------------------


class PeerInboxReadRefusedError(Exception):
    """A refused inbox read: a stable ``code``, prose, and the HTTP status the
    route reports. The verb reports ``code`` and ``message`` and ignores the
    status; a refusal is never an empty page.
    """

    def __init__(self, code: str, message: str, http_status: int = _HTTP_BAD_REQUEST) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def read_peer_inbox_for_session(
    raw: dict[str, Any],
    *,
    peer_registry: PeerRegistry,
    service: Any,
) -> dict[str, Any]:
    """PULL receive path — read the caller's own peer mail on demand.

    Identity is an explicit argument, but a caller may only name its OWN
    session: ``agent_session_id`` is looked up in ``peer_binding`` and the
    recipient triple is taken from that row (the same three fields the
    ``/peer/inbox`` route reads off its own binding). An unknown or duplicated
    session id is a loud refusal — never a silent read of an empty inbox.

    Deliberately NOT done here: this read does not retire the re-emit /
    escalation insurance on the rows it returns (the watcher long-poll ack and
    the MCP ``/peer/drain`` reconcile remain the two consumption authorities).
    Retiring insurance on a read that might not reach a model turn risks
    destroying content, which is the worse failure.

    Raises :class:`PeerInboxReadRefusedError` for every refusal.
    """
    agent_session_id = str(raw.get("agent_session_id", "")).strip()
    if not agent_session_id:
        raise PeerInboxReadRefusedError(
            "missing_argument",
            "peer_inbox requires the caller's own non-empty 'agent_session_id' "
            "(the launcher exports it as $AGENT_SESSION_ID).",
        )
    try:
        binding = peer_registry.resolve_by_agent_session_id(agent_session_id)
    except PeerSessionAmbiguousError as exc:
        raise PeerInboxReadRefusedError(
            "peer_session_ambiguous", str(exc), _HTTP_CONFLICT,
        ) from exc
    if binding is None:
        raise PeerInboxReadRefusedError(
            "identity_not_registered",
            f"no live peer_binding for agent_session_id {agent_session_id!r}. "
            f"This usually means this session's watcher or bridge is no longer "
            f"registered — re-arm it ('<solet> watch --role <role>', or "
            f"peer_register over MCP) and retry. Read this as 'the reader is "
            f"unknown', never as 'the reader has no mail': the messages are "
            f"durable and still waiting. A wrong agent_session_id produces this "
            f"same error, so check the value came from $AGENT_SESSION_ID and not "
            f"a stale note.",
        )
    try:
        request = build_peer_inbox_request(raw, binding)
    except InvalidInboxCursorError as exc:
        raise PeerInboxReadRefusedError(exc.code, str(exc)) from exc
    try:
        page = service.peer_inbox(request)
    except AgentMessagingError as exc:
        raise PeerInboxReadRefusedError(
            "peer_inbox_rejected", str(exc), exc.http_status,
        ) from exc
    # The caller proved liveness by reading; keep "last active" in step with
    # the delivery path, exactly as the /peer/inbox route does.
    peer_registry.touch_binding(binding.agent_instance_id)
    return serialize_peer_inbox_page(page, binding.agent_instance_id)


def build_peer_inbox_request(
    raw: dict[str, Any],
    binding: BridgeBinding,
) -> PeerInboxRequest:
    """Coerce caller args + the resolved binding into one ``PeerInboxRequest``.

    The recipient triple comes from ``binding`` and never from ``raw`` — a
    caller names only its own session, and the identity it reads with is the one
    the registry holds for that session. The two cursors are read independently
    and neither ever feeds the other. Raises ``InvalidInboxCursorError`` for a
    malformed ``after`` or ``since``: a broken cursor means the caller's paging
    is wrong, and silently restarting from page one would turn that into an
    unbounded re-read.
    """
    after_raw = raw.get("after")
    try:
        after_created_at = (
            datetime.fromisoformat(str(after_raw)) if after_raw not in (None, "") else None
        )
    except ValueError as exc:
        message = (
            f"'after' must be an ISO-8601 datetime (the previous newest-first page's "
            f"next_after_created_at): {exc}"
        )
        raise InvalidInboxCursorError("after", message) from exc
    role_after_raw = raw.get("role_after")
    return PeerInboxRequest(
        recipient_agent_id=binding.agent_id,
        recipient_agent_instance_id=binding.agent_instance_id,
        recipient_agent_session_id=binding.agent_session_id,
        after_created_at=after_created_at,
        since_created_at=parse_since_cursor(raw.get("since")),
        limit=clamp_peer_inbox_limit(raw.get("limit")),
        # A4 (2026-08-04): the silent/important split at send time is
        # retired, so the catch-up view is the only meaningful one — never
        # read from the caller. This closes the hatch the same way Amendment
        # 3 closes send_peer_message's: the schema entry AND the
        # read-and-branch code both go, not just one.
        include_important=True,
        role_after=(str(role_after_raw) if role_after_raw not in (None, "") else None),
        observer=bool(raw.get("observer", False)),
        include_covered=bool(raw.get("include_covered", False)),
    )


def clamp_peer_inbox_limit(raw: object) -> int:
    """Coerce a caller's ``limit`` to the supported page size.

    Absent or non-numeric → the modest default (the flood guard is what makes
    an unqualified ``peer_inbox`` call safe to advertise). Out-of-range values
    clamp rather than error: the caller asked for "as much as you'll give me",
    and a page size is not a correctness argument — unlike ``after`` /
    ``role_after``, where a malformed value means the caller's paging is broken
    and must fail loud.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return PEER_INBOX_DEFAULT_LIMIT
    return max(PEER_INBOX_MIN_LIMIT, min(raw, PEER_INBOX_MAX_LIMIT))
