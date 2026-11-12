"""External Postgres plugin entry point — the "super Datagrip" over foreign DBs.

Query, write, script, bulk-load, dump, restore and administer FOREIGN Postgres
databases the operator registers as ``external_pg::<name>`` address-book
entries. The registered credential's own server-side Postgres GRANTs are the
only boundary (USER ruling rul_4c99a30d, 2026-11-06; operator ruling 2026-08-09
+ Amendment 1): no verb restricts its connection, refuses a statement by its
leading keyword, or asks for confirmation. Every verb takes a connection
NAME (never a DSN); the platform's own DB instance is refused
role-independently, for every verb (connection.assert_foreign_target).

Verbs (all EDGE; knowledge_base/vendor_api_coverage.md maps them to the vendor):
  - list_schemas / list_tables / describe_table / test_connection / run_query /
    export_query / run_statement — the connector verbs.
  - run_script (transactions and scripts), copy (COPY files), dump_database /
    restore_database (pg_dump, pg_restore), activity (sessions, locks,
    cancel, terminate) and statistics (sizes, counters, resets) — build 1 of
    frq_37c141636f684f60a2c72e52885d039b.
  - list_connections — the registered external_pg::* connection names (still
    synchronous — a single address-book scan, not a foreign-DB round trip).

Every verb except list_connections is on the D0.3 deferred-completion shape
(workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md):
the dispatch handler returns ``{"job_id", "status": "queued"}`` in
milliseconds; ``async_jobs.py``'s single background worker thread does the
real I/O and completes the job. Row results are written to caller-supplied,
contained workspace files, never returned inline (query_actions.py).

No plugin-owned vault keys (the per-connection password is chain-consumed through
the address book's ``resolve_with_secrets``), so this plugin needs NO vault
binding — only address_book_service (the connection registry).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import OrchestratorProtocol, PluginBase
from ananta.file_locations.addresses import path_addresses
from ananta.file_locations.containment import Located, PathRefusedError, assert_path_allowed, derive_allowed_roots, location_advisories, location_session_id, retain_location_advisories
from ananta.file_locations.documents import default_solet_home, documents_folder
from ananta.file_locations.resolution import SoletTree, solet_tree_from_app_home
from ananta.interfaces.address_book_service_interface import AddressBookServiceInterface
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from . import admin_actions, async_jobs, client_tools, connection, copy_actions
from .app_config import AppConfigLoader, ExternalDsn, ExternalPgConfigError
from .client_tools import ClientToolError, ToolSettings
from .connection import ExternalPgGuardError
from .constants import (
    CLIENT_TOOL_TIMEOUT_SECONDS_DEFAULT,
    CONFIG_KEY_CLIENT_BIN_DIR,
    CONFIG_KEY_CLIENT_TOOL_TIMEOUT_SECONDS,
    CONFIG_KEY_PLATFORM_PG_PORT,
    CONFIG_KEY_STATEMENT_TIMEOUT_MS,
    DEFAULT_ROW_LIMIT,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_EXPORT_PATH_REFUSED,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    EXPORT_ROW_CAP,
    LIST_LIMIT_DEFAULT,
    LIST_LIMIT_MAX,
    MAX_ROWS_HARD_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    PLATFORM_PG_PORT_DEFAULT,
    PLUGIN_NAME,
    RESULT_TYPE_ACTIVITY,
    RESULT_TYPE_COPY,
    RESULT_TYPE_DESCRIBE_TABLE,
    RESULT_TYPE_DUMP_DATABASE,
    RESULT_TYPE_EXPORT_QUERY,
    RESULT_TYPE_LIST_CONNECTIONS,
    RESULT_TYPE_LIST_SCHEMAS,
    RESULT_TYPE_LIST_TABLES,
    RESULT_TYPE_RESTORE_DATABASE,
    RESULT_TYPE_RUN_QUERY,
    RESULT_TYPE_RUN_SCRIPT,
    RESULT_TYPE_RUN_STATEMENT,
    RESULT_TYPE_STATISTICS,
    RESULT_TYPE_TEST_CONNECTION,
    STATEMENT_TIMEOUT_MS_DEFAULT,
)
from .script_actions import ScriptStatementError
from .statement_guard import StatementGuardError

_QUEUED_RETURN = ReturnValueSchema(
    type=ParameterType.OBJECT,
    description="Dispatch envelope — job_id + status: queued. The result arrives when the job completes.",
    properties={
        "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
        "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
    },
)
_CONNECTION_PARAMETER = ParameterMetadata(
    type=ParameterType.STRING,
    required=True,
    description="The registered connection name (from list_connections).",
)
_OPTION_TYPES = {
    "bool": ParameterType.BOOLEAN,
    "str": ParameterType.STRING,
    "int": ParameterType.INTEGER,
    "list": ParameterType.LIST,
    "path": ParameterType.STRING,
}
_COPY_OPTION_TEXT = {
    "format": "COPY FORMAT: text, csv or binary.",
    "freeze": "COPY FREEZE (from_file): load rows already frozen.",
    "delimiter": "COPY DELIMITER: the column separator character.",
    "null": "COPY NULL: the string that represents a null value.",
    "default": "COPY DEFAULT (from_file): the string that means a column's default.",
    "header": "COPY HEADER: true, false, or match (from_file checks the header names).",
    "quote": "COPY QUOTE (csv): the quoting character.",
    "escape": "COPY ESCAPE (csv): the escape character.",
    "force_quote": 'COPY FORCE_QUOTE (to_file, csv): column names to always quote, or ["*"].',
    "force_not_null": 'COPY FORCE_NOT_NULL (from_file, csv): column names, or ["*"].',
    "force_null": 'COPY FORCE_NULL (from_file, csv): column names, or ["*"].',
    "on_error": "COPY ON_ERROR (from_file): stop or ignore rows that fail type conversion.",
    "encoding": "COPY ENCODING: the file's encoding name.",
    "log_verbosity": "COPY LOG_VERBOSITY: default, verbose or silent.",
}


def _option_parameters(spec: dict[str, str], program: str) -> dict[str, ParameterMetadata]:
    """One optional parameter per documented client-program option (long flag, snake_case)."""
    return {
        name: ParameterMetadata(
            type=_OPTION_TYPES[kind],
            required=False,
            description=(f"{program} --{name.replace('_', '-')}" + (" (one flag per list item)" if kind == "list" else "") + (" (a contained workspace file path)" if kind == "path" else "") + "."),
        )
        for name, kind in spec.items()
    }


def _paging_parameters() -> dict[str, ParameterMetadata]:
    return {
        "limit": ParameterMetadata(
            type=ParameterType.INTEGER,
            required=False,
            description=(f"List operations: rows per page, 1 to {LIST_LIMIT_MAX} (default {LIST_LIMIT_DEFAULT})."),
        ),
        "offset": ParameterMetadata(
            type=ParameterType.INTEGER,
            required=False,
            description="List operations: rows to skip; pass the previous page's next_offset.",
        ),
    }


def _launch_directory(orchestrator: OrchestratorProtocol) -> Located[str | None]:
    """Read this job's exact active A2 bridge capture, never process cwd."""
    session_id = location_session_id()
    manager = getattr(orchestrator, "plugin_manager", None)
    if not session_id or manager is None:
        return Located(None, ())
    try:
        messaging = manager.get_plugin("agent_messaging_plugin")
        surface = getattr(messaging, "_platform_surface", None)
        capture = surface.launch_directory_for_session(session_id) if surface is not None else None
        if isinstance(capture, dict) and isinstance(capture.get("path"), str):
            advisories = ("Launch directory uses the bridge client's cwd fallback; refresh the launcher.",) if capture.get("source") == "cwd_fallback" else ()
            return Located(capture["path"], advisories)
    except Exception:
        return Located(None, ("Launch directory capture is unavailable; using path addresses and solet_home.",))
    return Located(None, ())


def _read_location_roots(orchestrator: OrchestratorProtocol | None) -> tuple[Located[tuple[Path, ...]], SoletTree]:
    """Read live address inputs and assemble advisory-bearing roots."""
    if orchestrator is None:
        raise RuntimeError("orchestrator APP_HOME must be injected before file I/O")
    tree = solet_tree_from_app_home(Path(orchestrator.APP_HOME))
    try:
        service = orchestrator.get_service("address_book_service")
        addresses = path_addresses(cast(AddressBookServiceInterface, service)) if service is not None else Located((), ("Path address service is unavailable; using computed solet_home.",))
    except Exception:
        addresses = Located((), ("Path addresses could not be read; using computed solet_home.",))
    launch = _launch_directory(orchestrator)
    name = os.environ.get("SOLET_NAME", tree.repo_root.name)
    home = default_solet_home(name, documents_folder(sys.platform, os.environ, Path.home()), tree, Path.home() / "Library/Application Support" / name / "Files")
    roots = derive_allowed_roots(addresses.value, launch.value, tree, home.value)
    return Located(roots.value, (*addresses.advisories, *launch.advisories, *home.advisories, *roots.advisories)), tree


class ExternalPostgresPlugin(PluginBase, EdgeProcessProvider):
    """Foreign-Postgres plugin (the "super Datagrip"); vendor GRANTs are the only boundary."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None
        # D0.3 deferred-completion machinery (async_jobs.py) — lazily acquired /
        # started on first async-shaped dispatch, mirroring
        # comfyui_image_generation_plugin's _try_acquire_job_manager (boot order
        # does not guarantee orchestrator_ref.async_job_manager is set yet at
        # prepare_for_readiness time, but it always is by first dispatch).
        self._async_job_manager: Any | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned keys (password chain-consumed)
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required — per-connection passwords are chain-consumed.

        Each connection's password lives in the address book RESOLVER's namespace
        (``<solet>.default_address_book_plugin.external_pg_<name>_password``)
        and is read only through ``resolve_with_secrets`` under the resolver's
        identity — never a direct vault verb under this plugin. Post-2026-06-07
        namespace enforcement would reject such a key, so it is declared nowhere.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """No scoped vault keys are read or written directly by this plugin."""
        return []

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def initialize(self, config: dict[str, object]) -> None:
        """Bind config_provider so yaml defaults + operator overrides take effect."""
        self.config_provider = ConfigProvider(self.name, config)

    def _config(self) -> ConfigProvider:
        """The bound config provider — fail loud if boot never called initialize().

        A missing binding is a lifecycle fault (a boot or re-instantiation path
        that skipped ``initialize``), never a license to guess: the prior
        ``or {}`` fallback silently turned exactly that fault into refuse-all
        exports on the snowflake sibling's live boot (2026-07-16).
        """
        if self.config_provider is None:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                "config_provider not bound — plugin.initialize() was never called (boot/lifecycle fault)",
            )
        return self.config_provider

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        self.logger = logging.getLogger(self.name)
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires address_book_service as the connection registry")
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        self.set_ready()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _success(self, data: dict[str, Any]) -> dict[str, Any]:
        if location_advisories():
            data = {**data, "advisories": [*data.get("advisories", []), *location_advisories()]}
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _error(self, code: str, message: str) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.ERROR.value,
            "data": {},
            "actions": [],
            "error": {"code": code, "message": message},
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _statement_timeout_ms(self) -> int:
        """The per-statement timeout (ms) from plugin config — enforced POSITIVE, fail-loud.

        The timeout is the belt-tier DoS bound and is NON-DISABLEABLE by design:
        ``statement_timeout=0`` (or a negative value) disables the server-side
        cancel, so a non-positive / non-integer config value is REFUSED at parse
        (fast-fail) rather than silently opening a timeout-less connection. This is
        evaluated before every ``connection.connect`` call in ``_run_on_connection``,
        so a misconfigured plugin never opens a connection without the bound.
        """
        config = self._config()
        raw = config.get(CONFIG_KEY_STATEMENT_TIMEOUT_MS, STATEMENT_TIMEOUT_MS_DEFAULT)
        try:
            timeout_ms = int(str(raw))
        except ValueError as exc:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_STATEMENT_TIMEOUT_MS} must be a positive integer (got {raw!r})",
            ) from exc
        if timeout_ms <= 0:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_STATEMENT_TIMEOUT_MS} must be > 0 (got {timeout_ms}); a non-positive value would disable the statement-timeout DoS bound",
            )
        return timeout_ms

    def _platform_pg_port(self) -> int:
        """The platform's own Postgres port from plugin config — enforced valid, fail-loud.

        This value feeds the §8.4 containment guard's ``(host, port, dbname)`` INSTANCE
        compare, so a fat-fingered non-integer or out-of-range value would silently
        WEAKEN the guard's port arm (guard-integrity class). Mirrors F2's timeout
        bound: a present-but-invalid value (non-integer, or outside the TCP port
        range 1–65535) is REFUSED at parse (fast-fail) rather than silently degrading
        containment. Evaluated before every ``connection.connect`` in
        ``_run_on_connection``. An absent value keeps the sane 5432 default (the
        platform's own port), matching ``_statement_timeout_ms``'s absent→default.
        """
        config = self._config()
        raw = config.get(CONFIG_KEY_PLATFORM_PG_PORT, PLATFORM_PG_PORT_DEFAULT)
        try:
            port = int(str(raw))
        except ValueError as exc:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_PLATFORM_PG_PORT} must be an integer in [1, 65535] (got {raw!r})",
            ) from exc
        if not 1 <= port <= 65535:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_PLATFORM_PG_PORT} must be in [1, 65535] (got {port}); an out-of-range value would weaken the platform-DB containment guard's port arm",
            )
        return port

    def _location_roots(self) -> tuple[Located[tuple[Path, ...]], SoletTree]:
        return _read_location_roots(self.orchestrator_ref)

    def _assert_path(self, path: str, *, suffix: str | None, param_name: str, reading: bool = False) -> str:
        """The single binding to shared connector containment on every I/O."""
        roots, tree = self._location_roots()
        retain_location_advisories(roots.advisories)
        try:
            result = assert_path_allowed(path, roots, tree, suffix=suffix, param_name=param_name, plugin_name=self.name)
            if reading and not Path(result.value).is_file():
                raise PathRefusedError(f"{param_name} must name an existing regular file")
            return result.value
        except PathRefusedError as exc:
            exc.code = ERROR_EXPORT_PATH_REFUSED
            raise

    def _export_path_gate(self, output_tsv_path: str) -> str:
        self._config()
        return self._assert_path(output_tsv_path, suffix=".tsv", param_name="output_tsv_path")

    def _file_path_gate(self, path: str, param_name: str) -> str:
        self._config()
        return self._assert_path(path, suffix=None, param_name=param_name)

    def _tool_settings(self) -> ToolSettings:
        """Where pg_dump / pg_restore live and their run timeout — fail loud if invalid."""
        config = self._config()
        bin_dir = config.get(CONFIG_KEY_CLIENT_BIN_DIR, "")
        if not isinstance(bin_dir, str):
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_CLIENT_BIN_DIR} must be a directory path string",
            )
        raw = config.get(CONFIG_KEY_CLIENT_TOOL_TIMEOUT_SECONDS, CLIENT_TOOL_TIMEOUT_SECONDS_DEFAULT)
        try:
            timeout = int(str(raw))
        except ValueError as exc:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_CLIENT_TOOL_TIMEOUT_SECONDS} must be a positive integer (got {raw!r})",
            ) from exc
        if timeout <= 0:
            raise ExternalPgConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_CLIENT_TOOL_TIMEOUT_SECONDS} must be > 0 (got {timeout})",
            )
        return ToolSettings(bin_dir=bin_dir, timeout_seconds=timeout, platform_pg_port=self._platform_pg_port())

    def _run_with_dsn(
        self,
        params: dict[str, Any],
        action: Callable[[ExternalDsn], dict[str, Any]],
        verb: str,
    ) -> dict[str, Any]:
        """Resolve a connection NAME to its platform-guarded target and run a client program.

        Same envelope and typed, topology-safe errors as ``_run_on_connection``;
        no psycopg connection is opened (pg_dump / pg_restore open their own).
        """
        if self._app_config_loader is None:
            return self._error(ERROR_NOT_CONFIGURED, f"{self.name} is not ready")
        raw_name = params.get("connection_name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return self._error(ERROR_INVALID_PARAMS, "'connection_name' is required")
        dsn: ExternalDsn | None = None
        try:
            dsn = self._app_config_loader.resolve(raw_name)
            connection.assert_foreign_target(dsn, self._platform_pg_port())
            outcome = self._success(action(dsn))
        except Exception as exc:  # coded guards + client-program faults -> typed
            code, message = self._classify_run_error(exc, verb)
            outcome = self._error(code, message)
        return outcome

    def _run_on_connection(
        self,
        params: dict[str, Any],
        action: Callable[[Any], dict[str, Any]],
        verb: str,
    ) -> dict[str, Any]:
        """Resolve a connection NAME, open a hardened connection, run the action.

        What the connection can do is decided by the server's own GRANTs on the
        registered credential, never a plugin-side check.

        Error classification is TOPOLOGY-SAFE: connection/auth/permission classes
        return a generic fixed message; only the caller's-own-query classes carry
        driver detail (connection.classify_pg_error). The connection is always
        closed in ``finally`` — fresh connection per call (§8.6).
        """
        if self._app_config_loader is None:
            return self._error(ERROR_NOT_CONFIGURED, f"{self.name} is not ready")
        raw_name = params.get("connection_name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return self._error(ERROR_INVALID_PARAMS, "'connection_name' is required")
        conn: Any = None
        dsn: ExternalDsn | None = None
        try:
            dsn = self._app_config_loader.resolve(raw_name)
            conn = connection.connect(
                dsn,
                statement_timeout_ms=self._statement_timeout_ms(),
                platform_pg_port=self._platform_pg_port(),
            )
            outcome = self._success(action(conn))
        except Exception as exc:  # our coded guards + any driver fault -> typed
            code, message = self._classify_run_error(exc, verb)
            outcome = self._error(code, message)
        finally:
            if conn is not None:
                conn.close()
        return outcome

    def _classify_run_error(self, exc: Exception, verb: str) -> tuple[str, str]:
        """Map a ``_run_on_connection`` exception to a typed, topology-safe (code, message).

        Our own coded guard errors surface their (safe) message; a ValueError is a
        bad param; anything else is a driver fault classified generically for the
        connection/auth/permission classes (connection.classify_pg_error).
        """
        if isinstance(
            exc,
            (
                ExternalPgConfigError,
                ExternalPgGuardError,
                StatementGuardError,
                ScriptStatementError,
                ClientToolError,
                PathRefusedError,
            ),
        ):
            return exc.code, str(exc)
        if isinstance(exc, ValueError):
            return ERROR_INVALID_PARAMS, str(exc)
        code, message = connection.classify_pg_error(exc)
        if self.logger:
            self.logger.warning("%s failed: %s", verb, code)
        return code, message

    def _dispatch_async(
        self,
        action_name: str,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """D0.3 ms-scale dispatch: create the job, return immediately — no I/O here."""
        try:
            create_result = async_jobs.create_job(
                self,
                action_name=action_name,
                params=params,
                state=state,
            )
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except RuntimeError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        if create_result.get("action_status") != "completed":
            error = create_result.get("error", {})
            message = str(error.get("message", "failed to create async job"))
            return self._error(ERROR_API_ERROR, message)
        return self._success(create_result["data"])

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "run_query": _edge("run_query", RESULT_TYPE_RUN_QUERY),
            "list_connections": _edge("list_connections", RESULT_TYPE_LIST_CONNECTIONS),
            "list_schemas": _edge("list_schemas", RESULT_TYPE_LIST_SCHEMAS),
            "list_tables": _edge("list_tables", RESULT_TYPE_LIST_TABLES),
            "describe_table": _edge("describe_table", RESULT_TYPE_DESCRIBE_TABLE),
            "export_query": _edge("export_query", RESULT_TYPE_EXPORT_QUERY),
            "test_connection": _edge("test_connection", RESULT_TYPE_TEST_CONNECTION),
            "run_statement": _edge("run_statement", RESULT_TYPE_RUN_STATEMENT),
            "run_script": _edge("run_script", RESULT_TYPE_RUN_SCRIPT),
            "copy": _edge("copy", RESULT_TYPE_COPY),
            "dump_database": _edge("dump_database", RESULT_TYPE_DUMP_DATABASE),
            "restore_database": _edge("restore_database", RESULT_TYPE_RESTORE_DATABASE),
            "activity": _edge("activity", RESULT_TYPE_ACTIVITY),
            "statistics": _edge("statistics", RESULT_TYPE_STATISTICS),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="run_query",
        display_name="External Postgres: Run Query",
        description=(
            "Run ONE SQL statement against a registered foreign Postgres connection "
            "(by connection_name) and write its result rows to a TSV file; the transaction commits "
            "after the file is written, so a data-modifying CTE takes effect. What the statement "
            "may do is decided only by the registered role's own server-side GRANTs. Returns immediately "
            "with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch "
            "returning is NOT the same as the job finishing. When the job completes, the result is "
            "ALWAYS written to the caller-supplied output_tsv_path, never returned inline — this "
            "connection is an arbitrary customer database, so there is no vendor-imposed row "
            f"ceiling to defer to; the limit below is entirely our own policy. Defaults to "
            f"{DEFAULT_ROW_LIMIT} rows to avoid exhausting the target database and disk, and to "
            "discourage pulling all rows for client-side filtering that a WHERE clause should do "
            "instead. To fetch more, pass acknowledge_default_limit_override=true together with "
            f"an explicit row_limit (up to {MAX_ROWS_HARD_CAP}) — both are required together, and "
            f"a row_limit above {MAX_ROWS_HARD_CAP} is refused rather than silently clamped. For "
            f"pulls beyond {MAX_ROWS_HARD_CAP} rows, use export_query instead (same override "
            f"mechanism, hard cap {EXPORT_ROW_CAP}). Use list_connections to see registered names. "
            "When the goal is validating that records exist or picking one to act on next, prefer "
            "selecting stable ID columns over email addresses or other PII-bearing fields — the "
            "query decides what columns come back, so a narrower SELECT is both cheaper and "
            "lower-exposure."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name (from list_connections).",
            ),
            "sql": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="A single SQL statement whose result rows to write (any statement the role's GRANTs permit).",
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=("ABSOLUTE .tsv destination path, contained under a registered path address."),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(f"Must be exactly true, together with row_limit, to fetch more than the default {DEFAULT_ROW_LIMIT} rows. Requires understanding why the default exists: avoiding exhausted rate limits/disk, and pulling all rows to filter client-side instead of writing a proper WHERE clause."),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(f"Explicit row ceiling, up to {MAX_ROWS_HARD_CAP}. Only honored together with acknowledge_default_limit_override=true; refused (not clamped) above {MAX_ROWS_HARD_CAP}."),
            ),
        },
        output_type="object",
        output_description="Job ID and status for async query tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def run_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("run_query", params, state)

    @platform_process(
        name="list_connections",
        display_name="External Postgres: List Connections",
        description=("List the registered foreign-Postgres connection names (external_pg::* address-book entries). Names only — never hosts, users, or passwords. Use a name with the other verbs."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="The registered connection names.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_connections(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        if self._app_config_loader is None:
            return self._error(ERROR_NOT_CONFIGURED, f"{self.name} is not ready")
        try:
            names, truncated = self._app_config_loader.list_connection_names()
        except Exception:  # address-book fault -> generic
            return self._error(ERROR_API_ERROR, "could not list connections")
        if truncated and self.logger:
            self.logger.warning("list_connections truncated at the address-book scan limit")
        return self._success({"connections": names})

    @platform_process(
        name="list_schemas",
        display_name="External Postgres: List Schemas",
        description=(
            "List the non-system schemas in a registered foreign Postgres connection. Requires "
            "connection_name. Returns immediately with a job_id and status 'queued' — the actual "
            "schema list is delivered when the job completes (D0.3 deferred-completion shape); the "
            "dispatch returning is NOT the same as the job finishing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
        },
        output_type="object",
        output_description="Job ID and status for async schema-list tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the schema list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_schemas(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_schemas", params, state)

    @platform_process(
        name="list_tables",
        display_name="External Postgres: List Tables",
        description=(
            "List tables and views in a schema of a registered foreign Postgres connection. "
            "Requires connection_name and schema. Returns immediately with a job_id and status "
            "'queued' — the actual table list is delivered when the job completes (D0.3 "
            "deferred-completion shape); the dispatch returning is NOT the same as the job finishing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
            "schema": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The schema to list tables from.",
            ),
        },
        output_type="object",
        output_description="Job ID and status for async table-list tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the table list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_tables(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_tables", params, state)

    @platform_process(
        name="describe_table",
        display_name="External Postgres: Describe Table",
        description=(
            "Describe a table's columns (name, type, nullability, default) in a registered foreign "
            "Postgres connection. Requires connection_name, schema, and table. Returns immediately "
            "with a job_id and status 'queued' — the column description is delivered when the job "
            "completes (D0.3 deferred-completion shape); the dispatch returning is NOT the same as "
            "the job finishing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
            "schema": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The table's schema.",
            ),
            "table": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The table name.",
            ),
        },
        output_type="object",
        output_description="Job ID and status for async table-describe tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the column list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_table(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("describe_table", params, state)

    @platform_process(
        name="export_query",
        display_name="External Postgres: Export Query",
        description=(
            "The N>>500 route: run one SQL statement against a registered foreign Postgres "
            "connection, write the result as ONE tab-separated .tsv file at an ABSOLUTE "
            "output_tsv_path in the operator's workspace. Returns immediately with a job_id and "
            "status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the "
            "same as the job finishing. The path must lie under an operator-configured "
            "registered path addresses entry (register a path address to authorize each export). Same commit-after-write "
            "behaviour and override mechanism as run_query, with a higher hard cap: this connection is an "
            "arbitrary customer database, so there is no vendor-imposed row ceiling, only our own "
            f"policy. Defaults to {DEFAULT_ROW_LIMIT} rows absent an acknowledged override — for "
            "that common small/default case, run_query has an identical interface with a lower "
            "ceiling. To fetch more, pass acknowledge_default_limit_override=true together with an "
            f"explicit row_limit (up to {EXPORT_ROW_CAP}) — both are required together, and a "
            f"row_limit above {EXPORT_ROW_CAP} is refused rather than silently clamped. Requires "
            "connection_name, sql, and output_tsv_path. When the goal is validating that records "
            "exist rather than inspecting their content, prefer selecting stable ID columns over "
            "email addresses or other PII-bearing fields — the query decides what columns land in "
            "the file."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
            "sql": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="A single SQL statement whose result rows to write (any statement the role's GRANTs permit).",
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=("ABSOLUTE .tsv destination path, contained under a registered path address."),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(f"Must be exactly true, together with row_limit, to fetch more than the default {DEFAULT_ROW_LIMIT} rows. Requires understanding why the default exists: avoiding exhausted rate limits/disk, and pulling all rows to filter client-side instead of writing a proper WHERE clause."),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(f"Explicit row ceiling, up to {EXPORT_ROW_CAP}. Only honored together with acknowledge_default_limit_override=true; refused (not clamped) above {EXPORT_ROW_CAP}."),
            ),
        },
        output_type="object",
        output_description="Job ID and status for async export tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the TSV handle itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def export_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("export_query", params, state)

    @platform_process(
        name="test_connection",
        display_name="External Postgres: Test Connection",
        description=(
            "Open a registered foreign Postgres connection and confirm it: the resolved host it "
            "connected to, the server version, the current role, and the server's own "
            "transaction_read_only setting (true only on a hot standby or a role configured that "
            "way). Use this to verify a connection points where you expect (the host you "
            "registered) before trusting it. Requires connection_name. Returns immediately with a "
            "job_id and status 'queued' — the confirmation is delivered when the job completes "
            "(D0.3 deferred-completion shape); the dispatch returning is NOT the same as the job "
            "finishing."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
        },
        output_type="object",
        output_description="Job ID and status for async connection-test tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the confirmation itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("test_connection", params, state)

    @platform_process(
        name="run_statement",
        display_name="External Postgres: Run Statement",
        description=(
            "Run ONE SQL statement against a registered foreign Postgres connection (by "
            "connection_name) — INSERT/UPDATE/"
            "DELETE/DDL/anything, not just reads. What the statement is actually allowed to do "
            "is decided entirely by the registered credential's own server-side Postgres GRANTs "
            "— this verb performs no read/write classification or permission check of its own "
            "(vendor RBAC is the control plane, operator ruling 2026-08-09 + Amendment 1). "
            "Single-statement contract for v1 (an engineering convention for predictable commit "
            "semantics, not a permission gate); run_script runs several statements in one "
            "transaction. autocommit=true runs a command the server refuses inside a transaction "
            "block (VACUUM, CREATE DATABASE, CREATE INDEX CONCURRENTLY, ALTER SYSTEM). A statement with no "
            "result set (the common INSERT/UPDATE/DELETE/DDL case, no RETURNING) commits and "
            "returns rowcount inline. A statement that DOES produce a result set (a RETURNING "
            "clause) routes through the SAME always-TSV export path as run_query: rows are never "
            "returned inline, at any size, so output_tsv_path is then required — its absence "
            "rolls the whole statement back rather than silently discarding the returned rows "
            "while still committing the write. Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same "
            "as the job finishing. Use list_connections to see registered names."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name (from list_connections).",
            ),
            "sql": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=("A single SQL statement — any statement the registered credential's own server-side GRANTs permit, not just reads."),
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=("ABSOLUTE .tsv destination path, contained under a registered path address. Required ONLY if the statement produces a result set (e.g. a RETURNING clause) — omit it for a plain INSERT/UPDATE/DELETE/DDL with no RETURNING, which returns rowcount inline instead."),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(f"Only relevant when the statement has a RETURNING clause: must be exactly true, together with row_limit, to fetch more than the default {DEFAULT_ROW_LIMIT} returned rows."),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(f"Only relevant when the statement has a RETURNING clause: explicit row ceiling on the RETURNING rows, up to {MAX_ROWS_HARD_CAP}. Only honored together with acknowledge_default_limit_override=true."),
            ),
            "autocommit": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=("true runs the statement outside a transaction block, for commands the server refuses inside one (VACUUM, CREATE DATABASE, CREATE INDEX CONCURRENTLY, ALTER SYSTEM). The server commits it as it runs. Default false."),
            ),
        },
        output_type="object",
        output_description="Job ID and status for async statement-execution tracking.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the statement's own result.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def run_statement(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("run_statement", params, state)

    @platform_process(
        name="run_script",
        display_name="External Postgres: Run Script",
        description=(
            "Run several SQL statements (a migration, a seed, any script) against a registered "
            "foreign Postgres connection in ONE transaction, each statement inside its own "
            "savepoint. on_error='rollback' (default) rolls the whole script back at the first "
            "failure and reports external_pg.script_statement_failed naming the statement's "
            "number, keyword and text; on_error='continue' rolls back only the failing statement "
            "and commits the rest; commit=false runs everything then rolls back (a dry run). "
            "Statements are split by a dollar-quote and comment aware parser, so function bodies "
            "stay whole. The script must not contain BEGIN/COMMIT/ROLLBACK/SAVEPOINT (the verb "
            "owns the transaction); commands the server refuses inside a transaction block "
            "(VACUUM, CREATE DATABASE) belong in run_statement with autocommit=true. Each "
            "statement reports its command tag and row count; rows are never returned (use "
            "run_query). Vendor GRANTs alone decide what runs. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": _CONNECTION_PARAMETER,
            "sql": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The script: one or more SQL statements separated by semicolons.",
            ),
            "on_error": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="rollback (default: undo the whole script) or continue (undo only the failing statement).",
            ),
            "commit": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description="true (default) commits; false rolls back after running (a dry run).",
            ),
        },
        output_type="object",
        output_description="Job ID and status for async script tracking.",
        return_value_schema=_QUEUED_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def run_script(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("run_script", params, state)

    @platform_process(
        name="copy",
        display_name="External Postgres: COPY Files",
        description=(
            "Bulk-move rows between a registered foreign Postgres connection and a file in the "
            "operator's workspace with PostgreSQL COPY. operation from_file loads a CSV, text or "
            "binary file into schema.table (optional columns, where filter) and commits; "
            "operation to_file writes schema.table (optional columns) or a query to the file. "
            "Every COPY option is a parameter: format, freeze, delimiter, null, default, header, "
            "quote, escape, force_quote, force_not_null, force_null, on_error, encoding, "
            "log_verbosity. The data streams over the connection (COPY FROM STDIN / TO STDOUT), "
            "so path is an ABSOLUTE path under a registered path address. Vendor GRANTs alone "
            "decide what is allowed. Returns a queued job; the result has rowcount and bytes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": _CONNECTION_PARAMETER,
            "operation": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=f"One of: {', '.join(copy_actions.OPERATIONS)}.",
            ),
            "path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE file path under a registered path address (read for from_file, written for to_file).",
            ),
            "schema": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="The table's schema (required for from_file, and for to_file without query).",
            ),
            "table": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="The table name (required for from_file, and for to_file without query).",
            ),
            "columns": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description="Column names, in file order; omit for every column.",
            ),
            "query": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="to_file only: a query whose rows to write, instead of schema/table.",
            ),
            "where": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="from_file only: COPY FROM … WHERE condition; only matching rows load.",
            ),
            **{
                name: ParameterMetadata(
                    type=ParameterType.LIST if name.startswith("force_") else (ParameterType.BOOLEAN if name == "freeze" else ParameterType.STRING),
                    required=False,
                    description=_COPY_OPTION_TEXT[name],
                )
                for name in copy_actions.COPY_OPTIONS
            },
        },
        output_type="object",
        output_description="Job ID and status for async COPY tracking.",
        return_value_schema=_QUEUED_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def copy(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("copy", params, state)

    @platform_process(
        name="dump_database",
        display_name="External Postgres: Dump Database",
        description=(
            "Back up a registered foreign Postgres database with pg_dump into the operator's "
            "workspace. format plain (SQL script, the vendor default), custom, directory or tar; "
            "output_path is an ABSOLUTE path under a registered path address. Every pg_dump "
            "option is a parameter named after its long flag (schema, exclude_schema, table, "
            "exclude_table_data, schema_only, data_only, clean, if_exists, create, no_owner, "
            "no_privileges, compress, jobs, section, inserts, role and the rest). The password "
            "reaches pg_dump through a private temporary password file only. pg_dump is found in "
            "client_bin_dir, then PATH, then the standard PostgreSQL directories. Returns a "
            "queued job; the result has output_path, format, bytes and pg_dump's messages."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": _CONNECTION_PARAMETER,
            "output_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE file (or, for format directory, new directory) path under a registered path address.",
            ),
            "format": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="plain (default), custom, directory or tar.",
            ),
            **_option_parameters(dict(client_tools.PG_DUMP_OPTIONS), "pg_dump"),
        },
        output_type="object",
        output_description="Job ID and status for async dump tracking.",
        return_value_schema=_QUEUED_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def dump_database(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("dump_database", params, state)

    @platform_process(
        name="restore_database",
        display_name="External Postgres: Restore Database",
        description=(
            "Restore a pg_dump archive from the operator's workspace into a registered foreign "
            "Postgres connection (it can be a different server from the one dumped). Archives only "
            "(custom, directory, tar; detected), restored through pg_restore, which runs no "
            "client-side commands, with every restore option as a parameter (clean, if_exists, "
            "create, data_only, schema_only, no_owner, no_privileges, single_transaction, "
            "exit_on_error, jobs, schema, table, index, function, trigger, section, use_list, "
            "transaction_size, role and the rest). A plain-text SQL file is refused "
            "(external_pg.plain_sql_restore_refused): replaying it needs psql, whose backslash "
            "meta-commands run on this host outside the database's grants; use run_script for "
            "plain SQL. With create, the archive's own database name is platform-guarded first. "
            "input_path is an ABSOLUTE path under a registered path address. Vendor GRANTs "
            "alone decide what is created, replaced or dropped. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": _CONNECTION_PARAMETER,
            "input_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE dump file (or directory-format dump directory) under a registered path address.",
            ),
            "format": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Optional check: plain, custom, directory or tar; refused if it differs from the detected format.",
            ),
            **_option_parameters(dict(client_tools.PG_RESTORE_OPTIONS), "pg_restore"),
        },
        output_type="object",
        output_description="Job ID and status for async restore tracking.",
        return_value_schema=_QUEUED_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def restore_database(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("restore_database", params, state)

    @platform_process(
        name="activity",
        display_name="External Postgres: Sessions and Locks",
        description=(
            "See and control what is running on a registered foreign Postgres server. operation "
            "list reads pg_stat_activity (pid, database, user, application, client, state, waits, "
            "timings, query; optional state and database filters); locks reads pg_locks with "
            "pg_blocking_pids (optional granted filter); cancel calls pg_cancel_backend(pid); "
            "terminate calls pg_terminate_backend(pid, timeout_ms). List operations page with "
            "limit and offset and return next_offset when more rows exist. What the role sees "
            "and may signal is its own privilege (pg_read_all_stats, pg_signal_backend). Returns "
            "a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": _CONNECTION_PARAMETER,
            "operation": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=f"One of: {', '.join(admin_actions.ACTIVITY_OPERATIONS)}.",
            ),
            "pid": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description="cancel/terminate: the backend process id (from list).",
            ),
            "timeout_ms": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description="terminate: milliseconds to wait for the backend to exit (0 = do not wait, the default).",
            ),
            "state": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="list: only sessions in this state (active, idle, idle in transaction, …).",
            ),
            "database": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="list: only sessions connected to this database.",
            ),
            "granted": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description="locks: true for held locks only, false for awaited locks only.",
            ),
            **_paging_parameters(),
        },
        output_type="object",
        output_description="Job ID and status for async activity tracking.",
        return_value_schema=_QUEUED_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def activity(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("activity", params, state)

    @platform_process(
        name="statistics",
        display_name="External Postgres: Sizes and Statistics",
        description=(
            "Read and reset a registered foreign Postgres server's size and usage statistics. "
            "operation database_sizes (pg_database_size per database the role may connect to); "
            "relation_sizes (total, table and index bytes and estimated rows per table, largest "
            "first, optional schema); table_stats (pg_stat_user_tables scans, tuples, dead "
            "tuples, vacuum and analyze times); index_stats (pg_stat_user_indexes scans and "
            "size); reset (pg_stat_reset for the current database); reset_table (one table's "
            "counters, schema and table). List operations page with limit and offset and return "
            "next_offset. Vendor privileges alone decide access. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": _CONNECTION_PARAMETER,
            "operation": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=f"One of: {', '.join(admin_actions.STATISTICS_OPERATIONS)}.",
            ),
            "schema": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="relation_sizes/table_stats/index_stats: one schema only; reset_table: the table's schema.",
            ),
            "table": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="reset_table: the table whose counters to reset.",
            ),
            **_paging_parameters(),
        },
        output_type="object",
        output_description="Job ID and status for async statistics tracking.",
        return_value_schema=_QUEUED_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def statistics(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("statistics", params, state)


def _edge(
    name: str,
    result_type: str,
) -> EdgeProcessDefinition:
    return EdgeProcessDefinition(
        name=name,
        result_processor_template_customizations=MergeResultProcessorCustomizations(
            result_type=result_type,
        ),
        error_processor_template_customizations=MergeErrorProcessorCustomizations(
            retryable=True,
        ),
    )
