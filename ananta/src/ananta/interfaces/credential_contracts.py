"""Dependency-light internal credential contracts and provider operation component."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Literal, Protocol

from ananta.core.domain.types import ActionResult
from ananta.core.services.call_context import CallContext

OperationStatus = Literal["completed", "not_found", "denied", "invalid_reference", "invalid_request", "invalid_credential", "provider_error"]
KeyEncoding = Literal["utf8", "base64"]


@dataclass(frozen=True, slots=True)
class VaultReference:
    """An existing scoped key, without its credential."""

    key: str


@dataclass(frozen=True, slots=True)
class SignRequest:
    """Sign public bytes with the referenced HMAC-SHA256 key."""

    reference: VaultReference
    message: bytes
    key_encoding: KeyEncoding = "utf8"


@dataclass(frozen=True, slots=True)
class VerifyRequest:
    """Verify a public HMAC-SHA256 signature with the current reference."""

    reference: VaultReference
    message: bytes
    signature: str
    key_encoding: KeyEncoding = "utf8"


UseRequest = SignRequest | VerifyRequest


@dataclass(frozen=True, slots=True)
class UseResult:
    """Only the intended operation outcome, never authentication configuration."""

    status: OperationStatus
    signature: str | None = None
    verified: bool | None = None


@dataclass(frozen=True, slots=True)
class MetadataRequest:
    """Read existence, owned references, or public vault status."""

    kind: Literal["exists", "list", "status"]
    reference: VaultReference | None = None
    tag: str | None = None


@dataclass(frozen=True, slots=True)
class VaultStatus:
    """Unknown ancillary status stays explicit; no raw provider object escapes."""

    initialized: bool | None
    locked: bool | None


@dataclass(frozen=True, slots=True)
class MetadataResult:
    """Approved metadata only; deliberately has no credential-value field."""

    status: OperationStatus
    exists: bool | None = None
    references: tuple[VaultReference, ...] = ()
    vault_status: VaultStatus | None = None


class CredentialProvider(Protocol):
    """Existing internal migration reads, always with server-bound authority."""

    @abstractmethod
    def retrieve(self, key: str, *, call_context: CallContext | None = None) -> ActionResult: ...

    @abstractmethod
    def exists(self, key: str, *, call_context: CallContext | None = None) -> ActionResult: ...

    @abstractmethod
    def list(self, tag: str | None = None, *, call_context: CallContext | None = None) -> ActionResult: ...

    @abstractmethod
    def status(self, *, call_context: CallContext | None = None) -> ActionResult: ...


class CredentialOperations(CredentialProvider):
    """One fixed implementation inherited by storage providers.

    Loading interface contracts must not initialize the services package.
    Implementation imports happen at the operation boundary, after provider
    construction; there is no alternate implementation or fallback path.
    """

    def use(self, request: UseRequest, *, call_context: CallContext | None = None) -> UseResult:
        """Perform a caller-authorized operation; internal, never a process."""
        from ananta.services.vault_service.credential_operations import use_credential

        return use_credential(self, request, call_context)

    def metadata(self, request: MetadataRequest, *, call_context: CallContext | None = None) -> MetadataResult:
        """Read typed public metadata without retrieving a credential."""
        from ananta.services.vault_service.credential_operations import project_metadata

        return project_metadata(self, request, call_context)
