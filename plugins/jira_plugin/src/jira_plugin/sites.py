"""Vault-backed Jira site bindings and Atlassian OAuth 2.0 authorization grants.

The legacy address-book site stays available as ``legacy``. New bindings copy
resolved credentials into distinct plugin-owned vault envelopes, like Google's
per-account token store. One plugin lock serializes binding and refresh writes.
The vault is the source of truth; no credentials are accepted as process params.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from .app_config import AppConfigLoader
from .constants import _SOLET, DEFAULT_OAUTH_STATE_TTL_SECONDS, DEFAULT_REQUEST_TIMEOUT_SECONDS, DEFAULT_TOKEN_REFRESH_SKEW_SECONDS, PLUGIN_NAME
from .errors import JiraServiceError
from .http import Response, request

PREFIX = f"{_SOLET}.{PLUGIN_NAME}."
INDEX_KEY = PREFIX + "sites_index"
SITE_PREFIX = PREFIX + "site__"
HttpRequest = Callable[..., Response]


def _fail(code: str, message: str) -> JiraServiceError:
    return JiraServiceError("jira." + code, message)


def _name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
        raise ValueError("site must contain 1-64 letters, digits, underscores or hyphens")
    return value.lower()


def _site_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("base_url must be an HTTPS Atlassian Cloud site URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url must be an HTTPS Atlassian Cloud site origin") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("base_url must be an HTTPS Atlassian Cloud site origin")
    if not parsed.hostname.endswith(".atlassian.net") or port not in (None, 443):
        raise ValueError("base_url must be an HTTPS Atlassian Cloud site origin")
    if any((parsed.username, parsed.password, parsed.query, parsed.fragment)) or parsed.path not in ("", "/"):
        raise ValueError("base_url must be an HTTPS Atlassian Cloud site origin")
    return f"https://{parsed.hostname}"


def _api_root(value: str) -> str:
    """Scoped API tokens also use Atlassian's documented ex/jira cloud gateway."""
    if re.fullmatch(r"https://api\.atlassian\.com/ex/jira/[a-zA-Z0-9-]+/?", value):
        return value.rstrip("/")
    return _site_url(value)


class SiteStore:
    """Site lifecycle and rotating grants over the existing vault service API."""

    def __init__(self, vault: Any, address_book: Any, http: HttpRequest | None = None, *, timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS, oauth_state_ttl_seconds: int = DEFAULT_OAUTH_STATE_TTL_SECONDS, token_refresh_skew_seconds: int = DEFAULT_TOKEN_REFRESH_SKEW_SECONDS) -> None:
        self.vault = vault
        self.address_book = address_book
        self.http: HttpRequest = http if http is not None else partial(request, timeout=timeout)
        self.oauth_state_ttl_seconds = oauth_state_ttl_seconds
        self.token_refresh_skew_seconds = token_refresh_skew_seconds
        self.lock = threading.RLock()

    def _exists(self, key: str) -> bool:
        result = self.vault.exists(key)
        self._check(result)
        return bool(result.get("data", {}).get("exists"))

    def _read(self, key: str) -> dict[str, Any] | None:
        if not self._exists(key):
            return None
        result = self.vault.retrieve(key)
        self._check(result)
        try:
            value = json.loads(result["data"]["value"])
        except (ValueError, KeyError, TypeError) as exc:
            raise _fail("vault_error", "Jira vault envelope is invalid.") from exc
        if not isinstance(value, dict):
            raise _fail("vault_error", "Jira vault envelope must be an object.")
        return value

    def _write(self, key: str, value: dict[str, Any]) -> None:
        encoded = json.dumps(value)
        if self._exists(key):
            result = self.vault.rotate(key, encoded)
        else:
            result = self.vault.store(key=key, value=encoded, tags=["jira_site"], metadata={})
        self._check(result)

    def _delete(self, key: str) -> None:
        if self._exists(key):
            self._check(self.vault.delete(key))

    @staticmethod
    def _check(result: Any) -> None:
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise _fail("vault_error", "Jira vault operation failed; no secret details returned.")

    def index(self) -> dict[str, Any]:
        return self._read(INDEX_KEY) or {"version": 0, "default": "legacy", "sites": {}, "history": [], "retired": {}}

    def describe(self) -> dict[str, Any]:
        index = self.index()
        return {
            "version": index["version"],
            "default": index["default"],
            "sites": [{"site": name, **{k: v for k, v in metadata.items() if k != "credential_key"}} for name, metadata in index["sites"].items()],
            "credential_cleanup_pending": sorted(index.get("retired", {})),
            "legacy_site": "legacy (address-book jira_site; resolved on use)",
        }

    def select(self, selector: Any = None) -> str:
        index = self.index()
        name = index["default"] if selector in (None, "") else _name(selector)
        if name != "legacy" and name not in index["sites"]:
            raise _fail("unknown_site", "Unknown site; call sites(operation=list) for bound sites.")
        return name

    @staticmethod
    def key(site: str) -> str:
        return SITE_PREFIX + hashlib.sha256(site.encode()).hexdigest()

    def credentials(self, site: str, binding_key: str | None = None) -> dict[str, Any]:
        with self.lock:
            if site == "legacy":
                config = AppConfigLoader(self.address_book).load()
                value = {
                    "base_url": _api_root(config.base_url),
                    "email": config.email,
                    "api_token": config.api_token,
                    "auth_type": "api_token",
                }
            else:
                current_key = self.credential_key(site)
                if binding_key is not None and binding_key != current_key:
                    raise _fail("binding_changed", "Site binding changed after this job was queued; inspect the target before submitting again.")
                value = self._read(current_key)
                if value is None:
                    raise _fail("not_configured", "Site credentials missing; reconnect this site.")
            if value["auth_type"] == "oauth" and float(value.get("expires_at", 0)) <= time.time() + self.token_refresh_skew_seconds:
                from .oauth import OAuthFlow

                tokens = OAuthFlow(self).token(
                    {
                        "grant_type": "refresh_token",
                        "client_id": value["client_id"],
                        "client_secret": value["client_secret"],
                        "refresh_token": value["refresh_token"],
                    }
                )
                value.update(tokens)
                self._write(self.credential_key(site), value)
            return value

    def credential_key(self, site: str) -> str:
        record = self.index()["sites"].get(site)
        if not record:
            raise _fail("unknown_site", "Unknown site; list current bindings first.")
        return record["credential_key"]

    def _append(self, index: dict[str, Any], operation: str, site: str) -> None:
        index["version"] += 1
        index.setdefault("history", []).append(
            {
                "version": index["version"],
                "operation": operation,
                "site": site,
                "default": index["default"],
                "observed_at": time.time(),
                "binding": {k: v for k, v in index["sites"].get(site, {}).items() if k != "credential_key"},
            }
        )
        self._write(INDEX_KEY, index)

    def _update(self, site: str, value: dict[str, Any], expected: Any, default: bool) -> None:
        index = self.index()
        self._expected(index, expected)
        # An unpublished credential envelope cannot alter an existing binding.
        key = self.key(site) + "__" + secrets.token_hex(16)
        self._write(key, value)
        previous = index["sites"].get(site, {}).get("credential_key")
        index["sites"][site] = {"base_url": value["base_url"], "auth_type": value["auth_type"], "credential_key": key}
        if default:
            index["default"] = site
        if previous:
            index.setdefault("retired", {}).setdefault(site, []).append(previous)
        try:
            self._append(index, "bind", site)
        except JiraServiceError:
            self._delete(key)
            raise

    @staticmethod
    def _expected(index: dict[str, Any], expected: Any) -> None:
        if type(expected) is not int or expected != index["version"]:
            raise _fail("stale_binding", "Read sites first and pass its current expected_version.")

    def manage(self, params: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            operation = params.get("operation")
            if operation == "list":
                return self.describe()
            if operation == "history":
                return {"history": self.index().get("history", [])}
            site = _name(params.get("site"))
            if operation == "bind_api_token":
                self._bind_api_token(site, params)
            elif operation in ("set_default", "disconnect"):
                self._change_binding(site, params)
            else:
                raise ValueError("operation must be list, bind_api_token, set_default or disconnect")
            return self.describe()

    def _bind_api_token(self, site: str, params: dict[str, Any]) -> None:
        if site == "legacy":
            raise ValueError("legacy is reserved for the original address-book site")
        entry = params.get("entry_name")
        if not isinstance(entry, str) or not entry:
            raise ValueError("entry_name is required; use a vault-referenced address-book entry")
        config = AppConfigLoader(self.address_book).load(entry)
        value = {"auth_type": "api_token", "base_url": _api_root(config.base_url), "email": config.email, "api_token": config.api_token, "expires_at": config.expires_at.isoformat(), "scope_note": config.scope_note}
        self._update(site, value, params.get("expected_version"), params.get("set_default") is True)

    def _change_binding(self, site: str, params: dict[str, Any]) -> None:
        index = self.index()
        self._expected(index, params.get("expected_version"))
        if params["operation"] == "set_default":
            self.select(site)
            index["default"] = site
            self._append(index, "set_default", site)
            return
        if site == "legacy":
            raise ValueError("Retire legacy through the original address-book/vault lifecycle")
        record = index["sites"].get(site)
        if record:
            index.setdefault("retired", {}).setdefault(site, []).append(record["credential_key"])
            del index["sites"][site]
            if index["default"] == site:
                index["default"] = "legacy"
            self._append(index, "disconnect", site)
        self._purge_retired(site, index)

    def _purge_retired(self, site: str, index: dict[str, Any]) -> None:
        for key in index.get("retired", {}).get(site, []):
            self._delete(key)
        self._delete(self.key(site) + "__pending")
        # Persist a resumable cleanup checkpoint; a vault failure remains visible.
        if site in index.get("retired", {}):
            del index["retired"][site]
            self._append(index, "credentials_removed", site)

    def oauth(self, params: dict[str, Any]) -> dict[str, Any]:
        from .oauth import OAuthFlow

        with self.lock:
            return OAuthFlow(self).manage(params)


def authorization(value: dict[str, Any]) -> tuple[str, str]:
    if value["auth_type"] == "oauth":
        return "https://api.atlassian.com/ex/jira/" + value["cloud_id"], "Bearer " + value["access_token"]
    encoded = base64.b64encode(f"{value['email']}:{value['api_token']}".encode()).decode()
    return value["base_url"], "Basic " + encoded
