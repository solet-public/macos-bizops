"""Ordered operator rules, atomic correction, and policy-aware catalog writes.

No telemetry participates. The singleton's ordinary updated_at compare-and-set
is shared by policy edits and catalog insertion so a new cell cannot commit
using an obsolete rule. History uses the catalog's existing audit tables.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE as NS
from ananta.llm.agent_messaging.state_results import require_records

from .model_capability_store import CatalogError, read_cells_page
from .schema import TABLE_MODEL_CAPABILITY_CELL as CELLS
from .schema import TABLE_MODEL_CAPABILITY_OBSERVATION as OBSERVATIONS
from .schema import TABLE_MODEL_CAPABILITY_REFRESH_RUN as RUNS
from .schema import TABLE_MODEL_CELL_POLICY as POLICY

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface, StateTransaction

_POLICY_ID = "mcp-current"
_DIMENSIONS = frozenset({"provider", "runtime", "model_family_prefix", "model", "effort"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CellRule:
    id: str
    selector: dict[str, str]
    enabled: bool


@dataclass(frozen=True, slots=True)
class CellPolicy:
    rules: tuple[CellRule, ...]
    updated_at: object


def _selector(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) - _DIMENSIONS:
        raise CatalogError("parameter_invalid", "selector must contain only provider, runtime, model_family_prefix, model and effort.")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise CatalogError("parameter_invalid", "selector dimensions must be nonempty strings.")
        result[str(key)] = value
    return result


def _boolean(raw: object, name: str) -> bool:
    if not isinstance(raw, bool):
        raise CatalogError("parameter_invalid", f"{name} must be boolean.")
    return raw


def _rules(raw: object) -> tuple[CellRule, ...]:
    if not isinstance(raw, list):
        raise CatalogError("policy_invalid", "stored rules must be an ordered JSON list.")
    result: list[CellRule] = []
    try:
        for row in raw:
            if not isinstance(row, dict) or set(row) != {"id", "selector", "enabled"} or not isinstance(row["id"], str) or not row["id"]:
                raise CatalogError("parameter_invalid", "rule requires id, selector and enabled.")
            result.append(CellRule(row["id"], _selector(row["selector"]), _boolean(row["enabled"], "enabled")))
    except CatalogError as exc:
        raise CatalogError("policy_invalid", f"Malformed stored policy: {exc.message}") from exc
    if len({rule.id for rule in result}) != len(result):
        raise CatalogError("policy_invalid", "stored rule identifiers repeat.")
    return tuple(result)


def _policy(rows: list[dict[str, Any]]) -> CellPolicy:
    if not rows:
        logger.warning("model cell policy is uninitialized; explicit default all-on applies")
        return CellPolicy((), None)
    row = rows[0]
    if row.get("updated_at") is None:
        raise CatalogError("policy_invalid", "stored policy has no updated_at premise.")
    return CellPolicy(_rules(row.get("rules")), row["updated_at"])


def read_policy(state: StateManagementInterface) -> CellPolicy:
    return _policy(require_records(state.query_ordered(NS, {"table": POLICY, "filters": {"id": _POLICY_ID}, "order_by": [["created_at", "asc"], ["id", "asc"]], "limit": 1})))


def matches(selector: dict[str, str], cell: dict[str, Any]) -> bool:
    return all(str(cell.get("model", "")).startswith(value) if key == "model_family_prefix" else cell.get(key) == value for key, value in selector.items())


def decision(policy: CellPolicy, cell: dict[str, Any]) -> tuple[bool, str]:
    enabled, provenance = True, "default_on"
    for rule in policy.rules:
        if matches(rule.selector, cell):
            enabled, provenance = rule.enabled, rule.id
    return enabled, provenance


def _record(table: str, prefix: str, fields: dict[str, object]) -> dict[str, object]:
    stamp = datetime.now(UTC)
    identifier = f"{prefix}-{uuid4().hex}"
    return {"table": table, "record": {"id": identifier, "name": identifier, "namespace": NS, "created_at": stamp, "updated_at": stamp, "created_by": "model_cell_enablement", "updated_by": "model_cell_enablement", **fields}}


def _claim_policy(txn: StateTransaction, policy: CellPolicy, rules: tuple[CellRule, ...]) -> None:
    values: dict[str, object] = {"rules": [asdict(rule) for rule in rules], "updated_at": datetime.now(UTC)}
    if policy.updated_at is None:
        data = _record(POLICY, "mcp", values)
        cast(dict[str, object], data["record"])["id"] = _POLICY_ID
        txn.write_state(NS, data)
    elif txn.update_state(NS, {"table": POLICY, "filters": {"id": _POLICY_ID, "updated_at": policy.updated_at}}, values) != 1:
        raise CatalogError("stale_policy_state", "Policy changed; read it again before correcting it. No changes committed.")


def write_catalog_cell(state: StateManagementInterface, record: dict[str, Any]) -> None:
    """Preserve existing flags; evaluate standing policy for a newly inserted cell."""
    policy = read_policy(state)
    with state.transactional() as txn:
        _claim_policy(txn, policy, policy.rules)
        key = {name: record[name] for name in ("runtime", "model", "effort")}
        rows = txn.query_state(NS, {"table": CELLS, "filters": key, "limit": 1})
        if rows:
            if txn.update_state(NS, {"table": CELLS, "filters": {"id": rows[0]["id"]}}, dict(record)) != 1:
                raise CatalogError("stale_cell_state", "Catalog cell disappeared during correction.")
        else:
            enabled, _ = decision(policy, record)
            txn.write_state(NS, _record(CELLS, "mcc", {**record, "enabled": enabled}))


def _rule_index(policy: CellPolicy, rule_id: object, action: object) -> int | None:
    index = next((i for i, rule in enumerate(policy.rules) if rule.id == rule_id), None)
    if action in ("correct", "retire", "clear") and index is None:
        raise CatalogError("rule_not_found", "Read the current rules and supply an existing rule_id.")
    return index if action in ("correct", "retire", "clear") else None


def _set_rule(policy: CellPolicy, params: dict[str, Any], index: int | None) -> tuple[tuple[CellRule, ...], str, dict[str, str]]:
    action = params.get("action", "set")
    selector = _selector(params.get("selector"))
    enabled = _boolean(params.get("enabled"), "enabled")
    only = _boolean(params.get("only", False), "only")
    if only and (not enabled or action != "set"):
        raise CatalogError("parameter_invalid", "only=true requires action=set and enabled=true.")
    identifier = str(params.get("rule_id")) if action == "correct" else f"rule-{uuid4().hex}"
    rule = CellRule(identifier, selector, enabled)
    if only:
        return (CellRule(f"rule-{uuid4().hex}", {}, False), rule), identifier, selector
    if index is not None:
        return (*policy.rules[:index], rule, *policy.rules[index + 1 :]), identifier, selector
    if params.get("rule_id") is not None:
        raise CatalogError("parameter_invalid", "set allocates a rule_id; use correct to replace an existing rule.")
    return (*policy.rules, rule), identifier, selector


def _next_rules(policy: CellPolicy, params: dict[str, Any]) -> tuple[tuple[CellRule, ...], str | None, dict[str, str] | None]:
    action = params.get("action", "set")
    rule_id = params.get("rule_id")
    if action == "clear" and rule_id is None:
        return (), None, None
    index = _rule_index(policy, rule_id, action)
    if action in ("retire", "clear"):
        return tuple(rule for rule in policy.rules if rule.id != rule_id), str(rule_id), None
    if action not in ("set", "correct"):
        raise CatalogError("parameter_invalid", "action must be set, correct, retire or clear.")
    return _set_rule(policy, params, index)


def _audit_run(txn: StateTransaction, previous: CellPolicy, rules: tuple[CellRule, ...], actor: str, reason: str, count: int) -> str:
    stamp = datetime.now(UTC).isoformat()
    snapshot = {"previous_rules": [asdict(rule) for rule in previous.rules], "next_rules": [asdict(rule) for rule in rules], "actor": actor, "reason": reason}
    return txn.write_state(
        NS,
        _record(
            RUNS,
            "mcr",
            {
                "started_at": stamp,
                "finished_at": stamp,
                "trigger": "policy_change",
                "status": "completed",
                "sources_ok": ["operator_policy"],
                "sources_failed": [],
                "cells_accepted": 0,
                "cells_conflicted": 0,
                "cells_unchanged": count,
                "note": json.dumps(snapshot, sort_keys=True),
            },
        ),
    )


def _apply_cells(txn: StateTransaction, rows: list[dict[str, Any]], policy: CellPolicy, run_id: str, actor: str, reason: str) -> None:
    for row in rows:
        enabled, provenance = decision(policy, row)
        if row.get("enabled", True) == enabled:
            continue
        if row.get("updated_at") is None:
            raise CatalogError("catalog_invalid", "Catalog cell has no updated_at premise.")
        changed = txn.update_state(NS, {"table": CELLS, "filters": {"id": row["id"], "updated_at": row["updated_at"]}}, {"enabled": enabled})
        if changed != 1:
            raise CatalogError("stale_cell_state", "Catalog changed during the policy edit; no changes committed.")
        txn.write_state(
            NS,
            _record(
                OBSERVATIONS,
                "mco",
                {
                    "refresh_run_id": run_id,
                    "source_id": "operator_policy",
                    "fetch_method": f"set_model_cells_enabled by {actor}: {reason}",
                    "fetched_at": datetime.now(UTC).isoformat(),
                    **{key: row[key] for key in ("provider", "runtime", "model", "effort")},
                    "metric": "enabled_change",
                    "value_number": None,
                    "value_text": json.dumps({"previous": row.get("enabled", True), "enabled": enabled, "provenance": provenance}),
                    "raw_excerpt": None,
                    "fetch_status": "ok",
                },
            ),
        )


def _validate_supplied(params: dict[str, Any]) -> None:
    for name in ("enabled", "only"):
        if name in params:
            _boolean(params[name], name)
    if "selector" in params:
        _selector(params["selector"])
    premise = params.get("expected_updated_at")
    if premise is None or isinstance(premise, datetime):
        return
    if isinstance(premise, str):
        try:
            datetime.fromisoformat(premise)
            return
        except ValueError as exc:
            raise CatalogError("parameter_invalid", "expected_updated_at must be the policy timestamp from readback.") from exc
    raise CatalogError("parameter_invalid", "expected_updated_at must be a timestamp or initial null.")


def set_model_cells_enabled(state: StateManagementInterface, params: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Called only after the plugin derives and authorizes the current Operator."""
    reason = params.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise CatalogError("parameter_invalid", "reason must be nonempty.")
    if "expected_updated_at" not in params:
        raise CatalogError("parameter_invalid", "Read policy first; pass expected_updated_at (null only for initial create).")
    _validate_supplied(params)
    policy = read_policy(state)
    rules, identifier, selector = _next_rules(policy, params)
    if params["expected_updated_at"] is None and policy.updated_at is not None:
        raise CatalogError("stale_policy_state", "Initial create requires an absent policy; read the current policy.")
    with state.transactional() as txn:
        # Claim using the caller's premise, rather than the fresh read's timestamp.
        _claim_policy(txn, CellPolicy(policy.rules, params["expected_updated_at"]), rules)
        rows = read_cells_page(state, ceiling=None).rows
        run_id = _audit_run(txn, policy, rules, actor, reason, len(rows))
        _apply_cells(txn, rows, CellPolicy(rules, None), run_id, actor, reason)
    result = read_model_cell_policy(state)
    result.update({"rule_id": identifier, "run_id": run_id})
    if selector is not None and not any(matches(selector, row) for row in rows):
        result.setdefault("warnings", []).append("Selector matches zero current cells; standing rule retained for future cells.")
    return result


def read_model_cell_policy(state: StateManagementInterface, *, history_limit: object = 20) -> dict[str, Any]:
    if isinstance(history_limit, bool) or not isinstance(history_limit, int) or not 1 <= history_limit <= 100:
        raise CatalogError("parameter_invalid", "history_limit must be 1..100.")
    policy = read_policy(state)
    history = require_records(state.query_ordered(NS, {"table": RUNS, "filters": {"trigger": "policy_change"}, "order_by": [["created_at", "desc"], ["id", "desc"]], "limit": history_limit}))
    cells = [{**row, "enabled_provenance": decision(policy, row)[1]} for row in read_cells_page(state, ceiling=None).rows]
    result: dict[str, Any] = {"rules": [asdict(rule) for rule in policy.rules], "updated_at": policy.updated_at, "cells": cells, "history": history}
    if policy.updated_at is None:
        result["warnings"] = ["Policy uninitialized; explicit default all-on applies. Initial create uses expected_updated_at=null."]
    return result
