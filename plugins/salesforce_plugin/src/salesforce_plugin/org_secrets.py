"""Per-org login secrets: vault values the operator stores, used only inside the call that needs them.

USER words, clause 22 of ``rul_e9a942a3`` (2026-11-09): "There should be no
opportunity for passwords to be in logs, and no redaction should be needed."
So a secret is never a verb parameter (the job ledger stores every parameter),
never part of argv, never in a result and never in a log line, and nothing
here masks anything. The operator stores each secret once, without it passing
through a conversation, with ``service_interface::vault_service::store_from_file``
under ``<solet>.salesforce_plugin.org_secret__<name>``; a verb names only
``<name>``. The value is read from the vault inside the call that uses it and
handed to the sf CLI the way the CLI documents for unattended use:

- a JWT private key as a file (``--jwt-key-file``). The CLI records the file's
  path in its own auth file and reads the file again whenever it refreshes the
  org's session (``@salesforce/core`` ``authInfo.js``: ``refreshFn`` calls
  ``initAuthOptions``, which signs a new JWT from ``privateKey``). The file is
  therefore written at a fixed path derived from the secret's name, mode 0600
  in a 0700 directory, only while a CLI call that may reach an org using that
  key runs (that org's own calls, ``org list``, and ``org display`` before an
  org is identified), and removed when the call ends. A scratch org or sandbox created from
  a JWT-bound org is authorized with the same key and path, so its binding
  names the same secret;
- an SFDX auth URL on standard input (``--sfdx-url-stdin -``);
- an access token in the ``SF_ACCESS_TOKEN`` environment variable of that one
  call (``org login access-token --no-prompt``);
- a connected app's client secret on standard input, which ``org create
  scratch --client-id`` reads from its own prompt.

The vault keys live in this plugin's own namespace, so the plugin can also
delete them when the last org that uses one is disconnected.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import ERROR_NOT_CONFIGURED, ERROR_VAULT_NOT_AVAILABLE, VAULT_KEY_ORG_SECRET_PREFIX
from .errors import SalesforceServiceError, TypedFamilyError

logger = logging.getLogger(__name__)

# A secret's name: what follows org_secret__ in its vault key.
_SECRET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def secret_name(value: object, param: str) -> str:
    """Validate a caller-supplied secret name (never a secret value)."""
    if not isinstance(value, str) or _SECRET_NAME.fullmatch(value) is None:
        raise TypedFamilyError(f"{param} must be the name of a vault secret stored under {VAULT_KEY_ORG_SECRET_PREFIX}<name>: letters, digits, '_', '.' or '-', at most 128 characters")
    return value


def secret_key(name: str) -> str:
    """The full vault key of a named org secret."""
    return f"{VAULT_KEY_ORG_SECRET_PREFIX}{name}"


@dataclass(frozen=True)
class JwtKeyFile:
    """A vault-held JWT key that is on disk only while a CLI call that may need it runs.

    An ``offered`` key goes to a call that may reach any bound org (``org list``,
    or ``org display`` before the org is known). When the vault no longer holds
    it, that call goes on without it and a warning names the vault key, so one
    org's missing key fails only that org's own calls, never another org's
    (operator ruling rul_becb4bed: missing data does not block unrelated work).
    """

    path: Path
    read: Callable[[], str]
    offered: bool = False

    @contextmanager
    def present(self) -> Generator[Path]:
        """Write the key (0600, directory 0700), yield its path, remove it whatever happens."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        value = self._value()
        if value is None:
            yield self.path
            return
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value if value.endswith("\n") else value + "\n")
            yield self.path
        finally:
            self.path.unlink(missing_ok=True)

    def _value(self) -> str | None:
        """The key, or None for an offered key the vault no longer holds."""
        try:
            return self.read()
        except SalesforceServiceError as exc:
            if not self.offered or exc.code != ERROR_NOT_CONFIGURED:
                raise
            logger.warning("%s This call goes on without that key; only the org that uses it can fail.", exc)
            return None


class OrgSecrets:
    """Reads and deletes the operator-stored login secrets of bound orgs."""

    def __init__(self, vault: Any, keys_dir: Path) -> None:
        self._vault = vault
        self._keys_dir = keys_dir

    def read(self, name: str) -> str:
        """The secret's value, for the one call that uses it."""
        vault = self._require_vault()
        result = vault.retrieve(f"{VAULT_KEY_ORG_SECRET_PREFIX}{name}")
        data = result.get("data") if isinstance(result, dict) and result.get("action_status") == "completed" else None
        value = data.get("value") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise SalesforceServiceError(ERROR_NOT_CONFIGURED, f"The vault holds no value under {secret_key(name)}. Store it with service_interface::vault_service::store_from_file under that key, then run this again.")
        return value.strip()

    def delete(self, name: str) -> bool:
        """Remove a secret once no bound org uses it; True when the vault confirmed it."""
        vault = self._require_vault()
        result = vault.delete(f"{VAULT_KEY_ORG_SECRET_PREFIX}{name}")
        return isinstance(result, dict) and result.get("action_status") == "completed"

    def jwt_key_file(self, name: str, *, offered: bool = False) -> JwtKeyFile:
        """The fixed-path key file for one JWT secret (the path the CLI records at login)."""
        digest = hashlib.sha256(secret_key(name).encode("utf-8")).hexdigest()[:24]
        return JwtKeyFile(path=self._keys_dir / f"{digest}.key", read=lambda: self.read(name), offered=offered)

    def sweep(self) -> int:
        """Remove key files a crashed call left behind; returns how many were removed."""
        if not self._keys_dir.is_dir():
            return 0
        removed = 0
        for leftover in self._keys_dir.glob("*.key"):
            leftover.unlink(missing_ok=True)
            removed += 1
        return removed

    def _require_vault(self) -> Any:
        if self._vault is None:
            raise SalesforceServiceError(ERROR_VAULT_NOT_AVAILABLE, "The vault service is not available to salesforce_plugin, so org secrets cannot be read.")
        return self._vault
