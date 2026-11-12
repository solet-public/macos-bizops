"""Owned external-secret-store fixture for the fast-return census.

All state is synthetic and in memory; no host vault provider is opened.
"""

from __future__ import annotations

import itertools
import json
import time
from typing import Any


class CensusVault:
    """Owned in-memory external-store fixture; never reads host credentials.

    Only explicitly stored synthetic values exist. The two private database
    passwords preserve the provisioner's trust-authenticated startup path.
    """

    def __init__(self, identity: str) -> None:
        self.identity = identity
        self.keys: list[str] = []
        self._values: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, dict[str, Any]] = {}
        self._client_ids = itertools.count(1)
        self._unlocked = True
        for suffix in ("postgres_state_management_plugin.db_password", "pgvector_service_plugin.password"):
            self.store(f"{identity}.{suffix}", "census-synthetic-value")

    @staticmethod
    def _ok(data: dict[str, Any]) -> dict[str, Any]:
        # Copy metadata too: a caller's readback cannot mutate stored state.
        return {"action_status": "completed", "data": json.loads(json.dumps(data)), "error": None, "actions": []}

    @staticmethod
    def _error(code: str) -> dict[str, Any]:
        return {"action_status": "error", "data": {}, "error": {"type": "VaultError", "code": code, "message": code}, "actions": []}

    def _access_error(self, key: str) -> str | None:
        if not self._unlocked:
            return "vault.locked"
        return None if key.strip() else "vault.invalid_key"

    def store(self, key: str, value: str, tags: Any = None, metadata: dict[str, str] | None = None, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        error = self._access_error(key)
        if error:
            return self._error(error)
        if key in self._values:
            return self._error("vault.key_exists")
        self._values[key] = {"key": key, "value": value, "tags": list(tags or []), "metadata": dict(metadata or {}), "version": 1, "created_at": None, "updated_at": None}
        return self._ok({"key": key, "version": 1, "message": "Secret stored"})

    def retrieve(self, key: str, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        self.keys.append(key)
        error = self._access_error(key)
        if error or key not in self._values:
            return self._error(error or "vault.not_found")
        return self._ok({"key": key, "value": self._values[key]["value"]})

    def delete(self, key: str, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        error = self._access_error(key)
        if error or key not in self._values:
            return self._error(error or "vault.not_found")
        del self._values[key]
        return self._ok({"key": key, "message": "Secret deleted"})

    def exists(self, key: str, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        error = self._access_error(key)
        return self._error(error) if error else self._ok({"key": key, "exists": key in self._values})

    def rotate(self, key: str, new_value: str, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        error = self._access_error(key)
        if error or key not in self._values:
            return self._error(error or "vault.not_found")
        self._values[key]["value"] = new_value
        self._values[key]["version"] += 1
        return self._ok({"key": key, "version": self._values[key]["version"], "message": "Secret rotated"})

    def rename(self, old_key: str, new_key: str, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        error = self._access_error(old_key) or self._access_error(new_key)
        if error or old_key not in self._values:
            return self._error(error or "vault.not_found")
        if old_key != new_key:
            if new_key in self._values:
                return self._error("vault.key_exists")
            self._values[new_key] = self._values.pop(old_key) | {"key": new_key}
        return self._ok({"old_key": old_key, "new_key": new_key, "message": "Secret renamed"})

    def store_random(self, key: str, byte_length: int = 32, tags: Any = None, metadata: dict[str, str] | None = None, *, call_context: Any = None) -> dict[str, Any]:
        import base64
        import hashlib
        import secrets

        if isinstance(byte_length, bool) or byte_length < 16:
            return self._error("vault.invalid_byte_length")
        value = base64.b64encode(secrets.token_bytes(byte_length)).decode("ascii")
        result = self.store(key, value, tags, metadata, call_context=call_context)
        if result["action_status"] == "completed":
            result["data"].update(byte_length=byte_length, plaintext_fingerprint=hashlib.sha256(value.encode()).hexdigest()[:16])
        return result

    def status(self, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        return self._ok({"initialized": True, "unlocked": self._unlocked, "backend": "census-owned-memory"})

    def unlock(self, passphrase: str, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        if passphrase != "census-synthetic-passphrase":
            return self._error("vault.invalid_passphrase")
        self._unlocked = True
        return self._ok({"unlocked": True, "message": "Vault unlocked"})

    def lock(self, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        self._unlocked = False
        return self._ok({"unlocked": False, "message": "Vault locked"})

    def oauth_client_register(self, client_name: str, scopes: Any = None, redirect_uris: Any = None, grant_types: Any = None, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        if not client_name or not self._unlocked:
            return self._error("vault.oauth_client_invalid_name" if self._unlocked else "vault.locked")
        grants = list(grant_types) if grant_types is not None else ["authorization_code", "refresh_token"]
        if set(grants) - {"authorization_code", "client_credentials", "refresh_token"}:
            return self._error("vault.oauth_client_invalid_name")
        client_id = f"{self.identity}.synthetic-client-{next(self._client_ids)}"
        value = {"client_id": client_id, "client_name": client_name, "scopes": list(scopes or ["mcp:read", "mcp:write"]), "redirect_uris": list(redirect_uris or []), "grant_types": grants, "operator_approved": True, "created_at": time.time()}
        self._clients[client_id] = value
        return self._ok(value | {"client_secret": "census-synthetic-client-secret"})

    def oauth_client_revoke(self, client_id: str, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        if not self._unlocked:
            return self._error("vault.locked")
        return self._ok({"client_id": client_id, "removed": int(self._clients.pop(client_id, None) is not None)})

    def oauth_client_list(self, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        if not self._unlocked:
            return self._error("vault.locked")
        return self._ok({"clients": sorted(self._clients.values(), key=lambda client: client["created_at"])})

    def oauth_client_add_redirect_uri(self, client_id: str, redirect_uri: str, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        if not self._unlocked:
            return self._error("vault.locked")
        if client_id not in self._clients:
            return self._error("vault.oauth_client_not_found")
        if not redirect_uri:
            return self._error("vault.oauth_client_invalid_name")
        uris = self._clients[client_id]["redirect_uris"]
        added = redirect_uri not in uris
        if added:
            uris.append(redirect_uri)
        return self._ok({"client_id": client_id, "redirect_uris": uris, "added": added})

    def list(self, tag: str | None = None, *, call_context: Any = None) -> dict[str, Any]:
        del call_context
        if not self._unlocked:
            return self._error("vault.locked")
        secrets = [{key: value for key, value in entry.items() if key != "value"} for _name, entry in sorted(self._values.items()) if tag is None or tag in entry["tags"]]
        return self._ok({"secrets": secrets, "count": len(secrets)})
