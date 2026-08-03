"""Tool descriptors for the Streamable HTTP MCP transport.

Mirrors the MCP tool surface exposed by the stdio bridge subprocess
(:mod:`agent_messaging_plugin.mcp_bridge.__main__`).  Kept here as
plain dicts — the Streamable HTTP transport speaks raw JSON-RPC, not
the Python MCP SDK ``Tool`` type, so a separate descriptor table is
the most direct path.

The Streamable HTTP wording intentionally differs where hosted clients
cannot consume the stdio bridge's ``notifications/claude/channel``
convention. In particular, process calls should be followed with
``process_result`` polling instead of relying on bridge delivery events.
"""

from __future__ import annotations

from typing import Any, Final

# ---------------------------------------------------------------------
# Tool descriptors.  Each entry is the ``tools/list`` row the server
# returns verbatim; ``inputSchema`` is the JSON Schema MCP clients use
# for argument validation.
# ---------------------------------------------------------------------

_PROCESS_CALL_DESCRIPTION: Final[str] = (
    "Direct invocation of a homunculus process by process_key.  Zero "
    "inference, deterministic, fast.  THE PREFERRED entry point for any "
    "known process — knowledge base searches "
    "(service_interface::knowledge_service::search), memory recall "
    "(service_interface::memory_service::recall), plugin tools "
    "(plugin::<plugin>::<function>), everything.  Returns action_id + "
    "flow_id immediately; then call `process_result` with that action_id "
    "until the result appears or the action reaches an error status.  Use "
    "process_search first if you don't know the process_key and "
    "process_schema to confirm the argument shape."
)

_PEER_SEND_DESCRIPTION: Final[str] = "\n".join(
    [
        "Send a peer message to another live MCP session.",
        "",
        "Operator management:",
        "  For fleet/task management, prefer peer_send_by_name with a durable",
        "  role name such as Coordinator, Coordinator-Dusk, Architect, or",
        "  Git-Controller. Use raw peer_send only for replies to a specific",
        "  sender instance or when the operator explicitly names a live",
        "  session. Do not fan one task out to many peer_list entries.",
        "",
        "Addressing -- multi-instance:",
        "  peer_id is the stable agent kind (e.g., \"claude_code\", \"codex\").",
        "  Multiple instances of the same agent_id can be registered",
        "  concurrently. peer_agent_instance_id picks a specific one:",
        "    - omit when only one instance of peer_id is registered",
        "      (single-binding default); the call delivers to it.",
        "    - supply when multiple are registered; otherwise the call",
        "      fails with peer_ambiguous, listing the candidate",
        "      instance_ids and session_labels.",
        "  Discover candidates via peer_list. For replies, take the",
        "  sender_agent_instance_id from the inbox entry or the",
        "  peer_message notification meta and pass it as",
        "  peer_agent_instance_id here. When the reply hint also",
        "  supplies peer_agent_session_id, pass both: peer_send resolves",
        "  the exact instance FIRST and consults the stable session key",
        "  only after that instance is peer_unreachable. A live instance",
        "  always wins, even if the session key points elsewhere.",
        "",
        "Loop-prevention contract:",
        "  Messages are ALWAYS persisted in the (sender_bridge, peer_instance)",
        "  agent_thread. They only fire a notification on the receiver if the",
        '  prose begins with the marker "IMPORTANT" (case-sensitive, followed',
        '  by ":" or whitespace). The marker is stripped before delivery.',
        "",
        "  - With IMPORTANT marker -> notification fires. Claude Code wakes",
        "    through its registered native adapter; locally patched Codex",
        "    wakes through notifications/homunculus/peer_message. Use ONLY when",
        "    you genuinely need a response.",
        "  - Without marker -> silent persistence. The receiver only sees the",
        "    message if they explicitly call peer_inbox. Use for acks, status",
        "    updates, FYI notes -- anything that does not require a response.",
        "",
        "Forgetting the marker on a real question means the receiver never sees",
        "it -- you will need to resend with the marker. That is by design: it",
        'forces conscious "I want a response" choices and breaks ack loops.',
        "",
        "Fails (HTTP 400) if the peer is not currently registered, the",
        "peer_id is ambiguous without an instance hint (peer_ambiguous),",
        "or the supplied peer_agent_instance_id has no matching binding",
        "(peer_unreachable).",
    ],
)

_PEER_SEND_BY_NAME_DESCRIPTION: Final[str] = "\n".join(
    [
        "Send a peer message to the current holder of a durable homunculus role.",
        "",
        "Use this for ChatGPT/operator fleet management. The role binding is",
        "resolved at send time, so reconnects and bridge churn do not require",
        "ChatGPT to choose an agent_instance_id from peer_list.",
        "",
        "Examples of role names are Coordinator, Coordinator-Dusk, Architect,",
        "Git-Controller, and Codex-Reviewer, depending on which roles are",
        "currently claimed in the homunculus.",
        "",
        "Loop-prevention contract:",
        "  Prefix content with \"IMPORTANT: \" only when the role holder should",
        "  wake and act now. Without IMPORTANT the message is persisted for the",
        "  holder's role inbox but does not wake a live session.",
        "",
        "This is the preferred task-assignment tool. Use peer_send only for",
        "direct replies or an operator-requested exact live instance.",
    ],
)

_PEER_INBOX_DESCRIPTION: Final[str] = "\n".join(
    [
        "Pull peer messages addressed to your agent_id.",
        "",
        "Default behavior: return silent and IMPORTANT-marked messages",
        "as a durable catch-up view. Use an `after` timestamp when polling",
        "during an active incident so old IMPORTANT history does not flood",
        "the context window.",
        "",
        "Set include_important=false only for intentional silent-only",
        "status checks where previously-notified IMPORTANT messages would",
        "be noise.",
        "",
        "Spans every peer thread targeting you, regardless of which bridge owns",
        "the thread. Pagination uses after (ISO-8601 timestamp); pass the previous",
        "page's next_after_created_at back to read incrementally.",
        "",
        "Reading the inbox does NOT obligate you to reply.",
    ],
)

_PEER_LIST_DESCRIPTION: Final[str] = "\n".join(
    [
        "List currently-registered agent_ids (across all live bridges).",
        "",
        "Each instance entry carries agent_instance_id, session_label,",
        "parent_pid, created_at, and updated_at (all ISO-8601 UTC).",
        "created_at is the timestamp the bridge connected.",
        "updated_at advances on every dispatch operation (peer_send,",
        "peer_inbox, native wake) — it carries 'last active'",
        "semantics, so the newest updated_at points at the most",
        "recently active instance when multiple are registered",
        "concurrently.",
        "",
        "registered_at is also present as a deprecated alias for",
        "created_at; new code should read created_at + updated_at.",
    ],
)

_PEER_REGISTER_DESCRIPTION: Final[str] = "\n".join(
    [
        "Register or relabel this MCP session in the peer registry.",
        "",
        "Auto-registration: each transport registers this MCP session",
        "when it opens. Stdio uses $AGENT_IDENTITY when set and",
        "otherwise generates a durable agent_instance_id; Streamable",
        "HTTP uses the bearer-token claim. Call this tool manually",
        "only to change agent_id or session_label; the durable",
        "agent_instance_id stays the same so the registry replaces",
        "(not duplicates) the existing binding for this session.",
        "",
        "session_label is purely a human-facing display field (peer_list,",
        "envelope text). It is never used as a routing key.",
        "For identity introspection, use current_identity.",
    ],
)

TOOLS: Final[list[dict[str, Any]]] = [
    {
        "name": "current_identity",
        "description": (
            "Return identity and routing metadata for the current MCP session, "
            "including transport, homunculus_name, agent_id, "
            "agent_instance_id, agent_session_id, session_label, bridge_id, "
            "mcp_session_id, roles_held, and identity_trust. Use this to "
            "answer 'who am I?' or verify routing before peer_register, "
            "peer_claim_role, peer_send, or Streamable HTTP peer receive work. "
            "Returns no secrets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "download",
        "description": "Download a blob from homunculus storage to a local file path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "blob_id": {
                    "type": "string",
                    "description": 'Blob ID to download (e.g., "bmd-abc123def456").',
                },
                "output_path": {
                    "type": "string",
                    "description": "Local file path to write the downloaded file to.",
                },
            },
            "required": ["blob_id", "output_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "process_search",
        "description": (
            "Search the homunculus process registry for processes matching a "
            "natural-language query."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "process_schema",
        "description": (
            "Retrieve the invocation schema for a single homunculus process by "
            "its process_key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_key": {
                    "type": "string",
                    "description": "provider_type::provider::function_name",
                },
            },
            "required": ["process_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "process_call",
        "description": _PROCESS_CALL_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_key": {"type": "string"},
                "arguments": {"type": "object"},
                "reason": {"type": "string"},
            },
            "required": ["process_key", "arguments"],
            "additionalProperties": False,
        },
    },
    {
        "name": "process_result",
        "description": (
            "Snapshot read of an action_id's current observable state "
            "(status, error_message, latest stored raw result row if "
            "any). For hosted Streamable HTTP clients such as ChatGPT, "
            "this is the follow-up read after process_call returns "
            "action_id + flow_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string"},
            },
            "required": ["action_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_thread_open",
        "description": (
            "Open a durable inter-agent thread targeting a backend "
            "(codex|claude_code). Optionally include initial_message to "
            "dispatch a first turn immediately. Returns thread_id + (if "
            "initial_message) message_id/action_id/flow_id. Async -- turn "
            "results arrive as bridge_delivery_result channel notifications."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend": {
                    "type": "string",
                    "enum": ["codex", "claude_code"],
                    "description": "Backend that owns the thread.",
                },
                "working_directory": {
                    "type": "string",
                    "description": (
                        "Filesystem root for the backend; must be inside "
                        "allowed_working_directory_roots if configured."
                    ),
                },
                "title": {"type": "string", "description": "Optional short label."},
                "context": {
                    "type": "object",
                    "description": "Optional context passed to the backend.",
                    "properties": {
                        "summary": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
                "initial_message": {
                    "type": "object",
                    "description": (
                        "Optional first message; if present the thread is "
                        "dispatched immediately."
                    ),
                    "properties": {
                        "content": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": ["text"]},
                                    "text": {"type": "string"},
                                },
                                "required": ["type", "text"],
                                "additionalProperties": False,
                            },
                            "minItems": 1,
                        },
                        "response_mode": {"type": "string", "enum": ["async"]},
                        "timeout_seconds": {"type": "integer", "minimum": 1},
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
            },
            "required": ["backend"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_send",
        "description": (
            "Append a follow-up message to an existing thread and dispatch "
            "the next turn. Async -- completion arrives as a "
            "bridge_delivery_result notification with the structured "
            "payload (payload.status discriminates idle/interrupted/error)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "content": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["text"]},
                            "text": {"type": "string"},
                        },
                        "required": ["type", "text"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                },
                "response_mode": {"type": "string", "enum": ["async"]},
                "timeout_seconds": {"type": "integer", "minimum": 1},
            },
            "required": ["thread_id", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_messages",
        "description": (
            "Read messages from a thread using cursor pagination. Returns "
            "messages with cursor strictly greater than after_cursor, "
            "ordered ascending."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "after_cursor": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 50,
                },
            },
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_status",
        "description": (
            "Snapshot read of a thread: status "
            "(open|queued|running|idle|interrupted|error|closed), backend, "
            "last cursor, active_action_id/active_flow_id if a turn is in "
            "flight."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_close",
        "description": (
            "Close a thread terminally. Refuses (HTTP 409 "
            "agent_thread_running) if the thread has an active turn -- "
            "wait for it to land or for agent_interrupt support to ship."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_register",
        "description": _PEER_REGISTER_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        'Stable handle (e.g. "claude_code", "codex"); regex '
                        "[A-Za-z0-9._-]{1,64}."
                    ),
                },
                "session_label": {
                    "type": "string",
                    "description": (
                        'Optional human label for this session ("codex on '
                        'baroque-suite"). Defaults to the auto-inferred label.'
                    ),
                },
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_list",
        "description": _PEER_LIST_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_send",
        "description": _PEER_SEND_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer_id": {
                    "type": "string",
                    "description": "Target agent_id (must be currently registered).",
                },
                "peer_agent_instance_id": {
                    "type": "string",
                    "description": (
                        "Specific instance to address when multiple instances "
                        "of peer_id are registered. Omit when only one "
                        "instance exists."
                    ),
                },
                "peer_agent_session_id": {
                    "type": "string",
                    "description": (
                        "Stable logical-session fallback from a reply hint. "
                        "Used only after peer_agent_instance_id is unreachable; "
                        "never overrides a live instance."
                    ),
                },
                "content": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["text"]},
                            "text": {"type": "string"},
                        },
                        "required": ["type", "text"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                },
            },
            "required": ["peer_id", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_send_by_name",
        "description": _PEER_SEND_BY_NAME_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Durable role name registered in the homunculus role binding "
                        "table, such as Coordinator-Dusk or Architect."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Message text. Prefix with 'IMPORTANT: ' to wake the "
                        "current role holder."
                    ),
                },
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "peer_inbox",
        "description": _PEER_INBOX_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "after": {
                    "type": "string",
                    "description": "ISO-8601 high-water mark; omit for full inbox.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 50,
                },
                "include_important": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "When true, return the durable catch-up view including "
                        "messages whose sender used the IMPORTANT marker. "
                        "Default true. Set false only for intentional "
                        "silent-only status checks."
                    ),
                },
                "role_after": {
                    "type": "string",
                    "description": (
                        "Opaque cursor for the role_entries section (messages "
                        "addressed to a role you hold). Echo back the previous "
                        "page's next_role_cursor verbatim; omit for the first "
                        "page. Distinct from 'after' (the instance section)."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
]


SERVER_VERSION: Final[str] = "1.0.0"
SUPPORTED_PROTOCOL_VERSION: Final[str] = "2025-03-26"

# Fallback used when the homunculus name is empty / unset (laptop dev
# mode, unit tests).  Never reaches a production phone client — the
# plugin reads $HOMUNCULUS_NAME at start_interface time and threads it
# through to every layer that emits server identity.
FALLBACK_HOMUNCULUS_NAME: Final[str] = "homunculus"


def build_server_name(homunculus_name: str) -> str:
    """Render the ``serverInfo.name`` advertised in the initialize response.

    Format: ``"<homunculus>-streamable"`` so the MCP client surfaces
    the actual homunculus identity rather than a generic platform
    label.  Empty input falls back to ``FALLBACK_HOMUNCULUS_NAME``.
    """
    return f"{homunculus_name or FALLBACK_HOMUNCULUS_NAME}-streamable"


def build_server_instructions(homunculus_name: str) -> str:
    """Render the ``instructions`` block returned by ``initialize``.

    Embeds the homunculus name into the lead sentence so a Claude
    Desktop / claude.ai client doesn't refer to a remote homunculus
    by a generic label.
    """
    name = homunculus_name or FALLBACK_HOMUNCULUS_NAME
    return "\n".join(
        [
            f"{name} platform bridge (Streamable HTTP transport).",
            "",
            "Same process and messaging tools as the stdio bridge.  Use "
            "`process_call` for any known process_key (zero inference), then "
            "`process_result` with the returned action_id to read completion.  "
            "Peer messages between you and other live MCP-connected agents flow "
            "through `peer_send`; the loop-prevention contract gates wake-up "
            "on the IMPORTANT marker.",
            "",
            "When acting as an operator control plane, assign work through "
            "`peer_send_by_name` to a durable role such as Coordinator, "
            "Coordinator-Dusk, Architect, or Git-Controller.  Use raw "
            "`peer_send` only for direct replies to a specific sender instance "
            "or when the operator explicitly names a live session.",
            "",
            "Server-pushed peer notifications may arrive on the SSE channel "
            'opened by GET as JSON-RPC notifications named '
            '"notifications/claude/channel". Hosted clients should not rely on '
            "bridge_delivery_result for process_call completion; use "
            "`process_result` instead.",
        ],
    )


__all__ = [
    "FALLBACK_HOMUNCULUS_NAME",
    "SERVER_VERSION",
    "SUPPORTED_PROTOCOL_VERSION",
    "TOOLS",
    "build_server_instructions",
    "build_server_name",
]
