"""Bound orgs at run time: which org a call runs as, and binding, login, logout and disconnect.

Runs only on the plugin's job worker (``async_jobs``), never on the dispatch
path: resolving an org reads the vault and can call the sf CLI (verifying a
binding, migrating the pre-build-6 entry), and the dispatch path stays a
millisecond job write.

A binding is made from what the CLI and the org themselves report, never from
what a caller asserts: ``org display`` gives the org id, username and instance
URL (the instance host becomes the binding's pin; a caller-supplied
``instance_host`` must equal it), one Organization query says whether the org
is a sandbox, and the CLI's own Dev Hub test (``SELECT Id FROM ScratchOrgInfo``)
says whether it is a Dev Hub. A scratch org is recognised by the Dev Hub the
CLI records for it.

Login uses the CLI's documented commands (``org login web``, ``jwt``,
``sfdx-url`` and ``access-token``) with every documented flag; the secrets they
need come from the vault as ``org_secrets`` describes. When the plugin itself
supplied a secret to a login, a failure is reported by the CLI's error name
and a fixed hint, never by the CLI's message text, so no secret can travel
back out through an error.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

from . import soql_actions
from .app_config import AppConfigLoader, SalesforceOrgConfig
from .cli_commands import BOOLEAN, FILE, TEXT, CliCommand, Flag, ReadGate, check_flags, choice, command_argv
from .client import CallCredential, SalesforceCliExecutor, run_cli_json
from .constants import ERROR_API_ERROR, ERROR_AUTH_FAILED, ERROR_NOT_CONFIGURED, ERROR_UNKNOWN_ORG, SF_CLI_TIMEOUT_SECONDS, SF_CLI_WEB_LOGIN_TIMEOUT_SECONDS
from .errors import SalesforceCliCallError, SalesforceServiceError, TypedFamilyError
from .org_secrets import OrgSecrets, secret_name
from .orgs import OrgBinding, OrgIndex, OrgIndexState, org_selector

_ORG_ID = re.compile(r"00D[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}")
_NO_PROMPT = "the plugin runs the CLI without a terminal, so it answers the CLI's own confirmation with --no-prompt"

# `org display` fields that describe the org; the access token, password,
# SFDX auth URL and client id of the auth envelope are never returned.
DISPLAY_FIELDS: tuple[str, ...] = ("id", "username", "alias", "instanceUrl", "apiVersion", "connectedStatus", "devHubId", "status", "expirationDate", "createdBy", "createdDate", "edition", "orgName", "namespace", "signupUsername")

LOGIN_COMMANDS: dict[str, CliCommand] = {
    "web": CliCommand(
        ("org", "login", "web"),
        flags={"browser": choice("chrome", "edge", "firefox"), "client-id": TEXT, "instance-url": TEXT, "set-default-dev-hub": BOOLEAN, "set-default": BOOLEAN, "alias": TEXT, "client-app": TEXT, "username": TEXT, "scopes": TEXT},
        managed={"no-prompt": _NO_PROMPT},
    ),
    "jwt": CliCommand(
        ("org", "login", "jwt"),
        flags={"username": Flag("text", required=True), "jwt-key-file": FILE, "client-id": Flag("text", required=True), "instance-url": TEXT, "set-default-dev-hub": BOOLEAN, "set-default": BOOLEAN, "alias": TEXT},
        managed={"no-prompt": _NO_PROMPT},
    ),
    "sfdx_url": CliCommand(
        ("org", "login", "sfdx-url"),
        flags={"sfdx-url-file": FILE, "set-default-dev-hub": BOOLEAN, "set-default": BOOLEAN, "alias": TEXT},
        managed={"sfdx-url-stdin": "the plugin writes the SFDX auth URL from the vault secret named in sfdx_auth_url_secret to the CLI's standard input", "no-prompt": _NO_PROMPT},
    ),
    "access_token": CliCommand(
        ("org", "login", "access-token"),
        flags={"instance-url": Flag("text", required=True), "set-default-dev-hub": BOOLEAN, "set-default": BOOLEAN, "alias": TEXT},
        managed={"no-prompt": "the plugin passes the token from the vault secret named in access_token_secret in SF_ACCESS_TOKEN, which the CLI reads only with --no-prompt"},
    ),
}
LOGOUT_COMMAND = CliCommand(
    ("org", "logout"),
    flags={"client-app": TEXT},
    managed={
        "target-org": "the bound org named in `org`",
        "all": "the plugin acts only on bound orgs; logging the CLI out of every org would also end sessions the solet does not own",
        "no-prompt": _NO_PROMPT,
    },
)
# Per-method fixed hints for a login that carried a vault secret.
_LOGIN_HINTS: dict[str, str] = {
    "jwt": "Check the connected app's consumer key (client-id), that the user is pre-authorized for the app, that the app's certificate matches the stored key, and that instance-url is the org's login URL.",
    "sfdx_url": "Check that the stored value is an SFDX auth URL of the form force://<clientId>:<clientSecret>:<refreshToken>@<instanceUrl> and that its refresh token is still valid.",
    "access_token": "Check that the stored access token is current (it expires with the session) and that instance-url is the org's instance URL.",
}


@dataclass(frozen=True)
class BindingSource:
    """The executor source for one binding."""

    binding: OrgBinding

    def load(self) -> SalesforceOrgConfig:
        return SalesforceOrgConfig(target_org=self.binding.target_org, instance_host=self.binding.instance_host)


@dataclass(frozen=True)
class FixedSource:
    """The executor source for the pre-build-6 entry while it is migrated."""

    config: SalesforceOrgConfig

    def load(self) -> SalesforceOrgConfig:
        return self.config


@dataclass(frozen=True)
class _LoginFacts:
    """How a binding logged in: what a refresh keeps and a new login replaces."""

    login_method: str
    jwt_key_secret: str = ""
    secret_names: tuple[str, ...] = ()
    dev_hub_org_id: str = ""

    def against(self, existing: OrgBinding | None) -> _LoginFacts:
        """These facts for an org that may already be bound: a refresh keeps the binding's, a login merges secret names."""
        if existing is None:
            return self
        if self.login_method == "cli":
            return _LoginFacts(existing.login_method, existing.jwt_key_secret, existing.secret_names, existing.dev_hub_org_id)
        names = tuple(dict.fromkeys((*existing.secret_names, *self.secret_names)))
        return replace(self, secret_names=names, dev_hub_org_id=self.dev_hub_org_id or existing.dev_hub_org_id)


@dataclass(frozen=True)
class ResolvedOrg:
    binding: OrgBinding
    executor: SalesforceCliExecutor
    note: str = ""


def _chosen_alias(alias: str | None, existing: OrgBinding | None, display: dict[str, Any]) -> str:
    """The caller's alias, else the existing binding's, else the CLI alias, else the username."""
    return alias or (existing.alias if existing is not None else "") or str(display.get("alias") or "") or str(display.get("username") or "")


def public_display(result: dict[str, Any]) -> dict[str, Any]:
    """The org-describing fields of an `org display` result, nothing from the auth envelope."""
    return {key: result[key] for key in DISPLAY_FIELDS if key in result}


def binding_view(binding: OrgBinding, state: OrgIndexState) -> dict[str, Any]:
    """A binding as results show it: public facts plus the names (never values) of its vault secrets."""
    return {**binding.as_record(), "default": binding.org_id == state.default_org_id}


def instance_host(instance_url: object) -> str:
    try:
        return (urlparse(instance_url).hostname or "").lower() if isinstance(instance_url, str) else ""
    except ValueError:
        return ""


class OrgService:
    """The bound orgs of one plugin instance, used on the job worker."""

    def __init__(
        self,
        *,
        index: OrgIndex,
        secrets: OrgSecrets,
        legacy: AppConfigLoader | None,
        executor_for: Callable[[OrgBinding], SalesforceCliExecutor],
        legacy_executor: Callable[[SalesforceOrgConfig], SalesforceCliExecutor],
        sf_cli_path: Callable[[], str],
    ) -> None:
        self._index = index
        self._secrets = secrets
        self._legacy = legacy
        self._executor_for = executor_for
        self._legacy_executor = legacy_executor
        self._sf_cli_path = sf_cli_path

    # -- resolution ------------------------------------------------------------

    def state(self) -> OrgIndexState:
        return self._index.load()

    def sf_cli_path(self) -> str:
        return self._sf_cli_path()

    def read_secret(self, name: str) -> str:
        """One org secret's value, for the call that hands it to the CLI."""
        return self._secrets.read(name)

    def resolve(self, params: dict[str, Any]) -> ResolvedOrg:
        """The bound org a call runs as; migrates the pre-build-6 entry the first time."""
        selector = org_selector(params)
        state, note = self._with_legacy_migrated(self._index.load())
        binding = state.select(selector)
        return ResolvedOrg(binding=binding, executor=self._executor_for(binding), note=note)

    def resolve_dev_hub(self, params: dict[str, Any]) -> ResolvedOrg:
        """The Dev Hub a scratch org call uses: the named org, the only bound Dev Hub, or the default when it is one."""
        selector = org_selector(params)
        state, note = self._with_legacy_migrated(self._index.load())
        if selector is not None:
            binding = state.select(selector)
        else:
            hubs = tuple(candidate for candidate in state.bindings if candidate.is_dev_hub)
            default = state.default()
            if len(hubs) == 1:
                binding = hubs[0]
            elif default is not None and default.is_dev_hub:
                binding = default
            else:
                raise SalesforceServiceError(ERROR_UNKNOWN_ORG, f"Name the Dev Hub as `org`. Bound Dev Hubs: {state.describe(hubs)}. Bind one with bind_org or login if none is listed.")
        return ResolvedOrg(binding=binding, executor=self._executor_for(binding), note=note)

    def legacy_pending(self, state: OrgIndexState) -> tuple[bool | None, str]:
        """Whether the pre-build-6 entry still awaits migration; None with an advisory when unreadable."""
        if self._legacy is None or any(binding.migrated_from_legacy for binding in state.bindings):
            return False, ""
        try:
            return self._legacy.find() is not None, ""
        except Exception as exc:  # noqa: BLE001 — an unreadable address book is reported, never taken as absence
            return None, f"The salesforce_org address-book entry could not be read ({getattr(exc, 'code', '') or type(exc).__name__}); whether it awaits migration is unknown."

    def _with_legacy_migrated(self, state: OrgIndexState) -> tuple[OrgIndexState, str]:
        if self._legacy is None or any(binding.migrated_from_legacy for binding in state.bindings):
            return state, ""
        try:
            config = self._legacy.find()
            if config is None:
                return state, ""
            return self._migrate(state, config)
        except Exception as exc:  # noqa: BLE001 — with other orgs bound, a failed migration is an advisory (rul_becb4bed)
            if not state.bindings:
                raise
            code = getattr(exc, "code", "") or type(exc).__name__
            # A local name clash (TypedFamilyError) carries no vendor text; its message says the way out.
            why = f": {exc}" if isinstance(exc, TypedFamilyError) else ""
            return state, f"The salesforce_org address-book entry could not be migrated to a binding ({code}{why}); it is kept unchanged and the migration is retried on the next call."

    def _migrate(self, state: OrgIndexState, config: SalesforceOrgConfig) -> tuple[OrgIndexState, str]:
        executor = self._legacy_executor(config)
        display = executor.display()
        org_id = str(display.get("id") or "")
        existing = next((binding for binding in state.bindings if binding.org_id == org_id), None)
        if existing is not None:
            migrated = replace(existing, migrated_from_legacy=True)
        else:
            alias = config.target_org if state.find(config.target_org) is None else str(display.get("username") or config.target_org)
            migrated = self._binding_from(display, executor, alias=alias, target_org=config.target_org, facts=_LoginFacts("cli"))
            migrated = replace(migrated, migrated_from_legacy=True)
        new_state = state.with_binding(migrated, make_default=not state.default_org_id)
        self._index.save(new_state)
        role = "It is the default org." if new_state.default_org_id == migrated.org_id else "It is not the default; run set_default_org to make it one."
        return new_state, f"Bound the org registered in the salesforce_org address-book entry as {migrated.alias} ({migrated.org_id}). The entry was copied, not moved; it is deleted only when this org is disconnected. {role}"

    # -- binding -----------------------------------------------------------------

    def bind(
        self,
        target_org: str,
        *,
        alias: str | None,
        pinned_host: str | None,
        make_default: bool,
        login_method: str,
        jwt_key_secret: str = "",
        secret_names: tuple[str, ...] = (),
        dev_hub: OrgBinding | None = None,
    ) -> tuple[OrgBinding, OrgIndexState]:
        """Bind (or refresh) the org the CLI knows as ``target_org``; returns the binding and the new state.

        The existing binding is the one with the org id the CLI reports (see
        ``_identify``). Binding an already-bound org again (``login_method``
        cli) refreshes its facts and keeps how it logged in (its JWT key,
        secrets and Dev Hub), so the CLI can still renew its session; a new
        login replaces the method and key but keeps the earlier secret names,
        so disconnecting the org still deletes them. Without ``alias`` an
        existing binding keeps its own.
        """
        state = self._index.load()
        display, observed, existing = self._identify(state, target_org, pinned_host, jwt_key_secret)
        facts = _LoginFacts(login_method, jwt_key_secret, secret_names, dev_hub.org_id if dev_hub is not None else "").against(existing)
        username = str(display.get("username") or "")
        provisional = OrgBinding(org_id=str(display.get("id") or ""), alias=alias or username, target_org=username or target_org, username=username, instance_host=observed, jwt_key_secret=facts.jwt_key_secret)
        binding = self._binding_from(display, self._executor_for(provisional), alias=_chosen_alias(alias, existing, display), target_org=username or target_org, facts=facts)
        if existing is not None:
            binding = replace(binding, migrated_from_legacy=existing.migrated_from_legacy)
        new_state = state.with_binding(binding, make_default=make_default)
        self._index.save(new_state)
        return binding, new_state

    def _identify(self, state: OrgIndexState, target_org: str, pinned_host: str | None, jwt_key_secret: str) -> tuple[dict[str, Any], str, OrgBinding | None]:
        """What the CLI reports for ``target_org``, its instance host, and the binding with that org id.

        ``target_org`` is whatever name the CLI knows the org by and is never
        matched against binding names. Every bound JWT key, and the key this
        call supplies, is offered to the display because the org is not known
        until the CLI answers (as ``org list`` does).
        """
        credentials = self._credentials_for(jwt_key_secret, *(bound.jwt_key_secret for bound in state.bindings))
        display, observed = self._display_unbound(target_org, pinned_host, credentials)
        org_id = str(display.get("id") or "")
        return display, observed, next((bound for bound in state.bindings if bound.org_id == org_id), None)

    def _display_unbound(self, target_org: str, pinned_host: str | None, credentials: tuple[CallCredential, ...]) -> tuple[dict[str, Any], str]:
        """`org display` for an org not identified yet, and its instance host (which must equal a given pin)."""
        try:
            display = run_cli_json(self._sf_cli_path(), ["org", "display", "--target-org", target_org], label=target_org, credentials=credentials)
        except SalesforceCliCallError as exc:
            raise SalesforceServiceError(ERROR_AUTH_FAILED, f"The sf CLI has no usable session for {target_org!r} ({exc.error_code or 'unknown'}); log in to it first with login, or run: sf org login web --alias {target_org}") from None
        if not isinstance(display, dict):
            raise SalesforceCliCallError("", "sf CLI org display returned an unexpected result shape")
        observed = instance_host(display.get("instanceUrl"))
        if pinned_host and pinned_host.strip().lower() != observed:
            raise SalesforceServiceError(ERROR_NOT_CONFIGURED, f"sf CLI org {target_org!r} resolves to instance host {observed!r}, not the instance_host {pinned_host.strip().lower()!r} given; refusing to bind it.")
        return display, observed

    def _binding_from(self, display: dict[str, Any], executor: SalesforceCliExecutor, *, alias: str, target_org: str, facts: _LoginFacts) -> OrgBinding:
        executor.prime_verification(display)
        org_id = str(display.get("id") or "")
        if _ORG_ID.fullmatch(org_id) is None:
            raise SalesforceCliCallError("", "sf CLI org display returned no org id")
        if _ALIAS.fullmatch(alias) is None:
            raise TypedFamilyError(f"alias {alias!r} must start with a letter or digit and use letters, digits, '_', '.', '@', '+' or '-' (at most 80 characters)")
        is_scratch = bool(display.get("devHubId"))
        organization = soql_actions.org_facts(executor)
        return OrgBinding(
            org_id=org_id,
            alias=alias,
            target_org=target_org,
            username=str(display.get("username") or ""),
            instance_host=instance_host(display.get("instanceUrl")),
            org_type="scratch" if is_scratch else ("sandbox" if organization["is_sandbox"] else "production"),
            is_dev_hub=False if is_scratch else soql_actions.is_dev_hub(executor),
            login_method=facts.login_method,
            dev_hub_org_id=facts.dev_hub_org_id,
            jwt_key_secret=facts.jwt_key_secret,
            secret_names=facts.secret_names,
        )

    def unbind(self, binding: OrgBinding) -> tuple[OrgIndexState, list[str], bool]:
        """Remove one binding: (new state, secrets deleted, legacy entry deleted).

        A secret is deleted only when no remaining binding uses it (a scratch
        org created from a JWT Dev Hub uses the hub's key).
        """
        state, removed, orphaned = self._index.load().without(binding.org_id)
        self._index.save(state)
        deleted = [name for name in orphaned if self._secrets.delete(name)]
        legacy_deleted = False
        if removed.migrated_from_legacy and self._legacy is not None and self._legacy.find() is not None:
            self._legacy.delete()
            legacy_deleted = True
        return state, deleted, legacy_deleted

    def _credentials_for(self, *secret_names: str) -> tuple[CallCredential, ...]:
        """Keys offered to a call that may reach any of these orgs; one the vault lacks is left out."""
        return tuple(self._secrets.jwt_key_file(name, offered=True) for name in dict.fromkeys(secret_names) if name)

    def all_credentials(self, state: OrgIndexState) -> tuple[CallCredential, ...]:
        """Every bound JWT key, for a call that may touch any bound org (org list)."""
        return self._credentials_for(*(binding.jwt_key_secret for binding in state.bindings))

    # -- verbs -------------------------------------------------------------------

    def list_orgs(self) -> dict[str, Any]:
        state = self._index.load()
        default = state.default()
        pending, advisory = self.legacy_pending(state)
        result: dict[str, Any] = {
            "orgs": [binding_view(binding, state) for binding in state.bindings],
            "count": len(state.bindings),
            "default_org": default.alias if default is not None else "",
            "legacy_pending_migration": pending,
        }
        if advisory:
            result["advisories"] = [advisory]
        return result

    def bind_org(self, params: dict[str, Any]) -> dict[str, Any]:
        target = params.get("target_org")
        if not isinstance(target, str) or not target.strip():
            raise TypedFamilyError("target_org must be the sf CLI alias or username of an org the CLI is logged in to")
        binding, state = self.bind(target.strip(), alias=_optional_text(params, "alias"), pinned_host=_optional_text(params, "instance_host"), make_default=params.get("make_default") is True, login_method="cli")
        return {"binding": binding_view(binding, state), "org": binding.alias, "org_id": binding.org_id}

    def login(self, params: dict[str, Any], read_gate: ReadGate) -> dict[str, Any]:
        method, command = login_command(params)
        flags = check_flags(command, params.get("flags"))
        argv, stdin_text, env, credentials, jwt_secret, secrets = self._login_inputs(method, command, params, flags, read_gate)
        timeout = SF_CLI_WEB_LOGIN_TIMEOUT_SECONDS if method == "web" else SF_CLI_TIMEOUT_SECONDS
        try:
            result = run_cli_json(self._sf_cli_path(), [*argv, "--no-prompt"], label=str(flags.get("username") or flags.get("alias") or method), env_overrides=env, timeout=timeout, stdin_text=stdin_text, credentials=credentials)
        except SalesforceCliCallError as exc:
            raise _login_failure(method, exc, carried_secret=bool(secrets)) from None
        username = result.get("username") if isinstance(result, dict) else None
        if not isinstance(username, str) or not username:
            raise SalesforceServiceError(ERROR_AUTH_FAILED, f"The sf CLI login ({method}) finished without naming the logged-in user.")
        alias = _optional_text(params, "alias") or (str(flags["alias"]) if isinstance(flags.get("alias"), str) else None)
        binding, state = self.bind(username, alias=alias, pinned_host=None, make_default=params.get("make_default") is True, login_method=method, jwt_key_secret=jwt_secret, secret_names=secrets)
        return {"binding": binding_view(binding, state), "org": binding.alias, "org_id": binding.org_id, "method": method}

    def _login_inputs(
        self,
        method: str,
        command: CliCommand,
        params: dict[str, Any],
        flags: dict[str, object],
        read_gate: ReadGate,
    ) -> tuple[list[str], str | None, dict[str, str], tuple[CallCredential, ...], str, tuple[str, ...]]:
        """(argv, stdin, env, credentials, jwt key secret, secrets used) for one login method."""
        if method == "jwt":
            name = _secret_param(params, "jwt_key_secret")
            if (name is None) == ("jwt-key-file" not in flags):
                raise TypedFamilyError("a jwt login takes exactly one key: jwt_key_secret (a vault secret name) or the jwt-key-file flag")
            if name is None:
                return command_argv(command, flags, read_gate=read_gate), None, {}, (), "", ()
            key_file = self._secrets.jwt_key_file(name)
            return [*command_argv(command, flags, read_gate=read_gate), f"--jwt-key-file={key_file.path}"], None, {}, (key_file,), name, (name,)
        if method == "sfdx_url":
            name = _secret_param(params, "sfdx_auth_url_secret")
            if (name is None) == ("sfdx-url-file" not in flags):
                raise TypedFamilyError("an sfdx_url login takes exactly one auth URL: sfdx_auth_url_secret (a vault secret name) or the sfdx-url-file flag")
            if name is None:
                return command_argv(command, flags, read_gate=read_gate), None, {}, (), "", ()
            return [*command_argv(command, flags, read_gate=read_gate), "--sfdx-url-stdin=-"], self._secrets.read(name) + "\n", {}, (), "", (name,)
        if method == "access_token":
            name = _secret_param(params, "access_token_secret")
            if name is None:
                raise TypedFamilyError("an access_token login needs access_token_secret: the vault secret name holding the token")
            return command_argv(command, flags, read_gate=read_gate), None, {"SF_ACCESS_TOKEN": self._secrets.read(name)}, (), "", (name,)
        return command_argv(command, flags, read_gate=read_gate), None, {}, (), "", ()

    def logout(self, resolved: ResolvedOrg, params: dict[str, Any]) -> dict[str, Any]:
        flags = check_flags(LOGOUT_COMMAND, params.get("flags"))
        argv = command_argv(LOGOUT_COMMAND, flags, read_gate=_no_files)
        result = resolved.executor.run_unverified_json([*argv, "--target-org", resolved.binding.target_org, "--no-prompt"])
        usernames = [name for name in result if isinstance(name, str)] if isinstance(result, list) else []
        return {"logged_out_usernames": usernames, "binding_kept": True}

    def set_default_org(self, params: dict[str, Any]) -> dict[str, Any]:
        selector = org_selector(params)
        if selector is None:
            raise TypedFamilyError("org is required: the alias, username or org id of a bound org (see list_orgs)")
        state, chosen = self._index.load().with_default(selector)
        self._index.save(state)
        return {"org": chosen.alias, "org_id": chosen.org_id, "default": True}

    def disconnect_org(self, params: dict[str, Any]) -> dict[str, Any]:
        selector = org_selector(params)
        if selector is None:
            raise TypedFamilyError("org is required: the alias, username or org id of the bound org to disconnect (see list_orgs)")
        binding = self._index.load().select(selector)
        logout_status = "logged_out"
        try:
            self._executor_for(binding).run_unverified_json(["org", "logout", "--target-org", binding.target_org, "--no-prompt"])
        except (SalesforceCliCallError, SalesforceServiceError) as exc:
            logout_status = f"logout_failed_{getattr(exc, 'error_code', '') or getattr(exc, 'code', '') or type(exc).__name__}"
        state, deleted, legacy_deleted = self.unbind(binding)
        default = state.default()
        return {
            "org": binding.alias,
            "org_id": binding.org_id,
            "logout_status": logout_status,
            "binding_removed": True,
            "secrets_deleted": deleted,
            "legacy_entry_deleted": legacy_deleted,
            "default_org": default.alias if default is not None else "",
        }


def login_command(params: dict[str, Any]) -> tuple[str, CliCommand]:
    method = params.get("method")
    if not isinstance(method, str) or method not in LOGIN_COMMANDS:
        raise TypedFamilyError(f"method must be one of: {', '.join(LOGIN_COMMANDS)}")
    return method, LOGIN_COMMANDS[method]


def check_login(params: dict[str, Any]) -> None:
    """Dispatch-time shape checks for login (no I/O)."""
    method, command = login_command(params)
    check_flags(command, params.get("flags"))
    for key in ("jwt_key_secret", "sfdx_auth_url_secret", "access_token_secret"):
        if params.get(key) is not None:
            secret_name(params[key], key)
    _optional_text(params, "alias")
    if method == "access_token" and params.get("access_token_secret") is None:
        raise TypedFamilyError("an access_token login needs access_token_secret: the vault secret name holding the token")


def _login_failure(method: str, exc: SalesforceCliCallError, *, carried_secret: bool) -> SalesforceServiceError:
    name = exc.error_code or "unknown"
    if carried_secret:
        return SalesforceServiceError(ERROR_AUTH_FAILED, f"The sf CLI login ({method}) failed ({name}). The CLI's message is not returned because this login carried a secret from the vault. {_LOGIN_HINTS.get(method, '')}".strip())
    detail = f": {exc.detail_message}" if exc.detail_message else ""
    return SalesforceServiceError(ERROR_AUTH_FAILED, f"The sf CLI login ({method}) failed ({name}){detail}")


def _secret_param(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    return None if value is None else secret_name(value, key)


def _optional_text(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TypedFamilyError(f"{key} must be a non-empty string")
    return value.strip()


def _no_files(path: str) -> str:
    raise TypedFamilyError(f"this command takes no local file ({path})")


def describe_cli_failure(exc: SalesforceCliCallError) -> SalesforceServiceError:
    """A CLI error for a call that carried no plugin-supplied secret: its name, message and actions as received."""
    actions = f" Next: {' '.join(exc.actions)}" if exc.actions else ""
    detail = f": {exc.detail_message}" if exc.detail_message else ""
    return SalesforceServiceError(ERROR_API_ERROR, f"{exc.error_code or 'sf CLI error'}{detail}{actions}")
