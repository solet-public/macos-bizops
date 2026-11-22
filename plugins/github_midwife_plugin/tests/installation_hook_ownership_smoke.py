#!/usr/bin/env python3
"""Focused receipt/owner probes using the real shipped coordination entrypoints."""
# ruff: noqa: E402

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "plugins/github_midwife_plugin/src"))

from github_midwife_plugin import (
    coordination_hook_installation,  # noqa: E402
    setup_plugin_operations,  # noqa: E402
)
from github_midwife_plugin.coordination_hook_installation import (  # noqa: E402
    ReceiptSurface,
    build_receipt,
    hook_root_matches_expected,
    publish_receipt,
    receipt_matches_hook_root,
)
from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402

HOOKS = REPO / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks"
PYTHON = REPO / ".venv/bin/python3"
Check = Callable[[object, str], None]


def _owner_module() -> object:
    spec = importlib.util.spec_from_file_location("ownership_probe", HOOKS / "coordination_owner.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load coordination owner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_solet() -> str:
    return "fixture-solet"


def _receipt(home: Path, solet: str | None = None) -> Path:
    selected_solet = solet or _fixture_solet()
    profile = home / "profile"
    profile.mkdir(parents=True)
    receipt = build_receipt(
        solet_name=selected_solet,
        app_home=profile,
        plugin_selector=f"coordination-hooks@{selected_solet}",
        default_hook_root=HOOKS,
        installation_id="fixture-installation",
        surfaces=(ReceiptSurface("checkout", HOOKS, PYTHON, HOOKS / "hooks.json"),),
    )
    return publish_receipt(receipt)


def _env(receipt: Path, solet: str | None = None) -> dict[str, str]:
    return {
        "SOLET_NAME": solet or _fixture_solet(),
        "AGENT_INSTANCE_ID": "same-label",
        "AGENT_SESSION_ID": "same-session",
        "AGENT_COORDINATION_RECEIPT_PATH": str(receipt),
    }


# Every identity-bearing variable the shipped hooks read (coordination_owner's
# _managed() set and wake_waiter's transport variables) sits under one of these
# prefixes. The subprocess legs below
# must start from a base with NONE of them: this smoke runs inside real fleet
# workers (the r43 born-clone gate), whose own launch exports the full set --
# measured 2026-09-19, an ambient AGENT_CONTEXT_GAUGE_REPORTER_PATH pointing at
# the worker's real checkout redirected _selected_root() away from the fixture
# receipt and the hook correctly refused ("selected root is not recorded in
# receipt"), so no stamp was written and owner_stamps[0] raised IndexError.
# Stripping by prefix, not by name, keeps the next new identity variable from
# reopening the same hole.
_IDENTITY_ENV_PREFIXES = ("AGENT_", "SOLET_", "FLEET_")


def _hermetic_environ() -> dict[str, str]:
    """``os.environ`` with every ambient fleet-identity variable removed."""
    return {key: value for key, value in os.environ.items() if not key.startswith(_IDENTITY_ENV_PREFIXES)}


class _ProbeRuntime:
    def __init__(self, home: Path, selector: str) -> None:
        self.home = home
        self._selector = selector

    def run(self, vector: tuple[str, ...], **_: object) -> CommandOutcome:
        return CommandOutcome(0, False, 1, json.dumps([{"id": self._selector, "enabled": True}]), "")


def _probe_request(target: Path) -> AdapterRequest:
    return AdapterRequest(
        "fixture-request",
        "fixture-operation",
        "hydration::claude.install_plugin",
        "probe",
        "pre_apply",
        1,
        _fixture_solet(),
        target,
        "0" * 40,
        "sha256:" + "0" * 64,
        None,
        True,
        30,
        {},
    )


def _installer_probe_checks(check: object) -> None:
    if not callable(check):
        raise TypeError("check must be callable")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "target"
        home = root / "home"
        target.mkdir()
        home.mkdir()
        source = target / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks"
        cache = home / ".claude/plugins/cache/fixture/coordination-hooks/0.8.2"
        shutil.copytree(HOOKS.parent, source)
        shutil.copytree(HOOKS.parent, cache)
        interpreter = target / ".venv/bin/python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
        for manifest in (source / "hooks/hooks.json", cache / "hooks/hooks.json"):
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    '"command": "python3',
                    f'"command": "{interpreter}',
                ),
                encoding="utf-8",
            )
        selector = "coordination-hooks@fixture-solet"
        registry = home / ".claude/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({"plugins": {selector: [{"installPath": str(cache)}]}}), encoding="utf-8")
        request = _probe_request(target)
        runtime = _ProbeRuntime(home, selector)
        probe = setup_plugin_operations._plugin_install_probe(request, cast(Any, runtime), "claude", "/fixture/claude", "fixture-solet", selector, source / "hooks/hooks.json")
        check(probe["checkpoint_status"] == "pending", "missing receipt keeps visible matching cache pending")
        profile = target / "profile"
        profile.mkdir()
        shutil.rmtree(cache)
        shutil.copytree(source, cache)
        publish_receipt(build_receipt(solet_name=_fixture_solet(), app_home=profile, plugin_selector=selector, default_hook_root=cache / "hooks", surfaces=(ReceiptSurface("plugin_cache", cache / "hooks", interpreter, cache / "hooks/hooks.json"),)))
        check(hook_root_matches_expected(cache / "hooks", source / "hooks"), "fixture cache bytes match intended source")
        check(receipt_matches_hook_root(profile / "data/coordination-hooks/claude/installation.v1.json", cache / "hooks", solet_name=_fixture_solet(), app_home=profile, plugin_selector=selector, interpreter=interpreter), "fixture receipt matches its selected cache")
        verified = setup_plugin_operations._plugin_install_probe(request, cast(Any, runtime), "claude", "/fixture/claude", "fixture-solet", selector, source / "hooks/hooks.json")
        check(verified["checkpoint_status"] == "verified", f"current receipt and matching cache verify (got {verified['checkpoint_status']!r})")
        (source / "hooks/rotation_due_watch.py").write_text("changed", encoding="utf-8")
        upgraded = setup_plugin_operations._plugin_install_probe(request, cast(Any, runtime), "claude", "/fixture/claude", "fixture-solet", selector, source / "hooks/hooks.json")
        check(upgraded["checkpoint_status"] == "pending", "old self-consistent cache receipt fails after source upgrade")


def _stale_receipt_checks(owner: Any, check: Check) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        receipt = _receipt(root)
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["surfaces"][0]["files"]["heartbeat_report_alive.py"] = "0" * 64
        unsigned = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        payload["receipt_sha256"] = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        stale = owner.verify("wake", str(HOOKS / "wake_waiter.py"), _env(receipt))
        check(
            not stale.eligible and stale.diagnostic is not None and "republish the coordination receipt" in stale.diagnostic and "existing-install migration" in stale.diagnostic,
            "an old four-file receipt refuses loudly and names the republish migration cure",
        )


def _atomic_publication_checks(check: Check) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        alpha_home = root / "alpha"
        beta_home = root / "beta"
        alpha_home.mkdir()
        beta_home.mkdir()
        alpha_old = build_receipt(
            solet_name="alpha",
            app_home=alpha_home,
            plugin_selector="coordination-hooks@alpha",
            default_hook_root=HOOKS,
            surfaces=(ReceiptSurface("checkout", HOOKS, PYTHON, HOOKS / "hooks.json"),),
            installation_id="alpha-old",
        )
        alpha_path = publish_receipt(alpha_old)
        beta_path = publish_receipt(
            build_receipt(
                solet_name="beta",
                app_home=beta_home,
                plugin_selector="coordination-hooks@beta",
                default_hook_root=HOOKS,
                surfaces=(ReceiptSurface("checkout", HOOKS, PYTHON, HOOKS / "hooks.json"),),
                installation_id="beta-stable",
            )
        )
        old_alpha_bytes = alpha_path.read_bytes()
        old_beta_bytes = beta_path.read_bytes()
        alpha_new = build_receipt(
            solet_name="alpha",
            app_home=alpha_home,
            plugin_selector="coordination-hooks@alpha",
            default_hook_root=HOOKS,
            surfaces=(ReceiptSurface("checkout", HOOKS, PYTHON, HOOKS / "hooks.json"),),
            installation_id="alpha-new",
        )
        original_replace = coordination_hook_installation.os.replace
        try:
            coordination_hook_installation.os.replace = lambda _source, _target: (_ for _ in ()).throw(OSError("interrupted publication"))
            try:
                publish_receipt(alpha_new)
            except OSError:
                interrupted = True
            else:
                interrupted = False
        finally:
            coordination_hook_installation.os.replace = original_replace
        check(all((interrupted, alpha_path.read_bytes() == old_alpha_bytes)), "interrupted atomic receipt publication preserves alpha's old receipt")
        check(beta_path.read_bytes() == old_beta_bytes, "interrupted alpha publication leaves the other solet receipt unchanged")


def main() -> int:
    owner = _owner_module()
    failures: list[str] = []

    def check(condition: object, label: str) -> None:
        print(("PASS" if condition else "FAIL") + " " + label)
        if not condition:
            failures.append(label)

    check(
        coordination_hook_installation.HOOK_FILENAMES == ("coordination_owner.py", "rotation_due_watch.py", "wake_waiter.py"),
        "owned hook list contains only the owner, context watch and wake waiter",
    )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        receipt = _receipt(root)
        result = owner.verify("wake", str(HOOKS / "wake_waiter.py"), _env(receipt))
        check(result.eligible, "recorded owner-qualified wake waiter is eligible")
        foreign = owner.verify("wake", str(HOOKS / "wake_waiter.py"), _env(receipt, "bizopsb12"))
        check(all((not foreign.eligible, foreign.managed)), "same labels in foreign solet are refused")
        bad = dict(_env(receipt))
        bad["AGENT_CONTEXT_GAUGE_REPORTER_PATH"] = str(HOOKS / "wake_waiter.py")
        check(not owner.verify("context_watch", str(HOOKS / "rotation_due_watch.py"), bad).eligible, "wrong designated basename refuses")
        first = owner.runtime_identity_directory(result, _env(receipt))
        changed = dict(_env(receipt))
        changed["AGENT_INSTANCE_ID"] = "same/session"
        second = owner.runtime_identity_directory(result, changed)
        check(first != second, "identity marker roots use full hashes rather than separator sanitizing")
        receipt.write_text('{"schema":"solet_coordination_installation_v1","schema":"duplicate"}', encoding="utf-8")
        check(not owner.verify("wake", str(HOOKS / "wake_waiter.py"), _env(receipt)).eligible, "duplicate receipt keys refuse")

    _stale_receipt_checks(owner, check)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        receipt = _receipt(root, "foreign-solet")
        check(
            not receipt_matches_hook_root(
                receipt,
                HOOKS,
                solet_name=_fixture_solet(),
                app_home=root / "profile",
                plugin_selector="coordination-hooks@fixture-solet",
                interpreter=PYTHON,
            ),
            "foreign receipt with identical hook bytes never passes intended owner validation",
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        receipt = _receipt(root)
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["surfaces"][0]["kind"] = "unsupported-surface"
        unsigned = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        payload["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        check(
            not receipt_matches_hook_root(
                receipt,
                HOOKS,
                solet_name=_fixture_solet(),
                app_home=root / "profile",
                plugin_selector="coordination-hooks@fixture-solet",
                interpreter=PYTHON,
            ),
            "unsupported surface kind cannot pass doctor receipt validation",
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        copied_hooks = root / "hooks"
        shutil.copytree(HOOKS, copied_hooks)
        profile = root / "profile"
        profile.mkdir()
        receipt = publish_receipt(
            build_receipt(
                solet_name=_fixture_solet(),
                app_home=profile,
                plugin_selector="coordination-hooks@fixture-solet",
                default_hook_root=copied_hooks,
                surfaces=(ReceiptSurface("plugin_cache", copied_hooks, PYTHON, copied_hooks / "hooks.json"),),
            )
        )
        escaped = root / "outside.py"
        shutil.copy2(copied_hooks / "wake_waiter.py", escaped)
        (copied_hooks / "wake_waiter.py").unlink()
        (copied_hooks / "wake_waiter.py").symlink_to(escaped)
        check(
            not owner.verify("wake", str(copied_hooks / "wake_waiter.py"), _env(receipt)).eligible,
            "same-byte symlink outside the recorded hook root refuses at runtime",
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        watcher_called = root / "watcher-called"
        watcher = fake_bin / "watcher"
        watcher.write_text(f"#!/bin/sh\ntouch {watcher_called}\n", encoding="utf-8")
        watcher.chmod(0o755)
        partial_env = _hermetic_environ() | {
            "AGENT_SESSION_ID": "partial-session",
            "AGENT_WAKE_CLI": str(watcher),
            "FLEET_TRANSPORT": "watch",
        }
        partial = subprocess.run(
            [str(PYTHON), str(HOOKS / "wake_waiter.py")],
            env=partial_env,
            capture_output=True,
            text=True,
            check=False,
        )
        check(
            all((partial.returncode == 0, not watcher_called.exists(), "ownership" in partial.stderr)),
            "session-only watcher hint refuses before watcher effects",
        )

    _atomic_publication_checks(check)

    _installer_probe_checks(check)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
