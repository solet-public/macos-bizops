"""Static, qualified proofs for the thirty NM-2 Appendix B residual verbs.

Handlers are inspected, never invoked. A proof is separate from measured
FAST_RETURN evidence. Unknown calls cannot establish an enqueue proof.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import FunctionType
from typing import Any, cast

import fast_return_census as census

PREPARER_NAME = "residual"
PLUGINS: frozenset[str] = frozenset()
PROVER = "nm2-synchronous-helper-graph/v1"
RESIDUAL: Mapping[tuple[str, str], str] = {
    ("audio_separation_plugin", "separate_sources"): "R1",
    ("cosyvoice2_tts_plugin", "synthesize_speech_from_script"): "R1",
    ("cosyvoice2_tts_plugin", "synthesize_speech_from_ssml"): "R1",
    ("cosyvoice2_tts_plugin", "synthesize_speech_from_string"): "R1",
    ("whisper_asr_plugin", "extract_word_timestamps"): "R1",
    ("whisper_asr_plugin", "transcribe_audio"): "R1",
    ("audio_processing_plugin", "audition_play"): "R2",
    ("audio_processing_plugin", "audition_play_set"): "R2",
    ("audio_processing_plugin", "audition_stop"): "R2",
    ("macos_self_deployment_plugin", "complete_swap"): "R3",
    ("macos_self_deployment_plugin", "install_autostart"): "R3",
    ("macos_self_deployment_plugin", "status_autostart"): "R3",
    ("macos_self_deployment_plugin", "stop_self"): "R3",
    ("macos_self_deployment_plugin", "uninstall_autostart"): "R3",
    ("sound_effects_plugin", "find_sound_effect"): "R4",
    ("sound_effects_plugin", "preview_sound_effect"): "R4",
    ("sound_effects_plugin", "render_sound_effect"): "R4",
    ("rds_pgvector_service_plugin", "delete_vectors_action"): "R5",
    ("rds_pgvector_service_plugin", "get_namespace_stats_action"): "R5",
    ("rds_pgvector_service_plugin", "get_vector_action"): "R5",
    ("rds_pgvector_service_plugin", "list_namespaces_action"): "R5",
    ("rds_pgvector_service_plugin", "search_similar_action"): "R5",
    ("rds_pgvector_service_plugin", "store_vectors_action"): "R5",
    ("rds_pgvector_service_plugin", "update_metadata_action"): "R5",
    ("rds_postgres_state_management_plugin", "delete_records"): "R5",
    ("rds_postgres_state_management_plugin", "describe_schema"): "R5",
    ("rds_postgres_state_management_plugin", "execute_sql"): "R5",
    ("rds_postgres_state_management_plugin", "list_namespaces"): "R5",
    ("rds_postgres_state_management_plugin", "read_state"): "R5",
    ("rds_postgres_state_management_plugin", "write_state"): "R5",
}
_JOB_CALLS = frozenset({"create_job", "start_job", "submit_job", "dispatch_job", "enqueue", "to_thread", "run_in_executor"})
_PURE_CALLS = frozenset({"bool", "bytes", "dict", "float", "int", "isinstance", "len", "list", "set", "str", "tuple"})


@dataclass
class _Graph:
    """One traversal; no mutable proof state leaks across verbs or roots."""

    pending: list[FunctionType] = field(default_factory=list)
    seen: set[tuple[str, int]] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)
    inline: set[str] = field(default_factory=set)
    enqueues: set[str] = field(default_factory=set)
    dynamic: set[str] = field(default_factory=set)


def _definition_calls(node: ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.Call]:
    for default in (*node.args.defaults, *node.args.kw_defaults):
        if default is not None:
            yield from _calls(default)
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        for decorator in node.decorator_list:
            yield from _calls(decorator)


def _calls(node: ast.AST) -> Iterator[ast.Call]:
    """A callback's body is not executed merely by passing the callback.

    Nested definitions and lambdas remain deferred. Calling a lambda directly
    remains an unresolved edge, preventing a false positive enqueue proof.
    """
    if isinstance(node, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
        # Defaults (unlike the body) run while constructing the callback.
        yield from _definition_calls(node)
        return
    if isinstance(node, ast.ClassDef):
        # Class bodies execute immediately and may include unknown metaclasses.
        for child in (*node.bases, *node.keywords, *node.decorator_list, *node.body):
            yield from _calls(child)
        return
    if isinstance(node, ast.Call):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _calls(child)


def _thread_names(body: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    writes = Counter(_stored_names(body))
    for statement in body:
        if isinstance(statement, ast.Assign) and _thread_constructor(statement.value):
            names.update(target.id for target in statement.targets if isinstance(target, ast.Name))
    return {name for name in names if writes[name] == 1}


def _stored_names(body: list[ast.stmt]) -> Iterator[str]:
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                yield node.id


def _thread_constructor(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and census._call_name(node.func) in {"Thread", "threading.Thread"}


def _thread_start(call: ast.Call, names: set[str]) -> bool:
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "start":
        return False
    owner = call.func.value
    if isinstance(owner, ast.Name):
        return owner.id in names
    return isinstance(owner, ast.Call) and census._call_name(owner.func) in {"Thread", "threading.Thread"}


def _resolved_function(call: ast.Call, function: FunctionType, cls: type[object]) -> FunctionType | None:
    resolved: Any = census._resolve_call(call.func, function, cls)
    if isinstance(resolved, staticmethod | classmethod):
        resolved = resolved.__func__
    return cast(FunctionType, inspect.unwrap(resolved)) if inspect.isfunction(resolved) else None


def _record_call(root: Path, call: ast.Call, function: FunctionType, cls: type[object], location: str, threads: set[str], graph: _Graph) -> None:
    name = census._call_name(call.func)
    graph.evidence.add(location)
    if census._io_call(name):
        graph.inline.add(location)
        return
    resolved = _resolved_function(call, function, cls)
    # An owned helper named enqueue is still a helper: inspect its body.
    if resolved is not None and Path(resolved.__code__.co_filename).resolve().is_relative_to(root):
        graph.pending.append(resolved)
        return
    _external_call(call, name, resolved, location, threads, graph)


def _external_call(call: ast.Call, name: str, resolved: FunctionType | None, location: str, threads: set[str], graph: _Graph) -> None:
    leaf = name.rsplit(".", 1)[-1]
    if leaf in _JOB_CALLS or _thread_start(call, threads):
        graph.enqueues.add(location)
        return
    if name in _PURE_CALLS or name in {"Thread", "threading.Thread"}:
        return
    if resolved is not None:
        graph.pending.append(resolved)
    else:
        graph.dynamic.add(f"{location}:{name or '<dynamic callable>'}")


def _scan(root: Path, cls: type[object], function: FunctionType, graph: _Graph) -> None:
    path = Path(function.__code__.co_filename).resolve()
    key = (str(path), function.__code__.co_firstlineno)
    if key in graph.seen:
        return
    graph.seen.add(key)
    if not path.is_relative_to(root):
        graph.dynamic.add(f"outside measured root: {function.__qualname__}")
        return
    try:
        source, start = inspect.getsourcelines(function)
        tree = ast.parse(textwrap.dedent("".join(source)))
    except (OSError, TypeError, SyntaxError) as exc:
        graph.dynamic.add(f"source unavailable: {function.__qualname__}: {exc}")
        return
    definition = next((node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)), None)
    if definition is None:
        graph.dynamic.add(f"function definition unavailable: {function.__qualname__}")
        return
    prefix = path.relative_to(root).as_posix()
    graph.evidence.add(f"{prefix}:{start}")
    threads = _thread_names(definition.body)
    for statement in definition.body:
        for call in _calls(statement):
            _record_call(root, call, function, cls, f"{prefix}:{start + call.lineno - 1}", threads, graph)


def prove(root: Path, plugin_class: type[object], function_name: str) -> dict[str, object]:
    """Retain the NM-1 helper graph and qualify synchronous-path reachability.

    Inline tokens dominate, then unresolved edges, then enqueue evidence.
    Merely constructing a Thread is insufficient. Deferred worker targets
    are never followed; calls made while constructing its arguments are.
    No proof, including proved_enqueue, is a runtime fast-return verdict.
    """
    method: Any = inspect.getattr_static(plugin_class, function_name, None)
    if isinstance(method, staticmethod | classmethod):
        method = method.__func__
    graph = _Graph()
    baseline: dict[str, Any] = {"hits": [], "dynamic_edges": [], "functions_scanned": 0}
    if inspect.isfunction(method):
        method = inspect.unwrap(method)
        baseline = census.reachable_static(root, plugin_class, method)
        graph.pending.append(method)
    else:
        graph.dynamic.add(f"handler unavailable: {function_name}")
    while graph.pending:
        _scan(root.resolve(), plugin_class, graph.pending.pop(), graph)
    result = "inline_path" if graph.inline else "inconclusive"
    if not graph.inline and not graph.dynamic and graph.enqueues:
        result = "proved_enqueue"
    return {
        "result": result,
        "prover": PROVER,
        "evidence": sorted(graph.evidence),
        "inline": sorted(graph.inline),
        "enqueue": sorted(graph.enqueues),
        "dynamic_edges": sorted(graph.dynamic),
        "helper_graph": baseline,
        "qualification": "static synchronous path only; inconclusive is not a fast-return verdict",
    }
