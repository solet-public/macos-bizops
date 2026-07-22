"""Snowflake plugin entry point — a read-only warehouse query connector.

Query the operator-registered Snowflake account ("snowflake_account" address
book entry). READ-ONLY, HARD: no write verb exists. Unlike
external_postgres_plugin, Snowflake has NO session-level read-only flag, so
the statement-leader guard here is FAST-FAIL ONLY — the TRUE developer-proof
boundary is the read-only ROLE the connection is pinned to.

Verbs (all EDGE, all reads):
  - run_query       — one read-only statement; rows inline, FAILS LOUD over
    the inline caps (A4 — no blob spill)
  - list_databases  — databases visible to the current role
  - list_schemas / list_tables / describe_table — introspection
  - export_query    — full result written as a TSV file in the operator's
    workspace (absolute output_tsv_path, contained under the
    export_allowed_roots config; refuse-all when unset)
  - test_connection — account, user, role, warehouse, version

No plugin-owned vault keys (the private key is chain-consumed through the
address book's ``resolve_with_secrets``), so this plugin needs NO vault
binding — only address_book_service (credential resolution). Blob storage is
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
from .app_config import AppConfigLoader, SnowflakeConfigError
from .constants import (
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONFIG_KEY_LOGIN_TIMEOUT_SECONDS,
    CONFIG_KEY_STATEMENT_TIMEOUT_SECONDS,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    LOGIN_TIMEOUT_SECONDS_DEFAULT,
    PLUGIN_NAME,
    RESULT_TYPE_DESCRIBE_TABLE,
    RESULT_TYPE_EXPORT_QUERY,
    RESULT_TYPE_LIST_DATABASES,
    RESULT_TYPE_LIST_SCHEMAS,
    RESULT_TYPE_LIST_TABLES,
    RESULT_TYPE_RUN_QUERY,
    RESULT_TYPE_TEST_CONNECTION,
    STATEMENT_TIMEOUT_SECONDS_DEFAULT,
)
from .statement_guard import StatementGuardError


class SnowflakePlugin(PluginBase, EdgeProcessProvider):
    """Read-only Snowflake warehouse query plugin."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned keys (private key chain-consumed)
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required — the private key is chain-consumed.

        It lives in the address book RESOLVER's namespace
        (``<homunculus>.default_address_book_plugin.snowflake_private_key``)
        and is read only through ``resolve_with_secrets`` under the
        resolver's identity — never a direct vault verb under this plugin.
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
        exports on a live boot (2026-07-16).
        """
        if self.config_provider is None:
            raise SnowflakeConfigError(
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
                "address_book_service to resolve the Snowflake account"
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

    def _statement_timeout_seconds(self) -> int:
        """The session statement timeout (seconds) from plugin config — enforced positive."""
        config = self._config()
        raw = config.get(CONFIG_KEY_STATEMENT_TIMEOUT_SECONDS, STATEMENT_TIMEOUT_SECONDS_DEFAULT)
        try:
            timeout_s = int(str(raw))
        except ValueError as exc:
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_STATEMENT_TIMEOUT_SECONDS} must be a positive integer (got {raw!r})",
            ) from exc
        if timeout_s <= 0:
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_STATEMENT_TIMEOUT_SECONDS} must be > 0 (got {timeout_s}); a "
                "non-positive value would disable the statement-timeout DoS bound",
            )
        return timeout_s

    def _login_timeout_seconds(self) -> int:
        config = self._config()
        raw = config.get(CONFIG_KEY_LOGIN_TIMEOUT_SECONDS, LOGIN_TIMEOUT_SECONDS_DEFAULT)
        try:
            timeout_s = int(str(raw))
        except ValueError as exc:
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_LOGIN_TIMEOUT_SECONDS} must be a positive integer (got {raw!r})",
            ) from exc
        if timeout_s <= 0:
            raise SnowflakeConfigError(
                ERROR_NOT_CONFIGURED,
                f"{CONFIG_KEY_LOGIN_TIMEOUT_SECONDS} must be > 0 (got {timeout_s})",
            )
        return timeout_s

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
                raise SnowflakeConfigError(
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
        action: Callable[[Any], dict[str, Any]],
        verb: str,
    ) -> dict[str, Any]:
        """Resolve the account, open a hardened connection, run the action.

        Error classification is TOPOLOGY-SAFE: auth/connection/permission/
        timeout/warehouse classes return a generic fixed message; only the
        caller's-own-query/object classes carry driver detail
        (connection.classify_snowflake_error). Fresh connection per call.
        """
        if self._app_config_loader is None:
            return self._error(ERROR_NOT_CONFIGURED, f"{self.name} is not ready")
        conn: Any = None
        try:
            config = self._app_config_loader.resolve()
            conn = connection.connect(config, login_timeout_seconds=self._login_timeout_seconds())
            connection.apply_session_hardening(
                conn, statement_timeout_seconds=self._statement_timeout_seconds()
            )
            return self._success(action(conn))
        except Exception as exc:  # our coded guards + any driver fault -> typed
            code, message = self._classify_run_error(exc, verb)
            return self._error(code, message)
        finally:
            if conn is not None:
                conn.close()

    def _classify_run_error(self, exc: Exception, verb: str) -> tuple[str, str]:
        """Map a ``_run_on_connection`` exception to a typed, topology-safe (code, message)."""
        if isinstance(
            exc,
            (
                SnowflakeConfigError,
                StatementGuardError,
                query_actions.ResultTooLargeError,
                export_containment.ExportPathRefusedError,
            ),
        ):
            return exc.code, str(exc)
        if isinstance(exc, ValueError):
            return ERROR_INVALID_PARAMS, str(exc)
        code, message = connection.classify_snowflake_error(exc)
        if self.logger:
            self.logger.warning("%s failed: %s", verb, code)
        return code, message

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "run_query": _edge("run_query", RESULT_TYPE_RUN_QUERY),
            "list_databases": _edge(
                "list_databases", RESULT_TYPE_LIST_DATABASES),
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
        display_name="Snowflake: Run Query",
        description=(
            "Run ONE read-only SQL statement against the configured Snowflake account. Read "
            "leaders only (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH); the operator-granted role is "
            "expected to be read-only. Returns rows inline (up to max_rows, default 200, capped "
            "1000); fails loud with snowflake.result_too_large over the inline caps — use "
            "export_query for bulk."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
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
            lambda conn: query_actions.run_query(conn, params), "run_query"
        )

    @platform_process(
        name="list_databases",
        display_name="Snowflake: List Databases",
        description="List databases visible to the configured role.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="The visible database names."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_databases(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            lambda conn: query_actions.list_databases(conn, params), "list_databases"
        )

    @platform_process(
        name="list_schemas",
        display_name="Snowflake: List Schemas",
        description="List schemas in a database. Requires database.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "database": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The database to list schemas from."
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="The schema names."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_schemas(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            lambda conn: query_actions.list_schemas(conn, params), "list_schemas"
        )

    @platform_process(
        name="list_tables",
        display_name="Snowflake: List Tables",
        description="List tables in a database.schema. Requires database and schema.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "database": ParameterMetadata(type=ParameterType.STRING, required=True, description="The database."),
            "schema": ParameterMetadata(type=ParameterType.STRING, required=True, description="The schema."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="Tables with name and kind."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_tables(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            lambda conn: query_actions.list_tables(conn, params), "list_tables"
        )

    @platform_process(
        name="describe_table",
        display_name="Snowflake: Describe Table",
        description=(
            "Describe a table's columns (name, type, nullability, default). Requires database, "
            "schema, and table."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "database": ParameterMetadata(type=ParameterType.STRING, required=True, description="The database."),
            "schema": ParameterMetadata(type=ParameterType.STRING, required=True, description="The table's schema."),
            "table": ParameterMetadata(type=ParameterType.STRING, required=True, description="The table name."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="Columns with name, type, nullable, and default."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_table(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            lambda conn: query_actions.describe_table(conn, params), "describe_table"
        )

    @platform_process(
        name="export_query",
        display_name="Snowflake: Export Query",
        description=(
            "Run a read-only query and write the full result (up to 50000 rows) as ONE "
            "tab-separated .tsv file at an ABSOLUTE output_tsv_path in the operator's workspace. "
            "The path must lie under an operator-configured export_allowed_roots entry (empty "
            "config refuses every export). Same read-only rules as run_query. Requires sql and "
            "output_tsv_path."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sql": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="A single read-only SQL statement."
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
            lambda conn: query_actions.export_query(conn, params, self._export_path_gate),
            "export_query",
        )

    @platform_process(
        name="test_connection",
        display_name="Snowflake: Test Connection",
        description=(
            "Open the configured Snowflake account and confirm it: account, user, role, warehouse, "
            "and server version. Use this to verify the connector is reachable and the granted role "
            "is what you expect."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="ok, account, user, role, warehouse, version."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run_on_connection(
            lambda conn: query_actions.test_connection(conn, params), "test_connection"
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
