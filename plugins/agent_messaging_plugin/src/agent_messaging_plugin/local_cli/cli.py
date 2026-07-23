"""`homunculus` — invoke a running homunculus over its localhost bridge (no MCP).

Every command discovers THIS homunculus's bridge port from the CLI's own
install location (never a flag or ambient env), opens a one-shot bridge
session, performs the operation, prints the JSON result to stdout, and closes.
Errors go to stderr with a mapped exit code.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, NoReturn

import click
import httpx
from ananta.constants import ExitCodes

# The parent package __init__ is lazy (PEP 562) and ``models`` is stdlib-only,
# so this import keeps the console script's bare-PATH contract intact.
from ..models import WATCH_AGENT_INSTANCE_PREFIX
from . import __version__
from .client import (
    DEFAULT_POLL_TIMEOUT_S,
    BridgeCallError,
    BridgeClient,
    BridgeResultTimeoutError,
    HomunculusIdentityError,
    HomunculusNotRunningError,
    resolve_base_url,
)

# watch: reconnect backoff after a transient bridge error / homunculus-down /
# bridge rotation (blue-green swap 404, idle-reap). Kept short so a swap gap is
# a blip, not a stall; the loop is silent while waiting, so it wakes no model.
WATCH_RECONNECT_DELAY_S: Final[float] = 2.0
# The events long-poll holds ~25s server-side; give the HTTP client margin.
WATCH_REQUEST_TIMEOUT_S: Final[float] = 35.0
# Heartbeat re-register cadence (Dax Part 13): the peer BINDING can be dropped
# server-side (post-swap purge, registry eviction) while the BRIDGE stays
# healthy and keeps answering the events long-poll with empty 200s — no error
# ever reaches the client, so without a heartbeat the watcher becomes a
# permanent persisted_silent black hole. Registration is idempotent, so
# re-asserting it bounds the outage to one interval.
WATCH_REREGISTER_INTERVAL_S: Final[float] = 60.0
WATCH_INBOX_DRAIN_LIMIT: Final[int] = 100
WATCH_CLAIM_PROCESS_KEY: Final[str] = (
    "plugin::agent_messaging_plugin::peer_claim_role"
)
# Deterministic per-session instance id (prefix shared with the server via
# ``..models.WATCH_AGENT_INSTANCE_PREFIX``): re-registering after a bridge drop
# REPLACES the binding instead of minting a sibling, so the durable role
# binding keeps pointing at this watcher across reconnects — and the server
# recognises the binding as a pull watcher (queued_watcher delivery labelling,
# events-ack consumption).
WATCH_SESSION_LABEL_ENV: Final[str] = "HOMUNCULUS_AGENT_SESSION_LABEL"
WATCH_SESSION_ID_ENV: Final[str] = "HOMUNCULUS_AGENT_SESSION_ID"


@dataclass(frozen=True)
class WatchIdentity:
    """The registered-presence identity a `watch` run holds for its session."""

    role: str
    agent_id: str
    agent_session_id: str
    agent_instance_id: str


def _emit(payload: dict[str, Any]) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _die(message: str, code: ExitCodes) -> NoReturn:
    click.echo(f"homunculus: {message}", err=True)
    raise SystemExit(int(code))


def _parse_json_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _die(f"arguments must be a JSON object: {exc}", ExitCodes.UNKNOWN_ERROR)
    if not isinstance(parsed, dict):
        _die(
            'arguments must be a JSON object, e.g. \'{"query": "..."}\'',
            ExitCodes.UNKNOWN_ERROR,
        )
    return parsed


def _run(fn: Callable[[BridgeClient], dict[str, Any]]) -> dict[str, Any]:
    """Open a bridge for THIS homunculus, run ``fn`` against it, map failures."""
    try:
        base_url = resolve_base_url()
    except (HomunculusNotRunningError, HomunculusIdentityError) as exc:
        _die(str(exc), ExitCodes.CONNECTION_ERROR)
    try:
        with BridgeClient(base_url) as client:
            return fn(client)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        _die(f"cannot reach the homunculus bridge at {base_url}: {exc}",
             ExitCodes.CONNECTION_ERROR)
    except BridgeResultTimeoutError as exc:
        _die(str(exc), ExitCodes.TIMEOUT_ERROR)
    except (BridgeCallError, httpx.HTTPError) as exc:
        _die(str(exc), ExitCodes.EXTERNAL_ERROR)


@click.group()
@click.version_option(__version__, prog_name="homunculus")
def cli() -> None:
    """Invoke this homunculus's capabilities over its localhost bridge (no MCP)."""


@cli.command()
@click.argument("process_key")
@click.argument("arguments", default="{}")
@click.option("--reason", default=None, help="Optional human reason for the call.")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=DEFAULT_POLL_TIMEOUT_S,
    show_default=True,
    help="Seconds to wait for the result before giving up.",
)
def call(
    process_key: str,
    arguments: str,
    reason: str | None,
    timeout_s: float,
) -> None:
    """Invoke PROCESS_KEY with ARGUMENTS (a JSON object) and wait for the result."""
    args = _parse_json_args(arguments)
    result = _run(
        lambda c: c.call_and_wait(
            process_key, args, reason=reason, poll_timeout_s=timeout_s,
        ),
    )
    _emit(result)
    if str(result.get("status")) != "completed":
        raise SystemExit(int(ExitCodes.EXTERNAL_ERROR))


@cli.command()
@click.argument("query")
@click.option("--max-results", "-n", type=int, default=8, show_default=True)
def search(query: str, max_results: int) -> None:
    """Discover process keys by semantic QUERY."""
    _emit(_run(lambda c: c.process_search(query, max_results)))


@cli.command()
@click.argument("process_key")
def schema(process_key: str) -> None:
    """Fetch the argument schema for PROCESS_KEY."""
    _emit(_run(lambda c: c.process_schema(process_key)))


@cli.command()
@click.argument("action_id")
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Poll until the action reaches a terminal state.",
)
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=DEFAULT_POLL_TIMEOUT_S,
    show_default=True,
)
def result(action_id: str, wait: bool, timeout_s: float) -> None:
    """Fetch (or --wait for) the result of a previously dispatched ACTION_ID."""
    if wait:
        payload = _run(
            lambda c: c.wait_for_result(action_id, poll_timeout_s=timeout_s),
        )
    else:
        payload = _run(lambda c: c.process_result(action_id))
    _emit(payload)


@cli.command()
def health() -> None:
    """Check whether the homunculus bridge is answering."""
    try:
        base_url = resolve_base_url()
    except (HomunculusNotRunningError, HomunculusIdentityError) as exc:
        _die(str(exc), ExitCodes.CONNECTION_ERROR)
    client = BridgeClient(base_url)
    try:
        payload = client.health()
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        _die(f"cannot reach the homunculus bridge at {base_url}: {exc}",
             ExitCodes.CONNECTION_ERROR)
    except httpx.HTTPError as exc:
        _die(str(exc), ExitCodes.EXTERNAL_ERROR)
    finally:
        client.close()
    _emit(payload)


@cli.command()
@click.option(
    "--role",
    default=None,
    help=f"Role to register and claim (default: ${WATCH_SESSION_LABEL_ENV}).",
)
@click.option(
    "--agent-id",
    "agent_id",
    default="claude_code",
    show_default=True,
    help="Peer kind this session registers as.",
)
def watch(role: str | None, agent_id: str) -> None:
    """Hold this session's REGISTERED PRESENCE and stream its messages (no MCP).

    Registers a stable peer identity for the wrapping session, claims ROLE as
    its durable role binding, drains the durable inbox (catch-up on messages
    that arrived while unwatched), then long-polls the registered bridge and
    prints one JSON line per delivered event — and NOTHING while idle. Run it
    under a persistent monitor: it wakes the session only on a real event, at
    zero idle token cost. Auto-reconnects and re-claims across bridge rotation
    (blue-green swap) and idle-reap. Stop with Ctrl-C; the durable role
    binding remains, so role-addressed messages queue for the next start.
    """
    identity = _resolve_watch_identity(role, agent_id)
    try:
        _watch_forever(identity)
    except HomunculusIdentityError as exc:
        _die(str(exc), ExitCodes.CONNECTION_ERROR)
    except KeyboardInterrupt:
        raise SystemExit(0) from None


def _resolve_watch_identity(role: str | None, agent_id: str) -> WatchIdentity:
    """Build the watcher's stable identity from the launcher-exported env.

    The session id carrier must be per-logical-session (the launcher's
    ``ases-...`` export) — never a PID, which app-hosted siblings share. The
    reconnect self-refresh and `peer_claim_role` (REL-07) key on it, so watch
    fails loud rather than registering a degraded, self-refresh-disabled
    binding.
    """
    resolved_role = role or os.environ.get(WATCH_SESSION_LABEL_ENV, "")
    if not resolved_role:
        _die(
            f"watch needs a role: pass --role or export {WATCH_SESSION_LABEL_ENV} "
            "(the claude-<name> launcher and fleet functions do this)",
            ExitCodes.UNKNOWN_ERROR,
        )
    session_id = os.environ.get(WATCH_SESSION_ID_ENV, "")
    if not session_id:
        _die(
            f"watch needs the stable session id: export {WATCH_SESSION_ID_ENV} "
            "(the claude-<name> launcher and fleet functions do this); "
            "a PID is not an acceptable substitute",
            ExitCodes.UNKNOWN_ERROR,
        )
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return WatchIdentity(
        role=resolved_role,
        agent_id=agent_id,
        agent_session_id=session_id,
        agent_instance_id=f"{WATCH_AGENT_INSTANCE_PREFIX}{digest}",
    )


def _watch_forever(identity: WatchIdentity) -> NoReturn:
    """Reconnect loop: (re)discover the bridge, re-arm, and stream until drop."""
    while True:
        try:
            base_url = resolve_base_url()
        except HomunculusNotRunningError:
            time.sleep(WATCH_RECONNECT_DELAY_S)
            continue
        try:
            with BridgeClient(
                base_url, request_timeout_s=WATCH_REQUEST_TIMEOUT_S,
            ) as client:
                _arm_and_stream(client, identity)
        except (httpx.HTTPError, BridgeCallError, BridgeResultTimeoutError):
            # bridge rotated (swap 404), idle-reaped, or a transient error:
            # back off briefly (silent — no model wake) and re-arm from scratch.
            time.sleep(WATCH_RECONNECT_DELAY_S)


def _arm_and_stream(client: BridgeClient, identity: WatchIdentity) -> None:
    """One bridge lifetime: register, claim, drain catch-up, then long-poll."""
    claim = _register_and_claim(client, identity)
    _emit_line({"watch": "armed", "role": identity.role, "claim": claim})
    _drain_inbox(client)
    _stream_events(client, identity)


def _register_and_claim(
    client: BridgeClient, identity: WatchIdentity,
) -> dict[str, Any]:
    """Register the presence, then claim the durable role binding through it.

    The claim is dispatched over THIS registered bridge because the server
    sources the binding's stable session id from the caller's live peer
    binding, never from claim args (REL-07). A terminal non-completed claim is
    a permanent rejection (e.g. a reserved role name) and dies loud; transport
    errors propagate to the reconnect loop.
    """
    client.peer_register(
        agent_id=identity.agent_id,
        agent_instance_id=identity.agent_instance_id,
        session_label=identity.role,
        agent_session_id=identity.agent_session_id,
    )
    payload = client.call_and_wait(
        WATCH_CLAIM_PROCESS_KEY,
        {
            "name": identity.role,
            "agent_id": identity.agent_id,
            "agent_instance_id": identity.agent_instance_id,
            "session_label": identity.role,
        },
        reason="no-MCP registered-presence watcher claiming its session role",
    )
    if str(payload.get("status")) != "completed":
        _die(
            f"role claim for {identity.role!r} was rejected: "
            f"{json.dumps(payload, sort_keys=True)}",
            ExitCodes.EXTERNAL_ERROR,
        )
    return payload


def _drain_inbox(client: BridgeClient) -> None:
    """Emit durable messages that arrived while unwatched (instance + role)."""
    page = client.peer_inbox(
        include_important=True, limit=WATCH_INBOX_DRAIN_LIMIT,
    )
    for section in ("entries", "role_entries"):
        items = page.get(section, [])
        if not isinstance(items, list):
            raise BridgeCallError(f"inbox section {section!r} malformed: {page!r}")
        for entry in items:
            _emit_line({"watch": "inbox", "section": section, "entry": entry})


def _stream_events(client: BridgeClient, identity: WatchIdentity) -> None:
    """Long-poll one armed bridge, one JSON line per event, until it drops.

    Re-asserts ``peer/register`` on a heartbeat cadence: the server can drop
    the peer binding while this bridge stays healthy (the events long-poll
    then returns empty 200s with no error signal), which would otherwise
    black-hole deliveries as persisted_silent forever. Registration is
    idempotent server-side, so the heartbeat rebuilds a dropped binding
    within one interval — no restart, no operator action.
    """
    cursor = -1
    last_register = time.monotonic()
    while True:
        payload = client.events(after=cursor)
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise BridgeCallError(f"events response malformed: {payload!r}")
        for event in events:
            _emit_line({"watch": "event", "event": event})
        next_cursor = payload.get("next_cursor", cursor)
        cursor = next_cursor if isinstance(next_cursor, int) else cursor
        if time.monotonic() - last_register >= WATCH_REREGISTER_INTERVAL_S:
            client.peer_register(
                agent_id=identity.agent_id,
                agent_instance_id=identity.agent_instance_id,
                session_label=identity.role,
                agent_session_id=identity.agent_session_id,
            )
            last_register = time.monotonic()


def _emit_line(payload: dict[str, Any]) -> None:
    """One compact JSON line per delivery — the monitor-facing stream format."""
    click.echo(json.dumps(payload, sort_keys=True))


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":
    main()
