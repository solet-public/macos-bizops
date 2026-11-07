"""FastAPI route for the Google OAuth 2.0 callback.

Provides a single route: GET /oauth/google/callback

The callback validates the ``state`` nonce, restores the PKCE ``code_verifier``
persisted at authorize time, exchanges the authorization code via the Google
library, and writes both tokens to vault through the token store. On failure it
returns an HTTP error with a description.

The router is built by create_oauth_router() which captures the token store,
app-config loader, and pending-states dict by reference — no global state.
"""

from __future__ import annotations

import asyncio
import html
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any

from ..constants import ACCOUNT_TIER_UNKNOWN, ERROR_OAUTH_STATE_INVALID, OAUTH_CALLBACK_PATH, OAUTH_SCOPES
from .app_config import AppConfigError, AppConfigLoader
from .oauth_client import GoogleTokens, exchange_code
from .token_store import TokenStore, TokenStoreError

_logger = logging.getLogger(__name__)

# One worker preserves the pre-offload callback/ledger mutation ordering.
_REQUEST_WORKERS = 1
_REQUEST_MAX_PENDING = 32
_REQUEST_EXECUTOR = ThreadPoolExecutor(max_workers=_REQUEST_WORKERS, thread_name_prefix="google-oauth")
_REQUEST_SLOTS = threading.BoundedSemaphore(_REQUEST_WORKERS + _REQUEST_MAX_PENDING)
_logger.info("google-oauth executor created: workers=%d queue_bound=%d", _REQUEST_WORKERS, _REQUEST_MAX_PENDING)


async def _run_request_work[**P, T](function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Wait asynchronously for bounded admission; retain admitted mutations."""
    while not _REQUEST_SLOTS.acquire(blocking=False):
        await asyncio.sleep(0.005)
    context = copy_context()
    try:
        future = _REQUEST_EXECUTOR.submit(lambda: context.run(function, *args, **kwargs))
    except BaseException:
        _REQUEST_SLOTS.release()
        raise
    future.add_done_callback(lambda _done: _REQUEST_SLOTS.release())
    return await asyncio.shield(asyncio.wrap_future(future))


@dataclass(frozen=True)
class CallbackOptions:
    """Optional callback seams (Gmail build 1, 2026-11-07).

    ``requested_scopes`` maps a state nonce to the scope list its consent URL
    requested (absent = the default ``OAUTH_SCOPES``); the exchange must use the
    same list. ``account_probe`` reads (email, tier) with the new access token
    so the tier is recorded on the credential at connect time.
    """

    requested_scopes: dict[str, tuple[str, ...]] = field(default_factory=lambda: {})
    account_probe: Callable[[str], tuple[str, str]] | None = None
    # Multi-account completion (Operator ord 90): when set, it stores the
    # exchanged tokens under the account the token belongs to (state, tokens)
    # instead of the single legacy pair, and returns a note for the page ('' when none).
    complete: Callable[[str, GoogleTokens], str] | None = None


def _record_credential_facts(token_store: TokenStore, tokens: Any, options: CallbackOptions) -> None:
    """Record granted scopes and account tier; advisory, never fails the connect."""
    email, tier = "", ACCOUNT_TIER_UNKNOWN
    try:
        if options.account_probe is not None:
            email, tier = options.account_probe(tokens.access_token)
    except Exception as exc:  # noqa: BLE001 — a failed probe records "unknown", it never blocks connect
        _logger.warning("OAuth callback: account tier probe failed (%s); tier recorded as unknown", type(exc).__name__)
    try:
        token_store.record_credential_facts(
            granted_scopes=tuple(getattr(tokens, "granted_scopes", ()) or ()),
            account_email=email,
            account_tier=tier,
        )
    except Exception as exc:  # noqa: BLE001 — recording is advisory (rul_becb4bed)
        _logger.warning("OAuth callback: credential facts not recorded (%s)", type(exc).__name__)


def create_oauth_router(
    token_store: TokenStore,
    app_config_loader: AppConfigLoader,
    pending_states: dict[str, str],
    *,
    options: CallbackOptions | None = None,
) -> Any:
    """Return a FastAPI APIRouter wired to the token store and PKCE-state map.

    Args:
        token_store: Vault-backed token store to write tokens into.
        app_config_loader: Resolves OAuth app config from the address book.
        pending_states: Mutable dict mapping ``state_nonce -> code_verifier``.
            Populated by connect_account; consumed and cleared here.
    """
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import HTMLResponse

    callback_options = options or CallbackOptions()
    router = APIRouter()
    # Preserve callback mutation order even while bounded admission waits.
    request_order = asyncio.Lock()

    @router.get(OAUTH_CALLBACK_PATH)
    async def oauth_callback(  # pyright: ignore[reportUnusedFunction]
        code: str = "",
        state: str = "",
        error: str = "",
    ) -> HTMLResponse:
        """Receive the Google authorization_code redirect and exchange for tokens."""
        if error:
            # The callback can be externally reachable when a cloud deployment
            # explicitly binds 0.0.0.0 behind an ALB, so provider/query text must
            # never be reflected into the HTML body or logs verbatim. Log only the
            # short OAuth error CODE (a defined token like 'access_denied'), not the
            # free-text error_description; render a generic body.
            _logger.error("Google OAuth returned an authorization error: %s", error)
            return HTMLResponse(
                content=_html_result(
                    "OAuth Error",
                    "Google returned an error during authorization. Check the solet logs and re-run connect_account.",
                    success=False,
                ),
                status_code=400,
            )

        code_verifier = pending_states.pop(state, None) if state else None
        if not state or code_verifier is None:
            # state is max-sensitivity metadata (and usually attacker-supplied or
            # already spent here) — log only a short redacted prefix, never the value.
            _logger.warning("OAuth callback: invalid or expired state nonce (%s)", _redact(state))
            raise HTTPException(
                status_code=400,
                detail=f"{ERROR_OAUTH_STATE_INVALID}: state nonce is invalid or expired",
            )

        if not code:
            raise HTTPException(status_code=400, detail="OAuth callback received no authorization code")

        async with request_order:
            return await _run_request_work(_complete_callback, code, state, code_verifier)

    def _complete_callback(code: str, state: str, code_verifier: str) -> HTMLResponse:
        try:
            app_config = app_config_loader.load()
        except AppConfigError as exc:
            _logger.error("OAuth callback: failed to load app config: %s", exc)
            raise HTTPException(status_code=500, detail="OAuth app configuration is unavailable.") from exc

        try:
            scopes = callback_options.requested_scopes.pop(state, OAUTH_SCOPES)
            tokens = exchange_code(app_config, code=code, state=state, code_verifier=code_verifier, scopes=scopes)
        except Exception as exc:
            _logger.error("OAuth callback: token exchange failed: %s", exc)
            raise HTTPException(status_code=502, detail="Google token exchange failed.") from exc

        refresh_token = tokens.refresh_token
        if not refresh_token:
            raise HTTPException(
                status_code=502,
                detail=("Google did not return a refresh token. Re-run connect_account; the OAuth app must request offline access with prompt=consent."),
            )

        if callback_options.complete is not None:
            try:
                note = callback_options.complete(state, tokens)
            except Exception as exc:  # noqa: BLE001 — any storage/probe fault is a typed 500, logged without secrets
                _logger.error("OAuth callback: account storage failed: %s", type(exc).__name__)
                raise HTTPException(status_code=500, detail=f"Token storage failed: {type(exc).__name__}.") from exc
            if note:
                _logger.warning("OAuth callback: %s", note)
            _logger.info("Google OAuth bootstrap complete — account tokens stored in vault")
            return HTMLResponse(
                content=_html_result(
                    "Google Workspace Authentication Complete",
                    " ".join(filter(None, ["Account connected.", note, "You can close this window and return to the solet."])),
                    success=True,
                )
            )

        try:
            token_store.store_initial_tokens(
                refresh_token=refresh_token,
                access_token=tokens.access_token,
                expires_in=tokens.expires_in,
            )
        except TokenStoreError as exc:
            _logger.error("OAuth callback: failed to store tokens: %s", exc)
            raise HTTPException(status_code=500, detail="Token storage failed.") from exc

        _record_credential_facts(token_store, tokens, callback_options)
        _logger.info("Google OAuth bootstrap complete — tokens stored in vault")
        return HTMLResponse(
            content=_html_result(
                "Google Workspace Authentication Complete",
                "Tokens stored. You can close this window and return to the solet.",
                success=True,
            )
        )

    return router


def _redact(value: str) -> str:
    """Short, non-reversible-enough label for a sensitive nonce in a log line."""
    return f"prefix={value[:6]}…" if value else "empty"


def _html_result(title: str, body: str, *, success: bool) -> str:
    color = "#2d7a2d" if success else "#b00020"
    # Escape all rendered text (defense-in-depth): the callback origin is
    # externally reachable, so nothing dynamic may become markup.
    safe_title = html.escape(title)
    safe_body = html.escape(body)
    return f"<!DOCTYPE html><html><head><title>{safe_title}</title><style>body{{font-family:sans-serif;max-width:600px;margin:60px auto;padding:20px}}h1{{color:{color}}}</style></head><body><h1>{safe_title}</h1><p>{safe_body}</p></body></html>"
