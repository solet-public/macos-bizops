"""Org lifecycle over the sf CLI (build 6): scratch orgs from a bound Dev Hub, sandboxes from a bound production org.

Each operation is one documented ``sf`` command (``cli_commands``), or for
``sandbox_status`` one Tooling API query on SandboxProcess:

- ``list``: ``sf org list`` — every org the CLI knows (Dev Hubs, sandboxes,
  scratch orgs, others), each row cut to the fields that describe the org.
- ``display``: ``sf org display`` for a bound org (scratch orgs add their
  status, expiration, edition and Dev Hub), cut the same way.
- ``open``: ``sf org open`` opens the org in this machine's browser; with
  ``url-only`` it returns the single-use frontdoor link the CLI gets from
  ``/services/oauth2/singleaccess`` (open it promptly; it works once).
- ``create_scratch`` (``sf org create scratch --async``, Dev Hub as
  ``--target-dev-hub``; a definition file, or ``definition`` given inline),
  ``resume_scratch`` (``sf org resume scratch --wait 0``: one check, and when
  the org is ready the CLI authorizes it and the plugin binds it) and
  ``delete_scratch``.
- ``create_sandbox`` and ``clone_sandbox`` (``sf org create sandbox --async``,
  which inserts a Tooling SandboxInfo), ``refresh_sandbox`` (``sf org refresh
  sandbox --async``, which updates it), ``resume_sandbox`` (``sf org resume
  sandbox --wait 0``: one SandboxProcess check, and on completion the CLI
  logs in to the sandbox and the plugin binds it), ``sandbox_status`` (Tooling
  query of SandboxProcess by sandbox name) and ``delete_sandbox``.

Creates are always submitted with ``--async`` and resumes check once
(``--wait 0``): a scratch org takes minutes and a sandbox hours, and the
plugin's one job worker must not be held that long, the same reason Bulk
ingest is submitted ``--async``. The caller resumes until the org is ready.
Nothing here classifies an operation as safe or unsafe: deleting a scratch
org or sandbox is first-class, and Salesforce's permissions on the bound user
decide (a Dev Hub user needs scratch org permissions; sandbox work needs the
production org's sandbox licenses and Manage Sandboxes).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import soql_actions
from .cli_commands import BOOLEAN, FILE, INTEGER, TEXT, CliCommand, Flag, ReadGate, check_flags, choice, command_argv
from .client import run_cli_json
from .constants import ERROR_API_ERROR, ERROR_AUTH_FAILED, SF_CLI_LIFECYCLE_TIMEOUT_SECONDS
from .errors import SalesforceCliCallError, SalesforceServiceError, TypedFamilyError
from .org_secrets import secret_name
from .org_service import OrgService, ResolvedOrg, binding_view, describe_cli_failure, public_display
from .orgs import org_selector
from .request_actions import forward

_NO_PROMPT = "the plugin runs the CLI without a terminal, so it answers the CLI's own confirmation with --no-prompt"
_TARGET_ORG = "the bound org named in `org`"
_PRODUCTION = "the bound production org named in `org` (the org that holds the sandbox licenses)"
_ASYNC = "the plugin always submits with --async and finishes with a resume operation, so its one job worker is never held for the minutes or hours the vendor takes"
_WAIT = "the plugin never waits inside the CLI; it submits --async and the resume operation checks once (--wait 0)"
_RESUME_WAIT = "the plugin checks once (--wait 0) so its one job worker is never held; run the resume operation again while the org is not ready"
_SCRATCH_EDITIONS = ("developer", "enterprise", "group", "professional", "partner-developer", "partner-enterprise", "partner-group", "partner-professional")
_SANDBOX_FIELDS = ("Id", "Status", "SandboxName", "SandboxInfoId", "LicenseType", "CreatedDate", "CopyProgress", "SandboxOrganization", "SourceId", "Description", "EndDate", "Features", "SandboxUsername", "Message")
_SCRATCH_INFO_FIELDS = ("Id", "Status", "ScratchOrg", "SignupUsername", "LoginUrl", "ExpirationDate", "Edition", "OrgName", "DurationDays", "Namespace", "ErrorCode", "CreatedDate", "Snapshot", "SourceOrg", "Release")
# `org list` row fields that describe an org; tokens, passwords, client ids and key paths are never returned.
_LIST_FIELDS = (
    "alias",
    "username",
    "orgId",
    "instanceUrl",
    "loginUrl",
    "isDevHub",
    "isSandbox",
    "isScratch",
    "connectedStatus",
    "defaultMarker",
    "isDefaultUsername",
    "isDefaultDevHubUsername",
    "devHubUsername",
    "expirationDate",
    "status",
    "orgName",
    "edition",
    "isExpired",
    "namespace",
    "lastUsed",
    "instanceApiVersion",
    "createdDate",
    "createdBy",
    "signupUsername",
    "tracksSource",
)
_LIST_GROUPS = ("devHubs", "nonScratchOrgs", "sandboxes", "scratchOrgs", "other")
# CLI error names that mean the CLI holds no usable session for the org.
_AUTH_ERRORS = frozenset({"NamedOrgNotFoundError", "NoAuthInfoFound", "RefreshTokenAuthError", "JwtAuthError", "JwtGrantError", "AuthInfoCreationError", "INVALID_SESSION_ID"})

_SANDBOX_CREATE_FLAGS: dict[str, Flag] = {"definition-file": FILE, "set-default": BOOLEAN, "alias": TEXT, "name": TEXT, "no-track-source": BOOLEAN}
_SANDBOX_CREATE_MANAGED: dict[str, str] = {"target-org": _PRODUCTION, "async": _ASYNC, "wait": _WAIT, "poll-interval": _WAIT, "no-prompt": _NO_PROMPT}


@dataclass(frozen=True)
class LifecycleOperation:
    # None: sandbox_status, a Tooling query rather than a CLI command.
    command: CliCommand | None
    # Flags the plugin always sends (managed in the command's table).
    fixed: tuple[str, ...] = ()
    # Caller must give at least one of these flags.
    one_of: tuple[str, ...] = ()


OPERATIONS: dict[str, LifecycleOperation] = {
    "list": LifecycleOperation(CliCommand(("org", "list"), flags={"verbose": BOOLEAN, "all": BOOLEAN, "clean": BOOLEAN, "skip-connection-status": BOOLEAN}, managed={"no-prompt": _NO_PROMPT}), fixed=("--no-prompt",)),
    "display": LifecycleOperation(CliCommand(("org", "display"), flags={"api-version": TEXT, "verbose": BOOLEAN}, managed={"target-org": _TARGET_ORG})),
    "open": LifecycleOperation(CliCommand(("org", "open"), flags={"api-version": TEXT, "private": BOOLEAN, "browser": choice("chrome", "edge", "firefox"), "path": TEXT, "url-only": BOOLEAN, "source-file": FILE}, managed={"target-org": _TARGET_ORG})),
    "create_scratch": LifecycleOperation(
        CliCommand(
            ("org", "create", "scratch"),
            flags={
                "alias": TEXT,
                "set-default": BOOLEAN,
                "definition-file": FILE,
                "no-ancestors": BOOLEAN,
                "edition": choice(*_SCRATCH_EDITIONS),
                "snapshot": TEXT,
                "no-namespace": BOOLEAN,
                "duration-days": INTEGER,
                "api-version": TEXT,
                "client-id": TEXT,
                "track-source": Flag("boolean", negatable=True),
                "username": TEXT,
                "description": TEXT,
                "name": TEXT,
                "release": choice("preview", "previous"),
                "admin-email": TEXT,
                "source-org": TEXT,
            },
            managed={"target-dev-hub": "the bound Dev Hub named in `org`", "async": _ASYNC, "wait": _WAIT},
        ),
        fixed=("--async",),
    ),
    "resume_scratch": LifecycleOperation(CliCommand(("org", "resume", "scratch"), flags={"job-id": TEXT, "use-most-recent": BOOLEAN}, managed={"wait": _RESUME_WAIT}), fixed=("--wait=0",), one_of=("job-id", "use-most-recent")),
    "delete_scratch": LifecycleOperation(CliCommand(("org", "delete", "scratch"), managed={"target-org": "the bound scratch org named in `org`", "no-prompt": _NO_PROMPT}), fixed=("--no-prompt",)),
    "create_sandbox": LifecycleOperation(
        CliCommand(
            ("org", "create", "sandbox"),
            flags={**_SANDBOX_CREATE_FLAGS, "license-type": choice("Developer", "Developer_Pro", "Partial", "Full")},
            managed={**_SANDBOX_CREATE_MANAGED, "source-sandbox-name": "a copy of an existing sandbox is clone_sandbox", "source-id": "a copy of an existing sandbox is clone_sandbox"},
        ),
        fixed=("--async", "--no-prompt"),
    ),
    "clone_sandbox": LifecycleOperation(
        CliCommand(
            ("org", "create", "sandbox"),
            flags={**_SANDBOX_CREATE_FLAGS, "source-sandbox-name": TEXT, "source-id": TEXT},
            managed={**_SANDBOX_CREATE_MANAGED, "license-type": "a clone takes the source sandbox's license; choose a license with create_sandbox"},
        ),
        fixed=("--async", "--no-prompt"),
        one_of=("source-sandbox-name", "source-id"),
    ),
    "refresh_sandbox": LifecycleOperation(
        CliCommand(
            ("org", "refresh", "sandbox"),
            flags={"no-auto-activate": BOOLEAN, "source-sandbox-name": TEXT, "source-id": TEXT, "name": TEXT, "definition-file": FILE},
            managed={"target-org": _PRODUCTION, "async": _ASYNC, "wait": _WAIT, "poll-interval": _WAIT, "no-prompt": _NO_PROMPT},
        ),
        fixed=("--async", "--no-prompt"),
    ),
    "resume_sandbox": LifecycleOperation(CliCommand(("org", "resume", "sandbox"), flags={"name": TEXT, "job-id": TEXT, "use-most-recent": BOOLEAN}, managed={"wait": _RESUME_WAIT, "target-org": _PRODUCTION}), fixed=("--wait=0",), one_of=("name", "job-id", "use-most-recent")),
    "sandbox_status": LifecycleOperation(None),
    "delete_sandbox": LifecycleOperation(CliCommand(("org", "delete", "sandbox"), managed={"target-org": "the bound sandbox named in `org`", "no-prompt": _NO_PROMPT}), fixed=("--no-prompt",)),
}


def operation_names() -> str:
    return ", ".join(OPERATIONS)


def check_lifecycle(params: dict[str, Any]) -> tuple[str, LifecycleOperation, dict[str, object]]:
    """Dispatch-time and worker-time shape checks; no I/O."""
    name = params.get("operation")
    if not isinstance(name, str) or name not in OPERATIONS:
        raise TypedFamilyError(f"operation must be one of: {operation_names()}")
    operation = OPERATIONS[name]
    if operation.command is None:
        if params.get("flags"):
            raise TypedFamilyError("sandbox_status takes no flags; it reads SandboxProcess for sandbox_name")
        soql_actions.sandbox_process_soql(params.get("sandbox_name"))
        return name, operation, {}
    flags = check_flags(operation.command, params.get("flags"))
    if operation.one_of and not any(flag in flags for flag in operation.one_of):
        raise TypedFamilyError(f"{name} needs one of the flags {list(operation.one_of)}")
    _check_extras(name, params, flags)
    org_selector(params)
    return name, operation, flags


def _check_extras(name: str, params: dict[str, Any], flags: dict[str, object]) -> None:
    definition = params.get("definition")
    if definition is not None:
        if name != "create_scratch":
            raise TypedFamilyError("definition (an inline scratch org definition) applies only to create_scratch")
        if not isinstance(definition, dict) or not definition:
            raise TypedFamilyError('definition must be a non-empty scratch org definition object, e.g. {"edition": "Developer", "features": ["EnableSetPasswordInApi"]}')
        if "definition-file" in flags:
            raise TypedFamilyError("give the scratch org definition once: definition (inline) or the definition-file flag")
    secret = params.get("connected_app_secret")
    if secret is not None:
        secret_name(secret, "connected_app_secret")
    if name == "create_scratch" and ("client-id" in flags) != (secret is not None):
        raise TypedFamilyError("client-id and connected_app_secret go together: the CLI asks for the connected app's client secret, which the plugin reads from the vault secret named in connected_app_secret")


def run_lifecycle(service: OrgService, params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
    """Worker entry: run one lifecycle operation and say what changed."""
    name, operation, flags = check_lifecycle(params)
    try:
        return {"operation": name, **_HANDLERS[name](service, name, operation, flags, params, read_gate)}
    except SalesforceCliCallError as exc:
        if exc.error_code in _AUTH_ERRORS:
            raise SalesforceServiceError(ERROR_AUTH_FAILED, f"The sf CLI has no usable session for this org ({exc.error_code}). Log in again with login, then retry.") from None
        raise describe_cli_failure(exc) from None


Handler = Callable[[OrgService, str, LifecycleOperation, dict[str, object], dict[str, Any], ReadGate], dict[str, Any]]


def _argv(operation: LifecycleOperation, flags: dict[str, object], read_gate: ReadGate) -> list[str]:
    if operation.command is None:
        raise TypedFamilyError("this operation runs no sf command")
    return [*command_argv(operation.command, flags, read_gate=read_gate), *operation.fixed]


def _org_list(service: OrgService, _name: str, operation: LifecycleOperation, flags: dict[str, object], _params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
    state = service.state()
    result = run_cli_json(service.sf_cli_path(), _argv(operation, flags, read_gate), label="org list", timeout=SF_CLI_LIFECYCLE_TIMEOUT_SECONDS, credentials=service.all_credentials(state))
    groups = result if isinstance(result, dict) else {}
    return {group: [_pick(row, _LIST_FIELDS) for row in groups.get(group) or [] if isinstance(row, dict)] for group in _LIST_GROUPS}


def _display(service: OrgService, _name: str, operation: LifecycleOperation, flags: dict[str, object], params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
    resolved = service.resolve(params)
    result = resolved.executor.run_json(_argv(operation, flags, read_gate))
    return {"org_display": public_display(result) if isinstance(result, dict) else {}, **_named(resolved)}


def _open(service: OrgService, _name: str, operation: LifecycleOperation, flags: dict[str, object], params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
    resolved = service.resolve(params)
    result = resolved.executor.run_json(_argv(operation, flags, read_gate), timeout=SF_CLI_LIFECYCLE_TIMEOUT_SECONDS)
    url = result.get("url") if isinstance(result, dict) else None
    if flags.get("url-only") is True:
        return {"url": url if isinstance(url, str) else "", "url_note": "A single-use frontdoor link: it logs in once, so open it promptly and do not share it.", **_named(resolved)}
    return {"opened": True, **_named(resolved)}


def _create_scratch(service: OrgService, name: str, operation: LifecycleOperation, flags: dict[str, object], params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
    hub = service.resolve_dev_hub(params)
    secret = params.get("connected_app_secret")
    stdin_text = service.read_secret(str(secret)) + "\n" if isinstance(secret, str) else None
    with _inline_definition(params.get("definition")) as definition_path:
        argv = _argv(operation, flags, read_gate) + ([f"--definition-file={definition_path}"] if definition_path else [])
        try:
            result = hub.executor.run_json(argv, target_flag="--target-dev-hub", timeout=SF_CLI_LIFECYCLE_TIMEOUT_SECONDS, stdin_text=stdin_text)
        except SalesforceCliCallError as exc:
            if stdin_text is None:
                raise
            # The call carried the connected app's client secret: only the error name leaves.
            raise SalesforceServiceError(ERROR_API_ERROR, f"sf org create scratch failed ({exc.error_code or 'unknown'}). The CLI's message is not returned because this call carried a secret from the vault.") from None
    info = _pick(result.get("scratchOrgInfo") if isinstance(result, dict) else None, _SCRATCH_INFO_FIELDS)
    job_id = info.get("Id", "")
    return {
        "job_id": job_id,
        "scratch_org_info": info,
        "warnings": _strings(result.get("warnings") if isinstance(result, dict) else None),
        "next": f'Run org_lifecycle resume_scratch with org {hub.binding.alias!r} and flags {{"job-id": "{job_id}"}} until ready is true; the new org is then bound.',
        **_named(hub),
    }


def _resume_scratch(service: OrgService, _name: str, operation: LifecycleOperation, flags: dict[str, object], params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
    hub = service.resolve_dev_hub(params)
    try:
        result = hub.executor.run_unverified_json(_argv(operation, flags, read_gate), timeout=SF_CLI_LIFECYCLE_TIMEOUT_SECONDS)
    except SalesforceCliCallError as exc:
        if exc.error_code != "StillInProgressError":
            raise
        return {"ready": False, "message": exc.detail_message, "actions": list(exc.actions), **_named(hub)}
    username = result.get("username") if isinstance(result, dict) else None
    info = _pick(result.get("scratchOrgInfo") if isinstance(result, dict) else None, _SCRATCH_INFO_FIELDS)
    if not isinstance(username, str) or not username:
        return {"ready": False, "scratch_org_info": info, "message": "The CLI finished without naming the scratch org's user; nothing was bound.", **_named(hub)}
    binding, state = service.bind(username, alias=_alias(params), pinned_host=None, make_default=params.get("make_default") is True, login_method="scratch_create", jwt_key_secret=hub.binding.jwt_key_secret, dev_hub=hub.binding)
    return {"ready": True, "scratch_org_info": info, "binding": binding_view(binding, state), "warnings": _strings(result.get("warnings") if isinstance(result, dict) else None), "dev_hub": hub.binding.alias, "org": binding.alias, "org_id": binding.org_id}


def _delete_org(service: OrgService, _name: str, operation: LifecycleOperation, flags: dict[str, object], params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
    resolved = service.resolve(params)
    result = resolved.executor.run_json(_argv(operation, flags, read_gate), timeout=SF_CLI_LIFECYCLE_TIMEOUT_SECONDS)
    state, deleted, _legacy_deleted = service.unbind(resolved.binding)
    default = state.default()
    return {"deleted": True, "cli_result": _pick(result, ("username", "orgId")), "binding_removed": True, "secrets_deleted": deleted, "default_org": default.alias if default is not None else "", **_named(resolved)}


def _sandbox_request(service: OrgService, _name: str, operation: LifecycleOperation, flags: dict[str, object], params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
    resolved = service.resolve(params)
    result = resolved.executor.run_json(_argv(operation, flags, read_gate), timeout=SF_CLI_LIFECYCLE_TIMEOUT_SECONDS)
    process = _pick(result, _SANDBOX_FIELDS)
    return {"sandbox_process": process, "next": f'Run org_lifecycle resume_sandbox with org {resolved.binding.alias!r} and flags {{"name": "{process.get("SandboxName", "")}"}} until ready is true; the sandbox is then bound.', **_named(resolved)}


def _resume_sandbox(service: OrgService, _name: str, operation: LifecycleOperation, flags: dict[str, object], params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
    production = service.resolve(params)
    result = production.executor.run_json(_argv(operation, flags, read_gate), timeout=SF_CLI_LIFECYCLE_TIMEOUT_SECONDS, allow_partial=True)
    process = _pick(result, _SANDBOX_FIELDS)
    username = process.get("SandboxUsername")
    if process.get("Status") != "Completed" or "Message" in process or not isinstance(username, str) or not username:
        return {"ready": False, "sandbox_process": process, **_named(production)}
    binding, state = service.bind(username, alias=_alias(params), pinned_host=None, make_default=params.get("make_default") is True, login_method="sandbox_create", jwt_key_secret=production.binding.jwt_key_secret)
    return {"ready": True, "sandbox_process": process, "binding": binding_view(binding, state), "production_org": production.binding.alias, "org": binding.alias, "org_id": binding.org_id}


def _sandbox_status(service: OrgService, _name: str, _operation: LifecycleOperation, _flags: dict[str, object], params: dict[str, Any], _read_gate: ReadGate) -> dict[str, Any]:
    production = service.resolve(params)
    soql = soql_actions.sandbox_process_soql(params.get("sandbox_name"))
    forwarded = forward(production.executor, {"method": "GET", "path": f"/services/data/v{production.executor.api_version}/tooling/query", "query": {"q": soql}, "headers": {"Accept": "application/json"}}, expected_media="json")
    body = forwarded.raw_body
    records = body.get("records") if isinstance(body, dict) else None
    rows = [_pick(row, _SANDBOX_FIELDS) for row in records or [] if isinstance(row, dict)]
    result: dict[str, Any] = {"status": forwarded.raw_result.get("status"), "sandbox_processes": rows, "latest": rows[0] if rows else {}, **_named(production)}
    if not isinstance(records, list):
        result["body"] = body
    return result


_HANDLERS: dict[str, Handler] = {
    "list": _org_list,
    "display": _display,
    "open": _open,
    "create_scratch": _create_scratch,
    "resume_scratch": _resume_scratch,
    "delete_scratch": _delete_org,
    "create_sandbox": _sandbox_request,
    "clone_sandbox": _sandbox_request,
    "refresh_sandbox": _sandbox_request,
    "resume_sandbox": _resume_sandbox,
    "sandbox_status": _sandbox_status,
    "delete_sandbox": _delete_org,
}


@contextmanager
def _inline_definition(definition: object) -> Generator[str | None]:
    """An inline scratch org definition as the temporary file --definition-file reads, removed after."""
    if not isinstance(definition, dict):
        yield None
        return
    descriptor, path = tempfile.mkstemp(suffix=".json", prefix="salesforce_plugin_scratch_def_")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(definition, handle)
        yield path
    finally:
        os.unlink(path)


def _named(resolved: ResolvedOrg) -> dict[str, Any]:
    named: dict[str, Any] = {"org": resolved.binding.alias, "org_id": resolved.binding.org_id}
    if resolved.note:
        named["org_note"] = resolved.note
    return named


def _pick(row: object, fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: row[key] for key in fields if key in row} if isinstance(row, dict) else {}


def _strings(values: object) -> list[str]:
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def _alias(params: dict[str, Any]) -> str | None:
    value = params.get("alias")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TypedFamilyError("alias must be a non-empty string")
    return value.strip()
