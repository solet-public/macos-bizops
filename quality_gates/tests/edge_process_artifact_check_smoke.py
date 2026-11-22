#!/usr/bin/env python3
"""Regression coverage for quality_gates/edge_process_artifact_check.py.

Each check builds a one-plugin fixture tree from string constants in a temporary
directory and runs the check as a subprocess against it. Nothing under the
repository's own ``plugins/`` is read, so the smoke also runs in a seed that
ships without any particular plugin.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK_REL = "quality_gates/edge_process_artifact_check.py"
CHECK = ROOT / CHECK_REL

_FAILURES: list[str] = []

_PYPROJECT = """[project]
name = "fx_edge_plugin"
version = "1.0.0"

[project.entry-points."ananta.plugins"]
fx_edge_plugin = "fx_edge_plugin.plugin:FxEdgePlugin"
"""

_PLUGIN_HEAD = """from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

_PLUGIN_NAME = "fx_edge_plugin"
_RESULT_TYPE = "fx_edge_ping_result"


def _ping_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Liveness probe payload.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'alive'."),
            "plugin_name": ParameterMetadata(
                type=ParameterType.STRING,
                description="Always 'fx_edge_plugin'.",
            ),
        },
    )


class FxEdgePlugin(PluginBase, EdgeProcessProvider):
    name: str = _PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.name = _PLUGIN_NAME

    def prepare_for_readiness(self) -> None:
        self.set_ready()

"""

_DEFINITIONS_DECLARED = """    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "ping": EdgeProcessDefinition(
                name="ping",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=_RESULT_TYPE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
        }

"""

_DEFINITIONS_EMPTY = """    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {}

"""

_DECORATOR = """    @platform_process(
        name="ping",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="Liveness probe payload: status + plugin_name.",
        return_value_schema=_ping_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=_RESULT_TYPE,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
"""

_PING_METHOD = """    def ping(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        del params, state
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": {"status": "alive", "plugin_name": _PLUGIN_NAME},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
"""

_PLUGIN_BROKEN = 'raise RuntimeError("fixture")\n'

_ALLOWLIST_PING = "C2.2::fx_edge_plugin::ping  # owner: iss_c7634ec3 reason: fixture expires: 2099-12-31\n"

_COMPLETE_SUMMARY = "EDGE ARTIFACTS: 1 processes in 1 plugins measured, 0 incomplete, 0 tolerated, 0 advisories"


def check(name: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        _FAILURES.append(name)


def _plugin_source(*, declared: bool = True, decorated: bool = True) -> str:
    return _PLUGIN_HEAD + (_DEFINITIONS_DECLARED if declared else _DEFINITIONS_EMPTY) + (_DECORATOR if decorated else "") + _PING_METHOD


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_fixture(root: Path, plugin_source: str, *, with_json: bool = True) -> None:
    plugin_dir = root / "plugins" / "fx_edge_plugin"
    _write(plugin_dir / "pyproject.toml", _PYPROJECT)
    _write(plugin_dir / "src" / "fx_edge_plugin" / "__init__.py", "")
    _write(plugin_dir / "src" / "fx_edge_plugin" / "plugin.py", plugin_source)
    if with_json:
        _write(plugin_dir / "knowledge_base" / "processes" / "ping.json", "{}\n")


def _run_check(root: Path, *args: str) -> tuple[int, list[str]]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "ananta" / "src"), *filter(None, [env.get("PYTHONPATH")])])
    done = subprocess.run(
        [sys.executable, str(CHECK), "--repo-root", str(root), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return done.returncode, done.stdout.splitlines()


def _last(lines: list[str]) -> str:
    return lines[-1] if lines else ""


def test_complete_plugin_exits_zero() -> None:
    with tempfile.TemporaryDirectory(prefix="edge_check_") as tmp:
        root = Path(tmp)
        _build_fixture(root, _plugin_source())
        code, lines = _run_check(root)
        check("complete plugin exits 0", code == 0 and _last(lines) == _COMPLETE_SUMMARY)


def test_missing_declaration_is_named() -> None:
    with tempfile.TemporaryDirectory(prefix="edge_check_") as tmp:
        root = Path(tmp)
        _build_fixture(root, _plugin_source(declared=False))
        code, lines = _run_check(root)
        named = "MISSING plugin::fx_edge_plugin::ping: declaration" in lines
        check("missing declaration exits 1 and is named", code == 1 and named)


def test_missing_kb_json_is_named() -> None:
    with tempfile.TemporaryDirectory(prefix="edge_check_") as tmp:
        root = Path(tmp)
        _build_fixture(root, _plugin_source(), with_json=False)
        code, lines = _run_check(root)
        named = "MISSING plugin::fx_edge_plugin::ping: kb_json" in lines
        check("missing kb json exits 1 and is named", code == 1 and named)


def test_allowlisted_kb_json_is_tolerated() -> None:
    with tempfile.TemporaryDirectory(prefix="edge_check_") as tmp:
        root = Path(tmp) / "tree"
        _build_fixture(root, _plugin_source(), with_json=False)
        allowlist = Path(tmp) / "allowlist.txt"
        _write(allowlist, _ALLOWLIST_PING)
        code, lines = _run_check(root, "--allowlist", str(allowlist))
        tolerated = "TOLERATED plugin::fx_edge_plugin::ping: kb_json (allowlisted C2.2)" in lines
        check("allowlisted kb json is tolerated", code == 0 and tolerated)


def test_missing_decorator_is_named() -> None:
    with tempfile.TemporaryDirectory(prefix="edge_check_") as tmp:
        root = Path(tmp)
        _build_fixture(root, _plugin_source(decorated=False))
        code, lines = _run_check(root)
        named = "MISSING plugin::fx_edge_plugin::ping: decorator" in lines
        check("missing decorator exits 1 and is named", code == 1 and named)


def test_unmeasurable_plugin_is_an_advisory() -> None:
    with tempfile.TemporaryDirectory(prefix="edge_check_") as tmp:
        root = Path(tmp)
        _build_fixture(root, _PLUGIN_BROKEN)
        code, lines = _run_check(root)
        advisory = any(line.startswith("ADVISORY fx_edge_plugin:") for line in lines)
        counted = "0 incomplete, 0 tolerated, 1 advisories" in _last(lines)
        check("unmeasurable plugin is an advisory", code == 0 and advisory and counted)


def test_paths_select_only_their_plugin() -> None:
    with tempfile.TemporaryDirectory(prefix="edge_check_") as tmp:
        root = Path(tmp)
        _build_fixture(root, _plugin_source())
        other_code, other = _run_check(root, "ananta/src/x.py")
        own_code, own = _run_check(root, "plugins/fx_edge_plugin/src/fx_edge_plugin/plugin.py")
        check(
            "paths select only their plugin",
            other_code == 0 and "0 processes in 0 plugins" in _last(other) and own_code == 0 and "1 processes in 1 plugins" in _last(own),
        )


def main() -> int:
    print(f"Smoke for {CHECK_REL}\n")
    test_complete_plugin_exits_zero()
    test_missing_declaration_is_named()
    test_missing_kb_json_is_named()
    test_allowlisted_kb_json_is_tolerated()
    test_missing_decorator_is_named()
    test_unmeasurable_plugin_is_an_advisory()
    test_paths_select_only_their_plugin()
    if _FAILURES:
        print(f"\nFAILED: {_FAILURES}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
