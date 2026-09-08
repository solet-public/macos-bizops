"""Frozen request/result protocol for the stdlib bootstrap adapter."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    PROBE_TIMEOUT_SECONDS,
    AdapterError,
    AdapterRequestError,
    AdapterRuntime,
)

Request = dict[str, Any]
Result = dict[str, Any]
Executor = Callable[[object], Result]

REQUEST_KEYS = {
    "protocol_version",
    "kind",
    "request_id",
    "operation_id",
    "operation_ref",
    "phase",
    "probe_purpose",
    "attempt",
    "name",
    "target",
    "flow_id",
    "flow_source_revision",
    "answers_fingerprint",
    "approval_fingerprint",
    "dry_run",
    "timeout_seconds",
    "public_inputs",
}
PROBE_PURPOSES = {
    "preview",
    "pre_apply",
    "post_apply",
    "completion",
    "decision_discovery",
    "decision_qualification",
    "stage_entry",
    "stage_exit",
}
EMPTY_ACTION_PURPOSES = {
    "post_apply",
    "completion",
    "decision_discovery",
    "decision_qualification",
    "stage_entry",
    "stage_exit",
}

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_CALLABLE_REF = re.compile(r"^[a-z][a-z0-9_]*::[a-z][a-z0-9_.]*$")
_INPUT_KEY = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SECRET_FIELD = re.compile(
    r"password|secret|token|credential|private_key|oauth_code",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _closed_mapping(raw: object) -> Request:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise AdapterRequestError("adapter stdin must contain one JSON object")
    if set(raw) != REQUEST_KEYS:
        raise AdapterRequestError("request does not match the closed v1 field set")
    return raw


def _validate_protocol_identity(request: Request) -> None:
    if request["protocol_version"] != 1 or request["kind"] != "operation_request":
        raise AdapterRequestError("request protocol identity is invalid")
    try:
        uuid.UUID(str(request["request_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AdapterRequestError("request_id must be a UUID") from exc
    operation_id = request["operation_id"]
    operation_ref = request["operation_ref"]
    if not isinstance(operation_id, str) or _IDENTIFIER.fullmatch(operation_id) is None:
        raise AdapterRequestError("operation_id does not match the closed grammar")
    if not isinstance(operation_ref, str) or _CALLABLE_REF.fullmatch(operation_ref) is None:
        raise AdapterRequestError("operation_ref does not match the closed grammar")


def _validate_name_and_target(request: Request) -> None:
    name = request["name"]
    target = request["target"]
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise AdapterRequestError("name does not match the closed envelope grammar")
    if not isinstance(target, str) or len(target) < 2 or not Path(target).is_absolute():
        raise AdapterRequestError("target must be an absolute path")


def _validate_flow_identity(request: Request) -> None:
    revision = request["flow_source_revision"]
    answers = request["answers_fingerprint"]
    if request["flow_id"] != "macos.repository_setup":
        raise AdapterRequestError("flow_id is invalid")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise AdapterRequestError("flow_source_revision is invalid")
    if not isinstance(answers, str) or _FINGERPRINT.fullmatch(answers) is None:
        raise AdapterRequestError("answers_fingerprint is invalid")


def _validate_bounds(request: Request) -> None:
    attempt = request["attempt"]
    timeout = request["timeout_seconds"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise AdapterRequestError("attempt must be a positive integer")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 900:
        raise AdapterRequestError("timeout_seconds is outside the closed bound")


def _validate_public_value(value: object, label: str) -> None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        if len(value) != len(set(value)):
            raise AdapterRequestError(f"{label} contains duplicate array values")
        return
    raise AdapterRequestError(f"{label} is not a closed public value")


def _validate_public_inputs(request: Request) -> None:
    inputs = request["public_inputs"]
    if not isinstance(inputs, dict) or not all(isinstance(key, str) for key in inputs):
        raise AdapterRequestError("public_inputs must be one object")
    for key, value in inputs.items():
        if _INPUT_KEY.fullmatch(key) is None or _SECRET_FIELD.search(key):
            raise AdapterRequestError("public_inputs contains a forbidden field")
        _validate_public_value(value, f"public input {key!r}")


def _validate_phase(request: Request) -> None:
    phase = request["phase"]
    purpose = request["probe_purpose"]
    approval = request["approval_fingerprint"]
    dry_run = request["dry_run"]
    if phase == "probe":
        if purpose not in PROBE_PURPOSES or dry_run is not True or approval is not None:
            raise AdapterRequestError(
                "probe request requires a declared purpose, dry_run true, and null approval",
            )
        return
    if phase != "apply":
        raise AdapterRequestError("phase must be probe or apply")
    if purpose is not None or dry_run is not False:
        raise AdapterRequestError("apply request requires null purpose and dry_run false")
    if not isinstance(approval, str) or _FINGERPRINT.fullmatch(approval) is None:
        raise AdapterRequestError("apply request requires a well-formed approval fingerprint")


def validate_request(raw: object) -> Request:
    request = _closed_mapping(raw)
    _validate_protocol_identity(request)
    _validate_name_and_target(request)
    _validate_flow_identity(request)
    _validate_bounds(request)
    _validate_public_inputs(request)
    _validate_phase(request)
    return request


def captured_at(runtime: AdapterRuntime) -> str:
    value = runtime.now()
    if value.tzinfo is None:
        raise AdapterError("adapter clock returned a naive datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def evidence(
    runtime: AdapterRuntime,
    *,
    evidence_id: str,
    kind: str,
    status: str,
    summary: str,
    observed: bool | int | float | str | list[str] | None,
    expected: bool | int | float | str | list[str] | None,
    source: str,
) -> dict[str, Any]:
    canonical = json.dumps(observed, sort_keys=True, separators=(",", ":"))
    return {
        "id": evidence_id,
        "kind": kind,
        "status": status,
        "summary": summary[:512],
        "observed": observed,
        "expected": expected,
        "source": source[:512],
        "digest": f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        "captured_at": captured_at(runtime),
        "sensitivity": "public",
    }


def result(
    request: Mapping[str, Any],
    *,
    status: str,
    error_kind: str | None = None,
    retry_safe: bool = True,
    planned_actions: Sequence[dict[str, Any]] = (),
    evidence_items: Sequence[dict[str, Any]] = (),
    reason: dict[str, Any] | None = None,
    repair: str | None = None,
    duration_ms: int = 0,
) -> Result:
    purpose = request.get("probe_purpose")
    actions = list(planned_actions)
    if request.get("phase") == "apply" or purpose in EMPTY_ACTION_PURPOSES:
        actions = []
    error_status = status in {"awaiting_user", "blocked", "failed"}
    if error_status != (error_kind is not None):
        raise AdapterError("checkpoint status and error_kind disagree")
    return {
        "protocol_version": 1,
        "kind": "operation_result",
        "request_id": request["request_id"],
        "operation_id": request["operation_id"],
        "phase": request["phase"],
        "probe_purpose": purpose,
        "checkpoint_status": status,
        "error_kind": error_kind,
        "retry_safe": retry_safe,
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": max(0, duration_ms),
        "stdout": "",
        "stderr": "",
        "planned_actions": actions,
        "discovered_candidates": [],
        "evidence": list(evidence_items),
        "reason": reason,
        "repair": repair,
    }


def protocol_error_result(raw: object, message: str) -> Result:
    if not isinstance(raw, dict):
        raise AdapterRequestError(message)
    required_echo = {"request_id", "operation_id", "phase", "probe_purpose"}
    if not required_echo.issubset(raw) or raw.get("phase") not in {"probe", "apply"}:
        raise AdapterRequestError(message)
    request = dict(raw)
    if request["phase"] == "apply":
        request["probe_purpose"] = None
    return result(
        request,
        status="blocked",
        error_kind="adapter_protocol_error",
        retry_safe=False,
        repair="Correct the closed adapter request and retry.",
    )


def planned_action(
    action_id: str,
    title: str,
    mutation_kind: str,
    target: str,
    evidence_ref: str,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "title": title,
        "mutation_kind": mutation_kind,
        "target": target,
        "requires_confirmation": True,
        "condition_or_evidence_ref": evidence_ref,
    }


def run_public(
    runtime: AdapterRuntime,
    command: list[str],
    *,
    timeout: int = PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return runtime.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


_HOMEBREW_CANDIDATES = (
    "/opt/homebrew/bin/brew",
    "/usr/local/bin/brew",
)


def _brew_candidate_works(runtime: AdapterRuntime, candidate: str) -> bool:
    if not Path(candidate).is_absolute():
        return False
    completed = run_public(runtime, [candidate, "--version"])
    return completed is not None and completed.returncode == 0


def resolve_brew_executable(runtime: AdapterRuntime) -> str | None:
    """Resolve Homebrew through PATH first, then validated standard locations."""
    candidates = (runtime.which("brew"), *_HOMEBREW_CANDIDATES)
    for candidate in dict.fromkeys(item for item in candidates if item is not None):
        if _brew_candidate_works(runtime, candidate):
            return candidate
    return None


def operation_adapter_main(executor: Executor) -> int:
    """Read one UTF-8 request and emit exactly one compact JSON result."""

    try:
        raw_bytes = sys.stdin.buffer.read()
        raw_text = raw_bytes.decode("utf-8")
        decoder = json.JSONDecoder()
        raw, end = decoder.raw_decode(raw_text)
        if raw_text[end:].strip():
            raise AdapterRequestError("adapter stdin contains more than one JSON value")
        with contextlib.redirect_stdout(io.StringIO()):
            adapter_result = executor(raw)
    except (UnicodeError, json.JSONDecodeError, AdapterRequestError):
        print("adapter request was not one valid closed UTF-8 JSON object", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(adapter_result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0
