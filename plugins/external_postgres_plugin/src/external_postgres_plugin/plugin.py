"""External Postgres plugin entry point — the read-only "super Datagrip".

Query FOREIGN Postgres databases the operator registers as ``external_pg::<name>``
address-book entries. READ-ONLY, HARD: no write verb exists, and the psycopg3
connection read-only characteristic (connection.py) is the developer-proof
write-stopper. Every verb takes a connection NAME (never a DSN); the platform's
own DB instance is refused role-independently (connection.assert_foreign_target).

Verbs (all EDGE, all reads):
  - run_query        — one read-only statement; rows inline, FAILS LOUD over
    the inline caps (A4 — no blob spill)
  - list_connections — the registered external_pg::* connection names
  - list_schemas / list_tables / describe_table — first-class introspection
  - export_query     — full result written as a TSV file in the operator's
    workspace (absolute output_tsv_path, contained under the
    export_allowed_roots config; refuse-all when unset)
  - test_connection  — server version, current role, read-only flag

No plugin-owned vault keys (the per-connection password is chain-consumed through
the address book's ``resolve_with_secrets``), so this plugin needs NO vault
binding — only address_book_service (the connection registry). Blob storage is
no longer used anywhere (bulk data lands as workspace TSV files; interactive
overflows fail loud).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

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
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from . import connection, export_containment, query_actions
from .app_config import AppConfigLoader, ExternalPgConfigError
from .connection import ExternalPgGuardError
from .constants import (
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONFIG_KEY_PLATFORM_PG_PORT,
    CONFIG_KEY_STATEMENT_TIMEOUT_MS,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    PLATFORM_PG_PORT_DEFAULT,
    PLUGIN_NAME,
    RESULT_TYPE_DESCRIBE_TABLE,
    RESULT_TYPE_EXPORT_QUERY,
    RESULT_TYPE_LIST_CONNECTIONS,
    RESULT_TYPE_LIST_SCHEMAS,
    RESULT_TYPE_LIST_TABLES,
    RESULT_TYPE_RUN_QUERY,
    RESULT_TYPE_TEST_CONNECTION,
    STATEMENT_TIMEOUT_MS_DEFAULT,
)
from .statement_guard import StatementGuardError


class ExternalPostgresPlugin(PluginBase, EdgeProcessProvider):
    """Read-only foreign-Postgres query plugin (the "super Datagrip")."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned keys (password chain-consumed)
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required — per-connection passwords are chain-consumed.

        Each connection's password lives in the address book RESOLVER's namespace
        (``<homunculus>.default_address_book_plugin.external_pg_<name>_password``)
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
                "config_provider not bound — plugin.initialize() was never "
                "called (boot/lifecycle fault)",
            )
        return self.config_provider

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        self.logger = logging.getLogger(self.name)
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(
                f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires "
                "address_book_service as the connection registry"
            )
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        self.set_ready()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _success(self, data: dict[str, Any]) -> dict[str, Any]:
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
                f"{CONFIG_KEY_STATEMENT_TIMEOUT_MS} must be > 0 (got {timeout_ms}); a "
                "non-positive value would disable the statement-timeout DoS bound",
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
                f"{CONFIG_KEY_PLATFORM_PG_PORT} must be in [1, 65535] (got {port}); an "
                "out-of-range value would weaken the platform-DB containment guard's port arm",
            )
        return port

    def _export_path_gate(self, output_tsv_path: str) -> str:
        """Admit an export path via workspace-root containment; return the realpath.

        Binds the operator's ``export_allowed_roots`` config (yaml default
        ``[]`` = refuse-all; no hardcoded callsite default per authoring trap
        #10) to the own-copy containment gate. A malformed config value is a
        loud config fault, never a silent admit-all or refuse-all.
        """
        config = self._config()
        raw_roots = config.get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS)
        roots: list[str] = []
        if raw_roots is not None:
            if not isinstance(raw_roots, list) or not all(
                isinstance(entry, str) for entry in raw_roots
            ):
                raise ExternalPgConfigError(
                    ERROR_NOT_CONFIGURED,
                    f"{CONFIG_KEY_EXPORT_ALLOWED_ROOTS} must be a list of directory "
                    "path strings",
                )
            roots = list(raw_roots)
        return export_containment.assert_export_path_allowed(
            output_tsv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name=self.name,
        )

    def _run_on_connection(
        self,
        params: dict[str, Any],
        action: Callable[[Any], dict[str, Any]],
        verb: str,
    ) -> dict[str, Any]:
        """Resolve a connection NAME, open a hardened read-only connection, run the action.

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
        try:
            dsn = self._app_config_loader.resolve(raw_name)
            conn = connection.connect(
                dsn,
                statement_timeout_ms=self._statement_timeout_ms(),
                platform_pg_port=self._platform_pg_port(),
            )
            return self._success(action(conn))
        except Exception as exc:  # our coded guards + any driver fault -> typed
            code, message = self._classify_run_error(exc, verb)
            return self._error(code, message)
        finally:
            if conn is not None:
                conn.close()

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
                query_actions.ResultTooLargeError,
                export_containment.ExportPathRefusedError,
            ),
        ):
            return exc.code, str(exc)
        if isinstance(exc, ValueError):
            return ERROR_INVALID_PARAMS, str(exc)
        code, message = connection.classify_pg_error(exc)
        if self.logger:
            self.logger.warning("%s failed: %s", verb, code)
        return code, message

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "run_query": _edge("run_query", RESULT_TYPE_RUN_QUERY),
            "list_connections": _edge(
                "list_connections", RESULT_TYPE_LIST_CONNECTIONS),
            "list_schemas": _edge(
                "list_schemas", RESULT_TYPE_LIST_SCHEMAS),
            "list_tables": _edge(
                "list_tables", RESULT_TYPE_LIST_TABLES),
            "describe_table": _edge(
                "describe_table", RESULT_TYPE_DESCRIBE_TABLE),
            "export_query": _edge(
                "export_query", RESULT_TYPE_EXPORT_QUERY),
            "test_connection": _edge(
                "test_connection", RESULT_TYPE_TEST_CONNECTION),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="run_query",
        display_name="External Postgres: Run Query",
        description=(
            "Run ONE read-only SQL statement against a registered foreign Postgres connection "
            "(by connection_name). Read leaders only (SELECT/WITH/EXPLAIN/SHOW/VALUES/TABLE); the "
            "connection is read-only at the server, so writes/DDL are refused. Returns rows inline "
            "(up to max_rows, default 200, capped 1000); fails loud with "
            "external_pg.result_too_large over the inline caps — use export_query for bulk. "
            "Use list_connections to see registered names."
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
                description="A single read-only SQL statement.",
            ),
            "max_rows": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description="Max rows to return inline (default 200, capped at 1000).",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Rows inline (columns/rows/row_count/spilled=false). Fails loud over the inline caps.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def run_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            params, lambda conn: query_actions.run_query(conn, params), "run_query"
        )

    @platform_process(
        name="list_connections",
        display_name="External Postgres: List Connections",
        description=(
            "List the registered foreign-Postgres connection names (external_pg::* address-book "
            "entries). Names only — never hosts, users, or passwords. Use a name with the other verbs."
        ),
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
            "connection_name."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="The non-system schema names.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_schemas(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            params, lambda conn: query_actions.list_schemas(conn, params), "list_schemas"
        )

    @platform_process(
        name="list_tables",
        display_name="External Postgres: List Tables",
        description=(
            "List tables and views in a schema of a registered foreign Postgres connection. "
            "Requires connection_name and schema."
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
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Tables with name and kind.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_tables(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            params, lambda conn: query_actions.list_tables(conn, params), "list_tables"
        )

    @platform_process(
        name="describe_table",
        display_name="External Postgres: Describe Table",
        description=(
            "Describe a table's columns (name, type, nullability, default) in a registered foreign "
            "Postgres connection. Requires connection_name, schema, and table."
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
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Columns with name, type, nullable, and default.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_table(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            params, lambda conn: query_actions.describe_table(conn, params), "describe_table"
        )

    @platform_process(
        name="export_query",
        display_name="External Postgres: Export Query",
        description=(
            "Run a read-only query against a registered foreign Postgres connection and write the "
            "full result (up to 50000 rows) as ONE tab-separated .tsv file at an ABSOLUTE "
            "output_tsv_path in the operator's workspace. The path must lie under an "
            "operator-configured export_allowed_roots entry (empty config refuses every export). "
            "Same read-only rules as run_query. Requires connection_name, sql, and output_tsv_path."
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
                description="A single read-only SQL statement.",
            ),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(
                    "ABSOLUTE .tsv destination path, contained under an export_allowed_roots entry."
                ),
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="path of the written TSV, plus columns, row_count, and truncated.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def export_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            params,
            lambda conn: query_actions.export_query(conn, params, self._export_path_gate),
            "export_query",
        )

    @platform_process(
        name="test_connection",
        display_name="External Postgres: Test Connection",
        description=(
            "Open a registered foreign Postgres connection and confirm it: returns the resolved "
            "host it connected to, the server version, the current role, and the read-only flag "
            "(always true). Use this to verify a connection points where you expect (the host you "
            "registered) before trusting it. Requires connection_name."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "connection_name": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The registered connection name.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="ok, host, server_version, current_user, read_only.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            params, lambda conn: query_actions.test_connection(conn, params), "test_connection"
        )


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
