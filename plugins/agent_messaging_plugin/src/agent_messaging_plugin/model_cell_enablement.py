"""Ordered operator rules, atomic correction, and policy-aware catalog writes.

No telemetry participates. The singleton's ordinary updated_at compare-and-set
is shared by policy edits and catalog insertion so a new cell cannot commit
using an obsolete rule. History uses the catalog's existing audit tables.

A rule carries an ``enabled`` opinion, a preference ``weight`` opinion, or both.
Each opinion resolves on its own, last matching rule winning: a weight rule
never changes whether a cell is on, and an enabled rule never changes a weight.
The weight is not stored on cell rows; selection resolves it from the rules, so
changing a preference is one policy write with no catalog change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
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
_RULE_KEYS = frozenset({"id", "selector", "enabled", "weight", "kind", "words", "ends_at"})
DEFAULT_WEIGHT = 1.0
DEFAULT_WEIGHT_PROVENANCE = "default_weight"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CellRule:
    """One ordered rule: at least one of ``enabled`` and ``weight`` is set; an unset one has no opinion."""

    id: str
    selector: dict[str, str]
    enabled: bool | None = None
    weight: float | None = None
    kind: str | None = None
    words: str | None = None
    ends_at: str | None = None


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


def _weight(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not isfinite(raw) or raw <= 0:
        raise CatalogError("parameter_invalid", "weight must be a positive finite number.")
    return float(raw)


def _text(raw: object, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError("parameter_invalid", f"{name} must be a nonblank string.")
    return raw


def _end(raw: object) -> str:
    value = _text(raw, "ends_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CatalogError("parameter_invalid", "ends_at must be an offset-bearing ISO timestamp.") from exc
    if parsed.utcoffset() is None:
        raise CatalogError("parameter_invalid", "ends_at must be an offset-bearing ISO timestamp.")
    return value


def _now() -> datetime:
    return datetime.now(UTC)


def _lapsed(rule: CellRule, now: datetime) -> bool:
    return rule.ends_at is not None and datetime.fromisoformat(rule.ends_at) <= now


def _applies(rule: CellRule, kind: str | None, now: datetime) -> bool:
    """A missing kind is global; a named kind applies only to that request.

    The end is exclusive: at the end instant the rule has already lapsed.
    Selector matching stays separate because session limits share that grammar.
    """
    return (rule.kind is None or rule.kind == kind) and not _lapsed(rule, now)


def _optional(row: dict[str, Any], key: str) -> str | None:
    if key not in row:
        return None
    return _end(row[key]) if key == "ends_at" else _text(row[key], key)


def _stored_rule(row: object) -> CellRule:
    """Decode the persisted rule without requiring fields added after launch.

    Absent optional fields retain the legacy global, non-expiring behavior.
    Present fields are validated before they can affect a policy decision.
    An enabled or weight opinion is still required for every stored rule.
    """
    if not isinstance(row, dict) or not {"id", "selector"} <= set(row) or set(row) - _RULE_KEYS or not set(row) & {"enabled", "weight"} or not isinstance(row["id"], str) or not row["id"]:
        raise CatalogError(
            "parameter_invalid",
            "rule requires id, selector and at least one of enabled and weight, and no other keys.",
        )
    enabled = _boolean(row["enabled"], "enabled") if "enabled" in row else None
    weight = _weight(row["weight"]) if "weight" in row else None
    return CellRule(
        row["id"],
        _selector(row["selector"]),
        enabled,
        weight,
        _optional(row, "kind"),
        _optional(row, "words"),
        _optional(row, "ends_at"),
    )


def _rule_dict(rule: CellRule) -> dict[str, object]:
    """The stored shape: keys for the opinions the rule has, none for the ones it lacks."""
    stored: dict[str, object] = {"id": rule.id, "selector": rule.selector}
    if rule.enabled is not None:
        stored["enabled"] = rule.enabled
    if rule.weight is not None:
        stored["weight"] = rule.weight
    for key in ("kind", "words", "ends_at"):
        value = getattr(rule, key)
        if value is not None:
            stored[key] = value
    return stored


def _rules(raw: object) -> tuple[CellRule, ...]:
    if not isinstance(raw, list):
        raise CatalogError("policy_invalid", "stored rules must be an ordered JSON list.")
    result: list[CellRule] = []
    try:
        result.extend(_stored_rule(row) for row in raw)
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


def decision(policy: CellPolicy, cell: dict[str, Any], kind: str | None = None, now: datetime | None = None) -> tuple[bool, str]:
    enabled, provenance = True, "default_on"
    moment = now if now is not None else _now()
    for rule in policy.rules:
        if rule.enabled is not None and _applies(rule, kind, moment) and matches(rule.selector, cell):
            enabled, provenance = rule.enabled, rule.id
    return enabled, provenance


def preference(policy: CellPolicy, cell: dict[str, Any], kind: str | None = None, now: datetime | None = None) -> tuple[float, str | None]:
    """The weight of the last matching rule that carries one, with that rule's id; (1.0, None) when none does."""
    weight, provenance = DEFAULT_WEIGHT, None
    moment = now if now is not None else _now()
    for rule in policy.rules:
        if rule.weight is not None and _applies(rule, kind, moment) and matches(rule.selector, cell):
            weight, provenance = rule.weight, rule.id
    return weight, provenance


def _record(table: str, prefix: str, fields: dict[str, object]) -> dict[str, object]:
    stamp = datetime.now(UTC)
    identifier = f"{prefix}-{uuid4().hex}"
    return {"table": table, "record": {"id": identifier, "name": identifier, "namespace": NS, "created_at": stamp, "updated_at": stamp, "created_by": "model_cell_enablement", "updated_by": "model_cell_enablement", **fields}}


def _claim_policy(txn: StateTransaction, policy: CellPolicy, rules: tuple[CellRule, ...]) -> None:
    values: dict[str, object] = {"rules": [_rule_dict(rule) for rule in rules], "updated_at": datetime.now(UTC)}
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


def _opinions(params: dict[str, Any]) -> tuple[bool | None, float | None]:
    """The rule's enabled and weight as supplied. Null or omitted means no opinion; at least one is required."""
    enabled = None if params.get("enabled") is None else _boolean(params["enabled"], "enabled")
    weight = None if params.get("weight") is None else _weight(params["weight"])
    if enabled is None and weight is None:
        raise CatalogError("parameter_invalid", "supply enabled, weight or both; null or omitted means no opinion.")
    return enabled, weight


def _set_rule(policy: CellPolicy, params: dict[str, Any], index: int | None) -> tuple[tuple[CellRule, ...], str, dict[str, str]]:
    action = params.get("action", "set")
    selector = _selector(params.get("selector"))
    enabled, weight = _opinions(params)
    only = _boolean(params.get("only", False), "only")
    if only and (enabled is not True or action != "set"):
        raise CatalogError("parameter_invalid", "only=true requires action=set and enabled=true.")
    identifier = str(params.get("rule_id")) if action == "correct" else f"rule-{uuid4().hex}"
    rule = CellRule(
        identifier,
        selector,
        enabled,
        weight,
        params.get("kind"),
        params.get("words", params["reason"]),
        params.get("ends_at"),
    )
    if only:
        return (
            (
                CellRule(f"rule-{uuid4().hex}", {}, False, words=params.get("words", params["reason"])),
                rule,
            ),
            identifier,
            selector,
        )
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
    snapshot = {"previous_rules": [_rule_dict(rule) for rule in previous.rules], "next_rules": [_rule_dict(rule) for rule in rules], "actor": actor, "reason": reason}
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


def _apply_cells(
    txn: StateTransaction,
    rows: list[dict[str, Any]],
    policy: CellPolicy,
    run_id: str,
    actor: str,
    reason: str,
) -> None:
    moment = _now()
    for row in rows:
        enabled, provenance = decision(policy, row, None, moment)
        if row.get("enabled", True) == enabled:
            continue
        if row.get("updated_at") is None:
            raise CatalogError("catalog_invalid", "Catalog cell has no updated_at premise.")
        changed = txn.update_state(
            NS,
            {"table": CELLS, "filters": {"id": row["id"], "updated_at": row["updated_at"]}},
            {"enabled": enabled},
        )
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
                    "value_text": json.dumps(
                        {
                            "previous": row.get("enabled", True),
                            "enabled": enabled,
                            "provenance": provenance,
                        }
                    ),
                    "raw_excerpt": None,
                    "fetch_status": "ok",
                },
            ),
        )


def _validate_flags(params: dict[str, Any]) -> None:
    if "only" in params:
        _boolean(params["only"], "only")
    if params.get("enabled") is not None:
        _boolean(params["enabled"], "enabled")
    if params.get("weight") is not None:
        _weight(params["weight"])


def _validate_supplied(params: dict[str, Any]) -> None:
    _validate_flags(params)
    for name in ("kind", "words"):
        if name in params:
            _text(params[name], name)
    if "ends_at" in params:
        _end(params["ends_at"])
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
            raise CatalogError(
                "parameter_invalid",
                "expected_updated_at must be the policy timestamp from readback.",
            ) from exc
    raise CatalogError("parameter_invalid", "expected_updated_at must be a timestamp or initial null.")


def _discarded_weights(policy: CellPolicy, params: dict[str, Any]) -> str | None:
    """The advisory for an only=true call that replaces weight rules along with every other rule."""
    ids = [rule.id for rule in policy.rules if rule.weight is not None] if params.get("only") else []
    return f"only=true replaced every rule, including weight rule(s) {ids}; previous_rules in the policy history keeps them." if ids else None


def set_model_cells_enabled(state: StateManagementInterface, params: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Called only after the plugin derives and authorizes the current Operator.

    A rule sets ``enabled``, ``weight`` or both. ``correct`` replaces the identified
    rule with exactly what is supplied, so a correction that leaves the weight out
    clears it; ``retire`` removes the rule and with it any weight.
    """
    reason = params.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise CatalogError("parameter_invalid", "reason must be nonempty.")
    if "expected_updated_at" not in params:
        raise CatalogError(
            "parameter_invalid",
            "Read policy first; pass expected_updated_at (null only for initial create).",
        )
    _validate_supplied(params)
    policy = read_policy(state)
    rules, identifier, selector = _next_rules(policy, params)
    if params["expected_updated_at"] is None and policy.updated_at is not None:
        raise CatalogError(
            "stale_policy_state",
            "Initial create requires an absent policy; read the current policy.",
        )
    with state.transactional() as txn:
        # Claim using the caller's premise, rather than the fresh read's timestamp.
        _claim_policy(txn, CellPolicy(policy.rules, params["expected_updated_at"]), rules)
        rows = read_cells_page(state, ceiling=None).rows
        run_id = _audit_run(txn, policy, rules, actor, reason, len(rows))
        _apply_cells(txn, rows, CellPolicy(rules, None), run_id, actor, reason)
    result = read_model_cell_policy(state)
    result.update({"rule_id": identifier, "run_id": run_id})
    _write_warnings(result, selector, rows, policy, params)
    return result


def _write_warnings(result: dict[str, Any], selector: dict[str, str] | None, rows: list[dict[str, Any]], policy: CellPolicy, params: dict[str, Any]) -> None:
    if selector is not None and not any(matches(selector, row) for row in rows):
        result.setdefault("warnings", []).append("Selector matches zero current cells; standing rule retained for future cells.")
    if params.get("ends_at") is not None and datetime.fromisoformat(params["ends_at"]) <= _now():
        result.setdefault("warnings", []).append("Rule end time has already passed; it is lapsed.")
    if (notice := _discarded_weights(policy, params)) is not None:
        result.setdefault("warnings", []).append(notice)


def _collect_rule_words(row: dict[str, Any], missing: set[str], found: dict[str, str]) -> None:
    snapshot = json.loads(cast(str, row["note"]))
    reason = snapshot.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return
    previous = {item["id"]: item for item in snapshot["previous_rules"]}
    following = {item["id"]: item for item in snapshot["next_rules"]}
    for identifier in tuple(missing):
        if identifier in following and previous.get(identifier) != following[identifier]:
            found[identifier] = reason
            missing.remove(identifier)


def _rule_words(state: StateManagementInterface, rules: tuple[CellRule, ...]) -> dict[str, str]:
    """Recover user words for rules created before words were stored on rules.

    Policy history is ordered newest first by timestamp and stable identifier.
    The composite cursor continues through every page, including timestamp ties.
    Compare each rule's own content across snapshots: an unrelated policy edit
    is not a new explanation for an unchanged rule. An absent previous rule
    makes its introduction a content change. A rule with no matching event
    remains absent from this result so the caller can disclose that gap.
    Current rules that already carry words require no history traversal.
    """
    missing = {rule.id for rule in rules if rule.words is None}
    found: dict[str, str] = {}
    after: list[object] | None = None
    while missing:
        query: dict[str, object] = {
            "table": RUNS,
            "filters": {"trigger": "policy_change"},
            "order_by": [["created_at", "desc"], ["id", "desc"]],
            "limit": 100,
        }
        if after is not None:
            query["after"] = after
        page = require_records(state.query_ordered(NS, query))
        for row in page:
            _collect_rule_words(row, missing, found)
        if len(page) < 100:
            break
        after = [page[-1]["created_at"], page[-1]["id"]]
    return found


def read_model_cell_policy(state: StateManagementInterface, *, history_limit: object = 20) -> dict[str, Any]:
    """Read policy and resolve its no-kind view at one shared instant.

    The stored cell flag is a write-time snapshot. An expiring rule can change
    the effective value without another write, so expose both values and derive
    the effective flag and its provenance from the same decision. Preference
    and lapse use the same instant to avoid inconsistent boundary results.
    The requested bounded history is independent of the legacy words search:
    limiting display history does not hide older provenance from that search.
    """
    if isinstance(history_limit, bool) or not isinstance(history_limit, int) or not 1 <= history_limit <= 100:
        raise CatalogError("parameter_invalid", "history_limit must be 1..100.")
    policy = read_policy(state)
    moment = _now()
    history = require_records(
        state.query_ordered(
            NS,
            {
                "table": RUNS,
                "filters": {"trigger": "policy_change"},
                "order_by": [["created_at", "desc"], ["id", "desc"]],
                "limit": history_limit,
            },
        )
    )
    words = _rule_words(state, policy.rules)
    rules = [
        {
            **_rule_dict(rule),
            "kind": rule.kind,
            "words": rule.words or words.get(rule.id, "no words were recorded for this rule"),
            "ends_at": rule.ends_at,
            "lapsed": _lapsed(rule, moment),
        }
        for rule in policy.rules
    ]
    cells = []
    for row in read_cells_page(state, ceiling=None).rows:
        enabled, provenance = decision(policy, row, None, moment)
        weight, weight_rule = preference(policy, row, None, moment)
        cells.append(
            {
                **row,
                "stored_enabled": row.get("enabled", True),
                "enabled": enabled,
                "enabled_provenance": provenance,
                "preference_weight": weight,
                "preference_provenance": DEFAULT_WEIGHT_PROVENANCE if weight_rule is None else weight_rule,
            }
        )
    result: dict[str, Any] = {
        "rules": rules,
        "updated_at": policy.updated_at,
        "cells": cells,
        "history": history,
    }
    if policy.updated_at is None:
        result["warnings"] = ["Policy uninitialized; explicit default all-on applies. Initial create uses expected_updated_at=null."]
    return result
