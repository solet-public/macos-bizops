"""Lifecycle Management Service Public API.

AI-discoverable service lifecycle operations with @service_interface_process decorators.
All methods in this interface are indexed for process discovery.

Discoverability Policy (Task #47, 2026-05-24):
- EVERY method declares ``is_discoverable=True`` explicitly. The base decorator
  default for ``@service_interface_process`` is ``is_discoverable=False`` (service
  methods are presumed internal); lifecycle-management operations are all
  operator-callable (start_service_via_interface, stop_service,
  reload_python_module, list_plugins, list_available_plugins,
  set_plugin_enabled, set_plugin_priority, reload_plugin_config,
  update_platform_config, install_plugin_from_path, apply_manifest), so the
  per-method flag overrides the default. the solet is the operator's primary tool;
  these are first-class agent operations.
- Adding a new method without ``is_discoverable=True`` will SILENTLY exclude it
  from ``process_search`` and the agent will not be able to find it.
"""

from abc import ABC, abstractmethod
from typing import Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.services.call_context import CallContext
from ananta.core.services.service_interface_decorator import service_interface_process
from ananta.core.state.async_job_manager import registered_job_return_schema

LIST_PLUGINS_FILTER_VALUES: tuple[str, ...] = (
    "enabled",
    "loaded",
    "lifecycle_managed",
)

PLATFORM_CONFIG_TAKES_EFFECT_NEXT_RESTART = "next_restart"
PLATFORM_CONFIG_TAKES_EFFECT_IMMEDIATE = "immediate"


def _work_folder_row_properties() -> dict[str, ParameterMetadata]:
    """The keys of one work-folder row, shared by the verbs that return a row."""
    return {
        "folder_id": ParameterMetadata(type=ParameterType.STRING, description="Id of the record (wkf-<hex>).", required=True),
        "path": ParameterMetadata(type=ParameterType.STRING, description="Canonical real path of the folder.", required=True),
        "state": ParameterMetadata(type=ParameterType.STRING, description="'open', 'due', 'harvested', 'retired' or 'deleted'.", required=True),
        "version": ParameterMetadata(type=ParameterType.INTEGER, description="Row version: 1 when opened, raised by one at every change.", required=True),
        "dev": ParameterMetadata(type=ParameterType.STRING, description="st_dev of the directory when it was opened, as decimal text.", required=True),
        "ino": ParameterMetadata(type=ParameterType.STRING, description="st_ino of the directory when it was opened, as decimal text.", required=True),
        "purpose": ParameterMetadata(type=ParameterType.STRING, description="What the folder is for, in the creator's words.", required=True),
        "bound_kind": ParameterMetadata(type=ParameterType.STRING, description="Kind of the work item the folder belongs to.", required=True),
        "bound_ref": ParameterMetadata(type=ParameterType.STRING, description="Reference of the work item the folder belongs to.", required=True),
        "harvest_kind": ParameterMetadata(type=ParameterType.STRING, description="'regenerable', 'evidence_copy' or 'custody_packet': what must be kept before removal.", required=True),
        "owner": ParameterMetadata(type=ParameterType.STRING, description="Session or role that owns the folder; empty when none was given.", required=True),
        "harvest_receipt": ParameterMetadata(type=ParameterType.STRING, description="JSON text of the receipt recorded at harvest; empty until then and again after a reopen.", required=True),
        "advisories": ParameterMetadata(
            type=ParameterType.LIST,
            description="What this call has to tell the caller without refusing: each entry has code, message, folder_id and path. Empty unless the call found something to report.",
            required=True,
        ),
    }


class LifecycleManagementAPI(ABC):
    """Public lifecycle management operations - AI-discoverable via process registry.

    This interface defines service lifecycle operations that can be discovered
    and invoked by the AI orchestration system:

    1. start_service_via_interface - Start a service plugin with configuration
    2. stop_service - Stop a running service
    3. reload_python_module - Reload a single RELOAD_SAFE module without restart
    4. list_plugins - Enumerate the live plugin roster with status / config metadata
    5. list_available_plugins - Enumerate plugins loadable into the next manifest
       (installed entry points + source-tree candidates)
    6. apply_manifest - Validate + atomically write a new manifest + service
       bindings, then delegate restart to the bound self_deployment_service plugin

    Each method is decorated with complete metadata for process registry.
    """

    @service_interface_process(
        is_async=True,
        name="start_service_via_interface",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "service": ParameterMetadata(
                description="Service definition with provider information",
                required=True,
                type=ParameterType.OBJECT,
            ),
            "start": ParameterMetadata(
                description="Start action file to load (e.g., 'evaluate_input.json')",
                required=False,
                type=ParameterType.STRING,
            ),
            "config": ParameterMetadata(
                description="Service-specific configuration",
                required=False,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description="Service start result",
                type=ParameterType.OBJECT,
                properties={
                    "success": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description="Whether service started successfully",
                        required=False,
                    ),
                    "service_name": ParameterMetadata(type=ParameterType.STRING, description="Name of started service", required=False),
                    "message": ParameterMetadata(type=ParameterType.STRING, description="Status message", required=False),
                },
                usage_patterns=[
                    "Start console service for user interaction",
                    "Start background services for processing",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def start_service_via_interface(
        self,
        service: dict[str, Any] | str,
        start: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a service plugin with specified configuration.

        Args:
            service: Service definition dict with 'name' key, or service name string
            start: Start action file to load (e.g., 'evaluate_input.json')
            config: Service-specific configuration

        Returns:
            ActionResult with service start status
        """
        ...

    @service_interface_process(
        is_async=True,
        name="stop_service",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "service": ParameterMetadata(description="Name of the service to stop", required=True, type=ParameterType.STRING),
        },
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description="Service stop result",
                type=ParameterType.OBJECT,
                properties={
                    "success": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description="Whether service stopped successfully",
                        required=False,
                    ),
                    "service_name": ParameterMetadata(type=ParameterType.STRING, description="Name of stopped service", required=False),
                    "message": ParameterMetadata(type=ParameterType.STRING, description="Status message", required=False),
                },
                usage_patterns=[
                    "Stop console service cleanly",
                    "Shutdown background services",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    @abstractmethod
    def stop_service(self, service: str) -> dict[str, Any]:
        """Stop a running service.

        Args:
            service: Name of the service plugin to stop

        Returns:
            ActionResult with service stop status
        """
        ...

    @service_interface_process(
        is_async=True,
        name="reload_python_module",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "module_name": ParameterMetadata(
                description=("Fully-qualified Python module name to reload, as it appears in sys.modules (e.g. 'audio_processing_plugin.audio_analysis')."),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description="Module reload result",
                type=ParameterType.OBJECT,
                properties={
                    "success": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description="Whether the module was reloaded successfully",
                        required=False,
                    ),
                    "module_name": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Module that was targeted",
                        required=False,
                    ),
                    "message": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Status or refusal detail",
                        required=False,
                    ),
                },
                usage_patterns=[
                    "Reload a pure-DSP module after a code edit without restarting",
                    "Apply a synthesizers fix without losing blob storage state",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def reload_python_module(self, module_name: str) -> dict[str, Any]:
        """Reload a Python module marked ``RELOAD_SAFE = True`` without restarting the solet.

        The service refuses to reload modules that do not declare a module-level
        ``RELOAD_SAFE = True`` constant. Stateful modules (plugin classes,
        blob storage adapters, action queue, etc.) MUST NOT be marked safe;
        marking them is a configuration error that the safety gate protects
        against.

        Args:
            module_name: Fully-qualified module name as listed in ``sys.modules``.

        Returns:
            Result dict with success flag, module name, and status / refusal message.
        """
        ...

    @service_interface_process(
        name="list_plugins",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "filter": ParameterMetadata(
                description=(
                    "Optional filter restricting the returned roster. Accepted values: "
                    "'enabled' (only plugins with enabled != false in their config), "
                    "'loaded' (only plugins currently instantiated on the orchestrator), "
                    "'lifecycle_managed' (only plugins implementing the LifecycleManaged "
                    "protocol). Omit to return every loaded plugin."
                ),
                required=False,
                type=ParameterType.STRING,
                validation={"enum": list(LIST_PLUGINS_FILTER_VALUES)},
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Roster of plugins known to the live orchestrator",
            type=ParameterType.OBJECT,
            properties={
                "plugins": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Each entry carries name, version, readiness status, enabled flag, "
                        "load priority, number of registered processes, whether the plugin "
                        "is LifecycleManaged, whether its services are currently running, "
                        "and (when present) the last readiness error string and, for a ready "
                        "plugin missing an optional capability, a readiness warning string."
                    ),
                    required=False,
                ),
            },
            usage_patterns=[
                "Take inventory of installed plugins before reregistering one",
                "Check which plugins are LifecycleManaged before stopping services",
                "Diagnose missing capability by confirming the plugin is loaded and enabled",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def list_plugins(self, filter: str | None = None) -> dict[str, Any]:
        """Return the live plugin roster with status and config metadata.

        Args:
            filter: Optional roster filter — one of ``"enabled"``, ``"loaded"``,
                ``"lifecycle_managed"`` (validated against the allowed set). Omit
                to return every loaded plugin.

        Returns:
            Result dict containing ``plugins``: a list of dicts, each with
            ``name``, ``version``, ``status``, ``enabled``, ``priority``,
            ``process_count``, ``lifecycle_managed``, ``is_running``, and
            optionally ``last_error`` and ``warning`` (a ready plugin that is
            degraded, such as Apple FM on an ineligible device).
        """
        ...

    @service_interface_process(
        is_async=True,
        name="list_available_plugins",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={},
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description=("Plugins that could be loaded into the next manifest, sourced from installed entry points plus source-tree directories with valid plugin.yaml or pyproject.toml."),
                type=ParameterType.OBJECT,
                properties={
                    "plugins": ParameterMetadata(
                        type=ParameterType.LIST,
                        description=(
                            "Sorted list of per-plugin records. Each entry carries "
                            "name (str), source ('installed_entry_point' for plugins "
                            "registered via importlib.metadata.entry_points, or "
                            "'available_uninstalled' for plugins present in the "
                            "repo's plugins/ directory but not yet pip-installed), "
                            "has_metadata (bool), version (str|null), description "
                            "(str|null), and implements (list of interface short "
                            "names parsed from plugin.yaml)."
                        ),
                        required=False,
                    ),
                },
                usage_patterns=[
                    "Populate apply_manifest's diff preview before writing a new manifest",
                    "Confirm a candidate plugin is installable before referencing it in a manifest",
                    "Survey the solet image's plugin catalog",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def list_available_plugins(self) -> dict[str, Any]:
        """Enumerate plugins that could be loaded into the next manifest.

        Walks ``importlib.metadata.entry_points(group="ananta.plugins")``
        for installed plugins and the repo's ``plugins/`` directory for
        source-tree candidates not yet installed. The response shape is
        the input ``list_available_plugins`` expects from
        ``apply_manifest`` when populating its diff preview.

        Returns:
            Result dict containing ``plugins``: a list of dicts, each with
            ``name``, ``source`` (``"installed_entry_point"`` or
            ``"available_uninstalled"``), ``has_metadata``, ``version``,
            ``description``, and ``implements``.
        """
        ...

    @service_interface_process(
        is_async=True,
        name="set_plugin_enabled",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "plugin_name": ParameterMetadata(
                description=("Name of the plugin to enable or disable (matches the entry-point name and the key under orchestrator.plugin_manager.plugins)."),
                required=True,
                type=ParameterType.STRING,
            ),
            "enabled": ParameterMetadata(
                description=("Target state. True schedules the plugin into the live roster, load if needed, and starts services for LifecycleManaged plugins. False stops services, removes the plugin from the live roster, and refreshes the process registry so its action keys disappear."),
                required=True,
                type=ParameterType.BOOLEAN,
            ),
        },
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description="Plugin enable/disable result",
                type=ParameterType.OBJECT,
                properties={
                    "applied": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description="Whether the runtime state change took effect in this session",
                        required=False,
                    ),
                    "restart_required": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description="True if the persisted change needs a solet restart to fully apply",
                        required=False,
                    ),
                    "plugin_name": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Plugin the call targeted",
                        required=False,
                    ),
                    "message": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Status or refusal detail",
                        required=False,
                    ),
                },
                usage_patterns=[
                    "Disable a misbehaving plugin without restarting the solet",
                    "Re-enable a plugin after fixing its config",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def set_plugin_enabled(self, plugin_name: str, enabled: bool) -> dict[str, Any]:
        """Toggle a plugin's enabled state, persisting and applying live.

        Writes ``enabled`` into the per-plugin config file (creating it if
        absent) and then applies the change on the live orchestrator: enabling
        loads the plugin if it is not already present and starts its services
        when it is LifecycleManaged; disabling stops its services, removes it
        from the live roster, and refreshes the process registry so its action
        keys disappear from discovery.

        Args:
            plugin_name: Plugin to enable or disable.
            enabled: True to enable; False to disable.

        Returns:
            Result dict with ``applied``, ``restart_required``,
            ``plugin_name``, and ``message``.
        """
        ...

    @service_interface_process(
        is_async=True,
        name="set_plugin_priority",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "plugin_name": ParameterMetadata(
                description="Plugin whose load priority should change.",
                required=True,
                type=ParameterType.STRING,
            ),
            "priority": ParameterMetadata(
                description=("New load-priority integer. Lower values load earlier; foundational service plugins use values below 50. The value is persisted to the per-plugin config file and consulted by the plugin manager on the next start."),
                required=True,
                type=ParameterType.INTEGER,
            ),
        },
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description="Plugin priority update result",
                type=ParameterType.OBJECT,
                properties={
                    "applied": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description="Whether the change was persisted to disk",
                        required=False,
                    ),
                    "takes_effect": ParameterMetadata(
                        type=ParameterType.STRING,
                        description=("When the new priority becomes active. v1 always returns 'next_restart'; in-session reorder is out of scope."),
                        required=False,
                    ),
                    "plugin_name": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Plugin the call targeted",
                        required=False,
                    ),
                    "message": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Status or refusal detail",
                        required=False,
                    ),
                },
                usage_patterns=[
                    "Promote a plugin to load earlier on the next restart",
                    "Demote a plugin so a higher-priority alternative wins service binding",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def set_plugin_priority(self, plugin_name: str, priority: int) -> dict[str, Any]:
        """Persist a plugin's load priority; takes effect on the next start.

        Writes ``priority`` into the per-plugin config file (creating it if
        absent). The plugin manager re-reads the file on the next discovery
        pass and sorts entry points by the persisted value. v1 does not
        perform an in-session reorder, so the response carries
        ``takes_effect = "next_restart"``.

        Args:
            plugin_name: Plugin whose priority should change.
            priority: New load-priority integer.

        Returns:
            Result dict with ``applied``, ``takes_effect``, ``plugin_name``,
            and ``message``.
        """
        ...

    @service_interface_process(
        is_async=True,
        name="reload_plugin_config",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "plugin_name": ParameterMetadata(
                description="Plugin whose config should be re-read from disk.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description="Plugin config reload result",
                type=ParameterType.OBJECT,
                properties={
                    "success": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description="Whether the new config was pushed into the plugin",
                        required=False,
                    ),
                    "plugin_name": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Plugin the call targeted",
                        required=False,
                    ),
                    "dirty_keys": ParameterMetadata(
                        type=ParameterType.LIST,
                        description=("Config keys whose values differ between the prior in-memory view and the freshly-read on-disk file."),
                        required=False,
                    ),
                    "message": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Status or refusal detail",
                        required=False,
                    ),
                },
                usage_patterns=[
                    "Pick up a per-plugin config edit without restarting the solet",
                    "Apply a tweaked sample rate or model selection mid-session",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def reload_plugin_config(self, plugin_name: str) -> dict[str, Any]:
        """Re-read a plugin's config and push the new view into the plugin.

        Reads the on-disk per-plugin config, computes the diff against the
        plugin manager's cached view, refreshes the cache, and (when the
        plugin implements ``initialize``) calls ``initialize(new_config)``
        so the plugin can rebind its runtime parameters.

        Args:
            plugin_name: Plugin whose config should be re-read.

        Returns:
            Result dict with ``success``, ``plugin_name``, ``dirty_keys``
            (config keys whose values changed since the last load), and
            ``message``.
        """
        ...

    @service_interface_process(
        is_async=True,
        name="update_platform_config",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "scope": ParameterMetadata(
                description=("Top-level section of platform.json being mutated, e.g. 'logging'. Validated against the platform-config allowlist."),
                required=True,
                type=ParameterType.STRING,
            ),
            "key": ParameterMetadata(
                description=("Key within the scope, e.g. 'log_level'. Validated against the platform-config allowlist for the chosen scope."),
                required=True,
                type=ParameterType.STRING,
            ),
            "value": ParameterMetadata(
                description=("New value for the scope.key entry. Stored verbatim in platform.json; consumer responsibility to validate type/range."),
                required=True,
                type=ParameterType.OBJECT,
            ),
        },
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description="Platform config update result",
                type=ParameterType.OBJECT,
                properties={
                    "applied": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description="Whether the new value was persisted",
                        required=False,
                    ),
                    "prev_value": ParameterMetadata(
                        type=ParameterType.OBJECT,
                        description=("Previous value at scope.key (null when no prior entry existed)"),
                        required=False,
                    ),
                    "restart_required": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description=("True if the new value cannot be applied in-process and only takes effect on the next solet restart"),
                        required=False,
                    ),
                    "scope": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Scope written",
                        required=False,
                    ),
                    "key": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Key written",
                        required=False,
                    ),
                    "message": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Status or refusal detail",
                        required=False,
                    ),
                },
                usage_patterns=[
                    "Raise the platform log level to DEBUG without restarting",
                    "Persist an allowlisted env-var-like setting from a process call",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def update_platform_config(self, scope: str, key: str, value: Any) -> dict[str, Any]:
        """Persist a single platform-level config entry and apply it live where possible.

        Validates ``(scope, key)`` against the platform-config allowlist,
        writes ``platform.json`` under the active profile, and applies the
        change in-process for the small set of keys the platform knows how to
        rebind without a restart (currently only ``logging.log_level``). Other
        allowlisted entries are persisted but require a restart to take
        effect; ``restart_required`` discriminates.

        Args:
            scope: Top-level section of platform.json (e.g. ``"logging"``).
            key: Key within the scope (e.g. ``"log_level"``).
            value: New value to persist verbatim.

        Returns:
            Result dict with ``applied``, ``prev_value``, ``restart_required``,
            ``scope``, ``key``, and ``message``.
        """
        ...

    @service_interface_process(
        is_async=True,
        name="install_plugin_from_path",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "path": ParameterMetadata(
                description=("Filesystem path to the plugin source root (the directory that contains plugin.yaml or pyproject.toml). The directory will be installed into the active venv with `pip install -e <path>`."),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description="Plugin installation result",
                type=ParameterType.OBJECT,
                properties={
                    "installed": ParameterMetadata(
                        type=ParameterType.BOOLEAN,
                        description=("True when a brand-new plugin entry-point appeared after install"),
                        required=False,
                    ),
                    "plugin_name": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Name of the newly-installed plugin (empty if none new)",
                        required=False,
                    ),
                    "new_process_keys": ParameterMetadata(
                        type=ParameterType.LIST,
                        description="Process keys that the new plugin contributes to the registry",
                        required=False,
                    ),
                    "message": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Status or refusal detail",
                        required=False,
                    ),
                },
                usage_patterns=[
                    "Install a freshly-checked-out plugin without restarting the solet",
                    "Bring a sibling plugin online from a workbench scratch directory",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def install_plugin_from_path(self, path: str) -> dict[str, Any]:
        """Install a plugin from a local source directory at runtime.

        Validates that ``path`` exists and contains ``plugin.yaml`` or
        ``pyproject.toml``, runs ``pip install -e <path>`` against the active
        Python interpreter, re-runs entry-point discovery on the plugin
        manager, and reports whichever plugin entry-point is newly present.

        Args:
            path: Filesystem path to the plugin source root.

        Returns:
            Result dict with ``installed``, ``plugin_name``,
            ``new_process_keys``, and ``message``.
        """
        ...

    @service_interface_process(
        is_async=True,
        name="apply_manifest",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "new_manifest": ParameterMetadata(
                description=(
                    "Proposed manifest dict. v1.1 accepts "
                    "``{plugins: list[str], profile_name?: str, "
                    "service_bindings?: dict[str, str]}``. When supplied, "
                    "``service_bindings`` is merged over the currently-bound "
                    "services: caller values replace matching services and "
                    "omitted services keep their current bindings. This allows "
                    "plugin renames and service rebinds. If omitted, bindings "
                    "are synthesized from the current services. A non-object "
                    "``service_bindings`` value is rejected before disk activity. "
                    "``plugin_config_overrides`` remains rejected before disk "
                    "activity with ``forbidden_v1_keys_present``; full config "
                    "override externalization is deferred to v2. Per-plugin "
                    "config edits use ``reload_plugin_config``."
                ),
                required=True,
                type=ParameterType.OBJECT,
            ),
            "reason": ParameterMetadata(
                description=("Operator-supplied audit string. Recorded in the deployment plugin's restart message and surfaced in the response so future status calls can trace 'why was this restart triggered'."),
                required=True,
                type=ParameterType.STRING,
            ),
            "expected_etag": ParameterMetadata(
                description=(
                    "Compare-and-swap precondition. The caller passes the "
                    "etag they observed when they last read the on-disk "
                    "state. The verb refuses with status='rejected' + "
                    "reason='precondition_failed' if the on-disk etag has "
                    "shifted in the interim. Read the current etag from a "
                    "dry-run call's response (status='dry_run' carries the "
                    "current etag for first-time callers)."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "dry_run": ParameterMetadata(
                description=("When true, validate + diff + return without writing or delegating restart. Use to preview the change shape and to read the current etag before a subsequent committing call."),
                required=False,
                type=ParameterType.BOOLEAN,
                default=False,
            ),
        },
        return_value_schema=registered_job_return_schema(
            ReturnValueSchema(
                description="apply_manifest outcome",
                type=ParameterType.OBJECT,
                properties={
                    "status": ParameterMetadata(
                        type=ParameterType.STRING,
                        description=(
                            "'applied' when the manifest was validated, written, "
                            "and the deployment plugin reported a manifest-applied "
                            "restart status (queued / completed). "
                            "'dry_run' when the caller passed dry_run=true. "
                            "'rejected' when pre-flight validation or the CAS "
                            "check failed (no disk write happened); "
                            "rejection_reasons explains why. "
                            "'restart_failed_after_manifest_commit' (Codex review "
                            "#2 Finding 1) when the manifest WAS written locally "
                            "(and to S3 for cloud) but the deployment plugin "
                            "could not schedule the restart — a partial remote "
                            "commit that needs operator recovery; restart_status "
                            "carries the underlying failure token and "
                            "rejection_reasons formats the deploy error."
                        ),
                        required=False,
                    ),
                    "diff": ParameterMetadata(
                        type=ParameterType.OBJECT,
                        description=("Set-level diff between current and proposed: {added_plugins, removed_plugins, rebound_services}."),
                        required=False,
                    ),
                    "current_etag": ParameterMetadata(
                        type=ParameterType.STRING,
                        description=("The current on-disk etag at the moment of read. Populated on every response (rejection, dry-run, and committed). The caller passes this back as expected_etag on a subsequent committing call."),
                        required=False,
                    ),
                    "new_etag": ParameterMetadata(
                        type=ParameterType.STRING,
                        description=("Etag of the newly-written manifest + bindings pair. Populated when status='applied'."),
                        required=False,
                    ),
                    "manifest_written_to": ParameterMetadata(
                        type=ParameterType.STRING,
                        description=("Path of the manifest.yaml that was written. Populated when status='applied'."),
                        required=False,
                    ),
                    "service_bindings_written_to": ParameterMetadata(
                        type=ParameterType.STRING,
                        description=("Path of the service_bindings.json that was written. Populated when status='applied'."),
                        required=False,
                    ),
                    "restart_action_id": ParameterMetadata(
                        type=ParameterType.STRING,
                        description=(
                            "Identifier the bound deployment plugin returned "
                            "from its restart_with_manifest call. Semantics "
                            "depend on the implementation — cloud sibling "
                            "returns a pollable id consumable by "
                            "self_deployment_service::deploy_status; local "
                            "sibling returns an audit-only token. Empty "
                            "string on status='restart_failed_after_manifest_commit'."
                        ),
                        required=False,
                    ),
                    "restart_status": ParameterMetadata(
                        type=ParameterType.STRING,
                        description=(
                            "Verbatim status the bound deployment plugin "
                            "returned from restart_with_manifest. Populated "
                            "when status='applied' (carries the happy-path "
                            "token, e.g. 'queued' or 'completed') or "
                            "when status='restart_failed_after_manifest_commit' "
                            "(carries one of 'failed', 'unbound', "
                            "'plugin_protocol_error', 'plugin_raised') so the "
                            "operator can tell exactly what the deployment "
                            "plugin reported."
                        ),
                        required=False,
                    ),
                    "rejection_reasons": ParameterMetadata(
                        type=ParameterType.LIST,
                        description=("List of human-readable reasons populated when status='rejected'. Empty otherwise."),
                        required=False,
                    ),
                    "message": ParameterMetadata(
                        type=ParameterType.STRING,
                        description="Status detail.",
                        required=False,
                    ),
                },
                usage_patterns=[
                    "Preview a plugin add via dry_run=true; commit on a follow-up call",
                    "Atomically swap a service binding alongside a plugin set change",
                    "Roll forward to a new local manifest and trigger a self-restart",
                ],
            )
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def apply_manifest(
        self,
        new_manifest: dict[str, Any],
        reason: str,
        expected_etag: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Validate, write, and restart with a new manifest.

        The verb is the platform-tier orchestrator of the manifest
        lifecycle. Its v1.1 payload accepts ``plugins``, optional
        ``profile_name``, and optional ``service_bindings``. Caller-supplied
        bindings are merged over the current bindings, so caller values
        replace matching services while omitted services retain their
        current bindings. When omitted, current bindings are synthesized
        into the effective manifest. ``plugin_config_overrides`` remains
        rejected before any write with ``forbidden_v1_keys_present``
        (full config-override externalization is deferred to v2). The verb
        runs binding-satisfaction pre-flight against ``binding_validator``,
        computes a set-level diff, atomically writes the effective
        ``service_bindings.json`` followed by ``manifest.yaml`` under a sha256 CAS,
        then delegates the actual restart to whichever plugin is
        currently bound to ``self_deployment_service``. Per Codex review #2
        Finding 1, the restart status is propagated into the response —
        a write that succeeds but cannot schedule a cutover surfaces as
        ``status="restart_failed_after_manifest_commit"``, not
        ``"applied"``.

        Args:
            new_manifest: Proposed v1.1 manifest:
                ``{plugins: list[str], profile_name?: str,
                service_bindings?: dict[str, str]}``. Supplied bindings
                merge over current bindings; caller values win for matching
                services and omitted services keep their current bindings.
                ``plugin_config_overrides`` is rejected before disk activity
                with ``forbidden_v1_keys_present`` and remains deferred to v2.
            reason: Operator-supplied audit string.
            expected_etag: CAS token from the caller's prior read.
                When ``None``, the verb runs in unchecked mode (caller
                accepts the risk of a concurrent override).
            dry_run: When ``True``, validate + diff + return current
                etag without writing or restarting.

        Returns:
            Result dict per the return-value schema. ``status`` is one
            of ``"applied"``, ``"dry_run"``, ``"rejected"``, or
            ``"restart_failed_after_manifest_commit"``; ``restart_status``
            carries the deployment plugin's verbatim status on the
            applied/failed paths.
        """
        ...

    @service_interface_process(
        name="record_deployment",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "solet_name": ParameterMetadata(
                description="Name of the solet that was deployed.",
                required=True,
                type=ParameterType.STRING,
            ),
            "release_id": ParameterMetadata(
                description="Identifier of the release that went live.",
                required=True,
                type=ParameterType.STRING,
            ),
            "git_sha": ParameterMetadata(
                description="Full 40-character lowercase hex commit sha of the deployed tree.",
                required=True,
                type=ParameterType.STRING,
            ),
            "deployed_at": ParameterMetadata(
                description="ISO-8601 instant the release went live, with a UTC offset (for example 2026-11-13T12:00:00+00:00).",
                required=True,
                type=ParameterType.STRING,
            ),
            "trigger": ParameterMetadata(
                description="What caused the deploy: 'landing' or 'tick'.",
                required=True,
                type=ParameterType.STRING,
                validation={"enum": ["landing", "tick"]},
            ),
            "manifest_sha256": ParameterMetadata(
                description="sha256 of the manifest that was deployed.",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description="Agent instance recording the deployment. Recorded for history, not an authority.",
                required=True,
                type=ParameterType.STRING,
            ),
            "actor_model": ParameterMetadata(
                description="Model of the recording actor.",
                required=True,
                type=ParameterType.STRING,
            ),
            "actor_effort": ParameterMetadata(
                description="Effort tier of the recording actor.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="The deployment record row and whether this call created it",
            type=ParameterType.OBJECT,
            properties={
                "deployment_id": ParameterMetadata(type=ParameterType.STRING, description="Stable id of the record (dpr-<hex>).", required=True),
                "solet_name": ParameterMetadata(type=ParameterType.STRING, description="Solet the record belongs to.", required=True),
                "release_id": ParameterMetadata(type=ParameterType.STRING, description="Release that went live.", required=True),
                "git_sha": ParameterMetadata(type=ParameterType.STRING, description="Deployed commit sha.", required=True),
                "deployed_at": ParameterMetadata(type=ParameterType.STRING, description="Deploy instant, ISO-8601 in UTC with +00:00 offset.", required=True),
                "trigger": ParameterMetadata(type=ParameterType.STRING, description="'landing' or 'tick'.", required=True),
                "state": ParameterMetadata(type=ParameterType.STRING, description="'recorded' or 'withdrawn'.", required=True),
                "version": ParameterMetadata(type=ParameterType.INTEGER, description="Row version; the compare-and-set guard for set_deployment_state.", required=True),
                "created": ParameterMetadata(type=ParameterType.BOOLEAN, description="True when this call wrote the row; False when the same (solet, sha, release) already existed.", required=True),
            },
            usage_patterns=[
                "Record that a landing or tick deploy went live, once per (solet, sha, release)",
                "Repeat the call after a crash: a second call returns the same row with created False",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def record_deployment(
        self,
        solet_name: str,
        release_id: str,
        git_sha: str,
        deployed_at: str,
        trigger: str,
        manifest_sha256: str,
        agent_instance_id: str,
        actor_model: str,
        actor_effort: str,
    ) -> dict[str, Any]:
        """Record one deployment in the solet's own database; idempotent per (solet, sha, release).

        Returns:
            Result dict with ``deployment_id``, ``solet_name``, ``release_id``,
            ``git_sha``, ``deployed_at``, ``trigger``, ``state``, ``version``
            and ``created``. A malformed sha, trigger or timestamp is refused
            with the error code ``invalid_sha``, ``invalid_trigger`` or
            ``invalid_deployed_at``.
        """
        ...

    @service_interface_process(
        name="read_deployments",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "solet_name": ParameterMetadata(
                description="Solet whose deployments to read.",
                required=True,
                type=ParameterType.STRING,
            ),
            "mode": ParameterMetadata(
                description="'current' (newest non-withdrawn deployment, zero or one row), 'history' (newest first, withdrawn included, paged) or 'get' (one row by deployment_id).",
                required=True,
                type=ParameterType.STRING,
                validation={"enum": ["current", "history", "get"]},
            ),
            "deployment_id": ParameterMetadata(
                description="Record id to read. Required for mode 'get', ignored otherwise.",
                required=False,
                type=ParameterType.STRING,
            ),
            "limit": ParameterMetadata(
                description="Page size for mode 'history'. Default 20, maximum 100.",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "after": ParameterMetadata(
                description="The 'next' value of the previous history page, to continue it.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Deployment rows and the continuation token",
            type=ParameterType.OBJECT,
            properties={
                "rows": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=("Deployment rows, each with deployment_id, solet_name, release_id, git_sha, deployed_at, trigger, state and version."),
                    required=True,
                ),
                "next": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Token to pass as 'after' for the next history page; null when there is no further page.",
                    required=False,
                ),
            },
            usage_patterns=[
                "Ask what is deployed now and since when (mode current)",
                "List past deployments newest first, including withdrawn ones (mode history)",
                "Re-read one record before withdrawing it (mode get)",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def read_deployments(
        self,
        solet_name: str,
        mode: str,
        deployment_id: str | None = None,
        limit: int = 20,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Read the solet's deployment records.

        Returns:
            Result dict with ``rows`` and ``next``. Mode ``get`` with an id that
            is unknown (or belongs to another solet) is refused with the error
            code ``unknown_deployment``; a bad mode, limit or ``after`` is
            refused with ``invalid_argument``.
        """
        ...

    @service_interface_process(
        name="set_deployment_state",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "deployment_id": ParameterMetadata(
                description="Id of the deployment record to move.",
                required=True,
                type=ParameterType.STRING,
            ),
            "withdrawn": ParameterMetadata(
                description="True withdraws the record; False reinstates a withdrawn one.",
                required=True,
                type=ParameterType.BOOLEAN,
            ),
            "expected_version": ParameterMetadata(
                description="The row version the caller last read; a different current version refuses the write.",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "reason": ParameterMetadata(
                description="Why the record is withdrawn or reinstated; kept in the record's history.",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description="Agent instance making the change. Recorded for history, not an authority.",
                required=True,
                type=ParameterType.STRING,
            ),
            "actor_model": ParameterMetadata(
                description="Model of the acting actor.",
                required=True,
                type=ParameterType.STRING,
            ),
            "actor_effort": ParameterMetadata(
                description="Effort tier of the acting actor.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="The deployment record after the state change",
            type=ParameterType.OBJECT,
            properties={
                "deployment_id": ParameterMetadata(type=ParameterType.STRING, description="Id of the record.", required=True),
                "solet_name": ParameterMetadata(type=ParameterType.STRING, description="Solet the record belongs to.", required=True),
                "release_id": ParameterMetadata(type=ParameterType.STRING, description="Release that went live.", required=True),
                "git_sha": ParameterMetadata(type=ParameterType.STRING, description="Deployed commit sha.", required=True),
                "deployed_at": ParameterMetadata(type=ParameterType.STRING, description="Deploy instant, ISO-8601 in UTC with +00:00 offset.", required=True),
                "trigger": ParameterMetadata(type=ParameterType.STRING, description="'landing' or 'tick'.", required=True),
                "state": ParameterMetadata(type=ParameterType.STRING, description="'recorded' or 'withdrawn' after the change.", required=True),
                "version": ParameterMetadata(type=ParameterType.INTEGER, description="New row version (expected_version + 1).", required=True),
            },
            usage_patterns=[
                "Withdraw a deployment that was recorded wrongly, then record the right one",
                "Reinstate a record that was withdrawn by mistake",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def set_deployment_state(
        self,
        deployment_id: str,
        withdrawn: bool,
        expected_version: int,
        reason: str,
        agent_instance_id: str,
        actor_model: str,
        actor_effort: str,
    ) -> dict[str, Any]:
        """Withdraw or reinstate a deployment record by compare-and-set on its version.

        Returns:
            The row after the change. A version that moved since the caller read
            it is refused with the error code ``stale_version`` and nothing is
            written; an unknown id is refused with ``unknown_deployment``.
        """
        ...

    @service_interface_process(
        name="run_deploy_rule",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "solet_name": ParameterMetadata(
                description="This solet's name. The verb runs only this solet's own deploy rule; another name is refused with solet_mismatch.",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description="Agent instance asking for the deploy. The rule records it on the deployment for history; it is not an authority.",
                required=True,
                type=ParameterType.STRING,
            ),
            "actor_model": ParameterMetadata(
                description="Model of the asking actor, recorded with the deployment.",
                required=True,
                type=ParameterType.STRING,
            ),
            "actor_effort": ParameterMetadata(
                description="Effort tier of the asking actor, recorded with the deployment.",
                required=True,
                type=ParameterType.STRING,
            ),
            "trigger": ParameterMetadata(
                description="'landing' (default) skips a master move that changed no production path; 'tick' deploys master whatever changed.",
                required=False,
                type=ParameterType.STRING,
                default="landing",
                validation={"enum": ["landing", "tick"]},
            ),
            "attempt_id": ParameterMetadata(
                description="An attempt id this verb answered earlier. With it the verb only reads that attempt: still running, or its terminal outcome. Without it the verb starts the rule, or attaches to the attempt already running.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="The deploy-rule attempt's typed outcome",
            type=ParameterType.OBJECT,
            properties={
                "outcome": ParameterMetadata(type=ParameterType.STRING, description="'deployed' (the target is the recorded current deployment), 'skipped' (a landing with no production path), 'deferred' (running, or held: ask again) or 'failed' (needs a person).", required=True),
                "decision": ParameterMetadata(type=ParameterType.STRING, description="The rule's own terminal decision (for example deployed, already_current, deferred_gate, failed), or in_progress, attempt_abandoned or start_failed.", required=True),
                "attempt_id": ParameterMetadata(type=ParameterType.STRING, description="Id of the attempt (dra-<UTC time>-<hex>) to pass back as attempt_id; null when no attempt was started.", required=False),
                "target": ParameterMetadata(type=ParameterType.STRING, description="Full master commit sha the attempt deploys; the rule's own receipt wins once it has one. Null when master could not be read.", required=False),
                "child_pid": ParameterMetadata(type=ParameterType.INTEGER, description="Pid of the detached rule process; null when none was started.", required=False),
                "receipt": ParameterMetadata(type=ParameterType.OBJECT, description="The rule's terminal receipt once the attempt has finished; null while it runs or when it ended without one.", required=False),
                "detail": ParameterMetadata(type=ParameterType.STRING, description="Bounded text for a failure: the rule's error, the start error, or the end of an abandoned attempt's stderr; null otherwise.", required=False),
            },
            usage_patterns=[
                "After landing a solet repository, ask that solet to deploy master by its own rule, then read the attempt by id until it is terminal",
                "A person deploys the solet's master now with the same guarded rule",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def run_deploy_rule(
        self,
        solet_name: str,
        agent_instance_id: str,
        actor_model: str,
        actor_effort: str,
        trigger: str = "landing",
        attempt_id: str | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Start this solet's guarded deploy rule detached, or read one attempt of it.

        The rule (``_deploy_by_rule.py``, run from the solet's checkout) holds
        its own lock, admission and blue-green swap, keeps the old release
        serving on failure and sends its existing needs-a-person notice. Its
        process outlives a swap of the solet that started it, so the verb
        never waits on it: a start answers ``deferred`` / ``in_progress``
        with the attempt id, and a later call with that id answers the
        rule's terminal decision. ``call_context`` is server-built and is
        used only to name the caller in the child-start log line.

        Returns:
            Result dict with ``outcome``, ``decision``, ``attempt_id``,
            ``target``, ``child_pid``, ``receipt`` and ``detail``. Another
            solet's name is refused with ``solet_mismatch``, an unknown
            trigger with ``invalid_trigger``, a malformed attempt id with
            ``invalid_argument``, an attempt that is not kept with
            ``unknown_attempt``, and a profile with no checkout above it with
            ``deploy_rule_unconfigured``.
        """
        ...

    @service_interface_process(
        name="open_work_folder",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "path": ParameterMetadata(
                description="Absolute path of an existing real directory to register. A symlink resolves to its target, which is what is recorded; the filesystem root is refused.",
                required=True,
                type=ParameterType.STRING,
            ),
            "purpose": ParameterMetadata(
                description="What the folder is for, in the creator's words.",
                required=True,
                type=ParameterType.STRING,
            ),
            "bound": ParameterMetadata(
                description="The work item the folder belongs to, as 'kind:ref' split at the first colon, for example 'release_run:run-12'.",
                required=True,
                type=ParameterType.STRING,
            ),
            "harvest": ParameterMetadata(
                description="What must be kept before the folder may be removed: 'regenerable' (default, nothing to keep), 'evidence_copy' or 'custody_packet' (a harvest receipt is needed first).",
                required=False,
                type=ParameterType.STRING,
                validation={"enum": ["regenerable", "evidence_copy", "custody_packet"]},
            ),
            "owner": ParameterMetadata(
                description="Session or role that owns the folder. Optional; recorded for history, not an authority.",
                required=False,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description="Agent instance making the change. Optional; recorded for history, not an authority.",
                required=False,
                type=ParameterType.STRING,
            ),
            "agent_session_id": ParameterMetadata(
                description="Agent session making the change. Optional; recorded for history, not an authority.",
                required=False,
                type=ParameterType.STRING,
            ),
            "actor_model": ParameterMetadata(
                description="Model of the acting actor. Optional; recorded for history.",
                required=False,
                type=ParameterType.STRING,
            ),
            "actor_effort": ParameterMetadata(
                description="Effort tier of the acting actor. Optional; recorded for history.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="The work-folder record after the call",
            type=ParameterType.OBJECT,
            properties=_work_folder_row_properties(),
            usage_patterns=[
                "Register a scratch, capture or cache directory right after creating it",
                "Repeat the call for a path that may already be registered: the same row comes back and nothing is written",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def open_work_folder(
        self,
        path: str,
        purpose: str,
        bound: str,
        harvest: str = "regenerable",
        owner: str = "",
        agent_instance_id: str = "",
        agent_session_id: str = "",
        actor_model: str = "",
        actor_effort: str = "",
    ) -> dict[str, Any]:
        """Record an existing directory as a work folder in the solet's own database; idempotent for a live path.

        Returns:
            The row, with the keys in ``return_value_schema``. A path whose
            record is not deleted returns that same row unchanged and writes
            nothing; if the directory there is no longer the one recorded, the
            row carries one ``identity_changed`` advisory. A relative or missing
            path is refused with the error code ``invalid_path``, a bound that is
            not ``kind:ref`` with ``invalid_argument``, an unknown harvest kind
            with ``invalid_field``.
        """
        ...

    @service_interface_process(
        name="read_work_folders",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "mode": ParameterMetadata(
                description="'show' (one folder by folder_id), 'list' (folders, filtered, ordered by path, paged) or 'history' (the events of one folder, oldest first, paged).",
                required=True,
                type=ParameterType.STRING,
                validation={"enum": ["show", "list", "history"]},
            ),
            "folder_id": ParameterMetadata(
                description="Record id to read. Required for modes 'show' and 'history', ignored for 'list'.",
                required=False,
                type=ParameterType.STRING,
            ),
            "parent_dir": ParameterMetadata(
                description="Mode 'list' only: keep folders whose parent directory is exactly this canonical path.",
                required=False,
                type=ParameterType.STRING,
            ),
            "bound": ParameterMetadata(
                description="Mode 'list' only: keep folders bound to this work item, as 'kind:ref'.",
                required=False,
                type=ParameterType.STRING,
            ),
            # Not named "state": the platform replaces the value of any declared parameter by that name with its own.
            "state_filter": ParameterMetadata(
                description="Mode 'list' only: keep folders in this state.",
                required=False,
                type=ParameterType.STRING,
                validation={"enum": ["open", "due", "harvested", "deleted", "retired"]},
            ),
            "limit": ParameterMetadata(
                description="Page size for modes 'list' and 'history'. Default 50, maximum 100; a larger value is refused, not clamped.",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "after": ParameterMetadata(
                description="The 'next' value of the previous page, to continue it.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Work-folder rows or event rows, and the continuation token",
            type=ParameterType.OBJECT,
            properties={
                "folders": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=("Work-folder rows for modes 'show' and 'list', each with the row keys. For mode 'history', the event rows oldest first, each with folder_id, ordinal, kind, prior_version, actor_instance, actor_session, actor_model, actor_effort, detail (JSON text) and recorded_at."),
                    required=True,
                ),
                "next": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Token to pass as 'after' for the next page; null when there is no further page and always null for mode 'show'.",
                    required=False,
                ),
            },
            usage_patterns=[
                "List the work folders under one directory, or bound to one work item, or in one state",
                "Re-read one folder to get its current version before changing it (mode show)",
                "See what happened to a folder and who did it (mode history)",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def read_work_folders(
        self,
        mode: str,
        folder_id: str | None = None,
        parent_dir: str | None = None,
        bound: str | None = None,
        state_filter: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Read the solet's work-folder records.

        Returns:
            Result dict with ``folders`` and ``next``. An unknown id in mode
            ``show`` or ``history`` is refused with the error code
            ``unknown_folder``; a bad mode, a missing id, a bound that is not
            ``kind:ref`` or a malformed ``after`` with ``invalid_argument``; a
            limit outside 1..100 or an unknown ``state_filter`` with
            ``invalid_field``.
        """
        ...

    @service_interface_process(
        name="transition_work_folder",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "folder_id": ParameterMetadata(
                description="Id of the work-folder record to change.",
                required=True,
                type=ParameterType.STRING,
            ),
            "action": ParameterMetadata(
                description=(
                    "'close' (open to due: the creator's work is over), 'harvest_record' (due to harvested: records what was kept), 'reopen' (due or harvested to open; clears the current receipt), "
                    "'retire' (open, due or harvested to retired: the sweep keeps it), 'restore' (retired to open) or 'correct' (changes harvest_kind and/or owner)."
                ),
                required=True,
                type=ParameterType.STRING,
                validation={"enum": ["close", "reopen", "retire", "restore", "correct", "harvest_record"]},
            ),
            "expected_version": ParameterMetadata(
                description="The row version the caller last read; a different current version refuses the write.",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "reason": ParameterMetadata(
                description="Why the change is made; kept in the folder's history.",
                required=False,
                type=ParameterType.STRING,
            ),
            "harvest_kind": ParameterMetadata(
                description="Action 'correct' only: the new harvest kind. Ignored for other actions.",
                required=False,
                type=ParameterType.STRING,
                validation={"enum": ["regenerable", "evidence_copy", "custody_packet"]},
            ),
            "owner": ParameterMetadata(
                description="Action 'correct' only: the new owner; an empty string clears it. Ignored for other actions.",
                required=False,
                type=ParameterType.STRING,
            ),
            "receipt_json": ParameterMetadata(
                description="Action 'harvest_record' only: the harvest receipt as the text of a non-empty JSON object. Needed unless the folder is regenerable. Ignored for other actions.",
                required=False,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description="Agent instance making the change. Optional; recorded for history, not an authority.",
                required=False,
                type=ParameterType.STRING,
            ),
            "agent_session_id": ParameterMetadata(
                description="Agent session making the change. Optional; recorded for history, not an authority.",
                required=False,
                type=ParameterType.STRING,
            ),
            "actor_model": ParameterMetadata(
                description="Model of the acting actor. Optional; recorded for history.",
                required=False,
                type=ParameterType.STRING,
            ),
            "actor_effort": ParameterMetadata(
                description="Effort tier of the acting actor. Optional; recorded for history.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="The work-folder record after the change",
            type=ParameterType.OBJECT,
            properties=_work_folder_row_properties(),
            usage_patterns=[
                "Close a folder when its creator's work ends, then record what was kept so the sweep may remove it",
                "Reopen a folder that gets more work, or retire one the sweep must keep and restore it later",
                "Correct the harvest kind or owner of a folder that was registered wrongly",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def transition_work_folder(
        self,
        folder_id: str,
        action: str,
        expected_version: int,
        reason: str = "",
        harvest_kind: str | None = None,
        owner: str | None = None,
        receipt_json: str | None = None,
        agent_instance_id: str = "",
        agent_session_id: str = "",
        actor_model: str = "",
        actor_effort: str = "",
    ) -> dict[str, Any]:
        """Move one work folder through its life, or correct its record, by compare-and-set on its version.

        Returns:
            The row after the change, with the version raised by one. A version
            that moved since the caller read it is refused with the error code
            ``stale_version`` and nothing is written; an unknown id with
            ``unknown_folder``; a move the folder's state does not allow, or any
            move of a deleted folder, with ``invalid_transition``; a bad harvest
            kind or receipt, or a correction that changes nothing, with
            ``invalid_field``; an unknown action with ``invalid_argument``.
        """
        ...

    @service_interface_process(
        name="sweep_work_folders",
        is_discoverable=True,
        provider="lifecycle_management_service",
        parameters={
            "apply": ParameterMetadata(
                description="True removes the finished folders; False (the default) lists what would be done and removes nothing.",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
            "limit": ParameterMetadata(
                description="Most folders acted on in one run, at least 1. Default 200; the rest are listed in 'deferred' and the next run takes them.",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "folder_id": ParameterMetadata(
                description="Limit the run to the one record with this id (wkf-<hex>). Omitted, every finished folder is looked at. An id with no record is refused unknown_folder; the id of a folder that is not finished gives no candidates.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="What one sweep run found and did",
            type=ParameterType.OBJECT,
            properties={
                "candidates": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=("One entry per folder acted on, or in a dry run that would be: folder_id, path, state, action (remove, record_absent, harvest_and_remove or harvest_and_record_absent) and outcome (would_ plus the action in a dry run; deleted, absent, skipped or failed after an apply)."),
                    required=True,
                ),
                "counts": ParameterMetadata(
                    type=ParameterType.DICT,
                    description="Counts for the run: marked_harvested, deleted, absent, skipped and failed.",
                    required=True,
                ),
                "deferred": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Folder ids that were eligible but beyond the limit; the next run takes them.",
                    required=True,
                ),
                "advisories": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="What the sweep kept or could not do, each with code and message and, for one folder, folder_id and path: awaiting_harvest_receipt, no_custody, identity_changed, not_a_real_directory, inspect_failed, removal_failed or row_moved.",
                    required=True,
                ),
                "aborted": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True when an integrity check stopped an applying run before anything was removed: this Python's rmtree cannot avoid symlink attacks. The reason is in advisories.",
                    required=True,
                ),
            },
            usage_patterns=[
                "Preview what the sweep would remove (the default dry run)",
                "Remove the work folders that are finished and harvested, on a schedule",
            ],
        ),
        is_enabled=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
    )
    @abstractmethod
    def sweep_work_folders(self, apply: bool = False, limit: int = 200, folder_id: str | None = None) -> dict[str, Any]:
        """Sweep the work folders that are finished, or only the one named by ``folder_id``; a dry run (the default) lists and removes nothing.

        Returns:
            Result dict with ``candidates``, ``counts``, ``deferred``,
            ``advisories`` and ``aborted``. Only folders whose record says they
            are finished and harvested are removed; the sweep never looks for
            folders on disk and never decides by age. A limit below 1 is refused
            with the error code ``invalid_field``; a ``folder_id`` with no
            record is refused with the error code ``unknown_folder``.
        """
        ...
