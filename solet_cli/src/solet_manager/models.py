"""Typed public and durable manager records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Self

MANAGER_VERSION = "0.1.0"
SCHEMA_VERSION = 1

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class ExitCode(IntEnum):
    """Stable CLI exit contract."""

    OK = 0
    FAILED = 1
    INVALID = 2
    HUMAN_ACTION = 3


class CheckpointStatus(StrEnum):
    """The complete canonical setup-flow checkpoint vocabulary."""

    PENDING = "pending"
    AWAITING_USER = "awaiting_user"
    CONSENTED = "consented"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFIED = "verified"
    DECLINED = "declined"
    BLOCKED = "blocked"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class TransactionStatus(StrEnum):
    """Deterministic transaction-level roll-up vocabulary."""

    PENDING = "pending"
    AWAITING_USER = "awaiting_user"
    APPLYING = "applying"
    BLOCKED = "blocked"
    FAILED = "failed"
    VERIFIED = "verified"


@dataclass(frozen=True)
class Evidence:
    """Non-secret structured observation returned by a probe or adapter."""

    id: str
    kind: str
    status: str
    summary: str
    observed: JsonValue
    expected: JsonValue
    source: str
    digest: str
    captured_at: str
    sensitivity: str = "public"

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "observed": self.observed,
            "expected": self.expected,
            "source": self.source,
            "digest": self.digest,
            "captured_at": self.captured_at,
            "sensitivity": self.sensitivity,
        }

    @classmethod
    def from_dict(cls, value: dict[str, JsonValue]) -> Self:
        return cls(
            id=str(value["id"]),
            kind=str(value["kind"]),
            status=str(value["status"]),
            summary=str(value["summary"]),
            observed=value.get("observed"),
            expected=value.get("expected"),
            source=str(value["source"]),
            digest=str(value["digest"]),
            captured_at=str(value["captured_at"]),
            sensitivity=str(value.get("sensitivity", "public")),
        )


@dataclass(frozen=True)
class CommandResult:
    """Single semantic result consumed by both human and JSON renderers."""

    kind: str
    status: str
    message: str
    exit_code: ExitCode
    error_kind: str | None = None
    repair: str | None = None
    data: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    evidence: tuple[Evidence, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "exit_code": int(self.exit_code),
            "error_kind": self.error_kind,
            "repair": self.repair,
            "data": self.data,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class InstanceRecord:
    """Non-secret manager registry entry."""

    name: str
    target: str
    launcher: str
    seed_repository: str
    seed_tag: str | None
    seed_commit: str
    seed_tree_hash: str
    profile: str
    flow_id: str
    flow_source_revision: str
    flow_contract_digest: str
    created_at: str
    updated_at: str
    lifecycle_state: str = "verified"
    input_fingerprint: str = ""
    expected_router_name: str | None = None
    expected_router_socket: str | None = None
    expected_router_port_range: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "target": self.target,
            "launcher": self.launcher,
            "seed_repository": self.seed_repository,
            "seed_tag": self.seed_tag,
            "seed_commit": self.seed_commit,
            "seed_tree_hash": self.seed_tree_hash,
            "profile": self.profile,
            "flow_id": self.flow_id,
            "flow_source_revision": self.flow_source_revision,
            "flow_contract_digest": self.flow_contract_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "lifecycle_state": self.lifecycle_state,
            "input_fingerprint": self.input_fingerprint,
            "expected_router_name": self.expected_router_name,
            "expected_router_socket": self.expected_router_socket,
            "expected_router_port_range": self.expected_router_port_range,
        }

    @classmethod
    def from_dict(cls, value: dict[str, JsonValue]) -> Self:
        seed_tag = value["seed_tag"]
        if seed_tag is not None and not isinstance(seed_tag, str):
            raise TypeError("seed_tag must be a string or null")
        lifecycle_state = _lifecycle_state(value.get("lifecycle_state", "verified"))
        input_fingerprint = _string_or_empty(value.get("input_fingerprint", ""))
        return cls(
            name=str(value["name"]),
            target=str(value["target"]),
            launcher=str(value["launcher"]),
            seed_repository=str(value["seed_repository"]),
            seed_tag=seed_tag,
            seed_commit=str(value["seed_commit"]),
            seed_tree_hash=str(value["seed_tree_hash"]),
            profile=str(value["profile"]),
            flow_id=str(value["flow_id"]),
            flow_source_revision=str(value["flow_source_revision"]),
            flow_contract_digest=str(value["flow_contract_digest"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            lifecycle_state=lifecycle_state,
            input_fingerprint=input_fingerprint,
            expected_router_name=_optional_string(value.get("expected_router_name")),
            expected_router_socket=_optional_string(value.get("expected_router_socket")),
            expected_router_port_range=_optional_string(value.get("expected_router_port_range")),
        )


def _optional_string(value: JsonValue) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional instance-record field must be a string or null")
    return value


def _string_or_empty(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise TypeError("instance-record fingerprint must be a string")
    return value


def _lifecycle_state(value: JsonValue) -> str:
    if not isinstance(value, str) or value not in {"setup_incomplete", "verified"}:
        raise TypeError("instance-record lifecycle_state must be setup_incomplete or verified")
    return value
