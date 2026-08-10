"""`homunculus` — invoke a running homunculus over its localhost bridge (no MCP).

Every command discovers THIS homunculus's bridge port from the CLI's own
install location (never a flag or ambient env), opens a one-shot bridge
session, performs the operation, prints the JSON result to stdout, and closes.
Errors go to stderr with a mapped exit code.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final, NoReturn

import click
import httpx
from ananta.constants import ExitCodes

# Key spelling comes from the ONE definition the server stamps with; a local
# copy here would be a second convention free to drift silently.
from ananta.llm.agent_messaging.schema import (
    META_KEY_RECIPIENT_KIND,
    META_KEY_ROLE_CREATED_AT,
    RECIPIENT_KIND_ROLE,
)

# The parent package __init__ is lazy (PEP 562) and ``models`` is stdlib-only,
# so this import keeps the console script's bare-PATH contract intact.
from ..env_contract import enforce_no_legacy_agent_env
from ..models import WATCH_AGENT_INSTANCE_PREFIX
from . import __version__
from .client import (
    DEFAULT_POLL_TIMEOUT_S,
    BridgeCallError,
    BridgeClient,
    BridgeResultTimeoutError,
    HomunculusIdentityError,
    HomunculusNotRunningError,
    RoleClaimRejectedError,
    resolve_base_url,
    resolve_homunculus_name,
)
from .spool import (
    WATCH_SESSION_ID_ENV,
    WATCH_SESSION_LABEL_ENV,
    default_spool_path,
    read_watch_marks,
    spool_append,
    watch_instance_digest,
    watch_marks_path,
    watch_pairing_path,
    watch_singleton_lock_path,
    write_watch_marks,
    write_watch_pairing,
)
from .wake import wake

# watch: reconnect backoff after a transient bridge error / homunculus-down /
# bridge rotation (blue-green swap 404, idle-reap). Kept short so a swap gap is
# a blip, not a stall; the loop is silent while waiting, so it wakes no model.
WATCH_RECONNECT_DELAY_S: Final[float] = 2.0
# The events long-poll holds ~25s server-side; give the HTTP client margin.
WATCH_REQUEST_TIMEOUT_S: Final[float] = 35.0
# Heartbeat re-register cadence, field-observed on a live deployment: the peer
# BINDING can be dropped server-side (post-swap purge, registry eviction) while
# the BRIDGE stays healthy and keeps answering the events long-poll with empty
# 200s — no error ever reaches the client, so without a heartbeat the watcher
# becomes a
# permanent persisted_silent black hole. Registration is idempotent, so
# re-asserting it bounds the outage to one interval.
WATCH_REREGISTER_INTERVAL_S: Final[float] = 60.0
WATCH_INBOX_DRAIN_LIMIT: Final[int] = 100
# Bound on pages the arm-time drain will walk per section. A backlog deeper
# than this is a real operational event, not a routine catch-up: the remainder
# stays in the durable inbox and the session pulls it with
# `<name> call plugin::agent_messaging_plugin::peer_inbox`. The bound exists so
# a pathological backlog (or a server that never stops handing back cursors)
# cannot turn one arm into an unbounded request loop that also spools thousands
# of wake lines. Deliberately generous: 100 pages x 100 entries is far past any
# real session's unwatched window.
WATCH_INBOX_MAX_PAGES: Final[int] = 100
WATCH_CLAIM_PROCESS_KEY: Final[str] = (
    "plugin::agent_messaging_plugin::peer_claim_role"
)
# ARM failures (register AND claim) a RETRY WITH IDENTICAL INPUTS can never
# resolve. That question
# — not "does it look serious" — is the whole membership test, so a future code
# classifies itself rather than being pattern-matched in.
#
# Everything NOT listed is presumed TRANSIENT, including an unrecognized or
# absent code. The asymmetry is deliberate: a watcher retrying a permanent
# failure logs loudly on every attempt, while a watcher dying on a transient
# one is silent forever — which was the D2 defect.
#
# `peer_identity_unregistered` is POINTEDLY ABSENT. It is what the route
# returns when this bridge has no registered binding yet, i.e. the ordinary
# post-rotation window, and the very next register repairs it. Promoting it
# here "for completeness" would kill a watcher on a routine reconnect.
PERMANENT_ARM_FAILURES: Final[frozenset[str]] = frozenset({
    "role_held_live",            # a LIVE holder; needs an explicit takeover
    "system_slot_claim_denied",  # reserved keyspace
    "missing_argument",          # malformed call
    "missing_session_id",        # no stable session key to bind
    "missing_role_name",         # empty/whitespace role; malformed argv
    # §4.3.3a: this session id already belongs to a LIVE different session.
    # Retrying under the same inherited id against the same live incumbent can
    # never succeed — it needs a distinct AGENT_SESSION_ID or the incumbent to
    # exit, both external events. Permanent by the same test as the rest.
    "session_id_bound_to_live_session",
})
# The deterministic per-session instance id (digest + env contract shared with
# the wake hook via ``.spool``; prefix shared with the server via
# ``..models.WATCH_AGENT_INSTANCE_PREFIX``): re-registering after a bridge drop
# REPLACES the binding instead of minting a sibling, so the durable role
# binding keeps pointing at this watcher across reconnects — and the server
# recognises the binding as a pull watcher (queued_watcher delivery labelling,
# events-ack consumption).


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


def _caller_agent_session_id() -> str:
    """The launcher-exported session key this command inherits, or "".

    Read OPPORTUNISTICALLY: it is best-effort sender attribution (§34.6), not
    an identity the CLI asserts and not a precondition for invoking anything.
    A command run outside a launcher-started session simply carries no key and
    is attributed as before. Deliberately does NOT call
    ``enforce_no_legacy_agent_env()`` — that tripwire belongs to ``watch``,
    whose binding is durable; making the universal invocation verb fail loud on
    env drift would be a widening, not a fix.
    """
    return os.environ.get(WATCH_SESSION_ID_ENV, "")


def _run(fn: Callable[[BridgeClient], dict[str, Any]]) -> dict[str, Any]:
    """Open a bridge for THIS homunculus, run ``fn`` against it, map failures."""
    try:
        base_url = resolve_base_url()
    except (HomunculusNotRunningError, HomunculusIdentityError) as exc:
        _die(str(exc), ExitCodes.CONNECTION_ERROR)
    try:
        with BridgeClient(
            base_url, caller_agent_session_id=_caller_agent_session_id(),
        ) as client:
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
@click.option(
    "--spool",
    "spool_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Wake-hook spool file (default: this session's derived path).",
)
@click.option(
    "--no-spool",
    is_flag=True,
    default=False,
    help="Disable the wake-hook spool tee (the `wake` hook then never fires).",
)
@click.option(
    "--exit-with-parent",
    "exit_with_parent",
    type=int,
    default=None,
    help=(
        "Exit when this pid is gone. Inert unless a launcher passes it; "
        "checked on the reconnect loop's own cadence."
    ),
)
@click.option(
    "--takeover",
    is_flag=True,
    default=False,
    help=(
        "Take ROLE from a LIVE holder on THIS arm only, displacing it. Without "
        "it a live holder refuses the claim and the watcher exits. Applies to "
        "the arm-time claim and is NOT carried into reconnect or heartbeat "
        "re-claims, so a later contention is decided on its own merits rather "
        "than replaying this authorization. Confirm with the operator first: "
        "the displaced session stops receiving the role's deliveries."
    ),
)
def watch(
    role: str | None,
    agent_id: str,
    spool_path: Path | None,
    no_spool: bool,
    exit_with_parent: int | None,
    takeover: bool,
) -> None:
    """Hold this session's REGISTERED PRESENCE and stream its messages (no MCP).

    Registers a stable peer identity for the wrapping session, claims ROLE as
    its durable role binding, drains the durable inbox (catch-up on messages
    that arrived while unwatched), then long-polls the registered bridge and
    prints one JSON line per delivered event — and NOTHING while idle.
    Message-bearing lines are also teed to a per-session spool consumed by
    `wake`, the shipped Stop-hook waker that turns a delivery into a session
    turn (zero idle token cost; see the hydration settings template).
    Auto-reconnects and re-claims across bridge rotation (blue-green swap)
    and idle-reap. Stop with Ctrl-C; the durable role binding remains, so
    role-addressed messages queue for the next start.
    """
    identity = _resolve_watch_identity(role, agent_id)
    try:
        homunculus_name = resolve_homunculus_name()
        # W1 (§34.3): become the session's singleton BEFORE any network traffic,
        # so a refused second arm never touches the registry. The handle is bound
        # to a module-level name for the process lifetime — letting it be garbage
        # collected would close the fd and silently release the flock.
        _acquire_watch_singleton(
            watch_singleton_lock_path(homunculus_name, identity.agent_instance_id),
        )
        # W2 (§34.1): SIGTERM must unwind rather than terminate, so the
        # `with BridgeClient(...)` below reaches close() -> /close -> unregister
        # and the registry row is evicted synchronously. Python's default SIGTERM
        # disposition skips every finally block, which is exactly how a killed
        # watcher leaves a row that dispatch then reports as `queued_watcher`.
        _install_sigterm_unwind()
        spool = None if no_spool else (
            spool_path
            or default_spool_path(homunculus_name, identity.agent_instance_id)
        )
        # Census D4: publish the choice so the wake half pairs with the spool
        # this watcher ACTUALLY uses, not the one it would derive on its own.
        # Written before the first arm so a wake hook firing in the gap reads a
        # current answer rather than a stale predecessor's.
        write_watch_pairing(
            watch_pairing_path(homunculus_name, identity.agent_instance_id),
            spool,
        )
        # Marks are keyed on SESSION identity, never on the spool path: a
        # --spool move changes where lines are teed, not what this session has
        # already been shown (census D1).
        _watch_forever(
            identity,
            spool,
            watch_marks_path(homunculus_name, identity.agent_instance_id),
            exit_with_parent,
            takeover=takeover,
        )
    except HomunculusIdentityError as exc:
        _die(str(exc), ExitCodes.CONNECTION_ERROR)
    except KeyboardInterrupt:
        raise SystemExit(0) from None


# Held for the process lifetime. Module-level on purpose: a local would be
# collected when `watch` returns into the loop, closing the fd and releasing the
# flock while the watcher is still running — a singleton that silently stops
# being one.
_watch_singleton_handle: IO[str] | None = None


def _acquire_watch_singleton(lock_path: Path) -> None:
    """Take the per-session watcher flock, or REFUSE (never evict) — W1.

    A process holding this flock is by definition a live watcher for this
    session, so the incumbent is the correct one. That is the opposite of the
    registry's replace-on-register semantics, which exist for RECONNECT, where
    the incumbent is dead. Refusing here is what stops the two known false-
    liveness producers: a `nohup`-armed watcher reparented to launchd, and a
    pre-swap watcher left alive across a blue-green swap.
    """
    global _watch_singleton_handle
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip()
        handle.close()
        _die(
            f"another watcher already holds this session's lock "
            f"({lock_path})"
            + (f"; holder pid {holder}" if holder else "")
            + " — one watch per session; stop the incumbent first",
            ExitCodes.UNKNOWN_ERROR,
        )
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _watch_singleton_handle = handle


def _install_sigterm_unwind() -> None:
    """SIGTERM -> SystemExit, so the bridge context manager unwinds — W2.

    Raising here is deliberate and is NOT the D2 failure shape: SystemExit in
    this client means "die on purpose". The D2 defect is a TRANSIENT claim
    failure raising it; the fix for that is classifying the failure before
    deciding to die, never catching SystemExit in the reconnect loop (which
    would turn a designed refusal into retry-forever).
    """

    def _terminate(signum: int, _frame: object) -> NoReturn:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)


def _parent_is_gone(parent_pid: int | None) -> bool:
    """True when `--exit-with-parent` was given and that process is gone.

    Signal 0 probes existence without delivering anything. A ProcessLookupError
    is the answer we want; PermissionError means the pid EXISTS under another
    user, which is emphatically not "gone".
    """
    if parent_pid is None:
        return False
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _resolve_watch_identity(role: str | None, agent_id: str) -> WatchIdentity:
    """Build the watcher's stable identity from the launcher-exported env.

    The session id carrier must be per-logical-session (the launcher's
    ``ases-...`` export) — never a PID, which app-hosted siblings share. The
    reconnect self-refresh and `peer_claim_role` (REL-07) key on it, so watch
    fails loud rather than registering a degraded, self-refresh-disabled
    binding. An un-migrated launcher still exporting a legacy prefixed
    family (either pre-rename generation, e.g. ``HOMUNCULUS_AGENT_*``)
    fails loud the same way (one-release tripwire, env_contract.py).
    """
    try:
        enforce_no_legacy_agent_env()
    except RuntimeError as exc:
        _die(str(exc), ExitCodes.UNKNOWN_ERROR)
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
    digest = watch_instance_digest(session_id)
    return WatchIdentity(
        role=resolved_role,
        agent_id=agent_id,
        agent_session_id=session_id,
        agent_instance_id=f"{WATCH_AGENT_INSTANCE_PREFIX}{digest}",
    )


def _watch_forever(
    identity: WatchIdentity,
    spool: Path | None,
    marks: Path,
    exit_with_parent: int | None = None,
    takeover: bool = False,
) -> NoReturn:
    """Reconnect loop: (re)discover the bridge, re-arm, and stream until drop.

    The parent check sits here as well as inside the stream loop so a parent
    that dies while the homunculus is DOWN — i.e. while this loop is doing
    nothing but backing off — is still noticed.
    """
    while True:
        if _parent_is_gone(exit_with_parent):
            raise SystemExit(0)
        try:
            base_url = resolve_base_url()
        except HomunculusNotRunningError:
            time.sleep(WATCH_RECONNECT_DELAY_S)
            continue
        try:
            with BridgeClient(
                base_url, request_timeout_s=WATCH_REQUEST_TIMEOUT_S,
            ) as client:
                # ONE-SHOT (§4.3.3a binding 2): consume the authorization at
                # the first arm ATTEMPT, before any operation that can raise
                # into the reconnect loop. Clearing it after _arm_and_stream
                # was unreachable on the normal bridge-drop path (the stream
                # raises), so the old placement silently replayed takeover on
                # every reconnect exception.
                arm_takeover = takeover
                takeover = False
                _arm_and_stream(
                    client, identity, spool, marks, exit_with_parent,
                    takeover=arm_takeover,
                )
        except (httpx.HTTPError, BridgeCallError, BridgeResultTimeoutError):
            # bridge rotated (swap 404), idle-reaped, or a transient error:
            # back off briefly (silent — no model wake) and re-arm from scratch.
            time.sleep(WATCH_RECONNECT_DELAY_S)


def _arm_and_stream(
    client: BridgeClient,
    identity: WatchIdentity,
    spool: Path | None,
    marks: Path,
    exit_with_parent: int | None = None,
    takeover: bool = False,
) -> None:
    """One bridge lifetime: register, claim, drain catch-up, then long-poll.

    The armed line is deliberately NOT spooled: waking a session because its
    own watcher (re)armed is noise, not a delivery.
    """
    claim = _register_and_claim(client, identity, takeover=takeover)
    # The armed line carries the resolved spool (null when the tee is off) so
    # the pairing is legible to an operator reading the stream, not only to the
    # wake hook reading the sidecar — census D4.
    _emit_line({
        "watch": "armed",
        "role": identity.role,
        "claim": claim,
        "spool": None if spool is None else str(spool),
    })
    _drain_inbox(client, spool, marks)
    _stream_events(client, identity, spool, marks, exit_with_parent)


def _register_and_claim(
    client: BridgeClient, identity: WatchIdentity, *, takeover: bool = False,
) -> dict[str, Any]:
    """Register the presence, then claim the durable role binding through it.

    The claim goes over the INFRA bridge route, not the MODEL_INITIATED
    ``/process/call`` verb, for two independent reasons:

    * **The failure code survives.** The queue poller overwrites every plugin
      failure code with the constant ``action_failed`` and nulls ``data``, so
      on that path a permanent refusal and a transient hiccup are literally the
      same value and cannot be told apart.
    * **A watcher arm is not a model turn.** The MODEL_INITIATED verb stamps
      model activity and fires an EDGE_SINK delivery on every call. On a
      re-arming watcher that is a phantom stamp, and it corrupts the
      "no model activity since emission" discriminator other machinery keys on.

    Identity still comes from this bridge's REGISTERED binding (REL-07); the
    route reads it there rather than from the body, which is why the register
    above must precede the claim.
    """
    try:
        client.peer_register(
            agent_id=identity.agent_id,
            agent_instance_id=identity.agent_instance_id,
            session_label=identity.role,
            agent_session_id=identity.agent_session_id,
        )
        return client.peer_claim_role(name=identity.role, takeover=takeover)
    except RoleClaimRejectedError as rejection:
        if rejection.code in PERMANENT_ARM_FAILURES:
            _die(
                f"role claim for {identity.role!r} was refused "
                f"({rejection.code}): {rejection.message}",
                ExitCodes.EXTERNAL_ERROR,
            )
        # Presumed TRANSIENT — everything outside the explicit permanent set,
        # including an unrecognized or absent code. The direction is deliberate
        # (D2): a watcher that retries a genuinely permanent failure logs on
        # every attempt and is loud, while a watcher that dies on a transient
        # one is silent forever, which is the defect this replaced. Raising
        # BridgeCallError hands it to the reconnect loop's existing backoff.
        raise BridgeCallError(
            f"role claim for {identity.role!r} failed transiently "
            f"({rejection.code or 'no code'}): {rejection.message}",
        ) from rejection


def _drain_inbox(client: BridgeClient, spool: Path | None, marks_path: Path) -> None:
    """Emit only what arrived since this SESSION last looked (census D1).

    Before this, the drain was a single cursor-less page run on EVERY bridge
    lifetime: anything past the first ``limit`` of either section was
    permanently unreachable, and every re-arm — which a blue-green swap
    guarantees — re-spooled the same entries, waking the session about mail it
    handled yesterday. The durable inbox is a re-readable catch-up view with no
    consumption filter (ruled: cursors are the contract, PULL NEVER CONSUMES),
    so de-duplication is the CLIENT's job. This is that job.

    The two sections need DIFFERENT algorithms because they page in OPPOSITE
    directions (measured 2026-08-01):

    * instance — ``after`` walks FORWARD in time, so its cursor is a genuine
      high-water mark: resume from the stored mark and page to an empty page.
    * role — ``role_after`` walks BACKWARD into history, newest first, and the
      server mints no token at all on a partial page. So the stored value is
      NOT a cursor but a ``created_at`` high-water mark, and the algorithm is
      "read newest-first and walk back only while entries are still newer than
      the mark". A descending cursor is the right tool for that; persisting the
      token instead would resume reading OLDER mail and never surface new mail.

    With no marks (a genuinely new session, or a lost sidecar) this SEEDS to
    the newest and emits nothing, rather than replaying history as a wake
    storm. That is safe only because a session can now pull its own backlog on
    demand via ``plugin::agent_messaging_plugin::peer_inbox``, and because a new
    role holder is told so at claim time by the IMPORTANT handover notice.

    Fix (B): seeding is PER-SECTION, not one shared boolean. An empty
    ``role_high_water`` on its own means "this session has never been shown
    role mail", and the role section seeds to newest regardless of the
    instance mark — before (B) the global predicate required BOTH marks
    empty, so a session that had drained instance mail at least once but
    never been shown role mail (mark SET, role mark EMPTY) fell through to
    ``_drain_role_section`` with ``mark=""``, where every entry compares
    newer than ``""`` and the early-stop never fires: up to
    ``WATCH_INBOX_DRAIN_LIMIT * WATCH_INBOX_MAX_PAGES`` role entries replayed
    on a single re-arm. (B) is safe here only because (A) — see below — makes
    an empty role mark truthful even when nothing but live delivery has ever
    touched it.

    The instance section is deliberately UNTOUCHED by (B) (Architect's
    ruling, workbench/2026-08-01_architect_walkback_per_section_seeding_ruling.md
    §4): its own fail direction — a genuine forward high-water mark, with no
    notice-and-pull backstop for anything it suppresses — does not change,
    so it keeps the exact pre-(B) expression, including its dependency on
    ``role_high_water``.

    The notice dependency IS pinned: ``role_claim_handover_smoke.py`` asserts
    the dispatched notice text names ``peer_inbox``, the holder's own
    ``agent_session_id``, and paging ``role_after`` until ``next_role_cursor``
    is null — and, since A4 (2026-08-04), that it no longer mentions the
    retired ``include_important`` parameter. This
    docstring previously named ``handover_notice_runnable_smoke`` as the
    guard; that file never existed in the tree or the gate register (a
    design-doc name that leaked into shipped source).

    The ``role_high_water`` mark is advanced from BOTH directions: by this
    drain, and — since (A) — by ``_stream_events`` on live role delivery.
    Before (A) only the drain advanced it, so a session shown role mail live
    still arrived at its next arm with an empty mark, which is exactly the
    state (B) now has to seed correctly rather than replay.
    """
    instance_after, role_high_water = read_watch_marks(marks_path)
    fresh_instance, next_after = _drain_instance_section(
        client, instance_after,
        seeding=not instance_after and not role_high_water,
    )
    fresh_role, next_high_water = _drain_role_section(
        client, role_high_water, seeding=not role_high_water,
    )
    for entry in fresh_instance:
        _emit_line({"watch": "inbox", "section": "entries", "entry": entry}, spool)
    for entry in fresh_role:
        _emit_line({"watch": "inbox", "section": "role_entries", "entry": entry}, spool)
    write_watch_marks(
        marks_path,
        instance_after=next_after,
        role_high_water=next_high_water,
    )


def _inbox_section(page: dict[str, Any], section: str) -> list[Any]:
    items = page.get(section, [])
    if not isinstance(items, list):
        raise BridgeCallError(f"inbox section {section!r} malformed: {page!r}")
    return items


def _entry_created_at(entry: object) -> str:
    """The entry's ``created_at``, or ``""`` when the shape is unexpected.

    An entry whose timestamp cannot be read sorts as older than every mark, so
    it is never emitted twice and never advances the mark — the safe direction
    for an unrecognised shape on a path whose failure mode is a wake storm.
    """
    if not isinstance(entry, dict):
        return ""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    created_at = message.get("created_at")
    return created_at if isinstance(created_at, str) else ""


def _drain_instance_section(
    client: BridgeClient, mark: str, *, seeding: bool,
) -> tuple[list[Any], str]:
    """Page FORWARD from ``mark`` to exhaustion; return (fresh entries, new mark)."""
    after = mark
    fresh: list[Any] = []
    for _ in range(WATCH_INBOX_MAX_PAGES):
        page = client.peer_inbox(
            include_important=True,
            limit=WATCH_INBOX_DRAIN_LIMIT,
            after=after or None,
        )
        entries = _inbox_section(page, "entries")
        if not entries:
            break
        if not seeding:
            fresh.extend(entries)
        nxt = page.get("next_after_created_at")
        nxt = nxt if isinstance(nxt, str) and nxt else ""
        if not nxt or nxt == after:
            after = nxt or after
            break
        after = nxt
    return fresh, after


def _drain_role_section(
    client: BridgeClient, mark: str, *, seeding: bool,
) -> tuple[list[Any], str]:
    """Walk BACKWARD from newest, stopping at ``mark``; return (fresh, new mark).

    Returns the fresh entries in chronological order so the spool reads oldest
    first, matching the instance section and the order a reader expects.
    """
    role_after: str | None = None
    fresh: list[Any] = []
    newest = mark
    for _ in range(WATCH_INBOX_MAX_PAGES):
        page = client.peer_inbox(
            include_important=True,
            limit=WATCH_INBOX_DRAIN_LIMIT,
            role_after=role_after,
        )
        entries = _inbox_section(page, "role_entries")
        if not entries:
            break
        newest = max(newest, _entry_created_at(entries[0]))
        if seeding:
            break
        newer = [e for e in entries if _entry_created_at(e) > mark]
        fresh.extend(newer)
        # Reached mail this session has already been shown: everything older is
        # older still (the section is newest-first), so stop walking back.
        if len(newer) < len(entries):
            break
        cursor = page.get("next_role_cursor")
        if not isinstance(cursor, str) or not cursor or cursor == role_after:
            # No token, or a token that does not ADVANCE. A non-advancing cursor
            # would otherwise re-serve the same page until the page bound, and
            # every one of those entries is still "newer than the mark", so the
            # loop would spool the same mail up to MAX_PAGES times.
            break
        role_after = cursor
    fresh.reverse()
    return fresh, newest


def _live_role_created_at(event: object) -> str:
    """The role ROW's ``created_at`` off a LIVE delivery event, or ``""``.

    ⚠ Read the META key, never the event's own ``created_at``. A bridge event
    carries its OWN ``created_at`` (``datetime.now(UTC)`` at queue time, see
    ``QueuedEvent``) sitting one key away from this one, and the two are
    DIFFERENT QUANTITIES. The mark is compared against the role-inbox row's
    ``created_at``; marking with the event's clock reading would skip any row
    whose timestamp lands later — a silent loss, in the one direction this
    package must never move. That is also why ``sent_at`` and the unordered
    ``delivery_external_id`` were both rejected as substitutes.

    Returns ``""`` for a non-role event, an unexpected shape, or a server that
    predates the stamp — every one of which means "cannot advance", never
    "advance to something close enough".
    """
    if not isinstance(event, dict):
        return ""
    meta = event.get("meta")
    if not isinstance(meta, dict):
        return ""
    if meta.get(META_KEY_RECIPIENT_KIND) != RECIPIENT_KIND_ROLE:
        return ""
    created_at = meta.get(META_KEY_ROLE_CREATED_AT)
    return created_at if isinstance(created_at, str) else ""


def _commit_live_role_mark(marks_path: Path, newest: str) -> None:
    """Advance ``role_high_water`` to ``newest``, preserving ``instance_after``.

    Read-modify-write, and monotonic: a mark already at or past ``newest`` is
    left alone, so an out-of-order or replayed event can never rewind it.
    """
    instance_after, role_high_water = read_watch_marks(marks_path)
    if newest <= role_high_water:
        return
    write_watch_marks(
        marks_path,
        instance_after=instance_after,
        role_high_water=newest,
    )


def _stream_events(
    client: BridgeClient,
    identity: WatchIdentity,
    spool: Path | None,
    marks: Path,
    exit_with_parent: int | None = None,
) -> None:
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
        # RETURN rather than exit: unwinding lets the caller's
        # `with BridgeClient(...)` run close() -> /close -> unregister, so an
        # exit-with-parent teardown evicts the registry row on the same
        # graceful path W2 gives SIGTERM. Killing the process here would leave
        # exactly the stale binding this lane exists to remove.
        if _parent_is_gone(exit_with_parent):
            return
        payload = client.events(after=cursor)
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise BridgeCallError(f"events response malformed: {payload!r}")
        newest_role_delivery = ""
        for event in events:
            _emit_line({"watch": "event", "event": event}, spool)
            # EMIT-THEN-COMMIT (D5): the mark is only a candidate until the line
            # has actually been emitted. Collected here and committed after the
            # batch, so a crash mid-batch costs a duplicate on the next arm
            # (safe) rather than a message (not safe).
            newest_role_delivery = max(
                newest_role_delivery, _live_role_created_at(event),
            )
        if newest_role_delivery:
            # (A): a LIVE role delivery advances role_high_water. Without this
            # the mark only ever moved in _drain_inbox, so a session shown role
            # mail live still had an EMPTY mark at the next arm and re-spooled
            # that history — up to WATCH_INBOX_DRAIN_LIMIT * WATCH_INBOX_MAX_PAGES
            # entries — as a wake storm.
            _commit_live_role_mark(marks, newest_role_delivery)
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


def _emit_line(payload: dict[str, Any], spool: Path | None = None) -> None:
    """One compact JSON line per delivery — the monitor-facing stream format.

    With a spool, the line is also appended there for the `wake` Stop hook —
    stdout is the session-facing stream, the spool is the wake channel.
    """
    line = json.dumps(payload, sort_keys=True)
    click.echo(line)
    if spool is not None:
        spool_append(spool, line)


cli.add_command(wake)


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":
    main()
