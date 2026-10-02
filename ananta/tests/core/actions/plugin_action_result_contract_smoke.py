#!/usr/bin/env python3
"""Smoke: every plugin result carries the five ActionResult fields (iss_dae31745).

``ActionProcessor._execute_plugin_action`` refuses a plugin result that omits any of
``action_status``, ``data``, ``actions``, ``error``, ``timestamp`` (and requires ``data``
to be a dict and ``actions`` a list) with ``PLUGIN CONTRACT VIOLATION``.
``openai_embeddings_plugin.success_result`` omitted ``error`` and its ``error_result``
omitted ``data`` and ``actions``, so ``list_models_action``, ``generate_embeddings_action``
and ``get_embedding_dimension_action`` all failed at dispatch; the same shape was copied
into ``titanv2_embeddings_plugin``, both state-management result helpers,
``coreai_embeddings_plugin.failure``, and several inline literals.

This smoke drives each fixed site through the REAL validator (a real ``ActionProcessor``
whose plugin manager hands back the real plugin object), success and failure for each:

  (A) real ``@platform_process`` verbs with only the I/O seam stubbed: openai and titan
      embeddings (all three verbs), salesforce ``probe_cli``, postgres and rds
      ``list_namespaces_action``;
  (B) sites with no dispatchable verb, handed to the same validator through a one-method
      adapter plugin: coreai embeddings, the thinking planning-submit result, and the
      thinking / knowledge / g_suite lifecycle hooks;
  (C) a source audit of every ``plugins/*/src`` literal that builds an ActionResult
      (``return {...}``, ``cast(ActionResult, {...})``, ``ActionResult(...)``), for the
      sites a smoke cannot drive (provider health results, the playbook finalizer, the
      flow-complete result). A site that is not a plugin result is named in
      ``_NOT_A_PLUGIN_RESULT`` with its reason.

Project policy: no pytest. Exits 0 on success, 1 on any failure.

Run:
    .venv/bin/python3 ananta/tests/core/actions/plugin_action_result_contract_smoke.py
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
for _plugin_src in sorted((REPO_ROOT / "plugins").glob("*/src")):
    sys.path.insert(0, str(_plugin_src))
os.environ.setdefault("SOLET_NAME", "contract_smoke")

from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402

REQUIRED_FIELDS = ("action_status", "data", "actions", "error", "timestamp")

# Sites the source audit finds but which are not plugin results (value = why).
_NOT_A_PLUGIN_RESULT: dict[str, str] = {
    "audio_processing_plugin/sweep.py::_invoke_inner": (
        "the sweep's own per-combination record; never returned from a verb"
    ),
    "actr_memory_plugin/backend.py::*": (
        "memory-service API results (MemoryServiceInterface), reached through the "
        "service-interface path that requires only a dict; the plugin's own "
        "@platform_process verbs wrap them with _build_response"
    ),
}

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


def _bundled(plugin_name: str) -> bool:
    """False, with a SKIP line, when this bundle does not carry the plugin (profiles differ)."""
    present = (REPO_ROOT / "plugins" / plugin_name / "src").is_dir()
    if not present:
        print(f"  SKIP  {plugin_name} is not in this bundle")
    return present


class _PluginManager:
    """Hands the real validator the plugin under test."""

    def __init__(self, plugin: object) -> None:
        self._plugin = plugin

    def get_plugin(self, _name: str) -> object:
        return self._plugin


class _Action:
    """The queued-action fields the plugin path reads; no flow, so no trigger-data lookup."""

    id = "act-contract-smoke"
    process_key = "contract_smoke_verb"
    parameters = "{}"
    created_at = "2026-10-01T00:00:00+00:00"
    session_id = "ses-contract-smoke"
    flow_id = None
    context_id = None
    result_processor = None
    template_namespace = None
    flow_token_id = None
    source_plugin = None


def _dispatch(plugin: object, verb: str, params: dict[str, object] | None = None) -> dict[str, object]:
    """Run ``plugin.<verb>`` through ``ActionProcessor._execute_plugin_action``."""
    processor = ActionProcessor(plugin_manager=_PluginManager(plugin))  # type: ignore[arg-type]
    return processor._execute_plugin_action("contract_smoke", verb, params or {}, _Action())  # type: ignore[arg-type]


class _Adapter:
    """A one-verb plugin whose verb returns a site's result unchanged."""

    def __init__(self, produce: Callable[[], dict[str, object]]) -> None:
        self._produce = produce

    def verb(self, params: dict[str, object], state: dict[str, object]) -> dict[str, object]:
        del params, state
        return self._produce()


def _accepted(label: str, plugin: object, verb: str, params: dict[str, object] | None = None) -> None:
    """The real validator accepts the result and every required field is present."""
    try:
        result = _dispatch(plugin, verb, params)
    except RuntimeError as exc:
        _check(False, f"{label}: validator refused: {str(exc)[:140]}")
        return
    missing = [f for f in REQUIRED_FIELDS if f not in result]
    _check(not missing, f"{label}: validator accepts ({result.get('action_status')})")
    if "error" in result and "action_status" in result:
        failed = result["action_status"] != "completed"
        _check((result["error"] is not None) == failed, f"{label}: error is set exactly on failure")


def _adapter(label: str, produce: Callable[[], dict[str, object]]) -> None:
    _accepted(label, _Adapter(produce), "verb")


# --------------------------------------------------------------------------- (A) real verbs


def _embedding_verbs(label: str, plugin: Any) -> None:
    """Failure first (uninitialized), then success with the I/O seam stubbed."""
    for verb, params in (
        ("generate_embeddings_action", {"inputs": ["a"]}),
        ("get_embedding_dimension_action", {}),
        ("list_models_action", {}),
    ):
        _accepted(f"{label}.{verb} failure", plugin, verb, params)


def test_openai_embeddings() -> None:
    from openai_embeddings_plugin.plugin import OpenAIEmbeddingsPlugin

    plugin = OpenAIEmbeddingsPlugin()
    _embedding_verbs("openai", plugin)  # not initialized: every verb returns error_result
    plugin._initialized = True
    plugin._base_url = "http://embeddings.test/v1"
    plugin._call_embeddings_api = lambda _url, _payload: (  # type: ignore[method-assign]
        {"data": [{"index": 0, "embedding": [0.1, 0.2]}], "model": "m"},
        None,
    )
    plugin._call_models_api = lambda _url: (  # type: ignore[method-assign]
        {"data": [{"id": "m", "object": "model"}]},
        None,
    )
    for verb, params in (
        ("generate_embeddings_action", {"inputs": ["a"]}),
        ("get_embedding_dimension_action", {}),
        ("list_models_action", {}),
    ):
        _accepted(f"openai.{verb} success", plugin, verb, params)
    _accepted("openai.generate_embeddings_action bad input", plugin, "generate_embeddings_action", {"inputs": []})


class _Body:
    def read(self) -> bytes:
        return b'{"embedding": [0.1, 0.2]}'


class _BedrockRuntime:
    @staticmethod
    def invoke_model(**_kwargs: object) -> dict[str, object]:
        return {"body": _Body()}


class _BedrockControl:
    @staticmethod
    def list_foundation_models(**_kwargs: object) -> dict[str, object]:
        return {"modelSummaries": [
            {"modelId": "m", "modelName": "M", "outputModalities": ["EMBEDDING"]},
        ]}


def test_titanv2_embeddings() -> None:
    if not _bundled("titanv2_embeddings_plugin"):
        return
    from titanv2_embeddings_plugin.plugin import TitanV2EmbeddingsPlugin

    plugin = TitanV2EmbeddingsPlugin()
    _embedding_verbs("titanv2", plugin)
    plugin._initialized = True
    plugin._runtime_client = _BedrockRuntime()
    plugin._bedrock_client = _BedrockControl()
    for verb, params in (
        ("generate_embeddings_action", {"inputs": ["a"]}),
        ("get_embedding_dimension_action", {}),
        ("list_models_action", {}),
    ):
        _accepted(f"titanv2.{verb} success", plugin, verb, params)


def test_salesforce_probe_cli() -> None:
    if not _bundled("salesforce_plugin"):
        return
    from salesforce_plugin.plugin import SalesforcePlugin

    class _Executor:
        @staticmethod
        def probe_cli() -> dict[str, object]:
            return {"executable_path": "", "version": "", "executable": False, "configured": False}

    plugin = SalesforcePlugin()
    plugin._require_executor = lambda: _Executor()  # type: ignore[method-assign]
    _accepted("salesforce.probe_cli success", plugin, "probe_cli")


class _Cursor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def cursor(self, *_args: object, **_kwargs: object) -> _Cursor:
        return _Cursor(self._rows)

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _Config:
    pg_schema = "state"


class _Provider:
    config = _Config()

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    @contextmanager
    def get_connection(self) -> Any:
        yield _Connection(self._rows)


class _BrokenProvider:
    config = _Config()

    @staticmethod
    def get_connection() -> Any:
        raise OSError("database unavailable")


def _state_plugin_cases(label: str, plugin: Any) -> None:
    plugin._get_provider = lambda: _Provider([{"namespace": "core"}])
    _accepted(f"{label}.list_namespaces_action success", plugin, "list_namespaces_action")

    plugin._get_provider = lambda: _BrokenProvider()
    _accepted(f"{label}.list_namespaces_action failure", plugin, "list_namespaces_action")


def test_postgres_state_management() -> None:
    from postgres_state_management_plugin.plugin import PostgresStatePlugin

    _state_plugin_cases("postgres_state", PostgresStatePlugin())


def test_rds_postgres_state_management() -> None:
    if not _bundled("rds_postgres_state_management_plugin"):
        return
    from rds_postgres_state_management_plugin.plugin import RdsPostgresStateManagementPlugin

    _state_plugin_cases("rds_postgres_state", RdsPostgresStateManagementPlugin())


# ------------------------------------------------------------------- (B) adapter-driven sites


def test_coreai_embeddings() -> None:
    from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin

    plugin = CoreAIEmbeddingsPlugin()
    _adapter("coreai.list_models success", plugin.list_models)  # type: ignore[arg-type]
    _adapter(
        "coreai.get_embedding_dimension failure (unsupported model)",
        lambda: plugin.get_embedding_dimension("no-such-model"),  # type: ignore[arg-type,return-value]
    )


def test_thinking_planning_submit() -> None:
    from default_thinking_plugin.plugin import DefaultThinkingPlugin

    plugin = DefaultThinkingPlugin()
    _adapter(
        "thinking._submit_planning_actions success",
        lambda: plugin._submit_planning_actions([{"name": "a"}], "ctx-1", "pbk-1"),  # type: ignore[arg-type,return-value]
    )


def _lifecycle(label: str, plugin: Any) -> None:
    """Start/stop hooks (async, no params): completed both ways, and the refused stop."""
    plugin._services_started = False
    _adapter(f"{label}.stop_services (already stopped)", lambda: asyncio.run(plugin.stop_services()))
    plugin._services_started = True
    _adapter(f"{label}.start_services (already started)", lambda: asyncio.run(plugin.start_services()))


def test_lifecycle_hooks() -> None:
    from default_knowledge_plugin.plugin import DefaultKnowledgePlugin
    from default_thinking_plugin.plugin import DefaultThinkingPlugin

    plugins: list[tuple[str, Any]] = [
        ("knowledge", DefaultKnowledgePlugin()),
        ("thinking", DefaultThinkingPlugin()),
    ]
    if _bundled("g_suite_plugin"):
        from g_suite_plugin.plugin import GSuitePlugin

        plugins.append(("g_suite", GSuitePlugin()))
    for label, plugin in plugins:
        _lifecycle(label, plugin)
        plugin._services_started = True
        if hasattr(plugin, "is_active_interface_provider"):
            plugin.is_active_interface_provider = lambda: True
            _adapter(f"{label}.stop_services refused (active interface)", lambda p=plugin: asyncio.run(p.stop_services()))  # type: ignore[misc]


# --------------------------------------------------------------------- (C) source audit


# Builders that ship in every profile's surface (checked against the seed manifest and the
# capability bundles for macos-bizops, macos_free_minimal and macos_samantha). The audit must
# reach each one, so an audit that finds nothing, or misses a plugin directory, goes red in
# every profile instead of passing on an empty population.
_ANCHORS: frozenset[str] = frozenset({
    "openai_embeddings_plugin/response_builders.py::success_result",
    "openai_embeddings_plugin/response_builders.py::error_result",
    "postgres_state_management_plugin/result_helpers.py::create_success_result",
    "postgres_state_management_plugin/result_helpers.py::create_error_result",
    "coreai_embeddings_plugin/contracts.py::result",
    "coreai_embeddings_plugin/contracts.py::failure",
})


def _literal_values(node: ast.Dict) -> dict[str, ast.expr] | None:
    """String-keyed values of a dict literal, or None when it spreads another mapping in.

    A spread can supply any key, so such a literal cannot be judged from its source.
    """
    if any(key is None for key in node.keys):
        return None
    return {
        k.value: v for k, v in zip(node.keys, node.values, strict=True)
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def _result_literals(tree: ast.Module) -> list[tuple[ast.AST, dict[str, ast.expr], str]]:
    """Every judgeable literal that builds an ActionResult, with its enclosing function name."""
    found: list[tuple[ast.AST, dict[str, ast.expr], str]] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else function
            candidates: list[ast.AST] = []
            if isinstance(child, ast.Return) and child.value is not None:
                candidates.append(child.value)
            if isinstance(child, ast.AnnAssign) and child.value is not None and "ActionResult" in ast.unparse(child.annotation):
                candidates.append(child.value)
            for value in candidates:
                if isinstance(value, ast.Call) and ast.unparse(value.func) == "cast" and len(value.args) == 2:
                    value = value.args[1]
                if isinstance(value, ast.Dict):
                    values = _literal_values(value)
                    if values is not None and "action_status" in values:
                        found.append((value, values, function))
                elif isinstance(value, ast.Call) and ast.unparse(value.func) == "ActionResult":
                    found.append((value, {k.arg: k.value for k in value.keywords if k.arg}, function))
            visit(child, name)

    visit(tree, "<module>")
    return found


def _wrong_type(values: dict[str, ast.expr]) -> list[str]:
    """Fields whose literal value the validator rejects: ``data`` must be a dict, ``actions`` a list."""
    wrong: list[str] = []
    data = values.get("data")
    if data is not None and (
        isinstance(data, ast.List | ast.Tuple | ast.Set | ast.Constant)
    ):
        wrong.append("data is not a dict")
    actions = values.get("actions")
    if actions is not None and (
        isinstance(actions, ast.Dict | ast.Tuple | ast.Set | ast.Constant)
    ):
        wrong.append("actions is not a list")
    return wrong


def test_source_audit() -> None:
    sites = 0
    offenders: list[str] = []
    seen: set[str] = set()
    for path in sorted((REPO_ROOT / "plugins").glob("*/src/**/*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        rel_plugin = path.relative_to(REPO_ROOT / "plugins")
        short = f"{rel_plugin.parts[0]}/{path.name}"
        for node, values, function in _result_literals(tree):
            sites += 1
            seen.add(f"{short}::{function}")
            missing = sorted(set(REQUIRED_FIELDS) - values.keys())
            excluded = {f"{short}::{function}", f"{short}::*"} & _NOT_A_PLUGIN_RESULT.keys()
            if excluded:
                continue
            if missing:
                offenders.append(f"{rel_plugin}:{node.lineno} {function} missing {missing}")
            for problem in _wrong_type(values):
                offenders.append(f"{rel_plugin}:{node.lineno} {function} {problem}")
    print(f"  audited {sites} ActionResult literals")
    unreached = sorted(_ANCHORS - seen)
    _check(not unreached, f"the audit reaches every anchor builder (control; unreached: {unreached})")
    _check(not offenders, "no plugin ActionResult literal omits a required field or gives data/actions the wrong type")
    for line in offenders:
        print(f"        {line}")


def main() -> int:
    print("=== plugin_action_result_contract_smoke ===")
    test_openai_embeddings()
    test_titanv2_embeddings()
    test_salesforce_probe_cli()
    test_postgres_state_management()
    test_rds_postgres_state_management()
    test_coreai_embeddings()
    test_thinking_planning_submit()
    test_lifecycle_hooks()
    test_source_audit()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
