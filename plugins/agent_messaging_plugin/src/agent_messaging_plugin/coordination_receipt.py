"""Whether a managed Claude session's coordination ownership receipt will be accepted.

Every coordination hook (heartbeat, wake, context gauge) refuses a managed
session whose receipt is absent, foreign, or no longer matches the hook bytes
it bound, and it refuses to stderr only, so a bad receipt turns all three off
without a sound. This module is the one place that question is asked ahead of
the hooks and the one place the answer is worded, so the spawn warning and the
sweep's gauge notice cannot disagree.

The answer comes from the hooks' own guard (``coordination_owner.verify``,
loaded from the very directory the session will run its hooks from), never from
a second reading of the receipt: a copy of its rules here would drift from it.
A guard that cannot be loaded or run is itself the finding, so the probe can
never silence the notice or the spawn it is meant to inform.
"""

from __future__ import annotations

import json
import os
import runpy
from collections.abc import Mapping
from pathlib import Path

RECEIPT_RELATIVE_PATH = Path("data/coordination-hooks/claude/installation.v1.json")
RECEIPT_ENV = "AGENT_COORDINATION_RECEIPT_PATH"

DEVELOPMENT_CHECKOUT_HINT = (
    "Publish it with `python3 -m github_midwife_plugin.checkout_receipt "
    "--repo-root <clone> --solet-name <name>`, and again after any change to "
    "the four coordination hooks or their hooks.json."
)
INSTALLED_SOLET_HINT = (
    "Publish it with `solet-manager update <name>`: its plugin step "
    "(`claude.publish_coordination_receipt`) rebuilds the receipt over the "
    "installed plugin cache. Do not use the development-checkout publisher here."
)

_CAPABILITY_FILES = (
    ("heartbeat", "heartbeat_report_alive.py"),
    ("context_watch", "rotation_due_watch.py"),
    ("wake", "wake_waiter.py"),
)


def declared_receipt_path() -> Path | None:
    """The receipt is declared from APP_HOME, never inferred from cwd."""
    app_home = os.environ.get("APP_HOME", "").strip()
    if not app_home:
        return None
    return Path(app_home) / RECEIPT_RELATIVE_PATH


def _binds_installed_cache(receipt: Path) -> bool:
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    surfaces = payload.get("surfaces") if isinstance(payload, dict) else None
    return isinstance(surfaces, list) and any(
        isinstance(surface, dict) and surface.get("kind") == "plugin_cache" for surface in surfaces
    )


def publish_hint(receipt: Path, hook_root: Path) -> str:
    """The development-checkout publisher only for a development checkout.

    A development checkout runs its hooks from ``<clone>/.claude/hooks``; an
    installed solet runs them from the shipped plugin directory and its receipt
    binds the installed cache, which that publisher would overwrite."""
    development = hook_root.name == "hooks" and hook_root.parent.name == ".claude"
    if development and not _binds_installed_cache(receipt):
        return DEVELOPMENT_CHECKOUT_HINT
    return INSTALLED_SOLET_HINT


def _refusal(receipt: Path, hook_root: Path, why: str) -> str:
    return (
        f"The coordination receipt {receipt} is not accepted for {hook_root} ({why}), so every "
        f"managed session's heartbeat, wake and context-gauge hooks refuse ownership and do "
        f"nothing. {publish_hint(receipt, hook_root)}"
    )


def _first_refusal(owner: Path, reporter: Path, receipt: Path, solet_name: str) -> str | None:
    return _guard_refusal(owner, reporter, {
        "SOLET_NAME": solet_name,
        "AGENT_INSTANCE_ID": "receipt-check",
        "AGENT_SESSION_ID": "receipt-check",
        RECEIPT_ENV: str(receipt),
        "AGENT_CONTEXT_GAUGE_REPORTER_PATH": str(reporter),
    })


def _guard_refusal(owner: Path, reporter: Path, environ: Mapping[str, str]) -> str | None:
    guard = runpy.run_path(str(owner))["verify"]
    for capability, filename in _CAPABILITY_FILES:
        verdict = guard(capability, str(reporter.parent / filename), environ)
        if not verdict.eligible:
            return str(verdict.diagnostic)
    return None


def receipt_finding(solet_name: str, reporter: Path) -> str | None:
    """Why a session spawned with ``reporter`` as its gauge hook is refused, else ``None``.

    ``reporter`` is the designated ``rotation_due_watch.py``; its directory is
    the hook root the session runs from and holds the guard that judges it."""
    receipt = declared_receipt_path()
    if receipt is None:
        return None
    owner = reporter.parent / "coordination_owner.py"
    if not owner.is_file():
        return _refusal(receipt, reporter.parent, f"the hook root has no {owner.name}, so its hooks cannot be verified")
    try:
        why = _first_refusal(owner, reporter, receipt, solet_name)
    except Exception as exc:  # noqa: BLE001 -- the guard is arbitrary checkout code; its failure is the finding
        return _refusal(receipt, reporter.parent, f"the ownership guard {owner} could not be run: {type(exc).__name__}: {exc}")
    return None if why is None else _refusal(receipt, reporter.parent, why)


def launch_finding(environ: Mapping[str, str], reporter: Path) -> str | None:
    """Why the hooks beside ``reporter`` refuse a session launched with exactly ``environ``, else ``None``.

    :func:`receipt_finding` judges the receipt alone, with a placeholder
    identity. A launcher that starts a session itself has the real environment
    in hand, so it asks the same guard with that: a missing instance id,
    session id, solet name or receipt path is named here too, before the
    session starts, instead of only on each hook's stderr after it has."""
    hook_root = reporter.parent
    owner = hook_root / "coordination_owner.py"
    try:
        why = _guard_refusal(owner, reporter, environ) if owner.is_file() else (
            f"the hook root has no {owner.name}, so its hooks cannot be verified"
        )
    except Exception as error:  # noqa: BLE001 -- the guard is arbitrary checkout code; its failure is the finding
        why = f"the ownership guard {owner} could not be run: {type(error).__name__}: {error}"
    if why is None:
        return None
    receipt = environ.get(RECEIPT_ENV, "").strip()
    remedy = f" {publish_hint(Path(receipt), hook_root)}" if receipt and "refusal:" in why else ""
    return (
        f"This session's coordination hooks under {hook_root} refuse it ({why}), so its heartbeat, "
        f"wake and context-gauge hooks will do nothing and session_context_status will read ceiling 0 "
        f"for it.{remedy}"
    )
