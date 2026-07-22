"""LLM integration utilities for Ananta.

This module provides shared infrastructure for agent plugins that invoke
external LLM agents like Claude Code and Codex.
"""

from ananta.llm.guarded_agent import (
    AgentBackend as AgentBackend,
)
from ananta.llm.guarded_agent import (
    AgentCapabilities as AgentCapabilities,
)
from ananta.llm.guarded_agent import (
    ExecutionParams as ExecutionParams,
)
from ananta.llm.guarded_agent import (
    ExecutionResult as ExecutionResult,
)
from ananta.llm.guarded_agent import (
    TelemetryWriter as TelemetryWriter,
)
from ananta.llm.guarded_agent import (
    WatchPhraseAlert as WatchPhraseAlert,
)
from ananta.llm.guarded_agent import (
    WatchPhraseChecker as WatchPhraseChecker,
)
