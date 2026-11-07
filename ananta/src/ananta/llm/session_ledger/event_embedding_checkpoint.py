"""Disposable policy-sweep progress under the singleton embedding executor.

KV writes are checked before any durable frontier advances. The expected prior
checkpoint guards stale work; serialization is supplied by the installed
single-slot, ten-minute embedding drain, rather than a multiwriter CAS claim.
Transition logs provide append-only operational history without event content.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from ananta.llm.session_ledger.repository import SessionLedgerRepository

logger = logging.getLogger(__name__)

#: Generation of the checkpoint ``decode_checkpoint`` returns for unreadable progress: its target is lost.
CORRUPT_GENERATION = "corrupt"

#: Legacy ``previous`` of a generation minted from corrupt progress. It made every event stale, so the
#: sweep re-embedded the whole corpus (prb_f6964132); a running generation carrying it is corrected to
#: the recorded policy.
UNKNOWN_PREVIOUS = "unknown:corrupt"


@dataclass(frozen=True, slots=True)
class SweepCheckpoint:
    """One sweep generation; exhausted failures reopen on reconciliation only."""

    generation: str
    target: str
    previous: str | None
    after: tuple[str, str] | None = None
    status: Literal["running", "exhausted-with-failures"] = "running"
    failed_count: int = 0

    def payload(self) -> dict[str, object]:
        """Current readback; no ledger event content is persisted."""
        return asdict(self)


def decode_checkpoint(raw: object) -> SweepCheckpoint | None:
    """Missing/corrupt derived state is advisory: restart a safe full sweep."""
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise ValueError("expected object")
        return SweepCheckpoint(
            _policy_text(raw["generation"]),
            _policy_text(raw["target"]),
            _previous_policy(raw["previous"]),
            _checkpoint_cursor(raw["after"]),
            _checkpoint_status(raw["status"]),
            _failure_count(raw["failed_count"]),
        )
    except (KeyError, ValueError):
        logger.warning("event embedding checkpoint corrupt; restarting a sweep of missing work", exc_info=True)
        return SweepCheckpoint(CORRUPT_GENERATION, "", None)


def _policy_text(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError("invalid policy/generation")
    return raw


def _previous_policy(raw: object) -> str | None:
    return None if raw is None else _policy_text(raw)


def _checkpoint_cursor(raw: object) -> tuple[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError("invalid cursor")
    return _policy_text(raw[0]), _policy_text(raw[1])


def _checkpoint_status(raw: object) -> Literal["running", "exhausted-with-failures"]:
    if raw not in ("running", "exhausted-with-failures"):
        raise ValueError("invalid status")
    return "running" if raw == "running" else "exhausted-with-failures"


def _failure_count(raw: object) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ValueError("invalid failure count")
    return raw


def prepare_sweep(
    repository: SessionLedgerRepository,
    target: str,
    *,
    scheduled: bool,
) -> tuple[SweepCheckpoint | None, SweepCheckpoint | None]:
    """Return (active, pending); create, resume, supersede or reopen a sweep."""
    pending = repository.get_event_embed_checkpoint()
    previous = repository.get_event_embed_chunk_policy()
    if pending is None:
        return _new_sweep(repository, target, previous, pending, scheduled=scheduled)
    if pending.generation == CORRUPT_GENERATION:
        # Lost progress is not lost vectors: the recorded policy still names how they were cut, so
        # the fresh sweep embeds only missing or policy-changed chunks (prb_f6964132).
        logger.warning("event embedding checkpoint corrupt; new sweep compares against recorded policy=%s", previous)
        return _new_sweep(repository, target, previous, pending, scheduled=True)
    if pending.target != target:
        # A partially applied foreign policy must be replaced even on A->B->A.
        return _new_sweep(repository, target, pending.target, pending, scheduled=True)
    if pending.previous == UNKNOWN_PREVIOUS:
        pending = _correct_unknown_previous(repository, pending, previous)
    if pending.status == "running":
        return pending, pending
    if scheduled:
        current = replace(pending, after=None, status="running", failed_count=0)
        repository.set_event_embed_checkpoint(current, expected=pending)
        return current, current
    return None, pending


def _correct_unknown_previous(
    repository: SessionLedgerRepository,
    pending: SweepCheckpoint,
    recorded: str | None,
) -> SweepCheckpoint:
    """Correct a legacy unknown ``previous`` to the recorded policy in place, keeping the sweep's progress."""
    current = replace(pending, previous=recorded)
    repository.set_event_embed_checkpoint(current, expected=pending)
    logger.warning(
        "event embedding checkpoint generation=%s previous=%s corrected to recorded policy=%s; progress kept at %s",
        pending.generation,
        UNKNOWN_PREVIOUS,
        recorded,
        pending.after,
    )
    return current


def _new_sweep(
    repository: SessionLedgerRepository,
    target: str,
    previous: str | None,
    expected: SweepCheckpoint | None,
    *,
    scheduled: bool,
) -> tuple[SweepCheckpoint | None, SweepCheckpoint | None]:
    """Create one policy generation, including safe advisory corruption recovery."""
    if previous == target and not scheduled:
        return None, None
    if expected is None and previous != target:
        logger.warning("event embedding checkpoint absent for target=%s; starting safe sweep", target)
    current = SweepCheckpoint(uuid4().hex, target, previous)
    repository.set_event_embed_checkpoint(current, expected=expected)
    return current, current


def checkpoint_page(
    repository: SessionLedgerRepository,
    checkpoint: SweepCheckpoint,
    after: tuple[str, str],
    failures: int,
) -> SweepCheckpoint:
    """Correct completed-page progress; persistence failure leaves it replayable."""
    current = replace(checkpoint, after=after, failed_count=checkpoint.failed_count + failures)
    repository.set_event_embed_checkpoint(current, expected=checkpoint)
    return current


def finish_sweep(repository: SessionLedgerRepository, checkpoint: SweepCheckpoint) -> SweepCheckpoint | None:
    """Exhaust failures, or record policy before retiring clean progress."""
    if checkpoint.failed_count:
        pending = replace(checkpoint, status="exhausted-with-failures")
        repository.set_event_embed_checkpoint(pending, expected=checkpoint)
        return pending
    if repository.get_event_embed_checkpoint() != checkpoint:
        raise RuntimeError("event embedding checkpoint changed before completion; stale generation refused")
    repository.set_event_embed_chunk_policy(checkpoint.target)
    repository.set_event_embed_checkpoint(None, expected=checkpoint)
    return None
