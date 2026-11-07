"""Cooperative admission between whole sessions, never event/IO preemption.

One session and pre-yield IO may overrun. Unproved adapters keep whole polls.
No mutable operator activation field or durable queue is introduced here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ananta.llm.session_ledger.types import IngestSourceKind

RELOAD_SAFE = True
COMPLETED_VERSION_KEY = "_poll_completed_version"
OVERSIZED_SESSION_BYTES = 50_000_000
FILESYSTEM_POLL_KINDS = frozenset(
    {
        IngestSourceKind.CODEX_LOCAL,
        IngestSourceKind.CLAUDE_CODE_LOCAL,
        IngestSourceKind.CLAUDE_CODE_TASKS,
        IngestSourceKind.CODEX_AMBIENT,
    }
)
_SAFE_KINDS = frozenset(
    {
        IngestSourceKind.AGENT_MESSAGING,
        IngestSourceKind.CHATGPT_EXPORT,
        IngestSourceKind.CODEX_MEMORIES,
    }
)
_NON_YIELDING_REASONS = {
    IngestSourceKind.CODEX_HISTORY: "whole_file_eof_discovery_cursor",
    IngestSourceKind.CLAUDE_CODE_HISTORY: "whole_file_eof_discovery_cursor",
    IngestSourceKind.CODEX_GOALS: "scalar_updated_at_discovery_cursor",
    IngestSourceKind.CODEX_STATE: "post_yield_rowid_checkpoint",
}


class DiscoveryBudgetYield(Exception):  # noqa: N818 — named partial-progress signal, not an error
    """Normal partial metadata progress; not EOF and not a failed batch."""


def poll_yield_reason(kind: IngestSourceKind, *, completed_capability: bool) -> str | None:
    """None admits cuts; all other results advise unchanged whole-poll behavior.

    Capability is accepted only for the four known filesystem kinds. Default
    hooks, unknown kinds and future adapters do not silently activate slicing.
    """
    if kind in _SAFE_KINDS:
        return None
    if kind in FILESYSTEM_POLL_KINDS:
        return None if completed_capability else "awaiting_safe_adapter"
    return _NON_YIELDING_REASONS.get(kind, "unknown_source_contract")


@dataclass(slots=True)
class PollBudget:
    """Create/read an invocation deadline; retire by discarding it on return.

    The importer admits its first ref request even after slow setup. Adapters
    use can_admit_next between metadata candidates, including skipped prefixes;
    the importer checks it before requesting another ref after a whole session.
    """

    seconds: float = 60.0
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def can_admit_next(self) -> bool:
        return self.elapsed < self.seconds

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started
