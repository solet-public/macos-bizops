#!/usr/bin/env python3
"""D11 smoke — router-less bridge port discovery (no pytest).

Verifies the D11 ruling
(``workbench/2026-07-13_d11_bridge_port_discovery_routerless_ruling.md``):
``<name>.bridge.port`` has exactly one writer per solet topology —
the router when one is declared, ``agent_messaging_plugin`` itself when
one is not. R6 scenarios:

  (i)   router-less declared set -> ``AgentMessagingPlugin._router_is_declared``
        is False and ``write_routerless_bridge_port_file`` produces a file
        containing the bound port.
  (ii)  router-declared set -> ``_router_is_declared`` is True; the
        start_interface conditional (mirrored here) never writes, even
        when the file is absent (R4 — a missing file in router topology
        is the pre-install bootstrap window, not a gap to fill).
  (iii) the generic guard still raises for both write_port_file and
        remove_port_file with ``service_name='bridge'`` (unchanged by
        D11; also covered incidentally by
        ``plugins/macos_self_deployment_plugin/tests/blue_green_router/
        slice3_no_hardcoded_bands_smoke.py``, duplicated narrowly here so
        this module is a self-contained D11 regression guard).
  (iv)  a pre-existing stale file is overwritten on the next routerless
        write (R3 self-heal for port re-roll).
  (v)   every port file it publishes is mode 0o600 from creation (no chmod
        after the rename), and a failed rename leaves the old file intact.

Plus the R1 fail-loud contract for ``_router_is_declared``: an absent
``orchestrator_ref``, an absent ``APP_HOME``, and an absent
``manifest.yaml`` must all raise rather than guess.

Run from repo root:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/d11_routerless_bridge_port_discovery_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.runtime.port_manager import (  # noqa: E402
    get_runtime_dir,
    read_port_file,
    remove_port_file,
    write_port_file,
    write_routerless_bridge_port_file,
)

from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

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


class _FakeOrchestrator:
    """Minimal double exposing only what ``_router_is_declared`` reads."""

    def __init__(self, app_home: str | None) -> None:
        if app_home is not None:
            self.APP_HOME = app_home


def _write_manifest(app_home: Path, plugins: list[str]) -> None:
    config_dir = app_home / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    lines = ["plugins:"] + [f"  - {name}" for name in plugins]
    (config_dir / "manifest.yaml").write_text("\n".join(lines) + "\n")


def _new_plugin(app_home: Path | None) -> AgentMessagingPlugin:
    plugin: AgentMessagingPlugin = AgentMessagingPlugin()
    if app_home is not None:
        plugin.orchestrator_ref = _FakeOrchestrator(str(app_home))  # type: ignore[attr-defined]
    return plugin


def _scenario_routerless_declared_set_writes_bound_port(solet: str) -> None:
    print("Scenario (i): router-less declared set -> file written with bound port")
    with tempfile.TemporaryDirectory(prefix="d11_smoke_home_") as home:
        app_home = Path(home)
        _write_manifest(app_home, ["agent_messaging_plugin"])
        plugin = _new_plugin(app_home)

        declared = plugin._router_is_declared()  # noqa: SLF001
        _check(declared is False, "router NOT in declared set -> _router_is_declared() False")

        if not declared:
            write_routerless_bridge_port_file(54321, solet)

        read_back = read_port_file("bridge", solet)
        _check(read_back is not None and read_back > 0, "bridge port file is non-empty")
        _check(read_back == 54321, f"bridge port file contains the bound port (got {read_back!r})")


def _scenario_router_declared_set_never_writes(solet: str) -> None:
    print("\nScenario (ii): router-declared set -> never writes, even when file absent (R4)")
    with tempfile.TemporaryDirectory(prefix="d11_smoke_home_") as home:
        app_home = Path(home)
        _write_manifest(
            app_home,
            ["macos_self_deployment_plugin", "agent_messaging_plugin"],
        )
        plugin = _new_plugin(app_home)

        declared = plugin._router_is_declared()  # noqa: SLF001
        _check(declared is True, "router IS in declared set -> _router_is_declared() True")

        pre_existing = read_port_file("bridge", solet)
        _check(pre_existing is None, "precondition: no bridge port file exists yet")

        if not declared:
            write_routerless_bridge_port_file(11111, solet)  # pragma: no cover — must not run

        post = read_port_file("bridge", solet)
        _check(post is None, "file STILL absent — the plugin never fills the router's gap")


def _scenario_generic_guard_still_raises(solet: str) -> None:
    print("\nScenario (iii): generic write_port_file/remove_port_file('bridge') still raise")
    try:
        write_port_file(8765, "bridge", solet)
    except ValueError as exc:
        _check("forbidden" in str(exc).lower(), f"write_port_file raises forbidden (msg={exc})")
    else:
        _check(False, "write_port_file('bridge', ...) did NOT raise")

    try:
        remove_port_file("bridge", solet)
    except ValueError as exc:
        _check("forbidden" in str(exc).lower(), f"remove_port_file raises forbidden (msg={exc})")
    else:
        _check(False, "remove_port_file('bridge', ...) did NOT raise")


def _scenario_stale_file_overwritten_on_next_write(solet: str) -> None:
    print("\nScenario (iv): pre-existing stale file is overwritten (R3 self-heal)")
    write_routerless_bridge_port_file(40001, solet)
    stale = read_port_file("bridge", solet)
    _check(stale == 40001, "precondition: stale port on disk")

    write_routerless_bridge_port_file(40002, solet)
    healed = read_port_file("bridge", solet)
    _check(healed == 40002, f"rewrite on next start self-heals to the new port (got {healed!r})")
    _check(healed != stale, "healed value differs from the stale value")


_PRIVATE_PORT = 54321
_STALE_PORT_TEXT = "99999"


class _ChmodRecorder:
    """Stand-in for ``os.chmod`` that records the mode the final file has when chmod reaches it."""

    def __init__(self, target: str) -> None:
        self._target = target
        self._real_chmod = os.chmod
        self.modes_at_chmod: list[int] = []

    def __call__(self, path: str | os.PathLike[str], mode: int, **kwargs: Any) -> None:
        if os.fspath(path) == self._target and os.path.exists(self._target):
            self.modes_at_chmod.append(os.stat(self._target).st_mode & 0o777)
        self._real_chmod(path, mode, **kwargs)


def _check_private_publish(
    label: str,
    final_path: Path,
    write: Callable[[], object],
    *,
    pre_existing: bool,
) -> None:
    runtime = final_path.parent
    if pre_existing:
        final_path.write_text(_STALE_PORT_TEXT, encoding="utf-8")
        final_path.chmod(0o644)
    else:
        _check(not final_path.exists(), f"{label}: precondition, no file yet")
    before = set(os.listdir(runtime))
    recorder = _ChmodRecorder(str(final_path))
    try:
        with mock.patch("os.chmod", new=recorder):
            write()
    except OSError as exc:
        _check(False, f"{label}: writer completes (raised {exc!r})")
        return
    after = set(os.listdir(runtime))
    bad = [mode for mode in recorder.modes_at_chmod if mode != 0o600]
    _check(not bad, f"{label}: no chmod saw the final file below 0o600 (seen {[oct(m) for m in bad]})")
    mode = final_path.stat().st_mode & 0o777
    _check(mode == 0o600, f"{label}: final mode 0o600 (got {mode:#o})")
    _check(
        final_path.read_text(encoding="utf-8") == str(_PRIVATE_PORT),
        f"{label}: content is the port",
    )
    _check(after == before | {final_path.name}, f"{label}: no temp file left behind")


def _scenario_port_files_private_from_creation(solet: str) -> None:
    print("\nScenario (v): port files are published at mode 0o600 from creation (umask 022)")
    runtime = get_runtime_dir(solet)
    bridge = runtime / f"{solet}.bridge.port"
    rest = runtime / f"{solet}.rest.port"
    previous_umask = os.umask(0o022)
    try:
        _check_private_publish(
            "bridge, fresh",
            bridge,
            lambda: write_routerless_bridge_port_file(_PRIVATE_PORT, solet),
            pre_existing=False,
        )
        _check_private_publish(
            "bridge, over a 0o644 file",
            bridge,
            lambda: write_routerless_bridge_port_file(_PRIVATE_PORT, solet),
            pre_existing=True,
        )
        _check_private_publish(
            "rest, fresh",
            rest,
            lambda: write_port_file(_PRIVATE_PORT, "rest", solet),
            pre_existing=False,
        )
        _check_private_publish(
            "rest, over a 0o644 file",
            rest,
            lambda: write_port_file(_PRIVATE_PORT, "rest", solet),
            pre_existing=True,
        )
        # Failure leg: the rename fails, so the writer must raise, the old file
        # must keep its bytes, and no temp file may be left behind.
        before_bytes = bridge.read_bytes()
        before_names = set(os.listdir(runtime))
        raised = False
        with mock.patch("os.replace", side_effect=OSError("injected rename failure")):
            try:
                write_routerless_bridge_port_file(_PRIVATE_PORT + 1, solet)
            except OSError:
                raised = True
        _check(raised, "failed rename: the writer raises")
        _check(
            bridge.read_bytes() == before_bytes,
            "failed rename: the old bridge file keeps its bytes",
        )
        _check(set(os.listdir(runtime)) == before_names, "failed rename: no temp file remains")
    finally:
        os.umask(previous_umask)


def _scenario_predicate_fails_loud() -> None:
    print("\nScenario (R1 fail-loud): undeterminable router presence never guesses")

    plugin_no_orchestrator = AgentMessagingPlugin()
    try:
        plugin_no_orchestrator._router_is_declared()  # noqa: SLF001
    except RuntimeError as exc:
        _check("orchestrator_ref" in str(exc), f"missing orchestrator_ref raises (msg={exc})")
    else:
        _check(False, "missing orchestrator_ref did NOT raise")

    plugin_no_app_home = AgentMessagingPlugin()
    plugin_no_app_home.orchestrator_ref = _FakeOrchestrator(app_home=None)  # type: ignore[attr-defined]
    try:
        plugin_no_app_home._router_is_declared()  # noqa: SLF001
    except RuntimeError as exc:
        _check("APP_HOME" in str(exc), f"missing APP_HOME raises (msg={exc})")
    else:
        _check(False, "missing APP_HOME did NOT raise")

    with tempfile.TemporaryDirectory(prefix="d11_smoke_no_manifest_") as home:
        empty_app_home = Path(home)
        plugin_no_manifest = _new_plugin(empty_app_home)
        try:
            plugin_no_manifest._router_is_declared()  # noqa: SLF001
        except RuntimeError as exc:
            _check("manifest.yaml" in str(exc), f"missing manifest.yaml raises (msg={exc})")
        else:
            _check(False, "missing manifest.yaml did NOT raise")


def main() -> int:
    print("D11 smoke: router-less bridge port discovery\n")
    # Each scenario gets its own solet name — they share ONE
    # XDG_RUNTIME_DIR, and a shared name would let scenario (i)'s write
    # leak into scenario (ii)'s "no file exists yet" precondition.
    with tempfile.TemporaryDirectory(prefix="d11_smoke_runtime_") as runtime_dir:
        prior_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = runtime_dir
        try:
            _scenario_routerless_declared_set_writes_bound_port("d11-smoke-i")
            _scenario_router_declared_set_never_writes("d11-smoke-ii")
            _scenario_generic_guard_still_raises("d11-smoke-iii")
            _scenario_stale_file_overwritten_on_next_write("d11-smoke-iv")
            _scenario_predicate_fails_loud()
            _scenario_port_files_private_from_creation("d11-smoke-v")
        finally:
            if prior_xdg is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = prior_xdg

    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
