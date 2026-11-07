"""Pure registration projection from completed plugin classes.

Class decorators and inherited methods are visible without constructing a plugin.
This is a catalog projection, not a runtime census: instance-only actions and
get_available_actions overrides remain unmeasured. Runtime discovery still walks
dir(instance), validates signatures and attaches a copied metadata value.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace

from ananta.core.actions.action_metadata import ActionMetadata

# Methods that exist on PluginBase or are commonly added by plugins but
# are NOT @platform_process action methods. Discovered methods with these
# names are skipped during action enumeration. Names that don't exist on
# the class are harmlessly absent from `dir(plugin)` anyway, so this set
# can stay permissive without harm.
_SKIP_METHODS: frozenset[str] = frozenset(
    {
        "get_default_config",
        "set_orchestrator_ref",
        "get_available_actions",
        "execute",
        "set_config_provider",
        "set_event_bus",
        "_resolve_io_process_key",
        "set_validation_registry",
        "get_parameter_validators",
        "get_action_validators",
        "is_service_provider",
        "prepare_for_readiness",
        "supports_template_functions",
        "execute_template_function",
        "get_template_functions",
        "cleanup",
        "cleanup_event_handlers",
        "initialize_event_handlers",
        "execute_async",
        "get_custom_validators",
        "is_ready",
        "set_ready",
        "set_error",
        "set_action_factory",
        "set_state_service",
        "get_schema_definitions",
        "query_model",
        "query_model_async",
        "get_plugin_param",
        "set_blob_storage_service",
        "get_available_providers",
        "get_default_params",
        "get_service_metadata",
        "get_template_engine",
        "get_variable_resolver",
        "register_provider",
        "start_services",
        "stop_services",
        "clear_scheduled_action_async",
        "clear_scheduled_actions_by_tag_async",
        "create_cron_schedule_async",
        "execute_in_seconds_async",
        "process_telegram_action",
        "initialize",
    }
)


def is_candidate_action_name(plugin_class: type[object], name: str) -> bool:
    """Share runtime name/property exclusions before any instance getattr."""
    if name.startswith("_") or name in _SKIP_METHODS:
        return False
    return not any(isinstance(cls.__dict__.get(name), property) for cls in plugin_class.__mro__)


def should_skip_plugin_action(action: ActionMetadata) -> bool:
    """Keep action definitions out of the plugin process namespace."""
    return action.name == "evaluate_input"


def plugin_process_key(provider: str, action: ActionMetadata) -> str:
    """Project the runtime manager's provider identity and metadata action name."""
    return f"plugin::{provider}::{action.name}"


@dataclass(frozen=True)
class ClassRegistrationCensus:
    """Copied class metadata plus explicit limits on runtime coverage."""

    actions: tuple[ActionMetadata, ...]
    coverage_limits: tuple[str, ...] = (
        "instance_only_actions_unmeasured",
        "get_available_actions_overrides_unmeasured",
    )


def class_registration_census(
    plugin_class: type[object],
    provider: str,
) -> ClassRegistrationCensus:
    """Read completed-class metadata without invoking plugin descriptors.

    Static lookup avoids executing properties or custom descriptors. It includes
    inherited and class-decorator-generated functions, and unwraps static/class
    methods. This neither executes get_available_actions nor validates runtime
    signatures, binding policy, or readiness; those remain runtime responsibilities.
    """
    actions: list[ActionMetadata] = []
    names = sorted({name for cls in plugin_class.__mro__ for name in vars(cls)})
    for name in names:
        if not is_candidate_action_name(plugin_class, name):
            continue
        method = inspect.getattr_static(plugin_class, name)
        if isinstance(method, (staticmethod, classmethod)):
            method = method.__func__
        if not callable(method):
            continue
        metadata = inspect.getattr_static(method, "_platform_process_metadata", None)
        if isinstance(metadata, ActionMetadata) and not should_skip_plugin_action(metadata):
            actions.append(replace(metadata, plugin=provider))
    return ClassRegistrationCensus(tuple(actions))
