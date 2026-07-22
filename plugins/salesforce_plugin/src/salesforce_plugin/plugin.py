"""Salesforce plugin entry point — SOQL + record CRUD + describe.

Auth (operator-ratified 2026-07-14, full CLI delegation): every verb shells
out to the operator's `sf` CLI — the ``salesforce_org`` address-book entry
names the CLI alias (``target_org``) and the pinned ``instance_host``; the
CLI's keychain-backed refresh token is the durable credential and the
platform stores no Salesforce secret of its own, nor does any access token
ever enter this process. Full read/write including delete_record (record
deletion is an acceptable-loss class, RATIFY-2).

Verbs (all EDGE):
  - soql_query                                      — read (inline; fails loud over the caps)
  - export_soql                                     — read (full result as a workspace TSV,
    absolute output_tsv_path contained under export_allowed_roots; refuse-all when unset)
  - get_record / describe_sobject / list_sobjects   — read
  - create_record / update_record / delete_record   — write
  - test_connection                                 — diagnostic

Security posture (umbrella design §2; process_export deny retired by operator
ruling 2026-07-15 — see
workbench/2026-07-15_result_error_processing_architecture_deep_dive.md): every
verb is directly process_call-able like any other process; auth/session/
permission error messages are GENERIC fixed strings — never the driver
exception, which could leak the org host (§1.6);
the plugin reaches ONLY the address-book-resolved org (no org/domain param on
any verb). There is no retry-on-expiry mechanism — the CLI refreshes its own
credential transparently inside every invocation, so there is no mid-flight
session for this process to detect and re-mint.
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
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from . import export_containment, record_actions, soql_actions
from .app_config import AppConfigError, AppConfigLoader
from .client import SalesforceCliExecutor
from .constants import (
    CONFIG_KEY_API_VERSION,
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    CONFIG_KEY_SF_CLI_PATH,
    DEFAULT_API_VERSION,
    DEFAULT_SF_CLI_PATH,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    PLUGIN_NAME,
    RESULT_TYPE_CREATE_RECORD,
    RESULT_TYPE_DELETE_RECORD,
    RESULT_TYPE_DESCRIBE_SOBJECT,
    RESULT_TYPE_EXPORT_SOQL,
    RESULT_TYPE_GET_RECORD,
    RESULT_TYPE_LIST_SOBJECTS,
    RESULT_TYPE_SOQL_QUERY,
    RESULT_TYPE_TEST_CONNECTION,
    RESULT_TYPE_UPDATE_RECORD,
)
from .errors import SalesforceServiceError, classify_salesforce_error


class SalesforcePlugin(PluginBase, EdgeProcessProvider):
    """Salesforce org connector (SOQL / record CRUD / describe) plugin."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        # PluginBase.__init__ overwrites the class-level ``name`` with the
        # CLASS name ("SalesforcePlugin"); re-assert the entry-point name or
        # config lookups + log lines carry the wrong identity (comfyui precedent).
        self.name = PLUGIN_NAME
        self.logger: logging.Logger | None = None
        self._address_book_service: Any | None = None
        self._app_config_loader: AppConfigLoader | None = None
        self._cli_executor: SalesforceCliExecutor | None = None

    # ------------------------------------------------------------------
    # VaultKeysProvider — no plugin-owned vault keys
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required at readiness.

        The platform stores NO Salesforce secret at all: the durable
        credential is the sf CLI's own keychain-backed refresh token, and no
        access token of any kind ever enters this process (full CLI
        delegation — every verb shells out to `sf`).
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """No scoped vault keys are read or written directly by this plugin."""
        return []

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        self.logger = logging.getLogger(self.name)
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(
                f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires "
                "address_book_service to resolve the salesforce_org credentials"
            )
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        config = self._load_plugin_config()
        api_version = config.get(CONFIG_KEY_API_VERSION, DEFAULT_API_VERSION)
        sf_cli_path = config.get(CONFIG_KEY_SF_CLI_PATH, DEFAULT_SF_CLI_PATH)
        self._cli_executor = SalesforceCliExecutor(
            self._app_config_loader,
            api_version=api_version if isinstance(api_version, str) and api_version else DEFAULT_API_VERSION,
            sf_cli_path=sf_cli_path if isinstance(sf_cli_path, str) and sf_cli_path else DEFAULT_SF_CLI_PATH,
        )
        self.set_ready()

    def _load_plugin_config(self) -> dict[str, object]:
        """Merged plugin config (file overrides + env + CLI) from the config manager.

        Pulled explicitly per the comfyui precedent — the injected
        ``config_provider`` hook has no caller platform-wide, so relying on
        it silently pins every knob to its default.
        """
        config_manager = getattr(self.orchestrator_ref, "config_manager", None)
        if config_manager is None:
            raise RuntimeError(f"{self.name}: config_manager not available on orchestrator")
        result = config_manager.get_plugin_config(self.name)
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_executor(self) -> SalesforceCliExecutor:
        if self._cli_executor is None:
            raise RuntimeError(ERROR_ADDRESS_BOOK_NOT_AVAILABLE)
        return self._cli_executor

    def _export_path_gate(self, output_tsv_path: str) -> str:
        """Admit an export path via workspace-root containment; return the realpath.

        Binds the operator's ``export_allowed_roots`` config (yaml default
        ``[]`` = refuse-all; no hardcoded callsite default per authoring trap
        #10) to the own-copy containment gate. A malformed config value is a
        loud config fault, never a silent admit-all or refuse-all.
        """
        raw_roots = self._load_plugin_config().get(CONFIG_KEY_EXPORT_ALLOWED_ROOTS)
        roots: list[str] = []
        if raw_roots is not None:
            if not isinstance(raw_roots, list) or not all(
                isinstance(entry, str) for entry in raw_roots
            ):
                raise SalesforceServiceError(
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

    def _run(
        self,
        produce: Callable[[SalesforceCliExecutor], dict[str, Any]],
        endpoint_name: str,
    ) -> dict[str, Any]:
        """Shared error-classification path.

        ``produce`` takes the CLI executor and returns the result dict. No
        retry-on-expiry: the `sf` CLI refreshes its own credential
        transparently inside every invocation, so there is no mid-flight
        session for this process to detect and re-mint — a fault here is
        either a classified REST-level rejection or a CLI-level fault
        (``SalesforceServiceError``), never a stale token.
        """
        executor = self._require_executor()
        try:
            data = produce(executor)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except AppConfigError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        except (
            SalesforceServiceError,
            soql_actions.ResultTooLargeError,
            export_containment.ExportPathRefusedError,
        ) as exc:
            return self._error(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 — topology-safety boundary, classified below
            return self._classify_and_log(exc, endpoint_name)
        if self.logger:
            self.logger.debug("%s: success", endpoint_name)
        return self._success(data)

    def _classify_and_log(self, exc: Exception, endpoint_name: str) -> dict[str, Any]:
        code, message = classify_salesforce_error(exc)
        if code == ERROR_API_ERROR and self.logger:
            self.logger.warning("%s: unexpected fault (%s)", endpoint_name, type(exc).__name__)
        return self._error(code, message)

    # ------------------------------------------------------------------
    # EdgeProcessProvider
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "soql_query": _edge(
                "soql_query", RESULT_TYPE_SOQL_QUERY, retryable=True
            ),
            "export_soql": _edge(
                "export_soql", RESULT_TYPE_EXPORT_SOQL, retryable=True
            ),
            "get_record": _edge(
                "get_record", RESULT_TYPE_GET_RECORD, retryable=True
            ),
            "describe_sobject": _edge(
                "describe_sobject",
                RESULT_TYPE_DESCRIBE_SOBJECT,
                retryable=True,
            ),
            "list_sobjects": _edge(
                "list_sobjects", RESULT_TYPE_LIST_SOBJECTS, retryable=True
            ),
            "create_record": _edge(
                "create_record",
                RESULT_TYPE_CREATE_RECORD,
                retryable=False,
            ),
            "update_record": _edge(
                "update_record",
                RESULT_TYPE_UPDATE_RECORD,
                retryable=False,
            ),
            "delete_record": _edge(
                "delete_record",
                RESULT_TYPE_DELETE_RECORD,
                retryable=False,
            ),
            "test_connection": _edge(
                "test_connection",
                RESULT_TYPE_TEST_CONNECTION,
                retryable=True,
            ),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="soql_query",
        display_name="Salesforce: SOQL Query",
        description=(
            "Run a SOQL query (e.g. \"SELECT Id, Name FROM Account WHERE …\") against the "
            "configured Salesforce org. Capped at max_records (default 200, capped 1000) — "
            "never an unbounded fetch. Returns records inline; fails loud with "
            "sf.result_too_large over the inline byte cap — use export_soql for bulk."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "query": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The SOQL query string."
            ),
            "max_records": ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description="Max records to return (default 200, capped at 1000).",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "Records inline (records/total_size/row_count/spilled=false). Fails loud with "
                "sf.result_too_large over the inline byte cap — use export_soql for bulk."
            ),
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def soql_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda executor: soql_actions.soql_query(executor, params),
            "soql_query",
        )

    @platform_process(
        name="export_soql",
        display_name="Salesforce: Export SOQL",
        description=(
            "Run a SOQL query and write the full result (up to 50000 records) as ONE "
            "tab-separated .tsv file at an ABSOLUTE output_tsv_path in the operator's "
            "workspace. The path must lie under an operator-configured export_allowed_roots "
            "entry (empty config refuses every export). Nested relationship objects are "
            "serialized as JSON text in their cells. Requires query and output_tsv_path."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "query": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The SOQL query."
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
            description="path of the written TSV, plus columns, row_count, total_size, and truncated.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def export_soql(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda executor: soql_actions.export_soql(executor, params, self._export_path_gate),
            "export_soql",
        )

    @platform_process(
        name="get_record",
        display_name="Salesforce: Get Record",
        description="Fetch one record by sobject + id, optionally trimmed to specific fields.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Account'."
            ),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The record id."),
            "fields": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description="Optional list of field API names to fetch; all fields if omitted.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="The record's fields."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda executor: record_actions.get_record(executor, params), "get_record")

    @platform_process(
        name="describe_sobject",
        display_name="Salesforce: Describe SObject",
        description="Describe an sobject's fields (name/type/label/nillable/updateable, trimmed).",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Account'."
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="Trimmed field metadata."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_sobject(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda executor: record_actions.describe_sobject(executor, params), "describe_sobject"
        )

    @platform_process(
        name="list_sobjects",
        display_name="Salesforce: List SObjects",
        description="List the org's sobjects (API name + label).",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="sobjects: [{name, label}]."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_sobjects(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda executor: record_actions.list_sobjects(executor, params), "list_sobjects")

    @platform_process(
        name="create_record",
        display_name="Salesforce: Create Record",
        description="Create a record of the given sobject type with the given fields. Write action.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(
                type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Contact'."
            ),
            "fields": ParameterMetadata(
                type=ParameterType.OBJECT, required=True, description="Non-empty object of field values."
            ),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="New record id and success."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def create_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda executor: record_actions.create_record(executor, params), "create_record")

    @platform_process(
        name="update_record",
        display_name="Salesforce: Update Record",
        description="Apply a non-empty fields object to an existing record by sobject + id. Write action.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name."),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The record id."),
            "fields": ParameterMetadata(
                type=ParameterType.OBJECT, required=True, description="Non-empty object of field values to set."
            ),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Confirmation the update was applied."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def update_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda executor: record_actions.update_record(executor, params), "update_record")

    @platform_process(
        name="delete_record",
        display_name="Salesforce: Delete Record",
        description=(
            "Permanently delete a record by sobject + id. This is a destructive write action "
            "taking an EXPLICIT target — sobject and id are required, no bulk form."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name."),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The record id to delete."),
        },
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="Confirmation the record was deleted."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def delete_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._run(lambda executor: record_actions.delete_record(executor, params), "delete_record")

    @platform_process(
        name="test_connection",
        display_name="Salesforce: Test Connection",
        description=(
            "Verify the org binding by querying the org record through the sf CLI. Returns ok, "
            "org_id, the verified username, and the pinned api_version."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT, description="ok, org_id, username, api_version."
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def test_connection(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        def _do(executor: SalesforceCliExecutor) -> dict[str, Any]:
            org_id = soql_actions.fetch_org_id(executor)
            return {
                "ok": True,
                "org_id": org_id,
                "username": executor.username,
                "api_version": executor.api_version,
            }

        return self._run(_do, "test_connection")


def _edge(
    name: str,
    result_type: str,
    *,
    retryable: bool,
) -> EdgeProcessDefinition:
    return EdgeProcessDefinition(
        name=name,
        result_processor_template_customizations=MergeResultProcessorCustomizations(
            result_type=result_type,
        ),
        error_processor_template_customizations=MergeErrorProcessorCustomizations(
            retryable=retryable,
        ),
    )
