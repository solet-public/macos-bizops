"""``@platform_process`` account verbs (mixed into ``GSuitePlugin``).

list_accounts, set_default_account and disconnect_account manage the connected
Google accounts (contract in :mod:`accounts`). They read and write only the
local vault, except disconnect_account, which also revokes the account's grant
at Google: it checks the account at once and runs the revoke and the vault
deletion in the plugin's job worker (``_disconnect_now``).
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, cast

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory

from .accounts import AccountStore
from .completion_templates import queued_handle_schema
from .constants import ERROR_INVALID_PARAMS
from .errors import classify_exception
from .oauth.oauth_client import GoogleTokens, revoke_token
from .oauth.service_factory import probe_account
from .oauth.token_store import TokenStore, TokenStoreError

ACCOUNT_PARAM = ParameterMetadata(
    type=ParameterType.STRING,
    required=False,
    description=("Which connected Google account to use: its email or alias. Optional with one account; with several, the default account is used. The result names the account used."),
)
_REQUIRED_ACCOUNT = ParameterMetadata(
    type=ParameterType.STRING,
    required=True,
    description="The connected Google account: its email or alias (see list_accounts).",
)


# The connected account the current verb call runs as (multiple accounts,
# Operator ord 90). Set around each producer call; _require_factory reads it.
ACTIVE_ACCOUNT: ContextVar[str] = ContextVar("g_suite_active_account", default="")


def account_selector(params: dict[str, Any]) -> str | None:
    value = params.get("account")
    return value.strip() if isinstance(value, str) and value.strip() else None


class VerbFailureError(Exception):
    """A classified verb failure (code + message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AccountProcesses:
    """Account registry verbs and the per-call account runtime.

    ``GSuitePlugin`` supplies ``_require_accounts``, ``_success``, ``_error``
    and the ``_connect`` pending-consent state.
    """

    _connect: Any

    @staticmethod
    def _probe_store(store: TokenStore) -> tuple[str, str]:
        return probe_account(store.get_access_token())

    def _call_as_account[Result](self, verb: str, params: dict[str, Any], produce: Callable[[], Result]) -> Result:
        """Bind account metadata to the vendor result, returned as received; a non-mapping result is returned unchanged."""
        accounts = self._require_accounts()
        resolved = accounts.resolve(account_selector(params), self._probe_store)
        email = resolved.record.email
        store = accounts.token_store(email)
        token = ACTIVE_ACCOUNT.set(email)
        try:
            data = produce()
        except Exception as exc:  # noqa: BLE001 — classified, then re-raised typed
            code, message = classify_exception(exc, verb, store.credential_facts()["granted_scopes"])
            raise VerbFailureError(code, message) from exc
        finally:
            ACTIVE_ACCOUNT.reset(token)
        if not isinstance(data, dict):
            return data
        shaped = dict(cast(dict[str, Any], data))
        shaped["account"] = email
        if resolved.note:
            shaped["account_note"] = resolved.note
        return cast(Result, shaped)

    def _complete_connect(self, state: str, tokens: GoogleTokens) -> str:
        """OAuth callback completion: store tokens under the account they belong to.

        A legacy single-account pair still awaiting migration is migrated first,
        so it is indexed (and stays the default unless set_default) before the
        new account is added. Returns the migration note or advisory ('' when none).
        """
        email, tier = probe_account(tokens.access_token)
        alias, set_default = self._connect.choices.pop(state, ("", False))
        refresh_token = tokens.refresh_token
        if not refresh_token:
            raise TokenStoreError("token_exchange_failed", "Google returned no refresh token; re-run connect_account.")
        accounts = self._require_accounts()
        note = accounts.migrate_pending_legacy(self._probe_store)
        store = accounts.token_store(email)
        store.store_initial_tokens(refresh_token=refresh_token, access_token=tokens.access_token, expires_in=tokens.expires_in)
        store.record_credential_facts(granted_scopes=tokens.granted_scopes, account_email=email, account_tier=tier)
        accounts.upsert(email, alias=alias, set_default=set_default)
        return note

    def _require_accounts(self) -> AccountStore:
        raise NotImplementedError

    def _success(self, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _error(self, code: str, message: str) -> dict[str, Any]:
        raise NotImplementedError

    def _dispatch_deferred(self, params: dict[str, Any], state: dict[str, Any], verb: str) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="list_accounts",
        display_name="Google Workspace: List Connected Accounts",
        description=("List every connected Google account: email, alias, tier (consumer, workspace_user or unknown), granted OAuth scopes, the default flag and token health. Reads the local vault only."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="accounts: list of {email, alias, tier, granted_scopes, default, token_health}; count; legacy_pending_migration.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        context_handling=ContextHandling.NONE,
    )
    def list_accounts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        accounts = self._require_accounts()
        rows: list[dict[str, Any]] = []
        for record in accounts.records():
            store = accounts.token_store(record.email)
            facts = store.credential_facts()
            rows.append(
                {
                    "email": record.email,
                    "alias": record.alias,
                    "tier": facts["account_tier"],
                    "granted_scopes": facts["granted_scopes"],
                    "default": record.is_default,
                    "token_health": store.token_health(),
                    "migrated_from_legacy": record.migrated_from_legacy,
                }
            )
        return self._success({"accounts": rows, "count": len(rows), "legacy_pending_migration": accounts.legacy_pending()})

    @platform_process(
        name="set_default_account",
        display_name="Google Workspace: Set Default Account",
        description="Make one connected Google account (email or alias) the default that verbs use when no account is named.",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={"account": _REQUIRED_ACCOUNT},
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="The new default account {email, alias}."),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def set_default_account(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        selector = params.get("account")
        if not isinstance(selector, str) or not selector:
            return self._error(ERROR_INVALID_PARAMS, "'account' is required: an email or alias from list_accounts")
        try:
            record = self._require_accounts().set_default(selector)
        except TokenStoreError as exc:
            return self._error(exc.code, str(exc))
        return self._success({"account": record.email, "alias": record.alias, "default": True})

    @platform_process(
        name="disconnect_account",
        display_name="Google Workspace: Disconnect Account",
        description=(
            "Disconnect one Google account: revoke its grant at Google and delete only that account's credentials from the vault. Other connected accounts are untouched; if it was the default, the next account becomes the default. "
            "Runs as a background job: a missing or unknown account is refused at once; otherwise this returns a job_id immediately and the outcome arrives when the job completes."
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_async=True,
        is_long_running=True,
        parameters={"account": _REQUIRED_ACCOUNT},
        return_value_schema=queued_handle_schema("{account, revoke_status, credentials_deleted, new_default}"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        context_handling=ContextHandling.NONE,
    )
    def disconnect_account(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        selector = params.get("account")
        if not isinstance(selector, str) or not selector:
            return self._error(ERROR_INVALID_PARAMS, "'account' is required: an email or alias from list_accounts")
        try:
            self._require_accounts().select(selector)
        except TokenStoreError as exc:
            return self._error(exc.code, str(exc))
        return self._dispatch_deferred(params, state, "disconnect_account")

    def _disconnect_now(self, params: dict[str, Any]) -> dict[str, Any]:
        """Worker side of disconnect_account: revoke at Google (best effort), then delete only this account's credentials."""
        selector = str(params["account"])
        accounts = self._require_accounts()
        records = accounts.records()
        target = next((r for r in records if selector.strip().lower() in (r.email, r.alias.lower())), None)
        revoke_status = "not_attempted"
        if target is not None:
            refresh_value, _ = accounts.token_store(target.email).read_raw_pair()
            revoke_status = _revoke(refresh_value)
        removed, new_default = accounts.remove(selector)
        return {
            "account": removed.email,
            "revoke_status": revoke_status,
            "credentials_deleted": True,
            "new_default": new_default.email if new_default else "",
        }


def _revoke(refresh_value: str | None) -> str:
    if not refresh_value:
        return "no_refresh_token"
    try:
        return revoke_token(refresh_value)
    except Exception as exc:  # noqa: BLE001 — revoke is best effort; local deletion proceeds and the status says why
        return f"revoke_failed_{type(exc).__name__}"
