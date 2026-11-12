"""Dispatch checks and worker producers for six verbs that used to do vendor work inline (mixed into ``GSuitePlugin``).

test_connection, sheets_export, docs_export, slides_export, start_interface and
disconnect_account check their arguments on the calling thread, enqueue one job
on the plugin's existing ledger (AsyncJobManager action ``workspace_job``) and
return ``{job_id, status: queued}``. The plugin's single worker thread
(``GSuitePlugin._process_job``) does the vendor work later. A finished job's
``result`` is the ``data`` the verb used to return inline, and a failed job's
``error`` is the same ``{code, message}``; read either with
``service_interface::job_service::get_job``.

The calling thread builds no Google client, makes no vendor request and binds
no socket. It reads only the local vault (the account selector) and the
arguments. An invalid argument therefore still fails at once, without a job.

start_interface and disconnect_account are account-free: the callback server
must start before any account is connected, and a disconnect must not migrate
a legacy credential first. The worker calls them directly instead of inside
the account runtime. disconnect_account's producer lives with the other
account verbs in :mod:`account_processes`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, cast

from ananta.core.plugins.plugin_base import ServicePlugin

from . import docs_actions, drive_actions, sheets_actions, slides_actions
from .account_processes import ACTIVE_ACCOUNT, AccountProcesses, VerbFailureError, account_selector
from .accounts import AccountStore
from .constants import (
    DOCS_DEFAULT_EXPORT_FORMAT,
    DOCS_EXPORT_MIME_BY_FORMAT,
    ERROR_INVALID_PARAMS,
    ERROR_SERVER_START_FAILED,
    OAUTH_SCOPES,
    SHEETS_DEFAULT_EXPORT_FORMAT,
    SHEETS_EXPORT_MIME_BY_FORMAT,
    SLIDES_DEFAULT_EXPORT_FORMAT,
    SLIDES_EXPORT_MIME_BY_FORMAT,
)
from .drive_actions import BlobWriter
from .oauth.app_config import AppConfigLoader
from .oauth.http_server import OAuthServer, OAuthServerStartError, build_start_result
from .oauth.oauth_routes import CallbackOptions
from .oauth.service_factory import GoogleServiceFactory
from .oauth.token_store import TokenStore, TokenStoreError

ERROR_SERVICE_NOT_AVAILABLE: Final[str] = "service_not_available"

Export = Callable[[Any, dict[str, Any], BlobWriter], dict[str, Any]]
Producer = Callable[[dict[str, Any]], object]

# verb -> (default format, format -> export mime, the Drive export helper)
EXPORTS: Final[dict[str, tuple[str, dict[str, str], Export]]] = {
    "sheets_export": (SHEETS_DEFAULT_EXPORT_FORMAT, SHEETS_EXPORT_MIME_BY_FORMAT, sheets_actions.export_spreadsheet),
    "docs_export": (DOCS_DEFAULT_EXPORT_FORMAT, DOCS_EXPORT_MIME_BY_FORMAT, docs_actions.export_document),
    "slides_export": (SLIDES_DEFAULT_EXPORT_FORMAT, SLIDES_EXPORT_MIME_BY_FORMAT, slides_actions.export_presentation),
}


class QueuedVerbs:
    """Calling-thread checks and worker producers; ``GSuitePlugin`` supplies the hooks and state below."""

    _oauth_server: OAuthServer
    _pending_states: dict[str, str]
    _connect: Any

    def _dispatch_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    def _error(self, code: str, message: str) -> dict[str, Any]:
        raise NotImplementedError

    def _require_accounts(self) -> AccountStore:
        raise NotImplementedError

    def _require_factory(self) -> GoogleServiceFactory:
        raise NotImplementedError

    def _require_token_store(self) -> TokenStore:
        raise NotImplementedError

    def _require_app_config_loader(self) -> AppConfigLoader:
        raise NotImplementedError

    def _store_blob(self, content: bytes, filename: str, mime_type: str) -> str:
        raise NotImplementedError

    # -- calling thread --------------------------------------------------------

    def _queue_export(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        """Check the account, then the id and format, in the order the inline verb failed on them; enqueue."""
        default_format, mime_by_format, _ = EXPORTS[verb]
        try:
            self._require_accounts().check_selector(account_selector(params))
            drive_actions.export_target(params, default_format, mime_by_format)
        except TokenStoreError as exc:
            return self._error(exc.code, str(exc))
        except ValueError as exc:
            return self._error(ERROR_INVALID_PARAMS, str(exc))
        return self._dispatch_deferred(params, state, verb)

    def _queue_start_interface(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """An unparseable port still raises here at once; a plugin that is not ready still refuses at once."""
        self._interface_address(params)
        try:
            self._require_token_store()
            self._require_app_config_loader()
        except RuntimeError as exc:
            return self._error(ERROR_SERVICE_NOT_AVAILABLE, str(exc))
        return self._dispatch_deferred(params, state, "start_interface")

    def _interface_address(self, params: dict[str, Any]) -> tuple[str, int | None]:
        """(bind host, preferred port): the call's arguments, else the plugin config, else loopback and any port."""
        config = cast(ServicePlugin, self).config_provider or {}
        host = str(params.get("host") or config.get("callback_host") or "127.0.0.1")
        port = params.get("port") or config.get("callback_port")
        return host, int(str(port)) if port is not None else None

    # -- worker thread ---------------------------------------------------------

    def _queued_verb_producers(self) -> dict[str, Producer]:
        """Producers the worker runs inside the account runtime, with the job's account bound."""
        producers: dict[str, Producer] = {verb: self._export_producer(export) for verb, (_, _, export) in EXPORTS.items()}
        producers["test_connection"] = self._test_connection_now
        return producers

    def _account_free_producers(self) -> dict[str, Producer]:
        """Producers the worker calls directly, outside the account runtime."""
        return {"start_interface": self._start_interface_now, "disconnect_account": cast(AccountProcesses, self)._disconnect_now}

    def _export_producer(self, export: Export) -> Producer:
        return lambda params: export(self._require_factory().drive(), params, self._store_blob)

    def _test_connection_now(self, params: dict[str, Any]) -> dict[str, Any]:
        """Gmail's minimal profile read, plus the bound account's recorded tier and scopes."""
        profile = self._require_factory().gmail().users().getProfile(userId="me").execute()
        account_identity = profile.get("emailAddress")
        if not isinstance(account_identity, str) or not account_identity:
            raise ValueError("Gmail profile response did not contain emailAddress")
        facts = self._require_accounts().token_store(ACTIVE_ACCOUNT.get()).credential_facts()
        granted = facts["granted_scopes"]
        return {
            "account_identity": account_identity,
            "harmless_read_ok": True,
            "account_tier": facts["account_tier"],
            "granted_scopes": granted,
            "missing_default_scopes": sorted(set(OAUTH_SCOPES) - set(granted)) if granted is not None else None,
        }

    def _start_interface_now(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start the OAuth callback server; this waits until its socket is bound or the start fails."""
        host, preferred_port = self._interface_address(params)
        try:
            token_store = self._require_token_store()
            app_config_loader = self._require_app_config_loader()
        except RuntimeError as exc:
            raise VerbFailureError(ERROR_SERVICE_NOT_AVAILABLE, str(exc)) from exc
        try:
            port = self._oauth_server.start(
                host=host,
                preferred_port=preferred_port,
                token_store=token_store,
                app_config_loader=app_config_loader,
                pending_states=self._pending_states,
                callback_options=CallbackOptions(requested_scopes=self._connect.scopes, complete=cast(AccountProcesses, self)._complete_connect),
            )
        except OAuthServerStartError as exc:
            raise VerbFailureError(ERROR_SERVER_START_FAILED, str(exc)) from exc
        return build_start_result(port, host)
