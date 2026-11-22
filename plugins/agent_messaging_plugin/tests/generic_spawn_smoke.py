#!/usr/bin/env python3
"""``spawn_session`` takes generic fields only: it starts a session from the caller,
runtime, model, effort, workspace, first message, permissions and an opaque label.

The public cases drive the launch path (ActionProcessor -> ``spawn_session_action``
-> ``submit_launch`` -> launch job -> lifecycle) on the launch fixture's in-memory
state, with a recording host driver standing in for the default tmux host. No
database, git, register or real host is used.

Cases (the s1-* cases are plan s1's red-first proof that the old mode is gone):
  (s1-a) the declared parameters are exactly the generic set.
  (s1-b) every old field is refused as an unknown argument at the core boundary.
  (s1-c) the lifecycle verb takes no register, actor or messaging, and the
         request type carries no old field.
  (s1-d) spawn_session, qualify_fleet and list_sessions run no register CLI and no git.
  (s1-e) the lifecycle verb starts a session from the generic request alone and
         its result has no ownership_receipt or first_go_status.
  (a) a start completes: the host runs in the workspace verbatim, the first turn
      is the caller's text byte for byte, no authority prompt is rendered,
      session_status and list_sessions report label and workspace, and the
      calling session is recorded as lineage.
  (b) the same start runs no register CLI and no git subprocess.
  (d) a relative or missing workspace is refused generic_workspace_invalid before
      any row is written.
  (f) a model cell a policy rule turned off is refused before any row is written,
      naming the rule; a cell the catalog lacks starts with an advisory.
  (g) a caller with no session identity starts, with empty lineage.
  (h) the managed-dispatch verbs are gone; (j) its modules are not importable.
  (i) a host that reports first_turn_on_launch gets the first message in its spec
      and never has its channel send called, yet the result reads the first turn
      delivered; a host that does not report it gets no first_turn key and the
      channel send.
  (k) the spawn path carries no dispatch contract.
  (m) permission_mode, transport and the allowlist fall back to plugin.yaml only
      when omitted; an explicit value is never overridden.
  (n) an explicit empty allowed_tools stays empty in the request and in the host
      spawn spec (no tool allowed) even when plugin.yaml names a default; only an
      omitted allowed_tools takes the default.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/generic_spawn_smoke.py
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(ROOT / "ananta" / "tests" / "core" / "actions"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ananta.core.actions.action_factory as action_factory  # noqa: E402
from ananta.core.actions.action_factory import validate_process_arguments  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from async_job_fixture_support import JobState  # noqa: E402 - resolved at runtime by the sys.path.insert calls above
from session_launch_fixture_support import action, finish_fixture, plugin_fixture  # noqa: E402

import agent_messaging_plugin.operation_jobs as operation_jobs  # noqa: E402
import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as lifecycle_verbs  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin, _SessionLifecyclePolicyConfig, _spawn_session_request_from_params  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_SESSION,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CELL_POLICY,
)
from agent_messaging_plugin.session_lifecycle_verbs import SpawnSessionRequest  # noqa: E402

_PROCESS_KEY = "plugin::agent_messaging_plugin::spawn_session"
_RUNTIME = session_hosts.DEFAULT_AGENT_RUNTIME
_MODEL = "claude-fixture-model"
_EFFORT = "high"
_CALLER = "agi-generic-smoke-caller"
# Leading/trailing whitespace, blank lines and non-ASCII: any normalisation of
# the caller's text would show as a byte difference.
_FIRST_MESSAGE = "  Read the task file, then report.\n\nSecond line: naïve café ✓ \t\n"
_DISABLED_RULE = "rule-generic-smoke-off"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _RecordingChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def insert(self, text: str) -> None:
        self.sent.append(text)

    def submit(self) -> None:
        return None

    def send(self, text: str) -> None:
        self.sent.append(text)


class _RecordingDriver:
    """Records each host spec and every first-turn text; launches nothing."""

    def __init__(self) -> None:
        self.specs: list[dict[str, object]] = []
        self.channel = _RecordingChannel()

    def spawn(self, spec: object) -> str:
        self.specs.append(dict(cast(dict[str, object], spec)))
        return f"generic-smoke-ref-{len(self.specs)}"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _RecordingChannel:
        del host_ref
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


class _TerminatingDriver(_RecordingDriver):
    def __init__(self) -> None:
        super().__init__()
        self.stopped: set[str] = set()

    def alive(self, host_ref: str) -> bool:
        return host_ref not in self.stopped

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del grace_seconds
        self.stopped.add(host_ref)


class _RaisingChannel(_RecordingChannel):
    """The tmux boundary channel of a pane whose composer never became empty:
    every send refuses, as the shipped channel does after its 10 s wait."""

    def __init__(self) -> None:
        super().__init__()
        self.send_calls = 0

    def send(self, text: str) -> None:
        del text
        self.send_calls += 1
        raise session_hosts.DriverChannelSendError("tmux pane 'x' showed no empty composer within 10.0s; nothing was sent.")


class _LaunchDriver(_RecordingDriver):
    """A host that carries the first turn on its launch command, whose channel
    would refuse a send: delivery must not depend on that channel."""

    def __init__(self) -> None:
        super().__init__()
        self.refusing_channel = _RaisingChannel()

    def driver_channel(self, host_ref: str) -> _RaisingChannel:
        del host_ref
        return self.refusing_channel

    def capability_report(self) -> dict[str, object]:
        return {"first_turn_on_launch": True}


class _Fixture:
    """One plugin over one in-memory state, with the default host recorded."""

    def __init__(self) -> None:
        self.state = JobState()
        self.plugin: Any = plugin_fixture(self.state)
        self.driver = _RecordingDriver()
        self.workspace = tempfile.mkdtemp(prefix="generic-spawn-workspace-")
        write = self.state.write_state

        def stamped(namespace: str, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            # The real state layer defaults is_deleted to 0 on insert.
            if data.get("table") == TABLE_MANAGED_SESSION:
                data = {**data, "record": {"is_deleted": 0, **data["record"]}}
            return write(namespace, data, **kwargs)

        self.state.write_state = stamped

    def close(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def generic_params(self, **overrides: object) -> dict[str, Any]:
        params: dict[str, Any] = {
            "agent_runtime": _RUNTIME,
            "model": _MODEL,
            "effort": _EFFORT,
            "workspace": self.workspace,
            "first_message": _FIRST_MESSAGE,
            "permission_mode": "bypassPermissions",
            "label": "smoke-label",
        }
        params.update(overrides)
        return params

    def call(self, verb: str, params: dict[str, Any], *, caller: str | None) -> dict[str, Any]:
        processor = ActionProcessor(PluginManager())
        trigger = {"inference_vertex_session_id": caller} if caller else {}
        with patch.object(processor, "_get_flow_trigger_data", return_value=trigger):
            return cast(dict[str, Any], processor._execute_plugin_method(self.plugin, verb, params, action(verb)))  # noqa: SLF001

    def spawn(self, params: dict[str, Any], *, caller: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        """Submit a spawn and return its receipt and the job's stored envelope."""
        receipt = self.call("spawn_session", params, caller=caller)
        if receipt.get("action_status") != "completed":
            return receipt, {}
        finish_fixture(self.plugin)
        manager = self.plugin._try_acquire_async_job_manager()
        job_id = str(receipt["data"]["job_id"])
        job = manager.get_job(job_id)["data"]["job"]
        kind = "result" if job.get("result") is not None or job["status"] == "completed" else "error"
        return receipt, cast(dict[str, Any], manager.get_job_payload(job_id, kind)["data"]["payload"])

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.state.rows.get(table, [])

    def seed_cell(self, model: str, *, disabled_by: str = "") -> None:
        now = datetime.now(UTC)
        self.state.rows.setdefault(TABLE_MODEL_CAPABILITY_CELL, []).append(
            {
                "id": f"mcc-{model}",
                "provider": "anthropic",
                "runtime": _RUNTIME,
                "model": model,
                "effort": _EFFORT,
                "enabled": not disabled_by,
                "acceptance": "accepted",
                "created_at": now,
                "updated_at": now,
            },
        )
        if disabled_by:
            self.state.rows.setdefault(TABLE_MODEL_CELL_POLICY, []).append(
                {
                    "id": "mcp-current",
                    "rules": [{"id": disabled_by, "selector": {"model": model}, "enabled": False}],
                    "created_at": now,
                    "updated_at": now,
                },
            )


def _install_driver(fx: _Fixture, driver: Any) -> None:
    """Replace the default host's driver for this fixture's lifetime; the
    registry patch ``_fixture`` entered restores the original on exit."""
    fx.driver = driver
    session_hosts._REGISTRY[(_RUNTIME, session_hosts.DEFAULT_HOST)] = driver  # noqa: SLF001 -- test seam


@contextmanager
def _fixture() -> Generator[_Fixture]:
    fixture = _Fixture()
    key = (_RUNTIME, session_hosts.DEFAULT_HOST)
    with ExitStack() as stack:
        stack.enter_context(patch.dict(session_hosts._REGISTRY, {key: fixture.driver}))  # noqa: SLF001 -- test seam
        stack.enter_context(patch.dict(os.environ, {}, clear=False))
        os.environ.pop("FLEET_SESSION_HOST", None)
        try:
            yield fixture
        finally:
            fixture.close()


def _error_code(envelope: dict[str, Any]) -> str:
    error = envelope.get("error")
    return str(error.get("code") or "") if isinstance(error, dict) else ""


def _declared_parameters() -> dict[str, object]:
    metadata = cast(Any, AgentMessagingPlugin.spawn_session_action)._platform_process_metadata  # noqa: SLF001
    return {name: parameter.to_dict() for name, parameter in metadata.parameters.items()}


def _check_core_boundary_accepts(params: dict[str, Any]) -> None:
    try:
        validate_process_arguments(_PROCESS_KEY, _declared_parameters(), params)
        accepted = True
    except Exception as exc:  # noqa: BLE001 -- the refusal itself is the observation
        accepted = False
        print(f"        core argument boundary: {exc}")
    _check(accepted, "(a) the core argument boundary accepts a generic-only call")


def _check_start_result(receipt: dict[str, Any], envelope: dict[str, Any]) -> str:
    """Check the submission and the job's result; return the new instance id."""
    _check(receipt.get("action_status") == "completed", f"(a) submission accepted (got {_error_code(receipt) or receipt.get('action_status')})")
    _check(envelope.get("action_status") == "completed", f"(a) launch job completed (got {_error_code(envelope) or envelope.get('action_status')})")
    data = cast(dict[str, Any], envelope.get("data") or {})
    instance = str(data.get("agent_instance_id") or "")
    _check(instance.startswith("agi-"), "(a) result names the new agent_instance_id")
    _check(data.get("label") == "smoke-label", "(a) result reports the label")
    _check(data.get("first_turn_delivered") is True, "(a) result reports the first turn delivered")
    _check(data.get("first_turn_error") == "", "(a) result reports no first-turn error")
    _check(data.get("host") == session_hosts.DEFAULT_HOST, "(a) result reports the host")
    _check(bool(data.get("host_ref")), "(a) result reports the host_ref")
    _check(data.get("advisories") == [], f"(a) an enabled catalog cell starts with no advisory (got {data.get('advisories')!r})")
    return instance


def _check_host_launch(fx: _Fixture) -> None:
    spec = fx.driver.specs[0] if fx.driver.specs else {}
    _check(spec.get("workspace") == fx.workspace, "(a) the host spec carries the workspace, verbatim")
    _check(fx.driver.channel.sent == [_FIRST_MESSAGE], "(a) the delivered first turn equals first_message byte for byte")
    _check("first_turn" not in spec, "(g) a host that does not carry the first turn on launch gets no first_turn key in its spec")
    _check(os.listdir(fx.workspace) == [], "(a) nothing was created in the workspace")


def _label_and_workspace_of(row: dict[str, Any]) -> tuple[object, object]:
    return row.get("label"), row.get("workspace")


def _check_row_and_reads(fx: _Fixture, instance: str) -> None:
    expected = ("smoke-label", fx.workspace)
    rows = [row for row in fx.rows(TABLE_MANAGED_SESSION) if row.get("agent_instance_id") == instance]
    row = rows[0] if rows else {}
    _check(row.get("spawned_by_instance_id") == _CALLER, "(a) the calling session is recorded as lineage")
    _check(_label_and_workspace_of(row) == expected, "(a) the row stores label and workspace")
    status = fx.call("session_status", {"agent_instance_id": instance}, caller=_CALLER)
    _check(_label_and_workspace_of(cast(dict[str, Any], status.get("data") or {})) == expected, "(a) session_status returns label and workspace")
    listed = fx.call("list_sessions", {"host": session_hosts.DEFAULT_HOST}, caller=_CALLER)
    sessions = cast(list[dict[str, Any]], cast(dict[str, Any], listed.get("data") or {}).get("sessions") or [])
    mine = [_label_and_workspace_of(entry) for entry in sessions if entry.get("agent_instance_id") == instance]
    _check(mine == [expected], "(a) list_sessions returns label and workspace")


def case_a_generic_start_completes() -> None:
    print("(a) generic start with a calling session")
    with _fixture() as fx:
        fx.seed_cell(_MODEL)
        params = fx.generic_params()
        _check_core_boundary_accepts(params)
        receipt, envelope = fx.spawn(params, caller=_CALLER)
        instance = _check_start_result(receipt, envelope)
        _check_host_launch(fx)
        _check_row_and_reads(fx, instance)


def _guarded(original: Callable[..., Any], violations: list[str]) -> Callable[..., Any]:
    def guard(*args: Any, **kwargs: Any) -> Any:
        argv = args[0] if args else kwargs.get("args", "")
        text = " ".join(str(part) for part in argv) if isinstance(argv, list | tuple) else str(argv)
        if ("p" + "solet") in text or "git" in text:
            violations.append(text)
            raise AssertionError(f"generic spawn ran a forbidden subprocess: {text}")
        return original(*args, **kwargs)

    return guard


def case_b_no_register_cli_or_git() -> None:
    print("(b) generic start runs no register CLI and no git")
    violations: list[str] = []
    targets = ["execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"]
    with _fixture() as fx, ExitStack() as stack:
        fx.seed_cell(_MODEL)
        stack.enter_context(patch.object(subprocess, "run", _guarded(subprocess.run, violations)))
        stack.enter_context(patch.object(subprocess, "Popen", _guarded(subprocess.Popen, violations)))
        for name in targets:
            stack.enter_context(patch.object(os, name, _guarded(getattr(os, name), violations)))
        receipt, envelope = fx.spawn(fx.generic_params(), caller=_CALLER)
        _check(receipt.get("action_status") == "completed" and envelope.get("action_status") == "completed", f"(b) generic start completes under the subprocess guard (got {_error_code(receipt) or _error_code(envelope) or envelope.get('action_status')})")
        _check(violations == [], f"(b) no register CLI or git argv was executed (saw {violations})")


def case_d_workspace_refused() -> None:
    print("(d) relative or missing workspace")
    for label, workspace in (("relative", "relative/workspace"), ("missing", "/nonexistent/generic-spawn-smoke/workspace")):
        with _fixture() as fx:
            receipt, _ = fx.spawn(fx.generic_params(workspace=workspace), caller=_CALLER)
            _check(_error_code(receipt) == "generic_workspace_invalid", f"(d) a {label} workspace is refused generic_workspace_invalid (got {_error_code(receipt) or receipt.get('action_status')})")
            _check(fx.rows(TABLE_MANAGED_SESSION) == [] and fx.rows("job") == [], f"(d) {label}: no session or job row was written")


def case_f_model_cell_policy() -> None:
    print("(f) model cell policy")
    with _fixture() as fx:
        fx.seed_cell(_MODEL, disabled_by=_DISABLED_RULE)
        receipt, _ = fx.spawn(fx.generic_params(), caller=_CALLER)
        error = cast(dict[str, Any], receipt.get("error") or {})
        _check(_error_code(receipt) == "model_cell_disabled", f"(f) a turned-off cell is refused model_cell_disabled (got {_error_code(receipt) or receipt.get('action_status')})")
        _check(_DISABLED_RULE in str(error.get("message") or ""), "(f) the refusal names the rule that turned the cell off")
        _check(fx.rows(TABLE_MANAGED_SESSION) == [] and fx.rows("job") == [] and fx.driver.specs == [], "(f) no row was written and no host launched")


def case_f_unknown_cell_starts() -> None:
    print("(f) a model cell the catalog lacks")
    with _fixture() as fx:
        receipt, envelope = fx.spawn(fx.generic_params(model="claude-model-the-catalog-lacks"), caller=_CALLER)
        advisories = cast(dict[str, Any], envelope.get("data") or {}).get("advisories") or []
        _check(envelope.get("action_status") == "completed", f"(f) a cell the catalog lacks still starts (got {_error_code(receipt) or _error_code(envelope) or envelope.get('action_status')})")
        _check(any("model_cell_unknown" in str(item) for item in advisories), f"(f) the unknown cell is reported as an advisory (got {advisories!r})")


def case_g_no_session_identity() -> None:
    print("(g) generic start from a caller with no session identity")
    with _fixture() as fx:
        fx.seed_cell(_MODEL)
        receipt, envelope = fx.spawn(fx.generic_params(), caller=None)
        data = cast(dict[str, Any], envelope.get("data") or {})
        _check(receipt.get("action_status") == "completed" and envelope.get("action_status") == "completed", f"(g) the start completes without a caller identity (got {_error_code(receipt) or _error_code(envelope) or envelope.get('action_status')})")
        rows = [row for row in fx.rows(TABLE_MANAGED_SESSION) if row.get("agent_instance_id") == data.get("agent_instance_id")]
        _check(bool(rows) and rows[0].get("spawned_by_instance_id") == "", "(g) the row's lineage is empty")


_RETIRED_WORK_VERBS = (
    "dispatch_managed_work",
    "provision_role_session",
    "report_managed_dispatch",
    "resolve_managed_dispatch",
    "managed_dispatch_status",
    "managed_dispatch_inventory",
    "managed_dispatch_events",
    "managed_dispatch_uptake_rate",
)


def case_h_work_contract_verbs_are_gone() -> None:
    print("(h) the managed-dispatch work-contract verbs are gone")
    with _fixture() as fx:
        declared = set(fx.plugin.get_edge_process_definitions())
    _check(not declared & set(_RETIRED_WORK_VERBS), f"(h) the plugin declares none of the eight verbs (got {sorted(declared & set(_RETIRED_WORK_VERBS))})")
    _check(action_factory._SESSION_START_PROCESSES == frozenset({"plugin::agent_messaging_plugin::spawn_session"}), f"(h) spawn_session is the only session-start process (got {sorted(action_factory._SESSION_START_PROCESSES)})")  # noqa: SLF001
    leftovers = {"managed_dispatch_status", "report_managed_dispatch"} & set(operation_jobs.REGISTERED_OPERATIONS)
    _check(not leftovers, f"(h) the registered operations hold neither status nor report (got {sorted(leftovers)})")


def case_i_first_turn_rides_the_launch() -> None:
    print("(i) a host that carries the first turn on its launch needs no channel send")
    with _fixture() as fx:
        fx.seed_cell(_MODEL)
        driver = _LaunchDriver()
        _install_driver(fx, driver)
        receipt, envelope = fx.spawn(fx.generic_params(), caller=_CALLER)
        data = cast(dict[str, Any], envelope.get("data") or {})
        spec = driver.specs[0] if driver.specs else {}
        _check(receipt.get("action_status") == "completed" and envelope.get("action_status") == "completed", "(f) the spawn completes on a host whose channel would refuse a send")
        _check(spec.get("first_turn") == _FIRST_MESSAGE, "(f) the host spec's first_turn equals first_message byte for byte")
        _check(data.get("first_turn_delivered") is True and data.get("first_turn_error") == "", f"(f) the result reads first_turn_delivered true with no error (got {data.get('first_turn_delivered')!r}, {data.get('first_turn_error')!r})")
        _check(driver.refusing_channel.send_calls == 0, "(f) the driver channel's send was never called")


_DELETED_DISPATCH_MODULES = ("managed_dispatch", "managed_dispatch_uptake", "managed_dispatch_bridge", "_managed_dispatch_authority")


def case_j_dispatch_modules_are_gone() -> None:
    print("(j) the managed-dispatch modules are not importable")
    for name in _DELETED_DISPATCH_MODULES:
        try:
            importlib.import_module(f"agent_messaging_plugin.{name}")
        except ModuleNotFoundError:
            _check(True, f"(j) agent_messaging_plugin.{name} raises ModuleNotFoundError")
        else:
            _check(False, f"(j) agent_messaging_plugin.{name} raises ModuleNotFoundError (it imported)")


def case_k_spawn_path_has_no_dispatch_contract() -> None:
    print("(k) the spawn path carries no dispatch contract")
    offending = [name for name in inspect.signature(lifecycle_verbs.spawn_session).parameters if "dispatch" in name]
    _check(not offending, f"(k) spawn_session takes no dispatch parameter (got {offending})")
    fields = {field.name for field in dataclasses.fields(SpawnSessionRequest)}
    _check("dispatch_id" not in fields, "(k) SpawnSessionRequest has no dispatch_id field")
    _check(not hasattr(lifecycle_verbs, "spawn_dispatch_attempt"), "(k) spawn_dispatch_attempt is gone")
    with _fixture() as fx:
        fx.seed_cell(_MODEL)
        fx.spawn(fx.generic_params(), caller=_CALLER)
        _check(fx.rows("managed_" + "dispatch") == [], "(k) a spawn writes no managed_dispatch row")


# --- Plan s1: the old mode is gone; spawn_session takes generic fields only -----

_GENERIC_FIELDS = frozenset(
    {
        "agent_runtime",
        "model",
        "effort",
        "workspace",
        "first_message",
        "label",
        "permission_mode",
        "allowed_tools",
        "allow_askuserquestion",
        "host",
        "visibility",
        "local_name",
        "report_by_seconds",
        "transport",
        "provider_env",
        "degraded_hooks_acknowledged",
        "spawned_by_instance_id",
    },
)
_OLD_FIELDS = (
    "unit_" + "id",
    "expected_" + "revision_id",
    "role_class",
    "lane_" + "id",
    "repository_root",
    "work_class",
    "budget_line",
    "dispatch_id",
    "dispatch_kind",
    "difficulty_score",
    "selection_receipt",
    "first_go",
    "assignment",
    "role_name",
    "spawned_by_role",
    "reviewed_report_vendor",
    "pair_id",
    "scope_tags",
)


def case_s1_a_declared_parameters_are_the_generic_set() -> None:
    print("(s1-a) the declared parameters are exactly the generic set")
    declared = frozenset(_declared_parameters())
    _check(declared == _GENERIC_FIELDS, f"(s1-a) declared == generic set (extra {sorted(declared - _GENERIC_FIELDS)}, missing {sorted(_GENERIC_FIELDS - declared)})")


def case_s1_b_old_fields_are_unknown_arguments() -> None:
    print("(s1-b) an old field is refused as an unknown argument")
    with _fixture() as fx:
        base = fx.generic_params()
        for name in (*_OLD_FIELDS, "release_to"):
            try:
                validate_process_arguments(_PROCESS_KEY, _declared_parameters(), {**base, name: "x"})
            except Exception as exc:  # noqa: BLE001 -- the refusal is the observation
                _check("Unknown arguments" in str(exc) and name in str(exc), f"(s1-b) {name} is refused as an unknown argument")
            else:
                _check(False, f"(s1-b) {name} is refused as an unknown argument (it was accepted)")


def case_s1_c_signature_and_request_carry_no_old_field() -> None:
    print("(s1-c) the verb signature and the request type carry no old field")
    signature = set(inspect.signature(lifecycle_verbs.spawn_session).parameters)
    _check(not signature & {"register", "actor", "messaging"}, f"(s1-c) spawn_session takes no register, actor or messaging (got {sorted(signature)})")
    fields = {field.name for field in dataclasses.fields(SpawnSessionRequest)}
    leftover = sorted(fields & {*_OLD_FIELDS, "enforce_selection_receipt", "synthetic_qualification_no_worktree", "directed_by_role"})
    _check(not leftover, f"(s1-c) SpawnSessionRequest has no old field (got {leftover})")


def case_s1_d_no_register_cli_or_git_across_the_verbs() -> None:
    print("(s1-d) spawn_session, qualify_fleet and list_sessions run no register CLI and no git")
    violations: list[str] = []
    targets = ["execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"]
    with _fixture() as fx, ExitStack() as stack:
        fx.seed_cell(_MODEL)
        qualification = _RecordingDriver()
        stack.enter_context(patch.dict(session_hosts._REGISTRY, {(_RUNTIME, session_hosts.QUALIFICATION_HOST): qualification}))  # noqa: SLF001 -- test seam
        stack.enter_context(patch.object(subprocess, "run", _guarded(subprocess.run, violations)))
        stack.enter_context(patch.object(subprocess, "Popen", _guarded(subprocess.Popen, violations)))
        for name in targets:
            stack.enter_context(patch.object(os, name, _guarded(getattr(os, name), violations)))
        receipt, envelope = fx.spawn(fx.generic_params(), caller=_CALLER)
        _check(receipt.get("action_status") == "completed" and envelope.get("action_status") == "completed", f"(s1-d) spawn_session completes (got {_error_code(receipt) or _error_code(envelope)})")
        listed = fx.call("list_sessions", {"host": session_hosts.DEFAULT_HOST}, caller=_CALLER)
        _check(listed.get("action_status") == "completed", f"(s1-d) list_sessions completes (got {_error_code(listed)})")
        plugin = fx.plugin
        plugin._qualify_fleet_worker = lambda *args: (True, True, None)  # noqa: SLF001 -- the watch leg needs a live bridge; the spawn leg is under test
        plugin._retire_qualification_worker = lambda *args: None  # noqa: SLF001
        plugin.peer_release_role = lambda *args: {"action_status": "completed", "data": {}}
        qualified = plugin.qualify_fleet({}, {})
        _check(qualified.get("action_status") == "completed", f"(s1-d) qualify_fleet completes (got {_error_code(qualified)}: {(qualified.get('error') or {}).get('message')})")
        _check(len(qualification.specs) == 1 and "workspace" not in qualification.specs[0], "(s1-d) qualify_fleet launched one qualification host with no working directory")
        _check(violations == [], f"(s1-d) no register CLI or git argv was executed (saw {violations})")


def case_s1_e_result_has_no_owner_or_first_go() -> None:
    print("(s1-e) the lifecycle verb's result carries no ownership_receipt and no first_go_status")
    with _fixture() as fx:
        fx.seed_cell(_MODEL)
        req = SpawnSessionRequest(
            agent_runtime=_RUNTIME,
            model=_MODEL,
            effort=_EFFORT,
            workspace=fx.workspace,
            first_message=_FIRST_MESSAGE,
            label="s1-e",
            directed_by="operator:none",
        )
        data = lifecycle_verbs.spawn_session(cast(Any, fx.state), req)
        _check("agent_instance_id" in data and data.get("first_turn_delivered") is True, f"(s1-e) the verb starts a session from the generic request alone (got {sorted(data)})")
        _check("ownership_receipt" not in data and "first_go_status" not in data and "first_go_receipt" not in data, f"(s1-e) no ownership_receipt, first_go_status or first_go_receipt (got {sorted(data)})")


def case_m_policy_fills_only_omitted_fields() -> None:
    print("(m) plugin.yaml fills omitted permission fields and never overrides an explicit one")
    policy = _SessionLifecyclePolicyConfig(default_allowed_tools=("tool-a",), headless_permission_mode="policy-mode", default_fleet_transport="mcp")
    raw: dict[str, Any] = {"agent_runtime": _RUNTIME, "model": _MODEL, "effort": _EFFORT, "workspace": "/", "first_message": "x"}
    filled = _spawn_session_request_from_params(raw, {"call_context": None}, policy)
    _check((filled.allowed_tools, filled.permission_mode, filled.transport) == (("tool-a",), "policy-mode", "mcp"), f"(m) omitted fields take the plugin.yaml values (got {(filled.allowed_tools, filled.permission_mode, filled.transport)})")
    explicit = _spawn_session_request_from_params({**raw, "allowed_tools": ["tool-b"], "permission_mode": "own-mode", "transport": "watch"}, {"call_context": None}, policy)
    _check((explicit.allowed_tools, explicit.permission_mode, explicit.transport) == (("tool-b",), "own-mode", "watch"), "(m) explicit fields are kept")


def case_n_explicit_empty_allowlist_stays_empty() -> None:
    print("(n) an explicit empty allowed_tools is kept; only an omitted one takes the default")
    policy = _SessionLifecyclePolicyConfig(default_allowed_tools=("tool-a",), headless_permission_mode="policy-mode", default_fleet_transport="mcp")
    raw: dict[str, Any] = {"agent_runtime": _RUNTIME, "model": _MODEL, "effort": _EFFORT, "workspace": "/", "first_message": "x"}
    empty = _spawn_session_request_from_params({**raw, "allowed_tools": []}, {"call_context": None}, policy)
    omitted = _spawn_session_request_from_params(raw, {"call_context": None}, policy)
    _check(tuple(empty.allowed_tools or ()) == () and empty.allowed_tools is not None, f"(n) an explicit [] stays empty in the request (got {empty.allowed_tools!r})")
    _check(omitted.allowed_tools == ("tool-a",), f"(n) an omitted allowed_tools takes the default (got {omitted.allowed_tools!r})")
    for label, overrides, expected in (("explicit [] reaches the host spec as empty", {"allowed_tools": []}, ()), ("an omitted value reaches the host spec as the default", {}, ("tool-a",))):
        with _fixture() as fx:
            fx.seed_cell(_MODEL)
            fx.plugin._build_session_lifecycle_policy_config = lambda: policy  # noqa: SLF001 -- test seam: plugin.yaml names a default
            receipt, envelope = fx.spawn(fx.generic_params(**overrides), caller=_CALLER)
            started = envelope.get("action_status") == "completed" and len(fx.driver.specs) == 1
            _check(started, f"(n) the spawn completes (got {_error_code(receipt) or _error_code(envelope)})")
            _check(started and tuple(cast(Any, fx.driver.specs[0]["allowed_tools"])) == expected, f"(n) {label} (got {[spec.get('allowed_tools') for spec in fx.driver.specs]!r})")


def case_l1_removes_lane_custody() -> None:
    print("(l1-a) custody modules and processes are absent")
    for name in ("lane_worktrees", "lane_worktree_evidence", "lane_worktree_preservation"):
        try:
            importlib.import_module(f"agent_messaging_plugin.{name}")
        except ModuleNotFoundError:
            _check(True, f"(l1-a) {name} is absent")
        else:
            _check(False, f"(l1-a) {name} is absent")
    with _fixture() as fx:
        processes = set(fx.plugin.get_edge_process_definitions())
        for name in ("preserve_lane_evidence", "read_lane_evidence", "retire_completed_lane_sessions"):
            _check(name not in processes, f"(l1-b) {name} is absent")


def case_l1_retire_keeps_workspace() -> None:
    print("(l1-c) retire leaves the generic workspace untouched")
    violations: list[str] = []
    with _fixture() as fx, ExitStack() as stack:
        fx.seed_cell(_MODEL)
        _install_driver(fx, _TerminatingDriver())
        marker = Path(fx.workspace) / "marker"
        marker.write_text("keep", encoding="utf-8")
        stack.enter_context(patch.object(subprocess, "run", _guarded(subprocess.run, violations)))
        stack.enter_context(patch.object(subprocess, "Popen", _guarded(subprocess.Popen, violations)))
        receipt, envelope = fx.spawn(fx.generic_params(), caller=_CALLER)
        instance = str(cast(dict[str, Any], envelope.get("data") or {}).get("agent_instance_id") or "")
        try:
            result = lifecycle_verbs.retire_session(cast(Any, fx.state), agent_instance_id=instance, directed_by=_CALLER)
        except Exception as exc:  # noqa: BLE001 -- preserve the red observation
            result = {"error": str(exc)}
        _check(receipt.get("action_status") == "completed" and result.get("error") is None and result.get("already_retired") is False, f"(l1-c) retirement completes (got {result})")
        _check(result.get("host_liveness") == "dead", "(l1-c) host is observed stopped")
        _check(not violations and marker.read_text(encoding="utf-8") == "keep", f"(l1-c) no git and workspace preserved (saw {violations})")
        _check(not any("worktree" in key or "custody" in key for key in result), "(l1-c) result has no worktree or custody keys")


def case_l1_environment() -> None:
    print("(l1-d) host implementations do not derive PYTHONPATH from workspace")
    for name in ("headless_adapter", "tmux_adapter", "codex_tmux", "codex_app_server"):
        module = importlib.import_module(f"agent_messaging_plugin.{name}")
        _check("worktree_pythonpath" not in inspect.getsource(module), f"(l1-d) {name} does not derive PYTHONPATH")


def case_l1_published_process_schemas() -> None:
    from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator

    generator = InvocationSchemaGenerator()
    for name, expected in (("retire_session", {"agent_instance_id"}),):
        method = getattr(AgentMessagingPlugin, name)
        metadata = method._platform_process_metadata
        parameters = {key: value.to_dict() for key, value in metadata.parameters.items()}
        schema = generator.generate(f"plugin::agent_messaging_plugin::{name}", parameters)
        properties = cast(dict[str, Any], schema["properties"])
        arguments = cast(dict[str, Any], properties["arguments"])
        _check(set(cast(dict[str, Any], arguments["properties"])) == expected, f"(l1-schema) {name} exposes exactly {sorted(expected)}")
        _check(set(cast(list[str], arguments["required"])) == expected and arguments["additionalProperties"] is False, f"(l1-schema) {name} requires those inputs and closes extra arguments")


def case_codex_workspace() -> None:
    from agent_messaging_plugin.codex_app_server import CodexAppServerHostDriver
    from agent_messaging_plugin.codex_tmux import CodexTmuxHostDriver

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        workspace = root / "ordinary directory"
        workspace.mkdir()
        request = SpawnSessionRequest(agent_runtime="codex", workspace=str(workspace), transport="mcp")
        factory = Mock()
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        app = CodexAppServerHostDriver(cwd=root, client_factory=factory, solet_name="fixture", end_notice=False, codex_home=root)
        tmux = CodexTmuxHostDriver(cwd=root, run_fn=run, solet_name="fixture", codex_home=root)
        for driver, name in ((app, "headless"), (tmux, "tmux")):
            spec = lifecycle_verbs._host_spawn_spec(request, "agi-fixture", "fixture", name)
            with patch.object(driver, "verify_config", return_value=[]):
                for explicit, expected in ((True, workspace), (False, root)):
                    if not explicit:
                        spec.pop("workspace")
                    run.reset_mock()
                    factory.reset_mock()
                    driver.spawn(spec)
                    if driver is app:
                        actual = factory.call_args.kwargs["cwd"]
                    else:
                        command = next(call.args[0] for call in run.call_args_list if "new-session" in call.args[0])
                        actual = Path(command[command.index("-c") + 1])
                    _check(actual == expected, f"{name} starts in {expected} (got {actual})")
                _check_missing_codex_workspace(driver, spec, workspace / "missing", run, factory)


def _check_missing_codex_workspace(driver: session_hosts.HostDriver, spec: dict[str, object], missing: Path, run: Mock, factory: Mock) -> None:
    run.reset_mock()
    factory.reset_mock()
    try:
        driver.spawn({**spec, "workspace": str(missing)})
    except session_hosts.HostCannotSpawnError as exc:
        _check(str(missing) in str(exc) and not run.called and not factory.called, "Codex host refuses the missing workspace before launch")
    else:
        _check(False, "Codex host refuses the missing workspace before launch")


def case_s2p_host_builders_have_no_assignment_prompt() -> None:
    import agent_messaging_plugin.codex_app_server as app_host  # noqa: PLC0415
    import agent_messaging_plugin.codex_tmux as codex_host  # noqa: PLC0415
    import agent_messaging_plugin.headless_adapter as headless_host  # noqa: PLC0415
    import agent_messaging_plugin.tmux_adapter as claude_host  # noqa: PLC0415

    _check(not hasattr(headless_host, "_authority_system_prompt"), "(s2p) assignment authority renderer is removed")
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        spec: dict[str, object] = {"workspace": tmp, "first_turn": _FIRST_MESSAGE, "model": _MODEL, "effort": _EFFORT}
        hooks: dict[str, Path] = dict.fromkeys(("headless_tool_allowlist_gate.py", "capture_session_mapping.py", "rotation_due_watch.py", "wake_waiter.py", "session_start_context.py", "step_zero_reminder.py"), cwd / "hook.py")
        claude = claude_host.TmuxHostDriver(cwd=cwd, solet_name="fixture", solet_bin="/bin/true")
        with patch.object(claude_host, "_tmux_hook_settings_json", return_value="{}"):
            argv = claude._spawn_command(spec, transport="watch", label="fixture", resolved_hooks=hooks)
        _check("--append-system-prompt" not in argv, "(s2p) Claude launch omits the authority prompt option")
        _check(_FIRST_MESSAGE in argv and _MODEL in argv and _EFFORT in argv, "(s2p) Claude launch preserves first message, model and effort")
        codex = codex_host.CodexTmuxHostDriver(cwd=cwd, solet_name="fixture", solet_bin="/bin/true", codex_home=cwd)
        identity = codex_host._TmuxSpawnIdentity("agi-fixture", "ases-fixture", "fixture")
        with patch.object(codex_host, "_codex_config_overrides", return_value=[]):
            argv = codex._codex_command(spec, identity, "watch", cwd)
        _check(not any("developer_instructions=" in value for value in argv), "(s2p) Codex tmux omits the authority config option")
        _check(_MODEL in argv and any("model_reasoning_effort=" in value for value in argv), "(s2p) Codex tmux preserves model and effort")
        app = app_host.CodexAppServerHostDriver(cwd=cwd, solet_name="fixture", solet_bin="/bin/true", codex_home=cwd)
        with patch.object(app_host, "_codex_config_overrides", return_value=[]), patch.object(app_host, "_authority_system_prompt", return_value="legacy-authority", create=True):
            client = app._build_client(spec, app_host._SpawnIdentity("agi-fixture", "ases-fixture", "fixture"), "watch", {}, cwd)
        with patch.object(client, "_request", return_value={"thread": {"id": "fixture"}}) as request:
            client._start_thread()
        params = request.call_args.args[1]
        _check(params["developerInstructions"] == "", "(s2p) app-server constructs and sends empty developer instructions with no authority text")
        _check(params["model"] == _MODEL and params["cwd"] == tmp, "(s2p) app-server preserves model and workspace")


def case_s2_retired_surfaces_absent() -> None:
    for name in ("register_unit_client", "managed_dispatch_unit", "authority_contract"):
        try:
            importlib.import_module(f"agent_messaging_plugin.{name}")
        except ModuleNotFoundError as exc:
            _check(exc.name == f"agent_messaging_plugin.{name}", f"(s2-a) {name} is absent")
        else:
            _check(False, f"(s2-a) {name} is absent")
    with _fixture() as fx:
        _check("capture_lane_charter" not in fx.plugin.get_edge_process_definitions(), "(s2-b) capture_lane_charter is absent")
    root = ROOT / "plugins" / "agent_messaging_plugin"
    for name in ("spawn.fallback_first_turn", "spawn.charter_provenance_frame", "spawn.managed_dispatch_first_turn"):
        _check(not (root / "knowledge_base" / "driver_texts" / f"{name}.json").exists(), f"(s2-c) {name} is absent")
    import yaml

    config = yaml.safe_load((root / "plugin.yaml").read_text(encoding="utf-8"))
    _check(("p" + "solet_cli") not in str(config), "(s2-d) external register CLI config is absent")


def main() -> int:
    for case in (
        case_s2_retired_surfaces_absent,
        case_codex_workspace,
        case_s2p_host_builders_have_no_assignment_prompt,
        case_l1_removes_lane_custody,
        case_l1_retire_keeps_workspace,
        case_l1_environment,
        case_l1_published_process_schemas,
        case_s1_a_declared_parameters_are_the_generic_set,
        case_s1_b_old_fields_are_unknown_arguments,
        case_s1_c_signature_and_request_carry_no_old_field,
        case_s1_d_no_register_cli_or_git_across_the_verbs,
        case_s1_e_result_has_no_owner_or_first_go,
        case_a_generic_start_completes,
        case_b_no_register_cli_or_git,
        case_d_workspace_refused,
        case_f_model_cell_policy,
        case_f_unknown_cell_starts,
        case_g_no_session_identity,
        case_h_work_contract_verbs_are_gone,
        case_i_first_turn_rides_the_launch,
        case_j_dispatch_modules_are_gone,
        case_k_spawn_path_has_no_dispatch_contract,
        case_m_policy_fills_only_omitted_fields,
        case_n_explicit_empty_allowlist_stays_empty,
    ):
        try:
            case()
        except Exception:  # noqa: BLE001 -- one case's crash must not hide the others
            traceback.print_exc()
            _failed.append(f"{case.__name__} raised")
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
