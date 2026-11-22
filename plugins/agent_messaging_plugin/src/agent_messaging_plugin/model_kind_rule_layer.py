"""Evaluate ordered model-cell rules for one request before capability selection."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .dispatch_tier_selection import CapabilityCell, TierSelectionError
from .model_capability_store import CatalogError, _cell_key, read_cells_page
from .model_capability_verbs import _ChooseCell, _select_from_rows
from .model_cell_enablement import CellPolicy, CellRule, _applies, _rule_words, decision, matches, preference, read_policy
from .schema import CELL_ACCEPTANCE_ACCEPTED

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_UNAVAILABLE = frozenset({"catalog_empty", "catalog_unaccepted", "catalog_unknown_effort", "catalog_ineligible", "pin_cell_unavailable"})


class NoRuleServesError(CatalogError):
    """An applicable enablement rule removed the last otherwise selectable cell."""

    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = evidence
        super().__init__("no_rule_serves", f"No rule serves this request. Rule {evidence['rule_id']}: {evidence['words']}")


def _kind_word(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise CatalogError("parameter_invalid", "kind takes one nonempty word with no whitespace, or null.")
    return value


def _rule_evidence(state: StateManagementInterface, kind: str | None, rule: CellRule | None) -> dict[str, Any]:
    if rule is None:
        return {"kind": kind, "rule_id": None, "words": None}
    words = rule.words or _rule_words(state, (rule,)).get(rule.id, "no words were recorded for this rule")
    return {"kind": kind, "rule_id": rule.id, "words": words}


def _would_serve(cell: CapabilityCell, choose: _ChooseCell) -> bool:
    """Test only the enablement bit against the original score, pin and constraints."""
    try:
        choose((replace(cell, enabled=True),))
    except TierSelectionError as exc:
        if exc.code not in _UNAVAILABLE:
            raise
        return False
    return True


def _last_removal(policy: CellPolicy, kind: str | None, now: datetime, candidates: list[dict[str, Any]]) -> CellRule | None:
    """Find the transition to empty. A redundant off or a weight cannot claim it."""
    enabled = set(range(len(candidates)))
    deciding = None
    for rule in policy.rules:
        if rule.enabled is None or not _applies(rule, kind, now):
            continue
        matching = (index for index, row in enumerate(candidates) if matches(rule.selector, row))
        if rule.enabled:
            enabled.update(matching)
        elif enabled:
            enabled.difference_update(matching)
            if not enabled:
                deciding = rule
    return None if enabled else deciding


def select_model_cell_for_kind(state: StateManagementInterface, params: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Select from this call's policy view and return the enablement grant for the chosen cell."""
    narrowing = dict(params)
    kind = _kind_word(narrowing.pop("kind", None))
    moment = now or datetime.now(UTC)
    policy = read_policy(state)
    read = read_cells_page(state, ceiling=None)
    accepted = {_cell_key(row): row for row in read.rows if row.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED}
    enabled_by: dict[tuple[str, str, str], str] = {}
    for key, row in accepted.items():
        # read_cells_page returns detached row dictionaries. The stored global flag
        # continues to govern the direct selector and generic spawn.
        row["enabled"], enabled_by[key] = decision(policy, row, kind, moment)

    def explain_refusal(error: TierSelectionError, catalog: tuple[CapabilityCell, ...], choose: _ChooseCell) -> None:
        if error.code not in _UNAVAILABLE:
            return
        candidates = [accepted[(cell.runtime, cell.model, cell.effort)] for cell in catalog if cell.acceptance == CELL_ACCEPTANCE_ACCEPTED and not cell.enabled and _would_serve(cell, choose)]
        rule = _last_removal(policy, kind, moment, candidates)
        if rule is not None:
            raise NoRuleServesError(_rule_evidence(state, kind, rule)) from error

    result = _select_from_rows(state, narrowing, now=moment, read=read, preference_for=lambda row: preference(policy, row, kind, moment), explain_refusal=explain_refusal)
    rule_id = enabled_by[_cell_key(result["selected"])]
    grant = next((rule for rule in policy.rules if rule.id == rule_id), None)
    result["grant"] = _rule_evidence(state, kind, grant)
    return result
