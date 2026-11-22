"""Session limits: how many sessions a solet runs at once, and the one question "is there room".

A limit is a selector, a whole number of sessions at once, the user's words and an optional end time. The selector is the
rule selector by reference (``model_cell_enablement._DIMENSIONS``, ``_selector`` and ``matches``, imported and never copied),
so a dimension added to rules applies to limits with no edit here. Every matching limit must hold; 0 means start nothing new
while running work finishes; a lapsed limit does not apply and reads as lapsed. Only live sessions the solet itself started
are counted, so a session a person started by hand is never counted and never refused.

A limit counts nothing but sessions at once. It names no asker and no project: the caller is any asker with a candidate
session in hand, and nothing here assumes another system is present.

The store keeps one row per revision of a limit and never edits history. ``correct`` closes the active revision as
``replaced`` and writes the next one, ``retire`` closes it as ``retired``, and both name the revision they read
(``expected_revision``): when it is no longer the active one the call writes nothing and raises ``stale_limit``. There is no
reopen; a retired limit is set again as a new limit, and the retired one stays readable in the history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE as NS
from ananta.llm.agent_messaging.state_results import require_records
from ananta.services.state_service.bounded_read import iter_table_rows
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import ColumnDefinition, IndexDefinition, SchemaDefinition, TableSchema

from .model_capability_store import CatalogError, read_cells_page
from .model_cell_enablement import _DIMENSIONS, _selector, matches
from .schema import LIFECYCLE_RETIRED, LIFECYCLE_TERMINATED, LIFECYCLE_TRANSITIONS
from .session_hosts import DEFAULT_AGENT_RUNTIME
from .session_lifecycle_store import list_managed_sessions

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface, StateTransaction

TABLE_SESSION_LIMIT: Final = "session_limit"

# Live is every lifecycle state a session can be in before it ends: the keys of the transition matrix, dict order, minus the
# two that end it (terminated, retired).
LIVE_STATES: Final[tuple[str, ...]] = tuple(name for name in LIFECYCLE_TRANSITIONS if name not in (LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED))

_ACTIVE: Final = "active"
_REPLACED: Final = "replaced"
_RETIRED: Final = "retired"
_ACTIONS: Final = ("set", "correct", "retire")
_WRITER: Final = "session_limits"
_HISTORY_MAX: Final = 100

# The columns every table carries; a view of a limit shows its own columns and not these.
_STANDARD_COLUMNS: Final = frozenset({"id", "external_id", "namespace", "name", "created_at", "updated_at", "created_by", "updated_by", "is_deleted"})

# A selector dimension a session row holds in a column of its own. provider is read from the catalog, runtime has a default,
# and model_family_prefix is a prefix test on model, so none of the three is a column.
_ROW_DIMENSIONS: Final = tuple(sorted(_DIMENSIONS - {"provider", "runtime", "model_family_prefix"}))

_ACTIVE_CEILING: Final = 10_000
_ACTIVE_CEILING_REASON: Final = "a person sets each limit by hand and retires the ones that no longer apply, so a list of active limits is short; ten thousand active limits is a defect to look at, not a list to walk."


class LimitError(Exception):
    """A limit operation was refused. ``code`` is the stable token for the verb layer: parameter_invalid or stale_limit."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _lapsed(ends_at: datetime | None, now: datetime) -> bool:
    return ends_at is not None and ends_at <= now


@dataclass(frozen=True, slots=True)
class SessionLimit:
    """One limit as the room function reads it. ``ends_at`` is None for a limit with no end time."""

    limit_id: str
    revision: int
    selector: dict[str, str]
    sessions: int
    words: str
    ends_at: datetime | None

    def lapsed(self, now: datetime) -> bool:
        return _lapsed(self.ends_at, now)


@dataclass(frozen=True, slots=True)
class Refusal:
    """A limit that has no room: how many counted sessions match it, and which."""

    limit: SessionLimit
    count: int
    in_use: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoomAnswer:
    """Whether there is room for a candidate; when there is not, every limit that refused, in the order the limits were given."""

    room: bool
    refusals: tuple[Refusal, ...]

    def to_dict(self) -> dict[str, object]:
        return {"room": self.room, "refusals": [_refusal_dict(refusal) for refusal in self.refusals]}


def _refusal_dict(refusal: Refusal) -> dict[str, object]:
    limit = refusal.limit
    return {
        "limit_id": limit.limit_id,
        "revision": limit.revision,
        "selector": dict(limit.selector),
        "sessions": limit.sessions,
        "words": limit.words,
        "ends_at": None if limit.ends_at is None else limit.ends_at.isoformat(),
        "count": refusal.count,
        "in_use": list(refusal.in_use),
    }


def get_session_limit_schema_definition() -> SchemaDefinition:
    """The limit table, installed by the normal schema lifecycle: one row per revision of a limit."""
    return SchemaDefinition(
        namespace=NS,
        version="1.0.0",
        description="Session limits: how many sessions a solet runs at once, one append-only row per revision of a limit.",
        tables={
            TABLE_SESSION_LIMIT: TableSchema(
                table_name=TABLE_SESSION_LIMIT,
                id_prefix="slm",
                description="One row per revision of a session limit; a correction or a retirement closes the active row and never edits its content.",
                columns={
                    "limit_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True, description="Identity of the limit, shared by every revision of it."),
                    "revision": ColumnDefinition(type=ColumnType.INTEGER, not_null=True, description="1 for the limit as set; each correction adds one."),
                    "selector": ColumnDefinition(type=ColumnType.JSON, not_null=True, description="The rule selector the limit applies to; {} applies to every session."),
                    "sessions": ColumnDefinition(type=ColumnType.INTEGER, not_null=True, description="How many matching sessions may run at once; 0 starts nothing new."),
                    "words": ColumnDefinition(type=ColumnType.TEXT, not_null=True, description="The user's words for the limit, kept as given."),
                    "state": ColumnDefinition(type=ColumnType.TEXT, not_null=True, description="active, replaced (a correction followed) or retired."),
                    "reason": ColumnDefinition(type=ColumnType.TEXT, not_null=True, description="Why this revision was written."),
                    "set_by": ColumnDefinition(type=ColumnType.TEXT, not_null=True, description="Who wrote this revision."),
                    "ends_at": ColumnDefinition(type=ColumnType.TEXT, description="ISO-8601 UTC time from which the limit no longer applies; null for no end time."),
                    "ended_at": ColumnDefinition(type=ColumnType.TEXT, description="ISO-8601 UTC time this revision stopped being the active one; null while it is."),
                    "ended_by": ColumnDefinition(type=ColumnType.TEXT, description="Who replaced or retired this revision; null while it is active."),
                    "ended_reason": ColumnDefinition(type=ColumnType.TEXT, description="Why this revision was replaced or retired; null while it is active."),
                },
                indexes=[
                    IndexDefinition(name="idx_session_limit_revision", columns=["limit_id", "revision"], unique=True),
                    IndexDefinition(name="idx_session_limit_state", columns=["state"]),
                ],
            )
        },
    )


def _checked_selector(raw: object) -> dict[str, str]:
    try:
        return _selector(raw)
    except CatalogError as exc:
        raise LimitError(exc.code, exc.message) from exc


def selector_view(row: Mapping[str, Any], providers: Mapping[tuple[str, str, str], str]) -> dict[str, str]:
    """The dimensions of a session row, in the names a selector uses.

    runtime is the row's agent_runtime, or the default runtime when the row has none. Each other dimension the row holds
    non-empty is copied as text. provider comes from ``providers`` by the row's (runtime, model, effort), and is left out
    when no catalog cell has that key, so a limit that names a provider does not match a row it cannot place.
    """
    runtime = str(row.get("agent_runtime") or DEFAULT_AGENT_RUNTIME)
    view = {"runtime": runtime}
    for name in _ROW_DIMENSIONS:
        held = row.get(name)
        if held is not None and str(held):
            view[name] = str(held)
    provider = providers.get((runtime, str(row.get("model") or ""), str(row.get("effort") or "")))
    if provider is not None:
        view["provider"] = provider
    return view


def _is_counted(row: Mapping[str, Any]) -> bool:
    return row.get("lifecycle_state") in LIVE_STATES and row.get("session_origin") == "spawned"


def room_for(
    candidate: Mapping[str, Any],
    limits: Sequence[SessionLimit],
    sessions: Sequence[Mapping[str, Any]],
    providers: Mapping[tuple[str, str, str], str],
    now: datetime,
) -> RoomAnswer:
    """Whether the candidate may start, from the rows it is given; it reads nothing from the store.

    A session counts when it is live and the solet started it. Each limit, in the order given, that has not lapsed and
    matches the candidate refuses when as many counted sessions already match it as it allows. Every such limit must hold.
    The refusal names the counted sessions, so a count that is too high shows which sessions it came from.
    """
    candidate_view = selector_view(candidate, providers)
    counted = [(str(row["agent_instance_id"]), selector_view(row, providers)) for row in sessions if _is_counted(row)]
    refusals: list[Refusal] = []
    for limit in limits:
        if limit.lapsed(now) or not matches(limit.selector, candidate_view):
            continue
        in_use = sorted(instance for instance, view in counted if matches(limit.selector, view))
        if len(in_use) >= limit.sessions:
            refusals.append(Refusal(limit, len(in_use), tuple(in_use)))
    return RoomAnswer(not refusals, tuple(refusals))


def _stored_end(raw: object) -> datetime | None:
    return datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None


def _active_rows(state: StateManagementInterface) -> list[dict[str, object]]:
    """Every active limit row, oldest first: the order the limits are applied and shown in."""
    return list(iter_table_rows(state, namespace=NS, table=TABLE_SESSION_LIMIT, filters={"state": _ACTIVE}, ceiling=_ACTIVE_CEILING, reason=_ACTIVE_CEILING_REASON))


def _limit_of(row: Mapping[str, Any]) -> SessionLimit:
    return SessionLimit(str(row["limit_id"]), int(row["revision"]), _checked_selector(row.get("selector")), int(row["sessions"]), str(row["words"]), _stored_end(row.get("ends_at")))


def _provider_map(cells: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str], str]:
    return {(str(cell["runtime"]), str(cell["model"]), str(cell["effort"])): str(cell["provider"]) for cell in cells if cell.get("provider")}


def read_room(state: StateManagementInterface, candidate: Mapping[str, Any], now: datetime) -> RoomAnswer:
    """Is there room for the candidate, reading the active limits, the catalog and the live sessions the solet started.

    With no active limit there is room, and nothing else is read.
    """
    rows = _active_rows(state)
    if not rows:
        return RoomAnswer(True, ())
    providers = _provider_map(read_cells_page(state, ceiling=None).rows)
    sessions = list_managed_sessions(state, {"lifecycle_state": list(LIVE_STATES), "session_origin": "spawned"})
    return room_for(candidate, [_limit_of(row) for row in rows], sessions, providers, now)


def _invalid(message: str) -> LimitError:
    return LimitError("parameter_invalid", message)


def _text(raw: object, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid(f"{name} must be a nonblank string.")
    return raw


def _whole(raw: object, name: str, *, least: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < least:
        raise _invalid(f"{name} must be a whole number of at least {least}.")
    return raw


def _end_time(raw: object) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _invalid("ends_at must be an ISO-8601 time with a UTC offset, or omitted.")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise _invalid(f"ends_at is not an ISO-8601 time: {raw!r}.") from exc
    if parsed.utcoffset() is None:
        raise _invalid("ends_at must carry a UTC offset.")
    return parsed.astimezone(UTC)


def _content(params: Mapping[str, Any], now: datetime) -> tuple[dict[str, object], list[str]]:
    """The fields a set or a correct supplies, validated, with a warning when the end time is already past.

    A correct replaces every field with what is supplied, so an omitted ends_at is stored as no end time.
    """
    selector = _checked_selector(params.get("selector"))
    sessions = _whole(params.get("sessions"), "sessions", least=0)
    words = _text(params.get("words"), "words")
    ends_at = _end_time(params.get("ends_at"))
    warnings = ["ends_at is already past: the limit is lapsed and applies to nothing."] if _lapsed(ends_at, now) else []
    return {"selector": selector, "sessions": sessions, "words": words, "ends_at": None if ends_at is None else ends_at.isoformat()}, warnings


def _target(params: Mapping[str, Any]) -> tuple[str, int]:
    return _text(params.get("limit_id"), "limit_id"), _whole(params.get("expected_revision"), "expected_revision", least=1)


def _revision_record(limit_id: str, revision: int, content: dict[str, object], reason: str, actor: str, now: datetime) -> dict[str, object]:
    identifier = f"slm-{uuid4().hex}"
    row = {"id": identifier, "name": identifier, "namespace": NS, "created_at": now, "updated_at": now, "created_by": _WRITER, "updated_by": _WRITER}
    return {"table": TABLE_SESSION_LIMIT, "record": {**row, "limit_id": limit_id, "revision": revision, "state": _ACTIVE, "reason": reason, "set_by": actor, **content}}


def _end_revision(txn: StateTransaction, limit_id: str, revision: int, ended_as: str, actor: str, reason: str, now: datetime) -> None:
    """Close the active row at ``revision``; when it is not the active one, nothing in the transaction commits."""
    ended: dict[str, object] = {"state": ended_as, "ended_at": now.astimezone(UTC).isoformat(), "ended_by": actor, "ended_reason": reason, "updated_at": now}
    changed = txn.update_state(NS, {"table": TABLE_SESSION_LIMIT, "filters": {"limit_id": limit_id, "revision": revision, "state": _ACTIVE}}, ended)
    if changed != 1:
        raise LimitError("stale_limit", "That limit is not active at that revision; read the limits again. No changes committed.")


def _create(state: StateManagementInterface, params: Mapping[str, Any], actor: str, reason: str, now: datetime) -> tuple[str, int, list[str]]:
    content, warnings = _content(params, now)
    limit_id = f"lim-{uuid4().hex}"
    with state.transactional() as txn:
        txn.write_state(NS, _revision_record(limit_id, 1, content, reason, actor, now))
    return limit_id, 1, warnings


def _correct(state: StateManagementInterface, params: Mapping[str, Any], actor: str, reason: str, now: datetime) -> tuple[str, int, list[str]]:
    limit_id, expected = _target(params)
    content, warnings = _content(params, now)
    with state.transactional() as txn:
        _end_revision(txn, limit_id, expected, _REPLACED, actor, reason, now)
        txn.write_state(NS, _revision_record(limit_id, expected + 1, content, reason, actor, now))
    return limit_id, expected + 1, warnings


def _retire(state: StateManagementInterface, params: Mapping[str, Any], actor: str, reason: str, now: datetime) -> tuple[str, int, list[str]]:
    limit_id, expected = _target(params)
    with state.transactional() as txn:
        _end_revision(txn, limit_id, expected, _RETIRED, actor, reason, now)
    return limit_id, expected, []


def _view(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """A row without its standard columns, plus whether its end time has passed and when it was set."""
    own = {key: value for key, value in row.items() if key not in _STANDARD_COLUMNS}
    return {**own, "lapsed": _lapsed(_stored_end(row.get("ends_at")), now), "set_at": row.get("created_at")}


def _revision_row(state: StateManagementInterface, limit_id: str, revision: int) -> dict[str, object]:
    rows = require_records(state.query_ordered(NS, {"table": TABLE_SESSION_LIMIT, "filters": {"limit_id": limit_id, "revision": revision}, "order_by": [["created_at", "asc"], ["id", "asc"]], "limit": 1}))
    if not rows:
        raise RuntimeError(f"limit {limit_id} revision {revision} was committed and cannot be read back")
    return rows[0]


def set_limit(state: StateManagementInterface, params: Mapping[str, Any], *, actor: str, now: datetime | None = None) -> dict[str, Any]:
    """Set, correct or retire one limit; called only after the plugin derives and authorizes the current Operator.

    ``action`` is set (the default), correct or retire, and ``reason`` is always required. set and correct take selector
    (required, {} allowed), sessions (a whole number, 0 or more), words (nonblank, kept as given) and ends_at (an optional
    ISO-8601 time with a UTC offset, stored as UTC text). set writes revision 1 of a new limit. correct and retire take
    limit_id and expected_revision, the revision the caller read: they close that row as replaced or retired with who, when
    and why, in one transaction, and a row that is not the active one at that revision raises stale_limit with nothing
    written. correct then writes the next revision with exactly what it supplied, so an omitted ends_at clears the end time.
    An ends_at already past is kept, with a warning.

    Returns ``{"limit": that revision's view, "limits": the active views, "warnings": [...]}``.
    """
    moment = datetime.now(UTC) if now is None else now
    action = params.get("action", "set")
    reason = _text(params.get("reason"), "reason")
    if action == "set":
        limit_id, revision, warnings = _create(state, params, actor, reason, moment)
    elif action == "correct":
        limit_id, revision, warnings = _correct(state, params, actor, reason, moment)
    elif action == "retire":
        limit_id, revision, warnings = _retire(state, params, actor, reason, moment)
    else:
        raise _invalid(f"action must be one of {', '.join(_ACTIONS)}.")
    return {"limit": _view(_revision_row(state, limit_id, revision), moment), "limits": [_view(row, moment) for row in _active_rows(state)], "warnings": warnings}


def list_limits(state: StateManagementInterface, *, history_limit: object = 20, now: datetime | None = None) -> dict[str, Any]:
    """The active limits, and the newest ``history_limit`` revisions of every state, newest first.

    A limit that has lapsed is still listed, with ``lapsed`` true: it stays active until someone retires it.
    """
    if isinstance(history_limit, bool) or not isinstance(history_limit, int) or not 1 <= history_limit <= _HISTORY_MAX:
        raise _invalid(f"history_limit must be 1..{_HISTORY_MAX}.")
    moment = datetime.now(UTC) if now is None else now
    history = require_records(state.query_ordered(NS, {"table": TABLE_SESSION_LIMIT, "filters": {}, "order_by": [["created_at", "desc"], ["id", "desc"]], "limit": history_limit}))
    return {"limits": [_view(row, moment) for row in _active_rows(state)], "history": [_view(row, moment) for row in history], "now": moment.isoformat()}
