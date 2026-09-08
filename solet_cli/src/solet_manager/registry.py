"""Manager-created instance registry and unmanaged-instance discovery."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .errors import InstanceUnmanagedError, StateConflictError, StateError
from .models import SCHEMA_VERSION, InstanceRecord, JsonValue
from .state_io import atomic_write_json, load_json_object

_REGISTRY_KEYS = frozenset({"schema_version", "instances"})
_INSTANCE_KEYS = frozenset(
    {
        "name",
        "target",
        "launcher",
        "seed_repository",
        "seed_tag",
        "seed_commit",
        "seed_tree_hash",
        "profile",
        "flow_id",
        "flow_source_revision",
        "flow_contract_digest",
        "created_at",
        "updated_at",
        "lifecycle_state",
        "input_fingerprint",
        "expected_router_name",
        "expected_router_socket",
        "expected_router_port_range",
    }
)
_LEGACY_INSTANCE_KEYS = _INSTANCE_KEYS - {
    "lifecycle_state",
    "input_fingerprint",
    "expected_router_name",
    "expected_router_socket",
    "expected_router_port_range",
}
_FORMULA_PATH_MARKERS = ("/Cellar/solet/", "/homebrew/Cellar/solet/")


class InstanceRegistry:
    """Closed registry persisted as one atomic private JSON object."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def list(self) -> tuple[InstanceRecord, ...]:
        return tuple(sorted(self._read().values(), key=lambda item: item.name))

    def get(self, name: str) -> InstanceRecord | None:
        return self._read().get(name)

    def require(self, name: str, *, candidate_target: Path | None = None) -> InstanceRecord:
        record = self.get(name)
        if record is not None:
            return record
        if candidate_target is not None and candidate_target.exists():
            raise InstanceUnmanagedError(
                f"{candidate_target} exists but {name!r} is not manager-created",
                repair=(f"Resume the same reviewed transaction with 'solet create {name}'. Do not edit the registry by hand or adopt an arbitrary target."),
            )
        raise StateConflictError(f"managed instance {name!r} does not exist")

    def add(self, record: InstanceRecord) -> None:
        _reject_formula_paths(record)
        _validate_lifecycle_record(record)
        records = self._read()
        existing = records.get(record.name)
        if existing is not None and _is_provisional_upgrade(existing, record):
            records[record.name] = record
            self._write(records)
            return
        if existing is not None and existing != record:
            raise StateConflictError(f"registry already contains a different record for {record.name!r}")
        if existing == record:
            return
        records[record.name] = record
        self._write(records)

    def reconcile_contract(
        self,
        *,
        expected: InstanceRecord,
        flow_contract_digest: str,
        updated_at: str,
    ) -> InstanceRecord:
        """Compare-and-swap only a declared managed contract identity."""

        records = self._read()
        current = records.get(expected.name)
        if current != expected:
            raise StateConflictError(f"registry changed before contract reconciliation for {expected.name!r}")
        updated = self.reconciled_contract_record(
            expected,
            flow_contract_digest=flow_contract_digest,
            updated_at=updated_at,
        )
        records[updated.name] = updated
        self._write(records)
        return updated

    def reconciled_contract_record(
        self,
        expected: InstanceRecord,
        *,
        flow_contract_digest: str,
        updated_at: str,
    ) -> InstanceRecord:
        """Build the only permitted registry record identity replacement."""

        updated = replace(
            expected,
            flow_contract_digest=flow_contract_digest,
            updated_at=updated_at,
        )
        _reject_formula_paths(updated)
        _validate_lifecycle_record(updated)
        return updated

    def reconciled_identity_record(
        self,
        expected: InstanceRecord,
        *,
        flow_source_revision: str,
    ) -> InstanceRecord:
        """Build the only permitted in-field flow revision replacement."""

        if self.require(expected.name) != expected:
            raise StateConflictError(f"registry changed before identity reconciliation for {expected.name!r}")
        updated = replace(expected, flow_source_revision=flow_source_revision)
        _reject_formula_paths(updated)
        _validate_lifecycle_record(updated)
        return updated

    def _read(self) -> dict[str, InstanceRecord]:
        raw = load_json_object(self.path, missing_ok=True)
        if raw is None:
            return {}
        if frozenset(raw) != _REGISTRY_KEYS or raw.get("schema_version") != SCHEMA_VERSION:
            raise StateError(f"registry at {self.path} does not match the closed v1 schema")
        values = raw.get("instances")
        if not isinstance(values, dict):
            raise StateError(f"registry instances must be an object at {self.path}")
        parsed: dict[str, InstanceRecord] = {}
        for name, value in values.items():
            if not isinstance(value, dict):
                raise StateError(f"registry entry {name!r} is invalid")
            if frozenset(value) not in {_INSTANCE_KEYS, _LEGACY_INSTANCE_KEYS}:
                raise StateError(f"registry entry {name!r} does not match the closed v1 schema")
            try:
                record = InstanceRecord.from_dict(value)
            except (KeyError, TypeError, ValueError) as exc:
                raise StateError(f"registry entry {name!r} is incomplete: {exc}") from exc
            if record.name != name:
                raise StateError(f"registry key/name mismatch for {name!r}")
            _reject_formula_paths(record)
            _validate_lifecycle_record(record)
            parsed[name] = record
        return parsed

    def _write(self, records: dict[str, InstanceRecord]) -> None:
        payload: dict[str, JsonValue] = {
            "schema_version": SCHEMA_VERSION,
            "instances": {name: record.to_dict() for name, record in sorted(records.items())},
        }
        atomic_write_json(self.path, payload)


def _reject_formula_paths(record: InstanceRecord) -> None:
    for field_name, value in (("target", record.target), ("launcher", record.launcher)):
        if any(marker in value for marker in _FORMULA_PATH_MARKERS):
            raise StateError(f"registry {field_name} may not persist a formula-keg path: {value}")


def _validate_lifecycle_record(record: InstanceRecord) -> None:
    if record.lifecycle_state not in {"setup_incomplete", "verified"}:
        raise StateError("registry lifecycle_state is invalid")
    if record.lifecycle_state != "setup_incomplete":
        return
    if not record.input_fingerprint:
        raise StateError("setup-incomplete registry record requires an input fingerprint")
    if None in {
        record.expected_router_name,
        record.expected_router_socket,
        record.expected_router_port_range,
    }:
        raise StateError("setup-incomplete registry record requires expected router identity")


def _is_provisional_upgrade(existing: InstanceRecord, record: InstanceRecord) -> bool:
    return all(
        (
            _is_verified_transition(existing, record),
            _same_flow_identity(existing, record),
            _same_seed_identity(existing, record),
        )
    )


def _is_verified_transition(existing: InstanceRecord, record: InstanceRecord) -> bool:
    return existing.lifecycle_state == "setup_incomplete" and record.lifecycle_state == "verified" and existing.input_fingerprint == record.input_fingerprint


def _same_flow_identity(existing: InstanceRecord, record: InstanceRecord) -> bool:
    return existing.target == record.target and existing.flow_id == record.flow_id and existing.flow_source_revision == record.flow_source_revision and existing.flow_contract_digest == record.flow_contract_digest and existing.launcher == record.launcher and existing.created_at == record.created_at


def _same_seed_identity(existing: InstanceRecord, record: InstanceRecord) -> bool:
    return existing.seed_repository == record.seed_repository and existing.seed_tag == record.seed_tag and existing.seed_commit == record.seed_commit and existing.seed_tree_hash == record.seed_tree_hash and existing.profile == record.profile
