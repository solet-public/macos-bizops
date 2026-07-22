"""Data models for guarded agent execution.

Provides normalized data structures used by both Claude Code and Codex
agent plugins.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AgentBackend(StrEnum):
    """Supported agent backend types."""

    CODEX = "codex"
    CLAUDE_CODE = "claude_code"


@dataclass(frozen=True)
class AgentCapabilities:
    """Declares what an agent implementation supports.

    Used by orchestrators to understand agent capabilities and make
    routing decisions.
    """

    backend: AgentBackend
    supports_resumption: bool
    supports_interruption: bool
    supports_tool_tracking: bool
    supports_cost_tracking: bool
    supports_hooks: bool  # Native hook support (Claude only)


@dataclass(frozen=True)
class ExecutionParams:
    """Common execution parameters across agents.

    Base class for agent execution parameters. Backend-specific params
    can be handled via subclasses.
    """

    prompt: str
    working_directory: str | None = None
    timeout_seconds: int | None = None
    watch_phrases: list[str] | None = None


@dataclass
class ExecutionResult:
    """Normalized result from any agent execution.

    Provides a consistent structure for results regardless of which
    agent backend was used.

    Attributes:
        session_id: Ananta-generated session identifier
        backend_session_id: SDK-specific ID for resumption
        text: Concatenated output (populated from text_chunks if empty)
        text_chunks: Individual output chunks as received
        tool_calls: List of tool invocations with their inputs
        interrupted: Whether execution was interrupted
        interrupted_on: Reason for interruption (e.g., "timeout", "watch_phrase")
        alerts: Guard alerts triggered during execution
        metrics: Execution metrics (duration, cost, etc.)
        resumable: Whether session can be resumed
        error: Error message if execution failed
    """

    session_id: str
    backend_session_id: str | None = None
    text: str = ""
    text_chunks: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False
    interrupted_on: str | None = None
    alerts: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    resumable: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        """Ensure text is populated from chunks if empty."""
        if not self.text and self.text_chunks:
            # Use object.__setattr__ since dataclass might be frozen in future
            object.__setattr__(self, "text", "".join(self.text_chunks))
