"""Closed parsing for first-use inactive probe reconciliation rules."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ContractError
from .models import JsonValue

_RULE_KEYS = frozenset(
    {
        "rule_id",
        "destination",
        "resulting_status",
        "migration_reason",
        "evidence_disposition",
        "citation",
    }
)
_CITATION_KEYS = frozenset(
    {"manifest_entry_id", "source_digest", "destination_digest"}
)
_IDENTITY_KEYS = frozenset({"stage_id", "boundary", "probe_id"})


@dataclass(frozen=True)
class FirstUseInactiveProbeMigration:
    """One release-declared preservation of attempted first-use probe history."""

    rule_id: str
    destination: tuple[str, str, str]
    migration_reason: str
    manifest_entry_id: str
    source_digest: str
    destination_digest: str


def parse_first_use_inactive_probe_migrations(
    value: JsonValue,
    *,
    migration_id: str,
    source_digest: str,
    destination_digest: str,
) -> tuple[FirstUseInactiveProbeMigration, ...]:
    """Parse rules and bind every citation to the enclosing manifest entry."""

    if not isinstance(value, list):
        raise ContractError(
            "contract reconciliation first_use_inactive_probe_migrations must be an array"
        )
    rules = tuple(_parse_rule(item) for item in value)
    _validate_unique_rules(rules)
    for rule in rules:
        if (
            rule.manifest_entry_id != migration_id
            or rule.source_digest != source_digest
            or rule.destination_digest != destination_digest
        ):
            raise ContractError(
                "contract reconciliation first-use migration citation does not match its entry"
            )
    return rules


def _parse_rule(value: JsonValue) -> FirstUseInactiveProbeMigration:
    if not isinstance(value, dict) or frozenset(value) != _RULE_KEYS:
        raise ContractError("contract reconciliation first-use migration rule is invalid")
    rule_id = _required_string(value, "rule_id", "rule")
    reason = _required_string(value, "migration_reason", "reason")
    _validate_rule_values(value)
    citation = _parse_citation(value["citation"])
    return FirstUseInactiveProbeMigration(
        rule_id=rule_id,
        destination=_parse_identity(value["destination"]),
        migration_reason=reason,
        manifest_entry_id=citation[0],
        source_digest=citation[1],
        destination_digest=citation[2],
    )


def _validate_rule_values(value: dict[str, JsonValue]) -> None:
    if value["resulting_status"] != "not_applicable":
        raise ContractError("contract reconciliation first-use migration status is invalid")
    if value["evidence_disposition"] != "preserve":
        raise ContractError("contract reconciliation first-use migration evidence disposition is invalid")


def _parse_citation(value: JsonValue) -> tuple[str, str, str]:
    if not isinstance(value, dict) or frozenset(value) != _CITATION_KEYS:
        raise ContractError("contract reconciliation first-use migration citation is invalid")
    return (
        _required_string(value, "manifest_entry_id", "citation"),
        _required_string(value, "source_digest", "citation"),
        _required_string(value, "destination_digest", "citation"),
    )


def _parse_identity(value: JsonValue) -> tuple[str, str, str]:
    if not isinstance(value, dict) or frozenset(value) != _IDENTITY_KEYS:
        raise ContractError("contract reconciliation stage-probe identity is invalid")
    stage_id = _required_string(value, "stage_id", "stage-probe identity")
    boundary = value["boundary"]
    probe_id = _required_string(value, "probe_id", "stage-probe identity")
    if boundary not in {"entry", "exit"}:
        raise ContractError("contract reconciliation stage-probe identity values are invalid")
    return stage_id, boundary, probe_id


def _required_string(value: dict[str, JsonValue], key: str, label: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item:
        raise ContractError(f"contract reconciliation first-use migration {label} is invalid")
    return item


def _validate_unique_rules(rules: tuple[FirstUseInactiveProbeMigration, ...]) -> None:
    if len({item.rule_id for item in rules}) != len(rules):
        raise ContractError("contract reconciliation first-use migration rule ids must be unique")
    if len({item.destination for item in rules}) != len(rules):
        raise ContractError(
            "contract reconciliation first-use migration destinations must be unique"
        )
