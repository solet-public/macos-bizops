"""Guarded agent infrastructure for Claude Code and Codex plugins.

This module provides:
- GuardedAgentInterface protocol for agent plugin implementations
- TelemetryWriter for session artifact persistence
- WatchPhraseChecker for content-based guardrails (Codex only)
- Shared data models (ExecutionParams, ExecutionResult, etc.)
- Shared schema for agent session state tracking
"""

from ananta.llm.guarded_agent.models import (
    AgentBackend as AgentBackend,
)
from ananta.llm.guarded_agent.models import (
    AgentCapabilities as AgentCapabilities,
)
from ananta.llm.guarded_agent.models import (
    ExecutionParams as ExecutionParams,
)
from ananta.llm.guarded_agent.models import (
    ExecutionResult as ExecutionResult,
)
from ananta.llm.guarded_agent.schema import NAMESPACE
from ananta.llm.guarded_agent.schema import (
    get_agent_session_schema as get_agent_session_schema,
)
from ananta.llm.guarded_agent.telemetry import TelemetryWriter as TelemetryWriter
from ananta.llm.guarded_agent.watch_phrase_checker import (
    WatchPhraseAlert as WatchPhraseAlert,
)
from ananta.llm.guarded_agent.watch_phrase_checker import (
    WatchPhraseChecker as WatchPhraseChecker,
)

# Namespace for agent session table (shared by all GuardedAgentInterface plugins)
# Now uses "core" namespace since this is platform-level shared infrastructure
AGENT_SESSION_NAMESPACE = NAMESPACE

# Backwards compatibility alias (deprecated - use AGENT_SESSION_NAMESPACE)
GUARDED_AGENT_NAMESPACE = NAMESPACE
