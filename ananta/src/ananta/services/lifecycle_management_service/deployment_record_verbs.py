"""The solet's deployment verbs, as a mixin of the lifecycle service.

``record_deployment``, ``read_deployments`` and ``set_deployment_state`` make the
:mod:`deployment_records` store callable over the bridge, so deploy-by-rule, which
runs outside the solet, can record and read what is deployed. Each verb is a thin
envelope around one store operation: arguments are checked, the store call is
made, and the store's refusal (``invalid_sha``, ``stale_version``,
``unknown_deployment``) comes back as the error envelope's code, never as an
empty result.

``run_deploy_rule`` starts that rule from the solet itself, for whoever lands the
solet's repository and for a person. The rule must outlive a blue-green swap of
the colour that starts it (the new colour SIGTERMs the old one as soon as it is
active), and the serial action queue must stay free for the rule's own bridge
calls, so the verb never waits on it. It starts the rule as a detached child
that inherits an exclusive attempt lock, answers ``deferred`` / ``in_progress``
with an attempt id, and later reads that attempt from the child's own stdout
receipt. A call while an attempt runs attaches to it instead of starting another.

The store is the orchestrator's ``state_service``, reached the way the other
core services reach it. ``LifecycleManagementService`` lists this class before
``LifecycleManagementAPI`` so these concrete methods satisfy its abstract verbs.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import secrets
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ananta.core.config.environment_config import EnvironmentConfig
from ananta.core.domain.enums import ActionStatus
from ananta.core.services.call_context import CallContext
from ananta.services.lifecycle_management_service import deployment_records

logger = logging.getLogger(__name__)

_ROW_FIELDS = (
    "deployment_id",
    "solet_name",
    "release_id",
    "git_sha",
    "deployed_at",
    "trigger",
    "state",
    "version",
)
_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (deployment_records.InvalidShaError, "invalid_sha"),
    (deployment_records.InvalidTriggerError, "invalid_trigger"),
    (deployment_records.StaleVersionError, "stale_version"),
    (deployment_records.UnknownDeploymentError, "unknown_deployment"),
)
_AFTER_SEPARATOR = "|"
_READ_MODES = ("current", "history", "get")
_DEFAULT_LIMIT = 20

Envelope = dict[str, Any]


def _ok(data: dict[str, Any]) -> Envelope:
    return {"action_status": ActionStatus.COMPLETED.value, "data": data, "actions": []}


def _error(code: str, message: str) -> Envelope:
    """Error envelope that names the refusal code, never a silent empty result."""
    return {
        "action_status": ActionStatus.ERROR.value,
        "error": f"{code}: {message}",
        "data": {"error_code": code},
        "actions": [],
    }


def _refusal(error: deployment_records.DeploymentRecordError) -> Envelope:
    for error_type, code in _ERROR_CODES:
        if isinstance(error, error_type):
            return _error(code, str(error))
    return _error("invalid_argument", str(error))


def _row(row: dict[str, object]) -> dict[str, Any]:
    """Project a stored row to the contract fields; deployed_at carries its UTC offset."""
    out = {name: row[name] for name in _ROW_FIELDS}
    out["deployed_at"] = datetime.fromisoformat(str(row["deployed_at"])).replace(tzinfo=UTC).isoformat()
    return out


def _actor(instance: str, model: str, effort: str) -> deployment_records.Actor:
    return deployment_records.Actor(instance=instance, model=model, effort=effort)


def _parse_after(after: str | None) -> tuple[str, str] | None | Envelope:
    if after is None:
        return None
    deployed_at, separator, deployment_id = after.partition(_AFTER_SEPARATOR)
    if not separator or not deployed_at or not deployment_id:
        return _error("invalid_argument", f"after must be a 'next' value from a previous page, got {after!r}")
    return (deployed_at, deployment_id)


def _read_current(store: deployment_records.DeploymentRecordStore, solet_name: str) -> Envelope:
    row = deployment_records.current(store, solet_name=solet_name)
    return _ok({"rows": [] if row is None else [_row(row)], "next": None})


def _read_one(store: deployment_records.DeploymentRecordStore, solet_name: str, deployment_id: str | None) -> Envelope:
    if not deployment_id:
        return _error("invalid_argument", "deployment_id is required for mode 'get'")
    found = deployment_records.get(store, deployment_id=deployment_id)
    if found is None or found.get("solet_name") != solet_name:
        return _error("unknown_deployment", f"no deployment record {deployment_id!r} for solet {solet_name!r}")
    return _ok({"rows": [_row(found)], "next": None})


def _read_page(store: deployment_records.DeploymentRecordStore, solet_name: str, limit: int, after: str | None) -> Envelope:
    cursor = _parse_after(after)
    if isinstance(cursor, dict):
        return cursor
    page = deployment_records.history(store, solet_name=solet_name, limit=limit, after=cursor)
    next_token = None if page.next_after is None else _AFTER_SEPARATOR.join(page.next_after)
    return _ok({"rows": [_row(r) for r in page.rows], "next": next_token})


# --- run_deploy_rule ---------------------------------------------------------

# The rule runs from the solet's checkout: a release ships no quality_gates
# package, which the rule imports for host admission.
_RULE_SCRIPT = Path("ananta/src/ananta/services/lifecycle_management_service/_deploy_by_rule.py")
_VENV_BIN = Path(".venv/bin")
# The deployment plugin's release manager keeps each solet's releases under
# ~/.ananta/releases/<solet> (its RELEASES_ROOT_DEFAULT); the rule reads the
# serving release there.
_RELEASES_HOME = Path(".ananta/releases")
_ATTEMPT_DIR = Path("data/runtime/deploy-by-rule")
_ATTEMPT_LOCK = "attempt.lock"
_ATTEMPT_ID = re.compile(r"dra-\d{8}T\d{12}Z-[0-9a-f]{8}")
_SHA = re.compile(r"[0-9a-f]{40}")
_TRIGGERS = ("landing", "tick")
# Enough history for a caller to read an attempt back a few landings later.
# Each start removes anything older, so the folder has a fixed end.
_KEPT_ATTEMPTS = 20
_DETAIL_LIMIT = 1200
_GIT_TIMEOUT_SECONDS = 10
_DEPLOYED_DECISIONS = frozenset({"deployed", "already_recorded", "already_current"})


@dataclass(frozen=True)
class DeployRuleLaunch:
    """One detached start of the guarded deploy rule."""

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    stdout: Path
    stderr: Path
    lock_fd: int


def start_detached(launch: DeployRuleLaunch) -> int:
    """Start the rule in its own session; it holds the inherited attempt lock until it exits."""
    with launch.stdout.open("wb") as out, launch.stderr.open("wb") as err:
        child = subprocess.Popen(
            launch.argv,
            cwd=launch.cwd,
            env=launch.env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            pass_fds=(launch.lock_fd,),
            start_new_session=True,
        )
    return child.pid


@dataclass(frozen=True)
class DeployRulePorts:
    """The verb's one external effect: starting the rule process."""

    start: Callable[[DeployRuleLaunch], int] = start_detached


@dataclass(frozen=True)
class _DeployRuleSettings:
    """Where this solet's rule runs from, all read from the solet itself."""

    solet_name: str
    app_home: Path
    repo: Path
    releases_root: Path

    @property
    def python(self) -> Path:
        return self.repo / _VENV_BIN / "python3"

    @property
    def bridge(self) -> Path:
        return self.repo / _VENV_BIN / "solet-bridge"

    @property
    def script(self) -> Path:
        return self.repo / _RULE_SCRIPT

    @property
    def attempts(self) -> Path:
        return self.app_home / _ATTEMPT_DIR


@dataclass(frozen=True)
class _DeployRequest:
    trigger: str
    agent_instance_id: str
    actor_model: str
    actor_effort: str
    caller: str


def _refuse_deploy_rule_arguments(solet_name: str, trigger: str, attempt_id: str | None) -> Envelope | None:
    own = EnvironmentConfig.solet_name()
    if solet_name != own:
        return _error("solet_mismatch", f"this is solet {own!r}; it runs only its own deploy rule, not {solet_name!r}'s")
    if trigger not in _TRIGGERS:
        return _error("invalid_trigger", f"trigger must be one of {list(_TRIGGERS)}, got {trigger!r}")
    if attempt_id is not None and not _ATTEMPT_ID.fullmatch(attempt_id):
        return _error("invalid_argument", f"attempt_id must be an id this verb answered (dra-<UTC time>-<hex>), got {attempt_id!r}")
    return None


def _deploy_rule_settings(app_home: Path, solet_name: str) -> _DeployRuleSettings | Envelope:
    """The checkout above APP_HOME, as the deployment plugin resolves it; refused when its rule files are absent."""
    settings = _DeployRuleSettings(solet_name, app_home, app_home.parent, Path.home() / _RELEASES_HOME / solet_name)
    missing = [str(path) for path in (settings.python, settings.bridge, settings.script) if not path.is_file()]
    if missing:
        return _error("deploy_rule_unconfigured", f"the deploy rule runs from the checkout above APP_HOME, which lacks {', '.join(missing)}")
    return settings


def _caller(call_context: CallContext | None) -> str:
    """The server-built caller, for the start line: attribution, never an authority."""
    if call_context is None:
        return "unknown"
    if call_context.calling_plugin:
        return f"plugin:{call_context.calling_plugin}"
    if call_context.principal_id:
        return f"{call_context.principal_kind}:{call_context.principal_id}"
    return call_context.principal_kind


def _clean_environment() -> dict[str, str]:
    """The solet's environment without settings that would point git or Python elsewhere."""
    return {name: value for name, value in os.environ.items() if not name.startswith("GIT_") and name not in ("PYTHONPATH", "PYTHONHOME")}


def _master(repo: Path) -> str | None:
    """Master's commit for the start line; the rule reads its own target, so an unread master is an advisory."""
    try:
        completed = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "master^{commit}"], capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS, check=False, env=_clean_environment())
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("deploy_rule advisory: master unread: %s", error)
        return None
    sha = completed.stdout.strip()
    if completed.returncode != 0 or not _SHA.fullmatch(sha):
        logger.warning("deploy_rule advisory: master unread: exit %s %s", completed.returncode, completed.stderr.strip()[:200])
        return None
    return sha


def _argv(settings: _DeployRuleSettings, request: _DeployRequest) -> tuple[str, ...]:
    return (
        str(settings.python),
        str(settings.script),
        "--trigger",
        request.trigger,
        "--repo",
        str(settings.repo),
        "--app-home",
        str(settings.app_home),
        "--releases-root",
        str(settings.releases_root),
        "--bridge",
        str(settings.bridge),
        "--agent-instance-id",
        request.agent_instance_id,
        "--actor-model",
        request.actor_model,
        "--actor-effort",
        request.actor_effort,
    )


def _bounded(value: object) -> str:
    text = str(value)
    return text if len(text) <= _DETAIL_LIMIT else f"…{text[-(_DETAIL_LIMIT - 1) :]}"


def _outcome(outcome: str, decision: str, attempt_id: str | None, target: str | None, child_pid: int | None, receipt: dict[str, Any] | None, detail: str | None) -> dict[str, Any]:
    return {"outcome": outcome, "decision": decision, "attempt_id": attempt_id, "target": target, "child_pid": child_pid, "receipt": receipt, "detail": detail}


def _typed(decision: str) -> str:
    """The caller's four outcomes over the rule's decisions; an unknown decision needs a person."""
    if decision in _DEPLOYED_DECISIONS:
        return "deployed"
    if decision.startswith("skipped_"):
        return "skipped"
    if decision.startswith("deferred_"):
        return "deferred"
    return "failed"


def _record_path(settings: _DeployRuleSettings, attempt_id: str) -> Path:
    return settings.attempts / f"{attempt_id}.json"


def _write_record(settings: _DeployRuleSettings, record: dict[str, Any]) -> None:
    """Replace the attempt record whole; only the verb writes it, the child writes only its own output."""
    path = _record_path(settings, str(record["attempt_id"]))
    scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    scratch.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(scratch, path)


def _load_record(path: Path) -> dict[str, Any] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _records(settings: _DeployRuleSettings) -> list[Path]:
    """Attempt records oldest first: an id leads with its UTC start time."""
    return sorted(settings.attempts.glob("dra-*.json"))


def _attempt_running(settings: _DeployRuleSettings) -> bool:
    """Whether a rule process still holds the attempt lock; its exit releases it, whatever the pid becomes."""
    try:
        lock_fd = os.open(settings.attempts / _ATTEMPT_LOCK, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(lock_fd)
    return False


def _running(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        return _outcome("deferred", "in_progress", None, None, None, None, "a deploy-rule attempt holds the lock, but its record is unreadable")
    return _outcome("deferred", "in_progress", record.get("attempt_id"), record.get("target"), record.get("child_pid"), None, None)


def _receipt(path: Path) -> dict[str, Any] | None:
    """The rule's terminal receipt: the last stdout line, a JSON object with a decision."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        receipt = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None
    return receipt if isinstance(receipt, dict) and isinstance(receipt.get("decision"), str) else None


def _stderr_tail(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return _bounded(text) if text else None


def _finished(settings: _DeployRuleSettings, record: dict[str, Any]) -> dict[str, Any]:
    attempt_id = str(record["attempt_id"])
    receipt = _receipt(settings.attempts / f"{attempt_id}.out")
    if receipt is None:
        detail = _stderr_tail(settings.attempts / f"{attempt_id}.err") or "the rule process ended without a terminal receipt"
        return _outcome("failed", "attempt_abandoned", attempt_id, record.get("target"), record.get("child_pid"), None, detail)
    decision = str(receipt["decision"])
    target = receipt.get("target") if isinstance(receipt.get("target"), str) else record.get("target")
    error = receipt.get("error")
    return _outcome(_typed(decision), decision, attempt_id, target, record.get("child_pid"), receipt, _bounded(error) if error else None)


def _read_attempt(settings: _DeployRuleSettings, attempt_id: str) -> Envelope:
    record = _load_record(_record_path(settings, attempt_id))
    if record is None:
        return _error("unknown_attempt", f"no deploy-rule attempt {attempt_id!r} is kept; the newest {_KEPT_ATTEMPTS} are")
    records = _records(settings)
    if records and records[-1].stem == attempt_id and _attempt_running(settings):
        return _ok(_running(record))
    return _ok(_finished(settings, record))


def _remove_attempt(settings: _DeployRuleSettings, attempt_id: str) -> None:
    for suffix in (".json", ".out", ".err"):
        try:
            (settings.attempts / f"{attempt_id}{suffix}").unlink(missing_ok=True)
        except OSError as error:
            logger.warning("deploy_rule advisory: attempt %s%s not removed: %s", attempt_id, suffix, error)


def _start(settings: _DeployRuleSettings, ports: DeployRulePorts, request: _DeployRequest, lock_fd: int) -> Envelope:
    """Start one attempt while holding the attempt lock; the child keeps the lock for its whole life."""
    attempt_id = f"dra-{datetime.now(UTC):%Y%m%dT%H%M%S%f}Z-{secrets.token_hex(4)}"
    target = _master(settings.repo)
    record: dict[str, Any] = {
        "attempt_id": attempt_id,
        "target": target,
        "trigger": request.trigger,
        "caller": request.caller,
        "agent_instance_id": request.agent_instance_id,
        "actor_model": request.actor_model,
        "actor_effort": request.actor_effort,
        "started_at": datetime.now(UTC).isoformat(),
        "child_pid": None,
    }
    environment = {**_clean_environment(), "SOLET_NAME": settings.solet_name}
    launch = DeployRuleLaunch(_argv(settings, request), environment, settings.attempts, settings.attempts / f"{attempt_id}.out", settings.attempts / f"{attempt_id}.err", lock_fd)
    try:
        _write_record(settings, record)
        child_pid = ports.start(launch)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        _remove_attempt(settings, attempt_id)
        logger.warning("deploy_rule start failed: %s", error)
        return _ok(_outcome("failed", "start_failed", None, target, None, None, _bounded(error)))
    try:
        _write_record(settings, {**record, "child_pid": child_pid})
    except OSError as error:
        logger.warning("deploy_rule advisory: attempt %s record keeps no child pid: %s", attempt_id, error)
    logger.info("deploy_rule target=%s trigger=%s child_pid=%d caller=%s", target or "unknown", request.trigger, child_pid, request.caller)
    for path in _records(settings)[:-_KEPT_ATTEMPTS]:
        _remove_attempt(settings, path.stem)
    return _ok(_outcome("deferred", "in_progress", attempt_id, target, child_pid, None, None))


def _start_or_attach(settings: _DeployRuleSettings, ports: DeployRulePorts, request: _DeployRequest) -> Envelope:
    """Take the attempt lock and start, or attach to the attempt that holds it."""
    try:
        settings.attempts.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(settings.attempts / _ATTEMPT_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        return _ok(_outcome("failed", "start_failed", None, None, None, None, _bounded(error)))
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            records = _records(settings)
            return _ok(_running(_load_record(records[-1]) if records else None))
        return _start(settings, ports, request, lock_fd)
    finally:
        os.close(lock_fd)


class DeploymentRecordVerbs:
    """Concrete deployment-record verbs for ``LifecycleManagementService``."""

    _orchestrator: Any
    _deploy_rule_ports: DeployRulePorts = DeployRulePorts()

    def run_deploy_rule(
        self,
        solet_name: str,
        agent_instance_id: str,
        actor_model: str,
        actor_effort: str,
        trigger: str = "landing",
        attempt_id: str | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> Envelope:
        """Start this solet's guarded deploy rule detached, attach to the running attempt, or read one attempt."""
        refusal = _refuse_deploy_rule_arguments(solet_name, trigger, attempt_id)
        if refusal is not None:
            return refusal
        app_home = getattr(self._orchestrator, "APP_HOME", None)
        if not app_home:
            return _error("deploy_rule_unconfigured", "orchestrator.APP_HOME is not set, so the solet's checkout is unknown")
        settings = _deploy_rule_settings(Path(app_home).resolve(), solet_name)
        if isinstance(settings, dict):
            return settings
        if attempt_id is not None:
            return _read_attempt(settings, attempt_id)
        request = _DeployRequest(trigger, agent_instance_id, actor_model, actor_effort, _caller(call_context))
        return _start_or_attach(settings, self._deploy_rule_ports, request)

    def record_deployment(
        self,
        solet_name: str,
        release_id: str,
        git_sha: str,
        deployed_at: str,
        trigger: str,
        manifest_sha256: str,
        agent_instance_id: str,
        actor_model: str,
        actor_effort: str,
    ) -> Envelope:
        """Record one deployment; idempotent per (solet, sha, release)."""
        store = self._deployment_store()
        if isinstance(store, dict):
            return store
        try:
            instant = datetime.fromisoformat(deployed_at)
        except ValueError:
            return _error("invalid_deployed_at", f"deployed_at must be ISO-8601, got {deployed_at!r}")
        if instant.tzinfo is None:
            return _error("invalid_deployed_at", f"deployed_at must carry a UTC offset, got {deployed_at!r}")
        try:
            row, created = deployment_records.record(
                store,
                solet_name=solet_name,
                release_id=release_id,
                git_sha=git_sha,
                deployed_at=instant,
                trigger=trigger,
                manifest_sha256=manifest_sha256,
                actor=_actor(agent_instance_id, actor_model, actor_effort),
            )
        except deployment_records.DeploymentRecordError as error:
            return _refusal(error)
        return _ok({**_row(row), "created": created})

    def read_deployments(
        self,
        solet_name: str,
        mode: str,
        deployment_id: str | None = None,
        limit: int = _DEFAULT_LIMIT,
        after: str | None = None,
    ) -> Envelope:
        """Read deployment records: the current one, a page of history, or one by id."""
        if mode not in _READ_MODES:
            return _error("invalid_argument", f"mode must be one of {list(_READ_MODES)}, got {mode!r}")
        store = self._deployment_store()
        if isinstance(store, dict):
            return store
        try:
            if mode == "current":
                return _read_current(store, solet_name)
            if mode == "get":
                return _read_one(store, solet_name, deployment_id)
            return _read_page(store, solet_name, limit, after)
        except deployment_records.DeploymentRecordError as error:
            return _refusal(error)

    def set_deployment_state(
        self,
        deployment_id: str,
        withdrawn: bool,
        expected_version: int,
        reason: str,
        agent_instance_id: str,
        actor_model: str,
        actor_effort: str,
    ) -> Envelope:
        """Withdraw or reinstate a deployment record by compare-and-set on its version."""
        store = self._deployment_store()
        if isinstance(store, dict):
            return store
        try:
            row = deployment_records.set_withdrawn(
                store,
                deployment_id=deployment_id,
                withdrawn=withdrawn,
                expected_version=expected_version,
                reason=reason,
                actor=_actor(agent_instance_id, actor_model, actor_effort),
            )
        except deployment_records.DeploymentRecordError as error:
            return _refusal(error)
        return _ok(_row(row))

    def _deployment_store(self) -> deployment_records.DeploymentRecordStore | Envelope:
        """The state service as the deployment record store, or an error envelope."""
        state = self._orchestrator.get_service("state_service")
        if not isinstance(state, deployment_records.DeploymentRecordStore):
            return _error("state_service_unavailable", "the state service is not available to record or read deployments")
        return state
