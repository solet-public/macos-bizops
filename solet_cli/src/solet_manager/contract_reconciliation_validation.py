"""Closed schemas for reconciliation declarations and retained retirement evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .errors import ContractError, StateError
from .models import JsonValue

if TYPE_CHECKING:
    from .contracts import ContractBundle, ContractReconciliation

_RECONCILIATION_ENTRY_KEYS = frozenset(
    {
        "migration_id",
        "source",
        "destination",
        "stage_probe_mappings",
        "first_use_inactive_probe_migrations",
        "operation_statuses_to_reset",
        "answer_value_migrations",
        "retired_decision_ids",
        "retired_public_input_ids",
    }
)


def reconciliation_entry(value: JsonValue) -> dict[str, JsonValue]:
    """Normalize one closed-schema reconciliation entry, including legacy defaults."""

    optional = {
        "operation_statuses_to_reset",
        "answer_value_migrations",
        "retired_decision_ids",
        "retired_public_input_ids",
    }
    if not isinstance(value, dict) or frozenset(value) - optional != (_RECONCILIATION_ENTRY_KEYS - optional):
        raise ContractError("contract reconciliation entry does not match closed v1 schema")
    return {
        "operation_statuses_to_reset": [],
        "answer_value_migrations": [],
        "retired_decision_ids": [],
        "retired_public_input_ids": [],
        **value,
    }


def parse_operation_statuses_to_reset(value: JsonValue) -> tuple[str, ...]:
    """Parse the declared operation ids that reconciliation invalidates."""

    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ContractError("contract reconciliation operation status resets must be an array of ids")
    values = cast(tuple[str, ...], tuple(value))
    if len(set(values)) != len(values):
        raise ContractError("contract reconciliation operation status resets must be unique")
    return values


def validate_retirement_declarations(reconciliation: ContractReconciliation, destination: ContractBundle) -> None:
    """A retirement must describe fields absent from the destination contract."""
    for ids, catalog in (
        (reconciliation.retired_decision_ids, destination.decisions),
        (reconciliation.retired_public_input_ids, destination.inputs),
    ):
        retained = sorted(set(ids) & set(catalog))
        if retained:
            raise ContractError(f"retired answers remain declared in the destination contract: {retained}")


def retired_answer_data(answers: dict[str, JsonValue], reconciliation: ContractReconciliation) -> list[JsonValue]:
    """Prior values are evidence; the ordinary receipt retains complete before-images."""
    entries: list[JsonValue] = []
    for section, ids in (
        ("decisions", reconciliation.retired_decision_ids),
        ("public_inputs", reconciliation.retired_public_input_ids),
    ):
        prior = answers.get(section)
        if isinstance(prior, dict):
            entries.extend({"section": section, "id": key, "prior_value": prior[key]} for key in ids if key in prior)
    return entries


def validate_retired_answer_data(value: JsonValue | None) -> None:
    if not isinstance(value, list):
        raise StateError("contract reconciliation retired answers must be an array")
    seen: set[tuple[str, str]] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"section", "id", "prior_value"}:
            raise StateError("contract reconciliation retired answer record is invalid")
        section, key = row["section"], row["id"]
        if not isinstance(section, str) or section not in {"decisions", "public_inputs"} or not isinstance(key, str) or not key:
            raise StateError("contract reconciliation retired answer identity is invalid")
        identity = (str(section), key)
        if identity in seen:
            raise StateError("contract reconciliation retired answer identities must be unique")
        seen.add(identity)
