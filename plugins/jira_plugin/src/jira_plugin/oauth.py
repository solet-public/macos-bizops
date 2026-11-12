"""Atlassian 3LO consent, code exchange, resource verification and rotating refresh."""

from __future__ import annotations

import hmac
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from .sites import PREFIX, SiteStore, _fail, _name, _site_url


class OAuthFlow:
    """One site's OAuth state machine, serialized by its owning SiteStore."""

    def __init__(self, store: SiteStore) -> None:
        self.store = store

    def manage(self, params: dict[str, Any]) -> dict[str, Any]:
        site = _name(params.get("site"))
        if site == "legacy":
            raise ValueError("legacy is reserved")
        key = self.store.key(site) + "__pending"
        operation = params.get("operation")
        if operation == "begin":
            return self.begin(site, key, params)
        if operation == "cancel":
            self.store._delete(key)
            return {"site": site, "cancelled": True}
        pending = self.store._read(key)
        if not pending:
            raise _fail("oauth_state", "No pending OAuth authorization for this site.")
        if operation == "status":
            return {"site": site, "pending": True, "expired": pending["deadline"] < time.time()}
        if operation != "complete":
            raise ValueError("OAuth operation must be begin, status, complete or cancel")
        return self.complete(site, key, pending, params)

    def begin(self, site: str, key: str, params: dict[str, Any]) -> dict[str, Any]:
        base_url = _site_url(params.get("base_url"))
        app = _address_fields(self.store.address_book, params.get("app_entry", "jira_oauth_app"))
        if not all(app.get(k) for k in ("client_id", "client_secret", "redirect_uri", "scopes")):
            raise ValueError("OAuth app entry needs client_id, vault client_secret, redirect_uri and scopes")
        state = secrets.token_urlsafe(32)
        self.store._write(key, {**app, "state": state, "base_url": base_url, "deadline": time.time() + self.store.oauth_state_ttl_seconds})
        return {
            "site": site,
            "authorize_url": "https://auth.atlassian.com/authorize?"
            + urlencode(
                {
                    "audience": "api.atlassian.com",
                    "client_id": app["client_id"],
                    "scope": app["scopes"],
                    "redirect_uri": app["redirect_uri"],
                    "state": state,
                    "response_type": "code",
                    "prompt": "consent",
                }
            ),
            "expires_in": self.store.oauth_state_ttl_seconds,
        }

    def complete(self, site: str, key: str, pending: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        if pending["deadline"] < time.time() or not hmac.compare_digest(str(params.get("state", "")), pending["state"]):
            raise _fail("oauth_state", "OAuth state is invalid or expired; begin again.")
        self.store._expected(self.store.index(), params.get("expected_version"))
        code_key, code = self._code(params.get("code_vault_key"))
        tokens = self.token({"grant_type": "authorization_code", "client_id": pending["client_id"], "client_secret": pending["client_secret"], "code": code, "redirect_uri": pending["redirect_uri"]})
        resource = self.resource(tokens["access_token"], pending["base_url"])
        value = {**tokens, "auth_type": "oauth", "base_url": pending["base_url"], "cloud_id": resource["id"], "client_id": pending["client_id"], "client_secret": pending["client_secret"], "scopes": resource.get("scopes", [])}
        self.store._update(site, value, params.get("expected_version"), params.get("set_default") is True)
        self.store._delete(key)
        self.store._delete(code_key)
        return {**self.store.describe(), "site": site}

    def _code(self, key: Any) -> tuple[str, str]:
        if not isinstance(key, str) or not key.startswith(PREFIX + "oauth_code"):
            raise ValueError("code_vault_key must be a plugin-owned oauth_code vault key")
        result = self.store.vault.retrieve(key)
        self.store._check(result)
        code = result.get("data", {}).get("value")
        if not isinstance(code, str) or not code:
            raise ValueError("authorization code vault value is empty")
        return key, code

    def resource(self, access_token: str, base_url: str) -> dict[str, Any]:
        response = self.store.http(
            "GET",
            "https://api.atlassian.com/oauth/token/accessible-resources",
            {
                "Authorization": "Bearer " + access_token,
                "Accept": "application/json",
            },
        )
        if response.status != 200 or not isinstance(response.data, list):
            raise _fail("oauth_resources", "Could not verify the grant's accessible sites.")
        resource = next((r for r in response.data if isinstance(r, dict) and r.get("url", "").rstrip("/") == base_url), None)
        if resource is None:
            raise _fail("oauth_resources", "The grant does not include the requested site.")
        cloud_id = resource.get("id")
        if not isinstance(cloud_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]+", cloud_id):
            raise _fail("oauth_resources", "Vendor returned an invalid cloud identifier.")
        return resource

    def token(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self.store.http(
            "POST",
            "https://auth.atlassian.com/oauth/token",
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body,
        )
        if response.status != 200 or not isinstance(response.data, dict):
            raise _fail("auth_failed", "Atlassian token exchange failed; reconnect or verify app credentials.")
        data = response.data
        if not all(isinstance(data.get(k), str) and data[k] for k in ("access_token", "refresh_token")):
            raise _fail("auth_failed", "Token response lacks access/rotating refresh token; request offline_access.")
        seconds = data.get("expires_in")
        if type(seconds) is not int or seconds <= 0:
            raise _fail("auth_failed", "Token response contains no valid expiry.")
        return {"access_token": data["access_token"], "refresh_token": data["refresh_token"], "expires_at": time.time() + seconds}


def _address_fields(address_book: Any, name: str) -> dict[str, str]:
    result = address_book.resolve_with_secrets(name=name)
    if not isinstance(result, dict) or result.get("action_status") != "completed":
        raise _fail("not_configured", "OAuth app address-book entry could not be resolved.")
    fields: dict[str, str] = {}
    for entry in result.get("data", {}).get("entries", []):
        if isinstance(entry, dict) and isinstance(entry.get("field_type"), str) and isinstance(entry.get("value"), str):
            fields[entry["field_type"]] = entry["value"]
    return fields
