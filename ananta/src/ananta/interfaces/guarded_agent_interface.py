"""Guarded Agent Interface - Protocol for agent plugins.

Defines the shared interface for agent plugins (Claude Code, Codex) that
invoke external LLM agents with guardrails.

Both plugins implement this interface:
- The async methods are called internally by the plugin
- @platform_process provides the sync bridge for action queue integration
"""

from typing import Any, ClassVar, Protocol

from ananta.llm.guarded_agent.models import (
    AgentCapabilities,
    ExecutionParams,
    ExecutionResult,
)


class GuardedAgentInterface(Protocol):
    """Protocol for agent plugins.

    Both Codex and Claude Code plugins implement this interface.
    The async methods are called internally; @platform_process provides sync bridge.

    Attributes:
        name: Plugin name (class variable)
        _active_sessions: Session tracking for interruption support
    """

    name: ClassVar[str]

    # Session tracking for interruption support
    _active_sessions: dict[str, Any]  # session_id -> client/process

    def describe_capabilities(self) -> AgentCapabilities:
        """Return capabilities of this agent backend.

        Returns:
            AgentCapabilities describing what this backend supports
        """
        ...

    async def execute_agent(
        self,
        params: ExecutionParams,
        app_home: str,
    ) -> ExecutionResult:
        """Execute agent task.

        Args:
            params: Execution parameters
            app_home: Ananta home directory for telemetry

        Returns:
            Normalized execution result
        """
        ...

    async def resume(
        self,
        session_id: str,
        prompt: str,
        app_home: str,
    ) -> ExecutionResult:
        """Resume a previous session.

        Args:
            session_id: Backend session ID from previous execution
            prompt: Follow-up prompt
            app_home: Ananta home directory for telemetry

        Returns:
            Normalized execution result
        """
        ...

    async def interrupt(self, session_id: str) -> bool:
        """Interrupt a running session.

        Implementation must:
        1. Look up session in _active_sessions
        2. Call appropriate interrupt method (client.interrupt() or proc.terminate())
        3. Remove from _active_sessions

        Args:
            session_id: Session ID to interrupt

        Returns:
            True if interrupt was sent, False if session not found
        """
        ...
