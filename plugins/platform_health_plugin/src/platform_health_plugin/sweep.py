"""Process-registry sweep engine.

Module-level free functions so the sweep can be unit-tested against fixture
registries without standing up a full orchestrator.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast

from platform_health_plugin.constants import (
    READ_SHAPE_PREFIXES,
    SELF_PROCESS_KEY,
    SENTINEL_BOOLEAN,
    SENTINEL_INTEGER,
    SENTINEL_NUMBER,
    SENTINEL_STRING,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED_SELF,
    STATUS_SKIPPED_UNRESOLVED,
    STATUS_SKIPPED_WRITE,
)

RELOAD_SAFE = True

# ─── Orchestrator protocol used by the sweep ────────────────────────────────


class _PluginManagerProtocol(Protocol):
    """Subset of ``PluginManager`` used by the sweep.

    Canonical plugin lookup per ``ActionProcessor._execute_plugin_action``:
    ``plugin_manager.get_plugin(plugin_name)``. The public
    ``OrchestratorProtocol`` does NOT declare ``plugin_manager`` (by design),
    so the dispatch site uses ``getattr`` + a runtime ``cast`` to this
    protocol — duck-typed, structurally correct, and reachable through any
    ``EventOrchestrator`` instance.
    """

    def get_plugin(self, plugin_name: str) -> object: ...


class _OrchestratorProtocol(Protocol):
    """Subset of ``OrchestratorProtocol`` (``plugin_base.py``) used by the sweep.

    Kept narrow to whatever the sweep calls directly on the orchestrator
    surface. ``plugin_manager`` access flows through ``_PluginManagerProtocol``
    at the callsite, not through this protocol — keeping the public
    ``OrchestratorProtocol`` honest.
    """

    def get_service(self, service_name: str) -> object | None: ...

    def get_process_registry(self) -> dict[str, object]: ...


# ─── Classification + sentinel synthesis ────────────────────────────────────


def classify_shape(method_name: str) -> str:
    """Return 'read' or 'write' for one bare method name.

    Per Architect Q1: list_*, get_*, list_active_* are read-shape; everything
    else is write-shape and default-skipped unless write_enabled=True.
    """
    for prefix in READ_SHAPE_PREFIXES:
        if method_name.startswith(prefix):
            return "read"
    return "write"


def build_sentinel_args(parameters: Mapping[str, Any]) -> dict[str, object]:
    """Synthesize a minimal kwargs dict for sweep invocation.

    Only ``required=True`` params get sentinel values; optional params are
    omitted so server-side defaults apply. Unknown types fall back to the
    string sentinel — a real wiring bug will surface via TypeError downstream
    if the method expected a different shape.
    """
    args: dict[str, object] = {}
    for name, meta in parameters.items():
        if not isinstance(meta, Mapping):
            continue
        if not meta.get("required", False):
            continue
        args[name] = _sentinel_for(_lower_type(meta.get("type")))
    return args


def _lower_type(raw: object) -> str:
    if isinstance(raw, str):
        return raw.lower()
    name = getattr(raw, "value", None) or getattr(raw, "name", None)
    if isinstance(name, str):
        return name.lower()
    return ""


def _sentinel_for(type_token: str) -> object:
    if type_token in {"integer", "int"}:
        return SENTINEL_INTEGER
    if type_token in {"number", "float", "double"}:
        return SENTINEL_NUMBER
    if type_token in {"boolean", "bool"}:
        return SENTINEL_BOOLEAN
    if type_token in {"list", "array"}:
        return []
    if type_token in {"dict", "object", "map"}:
        return {}
    return SENTINEL_STRING


# ─── Dispatch ──────────────────────────────────────────────────────────────


def split_process_key(process_key: str) -> tuple[str, str, str] | None:
    """Return (namespace, provider, method) or None if the key is malformed.

    Namespace is either ``service_interface`` or ``plugin``; provider is the
    service name or plugin entry-point name; method is the verb.
    """
    parts = process_key.split("::")
    if len(parts) != 3:
        return None
    namespace, provider, method = parts
    if namespace not in {"service_interface", "plugin"}:
        return None
    return namespace, provider, method


def dispatch_one(
    orchestrator: _OrchestratorProtocol,
    namespace: str,
    provider: str,
    method_name: str,
    args: Mapping[str, object],
) -> None:
    """Invoke a single registered process with the supplied sentinel args.

    Raises the underlying exception unchanged. The caller is responsible for
    catching + recording.
    """
    if namespace == "service_interface":
        service = _resolve_service(orchestrator, provider)
        method = getattr(service, method_name)
        method(**args)
        return
    # plugin_manager is not declared on the public OrchestratorProtocol; access
    # via cast(Any, ...) and re-cast to the structurally-correct protocol.
    plugin_manager: _PluginManagerProtocol = cast(Any, orchestrator).plugin_manager
    plugin = plugin_manager.get_plugin(provider)
    if plugin is None:
        raise LookupError(
            f"plugin {provider!r} not present in plugin_manager (get_plugin returned None)",
        )
    method = getattr(plugin, method_name)
    method(params=dict(args), state={})


def _resolve_service(
    orchestrator: _OrchestratorProtocol,
    provider: str,
) -> object:
    """Mirror ActionProcessor._resolve_service for the legacy-direct services."""
    direct = getattr(orchestrator, provider, None)
    if direct is not None:
        return direct
    service = orchestrator.get_service(provider)
    if service is None:
        raise LookupError(f"service {provider!r} not bound in orchestrator")
    return service


# ─── Top-level sweep ────────────────────────────────────────────────────────


def run_sweep(
    orchestrator: _OrchestratorProtocol,
    *,
    write_enabled: bool = False,
    include_pattern: str | None = None,
) -> dict[str, Any]:
    """Iterate the live process registry; return a per-process result list.

    Result shape:
        {
            "total": int,
            "ok": int,
            "failed": int,
            "skipped": int,
            "results": [
                {
                    "process_key": "service_interface::...",
                    "shape": "read" | "write",
                    "status": "ok" | "failed" | "skipped_write" | ...,
                    "error_class": str | None,
                    "error_message": str | None,
                },
                ...
            ],
        }
    """
    registry = orchestrator.get_process_registry()
    processes_obj = registry.get("processes", {})
    if not isinstance(processes_obj, dict):
        processes_obj = {}
    results: list[dict[str, object]] = []
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    for process_key, process_def in processes_obj.items():
        if not isinstance(process_key, str):
            continue
        if include_pattern is not None and include_pattern not in process_key:
            continue
        row = _evaluate_one(orchestrator, process_key, process_def, write_enabled)
        if row["status"] == STATUS_OK:
            counts["ok"] += 1
        elif row["status"] == STATUS_FAILED:
            counts["failed"] += 1
        else:
            counts["skipped"] += 1
        results.append(row)
    return {
        "total": len(results),
        "ok": counts["ok"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "results": results,
    }


def _evaluate_one(
    orchestrator: _OrchestratorProtocol,
    process_key: str,
    process_def: object,
    write_enabled: bool,
) -> dict[str, object]:
    if process_key == SELF_PROCESS_KEY:
        return _row(process_key, "read", STATUS_SKIPPED_SELF, None, None)
    split = split_process_key(process_key)
    if split is None:
        return _row(process_key, "write", STATUS_SKIPPED_UNRESOLVED, None,
                    "malformed process_key")
    namespace, provider, method_name = split
    shape = classify_shape(method_name)
    if shape == "write" and not write_enabled:
        return _row(process_key, shape, STATUS_SKIPPED_WRITE, None, None)
    parameters = _extract_parameters(process_def)
    args = build_sentinel_args(parameters)
    try:
        dispatch_one(orchestrator, namespace, provider, method_name, args)
    except Exception as exc:  # noqa: BLE001 — intentional broad capture: gate surfaces ANY exception
        return _row(
            process_key,
            shape,
            STATUS_FAILED,
            type(exc).__name__,
            str(exc),
        )
    return _row(process_key, shape, STATUS_OK, None, None)


def _row(
    process_key: str,
    shape: str,
    status: str,
    error_class: str | None,
    error_message: str | None,
) -> dict[str, object]:
    return {
        "process_key": process_key,
        "shape": shape,
        "status": status,
        "error_class": error_class,
        "error_message": error_message,
    }


def _extract_parameters(process_def: object) -> Mapping[str, Any]:
    if not isinstance(process_def, Mapping):
        return {}
    parameters = process_def.get("parameters")
    if isinstance(parameters, Mapping):
        return parameters
    return {}
