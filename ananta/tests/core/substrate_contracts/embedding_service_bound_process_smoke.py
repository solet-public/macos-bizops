#!/usr/bin/env python3
"""iss_5fdedd1c — an external consumer embeds through the BOUND embedding_service (no pytest).

Background: ``service_interface::embedding_service::generate_embeddings`` and
``get_embedding_dimension`` were declared ``is_discoverable=False`` (the decorator default) with
an empty ``embedding_description``, so ``process_search`` listed only the provider-named
``plugin::openai_embeddings_plugin::*`` actions -- which reach the pre-Core-AI LM Studio path --
and never the verb that follows the binding.  The declared return schema also named only
``action_status`` and ``data``, and the Core AI failure envelope carried no ``data`` key.

This smoke pins the fix through the real act-time entrypoint
(``ActionProcessor._execute_service_interface_action``) over registry entries built from the
live decorator metadata, with fake bound providers behind a REAL ``EventOrchestrator`` + ``ServiceBindings`` binding table:

  A. Declaration: both verbs discoverable, keyed on the service name (a ``ServiceName``, never a provider plugin), the schema names all five contract
     fields, and each KB JSON carries an in-bound ``embedding_description``.
  B. Routing follows the binding: two different fake providers give two different model ids and
     dimensions through the same process key; the verb's own answer never comes from a constant.
  C. Model and dimension accompany every result, success or batch, through dispatch.
  D. The real ``CoreAIEmbeddingsPlugin`` (fake native runtime): model and dimension come with
     the vectors; an over-batch, an over-long input, an empty input and a foreign model are all
     refused loudly with an error, never truncated and never answered by another provider; every
     result, success or refusal, carries action_status, data, actions, error and timestamp.

Project policy: no pytest.  Offline -- no live solet / Core AI assets / Postgres.
Exits 0 on success, 1 on first-failed-check aggregate.

Run from repo root:
    SOLET_NAME=<name> .venv/bin/python3 ananta/tests/core/substrate_contracts/embedding_service_bound_process_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import QueuedAction  # noqa: E402
from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.orchestration.service_bindings import ServiceBindings, ServiceName  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.interfaces.embedding_service_interface import (  # noqa: E402
    EmbeddingServiceInterface,
    TokenBudget,
)
from ananta.services.embedding_service.interfaces.public import EmbeddingServiceAPI  # noqa: E402
from coreai_embeddings_plugin.contracts import (  # noqa: E402
    DIMENSION,
    MODEL_ID,
    EmbeddingError,
    ErrorCode,
    result,
)
from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin  # noqa: E402
from substrate_contract_fixtures import Checker  # noqa: E402

_SERVICE = "embedding_service"
_CONTRACT_FIELDS = ("action_status", "data", "actions", "error", "timestamp")
_JSON_DIR = _REPO_ROOT / "ananta" / "knowledge_base" / "processes" / _SERVICE
_EMBEDDING_DESCRIPTION_BOUNDS = (200, 400)


class _FakeProvider(EmbeddingServiceInterface):
    """A bound provider with its own declared model and dimension."""

    def __init__(self, model: str, dimension: int, max_batch: int = 4) -> None:
        self.model = model
        self.dimension = dimension
        self._max_batch = max_batch

    def generate_embeddings(
        self, inputs: list[str], model: str | None = None, input_type: str = "text",
    ) -> ActionResult:
        if len(inputs) > self._max_batch:
            return _refusal(f"{self.model}: batch over {self._max_batch}")
        vectors = [[float(len(text))] * self.dimension for text in inputs]
        return result({"embeddings": vectors, "dimension": self.dimension, "model": self.model})

    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        return result({"dimension": self.dimension, "model": self.model})

    def list_models(self) -> ActionResult:
        return result({"models": []})

    def input_token_budget(self) -> TokenBudget | None:
        return None

    def is_ready(self) -> bool:
        return True

    def get_readiness_error(self) -> str | None:
        return None


def _refusal(message: str) -> ActionResult:
    return {
        "action_status": "error", "data": {}, "actions": [], "timestamp": "2026-10-01T00:00:00+00:00",
        "error": {"type": "FakeProviderError", "code": "fake.refused", "message": message},
    }


class _PluginTable:
    """The plugin manager seam: a loaded plugin by name, as ``PluginManager.get_plugin`` answers."""

    def __init__(self, plugins: dict[str, object]) -> None:
        self._plugins = plugins

    def get_plugin(self, name: str) -> object | None:
        return self._plugins.get(name)


_BINDING_HOMES = tempfile.TemporaryDirectory(prefix="embedding-bound-smoke-")


def _real_orchestrator(bindings: dict[str, str], plugins: dict[str, object]) -> EventOrchestrator:
    """The REAL EventOrchestrator.get_service over the REAL ServiceBindings loaded from a test binding table.

    Only the plugin manager is a stand-in (``__new__`` skips the startup that loads live plugins); the
    binding file, ``ServiceBindings.load`` and ``_get_bound_plugin_service`` are the production code, so a
    provider hard-coded in the binding resolution, or a cached first answer, cannot pass.
    """
    app_home = Path(tempfile.mkdtemp(dir=_BINDING_HOMES.name))
    (app_home / "config").mkdir()
    (app_home / "config" / ServiceBindings.CONFIG_FILENAME).write_text(json.dumps(bindings))
    service_bindings = ServiceBindings(app_home)
    service_bindings.load()
    orchestrator = EventOrchestrator.__new__(EventOrchestrator)
    orchestrator.APP_HOME = str(app_home)
    orchestrator.service_bindings = service_bindings
    setattr(orchestrator, "action_recorder", None)  # noqa: B010
    setattr(orchestrator, "plugin_manager", _PluginTable(plugins))  # noqa: B010
    return orchestrator


class _DispatchAction(QueuedAction):
    source_plugin: str | None = None


def _processor(bindings: dict[str, str], plugins: dict[str, object]) -> ActionProcessor:
    """The REAL ActionProcessor over registry entries built from the live decorator metadata."""
    processes: dict[str, object] = {}
    for api_method in (EmbeddingServiceAPI.generate_embeddings, EmbeddingServiceAPI.get_embedding_dimension):
        metadata = getattr(api_method, "_service_interface_metadata")  # noqa: B009
        processes[f"service_interface::{metadata.provider}::{metadata.name}"] = metadata.to_process_dict()
    return ActionProcessor(
        plugin_manager=PluginManager(),
        orchestrator=_real_orchestrator(bindings, plugins),  # type: ignore[arg-type]
        process_registry={"processes": processes},
    )


def _dispatch(processor: ActionProcessor, verb: str, arguments: dict[str, object]) -> dict[str, Any]:
    action = _DispatchAction(
        id="ae-embed-smoke", process_key=f"service_interface::{_SERVICE}::{verb}", parameters="{}",
        notes="", created_at="2026-10-01T00:00:00+00:00", session_id="sess-smoke", flow_id="flow-smoke",
    )
    return dict(processor._execute_service_interface_action(_SERVICE, verb, arguments, action))  # noqa: SLF001


def _has_contract(envelope: dict[str, Any]) -> bool:
    return all(field in envelope for field in _CONTRACT_FIELDS)


def _call(checker: Checker, label: str, processor: ActionProcessor, verb: str, arguments: dict[str, object]) -> dict[str, Any]:
    """Dispatch; a framework refusal is recorded as a named failure and answers an empty envelope.

    A run under a broken routing mutant then ends on named failures, never on a traceback.
    """
    try:
        return _dispatch(processor, verb, arguments)
    except FrameworkError as exc:
        checker.check(False, f"{label}: {verb} dispatch raised FrameworkError [{exc}]")
        return {}


def _payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """``data.result`` of a result envelope, or an empty dict when it carries none."""
    data = envelope.get("data")
    inner = data.get("result") if isinstance(data, dict) else None
    return inner if isinstance(inner, dict) else {}


def _pin_declaration(checker: Checker) -> None:
    """Case A: discoverable, service-keyed, five-field schema, in-bound embedding_description."""
    service_names = {service.value for service in ServiceName}
    for verb in ("generate_embeddings", "get_embedding_dimension"):
        metadata = getattr(getattr(EmbeddingServiceAPI, verb), "_service_interface_metadata")  # noqa: B009
        process = metadata.to_process_dict()
        key = f"service_interface::{metadata.provider}::{metadata.name}"
        checker.check(process["is_discoverable"] is True, f"A1: {verb} is discoverable (process_search can find it)")
        checker.check(
            metadata.provider == _SERVICE and metadata.provider in service_names,
            f"A2: {verb} is keyed on the service name {_SERVICE}, a ServiceName, never a provider plugin ({key})",
        )
        properties = process["return_value_schema"]["properties"]
        checker.check(
            set(properties) == set(_CONTRACT_FIELDS),
            f"A3: {verb} return schema names exactly the five contract fields (got {sorted(properties)})",
        )
        text = json.loads((_JSON_DIR / f"{verb}.json").read_text())["embedding_description"]
        low, high = _EMBEDDING_DESCRIPTION_BOUNDS
        checker.check(low <= len(text) <= high, f"A4: {verb} embedding_description is {len(text)} chars, within {low}-{high}")
    schema_text = json.dumps(
        getattr(EmbeddingServiceAPI.generate_embeddings, "_service_interface_metadata").to_process_dict()  # noqa: B009
    )
    checker.check(
        "model" in schema_text and "dimension" in schema_text and "embeddings" in schema_text,
        "A5: generate_embeddings schema declares embeddings, dimension and model",
    )


def _pin_bound_provider(checker: Checker, bound: str, plugins: dict[str, object]) -> tuple[object, object]:
    """Cases B1 + C: one binding's embed and read-only answers are that provider's own."""
    provider = plugins[bound]
    processor = _processor({_SERVICE: bound}, plugins)
    embedded = _call(checker, f"B1 [{bound}]", processor, "generate_embeddings", {"inputs": ["one", "three"]})
    payload = _payload(embedded)
    checker.check(
        isinstance(provider, _FakeProvider)
        and payload.get("model") == provider.model and payload.get("dimension") == provider.dimension,
        f"B1: bound to {bound}, generate_embeddings returns that provider's model and dimension",
    )
    vectors = payload.get("embeddings", [])
    checker.check(
        len(vectors) == 2 and all(len(v) == payload.get("dimension") for v in vectors),
        f"C1: {bound}: one vector per input, each of the reported dimension",
    )
    checker.check(_has_contract(embedded), f"C2: {bound}: generate_embeddings carries all five contract fields")
    probed = _call(checker, f"C3 [{bound}]", processor, "get_embedding_dimension", {})
    answer = _payload(probed)
    checker.check(
        (answer.get("model"), answer.get("dimension")) == (payload.get("model"), payload.get("dimension")) and bool(answer)
        and _has_contract(probed),
        f"C3: {bound}: get_embedding_dimension agrees with the embed result without embedding",
    )
    return payload.get("model"), payload.get("dimension")


def _pin_routing(checker: Checker) -> None:
    """Cases B + C: the binding picks the provider; model and dimension are the provider's own."""
    plugins: dict[str, object] = {
        "provider_a": _FakeProvider("fake/model-a", 8),
        "provider_b": _FakeProvider("fake/model-b", 16),
    }
    seen = {bound: _pin_bound_provider(checker, bound, plugins) for bound in ("provider_a", "provider_b")}
    checker.check(seen["provider_a"] != seen["provider_b"], "B2: two bindings give two different model ids and dimensions")
    _pin_passthrough_refusal(checker, plugins)
    _pin_unbound_refusal(checker, plugins)


def _pin_passthrough_refusal(checker: Checker, plugins: dict[str, object]) -> None:
    """Case B3: a bound provider's own over-limit refusal reaches the caller whole."""
    refused = _call(checker, "B3", _processor({_SERVICE: "provider_a"}, plugins), "generate_embeddings", {"inputs": ["x"] * 5})
    checker.check(
        refused.get("action_status") == "error" and refused.get("error") is not None and refused.get("data") == {}
        and _has_contract(refused),
        "B3: a provider's over-limit refusal passes through dispatch as an error with the contract fields",
    )


def _pin_unbound_refusal(checker: Checker, plugins: dict[str, object]) -> None:
    """Case B4: with no binding the dispatch raises the framework's own refusal, no fallback provider."""
    label = "B4: no binding raises FrameworkError \"Service 'embedding_service' not available in orchestrator\""
    try:
        _dispatch(_processor({}, plugins), "generate_embeddings", {"inputs": ["x"]})
    except FrameworkError as exc:
        checker.check(str(exc) == f"Service '{_SERVICE}' not available in orchestrator", f"{label} (got {exc})")
    except Exception as exc:  # noqa: BLE001 -- any other type is the failure being recorded
        checker.check(False, f"{label} [raised {type(exc).__name__}: {exc}]")
    else:
        checker.check(False, f"{label} [did not raise]")


class _FakeRuntime:
    """Stands in for the native Core AI runtime at the plugin's ``_runtime`` seam."""

    def __init__(self, ceiling: int) -> None:
        self._ceiling = ceiling
        self.generated: list[list[str]] = []

    def generate(self, inputs: list[str]) -> list[list[float]]:
        for text in inputs:
            if len(text) > self._ceiling:
                raise EmbeddingError(ErrorCode.INPUT_TOO_LONG, f"Input has {len(text)} tokens; maximum is {self._ceiling}")
        self.generated.append(list(inputs))
        return [[0.5] * DIMENSION for _ in inputs]

    def count_tokens(self, text: str) -> int:
        return len(text)

    def close(self) -> None:
        return None

    def diagnostics(self) -> dict[str, object]:
        return {}


def _real_coreai_plugin(runtime: _FakeRuntime) -> CoreAIEmbeddingsPlugin:
    plugin = CoreAIEmbeddingsPlugin({})
    setattr(plugin, "_runtime", runtime)  # noqa: B010
    plugin.set_ready()
    return plugin


def _pin_real_success(checker: Checker, processor: ActionProcessor) -> None:
    """Cases D1-D3: the real plugin's answers carry its own model and dimension."""
    embedded = _call(checker, "D1", processor, "generate_embeddings", {"inputs": ["alpha", "beta"]})
    payload = _payload(embedded)
    checker.check(
        payload.get("model") == MODEL_ID and payload.get("dimension") == DIMENSION and len(payload.get("embeddings", [])) == 2,
        "D1: real Core AI result carries the model id, the dimension and one vector per input",
    )
    checker.check(_has_contract(embedded) and embedded.get("error") is None, "D2: real Core AI success carries all five contract fields")
    described = _call(checker, "D3", processor, "get_embedding_dimension", {})
    checker.check(
        _payload(described) == {"dimension": DIMENSION, "model": MODEL_ID} and _has_contract(described),
        "D3: real Core AI get_embedding_dimension returns model and dimension without embedding",
    )


def _pin_real_refusals(checker: Checker, processor: ActionProcessor, runtime: _FakeRuntime) -> None:
    """Cases D4-D6: every over-limit or unsupported request is refused loudly, never partly served."""
    refusals: dict[str, dict[str, object]] = {
        "over-batch (129 inputs)": {"inputs": ["a"] * 129},
        "over-long input": {"inputs": ["z" * 51]},
        "empty list": {"inputs": []},
        "blank text": {"inputs": ["  "]},
        "foreign model": {"inputs": ["a"], "model": "some/other-model"},
    }
    batches_before = len(runtime.generated)
    for label, arguments in refusals.items():
        refused = _call(checker, f"D4 [{label}]", processor, "generate_embeddings", arguments)
        checker.check(
            refused.get("action_status") == "error" and refused.get("error") is not None and refused.get("data") == {}
            and _has_contract(refused),
            f"D4: {label} is refused with an error and all five contract fields, no vectors",
        )
    checker.check(runtime.generated[batches_before:] == [], "D5: no refused call embedded a partial batch")
    wrong = _call(checker, "D6", processor, "get_embedding_dimension", {"model": "some/other-model"})
    checker.check(
        wrong.get("action_status") == "error" and wrong.get("error") is not None and _has_contract(wrong),
        "D6: a foreign model on the read-only verb is refused with all five contract fields",
    )


def _pin_real_provider(checker: Checker) -> None:
    """Case D: the real Core AI plugin behind the binding, through the same dispatch."""
    runtime = _FakeRuntime(ceiling=50)
    plugin = _real_coreai_plugin(runtime)
    processor = _processor({_SERVICE: "coreai_embeddings_plugin"}, {"coreai_embeddings_plugin": plugin})
    _pin_real_success(checker, processor)
    _pin_real_refusals(checker, processor, runtime)


def main() -> int:
    checker = Checker("embedding_service bound process smoke")
    print("== A. declaration ==")
    _pin_declaration(checker)
    os.environ.pop("ANANTA_EMBEDDING_SERVICE", None)  # an env binding would override the test table
    print("== B/C. routing follows the binding ==")
    _pin_routing(checker)
    print("== D. real Core AI provider ==")
    _pin_real_provider(checker)
    for module in ("ananta", "coreai_embeddings_plugin"):
        print(f"loaded {module} from {Path(str(sys.modules[module].__file__)).resolve()}")
    return checker.summary()


if __name__ == "__main__":
    sys.exit(main())
