"""Default Address Book Plugin - Address registry with memory integration.

Features:
- One-to-many: address -> address_entry
- Auto-ingests all addresses to memory_service
- Memory strengthening on resolve
"""

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, ClassVar, cast

from ananta.core.actions.action_metadata import (
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_base import PluginBase
from ananta.file_locations.addresses import path_addresses
from ananta.file_locations.documents import default_solet_home, documents_folder
from ananta.file_locations.resolution import LocationRequest, Purpose, SoletTree, resolve_location, solet_tree_from_app_home
from ananta.interfaces.address_book_service_interface import AddressBookServiceInterface, AddressMetadataResult
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.logging_setup import configure_plugin_logging

from .address_ops import (
    add_entry_impl,
    delete_entry_impl,
    delete_impl,
    list_tags_impl,
    list_types_impl,
    project_address_metadata,
    register_impl,
    resolve_impl,
    resolve_with_secrets_impl,
    search_impl,
    update_entry_impl,
    update_impl,
)
from .address_queries import list_path_address_rows
from .async_jobs import JOB_RETURN_SCHEMA, AsyncJobs
from .constants import PLUGIN_NAME
from .result_helpers import success
from .seed_loader import auto_seed_entries_from_file


class DefaultAddressBookPlugin(PluginBase, AddressBookServiceInterface):
    """Address registry with automatic memory integration."""

    service_interfaces: ClassVar[tuple[type, ...]] = (AddressBookServiceInterface,)
    supported_interface_versions: ClassVar[dict[type, str]] = {AddressBookServiceInterface: AddressBookServiceInterface.INTERFACE_VERSION}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self.config = config or {}
        self.logger: logging.Logger = logging.getLogger(self.name)

        self.config_provider: ConfigProvider | None = None
        self._schema_created = False

        # Injected dependencies
        self.state_service: StateServiceProtocol | None = None
        self._memory_service: Any = None
        self._vault_service: object | None = None
        self._jobs = AsyncJobs()

    def set_vault_service(self, vault_service: object) -> None:
        """Receive caller-bound VaultServiceProxy from lifecycle injection.

        W-VAULT-INTERFACE-EXTEND Phase D-2 (P0 Tier 1, 2026-06-07): the
        proxy was constructed by ``_inject_vault_service`` in
        ``startup_sequence.py`` with this plugin's name baked into its
        bound ``CallContext``. Do NOT acquire vault via
        ``orchestrator.get_service`` — the proxy is the only allowed
        handle.
        """
        self._vault_service = vault_service

    # ------------------------------------------------------------------
    # VaultKeysProvider — W-PLUGIN-LAUNCH-KEYS (P0 Tier 2 sub-1, 2026-06-07)
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """Address book is a chain consumer, not a vault-key owner.

        Address-book entries reference vault keys via the
        ``vault::<scoped-key>`` field; the resolver's
        ``vault_service.retrieve(key)`` call at
        ``address_ops.py:232`` reads whatever key the operator
        authored into the entry, not a fixed-name credential this
        plugin owns. The W-INT Cycle 2 static gate accepts that call
        site via the allowlist (brief §5.2) rather than via a
        declared key list. Returns empty list per brief §3.7.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """Address book owns no vault keys directly — chain consumer."""
        return []

    def get_schema_definitions(self) -> list[Any]:
        """Return schema definitions for the address book tables."""
        from .schema import get_address_book_schema

        return [get_address_book_schema()]

    def prepare_for_readiness(self) -> None:
        """Initialize plugin. Fail-fast if dependencies unavailable."""
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")

        APP_HOME = getattr(self.orchestrator_ref, "APP_HOME", None)
        if not APP_HOME:
            raise RuntimeError(f"{self.name}: Application directory not configured - plugin cannot initialize")

        self.config_provider = ConfigProvider(self.name, self.config)
        self.logger = configure_plugin_logging(APP_HOME, self.name, self.config_provider)
        self.logger.debug(f"Initializing {self.name}")

        self.state_service = cast(StateServiceProtocol | None, self.orchestrator_ref.get_service("state_service"))
        if self.state_service is None:
            raise RuntimeError(f"{self.name}: state_service not available")
        self.logger.debug("state_service acquired from orchestrator")

        self._memory_service = self.orchestrator_ref.get_service("memory_service")
        if self._memory_service is None:
            self.logger.debug("memory_service not available - auto-ingest disabled")
        else:
            self.logger.debug("memory_service acquired from orchestrator")

        if self._vault_service is None:
            self.logger.debug("vault_service not available - vault references not resolved")
        else:
            self.logger.debug("vault_service acquired via setter injection")

        auto_seed_entries_from_file(
            Path(APP_HOME),
            self.name,
            self.logger,
            self.state_service,
            self._memory_service,
            self._auto_ingest_enabled(),
        )

    def _ensure_schema(self) -> None:
        """Schema is created during startup via get_schema_definitions()."""
        if self._schema_created:
            return
        self._schema_created = True

    def _location_tree(self) -> SoletTree | None:
        if self.orchestrator_ref is None:
            return None
        return solet_tree_from_app_home(Path(self.orchestrator_ref.APP_HOME))

    def _launch_directory_capture(self, state: dict[str, Any]) -> dict[str, str] | None:
        """Read ephemeral location context through the loaded messaging plugin.

        Messaging is an EDGE plugin, not a bound service. The action pipeline
        supplies session_id; no location parameter or persisted field is used.
        Main-seat R2 (2026-11-08) reverses the 2026-07-15 no-session-cwd ruling.
        """
        session_id = state.get("session_id")
        manager = getattr(self.orchestrator_ref, "plugin_manager", None)
        if not isinstance(session_id, str) or not session_id or manager is None:
            return None
        messaging = manager.get_plugin("agent_messaging_plugin")
        surface = getattr(messaging, "_platform_surface", None)
        if surface is None:
            return None
        return cast(dict[str, str] | None, surface.launch_directory_for_session(session_id))

    def get_config_schema(self) -> dict[str, object]:
        """Declare configuration schema for the address book plugin."""
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "Default Address Book Plugin",
            "description": "Address registry with automatic memory integration and vault support",
            "type": "object",
            "properties": {
                "auto_ingest_to_memory": {
                    "type": "boolean",
                    "default": True,
                    "description": "Auto-ingest address entries to memory service",
                },
                "strengthen_on_resolve": {
                    "type": "boolean",
                    "default": True,
                    "description": "Strengthen memory on address resolve",
                },
                "log_level": {
                    "type": "string",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR"],
                    "default": "INFO",
                },
            },
        }

    def _get_config(self) -> dict[str, Any]:
        if self.config_provider:
            config = self.config_provider.config
            if isinstance(config, dict):  # type: ignore[reportUnnecessaryIsInstance]
                return cast(dict[str, Any], config)
        return self.config or {}

    def _auto_ingest_enabled(self) -> bool:
        config = self._get_config()
        if isinstance(config, dict):  # type: ignore[reportUnnecessaryIsInstance]
            return bool(config.get("auto_ingest_to_memory", True))
        return True

    def _strengthen_on_resolve_enabled(self) -> bool:
        config = self._get_config()
        if isinstance(config, dict):  # type: ignore[reportUnnecessaryIsInstance]
            return bool(config.get("strengthen_on_resolve", True))
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Interface Methods (direct calls)
    # ─────────────────────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        address_type: str,
        description: str,
        entries: list[dict[str, str]],
        tags: list[str] | None = None,
    ) -> ActionResult:
        assert self.state_service is not None
        return register_impl(
            self.state_service,
            self.name,
            self._memory_service,
            self._auto_ingest_enabled(),
            self.logger,
            name,
            address_type,
            description,
            entries,
            tags or [],
            tree=self._location_tree(),
        )

    def resolve(self, name: str) -> ActionResult:
        assert self.state_service is not None
        return resolve_impl(
            self.state_service,
            self.name,
            self._memory_service,
            self._strengthen_on_resolve_enabled(),
            name,
        )

    def add_entry(
        self,
        name: str,
        field_type: str,
        description: str,
        value: str,
    ) -> ActionResult:
        assert self.state_service is not None
        return add_entry_impl(
            self.state_service,
            self.name,
            self.logger,
            name,
            field_type,
            description,
            value,
            tree=self._location_tree(),
        )

    def update_entry(
        self,
        entry_id: str,
        field_type: str | None = None,
        description: str | None = None,
        value: str | None = None,
    ) -> ActionResult:
        assert self.state_service is not None
        return update_entry_impl(
            self.state_service,
            self.name,
            entry_id,
            field_type,
            description,
            value,
            tree=self._location_tree(),
        )

    def delete_entry(self, entry_id: str) -> ActionResult:
        assert self.state_service is not None
        return delete_entry_impl(self.state_service, self.name, self.logger, entry_id)

    def update(
        self,
        name: str,
        address_type: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> ActionResult:
        assert self.state_service is not None
        return update_impl(
            self.state_service,
            self.name,
            self.logger,
            name,
            address_type,
            description,
            tags,
            tree=self._location_tree(),
        )

    def delete(self, name: str) -> ActionResult:
        assert self.state_service is not None
        return delete_impl(
            self.state_service,
            self.name,
            self._memory_service,
            self.logger,
            name,
        )

    def search(
        self,
        query: str | None = None,
        address_type: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> ActionResult:
        assert self.state_service is not None
        return search_impl(self.state_service, self.name, query, address_type, tag, limit)

    def list_path_addresses(self) -> ActionResult:
        """Read every live location, including entries, for internal consumers."""
        assert self.state_service is not None
        try:
            addresses = list_path_address_rows(self.state_service, self.name)
        except RuntimeError as exc:
            from .result_helpers import error

            return error("address_book.path_read_unavailable", str(exc))
        return success({"addresses": addresses, "count": len(addresses)})

    def list_types(self) -> ActionResult:
        assert self.state_service is not None
        return list_types_impl(self.state_service, self.name)

    def list_tags(self) -> ActionResult:
        assert self.state_service is not None
        return list_tags_impl(self.state_service, self.name)

    def metadata(self, name: str) -> AddressMetadataResult:
        """Read public fields and references without resolving credentials."""
        try:
            return project_address_metadata(name, self.resolve(name))
        except Exception:
            return AddressMetadataResult("provider_error")

    def resolve_with_secrets(self, name: str) -> ActionResult:
        assert self.state_service is not None
        return resolve_with_secrets_impl(
            self.state_service,
            self.name,
            self._memory_service,
            self._strengthen_on_resolve_enabled(),
            self._vault_service,
            self.logger,
            name,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Plugin Processes (exposed via @platform_process)
    # ─────────────────────────────────────────────────────────────────────────

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/register.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="register",
        parameters={
            "name": ParameterMetadata(
                description="Unique lookup key (e.g., 'openai_api')",
                required=True,
                type=ParameterType.STRING,
            ),
            "address_type": ParameterMetadata(
                description="Type classification (url, endpoint, path, service, etc.)",
                required=True,
                type=ParameterType.STRING,
            ),
            "description": ParameterMetadata(
                description="Human-readable description (used for memory/search)",
                required=True,
                type=ParameterType.STRING,
            ),
            "entries": ParameterMetadata(
                description="List of entry dicts with field_type, description, value",
                required=True,
                type=ParameterType.LIST,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        return_value_schema=JOB_RETURN_SCHEMA,
        is_async=True,
        is_long_running=True,
        summary="Register address with entries",
    )
    def register_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Queue registration and its embedding work; register stays a library API."""
        for name in ("name", "address_type", "description", "entries"):
            if name not in params:
                raise KeyError(name)
        return self._jobs.submit(self, "register", params, state, self._register_now)

    def _register_now(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run the original registration on this plugin's serial job worker."""
        self._ensure_schema()
        assert self.state_service is not None
        return cast(
            dict[str, Any],
            register_impl(
                self.state_service,
                self.name,
                self._memory_service,
                self._auto_ingest_enabled(),
                self.logger,
                params["name"],
                params["address_type"],
                params["description"],
                params["entries"],
                params.get("tags", []),
                tree=self._location_tree(),
            ),
        )

    def admit_registered_operation(
        self,
        action_name: str,
        parameters: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Queue the complete registered location operation on our owner worker.

        Server-derived state is captured separately from request parameters;
        direct calls retain the complete synchronous location result.
        """
        if action_name != "resolve_location":
            return None
        captured_state = dict(state)
        return self._jobs.submit(
            self,
            action_name,
            parameters,
            captured_state,
            lambda request: self.resolve_location_action(request, captured_state),
        )

    @platform_process(
        name="resolve_location",
        parameters={
            "path": ParameterMetadata(type=ParameterType.STRING, required=False, description="Explicit directory"),
            "project": ParameterMetadata(type=ParameterType.STRING, required=False, description="Project tag value"),
            "file_kind": ParameterMetadata(type=ParameterType.STRING, required=False, description="File-kind tag value"),
            "purpose": ParameterMetadata(type=ParameterType.STRING, required=False, default="file", description="file or export"),
        },
        return_value_schema=JOB_RETURN_SCHEMA,
        is_async=True,
        is_long_running=True,
        summary="Resolve a file or export directory",
    )
    def resolve_location_action(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Resolve address inputs and probe writing in the solet's own process.

        The calling bridge supplies the launch directory, never a request
        argument or this server's cwd. Missing/closed bridges advise and use home.
        """
        tree = self._location_tree()
        assert tree is not None, "orchestrator APP_HOME must be injected before serving actions"
        documents = documents_folder(sys.platform, os.environ, Path.home())
        solet_name = os.environ.get("SOLET_NAME", tree.repo_root.name)
        home = default_solet_home(solet_name, documents, tree, Path.home() / "Library/Application Support" / solet_name / "Files")
        purpose = params.get("purpose", "file")
        advisories: list[str] = list(home.advisories)
        if purpose not in ("file", "export"):
            advisories.append(f"Unknown purpose {purpose!r}; resolving as a file.")
            purpose = "file"
        capture = self._launch_directory_capture(state)
        launch_directory = capture.get("path") if capture is not None else None
        if capture is not None and capture.get("source") == "cwd_fallback":
            advisories.append("Launch directory uses the bridge client's cwd fallback. Refresh the launcher in hydration step 2, then launch from your project folder.")
        request = LocationRequest(params.get("path"), params.get("project"), params.get("file_kind"), Purpose(purpose), launch_directory)
        addresses = path_addresses(self)
        advisories.extend(addresses.advisories)
        result = resolve_location(request, addresses.value, tree, home.value)
        advisories.extend(result.advisories)
        if not launch_directory and result.rung.value == "solet_home":
            advisories.append("No launch directory was recorded. Refresh the launcher in hydration step 2, then launch from your project folder; using solet_home.")
        if not documents.proven:
            advisories.append(f"Documents folder is unproven on {sys.platform}; using {documents.path}.")
        writable = False
        try:
            result.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=result.directory) as probe:
                probe.write(b"solet location write check")
                probe.flush()
            writable = True
        except OSError as exc:
            advisories.append(f"Cannot write to {result.directory}: {exc}. Choose another location or grant access.")
        return cast(
            dict[str, Any],
            success(
                {
                    "directory": str(result.directory),
                    "rung": result.rung.value,
                    "address_name": result.address_name,
                    "advisories": advisories,
                    "writable": writable,
                }
            ),
        )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/resolve.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="resolve",
        parameters={
            "name": ParameterMetadata(
                description="The registered name",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Full address with entries",
        ),
        summary="Resolve address by name",
    )
    def resolve_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve address - plugin action."""
        del state
        self._ensure_schema()
        assert self.state_service is not None
        return cast(
            dict[str, Any],
            resolve_impl(
                self.state_service,
                self.name,
                self._memory_service,
                self._strengthen_on_resolve_enabled(),
                params["name"],
            ),
        )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/add_entry.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="add_entry",
        parameters={
            "name": ParameterMetadata(
                description="The address name",
                required=True,
                type=ParameterType.STRING,
            ),
            "field_type": ParameterMetadata(
                description="Type of entry (url, note, port, host, etc.)",
                required=True,
                type=ParameterType.STRING,
            ),
            "description": ParameterMetadata(
                description="Human-readable description",
                required=True,
                type=ParameterType.STRING,
            ),
            "value": ParameterMetadata(
                description="The entry value",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Entry creation result",
            properties={
                "entry_id": ParameterMetadata(type=ParameterType.STRING, description="Entry ID"),
            },
        ),
        summary="Add entry to address",
    )
    def add_entry_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Add entry - plugin action."""
        del state
        self._ensure_schema()
        assert self.state_service is not None
        return cast(
            dict[str, Any],
            add_entry_impl(
                self.state_service,
                self.name,
                self.logger,
                params["name"],
                params["field_type"],
                params["description"],
                params["value"],
                tree=self._location_tree(),
            ),
        )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/update_entry.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="update_entry",
        parameters={
            "entry_id": ParameterMetadata(
                description="The entry ID",
                required=True,
                type=ParameterType.STRING,
            ),
            "field_type": ParameterMetadata(
                description="New field type",
                required=False,
                type=ParameterType.STRING,
            ),
            "description": ParameterMetadata(
                description="New description",
                required=False,
                type=ParameterType.STRING,
            ),
            "value": ParameterMetadata(
                description="New value",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Updated entry",
        ),
        summary="Update entry by ID",
    )
    def update_entry_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Update entry - plugin action."""
        del state
        self._ensure_schema()
        assert self.state_service is not None
        return cast(
            dict[str, Any],
            update_entry_impl(
                self.state_service,
                self.name,
                params["entry_id"],
                params.get("field_type"),
                params.get("description"),
                params.get("value"),
                tree=self._location_tree(),
            ),
        )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/delete_entry.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="delete_entry",
        parameters={
            "entry_id": ParameterMetadata(
                description="The entry ID",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Delete confirmation",
        ),
        summary="Delete entry by ID",
    )
    def delete_entry_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Delete entry - plugin action."""
        del state
        self._ensure_schema()
        assert self.state_service is not None
        return cast(
            dict[str, Any],
            delete_entry_impl(self.state_service, self.name, self.logger, params["entry_id"]),
        )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/update.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="update",
        parameters={
            "name": ParameterMetadata(
                description="The registered name (cannot be changed)",
                required=True,
                type=ParameterType.STRING,
            ),
            "address_type": ParameterMetadata(
                description="New address type",
                required=False,
                type=ParameterType.STRING,
            ),
            "description": ParameterMetadata(
                description="New description",
                required=False,
                type=ParameterType.STRING,
            ),
            "tags": ParameterMetadata(
                description="New tags (replaces existing)",
                required=False,
                type=ParameterType.LIST,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Updated address",
        ),
        summary="Update address metadata",
    )
    def update_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Update address - plugin action."""
        del state
        self._ensure_schema()
        assert self.state_service is not None
        return cast(
            dict[str, Any],
            update_impl(
                self.state_service,
                self.name,
                self.logger,
                params["name"],
                params.get("address_type"),
                params.get("description"),
                params.get("tags"),
                tree=self._location_tree(),
            ),
        )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/delete.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="delete",
        parameters={
            "name": ParameterMetadata(
                description="The registered name",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Delete confirmation",
        ),
        summary="Delete address",
    )
    def delete_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Delete address - plugin action."""
        del state
        self._ensure_schema()
        assert self.state_service is not None
        return cast(
            dict[str, Any],
            delete_impl(
                self.state_service,
                self.name,
                self._memory_service,
                self.logger,
                params["name"],
            ),
        )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/search.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="search",
        parameters={
            "query": ParameterMetadata(
                description="Optional text search in name/description",
                required=False,
                type=ParameterType.STRING,
            ),
            "address_type": ParameterMetadata(
                description="Filter by type",
                required=False,
                type=ParameterType.STRING,
            ),
            "tag": ParameterMetadata(
                description="Filter by tag",
                required=False,
                type=ParameterType.STRING,
            ),
            "limit": ParameterMetadata(
                description="Maximum results",
                required=False,
                type=ParameterType.INTEGER,
                default=20,
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Search results",
            properties={
                "addresses": ParameterMetadata(type=ParameterType.LIST, description="Matching addresses"),
                "count": ParameterMetadata(type=ParameterType.INTEGER, description="Number of results"),
            },
        ),
        summary="Search addresses",
    )
    def search_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Search addresses - plugin action."""
        del state
        self._ensure_schema()
        assert self.state_service is not None
        return cast(
            dict[str, Any],
            search_impl(
                self.state_service,
                self.name,
                params.get("query"),
                params.get("address_type"),
                params.get("tag"),
                params.get("limit", 20),
            ),
        )

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/list_types.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="list_types",
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Types with counts",
        ),
        summary="List address types",
    )
    def list_types_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """List types - plugin action."""
        del state, params
        self._ensure_schema()
        assert self.state_service is not None
        return cast(dict[str, Any], list_types_impl(self.state_service, self.name))

    # Text fields (display_name, description, embedding_description) are defined in
    # knowledge_base/processes/list_tags.json — the builder merges them at startup,
    # overwriting any values set here in the decorator.
    @platform_process(
        name="list_tags",
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Tags with counts",
        ),
        summary="List address tags",
    )
    def list_tags_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """List tags - plugin action."""
        del state, params
        self._ensure_schema()
        assert self.state_service is not None
        return cast(dict[str, Any], list_tags_impl(self.state_service, self.name))
