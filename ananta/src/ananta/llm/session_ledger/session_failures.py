"""Durable parse diagnostics, separate from successful event checkpoints.

The source lease authorizes writes; each transition locks/rechecks that lease
and the expected current receipt. Old receipt values remain readable after a
new current receipt supersedes them. File changes clear a failure; a new
parser digest starts a separate version receipt and reopens reading. Advisory state survives file changes within a parser version.
No transcript text is stored here and no cursor is written by this module.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ananta.llm.session_ledger.filesystem_poll import FilesystemCandidate, FilesystemPollingMixin, FilesystemPollRef
from ananta.llm.session_ledger.poll_advisories import claude_line_context
from ananta.llm.session_ledger.poll_advisories import record_unknown_line as record_poll_unknown_line
from ananta.llm.session_ledger.types import ExternalSessionRef, RawSessionEvent
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition, IndexDefinition, TableSchema

if TYPE_CHECKING:
    from ananta.interfaces.llm_session_source_interface import PullingSourceMixin
    from ananta.llm.session_ledger.lease import LeaseHeartbeat
    from ananta.llm.session_ledger.repository import SessionLedgerRepository, SourceRow

RELOAD_SAFE = True
TABLE_SESSION_FAILURE = "session_failure"
logger = logging.getLogger(__name__)
_reporter: ContextVar[Callable[[str, str | None], None] | None] = ContextVar("session_parse_reporter", default=None)
_persisted: ContextVar[list[int] | None] = ContextVar("session_parse_persisted", default=None)
_FIELDS = ("source_id", "external_session_id", "parser_version", "content_fingerprint", "status", "first_failed_at", "advisory_raised", "notice_sent")


def session_failure_table() -> TableSchema:
    """Registered by the existing core ledger schema at every startup."""
    return TableSchema(
        table_name=TABLE_SESSION_FAILURE,
        id_prefix="sfl",
        description="Historical per-session/parser parse receipts; current pointer is source-lease fenced.",
        columns={
            "source_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True, description="repository_fk: source.id"),
            "external_session_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "parser_version": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "content_fingerprint": ColumnDefinition(type=ColumnType.JSON, not_null=True),
            "status": ColumnDefinition(type=ColumnType.TEXT, not_null=True, check="status IN ('ready', 'failed', 'cleared')"),
            "first_failed_at": ColumnDefinition(type=ColumnType.DATETIME),
            "advisory_raised": ColumnDefinition(type=ColumnType.BOOLEAN, not_null=True),
            "notice_sent": ColumnDefinition(type=ColumnType.BOOLEAN, not_null=True),
            "is_current": ColumnDefinition(type=ColumnType.BOOLEAN, not_null=True),
            "previous_id": ColumnDefinition(type=ColumnType.TEXT),
        },
        indexes=[
            IndexDefinition(name="idx_session_failure_current", columns=["source_id", "external_session_id", "parser_version"], unique=True, where="is_current = true"),
            IndexDefinition(name="idx_session_failure_history", columns=["source_id", "external_session_id", "created_at", "id"]),
        ],
    )


def parser_version(plugin: PullingSourceMixin) -> str:
    """Code digest, so a shipped parser correction reopens unchanged failures.

    Evaluated once per source visit, never once per physical transcript line.
    All vendor modules are included because adapters can share parser helpers.
    """
    paths = sorted((Path(__file__).parent / "vendor").glob("*.py"))
    source = inspect.getsourcefile(type(plugin))
    if source is not None:
        paths.append(Path(source))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class SessionReadError(ValueError):
    """The durable receipt already owns the one failure notice."""

    def __init__(self, session_id: str, events_persisted: int = 0) -> None:
        super().__init__(session_id)
        self.events_persisted = events_persisted


class SessionFailureRecords:
    """One source visit, with durable reads and lease-fenced receipt writes."""

    def __init__(self, repository: SessionLedgerRepository, source: SourceRow, plugin: PullingSourceMixin, heartbeat: LeaseHeartbeat) -> None:
        self.repository = repository
        self.source = source
        self.heartbeat = heartbeat
        self.version = parser_version(plugin)

    def _identity(self, session_id: str) -> dict[str, object]:
        return {"source_id": self.source.id, "external_session_id": session_id, "parser_version": self.version, "is_current": True}

    def lookup(self, session_id: str) -> dict[str, object] | None:
        """Current receipt; history remains available through the same table."""
        rows = self.repository._query(TABLE_SESSION_FAILURE, self._identity(session_id))
        return rows[0] if rows else None

    def _append(self, ref: FilesystemPollRef, previous: dict[str, object] | None, changes: dict[str, object]) -> dict[str, object]:
        from ananta.llm.session_ledger.repository import LeaseLostError, LedgerRepositoryError  # noqa: PLC0415
        from ananta.llm.session_ledger.schema import NAMESPACE, TABLE_SOURCE  # noqa: PLC0415

        now = self.repository._clock().astimezone(UTC).replace(tzinfo=None)
        record: dict[str, object] = {
            "source_id": self.source.id,
            "external_session_id": ref.external_session_id,
            "parser_version": self.version,
            "content_fingerprint": ref.selected_version,
            "status": "ready",
            "first_failed_at": None,
            "advisory_raised": False,
            "notice_sent": False,
        }
        if previous is not None:
            record.update({key: previous[key] for key in _FIELDS})
        record.update(changes)
        record.update({"id": f"sfl_{uuid.uuid4().hex}", "namespace": NAMESPACE, "created_at": now, "updated_at": now, "is_deleted": 0, "is_current": True, "previous_id": previous["id"] if previous else None})
        with self.repository._state.transactional() as txn:
            # Same-value UPDATE locks the source row and fences a stale owner.
            # A successor cannot take custody between this test and the insert.
            token = self.heartbeat.handle.lease_token
            held = txn.update_state(NAMESPACE, {"table": TABLE_SOURCE, "filters": {"id": self.source.id, "polling_lease_token": token, "polling_lease_until": {"op": "gte", "value": now}}}, {"polling_lease_token": token})
            if held != 1:
                raise LeaseLostError("parse receipt refused: source lease no longer held")
            current = txn.query_state(NAMESPACE, {"table": TABLE_SESSION_FAILURE, "filters": self._identity(ref.external_session_id)})
            actual = current[0]["id"] if current else None
            expected = previous["id"] if previous else None
            if actual != expected:
                raise LedgerRepositoryError("parse receipt refused: stale current receipt")
            if previous is not None:
                txn.update_state(NAMESPACE, {"table": TABLE_SESSION_FAILURE, "filters": {"id": expected, "is_current": True}}, {"is_current": False})
            txn.write_state(NAMESPACE, {"table": TABLE_SESSION_FAILURE, "record": record})
        return record

    def blocked(self, ref: FilesystemPollRef) -> bool:
        row = self.lookup(ref.external_session_id)
        return row is not None and row["status"] == "failed" and row["content_fingerprint"] == ref.selected_version

    def clear_on_change(self, ref: FilesystemPollRef) -> None:
        """Append a reversal when a failed file's stat/mtime version changes."""
        row = self.lookup(ref.external_session_id)
        if row is not None and row["status"] == "failed" and row["content_fingerprint"] != ref.selected_version:
            self._append(ref, row, {"status": "cleared", "content_fingerprint": ref.selected_version, "first_failed_at": None, "notice_sent": False})

    def fail(self, ref: FilesystemPollRef, exc: Exception) -> None:
        """Record one failed version and emit its one existing warning notice."""
        row = self.lookup(ref.external_session_id)
        if row is not None and row["status"] == "failed" and row["content_fingerprint"] == ref.selected_version:
            return
        self._append(ref, row, {"status": "failed", "content_fingerprint": ref.selected_version, "first_failed_at": self.repository._clock().astimezone(UTC).replace(tzinfo=None), "notice_sent": True})
        logger.warning("ledger poll: FAILED session %s (%s) parser=%s; unchanged file will not be read again: %s", ref.external_session_id, self.source.source_kind.value, self.version, exc)

    def advise(self, ref: FilesystemPollRef, line_type: str, session_id: str | None) -> None:
        row = self.lookup(ref.external_session_id)
        if row is not None and row["advisory_raised"] is True:
            return
        self._append(ref, row, {"advisory_raised": True, "content_fingerprint": ref.selected_version})
        with claude_line_context(self.source.root_uri, ref.external_session_id, 0):
            record_poll_unknown_line(line_type, session_id)

    @contextmanager
    def reading(self, ref: ExternalSessionRef) -> Generator[None]:
        if not isinstance(ref, FilesystemPollRef):
            yield
            return
        if self.blocked(ref):
            raise SessionReadError(ref.external_session_id)
        self.clear_on_change(ref)
        token = _reporter.set(lambda kind, session: self.advise(ref, kind, session))
        progress = [0]
        progress_token = _persisted.set(progress)
        try:
            yield
        except (ValueError, OSError) as exc:
            self.fail(ref, exc)
            raise SessionReadError(ref.external_session_id, progress[0]) from exc
        finally:
            _reporter.reset(token)
            _persisted.reset(progress_token)


class FailureAwareDiscovery(FilesystemPollingMixin):
    """Reuse existing discovery policy while suppressing failed header reads."""

    def __init__(self, plugin: FilesystemPollingMixin, records: SessionFailureRecords) -> None:
        self.plugin = plugin
        self.records = records

    def _poll_candidates(self, root_uri: str) -> Iterator[FilesystemCandidate]:
        return self.plugin._poll_candidates(root_uri)

    def discover_sessions(self, root_uri: str, cursor_payload: dict[str, object] | None) -> Iterator[ExternalSessionRef]:
        return self.plugin.discover_sessions(root_uri, cursor_payload)

    def read_events(self, root_uri: str, session: ExternalSessionRef, cursor_payload: dict[str, object] | None) -> Iterator[RawSessionEvent]:
        return self.plugin.read_events(root_uri, session, cursor_payload)

    def session_discovery_cursor(self, root_uri: str, last_seen: ExternalSessionRef | None) -> dict[str, object]:
        return self.plugin.session_discovery_cursor(root_uri, last_seen)

    def event_read_cursor(self, root_uri: str, session: ExternalSessionRef, last_event: RawSessionEvent | None) -> dict[str, object]:
        return self.plugin.event_read_cursor(root_uri, session, last_event)

    def _poll_ref(self, root_uri: str, candidate: FilesystemCandidate) -> ExternalSessionRef:
        ref = FilesystemPollRef(candidate.external_id, None, None, datetime.fromtimestamp(candidate.mtime_ns / 1_000_000_000, UTC), selected_version=candidate.version, candidate=candidate)
        if self.records.blocked(ref):
            return ref
        self.records.clear_on_change(ref)
        try:
            return self.plugin._poll_ref(root_uri, candidate)
        except (ValueError, OSError) as exc:
            self.records.fail(ref, exc)
            return ref


def failure_records(repository: SessionLedgerRepository, source: SourceRow, plugin: PullingSourceMixin, heartbeat: LeaseHeartbeat) -> SessionFailureRecords | None:
    """Only filesystem adapters have an authoritative observed file version."""
    return SessionFailureRecords(repository, source, plugin, heartbeat) if isinstance(plugin, FilesystemPollingMixin) else None


def discovery_source(plugin: PullingSourceMixin, records: SessionFailureRecords | None) -> PullingSourceMixin:
    return FailureAwareDiscovery(plugin, records) if records is not None and isinstance(plugin, FilesystemPollingMixin) else plugin


def reading_session(records: SessionFailureRecords | None, ref: ExternalSessionRef) -> AbstractContextManager[None]:
    return records.reading(ref) if records is not None else nullcontext()


def record_persisted_events(count: int) -> None:
    progress = _persisted.get()
    if progress is not None:
        progress[0] += count


def record_unknown_line(line_type: str, external_session_id: str | None = None) -> None:
    """Reuse poll diagnostics; importer custody adds durable session-level dedup."""
    report = _reporter.get()
    if report is None:
        record_poll_unknown_line(line_type, external_session_id)
    else:
        report(line_type, external_session_id)
