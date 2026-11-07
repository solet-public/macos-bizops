"""The authoritative retired-role registry (iss_d9c955a2, transport part (a)).

Operator ruling ``arm-2305256f5b4cdf9d657a48cc466bc479`` names ONE retired-role
name set: this solet owns it, every role-addressed transport refuses a name in
it, and project-solet's content guard consumes the same set through an explicit
versioned read and fails closed when that read fails. There is no static
fallback list anywhere, and a vacant binding is not proof of retirement — the
only proof is a row in this table.

The registry is append-only. ``registry_version`` is stamped on each row at
insert as one more than the highest version already present, and the column is
UNIQUE, so the registry's version (the highest row version) strictly increases
with every retirement and two concurrent retirements cannot both take the same
number: the loser's insert is refused loudly and the caller retries. Un-retiring
is out of scope (iss_d9c955a2 brief); a future un-retire must append a
tombstone row at a fresh version rather than delete one, or the version would
step backwards.

Names are matched after an explicit, bounded normalization — Unicode NFKC, then
``casefold``, then surrounding whitespace stripped — so ``register-lead`` or a
full-width spelling cannot be claimed or addressed as a fresh role beside the
retired one. Display keeps the name exactly as it was retired.

Every read failure is an error, never an empty answer: a failed page, a
malformed row, and an EMPTY registry (the seed below guarantees at least one
row, so empty means the seed never ran) all raise
:class:`RetiredRoleRegistryError`.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_ROLE,
    COL_ROLE_CLASS,
    ROLE_CLASS_PRIMARY,
    ROLE_CLASS_PRINCIPAL,
    TABLE_ROLE,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import (
    StateOperationError,
    require_completed,
    require_records,
)
from ananta.services.state_service.bounded_read import (
    PagedReadError,
    ReadCeilingError,
    iter_table_rows,
)

from .role_binding_store import RoleBindingMalformedError, list_roles_for_agent_instance
from .schema import TABLE_RETIRED_ROLE

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

CODE_ROLE_RETIRED = "peer_role_retired"
CODE_REGISTRY_UNAVAILABLE = "retired_role_registry_unavailable"

COL_ROLE_KEY = "role_key"
COL_RETIRED_AT = "retired_at"
COL_RETIRED_BY = "retired_by"
COL_REASON = "reason"
COL_SUCCESSOR = "successor"
COL_REGISTRY_VERSION = "registry_version"

_COL_EXTERNAL_ID = "external_id"
_COL_IS_DELETED = "is_deleted"
_EXTERNAL_ID_PREFIX = "retired_role:"

# A retired role is a governance act, one per role name the fleet has ever
# stopped using. One row was the whole registry at birth; a thousand is far past
# any vocabulary an operator could steward, which is the condition worth
# failing loudly on.
_REGISTRY_CEILING = 1_000
_CEILING_REASON = (
    "one row per retired role name; retirement is an operator or Main-seat act, "
    "so the registry grows with governance decisions, not traffic."
)

# The seats allowed to retire a role. Classes, never names: primary is the
# reserved <solet>-Main seat, principal is the legislated operator-grade seat
# (legislate_role is the one path that stamps either).
_RETIRING_ROLE_CLASSES = frozenset({ROLE_CLASS_PRIMARY, ROLE_CLASS_PRINCIPAL})

# The migration seed (iss_d9c955a2 brief, step 1). Register-Lead was retired by
# iss_bfdb51c1 on 2026-09-28; it is the required fixture of ruling arm-2305256f.
SEED_ROLE = "Register-Lead"
SEED_RETIRED_AT = "2026-09-28T00:00:00+00:00"
SEED_RETIRED_BY = "iss_bfdb51c1-7a31-443f-b9c9-e15a6bcd7d8c"
SEED_REASON = "role retired; triage is filed as project-solet register tickets"


class RetiredRoleRegistryError(RuntimeError):
    """A registry read or write that could not answer. Never means "not retired"."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RetiredRole:
    """One retired role, exactly as the registry holds it."""

    role: str
    retired_at: str
    retired_by: str
    reason: str
    successor: str | None
    registry_version: int

    def to_public(self) -> dict[str, object]:
        return {
            "role": self.role,
            "retired_at": self.retired_at,
            "retired_by": self.retired_by,
            "reason": self.reason,
            "successor": self.successor,
            "registry_version": self.registry_version,
        }

    def refusal_message(self) -> str:
        """The sender-facing refusal: what was retired, why, and where to go."""
        successor = (
            f"send to its successor {self.successor!r} instead"
            if self.successor
            else "it has no successor role"
        )
        return (
            f"role {self.role!r} is retired (since {self.retired_at}, by "
            f"{self.retired_by}): {self.reason}; {successor}. Nothing was sent "
            "or persisted."
        )


class RoleRetiredError(Exception):
    """A send or claim named a retired role. Carries the registry row."""

    code = CODE_ROLE_RETIRED

    def __init__(self, retired: RetiredRole) -> None:
        self.retired = retired
        super().__init__(retired.refusal_message())

    def to_public(self) -> dict[str, object]:
        return {
            "code": CODE_ROLE_RETIRED,
            "message": str(self),
            "name": self.retired.role,
            "successor": self.retired.successor,
            "reason": self.retired.reason,
        }


@dataclass(frozen=True, slots=True)
class RetiredRoleRegistry:
    """The whole registry at one version."""

    registry_version: int
    roles: tuple[RetiredRole, ...]

    def to_public(self) -> dict[str, object]:
        return {
            "registry_version": self.registry_version,
            "roles": [role.to_public() for role in self.roles],
        }


@dataclass(frozen=True, slots=True)
class RetireRoleRequest:
    """A retirement as the verb received it. ``retired_by`` is server-derived."""

    role: str
    reason: str
    retired_by: str
    retired_at: str
    successor: str = ""


def normalize_role_name(name: str) -> str:
    """The matching key: NFKC, casefold, surrounding whitespace stripped."""
    return unicodedata.normalize("NFKC", name).casefold().strip()


def _external_id(name: str) -> str:
    return f"{_EXTERNAL_ID_PREFIX}{normalize_role_name(name)}"


def _row_text(row: dict[str, object], column: str) -> str:
    value = row.get(column)
    if not isinstance(value, str) or not value:
        raise RetiredRoleRegistryError(
            CODE_REGISTRY_UNAVAILABLE,
            f"retired_role row {row.get('id')!r} has no text {column!r} (got {value!r})",
        )
    return value


def _parse_row(row: dict[str, object]) -> RetiredRole:
    version = row.get(COL_REGISTRY_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RetiredRoleRegistryError(
            CODE_REGISTRY_UNAVAILABLE,
            f"retired_role row {row.get('id')!r} has no positive integer "
            f"registry_version (got {version!r})",
        )
    retired_at = row.get(COL_RETIRED_AT)
    if isinstance(retired_at, datetime):
        retired_at = retired_at.isoformat()
    if not isinstance(retired_at, str) or not retired_at:
        raise RetiredRoleRegistryError(
            CODE_REGISTRY_UNAVAILABLE,
            f"retired_role row {row.get('id')!r} has no retired_at (got {retired_at!r})",
        )
    successor = row.get(COL_SUCCESSOR)
    return RetiredRole(
        role=_row_text(row, COL_ROLE),
        retired_at=retired_at,
        retired_by=_row_text(row, COL_RETIRED_BY),
        reason=_row_text(row, COL_REASON),
        successor=successor if isinstance(successor, str) and successor else None,
        registry_version=version,
    )


def _read_rows(state: StateManagementInterface) -> list[RetiredRole]:
    try:
        rows = list(
            iter_table_rows(
                state,
                namespace=AGENT_ROLE_BINDING_NAMESPACE,
                table=TABLE_RETIRED_ROLE,
                filters={},
                ceiling=_REGISTRY_CEILING,
                reason=_CEILING_REASON,
            ),
        )
    except (PagedReadError, ReadCeilingError, StateOperationError) as exc:
        raise RetiredRoleRegistryError(
            CODE_REGISTRY_UNAVAILABLE, f"retired-role registry read failed: {exc}",
        ) from exc
    return [_parse_row(row) for row in rows]


def read_retired_role_registry(state: StateManagementInterface) -> RetiredRoleRegistry:
    """The whole registry and its version. Raises on every failure, and on empty."""
    roles = _read_rows(state)
    if not roles:
        raise RetiredRoleRegistryError(
            CODE_REGISTRY_UNAVAILABLE,
            "retired-role registry is empty: the Register-Lead seed has not run, so "
            "the registry cannot vouch for any name. Refusing rather than answering "
            "'nothing is retired'.",
        )
    ordered = tuple(sorted(roles, key=lambda role: role.registry_version))
    return RetiredRoleRegistry(
        registry_version=ordered[-1].registry_version, roles=ordered,
    )


def find_retired_role(state: StateManagementInterface, name: str) -> RetiredRole | None:
    """The registry row for ``name`` (normalized), or ``None``. Raises on a failed read."""
    try:
        records = require_records(
            state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_RETIRED_ROLE,
                    "filters": {_COL_EXTERNAL_ID: _external_id(name), _COL_IS_DELETED: 0},
                },
            ),
        )
    except StateOperationError as exc:
        raise RetiredRoleRegistryError(
            CODE_REGISTRY_UNAVAILABLE,
            f"retired-role registry lookup for {name!r} failed: {exc}",
        ) from exc
    return _parse_row(records[0]) if records else None


def refuse_if_retired(state: StateManagementInterface, name: str) -> None:
    """Raise :class:`RoleRetiredError` when ``name`` is retired.

    Called by every role-addressed transport BEFORE it resolves or persists
    anything, so a refused send leaves no row behind.
    """
    retired = find_retired_role(state, name)
    if retired is not None:
        raise RoleRetiredError(retired)


def _insert(state: StateManagementInterface, role: RetiredRole) -> None:
    record: dict[str, object] = {
        _COL_EXTERNAL_ID: _external_id(role.role),
        COL_ROLE: role.role,
        COL_ROLE_KEY: normalize_role_name(role.role),
        COL_RETIRED_AT: role.retired_at,
        COL_RETIRED_BY: role.retired_by,
        COL_REASON: role.reason,
        COL_REGISTRY_VERSION: role.registry_version,
    }
    if role.successor:
        record[COL_SUCCESSOR] = role.successor
    try:
        require_completed(
            state.write_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {"table": TABLE_RETIRED_ROLE, "record": record},
            ),
            "insert retired_role",
        )
    except StateOperationError as exc:
        raise RetiredRoleRegistryError(
            "retired_role_write_conflict",
            f"retiring {role.role!r} at registry_version {role.registry_version} "
            f"was refused ({exc}); another retirement may have taken that version. "
            "Re-read the registry and retry.",
        ) from exc


def _validate_request(state: StateManagementInterface, req: RetireRoleRequest) -> None:
    if not req.role.strip() or not req.reason.strip():
        raise RetiredRoleRegistryError(
            "missing_argument", "retire_role requires non-empty 'name' and 'reason'.",
        )
    if find_retired_role(state, req.role) is not None:
        raise RetiredRoleRegistryError(
            "role_already_retired", f"role {req.role!r} is already retired.",
        )
    successor = req.successor.strip()
    if not successor:
        return
    if normalize_role_name(successor) == normalize_role_name(req.role):
        raise RetiredRoleRegistryError(
            "invalid_successor", "a retired role cannot be its own successor.",
        )
    if find_retired_role(state, successor) is not None:
        raise RetiredRoleRegistryError(
            "invalid_successor", f"successor {successor!r} is itself retired.",
        )


def retire_role(state: StateManagementInterface, req: RetireRoleRequest) -> RetiredRole:
    """Append one retirement at the next registry version.

    Authorization and the live-holder check are the verb's job (they need the
    caller identity and the bridge); this is the registry write alone.
    """
    _validate_request(state, req)
    registry = read_retired_role_registry(state)
    retired = RetiredRole(
        role=req.role.strip(),
        retired_at=req.retired_at,
        retired_by=req.retired_by,
        reason=req.reason.strip(),
        successor=req.successor.strip() or None,
        registry_version=registry.registry_version + 1,
    )
    _insert(state, retired)
    return retired


def seed_retired_role_registry(state: StateManagementInterface) -> str:
    """Idempotent migration seed: ensure Register-Lead is retired. Returns the outcome."""
    if find_retired_role(state, SEED_ROLE) is not None:
        return "already_seeded"
    existing = _read_rows(state)
    version = max((role.registry_version for role in existing), default=0) + 1
    _insert(
        state,
        RetiredRole(
            role=SEED_ROLE,
            retired_at=SEED_RETIRED_AT,
            retired_by=SEED_RETIRED_BY,
            reason=SEED_REASON,
            successor=None,
            registry_version=version,
        ),
    )
    return "seeded"


def retiring_authority(state: StateManagementInterface, agent_instance_id: str) -> str | None:
    """The caller's primary- or principal-class role that may retire a role, else ``None``."""
    if not agent_instance_id:
        return None
    for role in list_roles_for_agent_instance(state, agent_instance_id):
        records = require_records(
            state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_ROLE,
                    "filters": {_COL_EXTERNAL_ID: role_binding_external_id(role)},
                },
            ),
        )
        if records and records[0].get(COL_ROLE_CLASS) in _RETIRING_ROLE_CLASSES:
            return role
    return None


@dataclass(frozen=True, slots=True)
class RetireRoleCall:
    """``retire_role`` as the verb received it, with the server-derived caller."""

    caller_instance_id: str
    name: str
    reason: str
    authority: str
    successor: str
    retired_at: str


def retire_role_for_caller(
    state: StateManagementInterface,
    call: RetireRoleCall,
    live_holder_label: Callable[[str], str | None],
) -> RetiredRole:
    """The ``retire_role`` verb body: authorize, refuse a live holder, then append.

    ``live_holder_label`` returns the live holder's label for a role, or ``None``
    when no live session holds it; the verb binds it to the bridge's liveness
    predicate (the same one the claim gate uses).
    """
    try:
        seat = retiring_authority(state, call.caller_instance_id)
    except StateOperationError as exc:
        raise RetiredRoleRegistryError(
            "retire_role_authority_unreadable",
            f"retire_role could not read the caller's role classes: {exc}",
        ) from exc
    if seat is None:
        raise RetiredRoleRegistryError(
            "retire_role_not_authorized",
            "retire_role is an operator or Main-seat act: the caller must hold a "
            "primary- or principal-class role (legislate_role assigns them).",
        )
    name = call.name.strip()
    authority = call.authority.strip()
    if not name or not authority:
        raise RetiredRoleRegistryError(
            "missing_argument",
            "retire_role requires non-empty 'name', 'reason' and 'authority'.",
        )
    try:
        holder = live_holder_label(name)
    except (
        PagedReadError, ReadCeilingError, RoleBindingMalformedError, StateOperationError,
    ) as exc:
        raise RetiredRoleRegistryError(
            "role_binding_unreadable",
            f"retire_role could not read the role bindings to check for a live "
            f"holder of {name!r}: {exc}. Refusing rather than retiring a role "
            "that may be held.",
        ) from exc
    if holder is not None:
        raise RetiredRoleRegistryError(
            "role_held_live",
            f"role {name!r} is held by live session {holder}; it must release the "
            "role (peer_release_role) before it can be retired.",
        )
    return retire_role(
        state,
        RetireRoleRequest(
            role=name,
            reason=call.reason,
            retired_by=f"{seat}; {authority}",
            retired_at=call.retired_at,
            successor=call.successor,
        ),
    )


__all__ = [
    "CODE_REGISTRY_UNAVAILABLE",
    "CODE_ROLE_RETIRED",
    "SEED_ROLE",
    "RetireRoleCall",
    "RetireRoleRequest",
    "RetiredRole",
    "RetiredRoleRegistry",
    "RetiredRoleRegistryError",
    "RoleRetiredError",
    "find_retired_role",
    "normalize_role_name",
    "read_retired_role_registry",
    "refuse_if_retired",
    "retire_role",
    "retire_role_for_caller",
    "retiring_authority",
    "seed_retired_role_registry",
]
