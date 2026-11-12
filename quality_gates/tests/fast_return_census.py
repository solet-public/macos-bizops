"""Advisory census of manifest-registered plugin handlers; never a latency gate.

Run with ``PYTHONPATH=src .venv/bin/python
quality_gates/tests/fast_return_census.py --output /absolute/scratch/census.json``.
Without --measure this only enumerates the real registration surface. Measurement
uses real handlers, metadata-derived inputs, real private-PG jobs and vendor boundaries
that record their thread and answer after 300 ms with a fixture exception. Job
workers are parked: queue reachability means a real dispatch created a job whose
action/request names the verb, not that the worker or vendor work completed.

Settings (environment defaults): FAST_RETURN_CENSUS_DELAY_SECONDS=0.3,
FAST_RETURN_CENSUS_FAST_MS=50, FAST_RETURN_CENSUS_CPU_MS=250. The CLI can override
the fake delay for harness-only smoke fixtures. Slow handlers never change exit
status. Missing prerequisites are explicit. LOCAL_FAST is authorized by the coordinator
ruling recorded in CLASSIFICATION_RULING; static alternative paths stay visible.

An audit guard prevents real sockets, subprocesses and reads/writes outside
code/library roots and the caller-owned scratch directory. It is defense in depth
behind the fake transports, and remains installed but inactive after the context.
A real service graph is assembled per plugin on a provisioner-owned private
database. No full startup or persistent credential provider is constructed.
Each context owns an app home, removed on success or failure. Readiness runs
for every plugin; preparers supply isolated inputs through a discovered contract.
Listeners require explicit opt-in; residual proofs never invoke their handlers.

The default operation is enumeration. It imports completed plugin classes under
the same boundary guard used for measurement, then reads registration metadata.
It does not construct a runtime, initialize schemas or invoke plugin handlers.
Inherited and generated action definitions participate in the population.
Connectors appear first, followed by the remaining manifest entry points.

Use --measure only for a deliberately authorized isolated measurement pass.
--plugin selects which unfinished plugins to invoke; the remaining plugins are
still enumerated so the full manifest population remains visible in the report.
Enumeration-only rows explicitly say that no handler was invoked. They are not
eligible for a completed checkpoint, even though they use NOT_MEASURABLE.

Use --checkpoint-dir to retain each completed plugin before starting the next.
Every result and index snapshot has a content-addressed filename. The most
recent CHECKPOINT line on stderr identifies a resumable index; no mutable
"latest" file is required. An interruption preserves earlier complete snapshots.
An interrupted plugin may need measurement again, but completed plugins do not.

Use --resume-index with that index, or with the registered carry-over frozen
index. Completed rows are read before dispatch selection, so even --measure
cannot re-invoke a covered handler. Without --measure, new plugins are only
enumerated. A fully covered resume therefore performs no handler measurements.
The JSON output is a new report; source reports and snapshots remain unchanged.

Resume verifies settings, the classification ruling, each plugin result hash,
the manifest identity, the complete verb-name set and the recorded counts.
These are evidence-integrity checks, not performance or availability gates.
The index does not fingerprint source code. Retain the measured commit and
source-report digest alongside the report when handing it to another lane.
Matching verb names alone cannot prove that two revisions have identical code.

Correction is a separate measurement with a new checkpoint selection. Retire
old evidence by no longer supplying its index, keeping it available as history.
The CLI neither edits completed evidence nor deletes checkpoint directories.
An explicit output filename is caller-owned and should also be unique whenever
earlier reports are being retained as review or register evidence.

Wall time covers the handler call and an awaited coroutine result. Imports,
fixture construction, parameter derivation and AST analysis happen before that
timer. Transport, state and file touches retain their thread identifiers. Fake
transport touches establish caller I/O; caller-thread state calls follow OD-1.
File activity remains diagnostic. A boundary refusal identifies a missing isolated prerequisite
rather than proving I/O ran. Nested failed replies retain their missing state.
The full row preserves both timing and the outcome that justified its class.

FAST_RETURN additionally requires the queued handle and matching enqueue
evidence. LOCAL_FAST is a successful local result with no enqueue. A successful
no-transport call can be CPU_INLINE above the configured CPU threshold. Inputs
that do not establish these premises retain an explicit NOT_MEASURABLE reason.
Static hits are potential paths, so disagreement stays visible in either case.

The smoke independently checks manifest/runtime enumeration, exercises timing
with a local synthetic handler, and checks partial and complete checkpoint
reuse. It also checks evidence corruption and incompatible settings. No measured
plugin handler is repeated, and no class or latency value is a smoke pass rule.
All classifications, including slow and wholly unmeasurable populations, remain
advisory output rather than a reason for this command to fail a build.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fast_return_census_context import (  # noqa: E402
    discover_preparers,
    excluded_reason,
    finish_context_report,
    job_readback,
    measurement_metadata,
    measurement_options,
    native_instance,
    plugin_context,
    prepare_parameters,
    timed_handler,
)
from fast_return_census_fixtures import (
    BoundaryAnswer,
    UnsafeAttempt,
    _check_scratch,
    exception_reason,
    isolated_boundaries,
    library_roots,
    temporary_app_home,
)

CONNECTORS = ("zuora_plugin", "g_suite_plugin", "marketo_plugin", "salesforce_plugin", "snowflake_plugin", "jira_plugin", "external_postgres_plugin")
CLASSES = ("FAST_RETURN", "LOCAL_FAST", "INLINE_IO", "CPU_INLINE", "NOT_MEASURABLE", "RESIDUAL")
CLASSIFICATION_RULING = "dec_ec150183"
_UNMEASURED_ACTIONS = {
    ("agent_messaging_plugin", "start_interface"): "listener: requires an isolated bridge runtime",
    ("audio_processing_plugin", "audition_play"): "playback: requires an isolated audio playback process",
    ("audio_processing_plugin", "audition_play_set"): "playback: requires an isolated audio playback process",
    ("audio_processing_plugin", "audition_stop"): "process control: requires an isolated audio playback process",
    ("audio_separation_plugin", "separate_sources"): "model download: requires isolated Demucs weights",
    ("discord_plugin", "start_interface"): "listener: requires an isolated Discord interface runtime",
    ("signal_plugin", "start_interface"): "listener: requires an isolated Signal interface runtime",
    ("whisper_asr_plugin", "extract_word_timestamps"): "model download: requires isolated Whisper weights",
    ("whisper_asr_plugin", "transcribe_audio"): "model download: requires isolated Whisper weights",
    ("macos_self_deployment_plugin", "install_autostart"): "process control: requires an isolated autostart installation",
    ("macos_self_deployment_plugin", "status_autostart"): "deployment state: requires an isolated autostart installation",
    ("macos_self_deployment_plugin", "uninstall_autostart"): "process control: requires an isolated autostart installation",
    ("macos_self_deployment_plugin", "complete_swap"): "process control: requires an isolated deployment runtime",
    ("macos_self_deployment_plugin", "stop_self"): "process control: requires an isolated deployment runtime",
    ("macos_midwife_plugin", "birth_solet"): "filesystem target: requires an isolated clone destination",
}
_IO_TOKENS = ("subprocess", "urllib", "requests.", "httpx.", "boto3.", "botocore.", "googleapiclient", ".execute", ".request", "client.", "urlopen", ".read_", ".write_", ".open", ".mkdir", ".unlink", ".stat", ".is_file", ".exists", ".iterdir")
_SLEEP = time.sleep


class MissingFixtureError(Exception):
    """A real handler needs additional isolated state before its work is reachable."""


@dataclass(frozen=True)
class Settings:
    """Measurement premises retained alongside every complete report.

    Defaults model a 300 ms vendor response, a 50 ms prompt return and a
    250 ms CPU observation. Explicit environment or CLI overrides create a
    different measurement premise and cannot reuse incompatible checkpoints.
    None of these values is used as a build acceptance threshold.
    """

    delay_seconds: float = 0.3
    fast_ms: float = 50.0
    cpu_ms: float = 250.0
    context: str = "private-pg-v1"

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(float(os.environ.get("FAST_RETURN_CENSUS_DELAY_SECONDS", "0.3")), float(os.environ.get("FAST_RETURN_CENSUS_FAST_MS", "50")), float(os.environ.get("FAST_RETURN_CENSUS_CPU_MS", "250")))


@dataclass(frozen=True)
class ManifestEntry:
    """One distribution entry point and its repository-owned manifest directory.

    The provider name is the registered name, not a guessed module stem.
    The target may name a completed class whose actions are inherited or
    generated at import time. The directory anchors static helper discovery.
    """

    plugin: str
    target: str
    directory: Path


@dataclass
class PluginVerbs:
    """A completed registration census, or an explicit unresolved import.

    Import failure is retained on the plugin row instead of disappearing
    from the population. Such a row has no invented verb count. The smoke
    checks that the installed measurement environment resolves every class.
    """

    entry: ManifestEntry
    plugin_class: type[Any] | None
    actions: list[Any]
    error: str = ""


@dataclass
class Probe:
    """Per-call transport and parked-worker observations.

    Fakes append a boundary name and the actual executing thread before
    waiting. Observations during setup are cleared immediately before the
    timed handler, so library import probes are not handler I/O evidence.
    """

    settings: Settings
    touches: list[dict[str, Any]] = field(default_factory=list)
    starts: list[str] = field(default_factory=list)

    def boundary(self, name: str, *, socket_error: bool = False) -> Callable[..., Any]:
        def answer(*_args: Any, **_kwargs: Any) -> Any:
            self.touches.append({"boundary": name, "thread_id": threading.get_ident()})
            _SLEEP(self.settings.delay_seconds)
            if socket_error:
                raise OSError("census fixture: local socket unavailable")
            raise BoundaryAnswer(name)

        return answer


class JobLedger:
    """Synthetic engine-smoke fixture only; full measurements use the real manager."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        self.jobs.append(kwargs)
        return {"action_status": "completed", "data": {"job_id": f"census-job-{len(self.jobs)}", "status": "queued"}}

    def list_jobs(self, **_kwargs: Any) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"jobs": []}}


def manifest_entries(root: Path) -> list[ManifestEntry]:
    """Use the platform's ananta.plugins distribution entry-point group.

    Every manifest contributes its actual registered provider/target pairs.
    The connector ordering is presentation and measurement priority only;
    all remaining entry points are retained in deterministic name order.
    No verb or provider is inferred from a filename convention.
    """
    entries: list[ManifestEntry] = []
    for manifest in sorted(root.glob("plugins/*/pyproject.toml")):
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))
        group = document.get("project", {}).get("entry-points", {}).get("ananta.plugins", {})
        entries.extend(ManifestEntry(str(name), str(target), manifest.parent) for name, target in group.items())
    priority = {name: index for index, name in enumerate(CONNECTORS)}
    return sorted(entries, key=lambda item: (priority.get(item.plugin, len(priority)), item.plugin))


def enumerate_verbs(root: Path) -> list[PluginVerbs]:
    """Read completed classes, including inherited/generated process methods.

    This calls the same registration census used by the platform. Resolved
    class sources must belong to the requested tree, preventing editable
    installations from silently measuring another checkout. Callers install
    isolated_boundaries before importing any entry point.
    """
    sys.path[:0] = [str(root / "ananta/src"), str(root / "solet_setup_contracts/src"), str(root), *(str(path) for path in sorted(root.glob("plugins/*/src")))]
    from ananta.core.process_registry.registration_census import class_registration_census

    plugins: list[PluginVerbs] = []
    for entry in manifest_entries(root):
        try:
            module, attribute = entry.target.split(":", 1)
            cls = cast(type[Any], getattr(importlib.import_module(module), attribute))
            source = Path(inspect.getfile(cls)).resolve()
            if not source.is_relative_to(root):
                raise MissingFixtureError(f"entry point resolved outside measured tree: {source}")
            plugins.append(PluginVerbs(entry, cls, list(class_registration_census(cls, entry.plugin).actions)))
        except (Exception, BoundaryAnswer, UnsafeAttempt) as exc:
            plugins.append(PluginVerbs(entry, None, [], f"{type(exc).__name__}: {exc}"))
    return plugins


def _call_name(node: ast.expr) -> str:
    """Render a conservative dotted call name without evaluating the AST.

    Chained builder calls retain their named owner for token matching.
    Unsupported expressions produce no invented name; the dynamic-graph
    qualification in the report covers what static resolution cannot prove.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


def _io_call(name: str) -> bool:
    """Match the declared conservative transport and filesystem token set.

    A token hit is a candidate source location, never a runtime conclusion.
    The same predicate serves whole-plugin and reachable-helper inventories
    so their path/line observations can be compared without different rules.
    """
    return name == "open" or any(token in name.lower() for token in _IO_TOKENS)


def static_inventory(root: Path, entry: ManifestEntry) -> dict[str, Any]:
    """List potential I/O and worker primitives with exact source locations.

    The scan covers every source/helper module in the plugin directory.
    It does not imply that a given verb reaches every listed call. Worker
    primitive locations likewise describe available machinery, while the
    measured row separately records whether this dispatch enqueued work.
    """
    hits: list[dict[str, Any]] = []
    workers: list[str] = []
    for path in sorted((entry.directory / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if _io_call(name):
                hits.append({"path": relative, "line": node.lineno, "call": name})
            if any(token in name for token in ("Thread", "Executor", "create_job", "start_job", "submit_job", "dispatch_job", "to_thread", "run_in_executor", ".submit", "enqueue")):
                workers.append(f"{relative}:{node.lineno}")
    return {"hits": hits, "worker_primitives": sorted(set(workers)), "scope": "all handler/helper modules in this plugin; not all hits are reachable from each verb"}


def _resolve_call(node: ast.expr, function: Any, cls: type[Any]) -> Any:
    """Resolve named globals and self attributes without invoking descriptors.

    inspect.getattr_static avoids executing properties while inspecting
    helper relationships. Only functions that can actually be identified
    are traversed; unknown producers remain named dynamic edges instead.
    """
    if isinstance(node, ast.Name):
        return function.__globals__.get(node.id)
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            return inspect.getattr_static(cls, node.attr, None)
        owner = _resolve_call(node.value, function, cls)
        if owner is not None:
            return inspect.getattr_static(owner, node.attr, None)
    return None


def reachable_static(root: Path, cls: type[Any], method: Any) -> dict[str, Any]:
    """Follow resolvable helpers and retain unresolved dynamic call names.

    A source-file/first-line key breaks recursive helper cycles. Traversal
    stops outside repository plugin sources, keeping SDK internals outside
    this inventory. The measured boundary still observes SDK activity even
    when this conservative helper graph cannot identify the path to it.
    """
    pending = [inspect.unwrap(getattr(method, "__func__", method))]
    seen: set[tuple[str, int]] = set()
    locations: set[str] = set()
    dynamic: set[str] = set()
    while pending:
        function = pending.pop()
        if not inspect.isfunction(function):
            continue
        path = Path(function.__code__.co_filename).resolve()
        key = (str(path), function.__code__.co_firstlineno)
        if key in seen or not path.is_relative_to(root / "plugins"):
            continue
        seen.add(key)
        _follow_calls(root, path, function, cls, pending, locations, dynamic)
    return {"hits": sorted(locations), "dynamic_edges": sorted(dynamic), "functions_scanned": len(seen)}


def _follow_calls(root: Path, path: Path, function: Any, cls: type[Any], pending: list[Any], locations: set[str], dynamic: set[str]) -> None:
    """Expand one function's AST while retaining original source line numbers.

    inspect's starting line offsets the dedented function AST. Potential
    I/O locations are reported independently of helper traversal. Dynamic
    dispatch-related names remain qualifications, not guessed callees.
    """
    import textwrap

    source, start = inspect.getsourcelines(function)
    tree = ast.parse(textwrap.dedent("".join(source)))
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        name = _call_name(call.func)
        if _io_call(name):
            locations.add(f"{path.relative_to(root).as_posix()}:{start + call.lineno - 1}")
        resolved = _resolve_call(call.func, function, cls)
        if inspect.isfunction(resolved):
            pending.append(resolved)
        elif name and any(token in name for token in ("produce", "handler", "dispatch")):
            dynamic.add(name)


def _parameter(name: str, metadata: Any, scratch: Path) -> Any:
    """Derive a minimal fixture from the declared parameter contract.

    Examples precede enum constraints, operation descriptions and defaults.
    These values make no promise that a stateful operation is executable;
    the outcome retains any more specific fixture prerequisite it reports.
    """
    if metadata.examples:
        return metadata.examples[0]
    validation = metadata.validation or {}
    if validation.get("enum"):
        return validation["enum"][0]
    if name == "operation":
        match = re.search(r"[Oo]ne of[: ]+[\[(]?[\"']?([A-Za-z_]+)", metadata.description)
        if match is not None:
            return match.group(1)
    if metadata.default is not None:
        return metadata.default
    return _typed_parameter(name, metadata.type.value, validation, scratch)


def _typed_parameter(name: str, kind: str, validation: dict[str, Any], scratch: Path) -> Any:
    if kind in {"int", "integer", "float", "number"}:
        return validation.get("minimum", 1)
    values = {"bool": True, "boolean": True, "list": ["census-fixture"], "array": ["census-fixture"], "object": {"name": "census-fixture"}, "dict": {"name": "census-fixture"}}
    return values.get(kind, _string_parameter(name, scratch))


def _string_parameter(name: str, scratch: Path) -> str:
    if "path" in name:
        return str(scratch / ("fixture.tsv" if "tsv" in name else "fixture.json"))
    if "date" in name:
        return "2026-01-31"
    if name in {"method", "http_method"}:
        return "GET"
    if "email" in name:
        return "census@example.invalid"
    if "url" in name:
        return "https://census.invalid/fixture"
    return "census-fixture"


def minimal_parameters(action: Any, scratch: Path) -> dict[str, Any]:
    """Declared required arguments, preferring declared examples and constraints."""
    params = {name: _parameter(name, metadata, scratch) for name, metadata in action.parameters.items() if metadata.required}
    if action.examples:
        params.update(action.examples[0].parameters)
    for name, value in params.items():
        if isinstance(value, str) and ("file_path" in name or name.endswith("_path")):
            params[name] = str(scratch / ("fixture" + (Path(value).suffix or ".json")))
    return params


def _body_value(name: str) -> Any:
    folded = name.lower()
    if "date" in folded:
        return "2026-01-31"
    values: dict[str, Any] = {
        "amount": 1,
        "totalamount": 1,
        "quantity": 1,
        "processpayment": False,
        "usediscountspecificaccountingcode": False,
        "billtocontact": {"name": "census-fixture"},
        "previewoptions": {"name": "census-fixture"},
        "productrateplanchargetierdata": {"name": "census-fixture"},
        "definitions": [{"id": "census-fixture"}],
        "records": [{"id": "census-fixture"}],
        "actions": [{"id": "census-fixture"}],
        "subscribetorateplans": [{"id": "census-fixture"}],
        "currency": "USD",
    }
    return values.get(folded, "census-fixture")


def _connector_parameters(provider: str, action: Any, params: dict[str, Any], scratch: Path) -> dict[str, Any]:
    if provider == "g_suite_plugin" and "account" in params:
        params["account"] = "census@example.invalid"
    if provider == "marketo_plugin":
        _marketo_parameters(action, params)
    if provider == "zuora_plugin":
        _zuora_parameters(action, params, scratch)
    return params


def _marketo_parameters(action: Any, params: dict[str, Any]) -> None:
    values = {
        "records": [{"id": 1, "filename": "fixture.csv", "content_base64": "YQ=="}],
        "keys": [{"id": 1, "filename": "fixture.csv", "content_base64": "YQ=="}],
        "files": [{"id": 1, "filename": "fixture.csv", "content_base64": "YQ=="}],
        "lead_ids": [1],
        "ids": [1],
        "id": "1",
        "list_id": "1",
        "program_id": "1",
        "folder_type": "Folder",
        "format": "csv",
    }
    for name in params.keys() & values.keys():
        params[name] = values[name]
    if action.name in {"get_leads", "get_leads_by_filter"}:
        params.update(filter_type="id", filter_values=["1"])


def _zuora_parameters(action: Any, params: dict[str, Any], scratch: Path) -> None:
    from zuora_plugin import typed_verbs

    operation = typed_verbs._SPEC_OPERATIONS.get(action.name)
    if operation is not None:
        for name, spec in operation.path_fields.items():
            params[name] = 1 if spec.kind == "int_id" else "census-fixture"
        if operation.body is not None and inspect.isfunction(operation.body):
            required = inspect.getclosurevars(operation.body).nonlocals.get("required", ())
            if isinstance(required, tuple) and required:
                params["body"] = {name: _body_value(name) for name in required}
    _zuora_special_parameters(action.name, params, scratch)


def _zuora_special_parameters(name: str, params: dict[str, Any], scratch: Path) -> None:
    if name.startswith("upload_") and "input_path" in params:
        params["input_path"] = str(scratch / "fixture.pdf")
    values = {
        "import_workflow": {"input_path": str(scratch / "fixture.json")},
        "import_workflow_version": {"input_path": str(scratch / "fixture.json")},
        "import_usage": {"input_path": str(scratch / "fixture.csv")},
        "create_workflow_auth_provider": {"auth_type": "basic_auth"},
        "update_workflow": {"name": "census-fixture"},
        "update_workflow_auth_provider": {"name": "census-fixture"},
        "update_workflow_tasks": {"tasks": [{"id": 1, "name": "census-fixture"}]},
    }
    params.update(values.get(name, {}))


def _prepare_plugin(plugin: PluginVerbs, ledger: Any, scratch: Path, context: Any = None, prepared: Any = None) -> Any:
    if plugin.plugin_class is None:
        raise MissingFixtureError(plugin.error)
    instance = prepared if prepared is not None else plugin.plugin_class()
    instance.name = plugin.entry.plugin
    instance.orchestrator_ref = SimpleNamespace(APP_HOME=str(scratch), async_job_manager=ledger, get_service=lambda _name: None)
    from ananta.core.config.config_provider import ConfigProvider

    instance.config_provider = ConfigProvider(plugin.entry.plugin, {"export_allowed_roots": [str(scratch)]})
    if hasattr(instance, "_async_job_manager"):
        instance._async_job_manager = ledger
    if hasattr(instance, "_deferred"):
        instance._deferred.manager = ledger
    if context is not None:
        context.wire(instance)
    if prepared is None:
        _ready_instance(instance)
        if context is not None:
            context.initialize(instance)
    _prepare_connector(plugin, instance)
    return instance


def _ready_instance(instance: Any) -> None:
    """Mirror startup's readiness hook and successful uninitialized transition."""
    if callable(getattr(instance, "prepare_for_readiness", None)):
        instance.prepare_for_readiness()
        if callable(getattr(instance, "is_ready", None)) and not instance.is_ready():
            instance.set_ready()


def _prepare_connector(plugin: PluginVerbs, instance: Any) -> None:
    """Keep NM-1's synthetic account/transport setup without changing handlers."""
    if plugin.entry.plugin == "g_suite_plugin":
        spec = importlib.util.spec_from_file_location("_census_gsuite_fakes", plugin.entry.directory / "tests/gsuite_fakes.py")
        if spec is None or spec.loader is None:
            raise MissingFixtureError("g_suite isolated account fixture loader missing")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.connect_accounts(instance, ["census@example.invalid"])
    elif plugin.entry.plugin == "marketo_plugin":
        from marketo_plugin.app_config import MarketoInstanceConfig

        config = MarketoInstanceConfig("https://census.invalid", "fixture-client-id", "fixture-client-secret")
        instance._app_config_loader = SimpleNamespace(load=lambda: config)


def _queued(result: object) -> bool:
    """Recognize a queued job receipt without treating it as completion.

    Both the direct payload and conventional data envelope are accepted.
    A status alone is insufficient: the result must carry an actual job id,
    and classification separately requires matching in-memory enqueue evidence.
    """
    if not isinstance(result, dict):
        return False
    data = result.get("data", result)
    return isinstance(data, dict) and bool(data.get("job_id")) and data.get("status") in {"queued", "pending", "started", "running"}


def _failure_reason(result: Any) -> str:
    """Inspect conventional reply envelopes, retaining their exact failure detail.

    An outer completed action can carry a failed/rejected operation in data or
    result. Historical records in lists and arbitrary domain fields are not
    operation envelopes and do not change a successful local read's outcome.

    Explicit error and missing-readiness details take priority over a generic
    message. Status, success and ready flags describe the operation envelope;
    absent flags alone do not establish a failure. Advisory reads can therefore
    report unknown ancillary state without becoming failed operations.
    """
    if not isinstance(result, dict):
        return ""
    statuses = ("error", "failed", "cancelled", "rejected", "not_ready", "not-ready")
    detail = result.get("error") or result.get("missing_piece") or result.get("missing_readiness")
    failed = any((detail, result.get("action_status") in statuses, result.get("status") in statuses, result.get("success") is False, result.get("ready") is False))
    if failed:
        return str(detail or result.get("message") or f"unsuccessful reply: {result}")
    return _failure_reason(result.get("data")) or _failure_reason(result.get("result"))


def _classify(row: dict[str, Any], result: Any, error: str, settings: Settings, *, enqueued: bool) -> None:
    """Apply observed evidence while retaining uncertainty from setup or guards.

    A guard refusal wins over elapsed time or attempted file activity. A fake
    caller-thread transport boundary establishes INLINE_IO even when its
    terminal fixture answer raises. File diagnostics cannot establish that
    boundary, including when replaying a legacy row's derived caller_io flag.
    Other failures keep their missing premise rather than claiming LOCAL_FAST.
    """
    row.update({"class": "NOT_MEASURABLE", "missing_piece": ""})
    row["caller_io"] = any(touch["thread_id"] == row["calling_thread"] for touch in row.get("touches", []))
    failure = _failure_reason(result)
    elapsed = row["wall_ms"]
    if elapsed is None or row.get("unsafe_attempt"):
        row["missing_piece"] = error or "requires an isolated initialized plugin instance"
    elif row["caller_io"]:
        row["class"] = "INLINE_IO"
    elif _state_dominated(row, settings):
        row.update({"class": "INLINE_IO", "inline_boundary": "state_service"})
    elif not error and not failure:
        _classify_success(row, result, settings, enqueued=enqueued)
    else:
        row["missing_piece"] = error or f"requires valid fixture state/parameters: {failure}"


def _inline_state_ms(row: dict[str, Any]) -> float:
    return sum(call["elapsed_ms"] for call in row.get("state_calls", []) if call["thread_id"] == row["calling_thread"] and not call.get("sanctioned_job_persistence"))


def _state_dominated(row: dict[str, Any], settings: Settings) -> bool:
    """OD-1 excludes sanctioned job persistence while retaining its evidence."""
    return row["wall_ms"] >= settings.fast_ms and _inline_state_ms(row) >= row["wall_ms"] / 2


def _classify_success(row: dict[str, Any], result: Any, settings: Settings, *, enqueued: bool) -> None:
    if row["wall_ms"] < settings.fast_ms and _queued(result) and row["worker_reachable"]:
        row["class"] = "FAST_RETURN"
    elif row["wall_ms"] < settings.fast_ms and not enqueued:
        row["class"] = "LOCAL_FAST"
    elif not row["fake_touched"] and row.get("thread_cpu_ms", 0) > settings.cpu_ms:
        row["class"] = "CPU_INLINE"
    else:
        row["missing_piece"] = "requires representative execution state: minimal input returned outside the fast/CPU thresholds without a measured I/O boundary"


@dataclass
class _Call:
    """Capture timing even when a terminal fake or an audit refusal unwinds a call."""

    started: float | None = None
    finished: float | None = None
    result: Any = None
    error: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    setup_files: list[dict[str, Any]] = field(default_factory=list)
    thread_cpu_ms: float = 0.0


def _execute_call(root: Path, plugin: PluginVerbs, action: Any, scratch: Path, probe: Probe, ledger: Any, row: dict[str, Any], call: _Call, context: Any = None) -> None:
    """Retain unsafe defaults, then set up the real bound call under its guard.

    Fresh native providers open their private pools before worker parking.
    Preparers transform only inputs; they never replace the bound method.
    """
    prerequisite = excluded_reason(context, (plugin.entry.plugin, action.name), _UNMEASURED_ACTIONS)
    if prerequisite is not None:
        raise MissingFixtureError(prerequisite)
    prepared = native_instance(context, plugin.entry.plugin, plugin.plugin_class)
    with isolated_boundaries(root, scratch, probe, **measurement_options(context)) as guard:
        try:
            instance = _prepare_plugin(plugin, ledger, context.app_home if context is not None else scratch, context, prepared)
            row["parameters"] = prepare_parameters(context, plugin.entry.plugin, action.name, row["parameters"])
            method = getattr(instance, action.function or action.name)
            graph = reachable_static(root, type(instance), method)
            row["static_hits"] = graph["hits"]
            row["static_dynamic_edges"] = graph["dynamic_edges"]
            # Retain setup writes separately without promoting setup to timed I/O.
            call.setup_files = list(guard.files)
            row["setup_parked_starts"] = list(probe.starts)
            probe.touches.clear()
            probe.starts.clear()
            guard.files.clear()
            ledger.jobs.clear()
            if context is not None:
                context.state.calls.clear()
            timed_handler(method, row, call, guard, context)
        finally:
            if call.started is None:
                call.setup_files = list(guard.files)
            row["blocked_effects"] = list(guard.blocked)


def _capture_call(root: Path, plugin: PluginVerbs, action: Any, scratch: Path, probe: Probe, ledger: Any, row: dict[str, Any], context: Any = None) -> _Call:
    call = _Call()
    try:
        _execute_call(root, plugin, action, scratch, probe, ledger, row, call, context)
    except BoundaryAnswer as exc:
        call.error = f"fixture boundary answer: {exc}"
    except UnsafeAttempt as exc:
        row["unsafe_attempt"] = str(exc)
        call.error = f"requires isolated runtime/file prerequisite; guard refused before host effect: {exc}"
    except Exception as exc:
        call.error = exception_reason(root, exc)
    return call


def _record_probe(row: dict[str, Any], probe: Probe, ledger: Any, action: Any, call: _Call) -> bool:
    """Keep transport evidence distinct from diagnostic filesystem observations.

    Imports can lazily read bytecode during a timed call; fixture operations
    can also produce file events. Neither proves a vendor boundary was called.
    File events remain available for inspection and the audit guard still
    refuses host paths before any effect. Caller I/O uses only fake touches
    whose recorded thread matches this handler's calling thread.

    Matching job action/request names establish enqueue reachability, while
    parked thread starts remain observations rather than completed work.
    """
    elapsed = (call.finished - call.started) * 1000 if call.started is not None and call.finished is not None else None
    caller_touched = any(touch["thread_id"] == row["calling_thread"] for touch in probe.touches)
    row.update(wall_ms=elapsed, thread_cpu_ms=call.thread_cpu_ms, fake_touched=bool(probe.touches), touches=probe.touches, file_touches=call.files, setup_file_touches=call.setup_files, caller_io=caller_touched, worker_starts=probe.starts, enqueued_actions=[job.get("action_name") for job in ledger.jobs])
    row["worker_reachable"] = any(job.get("action_name") == action.name or job.get("request_data", {}).get("verb") == action.name for job in ledger.jobs)
    return caller_touched


def measure_verb(root: Path, plugin: PluginVerbs, action: Any, scratch: Path, settings: Settings, context: Any = None) -> dict[str, Any]:
    """Measure through the existing bound-handler caller with an owned home."""
    _check_scratch(root, scratch)
    if context is not None:
        return _measure_verb(root, plugin, action, context.scratch, settings, context)
    with temporary_app_home(scratch) as home:
        row = _measure_verb(root, plugin, action, home, settings)
        row["fixture_app_home"] = str(home)
    row["fixture_removed"] = not home.exists()
    return row


def _measure_verb(root: Path, plugin: PluginVerbs, action: Any, scratch: Path, settings: Settings, context: Any = None) -> dict[str, Any]:
    """Time one real dispatch; an early validation/configuration error is unmeasured."""
    probe = Probe(settings)
    ledger = context.jobs if context is not None else JobLedger()
    params = _connector_parameters(plugin.entry.plugin, action, minimal_parameters(action, scratch), scratch)
    row: dict[str, Any] = {"verb": action.name, "class": "NOT_MEASURABLE", "wall_ms": None, "fake_touched": False, "worker_reachable": False, "static_hits": [], "calling_thread": threading.get_ident(), "missing_piece": "", "parameters": params}
    row.update(measurement_metadata(context))
    call = _capture_call(root, plugin, action, scratch, probe, ledger, row, context)
    caller_touched = _record_probe(row, probe, ledger, action, call)
    row["succeeded"] = not call.error and not _failure_reason(call.result)
    if context is not None and _queued(call.result):
        job_readback(context, row, call.result, plugin.entry.plugin, action.name)
    _classify(row, call.result, call.error, settings, enqueued=bool(ledger.jobs))
    row["outcome"] = call.error or str(call.result)[:1500]
    row["static_disagreement"] = "runtime transport reached but static helper graph missed it" if caller_touched and not row.get("static_hits") else "static hits exist but this input reached no transport (deferred or incomplete path)" if row.get("static_hits") and not probe.touches else ""
    return row


def _read_object(path: Path, digest: str | None = None) -> dict[str, Any]:
    """Read an evidence object, verifying its exact bytes before interpreting it."""
    raw = path.read_bytes()
    if digest is not None and hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"checkpoint SHA-256 mismatch: {path}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"checkpoint object required: {path}")
    return value


def _checkpoint_path(index: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else index.parent / path


def _validate_result(item: dict[str, Any]) -> None:
    """Reject inconsistent evidence, never a slow or unmeasurable classification."""
    verbs = item["verbs"]
    names = [row["verb"] for row in verbs]
    if len(names) != len(set(names)) or item["verb_count"] != len(names):
        raise ValueError(f"checkpoint verb count or duplicate: {item['plugin']}")
    counts = Counter(row["class"] for row in verbs)
    if set(counts) - set(CLASSES):
        raise ValueError(f"checkpoint classification unknown: {item['plugin']}")
    if item["counts"] != {name: counts[name] for name in CLASSES}:
        raise ValueError(f"checkpoint counts disagree with rows: {item['plugin']}")
    if any(row.get("missing_piece") == "enumeration only; handler not invoked" for row in verbs):
        raise ValueError(f"enumeration is not a completed measurement: {item['plugin']}")


def load_checkpoint(index: Path, settings: Settings) -> dict[str, dict[str, Any]]:
    """Read the registered frozen index or an immutable checkpoint snapshot.

    The carry-over index names a hash-bound full report for its settings. New
    snapshots carry those settings directly. Neither input format permits an
    enumeration-only row to masquerade as a finished measurement.
    """
    document = _read_object(index)
    if document["schema"] != "fast-return-frozen-plugin-index/v2":
        raise ValueError(f"checkpoint schema unknown: {index}")
    recorded = document.get("settings")
    if recorded is None:
        source = _read_object(_checkpoint_path(index, document["source"]), document["source_sha256"])
        recorded = source["settings"]
    if recorded != asdict(settings) or document["classification_ruling"] != CLASSIFICATION_RULING:
        raise ValueError("checkpoint settings or classification ruling differ")
    completed: dict[str, dict[str, Any]] = {}
    for reference in document["plugins"]:
        item = _read_object(_checkpoint_path(index, reference["path"]), reference["sha256"])
        _validate_result(item)
        name = reference["plugin"]
        if name in completed or item["plugin"] != name:
            raise ValueError(f"checkpoint plugin identity or duplicate: {name}")
        if (item["verb_count"], item["counts"]) != (reference["verb_count"], reference["counts"]):
            raise ValueError(f"checkpoint index disagrees with plugin result: {name}")
        completed[name] = item
    return completed


def _publish_object(directory: Path, value: dict[str, Any]) -> tuple[Path, str]:
    """Publish complete, content-addressed bytes without replacing any history.

    A same-directory temporary file is flushed before its atomic hard-link
    publication. A concurrent identical publisher may reuse the same object;
    different bytes at that name are corruption. Only our temporary is removed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2, default=str) + "\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    target = directory / f"{digest}.json"
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != raw:
                raise ValueError(f"checkpoint object changed: {target}") from None
    finally:
        temporary.unlink()
    return target, digest


def write_checkpoint(directory: Path, completed: dict[str, dict[str, Any]], settings: Settings) -> Path:
    """Append a snapshot after each completed plugin, including named unknowns.

    Every result and index is immutable. Resume explicitly names an index from
    stderr; an interrupted run leaves its previous snapshot usable. Correction
    starts a separate run without the retired index. No completed row is updated
    or cleared in place, and no latest-pointer race or cleanup is required.
    """
    references: list[dict[str, Any]] = []
    for name, item in completed.items():
        _validate_result(item)
        path, digest = _publish_object(directory / "results", item)
        references.append({"plugin": name, "path": str(path.resolve()), "sha256": digest, "verb_count": item["verb_count"], "counts": item["counts"]})
    document = {"schema": "fast-return-frozen-plugin-index/v2", "settings": asdict(settings), "classification_ruling": CLASSIFICATION_RULING, "plugins": references}
    index, _digest = _publish_object(directory, document)
    print(f"CHECKPOINT {index}", file=sys.stderr, flush=True)
    return index


def _validate_population(root: Path, inventory: list[PluginVerbs], completed: dict[str, dict[str, Any]]) -> None:
    """Bind cached rows to the current manifest and full runtime verb names."""
    live = {plugin.entry.plugin: plugin for plugin in inventory}
    if set(completed) - set(live):
        raise ValueError(f"checkpoint plugins absent from manifest: {sorted(set(completed) - set(live))}")
    for name, item in completed.items():
        plugin = live[name]
        identity = (plugin.entry.target, str(plugin.entry.directory.relative_to(root) / "pyproject.toml"))
        if identity != (item["entry_point"], item["manifest"]):
            raise ValueError(f"checkpoint manifest identity differs: {name}")
        if plugin.error or sorted(row["verb"] for row in item["verbs"]) != sorted(action.name for action in plugin.actions):
            raise ValueError(f"checkpoint runtime verb population differs: {name}: {plugin.error}")


def _residual_row(root: Path, plugin: PluginVerbs, action: Any, modules: list[Any]) -> dict[str, Any] | None:
    for module in modules:
        reason = getattr(module, "RESIDUAL", {}).get((plugin.entry.plugin, action.name))
        if reason is not None:
            return {"verb": action.name, "class": "RESIDUAL", "wall_ms": None, "missing_piece": "", "residual_reason": reason, "proof": module.prove(root, plugin.plugin_class, action.function or action.name), "preparer": [module.PREPARER_NAME]}
    return None


def _plugin_result(root: Path, plugin: PluginVerbs, scratch: Path, settings: Settings, selected: bool, modules: list[Any], context_factory: Callable[..., Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"plugin": plugin.entry.plugin, "manifest": str(plugin.entry.directory.relative_to(root) / "pyproject.toml"), "entry_point": plugin.entry.target, "verb_count": len(plugin.actions), "import_error": plugin.error, "static": static_inventory(root, plugin.entry), "verbs": []}
    if selected and plugin.plugin_class is not None:
        with context_factory(root, scratch, plugin.entry.plugin, plugin.plugin_class, Probe(settings), modules) as context:
            for action in plugin.actions:
                row = _residual_row(root, plugin, action, modules)
                item["verbs"].append(dict(row if row is not None else measure_verb(root, plugin, action, scratch, settings, context)))
        item["context_run"] = context.receipt
        for row in item["verbs"]:
            row.update(fixture_app_home=str(context.app_home), fixture_removed=context.receipt.get("fixture_removed", False))
    else:
        item["verbs"] = [{"verb": action.name, "class": "NOT_MEASURABLE", "missing_piece": "enumeration only; handler not invoked"} for action in plugin.actions]
    item["counts"] = {name: sum(row["class"] == name for row in item["verbs"]) for name in CLASSES}
    item["compliant"] = item["counts"]["FAST_RETURN"] + item["counts"]["LOCAL_FAST"]
    item["violations"] = item["counts"]["INLINE_IO"] + item["counts"]["CPU_INLINE"]
    return item


def _local_static_risks(plugins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"plugin": plugin["plugin"], "verb": row["verb"], "hits": row["static_hits"], "dynamic_edges": row.get("static_dynamic_edges", [])} for plugin in plugins for row in plugin["verbs"] if row["class"] == "LOCAL_FAST" and row["static_hits"]]


def _missing_causes(plugins: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(row["missing_piece"]) for plugin in plugins for row in plugin["verbs"] if row["class"] == "NOT_MEASURABLE")


def _finish_report(report: dict[str, Any], inventory: list[PluginVerbs]) -> None:
    report["manifest_plugin_count"] = len(inventory)
    report["manifest_verb_count"] = sum(len(plugin.actions) for plugin in inventory)
    report["totals"] = {name: sum(plugin["counts"][name] for plugin in report["plugins"]) for name in CLASSES}
    report["compliant"] = report["totals"]["FAST_RETURN"] + report["totals"]["LOCAL_FAST"]
    report["violations"] = report["totals"]["INLINE_IO"] + report["totals"]["CPU_INLINE"]
    report["local_fast_static_risks"] = _local_static_risks(report["plugins"])
    report["no_worker_primitive"] = [plugin["plugin"] for plugin in report["plugins"] if plugin["verb_count"] and not plugin["static"]["worker_primitives"]]
    causes = _missing_causes(report["plugins"])
    report["missing_prerequisites"] = dict(causes)
    finish_context_report(report)
    common = causes.most_common(1)
    if common and report["manifest_verb_count"] and common[0][1] / report["manifest_verb_count"] > 0.4:
        report["advisories"] = [f"more than 40 percent share one missing prerequisite: {common[0]}"]


def _census_inventory(root: Path, scratch: Path, settings: Settings, audit: list[dict[str, Any]] | None = None) -> list[PluginVerbs]:
    # The shared editable venv lists the shared checkout on sys.path. Keep
    # runtime/library paths, replace every first-party source root with this
    # exact tree before even importing transport dependencies.
    allowed = (root, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), *library_roots())
    sys.path[:] = [value for value in sys.path if any(Path(value).resolve().is_relative_to(base) for base in allowed)]
    sys.path[:0] = [str(root / "ananta/src"), *(str(path) for path in sorted(root.glob("plugins/*/src")))]
    probe = Probe(settings)
    with isolated_boundaries(root, scratch, probe) as guard:
        inventory = enumerate_verbs(root)
        if audit is not None:
            audit.extend(guard.files)
        return inventory


def _selected(name: str, measure: bool, only: set[str] | None) -> bool:
    return measure and (only is None or name in only)


def run_census(root: Path, scratch: Path, settings: Settings, *, measure: bool = False, only: set[str] | None = None, resume_index: Path | None = None, checkpoint_dir: Path | None = None, context_factory: Callable[..., Any] = plugin_context) -> dict[str, Any]:
    """Own the run's isolated app home, including import/enumeration cleanup."""
    root, scratch = root.resolve(), scratch.resolve()
    _check_scratch(root, scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    with temporary_app_home(scratch) as home:
        report = _run_census(root, home, settings, measure=measure, only=only, resume_index=resume_index, checkpoint_dir=checkpoint_dir, context_factory=context_factory)
        report["fixture_app_home"] = str(home)
    report["fixture_removed"] = not home.exists()
    return report


def _run_census(root: Path, scratch: Path, settings: Settings, *, measure: bool = False, only: set[str] | None = None, resume_index: Path | None = None, checkpoint_dir: Path | None = None, context_factory: Callable[..., Any] = plugin_context) -> dict[str, Any]:
    """Enumerate, then reuse completed plugins before considering any dispatch.

    Registry names and evidence hashes are integrity premises, not latency
    thresholds. A slow handler or any proportion of NOT_MEASURABLE rows remains
    reportable. Checkpoints bind settings and registry identity; source revision
    provenance remains the caller's responsibility and must accompany a handoff.
    """
    root, scratch = root.resolve(), scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    completed = load_checkpoint(resume_index, settings) if resume_index is not None else {}
    inventory_audit: list[dict[str, Any]] = []
    inventory = _census_inventory(root, scratch, settings, inventory_audit)
    modules = discover_preparers()
    _validate_population(root, inventory, completed)
    report: dict[str, Any] = {
        "schema": "fast-return-census/v2",
        "settings": asdict(settings),
        "mode": "measurement" if measure or completed else "enumeration",
        "classification_ruling": CLASSIFICATION_RULING,
        "plugins": [],
        "inventory_file_touches": inventory_audit,
        "stopped": "",
        "limits": [
            "workers parked; reachability is enqueue evidence, not completed vendor work",
            "static graph cannot resolve every dynamically chosen callable",
            "NOT_MEASURABLE retains the exact missing fixture or classification premise",
            "checkpoint hashes bind data/settings/registry; caller retains measured source revision provenance",
        ],
        "enumeration": "manifest ananta.plugins entry points -> registration_census.class_registration_census; inherited/generated metadata included",
    }
    for plugin in inventory:
        name = plugin.entry.plugin
        selected = _selected(name, measure, only)
        item = completed.get(name)
        if item is None:
            item = _plugin_result(root, plugin, scratch, settings, selected, modules, context_factory)
            if selected and not plugin.error:
                completed[name] = item
                if checkpoint_dir is not None:
                    report["checkpoint_index"] = str(write_checkpoint(checkpoint_dir, completed, settings))
        report["plugins"].append(item)
        print(f"CENSUS {plugin.entry.plugin}: {len(item['verbs'])}/{len(plugin.actions)} {item['counts']}", file=sys.stderr, flush=True)
    _finish_report(report, inventory)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--measure", action="store_true")
    parser.add_argument("--plugin", action="append", default=[])
    parser.add_argument("--delay-seconds", type=float)
    parser.add_argument("--resume-index", type=Path, help="reuse hash-verified completed plugins without invoking their handlers")
    parser.add_argument("--checkpoint-dir", type=Path, help="append immutable per-plugin results and index snapshots here")
    args = parser.parse_args()
    settings = Settings.from_environment()
    if args.delay_seconds is not None:
        settings = Settings(args.delay_seconds, settings.fast_ms, settings.cpu_ms)
    from quality_gates.private_test_pg import MissingFixtureError as PrivateFixtureMissing
    from quality_gates.private_test_pg import skip_fixture

    started = time.perf_counter()
    try:
        report = run_census(args.root, args.output.parent, settings, measure=args.measure, only=set(args.plugin) or None, resume_index=args.resume_index, checkpoint_dir=args.checkpoint_dir)
    except PrivateFixtureMissing:
        return skip_fixture()
    report["elapsed_seconds"] = time.perf_counter() - started
    if report["elapsed_seconds"] > 300:
        report.setdefault("advisories", []).append("measurement exceeded the advisory 300 second budget")
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "totals": report["totals"], "stopped": report["stopped"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
