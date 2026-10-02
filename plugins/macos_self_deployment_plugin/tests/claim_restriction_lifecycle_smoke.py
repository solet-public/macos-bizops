#!/usr/bin/env python3
"""Smoke: a booting colour claims only its own rows until the router names it active (iss_faf5802c).

On the 2026-10-01 deploy of 6ebccd2de the booting candidate, not yet
router-active and with no bridge, claimed the live colour's ``peer_inbox`` and
failed it ``bridge.not_running``. The fix restricts an instance's poller to rows
pinned to itself (``EventOrchestrator.claims_own_rows_only``) from
``prepare_for_readiness`` until the router names the instance active. The core
claim semantics are proved by
``ananta/tests/core/actions/candidate_starting_actions_pin_smoke.py``; this smoke
proves the plugin's half of the lifecycle.

Asserts:

  (1) ``prepare_for_readiness`` turns the restriction on (read from its source:
      the method waits on a live router socket, so it is not booted here);
  (2) the real heartbeat activation check (``_notify_post_registration_if_active``
      with the plugin's own ``_start_post_registration_work`` as callback) keeps
      the restriction while the router names ANOTHER instance active, and
      releases it once the router names this instance;
  (3) the release is idempotent across healthy heartbeats and logs once.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/claim_restriction_lifecycle_smoke.py
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from macos_self_deployment_plugin import heartbeat_lifecycle  # noqa: E402
from macos_self_deployment_plugin import plugin as plugin_module  # noqa: E402

SELF = "solet-green-7edf1bfc"
LIVE = "solet-blue-0fea479c"

_failures: list[str] = []


def _check(condition: object, label: str) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL {label}")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _Router:
    """``RouterClient.status`` stand-in: names whichever instance is active."""

    def __init__(self, active: str) -> None:
        self.active = active

    def status(self) -> dict[str, Any]:
        return {"active_instance_id": self.active, "active_color": "blue"}


def _is_restrict_call(node: ast.AST) -> bool:
    """``_set_claims_own_rows_only(<orchestrator>, True, ...)``."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return False
    if node.func.id != "_set_claims_own_rows_only" or len(node.args) < 2:
        return False
    flag = node.args[1]
    return isinstance(flag, ast.Constant) and flag.value is True


def _readiness_restrict_calls() -> int:
    """How many restrict calls ``prepare_for_readiness`` makes, read from the source."""
    source = Path(plugin_module.__file__).read_text(encoding="utf-8")
    readiness = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "prepare_for_readiness"
    )
    return sum(1 for node in ast.walk(readiness) if _is_restrict_call(node))


def _plugin(orchestrator: Any, logger: logging.Logger) -> Any:
    deploy_plugin: Any = plugin_module.MacosSelfDeploymentPlugin.__new__(
        plugin_module.MacosSelfDeploymentPlugin,
    )
    deploy_plugin.orchestrator_ref = orchestrator
    deploy_plugin.logger = logger
    return deploy_plugin


def test_readiness_restricts() -> None:
    print("\n[1] prepare_for_readiness restricts the instance to its own rows")
    _check(_readiness_restrict_calls() == 1, "prepare_for_readiness makes one restrict call")
    orch: Any = SimpleNamespace(claims_own_rows_only=False)
    plugin_module._set_claims_own_rows_only(orch, True, logging.getLogger("restrict"))
    _check(orch.claims_own_rows_only is True, "the restrict call sets the orchestrator flag")


def test_router_activation_releases() -> None:
    print("\n[2]+[3] released only once the router names this instance active")
    logger = logging.getLogger("claim_restriction_lifecycle_smoke")
    capture = _Capture()
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    # ``_post_registration_work_started`` keeps the core startup thread from
    # spawning; this smoke is about the restriction only.
    orch: Any = SimpleNamespace(claims_own_rows_only=True, _post_registration_work_started=True)
    deploy_plugin = _plugin(orch, logger)
    router = _Router(active=LIVE)

    def tick() -> None:
        heartbeat_lifecycle._notify_post_registration_if_active(
            client=router,  # type: ignore[arg-type]
            self_instance_id=SELF,
            logger=logger,
            callback=deploy_plugin._start_post_registration_work,
        )

    tick()
    _check(
        orch.claims_own_rows_only is True,
        "while the router names the live instance, the candidate stays restricted",
    )
    router.active = SELF
    tick()
    _check(orch.claims_own_rows_only is False, "once the router names this instance, it is released")
    tick()
    tick()
    released = [m for m in capture.messages if "CLAIM_RESTRICTION off" in m]
    _check(len(released) == 1, f"the release logged once across healthy ticks ({len(released)})")
    _check(orch.claims_own_rows_only is False, "and stays released")


def main() -> int:
    print("Claim-restriction lifecycle smoke (iss_faf5802c)")
    test_readiness_restricts()
    test_router_activation_releases()
    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: restricted from readiness, released by router activation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
