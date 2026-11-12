"""Offline async action, base-result and real executor coverage for macos_self_deployment_plugin."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/macos_self_deployment_plugin/src", ROOT / "plugins/agent_messaging_plugin/tests"):
    sys.path.insert(0, str(source))

from ananta.interfaces.lifecycle_result_types import RestartResult, RestartStatus, StopSelfResult, StopSelfStatus  # noqa: E402
from ananta.services.local_self_deployment_service.interfaces.public import LocalSelfDeploymentServicePublicAPI  # noqa: E402
from ananta.services.self_deployment_service.interfaces.public import SelfDeploymentServicePublicAPI  # noqa: E402
from macos_self_deployment_plugin import plugin as plugin_module  # noqa: E402
from macos_self_deployment_plugin.autostart_manager import AutostartManager  # noqa: E402
from macos_self_deployment_plugin.pending_finisher import PendingFinisher, write_pending_finisher  # noqa: E402
from macos_self_deployment_plugin.reconciliation_cutover import SwapEvidence  # noqa: E402

from plugins.agent_messaging_plugin.tests.small_plugin_job_support import Ledger, check_wrapper, measure_case, normalized, serial_worker, terminal  # noqa: E402
from plugins.macos_self_deployment_plugin.tests.reconciliation_cutover_smoke import _install_test_capability, _observation, _remove_test_capability  # noqa: E402

CASES: tuple[tuple[str, dict[str, Any]], ...] = (("swap_status", {}), ("swap_rollback", {"reason": "fixture"}))


def test_registered_owner_hook() -> None:
    """The registered boundary must have an explicit owner admission seam."""
    with fixture(plugin_module, "swap_status") as (plugin, ledger, touches):
        hook = getattr(plugin, "admit_registered_operation", None)
        assert callable(hook), "registered deployment owner admission hook is missing"
        assert hook("unselected", {}, {}) is None
        assert not ledger.rows and not touches


@contextmanager
def owner_fixture() -> Generator[tuple[Any, Ledger, dict[str, dict[str, Any]], list[int], threading.Event, Any]]:
    """Real plugin bodies; every process/router/launchctl leaf is owned."""
    with fixture(plugin_module, "swap_status") as (plugin, ledger, touches), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
        root = Path(folder).resolve()
        release = threading.Event()
        release.set()

        def effect(value: Any) -> Any:
            touches.append(threading.get_ident())
            assert release.wait(3), "owned effect was not released"
            return value

        code = root / "rel-fixture/code"
        for relative in ("ananta/a.py", "solet_cli/a.py", "solet_setup_contracts/src/a.py", "plugins/example/src/a.py"):
            path = code / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("answer = 42\n")
        code.parent.joinpath("VERSION").write_text(json.dumps({"manifest_etag": "fixture-etag"}))
        stack.enter_context(patch.dict("os.environ", {"SOLET_RELEASE_ID": "rel-fixture", "APP_HOME": str(root / "profile")}))
        stack.enter_context(patch.object(plugin, "_get_release_manager", return_value=SimpleNamespace(releases_root=root, current_release="rel-fixture")))
        client = SimpleNamespace(status=lambda: effect({"active_color": "green", "active_instance_id": "green-instance", "colors": [], "drain_entries": []}), rollback=lambda *_args: effect({"rolled_back": True, "active_color": "blue"}))
        stack.enter_context(patch.object(plugin, "_require_client", return_value=client))
        stack.enter_context(patch.object(plugin, "_collect_set_active_targets", return_value=()))
        stack.enter_context(patch.object(plugin_module.process_identity, "start_token", return_value="fixture-token"))

        def restart(**kwargs: Any) -> RestartResult:
            return effect(RestartResult(status=RestartStatus.QUEUED, restart_action_id="fixture-finisher", message="owned swap outcome", reason=kwargs["reason"], expected_etag=kwargs["expected_etag"], dry_run=kwargs.get("dry_run", False), probe={"ok": True, "fixture": True}))

        stack.enter_context(patch.object(plugin, "_require_orchestrator", return_value=SimpleNamespace(restart=restart, rollback_release=restart)))
        stopped = StopSelfResult(status=StopSelfStatus.SUCCESS, reason="fixture", duration_seconds=0.0, stopped_at="", backend_action_id="fixture-watchdog", dry_run=False, message="owned stop")
        stack.enter_context(patch.object(plugin_module.stop_self_runner, "run", side_effect=lambda **_kwargs: effect(stopped)))
        finisher = root / "pending-finisher.json"

        def reset_finisher() -> None:
            write_pending_finisher(finisher, PendingFinisher(prior_pid=701, prior_instance_id="fixture-prior", prior_color="blue", candidate_release_id="rel-fixture", prior_start_token="fixture-token"))

        stack.enter_context(patch.object(plugin_module, "pending_finisher_path", return_value=finisher))
        stack.enter_context(patch.object(plugin_module, "_runtime_dir", return_value=root))
        stack.enter_context(patch.object(plugin_module.drain_sentinel, "held", side_effect=lambda _name: nullcontext()))
        stack.enter_context(patch.object(plugin, "_signal_and_wait", side_effect=lambda *_args, **_kwargs: effect("prior_already_gone")))
        stack.enter_context(patch.object(plugin, "_safe_unregister", side_effect=lambda _instance: effect("unregistered")))
        autostart = AutostartManager(solet_name="fixture", project_root=root, plist_dir=root / "agents", releases_root=root, log_dir=root / "logs")
        plugin.set_autostart_manager(autostart)
        stack.enter_context(patch("macos_self_deployment_plugin.autostart_manager.subprocess.run", side_effect=lambda args, **_kwargs: effect(subprocess.CompletedProcess(args, 1, "", "absent"))))
        stack.enter_context(patch.object(plugin, "_reconciliation_observation", side_effect=lambda: effect(_observation())))
        stack.enter_context(patch.object(plugin, "_reconciliation_swap", side_effect=lambda **_kwargs: effect(SwapEvidence(status="queued", reason_code="none"))))
        prior = _install_test_capability()
        stack.callback(_remove_test_capability, *prior)
        parameters: dict[str, dict[str, Any]] = {
            "attest_runtime_code": {"verification_modules": [], "reconciliation_id": "rec_fixture"},
            "complete_swap": {"prior_pid": 701, "prior_instance_id": "fixture-prior", "prior_color": "blue"},
            "cutover_release": {
                "reconciliation_id": "rec_fixture",
                "expected_source_surface_sha256": "sha256:" + "a" * 64,
                "expected_release_surface_sha256": "sha256:" + "b" * 64,
                "expected_manifest_etag": "etag-1",
                "expected_current_release_id": "rel-1",
                "expected_active_instance_id": "inst-1",
                "expected_active_start_token": "tok-1",
                "dry_run": True,
            },
            "install_autostart": {"dry_run": True},
            "rollback_release": {"reason": "fixture", "expected_etag": "fixture-etag", "expected_current_release": "rel-fixture"},
            "restart_with_manifest": {"new_manifest": {}, "expected_etag": "fixture-etag", "reason": "fixture", "dry_run": True},
            "status_autostart": {},
            "stop_self": {"reason": "fixture"},
            "swap_rollback": {"reason": "fixture"},
            "swap_status": {},
            "uninstall_autostart": {"dry_run": True},
        }
        reset_finisher()
        yield plugin, ledger, parameters, touches, release, reset_finisher
        release.set()
        thread = plugin._jobs._thread
        if thread is not None:
            thread.join(3)
            assert not thread.is_alive(), "owned worker did not retire"


def _original_outcome(plugin: Any, name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Read the original full body without submitting a bookkeeping job."""
    if name in {"swap_status", "swap_rollback"}:
        return getattr(plugin, f"_run_{name}")(parameters)
    return getattr(plugin, f"{name}_action")(parameters, {})


def test_owner_service_aliases_and_original_outcomes() -> None:
    """All eleven actual service selections share their original plugin body.

    U1 owns outer registered dispatch activation; this exercises its agreed
    owner hook using each actual interface metadata selection and bound owner.
    """
    golden = json.loads(Path(__file__).with_name("async_actions_base.json").read_text())
    aliases = golden["owner_service_aliases"]
    assert len(aliases) == 11
    for provider, name in aliases:
        api = SelfDeploymentServicePublicAPI if provider == "self_deployment_service" else LocalSelfDeploymentServicePublicAPI
        declaration = getattr(api, name)._service_interface_metadata
        assert declaration.provider == provider and declaration.name == name
        with owner_fixture() as (plugin, ledger, parameters, touches, release, reset):
            expected = _original_outcome(plugin, name, parameters[name])
            reset()
            touches.clear()
            release.clear()
            caller = threading.get_ident()
            started = time.perf_counter()
            receipt = plugin.admit_registered_operation(name, parameters[name], {"session_id": "fixture-session", "flow_id": "fixture-flow"})
            elapsed_ms = 1000 * (time.perf_counter() - started)
            assert receipt["data"]["status"] == "queued" and len(ledger.rows) == 1, receipt
            assert caller not in touches, (name, touches)
            assert elapsed_ms < 50, (name, elapsed_ms)
            release.set()
            row = terminal(plugin, ledger, receipt["data"]["job_id"])
            assert normalized(row["result"]) == normalized(expected), (name, row, expected)
            assert touches and caller not in touches, (name, touches)
            print(f"PASS owner alias {provider}.{name}: {elapsed_ms:.3f}ms persisted_receipt=true same_body=true jobs=1")


def test_owner_order_context_and_terminal_failure() -> None:
    """Across keys, preserve context and never replay an uncertain outcome."""
    marker: ContextVar[str] = ContextVar("deployment-owner-fixture", default="absent")
    with fixture(plugin_module, "swap_status") as (plugin, ledger, _touches):
        entered, release = threading.Event(), threading.Event()
        calls: list[tuple[str, str]] = []

        def first(_params: dict[str, Any], _state: dict[str, Any]) -> dict[str, Any]:
            calls.append(("first", marker.get()))
            entered.set()
            assert release.wait(3)
            return {"action_status": "completed", "data": {"first": True}}

        def second(_params: dict[str, Any], _state: dict[str, Any]) -> dict[str, Any]:
            calls.append(("second", marker.get()))
            return {"action_status": "error", "error": {"code": "fixture.failure"}, "data": {"evidence": "preserved"}}

        with patch.object(plugin, "status_autostart_action", side_effect=first), patch.object(plugin, "uninstall_autostart_action", side_effect=second):
            marker.set("first-context")
            one = plugin.admit_registered_operation("status_autostart", {}, {"session_id": "s", "flow_id": "f"})
            assert entered.wait(2)
            marker.set("second-context")
            two = plugin.admit_registered_operation("uninstall_autostart", {}, {"session_id": "s", "flow_id": "f"})
            assert calls == [("first", "first-context")]
            release.set()
            terminal(plugin, ledger, one["data"]["job_id"])
            failed = terminal(plugin, ledger, two["data"]["job_id"])
            assert failed["error"] == {"code": "fixture.failure"}
            assert failed["result"]["data"] == {"evidence": "preserved"}
            assert calls == [("first", "first-context"), ("second", "second-context")]
        original_update = ledger.update_job

        def reject_terminal(job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
            if updates.get("status") in {"completed", "error"}:
                return {"action_status": "error", "error": {"code": "fixture.terminal_write"}}
            return original_update(job_id, updates)

        with patch.object(ledger, "update_job", side_effect=reject_terminal), patch.object(plugin, "status_autostart_action", side_effect=second):
            receipt = plugin.admit_registered_operation("status_autostart", {}, {"session_id": "s", "flow_id": "f"})
            thread = plugin._jobs._thread
            if thread is not None:
                thread.join(3)
            assert ledger.rows[receipt["data"]["job_id"]]["status"] == "processing"
            before = list(calls)
            plugin._jobs._drain()
            assert calls == before, "failed terminal persistence must not replay effects"


@contextmanager
def fixture(module: ModuleType, _verb: str, *, delay: float = 0.0, failed: bool = False) -> Generator[tuple[Any, Ledger, list[int]]]:
    ledger, touches = Ledger(), []

    def transport(result: Any) -> Any:
        touches.append(threading.get_ident())
        time.sleep(delay)
        if failed:
            raise RuntimeError("fixture transport failure")
        return result

    plugin = module.MacosSelfDeploymentPlugin()
    plugin.logger = logging.getLogger("async-action-fixture")
    state = None
    plugin.orchestrator_ref = SimpleNamespace(async_job_manager=ledger, get_service=lambda _name: state)
    with ExitStack() as stack:
        stack.enter_context(patch.dict("os.environ", {"SOLET_NAME": "fixture"}))
        client = SimpleNamespace(status=lambda: transport({"active_color": "green", "active_instance_id": "green-instance", "colors": [], "drain_entries": []}), rollback=lambda *_args: {"rolled_back": True, "active_color": "blue"})
        stack.enter_context(patch.object(plugin, "_require_client", return_value=client))
        stack.enter_context(patch.object(plugin, "_resolve_prior_target", return_value=("blue", "blue-instance")))
        yield plugin, ledger, touches


def test_execute_paths() -> None:
    goldens = json.loads(Path(__file__).with_name("async_actions_base.json").read_text())
    for verb, params in CASES:
        for failed in (False, True):
            measure_case(plugin_module, verb, params, failed, goldens, fixture)


def test_wrapper_selection() -> None:
    for verb, params in CASES:
        with fixture(plugin_module, verb) as (plugin, ledger, _touches):
            check_wrapper(plugin, ledger, verb, params)


def test_serial_worker_lifecycle() -> None:
    with fixture(plugin_module, CASES[0][0]) as (plugin, ledger, _touches):
        serial_worker(plugin, ledger)


def test_public_service_faces_unchanged() -> None:
    goldens = json.loads(Path(__file__).with_name("async_actions_base.json").read_text())
    for verb in ("swap_status", "swap_rollback"):
        with fixture(plugin_module, verb) as (plugin, ledger, _touches):
            outcome = plugin.swap_status() if verb == "swap_status" else plugin.swap_rollback("fixture")
            assert outcome == goldens["public_service"][verb]
            assert not ledger.rows, "The synchronous service face must retain the base result"


if __name__ == "__main__":
    test_registered_owner_hook()
    test_owner_service_aliases_and_original_outcomes()
    test_owner_order_context_and_terminal_failure()
    test_execute_paths()
    test_wrapper_selection()
    test_serial_worker_lifecycle()
    test_public_service_faces_unchanged()
    print("PASS macos_self_deployment_plugin async action coverage")
