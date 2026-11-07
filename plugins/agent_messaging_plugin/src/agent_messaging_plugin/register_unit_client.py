"""Registered Unit and immutable assignment boundary; never mint from a file.

The Project Solet CLI owns register access. Brief bytes come from stdout and
registered provenance from stderr, exactly as ``brief show[-revision]`` emit
it. Neither paths in provenance nor old dispatch rows are launch addresses.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

PSOLET_CLI_ENV = "PROJECT_SOLET_CLI"
DEFAULT_PSOLET_CLI = str(Path.home() / "Workspace" / "project-solet" / ".venv" / "bin" / "psolet")
REGISTER_TIMEOUT_SECONDS = 60.0
CONFIG_PSOLET_CLI = "psolet_cli"
UNIT_DISPATCHABLE_STATES = frozenset({"dispatched", "working"})
LINK_EVENT_SCHEMA = "managed-dispatch-link/v2"
ASSIGNMENT_SCHEMA = "registered-unit-assignment/v1"
SPAWN_OWNER_SCHEMA = "native-spawn-owner/v1"


class VerbError(Exception):
    """Shared lifecycle refusal, independent of any file snapshot mechanism."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class RegisterUnitError(RuntimeError):
    """A named register refusal or unavailable/inconsistent read."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class RegisterActor:
    agent_instance_id: str
    agent_session_id: str
    role_name: str
    lane_id: str


@dataclass(frozen=True, slots=True)
class UnitReadback:
    unit_id: str
    unit_key: str
    kind: str
    repository_id: str
    state: str
    is_deleted: bool
    revision_id: str


@dataclass(frozen=True, slots=True)
class RegisteredRevision:
    revision_id: str
    sha256: str
    byte_length: int
    text: str

    def verify(self) -> None:
        raw = self.text.encode("utf-8")
        if not self.revision_id or not raw:
            raise RegisterUnitError("unit_revision_missing", "Registered revision identity and body are required.")
        if len(raw) != self.byte_length or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise RegisterUnitError("unit_revision_mismatch", "Registered revision bytes disagree with their provenance.")


@dataclass(frozen=True, slots=True)
class UnitAssignment:
    """Exact registered revision frozen at dispatch; corrected by a new dispatch."""

    unit_id: str
    unit_key: str
    repository_id: str
    revision: RegisteredRevision

    def to_record(self) -> dict[str, Any]:
        self.revision.verify()
        if not all((self.unit_id, self.unit_key, self.repository_id)):
            raise RegisterUnitError("unit_assignment_invalid", "Assignment identity is incomplete.")
        return {"schema": ASSIGNMENT_SCHEMA, **asdict(self)}

    @property
    def sha256(self) -> str:
        encoded = json.dumps(self.to_record(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    return value if isinstance(value, str) else ""


def assignment_from_record(row: Mapping[str, Any]) -> UnitAssignment:
    """Decode a verified current assignment; old path rows are read-only history."""
    value = row.get("assignment")
    if not isinstance(value, Mapping) or value.get("schema") != ASSIGNMENT_SCHEMA:
        raise RegisterUnitError("unit_assignment_historical", "This row has no registered assignment and is read-only history.")
    revision = value.get("revision")
    if not isinstance(revision, Mapping) or type(revision.get("byte_length")) is not int:
        raise RegisterUnitError("unit_assignment_invalid", "Assignment revision provenance is incomplete.")
    assignment = UnitAssignment(
        unit_id=_text(value, "unit_id"), unit_key=_text(value, "unit_key"),
        repository_id=_text(value, "repository_id"),
        revision=RegisteredRevision(
            revision_id=_text(revision, "revision_id"), sha256=_text(revision, "sha256"),
            byte_length=revision["byte_length"], text=_text(revision, "text"),
        ),
    )
    if assignment.sha256 != row.get("assignment_sha256") or assignment.unit_id != row.get("unit_id"):
        raise RegisterUnitError("unit_assignment_mismatch", "Stored assignment digest or Unit identity disagrees.")
    return assignment


def render_assignment(assignment: UnitAssignment) -> str:
    """Render the pinned registered body for package 2's host first turn."""
    assignment.to_record()
    revision = assignment.revision
    return (
        "\n\n=== REGISTERED UNIT ASSIGNMENT BEGIN ===\n"
        f"unit_id: {assignment.unit_id}\nrepository_id: {assignment.repository_id}\n"
        f"revision_id: {revision.revision_id}\nassignment_sha256: {assignment.sha256}\n"
        f"revision_sha256: {revision.sha256}\nbytes: {revision.byte_length}\n"
        "=== REGISTERED BRIEF CONTENT BEGIN ===\n"
        f"{revision.text}\n=== REGISTERED UNIT ASSIGNMENT END ==="
    )


class RegisterUnitClient(Protocol):
    def resolve_repository(self, root: Path) -> str: ...
    def show_unit(self, unit_id: str) -> UnitReadback: ...
    def read_brief(self, unit: UnitReadback) -> RegisteredRevision: ...
    def read_revision(self, revision_id: str) -> RegisteredRevision: ...
    def record_unit_event(self, unit_id: str, detail: Mapping[str, Any], actor: RegisterActor) -> None: ...
    def prepare_spawn_owner(self, detail: Mapping[str, Any]) -> dict[str, Any]: ...
    def transfer_spawn_owner(self, receipt: Mapping[str, Any], actor: RegisterActor) -> dict[str, Any]: ...


def resolve_psolet_cli(configured: object) -> str:
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return os.environ.get(PSOLET_CLI_ENV, "").strip() or DEFAULT_PSOLET_CLI


def _current_state(events: object) -> str:
    if not isinstance(events, list):
        return ""
    rows = [event for event in events if isinstance(event, Mapping) and not event.get("is_deleted")]
    if not rows:
        return ""
    latest = max(rows, key=lambda event: (str(event.get("observed_at")), str(event.get("created_at"))))
    return _text(latest, "to_state")


def parse_unit_show(unit_id: str, payload: object) -> UnitReadback:
    units = payload.get("unit") if isinstance(payload, Mapping) else None
    if not isinstance(units, list) or len(units) != 1 or not isinstance(units[0], Mapping):
        raise RegisterUnitError("unit_not_found", f"Register has no single Unit row for {unit_id}.")
    unit: Mapping[str, Any] = units[0]
    return UnitReadback(
        unit_id=_text(unit, "id"), unit_key=_text(unit, "unit_key"), kind=_text(unit, "kind"),
        repository_id=_text(unit, "repository_id"),
        state=_current_state(payload.get("unit_state_event") if isinstance(payload, Mapping) else None),
        is_deleted=bool(unit.get("is_deleted")), revision_id=_text(unit, "brief_document_revision_id"),
    )


def _json_object(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RegisterUnitError("unit_register_invalid", f"psolet {label} printed invalid JSON.") from exc
    if not isinstance(value, Mapping):
        raise RegisterUnitError("unit_register_invalid", f"psolet {label} did not print an object.")
    return value


def _revision(stdout: bytes, provenance: Mapping[str, Any], revision_id: str) -> RegisteredRevision:
    if provenance.get("revision_id") != revision_id:
        raise RegisterUnitError("unit_revision_stale", "The returned revision differs from the Unit binding.")
    length = provenance.get("byte_length")
    if type(length) is not int:
        raise RegisterUnitError("unit_register_invalid", "Revision byte_length must be an integer.")
    try:
        text = stdout.decode("utf-8")
    except UnicodeError as exc:
        raise RegisterUnitError("unit_register_invalid", "Registered brief is not UTF-8.") from exc
    revision = RegisteredRevision(revision_id, _text(provenance, "sha256"), length, text)
    revision.verify()
    return revision


class PsoletRegisterUnitClient:
    """CLI read boundary plus append-only attempt observations; no Unit mint."""

    def __init__(self, psolet: str, *, solet_name: str, timeout_seconds: float = REGISTER_TIMEOUT_SECONDS) -> None:
        self._psolet = psolet
        self._solet_name = solet_name
        self._timeout = timeout_seconds

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        label = " ".join(arguments[:3])
        try:
            completed = subprocess.run(
                (self._psolet, *arguments), capture_output=True, check=False, timeout=self._timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RegisterUnitError("unit_register_unavailable", f"psolet {label} could not complete: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
            missing_unit = (
                len(arguments) == 4 and arguments[:3] == ("db", "unit", "show")
                and detail == f"project-solet: <unit_id>: unit prefix {arguments[3]!r} resolves to zero records; candidates: none"
            )
            code = "unit_not_found" if missing_unit else "unit_register_unavailable"
            raise RegisterUnitError(code, f"psolet {label} exited {completed.returncode}: {detail}")
        return completed

    def resolve_repository(self, root: Path) -> str:
        payload = _json_object(self._run("db", "repository", "resolve", str(root)).stdout, "repository resolve")
        repository_id = _text(payload, "repository_id")
        if not repository_id:
            raise RegisterUnitError("unit_repository_unresolved", f"No registered repository for {root}.")
        return repository_id

    def show_unit(self, unit_id: str) -> UnitReadback:
        return parse_unit_show(unit_id, _json_object(self._run("db", "unit", "show", unit_id).stdout, "unit show"))

    def read_brief(self, unit: UnitReadback) -> RegisteredRevision:
        result = self._run("brief", "show", unit.unit_id)
        metadata = _json_object(result.stderr, "brief show provenance")
        if metadata.get("unit_id") != unit.unit_id or metadata.get("unit_key") != unit.unit_key:
            raise RegisterUnitError("unit_identity_mismatch", "Brief provenance names a different Unit.")
        return _revision(result.stdout, metadata, unit.revision_id)

    def read_revision(self, revision_id: str) -> RegisteredRevision:
        result = self._run("brief", "show-revision", "--revision-id", revision_id)
        return _revision(result.stdout, _json_object(result.stderr, "brief show-revision provenance"), revision_id)

    def record_unit_event(self, unit_id: str, detail: Mapping[str, Any], actor: RegisterActor) -> None:
        pairs = (
            ("--solet", self._solet_name), ("--agent-instance-id", actor.agent_instance_id),
            ("--agent-session-id", actor.agent_session_id), ("--role-name", actor.role_name),
            ("--agent-id", f"{self._solet_name}_platform"),
            ("--session-name", "dispatch_managed_work"), ("--lane-id", actor.lane_id),
        )
        self._run(
            "db", "unit", "record-event", unit_id, "--event-kind", "observation",
            "--detail", json.dumps({"schema": LINK_EVENT_SCHEMA, **detail}, sort_keys=True),
            *(item for flag, value in pairs if value for item in (flag, value)),
        )

    def _owner_snapshot(self, unit_id: str) -> tuple[list[Mapping[str, Any]], int, Mapping[str, Any]]:
        events: list[Mapping[str, Any]] = []
        cursor = ""
        while True:
            page = _json_object(self._run(
                "db", "unit", "events", unit_id, "--limit", "100",
                *(("--cursor", cursor) if cursor else ()),
            ).stdout, "unit events")
            events.extend(page["records"])
            if not page.get("has_more"):
                break
            cursor = _text(page, "next_cursor")
            if not cursor:
                raise RegisterUnitError("unit_register_invalid", "Owner history page has no continuation cursor.")
        payload = _json_object(self._run("db", "unit", "show", unit_id).stdout, "unit show")
        return events, int(page["frontier_ordinal"]), payload

    def prepare_spawn_owner(self, detail: Mapping[str, Any]) -> dict[str, Any]:
        """Observe the future host and immutable predecessor; never attest uptake."""
        unit_id = _text(detail, "unit_id")
        events, _, payload = self._owner_snapshot(unit_id)
        identity = {"schema": SPAWN_OWNER_SCHEMA, **detail}
        matching = [event for event in events if self._matches_spawn_identity(event, identity)]
        if matching:
            event_id = _text(matching[0], "event_id")
        else:
            result = _json_object(self._run(
                "db", "unit", "record-event", unit_id, "--event-kind", "observation",
                "--detail", json.dumps({**identity, "predecessor_owner_actor_ids": self._active_owner_actor_ids(payload)}, sort_keys=True),
                "--solet", self._solet_name,
                "--agent-instance-id", _text(detail, "agent_instance_id"),
                "--agent-session-id", f"ases-{_text(detail, 'agent_instance_id')}",
                "--session-name", _text(detail, "local_name"),
                "--lane-id", _text(detail, "lane_id"),
                "--agent-id", _text(detail, "agent_runtime"),
                "--actor-model", _text(detail, "model"),
                "--actor-effort", _text(detail, "effort"),
            ).stdout, "unit spawn observation")
            event_id = _text(result, "unit_event_id")
        observed = _json_object(self._run("db", "unit", "event-show", event_id).stdout, "unit event-show")
        owner = _text(observed, "observed_by_actor_id")
        if observed.get("unit_id") != unit_id or not owner:
            raise RegisterUnitError("spawn_owner_identity_invalid", "Spawn identity observation has no matching actor/Unit.")
        predecessor = self._prepared_predecessor(observed)
        _, version, _ = self._owner_snapshot(unit_id)
        return {"schema": SPAWN_OWNER_SCHEMA, **detail, "owner_actor_id": owner,
                "identity_event_id": event_id, "expected_history_version": version,
                "predecessor_owner_actor_ids": predecessor}

    def transfer_spawn_owner(self, receipt: Mapping[str, Any], actor: RegisterActor) -> dict[str, Any]:
        """Use fresh explicit CAS only against the originally prepared predecessor."""
        unit_id = _text(receipt, "unit_id")
        events, version, payload = self._owner_snapshot(unit_id)
        owner = _text(receipt, "owner_actor_id")
        self._require_current_spawn_attempt(events, receipt)
        if self._spawn_owner_is_current(payload, owner):
            return {**receipt, "ownership_status": "already_assigned", "history_version": version}
        observed = next((event for event in events if event.get("event_id") == receipt.get("identity_event_id")), {})
        predecessor = self._prepared_predecessor(observed)
        if (observed.get("observed_by_actor_id") != owner
                or observed.get("unit_id") != unit_id
                or receipt.get("predecessor_owner_actor_ids") != predecessor):
            raise RegisterUnitError("spawn_owner_identity_invalid", "Receipt differs from its prepared predecessor evidence.")
        if self._active_owner_actor_ids(payload) != predecessor:
            raise RegisterUnitError("spawn_owner_predecessor_changed", "The prepared predecessor no longer owns this Unit.")
        result = _json_object(self._run(
            "db", "unit", "set-owner", unit_id, "--actor-id", owner,
            "--expected-history-version", str(version),
            "--reason", "native spawned lane ownership before worker first turn",
            "--evidence", json.dumps(dict(receipt), sort_keys=True),
            "--solet", self._solet_name, "--agent-instance-id", actor.agent_instance_id,
            "--agent-session-id", actor.agent_session_id,
            "--session-name", "native_spawn_owner", "--agent-id", f"{self._solet_name}_platform",
            "--lane-id", actor.lane_id,
            *(("--role-name", actor.role_name) if actor.role_name else ()),
        ).stdout, "unit set-owner")
        if result.get("owner_actor_id") != owner:
            raise RegisterUnitError("spawn_owner_transfer_invalid", "Set-owner receipt names a different spawned actor.")
        return {**receipt, **result, "ownership_status": "assigned"}

    @staticmethod
    def _active_owner_actor_ids(payload: Mapping[str, Any]) -> list[str]:
        return sorted(str(row["actor_id"]) for row in payload.get("unit_assignment", [])
                      if row.get("assignment_kind") == "owner" and not row.get("released_at") and not row.get("is_deleted"))

    @staticmethod
    def _matches_spawn_identity(event: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
        try:
            stored = json.loads(str(event.get("detail") or ""))
        except ValueError:
            return False
        return isinstance(stored, dict) and all(stored.get(key) == value for key, value in identity.items())

    @staticmethod
    def _prepared_predecessor(event: Mapping[str, Any]) -> list[str]:
        try:
            stored = json.loads(str(event.get("detail") or ""))
        except ValueError as exc:
            raise RegisterUnitError("spawn_owner_identity_invalid", "Prepared identity evidence is invalid.") from exc
        predecessor = stored.get("predecessor_owner_actor_ids") if isinstance(stored, dict) else None
        if (not isinstance(predecessor, list) or any(not isinstance(owner, str) or not owner for owner in predecessor)
                or predecessor != sorted(set(predecessor))):
            raise RegisterUnitError("spawn_owner_identity_invalid", "Prepared identity has no canonical predecessor evidence; explicit recovery required.")
        return predecessor

    @staticmethod
    def _spawn_owner_is_current(payload: Mapping[str, Any], owner: str) -> bool:
        assignments = payload.get("unit_assignment", [])
        current = [row["actor_id"] for row in assignments if row.get("assignment_kind") == "owner"
                   and not row.get("released_at") and not row.get("is_deleted")]
        if current == [owner]:
            return True
        if any(row.get("actor_id") == owner and row.get("assignment_kind") == "owner" for row in assignments):
            raise RegisterUnitError("spawn_owner_superseded", "This spawned actor previously owned the Unit; delayed replay cannot reclaim it.")
        return False

    @staticmethod
    def _require_current_spawn_attempt(events: list[Mapping[str, Any]], receipt: Mapping[str, Any]) -> None:
        for event in events:
            try:
                detail = json.loads(str(event.get("detail") or ""))
            except ValueError:
                continue
            if not isinstance(detail, dict) or detail.get("schema") != SPAWN_OWNER_SCHEMA:
                continue
            if (receipt.get("dispatch_id") and detail.get("dispatch_id") == receipt.get("dispatch_id")
                    and int(detail.get("attempt_number", 0)) > int(receipt.get("attempt_number", 0))):
                raise RegisterUnitError("spawn_owner_superseded", "A newer dispatch attempt has been observed; delayed replay cannot assign ownership.")

def owner_transfer_pending(row: Mapping[str, Any]) -> bool:
    """A failed ownership write retained its host, so recovery is the same attempt."""
    failure = row.get("attempt_failure")
    return (row.get("state") == "failed_start" and isinstance(failure, Mapping)
            and failure.get("code") == "spawn_owner_transfer_failed")
