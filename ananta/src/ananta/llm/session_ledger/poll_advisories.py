"""Ephemeral Claude line diagnostics and successful-read completion metadata.

Context variables contain invocation-local state, never a shared accumulator.
Physical lines are deduplicated only within one poll; a later replay counts anew.
Completion offsets are published only by a normally exhausted filesystem read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from typing import ParamSpec

from ananta.llm.session_ledger.types import ImporterReport, UnknownLineAdvisory

logger = logging.getLogger(__name__)
_P = ParamSpec("_P")


@dataclass(frozen=True, slots=True)
class ClaudeReadLine:
    """Physical location; no transcript content is retained."""

    root_uri: str
    external_session_id: str
    end_offset: int


@dataclass(slots=True)
class PollAdvisories:
    """Created per poll, updated locally, frozen into its report, then retired."""

    occurrences: set[tuple[str, str, str, int]] = field(default_factory=set)
    counts: dict[tuple[str, str, str], int] = field(default_factory=dict)
    completed_reads: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def snapshot(self) -> tuple[UnknownLineAdvisory, ...]:
        return tuple(UnknownLineAdvisory(*key, count) for key, count in sorted(self.counts.items()))


_collector: ContextVar[PollAdvisories | None] = ContextVar("claude_poll_advisories", default=None)
_source: ContextVar[str | None] = ContextVar("claude_poll_source", default=None)
_line: ContextVar[ClaudeReadLine | None] = ContextVar("claude_read_line", default=None)


@contextmanager
def collect_poll_advisories() -> Generator[PollAdvisories]:
    """Isolate nested/concurrent polls and reset on success or any exception."""
    collector = PollAdvisories()
    token = _collector.set(collector)
    try:
        yield collector
    finally:
        _collector.reset(token)


def report_poll_advisories(poll: Callable[_P, ImporterReport]) -> Callable[_P, ImporterReport]:
    """Attach the immutable snapshot without changing the four public counts."""

    @wraps(poll)
    def run(*args: _P.args, **kwargs: _P.kwargs) -> ImporterReport:
        with collect_poll_advisories() as collector:
            return replace(poll(*args, **kwargs), unknown_line_advisories=collector.snapshot())

    return run


@contextmanager
def poll_source_context(source_id: str) -> Generator[None]:
    token = _source.set(source_id)
    try:
        yield
    finally:
        _source.reset(token)


@contextmanager
def claude_line_context(root_uri: str, external_session_id: str, end_offset: int) -> Generator[None]:
    token = _line.set(ClaudeReadLine(root_uri, external_session_id, end_offset))
    try:
        yield
    finally:
        _line.reset(token)


def record_unknown_line(line_type: str, external_session_id: str | None = None) -> None:
    """Count physical input once in a poll, otherwise warn explicitly and safely."""
    collector, source_id, line = _collector.get(), _source.get(), _line.get()
    if collector is None or source_id is None or line is None:
        logger.warning("claude_code: skipping unknown line type=%r session=%r", line_type, external_session_id)
        return
    physical_key = (source_id, line.root_uri, line.external_session_id, line.end_offset)
    if physical_key in collector.occurrences:
        return
    collector.occurrences.add(physical_key)
    group = (source_id, line.external_session_id, line_type)
    collector.counts[group] = collector.counts.get(group, 0) + 1


def begin_claude_read(root_uri: str, external_session_id: str) -> None:
    """Discard any earlier completion before attempting a new read, even missing files."""
    collector, source_id = _collector.get(), _source.get()
    if collector is not None and source_id is not None:
        collector.completed_reads.pop((source_id, root_uri, external_session_id), None)


def complete_claude_read(root_uri: str, external_session_id: str, end_offset: int) -> None:
    """Publish only consumed complete bytes after normal EOF/partial-tail stop."""
    collector, source_id = _collector.get(), _source.get()
    if collector is not None and source_id is not None:
        collector.completed_reads[(source_id, root_uri, external_session_id)] = end_offset


def completed_claude_read(root_uri: str, external_session_id: str) -> int | None:
    collector, source_id = _collector.get(), _source.get()
    if collector is None or source_id is None:
        return None
    return collector.completed_reads.get((source_id, root_uri, external_session_id))
