"""Stable manager error taxonomy."""

from __future__ import annotations


class ManagerError(RuntimeError):
    """Base class for failures with a stable public error kind."""

    error_kind = "manager_error"
    exit_code = 1

    def __init__(self, message: str, *, repair: str | None = None) -> None:
        super().__init__(message)
        self.repair = repair


class InvocationError(ManagerError):
    """Invalid command or closed configuration."""

    error_kind = "invalid_invocation"
    exit_code = 2


class ConfigError(InvocationError):
    """Configuration could not be parsed or validated."""

    error_kind = "invalid_config"


class ApprovalFingerprintRequiredError(InvocationError):
    """The --yes/fingerprint approval carrier pair is incomplete."""

    error_kind = "approval_fingerprint_required"


class ApprovalFingerprintMalformedError(InvocationError):
    """The supplied approval fingerprint does not use the closed syntax."""

    error_kind = "approval_fingerprint_malformed"


class ContractError(InvocationError):
    """A shared flow or protocol contract is invalid."""

    error_kind = "invalid_contract"


class StateError(ManagerError):
    """Durable state is unsafe or inconsistent."""

    error_kind = "corrupt_state"


class StateConflictError(ManagerError):
    """Existing durable identity conflicts with the requested operation."""

    error_kind = "state_conflict"
    exit_code = 3


class ReopenUnsafeAppliedStateError(StateConflictError):
    """A new active probe cannot safely reopen already-applied work."""

    error_kind = "reopen_unsafe_applied_state"


class VenvIncompatibleError(StateConflictError):
    """The target venv cannot import authenticated locked-seed code."""

    error_kind = "venv_incompatible"


class SourceError(ManagerError):
    """The locked seed source cannot be trusted or materialized."""

    error_kind = "source_error"


class SourceIdentityError(SourceError):
    """Fetched seed identity differs from the formula lock."""

    error_kind = "source_identity_mismatch"


class AdapterError(ManagerError):
    """Target-local adapter invocation failed."""

    error_kind = "adapter_error"


class AdapterMissingError(AdapterError):
    """No reviewed adapter implements the requested operation."""

    error_kind = "adapter_missing"
    exit_code = 3


class AdapterProtocolError(AdapterError):
    """An adapter violated the shared JSON protocol."""

    error_kind = "adapter_protocol_error"


class ProbeDriftError(ManagerError):
    """Action-driving state changed after preview approval."""

    error_kind = "probe_drift"
    exit_code = 3


class InstanceUnmanagedError(ManagerError):
    """A target exists but has no manager registry record."""

    error_kind = "instance_unmanaged"
    exit_code = 3
