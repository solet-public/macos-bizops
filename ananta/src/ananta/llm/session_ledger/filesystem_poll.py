"""Filesystem metadata policy for cooperative, whole-session polling.

Each sweep sorts stat observations, not content, newest first. The after pair
is a within-sweep boundary and is cleared at actual EOF. Completed versions
live in existing event cursors; deferred work is recomputed under A's lease.
Enumeration/stat IO and an admitted session can overrun the admission budget.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from abc import abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ananta.interfaces.llm_session_source_interface import PullingSourceMixin
from ananta.llm.session_ledger.poll_budget import COMPLETED_VERSION_KEY, OVERSIZED_SESSION_BYTES, DiscoveryBudgetYield
from ananta.llm.session_ledger.types import ExternalSessionRef, OversizedSelection, PollDiscoveryProgress, PollSessionRef

RELOAD_SAFE = True
logger = logging.getLogger(__name__)
_SWEEP_VERSION = 1


@dataclass(frozen=True, slots=True)
class FilesystemCandidate:
    """One stat-only observation; discard after this invocation."""

    path: Path
    external_id: str
    relative_path: str
    version: dict[str, object]
    aggregate_bytes: int
    mtime_ns: int
    task_glob: str | None = None
    json_lines: bool = False

    @property
    def pair(self) -> tuple[int, str]:
        return self.mtime_ns, self.relative_path


@dataclass(slots=True)
class ReadObservation:
    """Per-ref proof that a snapshot parser actually reached a successful EOF."""

    complete: bool = False


@dataclass(frozen=True, slots=True)
class FilesystemPollRef(PollSessionRef):
    """Private observation token; none of these fields are external event IDs."""

    candidate: FilesystemCandidate | None = None
    observation: ReadObservation | None = None


def _file_tuple(path: Path, relative_path: str) -> tuple[str, int, int, int, int]:
    stat = path.stat()
    return relative_path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def observe_candidate(
    path: Path,
    *,
    external_id: str,
    relative_path: str,
    task_glob: str | None = None,
    json_lines: bool = False,
) -> FilesystemCandidate | None:
    """Observe a file or the COMPLETE configured task-directory population.

    Disappearance is an availability advisory, never completion of lost input.
    No transcript content is read to construct the version or byte census.
    """
    try:
        if task_glob is None:
            row = _file_tuple(path, relative_path)
            version: dict[str, object] = {"file": list(row)}
            size, mtime = row[3], row[4]
        else:
            rows = [_file_tuple(p, p.relative_to(path).as_posix()) for p in sorted(path.glob(task_glob))]
            if not rows:
                return None
            version = {"path": relative_path, "snapshot": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()}
            size = sum(row[3] for row in rows)
            mtime = max(row[4] for row in rows)
        return FilesystemCandidate(path, external_id, relative_path, version, size, mtime, task_glob, json_lines)
    except OSError as exc:
        logger.warning("ledger filesystem unavailable id=%s path=%s: %s", external_id, relative_path, exc)
        return None


def selected_path(ref: ExternalSessionRef) -> Path | None:
    """Avoid re-resolving a selected path through a potentially changed walk."""
    if isinstance(ref, FilesystemPollRef) and ref.candidate is not None:
        return ref.candidate.path
    return None


def acknowledge_read(ref: ExternalSessionRef) -> None:
    """Called only after the adapter's actual parser has completed successfully."""
    if isinstance(ref, FilesystemPollRef) and ref.observation is not None:
        ref.observation.complete = True


def _after(cursor: dict[str, object] | None) -> tuple[int, str] | None:
    if cursor is None or cursor.get("version") != _SWEEP_VERSION:
        return None  # Legacy scalar/highest-stem cursors reopen a full sweep.
    raw = cursor.get("after")
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 2 or not isinstance(raw[0], int) or not isinstance(raw[1], str):
        raise ValueError("filesystem sweep after must be [mtime_ns, relative_path] or null")
    return raw[0], raw[1]


def _is_completed(candidate: FilesystemCandidate, cursor: dict[str, object] | None) -> bool:
    return cursor is not None and cursor.get(COMPLETED_VERSION_KEY) == candidate.version


class FilesystemPollingMixin(PullingSourceMixin):
    """Shared metadata policy; no executor, provider, lease, or cursor storage."""

    supports_whole_session_poll: ClassVar[bool] = False
    snapshot_poll: ClassVar[bool] = False

    @abstractmethod
    def _poll_candidates(self, root_uri: str) -> Iterator[FilesystemCandidate]:
        """Preserve the adapter's existing configured source population."""

    @abstractmethod
    def _poll_ref(self, root_uri: str, candidate: FilesystemCandidate) -> ExternalSessionRef:
        """Load promoted header metadata only for a selected version."""

    def _selected_ref(self, root_uri: str, candidate: FilesystemCandidate) -> FilesystemPollRef:
        ref = self._poll_ref(root_uri, candidate)
        return FilesystemPollRef(
            external_session_id=ref.external_session_id,
            vendor_session_label=ref.vendor_session_label,
            project_path=ref.project_path,
            first_seen_at=ref.first_seen_at,
            originator_session_label=ref.originator_session_label,
            originator_agent_instance_id=ref.originator_agent_instance_id,
            recipient_session_label=ref.recipient_session_label,
            recipient_agent_instance_id=ref.recipient_agent_instance_id,
            summary_text_seed=ref.summary_text_seed,
            relative_path=candidate.relative_path,
            selected_version=candidate.version,
            aggregate_bytes=candidate.aggregate_bytes,
            discovery_pair=candidate.pair,
            candidate=candidate,
            observation=ReadObservation(),
        )

    def discover_sessions_for_poll(
        self,
        root_uri: str,
        cursor_payload: dict[str, object] | None,
        *,
        read_event_cursor: Callable[[str], dict[str, object] | None],
        can_admit_next: Callable[[], bool],
        progress: PollDiscoveryProgress,
    ) -> Iterator[ExternalSessionRef]:
        after = _after(cursor_payload)
        # Stat-only sorting is intentionally not claimed constant IO or bounded.
        candidates = sorted(self._poll_candidates(root_uri), key=lambda item: item.pair, reverse=True)
        progress.last_examined = after
        first = True
        for candidate in candidates:
            if after is not None and candidate.pair >= after:
                continue
            # A admits its first request even after slow setup. Match that
            # grace for one metadata candidate, then check between candidates.
            if not first and not can_admit_next():
                raise DiscoveryBudgetYield
            first = False
            stored = read_event_cursor(candidate.external_id)
            if _is_completed(candidate, stored):
                progress.last_examined = candidate.pair
            elif candidate.aggregate_bytes > OVERSIZED_SESSION_BYTES:
                logger.info("ledger filesystem deferred id=%s path=%s bytes=%d", candidate.external_id, candidate.relative_path, candidate.aggregate_bytes)
                progress.deferred_count += 1
                progress.deferred_bytes += candidate.aggregate_bytes
                progress.last_examined = candidate.pair
            else:
                # No post-yield progress side effect: A acknowledges this ref.
                yield self._selected_ref(root_uri, candidate)

    def completed_discovery_cursor(
        self,
        root_uri: str,
        previous_cursor: dict[str, object] | None,
        *,
        exhausted: bool,
        pending: bool,
        progress: PollDiscoveryProgress,
    ) -> dict[str, object]:
        del root_uri
        after = progress.last_examined if progress.last_examined is not None else _after(previous_cursor)
        return {"version": _SWEEP_VERSION, "state": "caught_up" if exhausted and not pending else "backfilling", "after": None if exhausted else list(after) if after is not None else None}

    def prepare_poll_event_cursor(
        self,
        root_uri: str,
        ref: ExternalSessionRef,
        stored_cursor: dict[str, object] | None,
    ) -> dict[str, object] | None:
        del root_uri
        if self.snapshot_poll or not isinstance(ref, PollSessionRef) or stored_cursor is None:
            return None
        if _append_continuation(stored_cursor.get(COMPLETED_VERSION_KEY), ref.selected_version):
            return dict(stored_cursor)
        logger.info("ledger filesystem replay id=%s: legacy, replacement or truncation", ref.external_session_id)
        return None

    def completed_session_fingerprint(self, root_uri: str, ref: ExternalSessionRef) -> dict[str, object] | None:
        del root_uri
        if not isinstance(ref, FilesystemPollRef) or ref.candidate is None or ref.observation is None or not ref.observation.complete:
            return None
        candidate = ref.candidate
        current = observe_candidate(candidate.path, external_id=candidate.external_id, relative_path=candidate.relative_path, task_glob=candidate.task_glob, json_lines=candidate.json_lines)
        if current is None or current.version != candidate.version:
            return None
        if candidate.json_lines and not _complete_physical_line(candidate.path):
            return None
        return candidate.version

    def select_oversized_session(
        self,
        root_uri: str,
        *,
        read_event_cursor: Callable[[str], dict[str, object] | None],
        threshold: int,
    ) -> OversizedSelection:
        selected: FilesystemCandidate | None = None
        for candidate in sorted(self._poll_candidates(root_uri), key=lambda item: item.pair, reverse=True):
            if _is_completed(candidate, read_event_cursor(candidate.external_id)):
                continue
            if candidate.aggregate_bytes <= threshold:
                return OversizedSelection(regular_pending=True)
            if selected is None:
                selected = candidate
        return OversizedSelection(candidate=self._selected_ref(root_uri, selected) if selected is not None else None)


def _file_version(version: object) -> list[object] | None:
    if not isinstance(version, dict):
        return None
    value = version.get("file")
    if isinstance(value, list) and len(value) == 5:
        return value
    return None


def _append_continuation(old: object, new: object) -> bool:
    before, current = _file_version(old), _file_version(new)
    if before is None or current is None or before[:3] != current[:3]:
        return False
    old_size, new_size = before[3], current[3]
    if not isinstance(old_size, int) or not isinstance(new_size, int):
        return False
    return new_size > old_size or current == before


def _complete_physical_line(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            if not os.fstat(handle.fileno()).st_size:
                return True
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"
    except OSError as exc:
        logger.warning("ledger filesystem EOF unavailable path=%s: %s", path, exc)
        return False
