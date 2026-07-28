"""Marketo REST client — OAuth 2.0 client-credentials, synchronous.

Synchronous (not async) to match the house verb shape used by
zuora_plugin/salesforce_plugin: every ``@platform_process`` method is a plain
``(self, params, state)`` call, so a sync ``httpx.Client`` avoids running an
event loop inside a sync verb call.

Unlike zuora_plugin's client, this one returns **decoded JSON envelopes**,
not raw ``httpx.Response`` objects — Marketo's fault model lives in the body
(``success: false`` + ``errors[]``) at HTTP 200, not in the status code (see
``errors.py`` module docstring). ``get_json``/``post_json``/``delete_json``
each: perform the request, decode the JSON body, and either return the
envelope dict (``success`` truthy — including partial-batch results where
individual records failed but the call itself succeeded) or raise
:class:`errors.MarketoEnvelopeError` (``success`` falsy) for the caller's
classifier. A transport-level fault (5xx, connection error, non-JSON body)
raises :class:`errors.MarketoTransportError` instead.

The bearer token is cached in-memory and re-fetched ~30s before its recorded
expiry (clock-skew margin), or once on a decoded envelope carrying error code
601/602 (invalid/expired token) — exactly one re-mint-and-retry, mirroring
zuora_plugin's 401-triggers-one-refetch pattern but keyed off the envelope
instead of the HTTP status. It is NEVER vaulted — only the client_secret (the
durable credential) is a secret; the token is trivially re-mintable.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .app_config import MarketoInstanceConfig
from .constants import (
    DEFAULT_TOKEN_TTL_SECONDS,
    IDENTITY_TOKEN_PATH,
    TOKEN_REFRESH_MARGIN_SECONDS,
)
from .errors import MarketoEnvelopeError, MarketoTransportError, is_retryable_auth_code

_HTTP_OK: int = 200


class MarketoAuthError(RuntimeError):
    """Raised when the OAuth client-credentials token fetch itself fails."""


class MarketoClient:
    """A synchronous Marketo REST client with cached, re-mintable bearer auth."""

    def __init__(
        self,
        config: MarketoInstanceConfig,
        *,
        timeout_seconds: float,
    ) -> None:
        self._config = config
        self._http = httpx.Client(base_url=config.base_url, timeout=timeout_seconds)
        self._token: str | None = None
        self._expires_at_monotonic: float = 0.0

    def close(self) -> None:
        self._http.close()

    def ensure_authenticated(self) -> None:
        """Mint (or reuse the cached) bearer token — used by test_connection to prove reachability."""
        self._bearer()

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("GET", path, params=params, json=None)

    def post_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_json("POST", path, params=params, json=json)

    def delete_json(self, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        # httpx.Client.delete() does not accept a json= body; Marketo's list
        # membership removal is DELETE-with-a-JSON-body, so this goes through
        # the low-level .request() form instead.
        return self._request_json("DELETE", path, params=None, json=json)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response = self._do_request(method, path, params=params, json=json)
        payload = self._decode(response)
        if payload.get("success"):
            return payload
        errors = payload.get("errors")
        first_code = str(errors[0].get("code", "")) if isinstance(errors, list) and errors and isinstance(errors[0], dict) else ""
        if is_retryable_auth_code(first_code):
            # Exactly one re-mint-and-retry, then classify whatever comes back.
            self._token = None
            response = self._do_request(method, path, params=params, json=json)
            payload = self._decode(response)
            if payload.get("success"):
                return payload
        raise MarketoEnvelopeError(payload)

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 500:
            raise MarketoTransportError(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketoTransportError(response.status_code) from exc
        if not isinstance(payload, dict):
            raise MarketoTransportError(response.status_code)
        return payload

    def _do_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        return self._http.request(method, path, params=params, json=json, headers=headers)

    def _bearer(self) -> str:
        if self._token is not None and time.monotonic() < self._expires_at_monotonic:
            return self._token
        response = self._http.get(
            IDENTITY_TOKEN_PATH,
            params={
                "grant_type": "client_credentials",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            },
        )
        if response.status_code != _HTTP_OK:
            raise MarketoAuthError("Marketo OAuth token request failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketoAuthError("Marketo OAuth token response was not JSON") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise MarketoAuthError("Marketo OAuth token response carried no access_token")
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else DEFAULT_TOKEN_TTL_SECONDS
        self._token = token
        self._expires_at_monotonic = time.monotonic() + max(0.0, ttl - TOKEN_REFRESH_MARGIN_SECONDS)
        return token
