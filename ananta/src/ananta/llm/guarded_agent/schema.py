"""Schema definition for guarded agent session tracking.

Provides a shared state table for all plugins implementing GuardedAgentInterface.
Session metadata is stored in state for queryability; raw telemetry remains
in files for streaming writes.
"""

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

NAMESPACE = "core"


def get_agent_session_schema() -> SchemaDefinition:
    """Schema for agent execution sessions.

    Tracks metadata for LLM agent invocations (Claude Code, Codex).
    Raw telemetry (chunks, events, tool_calls) remains in files.

    Table: guarded_agent__agent_session
    """
    return SchemaDefinition(
        namespace=NAMESPACE,
        tables={
            "agent_session": TableSchema(
                table_name="agent_session",
                description="Agent execution session metadata for GuardedAgentInterface plugins",
                id_prefix="ags",
                columns={
                    "session_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        unique=True,
                        description="Plugin-generated session identifier",
                    ),
                    "backend_session_id": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="SDK-specific session ID for resumption",
                    ),
                    "plugin_name": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Name of the agent plugin",
                    ),
                    "backend": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check="backend IN ('claude_code', 'codex')",
                        description="Agent backend type",
                    ),
                    "prompt": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Initial execution prompt",
                    ),
                    "working_directory": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Working directory for execution",
                    ),
                    "status": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        default="running",
                        check="status IN ('running', 'completed', 'interrupted', 'error')",
                        description="Execution status",
                    ),
                    "interrupted": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        default=0,
                        description="Whether execution was interrupted (0=false, 1=true)",
                    ),
                    "interrupted_on": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Reason for interruption (timeout, watch_phrase, manual)",
                    ),
                    "error": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Error message if execution failed",
                    ),
                    "metrics": ColumnDefinition(
                        type=ColumnType.JSON,
                        default="{}",
                        description="Execution metrics (duration, cost, turns, etc.)",
                    ),
                    "data_dir": ColumnDefinition(
                        type=ColumnType.TEXT,
                        description="Path to telemetry files directory",
                    ),
                    "resumable": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        default=0,
                        description="Whether session can be resumed (0=false, 1=true)",
                    ),
                    "started_at": ColumnDefinition(
                        type=ColumnType.DATETIME,
                        not_null=True,
                        description="Timestamp when execution started",
                    ),
                    "completed_at": ColumnDefinition(
                        type=ColumnType.DATETIME,
                        description="Timestamp when execution completed",
                    ),
                },
                indexes=[
                    IndexDefinition("idx_agent_session_session_id", ["session_id"]),
                    IndexDefinition("idx_agent_session_plugin", ["plugin_name"]),
                    IndexDefinition("idx_agent_session_backend", ["backend"]),
                    IndexDefinition("idx_agent_session_status", ["status"]),
                    IndexDefinition("idx_agent_session_started_at", ["started_at"]),
                ],
            ),
        },
    )
