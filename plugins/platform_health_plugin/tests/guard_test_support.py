"""Shared real-platform guard smoke fixtures."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

__all__ = [
    "_PROCESS_ERROR_KEY",
    "_FixturePluginManager",
    "_FixtureSchedulingService",
    "_FixtureStateService",
    "_RecordingActionEventRecorder",
    "_RecordingTemplateEngine",
    "_SuffixStateService",
    "_fire",
    "_real_registry",
    "_run_init_service_manager",
]

REPO_ROOT = Path(__file__).resolve().parents[3]
for _entry in (
    REPO_ROOT,
    REPO_ROOT / "ananta" / "src",
    REPO_ROOT / "plugins" / "platform_health_plugin" / "src",
    REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src",
):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.kb_overlay_loader import KnowledgeBaseOverlayLoader  # noqa: E402
from ananta.core.process_registry.plugin_process_scanner import PluginProcessScanner  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator  # noqa: E402
from ananta.core.process_registry.service_interface_metadata_generator import ServiceInterfaceMetadataGenerator  # noqa: E402
from ananta.services.key_value_operations.value_codec import decode_value, encode_value  # noqa: E402
from default_scheduling_plugin.execution.action_executor import ActionExecutor  # noqa: E402
from default_scheduling_plugin.models import ActionData  # noqa: E402
from platform_health_plugin.constants import PLUGIN_NAME  # noqa: E402
from platform_health_plugin.plugin import PlatformHealthPlugin  # noqa: E402


class _FixtureSchedulingService:
    """Mirrors the REAL ``ananta.services.scheduling_service.SchedulingService``
    keyword-argument seam exactly (``cron_expression``, ``action_definitions``,
    ``label``, ``tags``, ``state`` as named kwargs; ``tag``/``state`` for the
    lookup) -- not the raw plugin's ``(params, state)`` wrapper, and not a
    looser ``**kwargs`` catch-all that would silently accept a call the real
    service would reject. A fake looser than the real seam cancels out real
    defects (this codebase's own standing lesson)."""

    def __init__(self, *, existing_schedules: list[dict[str, Any]] | None = None, fail_lookup: bool = False) -> None:
        self._existing = existing_schedules or []
        self._fail_lookup = fail_lookup
        self.create_calls: list[dict[str, Any]] = []
        self.get_schedules_by_tag_calls: list[dict[str, Any]] = []

    def get_schedules_by_tag(self, tag: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        self.get_schedules_by_tag_calls.append({"tag": tag, "state": state})
        if self._fail_lookup:
            return {"action_status": "error", "data": {}, "error": {"message": "state backend unavailable"}}
        matching = [s for s in self._existing if tag in s.get("tags", [])]
        return {"action_status": "completed", "data": {"schedules": matching, "count": len(matching), "tag": tag}}

    def create_cron_schedule(
        self,
        cron_expression: str,
        actions: list[dict[str, Any]] | None = None,
        action_definitions: list[dict[str, Any]] | None = None,
        memory_tag: str | None = None,
        label: str | None = None,
        tags: list[str] | str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "cron_expression": cron_expression,
                "actions": actions,
                "action_definitions": action_definitions,
                "memory_tag": memory_tag,
                "label": label,
                "tags": tags,
                "state": state,
            }
        )
        schedule_id = f"sch_smoke_{len(self.create_calls)}"
        # Persist like the real scheduler, so a later get_schedules_by_tag finds it.
        self._existing.append({"schedule_id": schedule_id, "tags": [tags] if isinstance(tags, str) else list(tags or [])})
        return {"action_status": "completed", "data": {"schedule_id": schedule_id, "message": "created"}}


class _FixturePluginManager:
    def __init__(self, plugins: dict[str, object]) -> None:
        self._plugins = plugins

    def get_plugin(self, plugin_name: str) -> object:
        return self._plugins.get(plugin_name)


class _FixtureStateService:
    """In-memory double for the ``get_key_value``/``set_key_value`` corner of
    ``StateManagementInterface`` — the same ``{namespace, key, scope}`` ->
    value shape and the same ``ActionResult`` envelope the real postgres
    provider returns (``key_value_ops.py``'s ``kv_get``/``kv_set``)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str], str] = {}
        self.get_calls = 0
        self.set_calls = 0

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> dict[str, Any]:
        self.get_calls += 1
        value = self._store.get((namespace, key, scope))
        if value is None:
            return {"action_status": "completed", "data": {"namespace": namespace, "key": key, "scope": scope, "value": None, "found": False}}
        return {"action_status": "completed", "data": {"namespace": namespace, "key": key, "scope": scope, "value": decode_value(value), "found": True}}

    def set_key_value(self, namespace: str, key: str, value: object, scope: str = "GLOBAL", ttl: int | None = None) -> dict[str, Any]:
        self.set_calls += 1
        try:
            encoded = encode_value(value)
        except (TypeError, ValueError) as error:
            return {"action_status": "failed", "data": {}, "error": {"code": "kv.set_failed", "message": str(error)}}
        self._store[(namespace, key, scope)] = encoded
        return {"action_status": "completed", "data": {"namespace": namespace, "key": key, "scope": scope}}

    def values_where(self, *, key_prefix: str, key_suffix: str) -> list[object]:
        """Stored values whose key starts and ends as given: the guard's own
        marker for one path, without this smoke pinning the whole key."""
        return [decode_value(v) for (_ns, key, _scope), v in self._store.items() if key.startswith(key_prefix) and key.endswith(key_suffix)]


_PROCESS_ERROR_KEY = "service_interface::inference_service::process_error"


class _PluginRegistryView:
    """The one attribute the KB overlay loader reads off a plugin manager."""

    def __init__(self, plugins: dict[str, object]) -> None:
        self.plugins = plugins


class _RecordingActionEventRecorder:
    def __init__(self) -> None:
        self.stored: list[dict[str, object]] = []

    def store_action_event(self, action: dict[str, object]) -> str:
        self.stored.append(action)
        return f"ae-smoke-{len(self.stored)}"


class _SuffixStateService:
    def generate_unique_string(self, length: int, encoding: str) -> ActionResult:  # noqa: ARG002 — ActionFactory's StateService protocol
        return {"action_status": "completed", "data": {"random_string": "s" * length}}


class _RecordingTemplateEngine:
    """Records every resolution and returns the definition unchanged. The
    real factory calls it only when the definition carries a template
    pattern, which an attached inference processor's prompt does."""

    def __init__(self) -> None:
        self.calls = 0

    def resolve_templates(self, action_def: dict[str, object], context: dict[str, object]) -> dict[str, object]:  # noqa: ARG002 — ActionFactory's TemplateEngine protocol
        self.calls += 1
        return action_def


def _real_registry() -> dict[str, object]:
    """The registry entries production builds for this plugin: the real
    ``PluginProcessScanner`` over the real plugin's ``@platform_process``
    metadata (EdgeProcessProvider validation included), then the real KB
    overlay merge of every shipped process JSON (which hard-fails on a
    missing one). The inference ``process_error`` entry is added after, so a
    verb that declares error customizations has a base template to attach --
    the attachment the control proves happens."""
    plugin = PlatformHealthPlugin()
    registry: dict[str, object] = {"processes": {}}
    schema_generator = InvocationSchemaGenerator()
    scanner = PluginProcessScanner(
        plugin_manager=None,  # type: ignore[arg-type] — unused by the per-plugin registration path
        validator=PluginRegistrationValidator(),
        metadata_generator=ServiceInterfaceMetadataGenerator(),
        schema_generator=schema_generator,
    )
    scanner._process_individual_plugin(PLUGIN_NAME, plugin, registry)  # noqa: SLF001 — the real per-plugin registration step
    KnowledgeBaseOverlayLoader(
        plugin_manager=_PluginRegistryView({PLUGIN_NAME: plugin}),  # type: ignore[arg-type] — only .plugins is read
        schema_generator=schema_generator,
    ).apply(registry)
    processes = registry["processes"]
    assert isinstance(processes, dict)
    processes[_PROCESS_ERROR_KEY] = {"action_definition_template": {"arguments": {"params": {}}}}
    return registry


def _fire(
    registry: dict[str, object],
    action: ActionData,
    schedule_state: dict[str, Any],
) -> tuple[dict[str, Any], _RecordingActionEventRecorder, tuple[bool, str | None], _RecordingTemplateEngine]:
    """One scheduler fire: the real ``ActionExecutor`` builds the definition
    and submits it to the real ``ActionFactory``, whose runtime action lands
    in the recorder."""
    recorder = _RecordingActionEventRecorder()
    template_engine = _RecordingTemplateEngine()
    factory = ActionFactory(
        process_registry=registry,
        template_engine=template_engine,  # type: ignore[arg-type]
        state_service=_SuffixStateService(),  # type: ignore[arg-type]
        action_event_recorder=recorder,
    )
    executor = ActionExecutor(action_factory=factory, logger=logging.getLogger("guard_test_support"))  # type: ignore[arg-type]
    session_id = schedule_state.get("session_id")
    flow_id = schedule_state.get("flow_id")
    built = executor._build_action_definition(action, session_id, flow_id)  # noqa: SLF001 — the real fire-time builder
    outcome = executor._execute_single_action(  # noqa: SLF001 — build + submit, exactly as a fire does
        action=action,
        schedule_id="sch_smoke",
        session_id=session_id,
        flow_id=flow_id,
        action_index=1,
        total_actions=1,
    )
    return built, recorder, outcome, template_engine


def _run_init_service_manager(orch: EventOrchestrator, scheduling: _FixtureSchedulingService) -> None:
    """What startup's init_service_manager step does for this seam: assign
    orch.service_manager, which carries scheduling_service."""
    orch.service_manager = SimpleNamespace(scheduling_service=scheduling)  # type: ignore[assignment]
