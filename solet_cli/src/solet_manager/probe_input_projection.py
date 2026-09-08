"""Typed, non-secret decision facts needed by target-local aggregate probes."""

from __future__ import annotations

from typing import cast

from .errors import StateConflictError
from .models import JsonValue
from .transaction import Transaction

_AUTOSTART_PROBE_REFS = frozenset(
    {
        "genesis::solet.verify",
        "genesis::autostart.verify",
    }
)
_CODING_AGENT_PROBE_REFS = frozenset(
    {
        "setup::coding_agents.verify_plugins",
        "setup::coding_agents.verify_hooks",
        "plugin::agent_messaging_plugin.peer_identity",
    }
)
_CODING_AGENT_OPTIONS = frozenset({"codex", "claude_code"})
_STRUCTURED_ACTION_QUALIFICATION_PROBE = "setup::models.qualify_structured_actions"


def probe_public_inputs(
    transaction: Transaction,
    probe_ref: str,
) -> dict[str, JsonValue]:
    """Project only the resolved facts the declared probe is entitled to use."""

    if probe_ref in _AUTOSTART_PROBE_REFS:
        return {"autostart": _autostart_decision(transaction)}
    if probe_ref in _CODING_AGENT_PROBE_REFS:
        return {"selected_coding_agents": list(_coding_agent_selection(transaction))}
    if probe_ref == _STRUCTURED_ACTION_QUALIFICATION_PROBE:
        return {"candidate_id": _inference_model_selection(transaction)}
    return {}


def _decisions(transaction: Transaction) -> dict[str, JsonValue]:
    decisions = transaction.answers.get("decisions")
    if not isinstance(decisions, dict):
        raise StateConflictError("transaction decisions are not a typed object")
    return decisions


def _autostart_decision(transaction: Transaction) -> str:
    value = _decisions(transaction).get("autostart")
    if value not in {"enabled", "disabled"}:
        raise StateConflictError("transaction autostart decision is unresolved or invalid")
    return str(value)


def _coding_agent_selection(transaction: Transaction) -> tuple[str, ...]:
    value = _decisions(transaction).get("coding_agents")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item in _CODING_AGENT_OPTIONS for item in value)
        or len(value) != len(set(value))
    ):
        raise StateConflictError("transaction coding-agent selection is unresolved or invalid")
    return tuple(cast(str, item) for item in value)


def _inference_model_selection(transaction: Transaction) -> str:
    value = _decisions(transaction).get("inference_model")
    if not isinstance(value, str) or not value:
        raise StateConflictError("transaction inference-model selection is unresolved or invalid")
    return value
