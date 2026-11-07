"""Drive-on-delivery off the send request path (iss_ab680764, iss_ba4d1c81).

``dispatch_peer_send`` / ``dispatch_role_send`` used to run
:func:`session_lifecycle_verbs.drive_on_delivery` inline, so every send
waited for the recipient's tmux drive: an insert, then
``_wait_for_paste_stable`` (up to 10 s on a busy Claude pane, iss_ab1f0a21),
then the submitting Enter. ``af96438a5`` moved that wait off the event loop
onto asyncio's default executor, which kept ``/events`` alive but still held
the HTTP request — and a pool thread shared with every other offloaded route —
for the whole drive.

Here the send keeps the state half of the drive (the managed-row lookup and
the lifecycle gate, :func:`resolve_delivery_drive`) and hands only the host
half (:meth:`DeliveryDrive.run`) to ONE dedicated executor: a fixed, small
worker count with named threads, never asyncio's default executor. The
sender's ``drive_on_delivery`` field therefore stays truthful: the lookup
outcomes (``not_managed``, ``ineligible_state``, ``driver_unavailable``) are
reported as before, and a drive that was handed over reads
``driver_scheduled`` — never ``driver_sent``, which only a finished channel
send may claim. The finished outcome is logged against the message id, the
same key the sender holds from its own result, and kept for the most recent
drives so an in-process caller can read it back by that id
(:func:`finished_drive_outcome`).

That read-back covers EVERY outcome, including the ones the lookup decides
without reaching the executor (prb_351c9641 / iss_25ca0b87). A recipient whose
host has no driver channel — an ``operator`` seat with no verified pane, where
no wake mechanism exists at all — resolves to ``driver_unavailable`` in the
lookup, and :func:`_record_lookup_outcome` keeps that outcome WITH its own
detail so ``finished_drive_outcome`` answers it instead of ``None``. ``None``
already means "no such record", "aged out" and "not finished yet", so without
that record a caller could not tell a seat that never had a channel from a
drive still in flight. Nothing here refuses or drops a delivery: the message is
already durable, the lookup's own outcome is still what the sender receives,
and the seat takes the message up on its next inbox read.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from ananta.llm.agent_messaging.models import PeerInbox, PeerInboxRequest
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.service import AgentMessagingService
from ananta.llm.agent_messaging.state_results import require_records
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition, SchemaDefinition, TableSchema

from .composer_preservation import COMPOSER_RECOVERY_NEEDED
from .role_binding_store import resolve_role_binding
from .session_lifecycle_verbs import (
    DRIVE_DRIVER_ERROR,
    DRIVE_DRIVER_QUEUE_FULL,
    DRIVE_DRIVER_SCHEDULED,
    DRIVE_DRIVER_UNAVAILABLE,
    DRIVE_INELIGIBLE_STATE,
    DRIVE_NOT_MANAGED,
    DeliveryDrive,
    DriveOnDeliveryOutcome,
    resolve_delivery_drive,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

# A drive is a tmux insert + paste wait + Enter: seconds, mostly waiting on a
# subprocess. Four workers keep a burst of sends to different panes moving
# while bounding how many tmux clients this process runs at once; extra drives
# queue in submission order.
DELIVERY_DRIVE_WORKERS: Final[int] = 4
DELIVERY_DRIVE_THREAD_PREFIX: Final[str] = "peer-delivery-drive"

DELIVERY_DRIVE_EXECUTOR: ThreadPoolExecutor = ThreadPoolExecutor(
    max_workers=DELIVERY_DRIVE_WORKERS,
    thread_name_prefix=DELIVERY_DRIVE_THREAD_PREFIX,
)

# Review D1: the queue is bounded. Pending = queued + running drives. A send
# beyond the cap is refused with ``driver_queue_full`` (recorded and logged),
# never queued behind a backlog it would outlive and never silently dropped.
# Every queued drive re-checks the recipient's lifecycle before it touches the
# pane (``DeliveryDrive.run``), so the cap bounds lateness, not correctness.
DELIVERY_DRIVE_QUEUE_LIMIT: Final[int] = 32

# The finished (outcome, detail) of the most recent drives, keyed by message
# id. Bounded: the oldest record is dropped first.
DRIVE_RECORD_LIMIT: Final[int] = 512
_DriveResult = tuple[DriveOnDeliveryOutcome, str | None]
_DRIVE_RECORDS: OrderedDict[str, Future[_DriveResult]] = OrderedDict()
_DRIVE_RECORDS_LOCK: Final[threading.Lock] = threading.Lock()


class _PendingDrives:
    """Queued + running drive count; written under ``_DRIVE_RECORDS_LOCK``."""

    value: int = 0


_PENDING: Final[_PendingDrives] = _PendingDrives()

TABLE_DELIVERY_PICKUP: Final[str] = "delivery_pickup"
TABLE_DELIVERY_PICKUP_EVENT: Final[str] = "delivery_pickup_event"
PICKUP_ATTEMPT_LIMIT: Final[int] = 3
PICKUP_LEASE_SECONDS: Final[int] = 60
PICKUP_PAGE_LIMIT: Final[int] = 64
_PICKUP_LOCK: Final[threading.Lock] = threading.Lock()
_ACTIVE_PICKUPS: set[tuple[int, str]] = set()
_executor_stopped = False


def start_delivery_drives() -> None:
    """Start/restart the plugin-owned executor before its sweep rider starts."""
    global DELIVERY_DRIVE_EXECUTOR, _executor_stopped
    if _executor_stopped:
        DELIVERY_DRIVE_EXECUTOR = ThreadPoolExecutor(  # pyright: ignore[reportConstantRedefinition] -- restart owned resource
            max_workers=DELIVERY_DRIVE_WORKERS,
            thread_name_prefix=DELIVERY_DRIVE_THREAD_PREFIX,
        )
        _executor_stopped = False


def stop_delivery_drives() -> None:
    """Finish running drives and cancel queued ones before bridge teardown."""
    global _executor_stopped
    DELIVERY_DRIVE_EXECUTOR.shutdown(wait=True, cancel_futures=True)
    _executor_stopped = True


def get_delivery_pickup_schema() -> SchemaDefinition:
    """Durable retry projection and append-only evidence, without message bodies."""
    text = ColumnType.TEXT
    integer = ColumnType.INTEGER
    columns = {
        "message_id": ColumnDefinition(type=text, not_null=True),
        "role_name": ColumnDefinition(type=text, not_null=True),
        "recipient_instance": ColumnDefinition(type=text, not_null=True),
        "recipient_session": ColumnDefinition(type=text, not_null=True),
        "pending": ColumnDefinition(type=ColumnType.BOOLEAN, not_null=True),
        "phase": ColumnDefinition(type=text, not_null=True),
        "reason": ColumnDefinition(type=text, not_null=True),
        "attempts": ColumnDefinition(type=integer, not_null=True),
        "version": ColumnDefinition(type=integer, not_null=True),
        "checked_at": ColumnDefinition(type=ColumnType.DATETIME, not_null=True),
        "lease_until": ColumnDefinition(type=ColumnType.DATETIME),
    }
    event_columns = {
        "message_id": ColumnDefinition(type=text, not_null=True),
        "version": ColumnDefinition(type=integer, not_null=True),
        "evidence": ColumnDefinition(type=ColumnType.JSON, not_null=True),
    }
    return SchemaDefinition(
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        version="1.0.0",
        description="Codex delivery retries and exact inbox-return evidence.",
        tables={
            TABLE_DELIVERY_PICKUP: TableSchema(
                table_name=TABLE_DELIVERY_PICKUP,
                id_prefix="dpk",
                description="Pending Codex doorbells; message bodies remain in the inbox.",
                columns=columns,
            ),
            TABLE_DELIVERY_PICKUP_EVENT: TableSchema(
                table_name=TABLE_DELIVERY_PICKUP_EVENT,
                id_prefix="dpe",
                description="Append-only pickup transition evidence.",
                columns=event_columns,
            ),
        },
    )


class CodexPickupStore:
    """CAS projection plus immutable history; every operation closes its transaction.

    Routing fields are immutable facts of the original send. Correction is a
    new act-time role/session resolution, never rewriting that history. Pending
    failures may be explicitly reopened with a causal version; returned inbox
    evidence is terminal. No operation deletes durable mail or pickup history.
    """

    def __init__(self, state: StateManagementInterface) -> None:
        self.state = state

    def read(self, message_id: str) -> dict[str, object] | None:
        with self.state.transactional() as txn:
            rows = txn.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_DELIVERY_PICKUP,
                    "filters": {"external_id": f"pickup:{message_id}"},
                    "limit": 1,
                },
            )
        return dict(rows[0]) if rows else None

    def remember(
        self,
        message_id: str,
        instance: str,
        session: str,
        role_name: str,
    ) -> dict[str, object]:
        with _PICKUP_LOCK, self.state.transactional() as txn:
            rows = txn.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_DELIVERY_PICKUP,
                    "filters": {"external_id": f"pickup:{message_id}"},
                    "limit": 1,
                },
            )
            if rows:
                return dict(rows[0])
            row: dict[str, object] = {
                "id": f"dpk_{uuid.uuid4().hex}",
                "namespace": AGENT_ROLE_BINDING_NAMESPACE,
                "external_id": f"pickup:{message_id}",
                "message_id": message_id,
                "recipient_instance": instance,
                "recipient_session": session,
                "role_name": role_name,
                "pending": True,
                "phase": "queued",
                "reason": "durable mail; pickup not observed",
                "attempts": 0,
                "version": 1,
                "checked_at": datetime.now(UTC).isoformat(),
                "lease_until": None,
            }
            txn.write_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_DELIVERY_PICKUP,
                    "record": row,
                },
            )
            txn.write_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_DELIVERY_PICKUP_EVENT,
                    "record": self._event(row),
                },
            )
            return row

    @staticmethod
    def _event(row: Mapping[str, object]) -> dict[str, object]:
        return {
            "id": f"dpe_{uuid.uuid4().hex}",
            "namespace": AGENT_ROLE_BINDING_NAMESPACE,
            "external_id": f"pickup-event:{row['message_id']}:{row['version']}",
            "message_id": row["message_id"],
            "version": row["version"],
            "evidence": dict(row),
        }

    def change(
        self,
        row: Mapping[str, object],
        **updates: object,
    ) -> dict[str, object] | None:
        """Refuse stale writers; a returned inbox cannot be reopened by a late drive."""
        updated = dict(row)
        updated.update(updates, version=int(str(row["version"])) + 1, checked_at=datetime.now(UTC).isoformat())
        with self.state.transactional() as txn:
            count = txn.update_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_DELIVERY_PICKUP,
                    "filters": {"external_id": row["external_id"], "version": row["version"]},
                },
                updated,
            )
            if count != 1:
                return None
            txn.write_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_DELIVERY_PICKUP_EVENT,
                    "record": self._event(updated),
                },
            )
        return updated

    def pending(self) -> list[dict[str, object]]:
        result = self.state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_DELIVERY_PICKUP,
                "filters": {"pending": True},
                "order_by": [["checked_at", "asc"], ["id", "asc"]],
                "limit": PICKUP_PAGE_LIMIT,
            },
        )
        return [dict(row) for row in require_records(result)]

    def reopen(self, message_id: str, expected_version: int) -> bool:
        row = self.read(message_id)
        if row is None or row["version"] != expected_version or not row["pending"]:
            return False
        return self.change(row, phase="queued", attempts=0, lease_until=None, reason="explicit retry of retained mail") is not None

    def history(self, message_id: str, after_version: int = 0) -> list[dict[str, object]]:
        result = self.state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_DELIVERY_PICKUP_EVENT,
                "filters": {"message_id": message_id, "version": {"op": "gt", "value": after_version}},
                "order_by": [["version", "asc"], ["id", "asc"]],
                "limit": PICKUP_PAGE_LIMIT + 1,
            },
        )
        return [dict(row) for row in require_records(result)]

    def readback(self, instance: str, session: str) -> dict[str, object]:
        """Existing session_status enrichment; disclose the bounded scan/history."""
        result = self.state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_DELIVERY_PICKUP,
                "order_by": [["checked_at", "desc"], ["id", "desc"]],
                "limit": PICKUP_PAGE_LIMIT + 1,
            },
        )
        rows = require_records(result)
        items: list[dict[str, object]] = []
        for raw in rows[:PICKUP_PAGE_LIMIT]:
            row = dict(raw)
            if not _pickup_readback_matches(row, self.state, instance, session):
                continue
            history = self.history(str(row["message_id"]))
            more = len(history) > PICKUP_PAGE_LIMIT
            items.append({"pickup": row, "history": history[:PICKUP_PAGE_LIMIT], "next_history_version": history[PICKUP_PAGE_LIMIT - 1]["version"] if more else None})
        return {"items": items, "scan_limit": PICKUP_PAGE_LIMIT, "scan_truncated": len(rows) > PICKUP_PAGE_LIMIT, "coverage": "recent pickup records; not a complete recipient inbox"}


def _pickup_readback_matches(
    row: Mapping[str, object],
    state: StateManagementInterface,
    instance: str,
    session: str,
) -> bool:
    try:
        recipient_instance, recipient_session = _pickup_recipient(row, state)
    except Exception:  # noqa: BLE001 -- original addressee may inspect an unresolved routing failure
        recipient_instance, recipient_session = str(row["recipient_instance"]), str(row["recipient_session"])
    return instance == recipient_instance or bool(session and session == recipient_session)


def _pickup_recipient(row: Mapping[str, object], state: StateManagementInterface) -> tuple[str, str]:
    role_name = str(row["role_name"])
    if role_name:
        role = resolve_role_binding(state, role_name)
        if role.agent_id not in {"codex", "claude_code"}:
            raise ValueError("current role holder is not a supported managed recipient")
        return role.agent_instance_id, role.agent_session_id
    return str(row["recipient_instance"]), str(row["recipient_session"])


def _pickup_notice(row: Mapping[str, object]) -> str:
    reference = hashlib.sha256(str(row["message_id"]).encode()).hexdigest()[:16]
    role_reference = hashlib.sha256(str(row["role_name"]).encode()).hexdigest()[:16]
    return f"Solet delivery doorbell (message reference {reference}, role reference {role_reference}). Run solet-bridge inbox to drain both direct and role sections. Read message bodies and task authority from the durable inbox."


def _pickup_drive(
    row: Mapping[str, object],
    state: StateManagementInterface,
) -> DeliveryDrive | DriveOnDeliveryOutcome:
    instance, session = _pickup_recipient(row, state)
    return resolve_delivery_drive(
        state,
        recipient_agent_instance_id=instance,
        recipient_agent_session_id=session,
        sender_label="Solet",
        pickup_notice=_pickup_notice(row),
        recipient_role_name=str(row["role_name"]),
        pickup_message_id=str(row["message_id"]),
        initial_delivery=False,
    )


def _pickup_key(row: Mapping[str, object], state: StateManagementInterface) -> str:
    instance, session = _pickup_recipient(row, state)
    return session or instance


def _pickup_due(row: Mapping[str, object]) -> bool:
    lease = row["lease_until"]
    if not row["pending"]:
        return False
    if not lease:
        return True
    deadline = datetime.fromisoformat(str(lease))
    # Platform DATETIME columns read back as naive UTC, not local wall time.
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline <= datetime.now(UTC)


def _same_pickup_target(
    row: Mapping[str, object],
    state: StateManagementInterface,
    key: str,
) -> bool:
    try:
        return _pickup_key(row, state) == key
    except Exception:  # noqa: BLE001 -- one unresolvable role must not block unrelated mail
        logger.warning("Codex pickup role refresh pending for %s", row["message_id"], exc_info=True)
        return False


def sweep_codex_pickups(state: StateManagementInterface | None) -> int:
    """Existing startup-owned sweeper rider; retry durable pending rows after restart."""
    if state is None:
        return 0
    scheduled = 0
    for row in CodexPickupStore(state).pending():
        try:
            scheduled += int(_schedule_pickup(state, row) == DRIVE_DRIVER_SCHEDULED)
        except Exception:  # noqa: BLE001 -- retained mail survives lookup/schema faults
            logger.exception("Codex pickup pending for %s", row["message_id"])
            _record_pickup_resolution_failure(state, row)
    return scheduled


def _record_pickup_resolution_failure(
    state: StateManagementInterface,
    row: Mapping[str, object],
) -> None:
    try:
        _retain_pickup_resolution(CodexPickupStore(state), row, "current role/driver unresolved; refresh next tick")
    except Exception:  # noqa: BLE001 -- evidence failure must not skip unrelated pending mail
        logger.exception("Codex unresolved pickup evidence write failed for %s", row["message_id"])


def _retain_pickup_resolution(store: CodexPickupStore, row: Mapping[str, object], reason: str) -> None:
    if row["phase"] != "pending_resolution" or row["reason"] != reason or row["lease_until"] is not None:
        store.change(row, phase="pending_resolution", reason=reason, lease_until=None)


def _schedule_pickup(
    state: StateManagementInterface,
    row: Mapping[str, object],
) -> DriveOnDeliveryOutcome:
    if not row["pending"]:
        return DRIVE_DRIVER_UNAVAILABLE
    store = CodexPickupStore(state)
    waiting = _claude_pickup_wait(state, row)
    if waiting is not None:
        return waiting[0]
    if int(str(row["attempts"])) >= PICKUP_ATTEMPT_LIMIT:
        store.change(row, phase="pending_retry_limit", reason="bounded attempts exhausted; mail retained")
        return DRIVE_DRIVER_UNAVAILABLE
    if not _pickup_due(row):
        return DRIVE_DRIVER_SCHEDULED
    key = (id(state), _pickup_key(row, state))
    with _DRIVE_RECORDS_LOCK:
        if key in _ACTIVE_PICKUPS:
            return DRIVE_DRIVER_SCHEDULED
        if _PENDING.value >= DELIVERY_DRIVE_QUEUE_LIMIT:
            return DRIVE_DRIVER_QUEUE_FULL
        _ACTIVE_PICKUPS.add(key)
        _PENDING.value += 1
    try:
        future = DELIVERY_DRIVE_EXECUTOR.submit(_run_pickup, state, dict(row), key)
    except RuntimeError:
        _release_pickup(key)
        return DRIVE_DRIVER_UNAVAILABLE
    future.add_done_callback(lambda done: _release_pickup(key) if done.cancelled() else None)
    _record(str(row["message_id"]), future)
    return DRIVE_DRIVER_SCHEDULED


def _release_pickup(key: tuple[int, str]) -> None:
    with _DRIVE_RECORDS_LOCK:
        _ACTIVE_PICKUPS.discard(key)
        _PENDING.value -= 1


def _run_pickup(
    state: StateManagementInterface,
    row: dict[str, object],
    key: tuple[int, str],
) -> _DriveResult:
    """Resolve again at act time, coalesce pending mail and leave failures readable."""
    try:
        return _drive_pickup_batch(state, row)
    except Exception as exc:  # noqa: BLE001 -- durable sends must stay successful
        logger.exception("Codex doorbell for %s failed", row["message_id"])
        return DRIVE_DRIVER_ERROR, str(exc)
    finally:
        _release_pickup(key)


def _claude_pickup_wait(state: StateManagementInterface, row: Mapping[str, object]) -> _DriveResult | None:
    from .session_lifecycle_control import (  # noqa: PLC0415 -- load after the lifecycle facade initializes
        CLAUDE_PICKUP_PENDING,
        DRIVE_DRIVER_PENDING,
        _resolve_delivery_managed_session,
    )

    instance, session = _pickup_recipient(row, state)
    managed, _reason = _resolve_delivery_managed_session(
        state,
        recipient_agent_instance_id=instance,
        recipient_agent_session_id=session,
    )
    if managed is not None and managed.get("agent_runtime") == "claude_code":
        if row["phase"] != DRIVE_DRIVER_PENDING or row["reason"] != CLAUDE_PICKUP_PENDING:
            CodexPickupStore(state).change(row, phase=DRIVE_DRIVER_PENDING, reason=CLAUDE_PICKUP_PENDING, lease_until=None)
        return DRIVE_DRIVER_PENDING, CLAUDE_PICKUP_PENDING
    return None


def _drive_pickup_batch(state: StateManagementInterface, row: dict[str, object]) -> _DriveResult:
    store = CodexPickupStore(state)
    current = store.read(str(row["message_id"]))
    if current is None or not current["pending"]:
        return DRIVE_DRIVER_UNAVAILABLE, "inbox already returned or pickup record unavailable"
    waiting = _claude_pickup_wait(state, current)
    if waiting is not None:
        return waiting
    if not _pickup_due(current):
        return DRIVE_DRIVER_SCHEDULED, "another drive holds the bounded lease"
    drive = _pickup_drive(current, state)
    if not isinstance(drive, DeliveryDrive):
        _retain_pickup_resolution(store, current, str(drive))
        return drive, "durable mail retained; refresh at next sweep"
    if int(str(current["attempts"])) >= PICKUP_ATTEMPT_LIMIT:
        return DRIVE_DRIVER_UNAVAILABLE, "bounded attempts exhausted; mail retained"
    claimed = _claim_pickup_batch(store, current)
    if not claimed:
        return DRIVE_DRIVER_UNAVAILABLE, "pickup raced with recipient read"
    details: list[str] = []
    outcome = drive.run(details.append)
    detail = details[0] if details else str(outcome)
    _finish_pickup_batch(store, claimed, outcome, detail)
    _warn_unconfirmed_submission(state, current, claimed, outcome, detail)
    _alarm_composer_recovery(state, current, detail)
    logger.info("Codex pickup %s: %s (inbox return still pending)", row["message_id"], detail)
    return outcome, detail


def _warn_unconfirmed_submission(
    state: StateManagementInterface,
    current: Mapping[str, object],
    claimed: list[dict[str, object]],
    outcome: DriveOnDeliveryOutcome,
    detail: str,
) -> None:
    if outcome != DRIVE_DRIVER_ERROR or "submitted_no_turn" not in detail:
        return
    instance, session = _pickup_recipient(current, state)
    capped = any(int(str(item["attempts"])) + 1 >= PICKUP_ATTEMPT_LIMIT for item in claimed)
    logger.warning("Codex pickup %s for instance=%s session=%s role=%s: %s%s", current["message_id"], instance, session, current["role_name"], detail, "; pending_retry_limit" if capped else "")


def _alarm_composer_recovery(
    state: StateManagementInterface,
    current: Mapping[str, object],
    detail: str,
) -> None:
    """A human draft this doorbell could not restore: name it with its exact text."""
    if COMPOSER_RECOVERY_NEEDED not in detail:
        return
    instance, session = _pickup_recipient(current, state)
    logger.error("Codex pickup %s for instance=%s session=%s role=%s: %s", current["message_id"], instance, session, current["role_name"], detail)


def _claim_pickup_batch(
    store: CodexPickupStore,
    current: dict[str, object],
) -> list[dict[str, object]]:
    target_key = _pickup_key(current, store.state)
    batch = [item for item in store.pending() if item["role_name"] == current["role_name"] and _pickup_due(item) and _same_pickup_target(item, store.state, target_key) and int(str(item["attempts"])) < PICKUP_ATTEMPT_LIMIT]
    if not any(item["message_id"] == current["message_id"] for item in batch):
        batch.append(current)
    claimed: list[dict[str, object]] = []
    for item in batch:
        updated = store.change(
            item,
            phase="driver_scheduled",
            reason="submission pending; not inbox consumption",
            lease_until=(datetime.now(UTC) + timedelta(seconds=PICKUP_LEASE_SECONDS)).isoformat(),
        )
        if updated is not None:
            claimed.append(updated)
    return claimed


def _finish_pickup_batch(
    store: CodexPickupStore,
    claimed: list[dict[str, object]],
    outcome: DriveOnDeliveryOutcome,
    detail: str,
) -> None:
    attempted = outcome in {"driver_sent", "driver_turn_observed", "driver_error"} or any(failure in detail for failure in ("pending_escape_failed", "pending_paste_failed", "pending_enter_failed", "composer=recovery_needed"))
    for item in claimed:
        store.change(
            item,
            phase=str(outcome),
            reason=detail,
            lease_until=None,
            attempts=int(str(item["attempts"])) + int(attempted),
        )


def record_inbox_pickup(
    state: StateManagementInterface,
    request: PeerInboxRequest,
    page: PeerInbox,
) -> None:
    """Record exact returned ids from BOTH sections, never observer/read-failure uptake.

    This proves server-side inbox return, not display, message execution, or
    task completion. A truncated/error role section does not conceal a direct
    return, and an empty page does not settle any message.
    """
    if request.observer or request.recipient_agent_id not in {"codex", "claude_code"}:
        return
    for entry in (*page.entries, *page.role_entries):
        try:
            _record_returned_pickup(state, request, str(entry.message.id))
        except Exception:  # noqa: BLE001 -- each returned section/id remains independently recordable
            logger.exception("Codex inbox returned id %s; pickup evidence write failed", entry.message.id)


def _record_returned_pickup(
    state: StateManagementInterface,
    request: PeerInboxRequest,
    message_id: str,
) -> None:
    store = CodexPickupStore(state)
    row = store.read(message_id)
    if row is None or not row["pending"]:
        return
    instance, session = _pickup_recipient(row, state)
    if instance != request.recipient_agent_instance_id and not (session and session == request.recipient_agent_session_id):
        return
    store.change(row, pending=False, phase="inbox_returned", lease_until=None, reason="exact message id returned by recipient peer_inbox; display not proved")


class PickupTrackingMessagingService(AgentMessagingService):
    """Same service semantics on HTTP/MCP/process pulls, with additive pickup evidence."""

    def peer_inbox(self, request: PeerInboxRequest) -> PeerInbox:
        page = super().peer_inbox(request)
        try:
            record_inbox_pickup(self._state, request, page)
        except Exception:  # noqa: BLE001 -- telemetry cannot block the recipient's read
            logger.exception("Codex inbox returned, but pickup evidence could not be recorded")
        return page


def drive_queue_snapshot() -> dict[str, int]:
    """In-process queue counters for ``/api/v1/bridge/health`` (no lock, no I/O)."""
    return {
        "pending": _PENDING.value,
        "limit": DELIVERY_DRIVE_QUEUE_LIMIT,
        "workers": DELIVERY_DRIVE_WORKERS,
    }


def schedule_drive_on_delivery(
    state: StateManagementInterface | None,
    *,
    recipient_agent_instance_id: str,
    recipient_agent_session_id: str,
    sender_label: str,
    message_id: str,
    recipient_agent_id: str = "",
    recipient_role_name: str = "",
) -> DriveOnDeliveryOutcome:
    """Resolve the drive now; run its host half on the delivery-drive executor.

    Returns the lookup's own outcome when no drive is due,
    ``driver_queue_full`` when the queue is at its cap, ``driver_unavailable``
    when the executor has shut down (interpreter exit), else
    ``driver_scheduled``. Each refusal is logged as a WARNING and every
    outcome — refused, scheduled, or decided by the lookup itself — is
    recorded for :func:`finished_drive_outcome`. Never raises: like the inline
    drive it replaces, it must not fail a send whose message is already
    durable.
    """
    if state is not None and recipient_agent_id in {"codex", "claude_code"}:
        outcome = _remember_delivery_pickup(state, message_id, recipient_agent_instance_id, recipient_agent_session_id, recipient_role_name, recipient_agent_id)
        if outcome is not None:
            # NOT routed through _record_lookup_outcome: this branch is not
            # purely lookup-decided. _schedule_pickup hands off a REAL pickup
            # drive that records its own finished outcome against this same
            # message id, and a synthetic record written here would clobber it
            # (drive_on_delivery_smoke's "finished Codex role lookup preserves
            # not_managed" leg is exactly that read).
            return outcome
    drive = resolve_delivery_drive(
        state,
        recipient_agent_instance_id=recipient_agent_instance_id,
        recipient_agent_session_id=recipient_agent_session_id,
        sender_label=sender_label,
        recipient_role_name=recipient_role_name,
        pickup_message_id=message_id if recipient_agent_id == "claude_code" else "",
    )
    if not isinstance(drive, DeliveryDrive):
        return _record_lookup_outcome(message_id, recipient_agent_instance_id, drive)
    with _DRIVE_RECORDS_LOCK:
        pending = _PENDING.value
        admitted = pending < DELIVERY_DRIVE_QUEUE_LIMIT
        if admitted:
            _PENDING.value = pending + 1
    if not admitted:
        return _refuse(
            message_id,
            drive,
            DRIVE_DRIVER_QUEUE_FULL,
            f"{pending} drives pending (limit {DELIVERY_DRIVE_QUEUE_LIMIT})",
        )
    try:
        future = DELIVERY_DRIVE_EXECUTOR.submit(
            _run_and_log,
            drive,
            message_id,
            time.monotonic(),
        )
    except RuntimeError as exc:  # submit after shutdown: interpreter exit began
        with _DRIVE_RECORDS_LOCK:
            _PENDING.value -= 1
        return _refuse(
            message_id,
            drive,
            DRIVE_DRIVER_UNAVAILABLE,
            f"delivery-drive executor is shut down: {exc}",
        )
    future.add_done_callback(_release_cancelled_drive)
    _record(message_id, future)
    return DRIVE_DRIVER_SCHEDULED


def _remember_delivery_pickup(
    state: StateManagementInterface,
    message_id: str,
    instance: str,
    session: str,
    role: str,
    agent_id: str,
) -> DriveOnDeliveryOutcome | None:
    try:
        row = CodexPickupStore(state).remember(message_id, instance, session, role)
        if agent_id == "codex":
            return _schedule_pickup(state, row)
        if row["version"] != 1:
            return "driver_pending"
    except Exception:  # noqa: BLE001 -- never fail a persisted message
        logger.exception("Managed pickup could not be scheduled for %s", message_id)
        return DRIVE_DRIVER_UNAVAILABLE
    return None


# The manual equivalent for a seat no driver may reach (prb_351c9641). An
# operator-hosted seat is attended, so the platform never types into it; this
# is advice for the operator, never a gate. Shared with the driver-channel
# verbs' ``unsupported_on_host`` text so the instruction has one source.
OPERATOR_SEAT_MANUAL_WAKE: Final[str] = "to wake an attended seat by hand, type `drain peer_inbox` (or run `solet-bridge inbox`) in its own terminal; /clear and /compact are likewise typed there"

_LOOKUP_OUTCOME_DETAILS: Final[dict[str, str]] = {
    DRIVE_NOT_MANAGED: "no managed_session row for this recipient, so no drive applies; durable mail is retained and read on the recipient's next inbox read",
    DRIVE_INELIGIBLE_STATE: "the managed row's lifecycle state is not drive-eligible; durable mail is retained and read on the recipient's next inbox read",
    DRIVE_DRIVER_UNAVAILABLE: f"the managed row's host has no driver channel, so no wake mechanism exists for it; durable mail is retained and read on the recipient's next inbox read; {OPERATOR_SEAT_MANUAL_WAKE}",
}


def _record_lookup_outcome(
    message_id: str,
    recipient_agent_instance_id: str,
    outcome: DriveOnDeliveryOutcome,
) -> DriveOnDeliveryOutcome:
    """Keep a lookup-decided outcome readable by message id.

    The scheduled and refused paths each record a future, so
    :func:`finished_drive_outcome` can answer for them. The outcomes the
    managed-row lookup decides on its own recorded nothing, so that read
    answered ``None`` — indistinguishable from an aged-out record or a drive
    still running. The one that matters most is ``driver_unavailable`` for a
    host with no driver channel, which is exactly the channel-absent seat this
    answer was never available for.

    Only :func:`resolve_delivery_drive`'s own outcome comes here. The managed
    pickup branch is deliberately excluded: it can hand off a real pickup drive
    that records its own finished outcome under the same message id, and a
    synthetic record would clobber that one.

    Returns the outcome unchanged. This is a report, not a gate: it adds no
    refusal, drops no message and changes nothing about durability.

    Logged at DEBUG rather than :func:`_refuse`'s WARNING, because
    ``not_managed`` is the ordinary outcome for every non-managed recipient and
    a warning per send would be noise, not a signal.
    """
    detail = _LOOKUP_OUTCOME_DETAILS.get(
        outcome,
        "the managed-row lookup decided this outcome; no channel send was attempted and durable mail is retained",
    )
    logger.debug(
        "drive_on_delivery: no drive for message %s to %s: %s (%s)",
        message_id,
        recipient_agent_instance_id,
        outcome,
        detail,
    )
    decided: Future[_DriveResult] = Future()
    decided.set_result((outcome, detail))
    _record(message_id, decided)
    return outcome


def _refuse(
    message_id: str,
    drive: DeliveryDrive,
    outcome: DriveOnDeliveryOutcome,
    detail: str,
) -> DriveOnDeliveryOutcome:
    """Log and record a drive that was not scheduled."""
    logger.warning(
        "drive_on_delivery: drive for message %s to %s not scheduled: %s (%s)",
        message_id,
        drive.recipient_agent_instance_id,
        outcome,
        detail,
    )
    refused: Future[_DriveResult] = Future()
    refused.set_result((outcome, detail))
    _record(message_id, refused)
    return outcome


def _record(message_id: str, future: Future[_DriveResult]) -> None:
    with _DRIVE_RECORDS_LOCK:
        _DRIVE_RECORDS[message_id] = future
        while len(_DRIVE_RECORDS) > DRIVE_RECORD_LIMIT:
            _DRIVE_RECORDS.popitem(last=False)


def finished_drive_outcome(message_id: str, *, timeout_s: float) -> _DriveResult | None:
    """The finished ``(outcome, detail)`` of a drive, by message id.

    Waits up to ``timeout_s`` for a drive still queued or running. ``None``
    when no drive was scheduled or refused for ``message_id`` (or its record
    aged out), or when it has not finished within ``timeout_s``.
    """
    with _DRIVE_RECORDS_LOCK:
        future = _DRIVE_RECORDS.get(message_id)
    if future is None:
        return None
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeoutError:
        return None
    except CancelledError:
        return DRIVE_DRIVER_UNAVAILABLE, "plugin stopped before queued drive ran; durable mail retained"


def _release_cancelled_drive(future: Future[_DriveResult]) -> None:
    if future.cancelled():
        with _DRIVE_RECORDS_LOCK:
            _PENDING.value -= 1


def _run_and_log(drive: DeliveryDrive, message_id: str, queued_at: float) -> _DriveResult:
    """Run one queued drive and record its finished outcome and queue wait."""
    waited_s = time.monotonic() - queued_at
    details: list[str] = []
    try:
        outcome = drive.run(details.append)
    except Exception:  # noqa: BLE001 — a worker fault must still be recorded
        logger.exception(
            "drive_on_delivery: drive for message %s to %s faulted after %.2fs queued",
            message_id,
            drive.recipient_agent_instance_id,
            waited_s,
        )
        return DRIVE_DRIVER_ERROR, None
    finally:
        with _DRIVE_RECORDS_LOCK:
            _PENDING.value -= 1
    detail = details[0] if details else None
    log = logger.warning if outcome == DRIVE_DRIVER_ERROR else logger.info
    log(
        "drive_on_delivery: drive for message %s to %s finished %s after %.2fs queued%s",
        message_id,
        drive.recipient_agent_instance_id,
        outcome,
        waited_s,
        f" ({detail})" if detail else "",
    )
    return outcome, detail


__all__ = [
    "DELIVERY_DRIVE_EXECUTOR",
    "DELIVERY_DRIVE_QUEUE_LIMIT",
    "DELIVERY_DRIVE_THREAD_PREFIX",
    "DELIVERY_DRIVE_WORKERS",
    "DRIVE_RECORD_LIMIT",
    "CodexPickupStore",
    "PickupTrackingMessagingService",
    "get_delivery_pickup_schema",
    "record_inbox_pickup",
    "start_delivery_drives",
    "stop_delivery_drives",
    "sweep_codex_pickups",
    "drive_queue_snapshot",
    "finished_drive_outcome",
    "schedule_drive_on_delivery",
]
