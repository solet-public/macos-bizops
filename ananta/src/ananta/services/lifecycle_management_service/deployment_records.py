"""Store operations for the solet's own deployment records.

The deploy command asks one question, "what is deployed and since when", and
records one fact, "this release went live at this commit". These operations
answer and record that over the state interface, with no outside service.

Lifecycle (every row of ``deployment_record`` and its events):

- create: :func:`record` — idempotent on (solet, sha, release); a repeat returns
  the same row and writes nothing.
- read: :func:`current`, :func:`history`, :func:`get`, :func:`events`.
- update or correct: none by design. A row states a measured fact; a wrong one is
  withdrawn and the right one recorded anew.
- retire and reopen: :func:`set_withdrawn` — withdraw or reinstate, a
  compare-and-set on ``version`` (``update_state`` rows-affected). A stale
  ``expected_version`` raises :class:`StaleVersionError` and writes nothing.
- history: every transition appends one event whose ordinal equals the new
  version; events are never updated.

The actor is recorded, never an authority: no operation here refuses by who
is asking.

The store dependency is structural (:class:`DeploymentRecordStore`), so the real
``StateManagementInterface`` and the offline smoke fake both satisfy it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import uuid4

from ananta.core.domain.timestamps import to_naive_utc
from ananta.error_handling import FrameworkError
from ananta.llm.agent_messaging.state_results import (
    is_completed,
    require_completed,
    require_records,
    require_updated,
)
from ananta.services.lifecycle_management_service.deployment_record_schema import (
    COL_ACTOR_EFFORT,
    COL_ACTOR_INSTANCE,
    COL_ACTOR_MODEL,
    COL_DEPLOYED_AT,
    COL_DEPLOYMENT_ID,
    COL_GIT_SHA,
    COL_KIND,
    COL_MANIFEST_SHA256,
    COL_ORDINAL,
    COL_PRIOR_VERSION,
    COL_REASON,
    COL_RECORDED_AT,
    COL_RECORDED_BY_EFFORT,
    COL_RECORDED_BY_INSTANCE,
    COL_RECORDED_BY_MODEL,
    COL_RELEASE_ID,
    COL_SOLET_NAME,
    COL_STATE,
    COL_TRIGGER,
    COL_VERSION,
    DEPLOYMENT_RECORD_NAMESPACE,
    GIT_SHA_LENGTH,
    ID_PREFIX_DEPLOYMENT_RECORD,
    KIND_RECORDED,
    KIND_REINSTATED,
    KIND_WITHDRAWN,
    STATE_RECORDED,
    STATE_WITHDRAWN,
    TABLE_DEPLOYMENT_RECORD,
    TABLE_DEPLOYMENT_RECORD_EVENT,
    TRIGGERS,
)

Row = dict[str, object]

# The state interface caps an ordered page at 100 rows.
MAX_HISTORY_LIMIT = 100

_SHA_RE = re.compile(rf"^[0-9a-f]{{{GIT_SHA_LENGTH}}}$")


class DeploymentRecordError(FrameworkError):
    """Base of the typed deployment-record errors."""


class InvalidShaError(DeploymentRecordError):
    """The git sha is not 40 lowercase hex characters."""


class InvalidTriggerError(DeploymentRecordError):
    """The trigger is not one of the recorded trigger kinds."""


class StaleVersionError(DeploymentRecordError):
    """``expected_version`` no longer matches the row; nothing was written."""


class UnknownDeploymentError(DeploymentRecordError):
    """No deployment record has that id."""


@dataclass(frozen=True, slots=True)
class Actor:
    """Who caused a write. Recorded for the history, never used to refuse."""

    instance: str
    model: str
    effort: str


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of history, newest first; ``next_after`` continues it, or is None."""

    rows: list[Row]
    next_after: tuple[str, str] | None


@runtime_checkable
class DeploymentRecordStore(Protocol):
    """The state-interface surface the deployment record store needs."""

    # Positional-only: the real interface and the offline fake name these
    # parameters differently, and the store never passes them by keyword.
    def write_state(self, namespace: str, data: dict[str, object], /) -> object: ...

    def query_state(self, namespace: str, query: dict[str, object], /) -> object: ...

    def query_ordered(self, namespace: str, query: dict[str, object], /) -> object: ...

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
        /,
    ) -> object: ...


def _utc_text(value: object) -> str:
    """One fixed spelling of a UTC instant, so text order is time order."""
    return to_naive_utc(value).isoformat(timespec="microseconds")


def _now_text() -> str:
    return _utc_text(datetime.now(UTC))


def get(store: DeploymentRecordStore, *, deployment_id: str) -> Row | None:
    """Read one deployment record by id, or ``None``."""
    rows = require_records(
        store.query_state(
            DEPLOYMENT_RECORD_NAMESPACE,
            {
                "table": TABLE_DEPLOYMENT_RECORD,
                "filters": {COL_DEPLOYMENT_ID: deployment_id},
            },
        ),
    )
    return rows[0] if rows else None


def events(store: DeploymentRecordStore, *, deployment_id: str) -> list[Row]:
    """Every event of one deployment record, oldest first."""
    return require_records(
        store.query_ordered(
            DEPLOYMENT_RECORD_NAMESPACE,
            {
                "table": TABLE_DEPLOYMENT_RECORD_EVENT,
                "filters": {COL_DEPLOYMENT_ID: deployment_id},
                "order_by": [[COL_ORDINAL, "asc"], [COL_DEPLOYMENT_ID, "asc"]],
                "limit": MAX_HISTORY_LIMIT,
            },
        ),
    )


def _find_identity(
    store: DeploymentRecordStore,
    *,
    solet_name: str,
    git_sha: str,
    release_id: str,
) -> Row | None:
    rows = require_records(
        store.query_state(
            DEPLOYMENT_RECORD_NAMESPACE,
            {
                "table": TABLE_DEPLOYMENT_RECORD,
                "filters": {
                    COL_SOLET_NAME: solet_name,
                    COL_GIT_SHA: git_sha,
                    COL_RELEASE_ID: release_id,
                },
            },
        ),
    )
    return rows[0] if rows else None


def _append_event(
    store: DeploymentRecordStore,
    *,
    deployment_id: str,
    ordinal: int,
    kind: str,
    prior_version: int,
    reason: str,
    actor: Actor,
) -> None:
    require_completed(
        store.write_state(
            DEPLOYMENT_RECORD_NAMESPACE,
            {
                "table": TABLE_DEPLOYMENT_RECORD_EVENT,
                "record": {
                    COL_DEPLOYMENT_ID: deployment_id,
                    COL_ORDINAL: ordinal,
                    COL_KIND: kind,
                    COL_PRIOR_VERSION: prior_version,
                    COL_ACTOR_INSTANCE: actor.instance,
                    COL_ACTOR_MODEL: actor.model,
                    COL_ACTOR_EFFORT: actor.effort,
                    COL_REASON: reason,
                    COL_RECORDED_AT: _now_text(),
                },
            },
        ),
        "append deployment record event",
    )


def record(
    store: DeploymentRecordStore,
    *,
    solet_name: str,
    release_id: str,
    git_sha: str,
    deployed_at: datetime,
    trigger: str,
    manifest_sha256: str,
    actor: Actor,
) -> tuple[Row, bool]:
    """Record one deployment; return ``(row, created)``.

    Idempotent on (solet_name, git_sha, release_id): a repeat returns the
    existing row with ``created`` False and writes nothing, so a deploy command
    that runs twice records once. Naive and aware datetimes both store as UTC.
    """
    if not _SHA_RE.match(git_sha):
        raise InvalidShaError(
            f"git sha must be {GIT_SHA_LENGTH} lowercase hex characters, got {git_sha!r}",
        )
    if trigger not in TRIGGERS:
        raise InvalidTriggerError(f"trigger must be one of {TRIGGERS}, got {trigger!r}")
    existing = _find_identity(
        store,
        solet_name=solet_name,
        git_sha=git_sha,
        release_id=release_id,
    )
    if existing is not None:
        return existing, False
    deployment_id = f"{ID_PREFIX_DEPLOYMENT_RECORD}-{uuid4().hex}"
    written = store.write_state(
        DEPLOYMENT_RECORD_NAMESPACE,
        {
            "table": TABLE_DEPLOYMENT_RECORD,
            "record": {
                COL_DEPLOYMENT_ID: deployment_id,
                COL_SOLET_NAME: solet_name,
                COL_RELEASE_ID: release_id,
                COL_GIT_SHA: git_sha,
                COL_DEPLOYED_AT: _utc_text(deployed_at),
                COL_TRIGGER: trigger,
                COL_MANIFEST_SHA256: manifest_sha256,
                COL_RECORDED_BY_INSTANCE: actor.instance,
                COL_RECORDED_BY_MODEL: actor.model,
                COL_RECORDED_BY_EFFORT: actor.effort,
                COL_STATE: STATE_RECORDED,
                COL_VERSION: 1,
            },
        },
    )
    if not is_completed(written):
        # A concurrent recorder won the unique identity; that row is the answer.
        # Anything else is a real write failure and stays loud.
        raced = _find_identity(
            store,
            solet_name=solet_name,
            git_sha=git_sha,
            release_id=release_id,
        )
        if raced is None:
            require_completed(written, "record deployment")
        else:
            return raced, False
    _append_event(
        store,
        deployment_id=deployment_id,
        ordinal=1,
        kind=KIND_RECORDED,
        prior_version=0,
        reason="",
        actor=actor,
    )
    created = get(store, deployment_id=deployment_id)
    if created is None:
        raise DeploymentRecordError(
            f"deployment record {deployment_id} was written but cannot be read back",
        )
    return created, True


def current(store: DeploymentRecordStore, *, solet_name: str) -> Row | None:
    """The newest ``recorded`` deployment of a solet by ``deployed_at``, or ``None``."""
    rows = require_records(
        store.query_ordered(
            DEPLOYMENT_RECORD_NAMESPACE,
            {
                "table": TABLE_DEPLOYMENT_RECORD,
                "filters": {COL_SOLET_NAME: solet_name, COL_STATE: STATE_RECORDED},
                "order_by": [[COL_DEPLOYED_AT, "desc"], [COL_DEPLOYMENT_ID, "desc"]],
                "limit": 1,
            },
        ),
    )
    return rows[0] if rows else None


def history(
    store: DeploymentRecordStore,
    *,
    solet_name: str,
    limit: int,
    after: tuple[str, str] | None = None,
) -> HistoryPage:
    """Deployments of a solet newest first, withdrawn rows included.

    ``after`` is the ``next_after`` of the previous page. A ``limit`` outside
    1..:data:`MAX_HISTORY_LIMIT` is a caller error and raises.
    """
    if not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise DeploymentRecordError(
            f"history limit must be between 1 and {MAX_HISTORY_LIMIT}, got {limit}",
        )
    query: dict[str, object] = {
        "table": TABLE_DEPLOYMENT_RECORD,
        "filters": {COL_SOLET_NAME: solet_name},
        "order_by": [[COL_DEPLOYED_AT, "desc"], [COL_DEPLOYMENT_ID, "desc"]],
        "limit": limit + 1,
        "unbounded": True,
    }
    if after is not None:
        query["after"] = list(after)
    fetched = require_records(store.query_ordered(DEPLOYMENT_RECORD_NAMESPACE, query))
    rows = fetched[:limit]
    if len(fetched) <= limit:
        return HistoryPage(rows=rows, next_after=None)
    last = rows[-1]
    return HistoryPage(
        rows=rows,
        next_after=(_utc_text(last[COL_DEPLOYED_AT]), str(last[COL_DEPLOYMENT_ID])),
    )


def set_withdrawn(
    store: DeploymentRecordStore,
    *,
    deployment_id: str,
    withdrawn: bool,
    expected_version: int,
    reason: str,
    actor: Actor,
) -> Row:
    """Withdraw (``withdrawn`` True) or reinstate a record; return the new row.

    A compare-and-set on ``version``: if the row moved since the caller read it,
    :class:`StaleVersionError` is raised and nothing is written. The row version
    and the new event's ordinal both become ``expected_version + 1``.
    """
    row = get(store, deployment_id=deployment_id)
    if row is None:
        raise UnknownDeploymentError(f"no deployment record {deployment_id!r}")
    target = STATE_WITHDRAWN if withdrawn else STATE_RECORDED
    updated = require_updated(
        store.update_state(
            DEPLOYMENT_RECORD_NAMESPACE,
            {
                "table": TABLE_DEPLOYMENT_RECORD,
                "filters": {
                    COL_DEPLOYMENT_ID: deployment_id,
                    COL_VERSION: expected_version,
                },
            },
            {COL_STATE: target, COL_VERSION: expected_version + 1},
        ),
    )
    if updated != 1:
        raise StaleVersionError(
            f"deployment record {deployment_id} is not at version {expected_version}; re-read it and retry",
        )
    _append_event(
        store,
        deployment_id=deployment_id,
        ordinal=expected_version + 1,
        kind=KIND_WITHDRAWN if withdrawn else KIND_REINSTATED,
        prior_version=expected_version,
        reason=reason,
        actor=actor,
    )
    moved = get(store, deployment_id=deployment_id)
    if moved is None:
        raise DeploymentRecordError(
            f"deployment record {deployment_id} vanished after a state change",
        )
    return moved
