"""Salesforce plugin entry point — many bound orgs, SOQL, record CRUD, Bulk v2, typed API families and org lifecycle.

Auth (operator-ratified 2026-07-14, full CLI delegation): every verb shells
out to the operator's `sf` CLI, and the CLI keeps each org's session. Build 6
binds many orgs at once (``orgs.py``, the Google multi-account pattern): a
vault index holds one binding per org (org id, alias, the CLI target and the
pinned ``instance_host``) and the default; every verb that reaches an org
takes an optional ``org`` and every result names the org it ran against. The
single ``salesforce_org`` address-book entry of earlier builds becomes the
first binding on first use. Login secrets the operator stores in the vault
(a JWT key, an SFDX auth URL, an access token) are read only inside the call
that hands them to the CLI (``org_secrets.py``). Full read/write including
delete_record (record deletion is an acceptable-loss class, RATIFY-2).

Verbs (all EDGE; the data/org verbs use the D0.3 deferred-completion shape —
workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md):
every dispatch handler below returns ``{"job_id", "status": "queued"}`` in
milliseconds; ``async_jobs.py``'s single background worker thread does the
real `sf` CLI subprocess spawn off the action-dispatch path and completes the
job. The dispatch handlers never touch ``SalesforceCliExecutor`` directly —
only the worker thread does, preserving the plugin's original effective
concurrency of one.
  - soql_query                                      — read (always writes output_tsv_path)
  - export_soql                                     — read (full result as a workspace TSV,
    absolute output_tsv_path contained under registered path addresses; defaults include solet_home)
  - get_record / describe_sobject / list_sobjects   — read
  - create_record / update_record / delete_record   — write
  - bulk_ingest_submit / bulk_job_*                 — Bulk API 2.0 ingest, hardDelete included
  - composite / tooling / bulk_jobs / reports /     — typed families (typed_families.py):
    dashboards / rest_resources / actions /           reads, writes and deletes alike;
    metadata / content_files                          Salesforce permissions decide each operation;
                                                      metadata and content_files move contained
                                                      local files (transfers.py)
  - authenticated_request                           — any other endpoint
  - test_connection                                 — diagnostic
  - list_orgs / bind_org / login / logout /         — the bound orgs (org_service.py)
    set_default_org / disconnect_org
  - org_lifecycle                                   — scratch orgs from a Dev Hub, sandboxes
                                                      from a production org (org_lifecycle.py)
  - probe_cli / provision_cli                       — local CLI diagnostic and install; no org

Security posture (umbrella design §2; process_export deny retired by operator
ruling 2026-07-15 — see
workbench/2026-07-15_result_error_processing_architecture_deep_dive.md): every
verb is directly process_call-able like any other process; auth/session/
permission error messages are GENERIC fixed strings — never the driver
exception, which could leak the org host (§1.6); a login that carried a vault
secret reports only the CLI's error name. The plugin reaches only bound orgs:
an ``org`` that names no binding is refused, and each binding's executor
verifies once that its CLI target resolves to the binding's pinned host. The
org is resolved on the job worker, never on the dispatch path. There is no
retry-on-expiry mechanism — the CLI refreshes its own credential
transparently inside every invocation, so there is no mid-flight session for
this process to detect and re-mint.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections import deque
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

from . import async_jobs, org_lifecycle, org_service, transfers, typed_families
from .app_config import AppConfigError, AppConfigLoader, SalesforceOrgConfig
from .cli_commands import check_flags
from .client import SalesforceCliExecutor
from .constants import (
    BULK_INGEST_OPERATIONS,
    CONFIG_KEY_API_VERSION,
    CONFIG_KEY_SF_CLI_PATH,
    DEFAULT_API_VERSION,
    DEFAULT_ROW_LIMIT,
    DEFAULT_SF_CLI_PATH,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_API_ERROR,
    ERROR_EXPORT_PATH_REFUSED,
    ERROR_IMPORT_PATH_REFUSED,
    ERROR_INVALID_PARAMS,
    ERROR_NOT_CONFIGURED,
    ERROR_PROVISION_CONSENT_REQUIRED,
    JWT_KEY_DIRECTORY_PARTS,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_INCLUDE_DELETED,
    PARAM_ROW_LIMIT,
    PLUGIN_NAME,
    RESULT_TYPE_ACTIONS,
    RESULT_TYPE_AUTHENTICATED_REQUEST,
    RESULT_TYPE_BIND_ORG,
    RESULT_TYPE_BULK_INGEST_SUBMIT,
    RESULT_TYPE_BULK_JOB_ABORT,
    RESULT_TYPE_BULK_JOB_RESULTS,
    RESULT_TYPE_BULK_JOB_STATUS,
    RESULT_TYPE_BULK_JOBS,
    RESULT_TYPE_COMPOSITE,
    RESULT_TYPE_CONTENT_FILES,
    RESULT_TYPE_CREATE_RECORD,
    RESULT_TYPE_DASHBOARDS,
    RESULT_TYPE_DELETE_RECORD,
    RESULT_TYPE_DESCRIBE_SOBJECT,
    RESULT_TYPE_DISCONNECT_ORG,
    RESULT_TYPE_EXPORT_SOQL,
    RESULT_TYPE_GET_RECORD,
    RESULT_TYPE_LIST_ORGS,
    RESULT_TYPE_LIST_SOBJECTS,
    RESULT_TYPE_LOGIN,
    RESULT_TYPE_LOGOUT,
    RESULT_TYPE_METADATA,
    RESULT_TYPE_ORG_LIFECYCLE,
    RESULT_TYPE_PROBE_CLI,
    RESULT_TYPE_PROVISION_CLI,
    RESULT_TYPE_REPORTS,
    RESULT_TYPE_REST_RESOURCES,
    RESULT_TYPE_SET_DEFAULT_ORG,
    RESULT_TYPE_SOQL_QUERY,
    RESULT_TYPE_TEST_CONNECTION,
    RESULT_TYPE_TOOLING,
    RESULT_TYPE_UPDATE_RECORD,
    SOQL_EXPORT_ROW_CAP,
    SOQL_MAX_RECORDS_CAP,
    VAULT_KEY_ORG_SECRET_PREFIX,
    VAULT_KEY_ORGS_INDEX,
)
from .errors import SalesforceServiceError, TypedFamilyError, classify_salesforce_error
from .org_secrets import OrgSecrets
from .orgs import OrgBinding, OrgIndex, org_selector

_PATH_PARAMETER_TEXT: dict[str, str] = {
    "sobject": "sObject API name in the path, e.g. Account, Invoice__c, Order_Event__e, or a Tooling type such as ApexClass.",
    "external_id_field": "External ID field API name, e.g. External_Id__c.",
    "external_id": "External ID value of the record; percent-encoded into the path.",
    "next_records_url": "nextRecordsUrl from a previous result, followed verbatim: a query page for query_more, or a resultPages response for query_result_pages_more.",
    "id": "15- or 18-character record id.",
    "relationship": "Relationship name to traverse from the record, e.g. Contacts or Owner.",
    "job_id": "Bulk API 2.0 job id from query_create, query_list or bulk_ingest_submit.",
    "report_id": "15- or 18-character report id (00O...).",
    "instance_id": "Asynchronous report instance id from run_async or instances.",
    "report_type": "Report type API name from report_types, e.g. Opportunity or CustomEntity$Invoice__c.",
    "dashboard_id": "15- or 18-character dashboard id (01Z...).",
    "action_type": "Custom invocable action type from list_custom, e.g. apex, flow, quickAction or emailAlert.",
    "action": "Invocable action name, e.g. chatterPost, a flow API name or Account.NewCase.",
    "deploy_request_id": "Metadata deployRequest id (0Af...) from deploy; for deploy_quick, the id of a successful checkOnly validation.",
    "content_version_id": "15- or 18-character ContentVersion id (068...) to download.",
    "content_document_id": "15- or 18-character ContentDocument id (069...); deleting it deletes the file and all its versions.",
}

_LOCAL_PATH_TEXT: dict[str, str] = {
    "zip_path": "ABSOLUTE path of the .zip deploy package (package.xml plus component directories, optional destructiveChanges.xml), contained under a registered path address.",
    "file_path": "ABSOLUTE path of the file to upload as VersionData, contained under a registered path address.",
    "output_path": "ABSOLUTE destination for the downloaded file, contained under a registered path address; its parent directory must exist.",
}

_FAMILY_RETURN = ReturnValueSchema(
    type=ParameterType.OBJECT,
    description="Queue receipt; the vendor outcome follows asynchronously.",
    properties={
        "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
        "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
    },
)

_INCLUDE_DELETED = ParameterMetadata(
    type=ParameterType.BOOLEAN,
    required=False,
    description="true queries through the vendor queryAll resource, which also returns deleted (recycle bin) and archived records. Default false.",
)

# Build 6: every verb that reaches an org takes it (orgs.py).
ORG_PARAM = ParameterMetadata(
    type=ParameterType.STRING,
    required=False,
    description="Which bound org to use: its alias, username or org id. Optional with one bound org; with several, the default org is used. The result names the org (org, org_id). See list_orgs.",
)
_REQUIRED_ORG = ParameterMetadata(type=ParameterType.STRING, required=True, description="The bound org: its alias, username or org id (see list_orgs).")
_MAKE_DEFAULT = ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="true makes this org the default that verbs use when no org is named. The first bound org is always the default.")
_ALIAS = ParameterMetadata(type=ParameterType.STRING, required=False, description="The binding's alias, unique among bound orgs (letters, digits, '_', '.', '@', '+', '-'). Defaults to the CLI alias, else the username.")
_CLI_FLAGS = ParameterMetadata(type=ParameterType.OBJECT, required=False, description="sf CLI flags by their documented names without the leading -- (e.g. instance-url, alias, set-default), with the CLI's own value types. Flags the plugin sets itself are refused with the reason.")


def _with_org(parameters: dict[str, ParameterMetadata]) -> dict[str, ParameterMetadata]:
    return {**parameters, "org": ORG_PARAM}


def _family_parameters(family: str) -> dict[str, ParameterMetadata]:
    """Typed family inputs; typed_families validates them against the vendor shape."""
    parameters = {
        "operation": ParameterMetadata(type=ParameterType.STRING, required=True, description=f"One of: {typed_families.operation_names(family)}."),
        "query": ParameterMetadata(type=ParameterType.OBJECT, required=False, description="Vendor query parameters by their documented names (e.g. q, ids, fields, allOrNone, locator, maxRecords, queryLocator, includeDetails, cloneId, runningUser, filter1, isStickyFilterSave, updateOnly, start, end, limit, explain)."),
        "body": ParameterMetadata(type=ParameterType.OBJECT, required=False, description="Vendor JSON request body for operations that take one, passed unchanged."),
        "headers": ParameterMetadata(type=ParameterType.OBJECT, required=False, description="Optional documented request headers such as Sforce-Query-Options, Sforce-Call-Options or If-Match. Authorization and Host are managed by the CLI."),
    }
    for key in typed_families.path_parameters(family):
        parameters[key] = ParameterMetadata(type=ParameterType.STRING, required=False, description=_PATH_PARAMETER_TEXT[key])
    if typed_families.writes_csv(family):
        parameters["output_csv_path"] = ParameterMetadata(type=ParameterType.STRING, required=False, description="ABSOLUTE .csv destination for query_results, contained under a registered path address.")
    for key in typed_families.local_paths(family):
        parameters[key] = ParameterMetadata(type=ParameterType.STRING, required=False, description=_LOCAL_PATH_TEXT[key])
    return _with_org(parameters)


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
        # The root executor: local CLI concerns (probe, provision) and the
        # settings every per-org executor copies (sf_cli_path, api_version).
        self._cli_executor: SalesforceCliExecutor | None = None
        # Build 6: the vault proxy, one executor per bound org (each verifies
        # its own pin once), and the org service built on first use.
        self._vault_service: Any | None = None
        self._org_executors: dict[tuple[str, str, str, str], SalesforceCliExecutor] = {}
        self._orgs: org_service.OrgService | None = None
        # D0.3 deferred-completion machinery (async_jobs.py) — lazily acquired /
        # started on first async-shaped dispatch, mirroring
        # comfyui_image_generation_plugin's _try_acquire_job_manager (boot order
        # does not guarantee orchestrator_ref.async_job_manager is set yet at
        # prepare_for_readiness time, but it always is by first dispatch).
        self._async_job_manager: Any | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._pending_provision_ids: deque[str] = deque()

    # ------------------------------------------------------------------
    # VaultKeysProvider — the bound-org index and operator-stored org secrets
    # ------------------------------------------------------------------

    def set_vault_service(self, vault_service: object) -> None:
        """The platform injects this plugin's caller-bound vault proxy."""
        self._vault_service = vault_service

    def get_required_vault_keys(self) -> list[str]:
        """No vault keys are required at readiness.

        The org index is created by the first bind or login, and each org
        secret is stored by the operator only for the orgs that log in with
        one. The durable session of every org stays the sf CLI's own.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """The org index and every operator-stored org secret (org_secret__<name>)."""
        return [VAULT_KEY_ORGS_INDEX, VAULT_KEY_ORG_SECRET_PREFIX + "*"]

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")
        self.logger = logging.getLogger(self.name)
        self._address_book_service = self.orchestrator_ref.get_service("address_book_service")
        if self._address_book_service is None:
            raise RuntimeError(f"{ERROR_ADDRESS_BOOK_NOT_AVAILABLE}: {self.name} requires address_book_service to resolve the salesforce_org credentials")
        self._app_config_loader = AppConfigLoader(self._address_book_service)
        config = self._load_plugin_config()
        api_version = config.get(CONFIG_KEY_API_VERSION, DEFAULT_API_VERSION)
        sf_cli_path = config.get(CONFIG_KEY_SF_CLI_PATH, DEFAULT_SF_CLI_PATH)
        self._cli_executor = SalesforceCliExecutor(
            self._app_config_loader,
            api_version=api_version if isinstance(api_version, str) and api_version else DEFAULT_API_VERSION,
            sf_cli_path=sf_cli_path if isinstance(sf_cli_path, str) and sf_cli_path else DEFAULT_SF_CLI_PATH,
        )
        keys_dir = self._jwt_keys_dir()
        if keys_dir is not None:
            # A call killed mid-flight can leave a JWT key file; nothing else may.
            removed = OrgSecrets(self._vault_service, keys_dir).sweep()
            if removed and self.logger:
                self.logger.warning("%s: removed %d JWT key file(s) left by an interrupted call", self.name, removed)
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

    def _jwt_keys_dir(self) -> Path | None:
        app_home = getattr(self.orchestrator_ref, "APP_HOME", None)
        return Path(app_home).joinpath(*JWT_KEY_DIRECTORY_PARTS) if isinstance(app_home, (str, os.PathLike)) else None

    def _org_secrets(self) -> OrgSecrets:
        keys_dir = self._jwt_keys_dir()
        if keys_dir is None:
            raise RuntimeError("orchestrator APP_HOME must be injected before org secrets are used")
        return OrgSecrets(self._vault_service, keys_dir)

    def _org_service(self) -> org_service.OrgService:
        """The bound orgs of this plugin, built on first use on the job worker."""
        if self._orgs is None:
            self._require_executor()
            self._orgs = org_service.OrgService(
                index=OrgIndex(self._vault_service),
                secrets=self._org_secrets(),
                legacy=self._app_config_loader,
                executor_for=self._executor_for,
                legacy_executor=self._legacy_executor,
                sf_cli_path=lambda: self._require_executor().sf_cli_path,
            )
        return self._orgs

    def _executor_for(self, binding: OrgBinding) -> SalesforceCliExecutor:
        """One executor per bound org for this process, so each pin is verified once."""
        key = (binding.org_id, binding.target_org, binding.instance_host, binding.jwt_key_secret)
        executor = self._org_executors.get(key)
        if executor is None:
            root = self._require_executor()
            credential = self._org_secrets().jwt_key_file(binding.jwt_key_secret) if binding.jwt_key_secret else None
            executor = SalesforceCliExecutor(org_service.BindingSource(binding), api_version=root.api_version, sf_cli_path=root.sf_cli_path, credential=credential)
            self._org_executors[key] = executor
        return executor

    def _legacy_executor(self, config: SalesforceOrgConfig) -> SalesforceCliExecutor:
        """The executor of the pre-build-6 salesforce_org entry, verified against its own pin."""
        root = self._require_executor()
        return SalesforceCliExecutor(org_service.FixedSource(config), api_version=root.api_version, sf_cli_path=root.sf_cli_path)

    def _flag_file_gate(self, path: str) -> str:
        """Read containment for a local file a CLI flag names (definition, key or source file)."""
        return self._assert_path(path, suffix=None, param_name="flags", reading=True)

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
            exc.code = ERROR_IMPORT_PATH_REFUSED if reading else ERROR_EXPORT_PATH_REFUSED
            raise

    def _export_path_gate(self, output_tsv_path: str) -> str:
        return self._assert_path(output_tsv_path, suffix=".tsv", param_name="output_tsv_path")

    def _csv_export_path_gate(self, output_csv_path: str) -> str:
        return self._assert_path(output_csv_path, suffix=".csv", param_name="output_csv_path")

    def _import_path_gate(self, csv_path: str) -> str:
        return self._assert_path(csv_path, suffix=".csv", param_name="csv_path", reading=True)

    def _zip_import_path_gate(self, zip_path: str) -> str:
        return self._assert_path(zip_path, suffix=".zip", param_name="zip_path", reading=True)

    def _file_transfer_gates(self) -> transfers.TransferGates:
        return transfers.TransferGates(read=lambda path: self._assert_path(path, suffix=None, param_name="file_path", reading=True), write=lambda path: self._assert_path(path, suffix=None, param_name="output_path"))

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

    def _run(
        self,
        produce: Callable[[], dict[str, Any]],
        endpoint_name: str,
    ) -> dict[str, Any]:
        """Shared error-classification path.

        ``produce`` resolves its org, makes its calls and returns the result
        dict. No retry-on-expiry: the `sf` CLI refreshes its own credential
        transparently inside every invocation, so there is no mid-flight
        session for this process to detect and re-mint — a fault here is
        either a classified REST-level rejection or a CLI-level fault
        (``SalesforceServiceError``), never a stale token. Results and
        messages are returned as received.
        """
        try:
            data = produce()
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        except AppConfigError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))
        except (
            SalesforceServiceError,
            PathRefusedError,
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

    def _dispatch_async(
        self,
        action_name: str,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """D0.3 ms-scale dispatch: create the job, return immediately — no `sf` CLI call here."""
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
            "authenticated_request": _edge("authenticated_request", RESULT_TYPE_AUTHENTICATED_REQUEST, retryable=False),
            "probe_cli": _edge("probe_cli", RESULT_TYPE_PROBE_CLI, retryable=True),
            "provision_cli": _edge("provision_cli", RESULT_TYPE_PROVISION_CLI, retryable=True),
            "soql_query": _edge("soql_query", RESULT_TYPE_SOQL_QUERY, retryable=True),
            "export_soql": _edge("export_soql", RESULT_TYPE_EXPORT_SOQL, retryable=True),
            "get_record": _edge("get_record", RESULT_TYPE_GET_RECORD, retryable=True),
            "describe_sobject": _edge(
                "describe_sobject",
                RESULT_TYPE_DESCRIBE_SOBJECT,
                retryable=True,
            ),
            "list_sobjects": _edge("list_sobjects", RESULT_TYPE_LIST_SOBJECTS, retryable=True),
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
            "bulk_ingest_submit": _edge(
                "bulk_ingest_submit",
                RESULT_TYPE_BULK_INGEST_SUBMIT,
                retryable=False,
            ),
            "bulk_job_status": _edge(
                "bulk_job_status",
                RESULT_TYPE_BULK_JOB_STATUS,
                retryable=True,
            ),
            "bulk_job_results": _edge(
                "bulk_job_results",
                RESULT_TYPE_BULK_JOB_RESULTS,
                retryable=True,
            ),
            "bulk_job_abort": _edge(
                "bulk_job_abort",
                RESULT_TYPE_BULK_JOB_ABORT,
                retryable=False,
            ),
            "composite": _edge("composite", RESULT_TYPE_COMPOSITE, retryable=False),
            "tooling": _edge("tooling", RESULT_TYPE_TOOLING, retryable=False),
            "bulk_jobs": _edge("bulk_jobs", RESULT_TYPE_BULK_JOBS, retryable=False),
            "reports": _edge("reports", RESULT_TYPE_REPORTS, retryable=False),
            "dashboards": _edge("dashboards", RESULT_TYPE_DASHBOARDS, retryable=False),
            "rest_resources": _edge("rest_resources", RESULT_TYPE_REST_RESOURCES, retryable=False),
            "actions": _edge("actions", RESULT_TYPE_ACTIONS, retryable=False),
            "metadata": _edge("metadata", RESULT_TYPE_METADATA, retryable=False),
            "content_files": _edge("content_files", RESULT_TYPE_CONTENT_FILES, retryable=False),
            "list_orgs": _edge("list_orgs", RESULT_TYPE_LIST_ORGS, retryable=True),
            "bind_org": _edge("bind_org", RESULT_TYPE_BIND_ORG, retryable=False),
            "login": _edge("login", RESULT_TYPE_LOGIN, retryable=False),
            "logout": _edge("logout", RESULT_TYPE_LOGOUT, retryable=False),
            "set_default_org": _edge("set_default_org", RESULT_TYPE_SET_DEFAULT_ORG, retryable=False),
            "disconnect_org": _edge("disconnect_org", RESULT_TYPE_DISCONNECT_ORG, retryable=False),
            "org_lifecycle": _edge("org_lifecycle", RESULT_TYPE_ORG_LIFECYCLE, retryable=False),
        }

    # ------------------------------------------------------------------
    # @platform_process implementations
    # ------------------------------------------------------------------

    @platform_process(
        name="authenticated_request",
        display_name="Salesforce: Authenticated Request",
        description=(
            "Send an authenticated request to any relative endpoint on a bound Salesforce org (org). "
            "Supports REST, Tooling, Bulk, Apex REST and SOAP XML bodies, including writes and deletes. "
            "Salesforce credential permissions determine access. Returns a queued job; completion "
            "contains HTTP status, selected headers, JSON/text body and nextRecordsUrl when supplied."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "method": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="HTTP method, e.g. GET, POST, PUT, PATCH or DELETE.",
            ),
            "path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Path relative to the registered instance, e.g. /services/data/v62.0/tooling/query.",
            ),
            "query": ParameterMetadata(
                type=ParameterType.OBJECT,
                required=False,
                description="Optional query parameters; arrays repeat keys.",
            ),
            "headers": ParameterMetadata(
                type=ParameterType.OBJECT,
                required=False,
                description="Optional string HTTP headers, including Content-Type and SOAPAction. Authorization and Host are managed by the CLI.",
            ),
            "body": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Raw JSON, XML, CSV or text body, passed unchanged.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Queue receipt; HTTP outcome follows asynchronously.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def authenticated_request(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("authenticated_request", params, state)

    @platform_process(
        name="probe_cli",
        display_name="Salesforce: Probe CLI",
        description="Verify the local sf executable and read its version without contacting a Salesforce org.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Bounded local CLI verdict: executable_path, version, executable, and absolute binding compliance.",
            properties={
                "executable_path": ParameterMetadata(type=ParameterType.STRING, description="Resolved local sf executable path, or empty."),
                "version": ParameterMetadata(type=ParameterType.STRING, description="Local sf version string, or empty."),
                "executable": ParameterMetadata(type=ParameterType.BOOLEAN, description="True when sf --version completed successfully."),
                "configured": ParameterMetadata(type=ParameterType.BOOLEAN, description="True when sf_cli_path is an absolute path resolving to the verified executable."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def probe_cli(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": self._require_executor().probe_cli(),
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @platform_process(
        name="provision_cli",
        display_name="Salesforce: Provision CLI",
        description=("Queue local sf provisioning after explicit system-change acknowledgement. The existing worker probes and binds the verified executable; read its job result for the provisioning verdict. A compliant executable is never reinstalled; this does not log in to Salesforce."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "acknowledge_system_change": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=True,
                description="Must be true before this process may install sf or update its executable binding.",
            ),
        },
        return_value_schema=_FAMILY_RETURN,
        is_async=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def provision_cli(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Queue provisioning; the existing Salesforce worker probes and binds sf."""
        return self._dispatch_async("provision_cli", params, state)

    def _provision_cli_now(self, params: dict[str, Any]) -> dict[str, Any]:
        """The unchanged consent-gated operation, executed by the job worker."""

        acknowledge = params.get("acknowledge_system_change") is True
        try:
            observed = self._require_executor().provision_cli(acknowledge_system_change=acknowledge)
            executable_path = observed.get("executable_path")
            version = observed.get("version")
            if not isinstance(executable_path, str) or not executable_path.startswith("/"):
                raise RuntimeError("sf provisioner did not return an absolute executable path")
            if not isinstance(version, str) or not version:
                raise RuntimeError("sf provisioner did not return a version")
            if not acknowledge:
                raise SalesforceServiceError(
                    ERROR_PROVISION_CONSENT_REQUIRED,
                    "Explicit acknowledgement is required before the sf executable binding may change.",
                )
            self._bind_sf_cli_path(executable_path)
            return self._success(
                {
                    "provisioned": observed["provisioned"],
                    "reason": observed["reason"],
                    "executable_path": executable_path,
                    "version": version,
                    "configuration_boundary": "config_updated_and_executor_rebound_no_reload_or_restart",
                }
            )
        except SalesforceServiceError as exc:
            return self._error(exc.code, str(exc))
        except RuntimeError as exc:
            return self._error(ERROR_NOT_CONFIGURED, str(exc))

    def _bind_sf_cli_path(self, executable_path: str) -> None:
        """Persist one verified executable path and rebind this plugin immediately."""

        if self.orchestrator_ref is None or self._app_config_loader is None:
            raise RuntimeError("salesforce_plugin is not ready to update its CLI configuration")
        config_manager = getattr(self.orchestrator_ref, "config_manager", None)
        if config_manager is None or not hasattr(config_manager, "save_plugin_config"):
            raise RuntimeError("ordinary plugin config update path is unavailable")
        updated = self._load_plugin_config()
        updated[CONFIG_KEY_SF_CLI_PATH] = executable_path
        if config_manager.save_plugin_config(self.name, updated) is not True:
            raise RuntimeError("ordinary plugin config update path refused the verified sf executable")
        api_version = updated.get(CONFIG_KEY_API_VERSION, DEFAULT_API_VERSION)
        self._cli_executor = SalesforceCliExecutor(
            self._app_config_loader,
            api_version=api_version if isinstance(api_version, str) and api_version else DEFAULT_API_VERSION,
            sf_cli_path=executable_path,
        )
        # Per-org executors carry the old path; each is rebuilt on its next call.
        self._org_executors.clear()

    @platform_process(
        name="soql_query",
        display_name="Salesforce: SOQL Query",
        description=(
            'Run a SOQL query (e.g. "SELECT Id, Name FROM Account WHERE …") against the '
            "bound Salesforce org (org). Returns immediately with a job_id and status 'queued' "
            "(D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the job "
            "finishing. When the job completes, the result is ALWAYS written to the "
            "caller-supplied output_tsv_path, never returned inline — this verb never runs SOQL inside "
            "Apex, so the 50,000-record Apex governor limit does not apply to it; the actual "
            "Salesforce fact for this call path (sf CLI -> jsforce REST client) is a 2,000-record "
            "REST query batch size with no vendor total ceiling, and jsforce's autoFetch already "
            f"pages past that internally. The limit below is entirely our own policy. Defaults to "
            f"{DEFAULT_ROW_LIMIT} records to avoid exhausting API request limits and disk, and to "
            "discourage pulling all records for client-side filtering that a SOQL WHERE clause "
            "should do instead. To fetch more, pass acknowledge_default_limit_override=true "
            f"together with an explicit row_limit (up to {SOQL_MAX_RECORDS_CAP}) — both are "
            f"required together, and a row_limit above {SOQL_MAX_RECORDS_CAP} is refused rather "
            f"than silently clamped. For pulls beyond {SOQL_MAX_RECORDS_CAP} records, use "
            f"export_soql instead (same override mechanism, hard cap {SOQL_EXPORT_ROW_CAP}). When "
            "the goal is validating that records exist or picking one to act on next, prefer "
            "selecting stable ID fields over email addresses or other PII-bearing fields — the "
            "SOQL SELECT list decides what comes back, so a narrower query is both cheaper and "
            "lower-exposure."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "query": ParameterMetadata(type=ParameterType.STRING, required=True, description="The SOQL query string."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=("ABSOLUTE .tsv destination path, contained under a registered path address."),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the default {DEFAULT_ROW_LIMIT} records. Requires understanding why the default exists: avoiding exhausted API request limits/disk, and pulling all records to filter client-side instead of writing a proper SOQL WHERE clause."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(f"Explicit record ceiling, up to {SOQL_MAX_RECORDS_CAP}. Only honored together with acknowledge_default_limit_override=true; refused (not clamped) above {SOQL_MAX_RECORDS_CAP}."),
            ),
            PARAM_INCLUDE_DELETED: _INCLUDE_DELETED,
        },
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
    def soql_query(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("soql_query", params, state)

    @platform_process(
        name="export_soql",
        display_name="Salesforce: Export SOQL",
        description=(
            "The N>>500 route: run a SOQL query and write the result as ONE tab-separated .tsv "
            "file at an ABSOLUTE output_tsv_path in the operator's workspace. Returns immediately "
            "with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch "
            "returning is NOT the same as the job finishing. The path must lie "
            "under an registered path address (register a path address to authorize each "
            "export). Nested relationship objects are serialized as JSON text in their cells. Same "
            "read rules and override mechanism as soql_query, with a higher hard cap: this verb "
            "never runs SOQL inside Apex, so the 50,000-record Apex governor limit does not apply; the "
            "actual Salesforce fact for this call path is a 2,000-record REST query batch size "
            "with no vendor total ceiling, and jsforce's autoFetch already pages past that "
            f"internally via the SF_ORG_MAX_QUERY_LIMIT env override. Defaults to "
            f"{DEFAULT_ROW_LIMIT} records absent an acknowledged override — for that common "
            "small/default case, soql_query has an identical interface with a lower ceiling. To "
            "fetch more, pass acknowledge_default_limit_override=true together with an explicit "
            f"row_limit (up to {SOQL_EXPORT_ROW_CAP}) — both are required together, and a "
            f"row_limit above {SOQL_EXPORT_ROW_CAP} is refused rather than silently clamped. "
            "Requires query and output_tsv_path. When the goal is validating that records exist "
            "rather than inspecting their content, prefer selecting stable ID fields over email "
            "addresses or other PII-bearing fields — the SOQL SELECT list decides what lands in "
            "the file."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "query": ParameterMetadata(type=ParameterType.STRING, required=True, description="The SOQL query."),
            "output_tsv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=("ABSOLUTE .tsv destination path, contained under a registered path address."),
            ),
            PARAM_ACKNOWLEDGE_OVERRIDE: ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    f"Must be exactly true, together with row_limit, to fetch more than the default {DEFAULT_ROW_LIMIT} records. Requires understanding why the default exists: avoiding exhausted API request limits/disk, and pulling all records to filter client-side instead of writing a proper SOQL WHERE clause."
                ),
            ),
            PARAM_ROW_LIMIT: ParameterMetadata(
                type=ParameterType.INTEGER,
                required=False,
                description=(f"Explicit record ceiling, up to {SOQL_EXPORT_ROW_CAP}. Only honored together with acknowledge_default_limit_override=true; refused (not clamped) above {SOQL_EXPORT_ROW_CAP}."),
            ),
            PARAM_INCLUDE_DELETED: _INCLUDE_DELETED,
        },
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
    def export_soql(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("export_soql", params, state)

    @platform_process(
        name="get_record",
        display_name="Salesforce: Get Record",
        description=("Fetch one record by sobject + id, optionally trimmed to specific fields. Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the job finishing; the record's fields are delivered when the job completes."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Account'."),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The record id."),
            "fields": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description="Optional list of field API names to fetch; all fields if omitted.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the record itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def get_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("get_record", params, state)

    @platform_process(
        name="describe_sobject",
        display_name="Salesforce: Describe SObject",
        description=("Describe an sobject's fields (name/type/label/nillable/updateable, trimmed). Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the job finishing; the field metadata is delivered when the job completes."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Account'."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the field metadata itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def describe_sobject(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("describe_sobject", params, state)

    @platform_process(
        name="list_sobjects",
        display_name="Salesforce: List SObjects",
        description=("List the org's sobjects (API name + label). Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the job finishing; the sobject list is delivered when the job completes."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={"org": ORG_PARAM},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the sobject list itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_sobjects(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_sobjects", params, state)

    @platform_process(
        name="create_record",
        display_name="Salesforce: Create Record",
        description=(
            "Create a record of the given sobject type with the given fields. Write action. Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the record being created; the confirmation (new record id) is delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Contact'."),
            "fields": ParameterMetadata(type=ParameterType.OBJECT, required=True, description="Non-empty object of field values."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the new record id itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def create_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("create_record", params, state)

    @platform_process(
        name="update_record",
        display_name="Salesforce: Update Record",
        description=("Apply a non-empty fields object to an existing record by sobject + id. Write action. Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same as the update being applied; the confirmation is delivered when the job completes."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name."),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The record id."),
            "fields": ParameterMetadata(type=ParameterType.OBJECT, required=True, description="Non-empty object of field values to set."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the update confirmation itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def update_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("update_record", params, state)

    @platform_process(
        name="delete_record",
        display_name="Salesforce: Delete Record",
        description=(
            "Permanently delete a record by sobject + id. This is a destructive write action "
            "taking an EXPLICIT target — sobject and id are required, no bulk form. Returns "
            "immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — "
            "the dispatch returning is NOT the same as the record being deleted; the confirmation "
            "is delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name."),
            "id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The record id to delete."),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the delete confirmation itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def delete_record(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("delete_record", params, state)

    @platform_process(
        name="test_connection",
        display_name="Salesforce: Test Connection",
        description=(
            "Verify a bound org (org) by querying its org record through the sf CLI. Returns "
            "immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) — "
            "the dispatch returning is NOT the same as the job finishing; ok, org_id, the "
            "verified username, the pinned api_version and the org's alias are delivered when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={"org": ORG_PARAM},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the connection check itself.",
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
        name="bulk_ingest_submit",
        display_name="Salesforce: Bulk Ingest Submit",
        description=(
            "Submit a Bulk API v2 ingest job (insert, update, upsert, delete, or hardDelete, which "
            "bypasses the recycle bin and needs the vendor's Bulk API Hard Delete permission) from "
            "a CSV file already on disk. Returns immediately with a job_id "
            "and status 'queued' (D0.3 deferred-completion shape) — this is the PLUGIN's own async "
            "job envelope, distinct from the Salesforce Bulk job id delivered when this job "
            "completes. The Salesforce job is submitted with --async and NEVER waited on inline — "
            "a bulk job can run minutes, well past this connector's per-call subprocess bound — so "
            "poll bulk_job_status with the delivered job_id to track it to completion. csv_path "
            "must be an ABSOLUTE path contained under an registered path address "
            "entry (register a path address to authorize each ingest). external_id_field is required when, and "
            "only when, operation is 'upsert'."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "operation": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=(f"One of: {', '.join(sorted(BULK_INGEST_OPERATIONS))}. hardDelete bypasses the recycle bin; Salesforce requires the Bulk API Hard Delete permission for it."),
            ),
            "sobject": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sobject API name, e.g. 'Account'."),
            "csv_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description=("ABSOLUTE path to the ingest CSV, contained under a registered path address."),
            ),
            "external_id_field": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="Required iff operation is 'upsert'; refused for every other operation.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the Salesforce bulk job itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def bulk_ingest_submit(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("bulk_ingest_submit", params, state)

    @platform_process(
        name="bulk_job_status",
        display_name="Salesforce: Bulk Job Status",
        description=(
            "Fetch a Bulk API v2 ingest job's current state and record counts — the poll target "
            "for a job created by bulk_ingest_submit. Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same as "
            "the job finishing; the Salesforce job's state (Open, UploadComplete, InProgress, "
            "JobComplete, Failed, or Aborted) and processed/failed record counts are delivered when "
            "THIS dispatch's job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "job_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The Salesforce Bulk v2 job id, from bulk_ingest_submit's delivered result.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the bulk job's state itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def bulk_job_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("bulk_job_status", params, state)

    @platform_process(
        name="bulk_job_results",
        display_name="Salesforce: Bulk Job Results",
        description=(
            "Fetch a completed Bulk API v2 ingest job's three result CSVs (successful, failed, "
            "unprocessed records) and write each to a caller-supplied ABSOLUTE path, contained "
            "under an registered path address (register a path address to authorize each "
            "write) — never returned inline. Call this only after bulk_job_status reports the job "
            "reached JobComplete (or Failed/Aborted, which can still carry partial results). "
            "Returns immediately with a job_id and status 'queued' (D0.3 deferred-completion shape) "
            "— the three written paths are delivered when this dispatch's job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "job_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The Salesforce Bulk v2 job id, from bulk_ingest_submit's delivered result.",
            ),
            "successful_results_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .csv destination for successfully processed records.",
            ),
            "failed_results_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .csv destination for records that failed processing.",
            ),
            "unprocessed_results_path": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="ABSOLUTE .csv destination for records the job never reached (e.g. an aborted job).",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the written CSV files themselves.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def bulk_job_results(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("bulk_job_results", params, state)

    @platform_process(
        name="bulk_job_abort",
        display_name="Salesforce: Bulk Job Abort",
        description=(
            "Abort a running Bulk API v2 ingest job — the recovery lever for a job submitted in "
            "error or running against bad data. Returns immediately with a job_id and status "
            "'queued' (D0.3 deferred-completion shape) — the dispatch returning is NOT the same as "
            "the abort taking effect; the Salesforce job's resulting state is delivered when this "
            "dispatch's job completes. Records already processed before the abort are NOT rolled "
            "back — check bulk_job_results for what landed."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "org": ORG_PARAM,
            "job_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="The Salesforce Bulk v2 job id, from bulk_ingest_submit's delivered result.",
            ),
        },
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Dispatch envelope — job_id + status: queued. Not the abort confirmation itself.",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING, description="Job ID."),
                "status": ParameterMetadata(type=ParameterType.STRING, description="Always 'queued'."),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def bulk_job_abort(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("bulk_job_abort", params, state)

    def _dispatch_family(self, family: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Reject malformed typed input at dispatch, before a job exists; the worker re-validates."""
        api_version = self._cli_executor.api_version if self._cli_executor is not None else DEFAULT_API_VERSION
        try:
            typed_families.build_request(family, params, api_version)
        except typed_families.TypedFamilyError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        return self._dispatch_async(family, params, state)

    @platform_process(
        name="composite",
        display_name="Salesforce: Composite Resources",
        description=(
            "Run Salesforce REST composite resources on a bound org (org): composite (up to 25 "
            "dependent subrequests with reference ids, allOrNone), graph (composite graph), batch, "
            "tree (sObject tree create), and sObject Collections (collections_create, "
            "collections_retrieve, collections_retrieve_many, collections_update, "
            "collections_upsert, collections_delete). Reads, writes and deletes alike; Salesforce "
            "permissions decide each operation. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_family_parameters("composite"),
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def composite(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_family("composite", params, state)

    @platform_process(
        name="tooling",
        display_name="Salesforce: Tooling API",
        description=(
            "Use the Salesforce Tooling API on a bound org (org): query and query_more, search, "
            "execute_anonymous (anonymous Apex), run_tests_async, run_tests_sync, completions, "
            "list_sobjects, sobject_info, describe, Tooling record create, get, update and delete (ApexClass, "
            "ApexTrigger, TraceFlag and other Tooling types), composite_resources and composite (up to 25 "
            "Tooling subrequests in one call) and apex_compile_results (API 68.0 and later, so set the plugin "
            "api_version to 68.0 or above; empty body). Salesforce permissions decide each "
            "operation, including Apex execution and deletes. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_family_parameters("tooling"),
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def tooling(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_family("tooling", params, state)

    @platform_process(
        name="bulk_jobs",
        display_name="Salesforce: Bulk API 2.0 Jobs",
        description=(
            "Manage Salesforce Bulk API 2.0 jobs on a bound org (org): query jobs (query_create "
            "with operation query or queryAll, query_get, query_list, query_results, query_result_pages, "
            "query_result_pages_more, query_abort, query_delete) and ingest jobs (ingest_list, ingest_delete). query_results writes one "
            "CSV page to an ABSOLUTE output_csv_path under registered path addresses and returns the "
            "next locator; query_result_pages returns up to five page locators for parallel downloads and "
            "query_result_pages_more follows its nextRecordsUrl. "
            "Salesforce permissions decide each operation. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_family_parameters("bulk_jobs"),
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def bulk_jobs(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_family("bulk_jobs", params, state)

    @platform_process(
        name="reports",
        display_name="Salesforce: Reports",
        description=(
            "Use the Salesforce Reports REST API on a bound org (org): list, create, clone, describe, "
            "run (synchronous, includeDetails), run_filtered (with reportMetadata filters), run_async, "
            "instances, instance_results, instance_delete, query (run unsaved metadata), update, delete, report_types and "
            "describe_report_type. Reads, writes and deletes alike; Salesforce permissions decide each "
            "operation. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_family_parameters("reports"),
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def reports(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_family("reports", params, state)

    @platform_process(
        name="dashboards",
        display_name="Salesforce: Dashboards",
        description="Use the Salesforce Dashboards REST API on a bound org (org): list, clone, describe, results, refresh and status (with runningUser and filter1 to filter3), update and delete. Reads, writes and deletes alike; Salesforce permissions decide each operation. Returns a queued job.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_family_parameters("dashboards"),
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def dashboards(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_family("dashboards", params, state)

    @platform_process(
        name="rest_resources",
        display_name="Salesforce: REST Resources",
        description=(
            "Use Salesforce REST API resources beyond record CRUD on a bound org (org): resources, "
            "limits, recent, describe_global, sobject_info, describe, updated and deleted record ids, "
            "get_by_external_id, upsert_by_external_id, delete_by_external_id, relationship traversal, "
            "relationship_update, relationship_delete, query, query_all, query_more, explain, search and "
            "parameterized_search. Reads, writes and "
            "deletes alike; Salesforce permissions decide each operation. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_family_parameters("rest_resources"),
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def rest_resources(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_family("rest_resources", params, state)

    @platform_process(
        name="actions",
        display_name="Salesforce: Invocable Actions",
        description=(
            "Discover and invoke Salesforce invocable actions on a bound org (org): list, "
            "list_standard, list_custom, list_custom_type (apex, flow, quickAction, emailAlert...), "
            "describe_standard, describe_custom, invoke_standard and invoke_custom with an inputs "
            "list. Salesforce permissions decide each invocation. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_family_parameters("actions"),
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def actions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_family("actions", params, state)

    @platform_process(
        name="metadata",
        display_name="Salesforce: Metadata Deploy",
        description=(
            "Deploy Salesforce metadata to a bound org (org) through the Metadata REST API: deploy "
            "(a .zip package at an ABSOLUTE zip_path under registered path addresses, with deployOptions such "
            "as checkOnly, testLevel, runTests, rollbackOnError, purgeOnDelete; destructiveChanges.xml in "
            "the package deletes components), deploy_status (includeDetails), deploy_cancel and "
            "deploy_quick (quick-deploy a validated deployment). Salesforce permissions decide each "
            "deployment. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_family_parameters("metadata"),
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def metadata(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_family("metadata", params, state)

    @platform_process(
        name="content_files",
        display_name="Salesforce: Files (ContentVersion)",
        description=(
            "Move Salesforce Files on a bound org (org): upload (a local file at an ABSOLUTE file_path "
            "under registered path addresses becomes a new ContentVersion; body PathOnClient, Title, "
            "ContentDocumentId for a new version of an existing file, FirstPublishLocationId), download "
            "(a ContentVersion's VersionData to an ABSOLUTE output_path under registered path addresses, kept "
            "only when it matches the version's ContentSize and Checksum) and delete_document (the "
            "ContentDocument and all its versions). Salesforce permissions decide each operation. Returns a "
            "queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters=_family_parameters("content_files"),
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def content_files(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_family("content_files", params, state)

    # ------------------------------------------------------------------
    # Build 6: bound orgs and the org lifecycle
    # ------------------------------------------------------------------

    def _dispatch_checked(self, action_name: str, params: dict[str, Any], state: dict[str, Any], check: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
        """Refuse a malformed request before a job exists; the worker checks again with I/O."""
        try:
            check(params)
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        return self._dispatch_async(action_name, params, state)

    @platform_process(
        name="list_orgs",
        display_name="Salesforce: List Bound Orgs",
        description=("List every Salesforce org bound to this solet: alias, org id, username, pinned instance host, type (production, sandbox or scratch), Dev Hub flag, login method, the default flag and the names (never values) of the vault secrets its login uses. Reads the vault index only. Returns a queued job."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_orgs(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_async("list_orgs", params, state)

    @platform_process(
        name="bind_org",
        display_name="Salesforce: Bind Org",
        description=(
            "Bind an org the sf CLI is already logged in to, so verbs can name it with org. The binding is made from what the CLI and the org report "
            "(org display: org id, username, instance host; an Organization query: sandbox or not; the CLI's Dev Hub test), keyed by org id with a unique alias. "
            "Binding an org again refreshes it. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "target_org": ParameterMetadata(type=ParameterType.STRING, required=True, description="The sf CLI alias or username of a logged-in org (see org_lifecycle list)."),
            "alias": _ALIAS,
            "instance_host": ParameterMetadata(type=ParameterType.STRING, required=False, description="Optional expected my-domain host; binding is refused when the CLI's org resolves elsewhere."),
            "make_default": _MAKE_DEFAULT,
        },
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def bind_org(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        def check(checked: dict[str, Any]) -> None:
            target = checked.get("target_org")
            if not isinstance(target, str) or not target.strip():
                raise TypedFamilyError("target_org must be the sf CLI alias or username of an org the CLI is logged in to")

        return self._dispatch_checked("bind_org", params, state, check)

    @platform_process(
        name="login",
        display_name="Salesforce: Log In To An Org",
        description=(
            "Log the sf CLI in to an org and bind it. method web opens the browser for the operator (up to 5 minutes); jwt logs in unattended with a connected app's "
            "certificate key, read from the vault secret named in jwt_key_secret (or the jwt-key-file flag); sfdx_url uses an SFDX auth URL from the vault "
            "(sfdx_auth_url_secret) or the sfdx-url-file flag; access_token uses a token from the vault (access_token_secret). Every documented flag of the CLI's "
            "login command is accepted in flags. Secrets are stored by the operator with vault_service store_from_file under <solet>.salesforce_plugin.org_secret__<name> "
            "and are never parameters, results or log lines. Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "method": ParameterMetadata(type=ParameterType.STRING, required=True, description=f"One of: {', '.join(org_service.LOGIN_COMMANDS)}."),
            "flags": _CLI_FLAGS,
            "alias": _ALIAS,
            "make_default": _MAKE_DEFAULT,
            "jwt_key_secret": ParameterMetadata(type=ParameterType.STRING, required=False, description="jwt: name of the vault secret holding the private key (the part after org_secret__)."),
            "sfdx_auth_url_secret": ParameterMetadata(type=ParameterType.STRING, required=False, description="sfdx_url: name of the vault secret holding the force:// SFDX auth URL."),
            "access_token_secret": ParameterMetadata(type=ParameterType.STRING, required=False, description="access_token: name of the vault secret holding the access token."),
        },
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def login(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_checked("login", params, state, org_service.check_login)

    @platform_process(
        name="logout",
        display_name="Salesforce: Log Out Of An Org",
        description="Log the sf CLI out of one bound org (sf org logout); the binding stays, dormant, until the org is logged in to again. flags takes the CLI's client-app. Returns a queued job.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={"org": _REQUIRED_ORG, "flags": _CLI_FLAGS},
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def logout(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        def check(checked: dict[str, Any]) -> None:
            if org_selector(checked) is None:
                raise TypedFamilyError("org is required: the alias, username or org id of a bound org")
            check_flags(org_service.LOGOUT_COMMAND, checked.get("flags"))

        return self._dispatch_checked("logout", params, state, check)

    @platform_process(
        name="set_default_org",
        display_name="Salesforce: Set Default Org",
        description="Make one bound org (alias, username or org id) the default that verbs use when no org is named. Returns a queued job.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={"org": _REQUIRED_ORG},
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def set_default_org(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_checked("set_default_org", params, state, _require_org)

    @platform_process(
        name="disconnect_org",
        display_name="Salesforce: Disconnect Org",
        description=("Disconnect one bound org: log the sf CLI out of it, remove its binding and delete the vault secrets no other bound org uses. Other bound orgs are untouched; if it was the default, the next bound org becomes the default. Returns a queued job."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={"org": _REQUIRED_ORG},
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def disconnect_org(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_checked("disconnect_org", params, state, _require_org)

    @platform_process(
        name="org_lifecycle",
        display_name="Salesforce: Org Lifecycle",
        description=(
            "Manage org lifecycles through the sf CLI: list (every org the CLI knows), display, open (browser, or url-only for a single-use link), "
            "create_scratch (from a bound Dev Hub; definition-file or inline definition), resume_scratch (binds the scratch org once ready), delete_scratch, "
            "create_sandbox, clone_sandbox, refresh_sandbox (from a bound production org), resume_sandbox (binds the sandbox once ready), sandbox_status "
            "(Tooling SandboxProcess) and delete_sandbox. Creates are submitted --async and resumes check once. Every documented flag of each command is accepted in flags. "
            "Returns a queued job."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "operation": ParameterMetadata(type=ParameterType.STRING, required=True, description=f"One of: {org_lifecycle.operation_names()}."),
            "org": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description="The bound org the command runs against: the org itself (display, open, delete_*), the Dev Hub (create_scratch, resume_scratch) or the production org (sandbox operations). Defaults as for every verb; scratch operations default to the only bound Dev Hub.",
            ),
            "flags": _CLI_FLAGS,
            "definition": ParameterMetadata(type=ParameterType.OBJECT, required=False, description="create_scratch: an inline scratch org definition (the JSON a definition file holds), instead of the definition-file flag."),
            "sandbox_name": ParameterMetadata(type=ParameterType.STRING, required=False, description="sandbox_status: the sandbox name (a letter and up to 9 letters or digits)."),
            "connected_app_secret": ParameterMetadata(type=ParameterType.STRING, required=False, description="create_scratch with the client-id flag: name of the vault secret holding the connected app's client secret."),
            "alias": _ALIAS,
            "make_default": _MAKE_DEFAULT,
        },
        return_value_schema=_FAMILY_RETURN,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def org_lifecycle(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_checked("org_lifecycle", params, state, org_lifecycle.check_lifecycle)


def _require_org(params: dict[str, Any]) -> None:
    if org_selector(params) is None:
        raise TypedFamilyError("org is required: the alias, username or org id of a bound org (see list_orgs)")


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
