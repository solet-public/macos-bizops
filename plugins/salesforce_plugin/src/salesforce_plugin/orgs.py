"""Many Salesforce orgs bound at once — the Google multi-account pattern (build 6).

Contract (the same shape as ``g_suite_plugin.accounts``):

- One binding per org, keyed by its 18-character org id, with an alias that is
  unique among the bindings. Every binding and the default org id live in ONE
  vault value, ``<solet>.salesforce_plugin.orgs_index``, read and written
  whole, so a change is never half-applied. A binding holds no credential: the
  sf CLI keeps each org's session, and the binding records the public facts of
  the org (id, username, pinned instance host, type, Dev Hub flag) plus the
  NAMES of the vault secrets its login used (``org_secrets``).
- Every verb that reaches an org takes an optional ``org``: an alias, a
  username or an org id (18 characters, or its 15-character form). With one
  binding it can be left out; with several the default is used. Every result
  names the org it ran against (``org``, ``org_id``). An ``org`` that matches
  no binding, or several bindings with no default, is ``sf.unknown_org``, and
  the message lists the bound orgs.
- The ``salesforce_org`` address-book entry that predates build 6 becomes the
  first binding the first time any verb runs (``org_service.OrgService``):
  copied, not moved, and deleted only when that org is disconnected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from .constants import (
    ERROR_NOT_CONFIGURED,
    ERROR_ORG_REGISTRY_FAILED,
    ERROR_UNKNOWN_ORG,
    ERROR_VAULT_NOT_AVAILABLE,
    VAULT_KEY_ORGS_INDEX,
    VAULT_TAG_ORGS_INDEX,
)
from .errors import SalesforceServiceError, TypedFamilyError

ORG_TYPES = frozenset({"production", "sandbox", "scratch"})
LOGIN_METHODS = frozenset({"cli", "web", "jwt", "sfdx_url", "access_token", "scratch_create", "sandbox_create"})


@dataclass(frozen=True)
class OrgBinding:
    """One bound org as the index records it."""

    org_id: str
    alias: str
    target_org: str
    username: str
    instance_host: str
    org_type: str = "production"
    is_dev_hub: bool = False
    login_method: str = "cli"
    dev_hub_org_id: str = ""
    # The vault secret (org_secret__<name>) the CLI reads as this org's JWT key.
    jwt_key_secret: str = ""
    # Every org secret this binding's login used; deleted when no binding uses it.
    secret_names: tuple[str, ...] = field(default_factory=tuple)
    migrated_from_legacy: bool = False

    def matches(self, selector: str) -> bool:
        """Alias, username or CLI target (any case), or the org id (18 or 15 characters)."""
        wanted = selector.strip()
        lowered = wanted.lower()
        return lowered in (self.alias.lower(), self.username.lower(), self.target_org.lower()) or wanted in (self.org_id, self.org_id[:15])

    def as_record(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "alias": self.alias,
            "target_org": self.target_org,
            "username": self.username,
            "instance_host": self.instance_host,
            "org_type": self.org_type,
            "is_dev_hub": self.is_dev_hub,
            "login_method": self.login_method,
            "dev_hub_org_id": self.dev_hub_org_id,
            "jwt_key_secret": self.jwt_key_secret,
            "secret_names": list(self.secret_names),
            "migrated_from_legacy": self.migrated_from_legacy,
        }


def _text(row: dict[str, Any], key: str, default: str = "") -> str:
    value = row.get(key)
    return value if isinstance(value, str) and value else default


def _names(value: object) -> tuple[str, ...]:
    return tuple(name for name in value if isinstance(name, str)) if isinstance(value, list) else ()


def _binding_from(row: dict[str, Any]) -> OrgBinding:
    texts = {key: _text(row, key) for key in ("org_id", "alias", "target_org", "username", "instance_host", "dev_hub_org_id", "jwt_key_secret")}
    return OrgBinding(
        **texts,
        org_type=_text(row, "org_type", "production"),
        is_dev_hub=row.get("is_dev_hub") is True,
        login_method=_text(row, "login_method", "cli"),
        secret_names=_names(row.get("secret_names")),
        migrated_from_legacy=row.get("migrated_from_legacy") is True,
    )


@dataclass(frozen=True)
class OrgIndexState:
    """Every binding and the default org id, as one value."""

    bindings: tuple[OrgBinding, ...] = ()
    default_org_id: str = ""

    def default(self) -> OrgBinding | None:
        return next((binding for binding in self.bindings if binding.org_id == self.default_org_id), None)

    def describe(self, bindings: tuple[OrgBinding, ...] | None = None) -> str:
        chosen = self.bindings if bindings is None else bindings
        if not chosen:
            return "none"
        return ", ".join(f"{b.alias} ({b.org_id}, {b.username})" + (" [default]" if b.org_id == self.default_org_id else "") for b in chosen)

    def find(self, selector: str) -> OrgBinding | None:
        return next((binding for binding in self.bindings if binding.matches(selector)), None)

    def select(self, selector: str | None) -> OrgBinding:
        """The binding a call runs as: the named one, the only one, or the default."""
        if selector:
            found = self.find(selector)
            if found is None:
                raise SalesforceServiceError(ERROR_UNKNOWN_ORG, f"No bound Salesforce org matches {selector!r}. Bound orgs: {self.describe()}. Pass one of those aliases, usernames or org ids as `org`, or bind the org first with bind_org or login.")
            return found
        if not self.bindings:
            raise SalesforceServiceError(ERROR_NOT_CONFIGURED, "No Salesforce org is bound. Bind an org the sf CLI is logged in to with bind_org, or log in to one with login.")
        if len(self.bindings) == 1:
            return self.bindings[0]
        chosen = self.default()
        if chosen is None:
            raise SalesforceServiceError(ERROR_UNKNOWN_ORG, f"Several Salesforce orgs are bound and none is the default: {self.describe()}. Pass `org` (an alias, username or org id) or run set_default_org.")
        return chosen

    def with_binding(self, binding: OrgBinding, *, make_default: bool) -> OrgIndexState:
        """Add or refresh one org; the first binding becomes the default.

        No name of the org (its alias, username or CLI target) may select
        another binding, so ``find`` never depends on the order of bindings.
        """
        others = tuple(existing for existing in self.bindings if existing.org_id != binding.org_id)
        _refuse_shared_names(binding, others)
        default = binding.org_id if make_default or not others or not self.default_org_id else self.default_org_id
        return OrgIndexState(bindings=(*others, binding), default_org_id=default)

    def with_default(self, selector: str) -> tuple[OrgIndexState, OrgBinding]:
        chosen = self.select(selector)
        return replace(self, default_org_id=chosen.org_id), chosen

    def without(self, selector: str) -> tuple[OrgIndexState, OrgBinding, tuple[str, ...]]:
        """Remove one org: (new state, removed, secrets no remaining binding uses).

        When the removed org was the default, the first remaining binding
        becomes the default (none when nothing remains).
        """
        removed = self.select(selector)
        remaining = tuple(binding for binding in self.bindings if binding.org_id != removed.org_id)
        default_org_id = self.default_org_id
        if default_org_id == removed.org_id:
            default_org_id = remaining[0].org_id if remaining else ""
        return OrgIndexState(bindings=remaining, default_org_id=default_org_id), removed, _orphaned_secrets(removed, remaining)


def _refuse_shared_names(binding: OrgBinding, others: tuple[OrgBinding, ...]) -> None:
    """Refuse a binding whose alias, username or CLI target already selects another bound org."""
    for name in dict.fromkeys(name for name in (binding.alias, binding.username, binding.target_org) if name):
        clash = next((existing for existing in others if existing.matches(name)), None)
        if clash is None:
            continue
        if name == binding.alias:
            raise TypedFamilyError(f"alias {name!r} already names bound org {clash.alias} ({clash.org_id}, {clash.username}); choose another alias")
        raise TypedFamilyError(f"{name!r}, this org's username or CLI target, is the alias of bound org {clash.alias} ({clash.org_id}); give that binding another alias first (bind_org with alias)")


def _orphaned_secrets(removed: OrgBinding, remaining: tuple[OrgBinding, ...]) -> tuple[str, ...]:
    """The removed binding's secrets that no remaining binding uses."""
    still_used = {name for binding in remaining for name in (*binding.secret_names, binding.jwt_key_secret)}
    return tuple(name for name in dict.fromkeys((*removed.secret_names, removed.jwt_key_secret)) if name and name not in still_used)


class OrgIndex:
    """The vault value that holds every binding and the default."""

    def __init__(self, vault: Any) -> None:
        self._vault = vault

    def load(self) -> OrgIndexState:
        value = self._stored_value()
        return OrgIndexState() if value is None else _parse_index(value)

    def _stored_value(self) -> str | None:
        """The index text, or None when it was never written; a read failure is an error, never absence."""
        vault = self._require_vault()
        exists = vault.exists(VAULT_KEY_ORGS_INDEX)
        if not _completed(exists):
            raise SalesforceServiceError(ERROR_ORG_REGISTRY_FAILED, "The vault could not say whether the Salesforce org index exists.")
        if not (exists.get("data") or {}).get("exists"):
            return None
        result = vault.retrieve(VAULT_KEY_ORGS_INDEX)
        value = (result.get("data") or {}).get("value") if _completed(result) else None
        if not isinstance(value, str):
            raise SalesforceServiceError(ERROR_ORG_REGISTRY_FAILED, "The Salesforce org index could not be read from the vault.")
        return value

    def save(self, state: OrgIndexState) -> None:
        vault = self._require_vault()
        value = json.dumps({"orgs": [binding.as_record() for binding in state.bindings], "default_org_id": state.default_org_id}, separators=(",", ":"))
        exists = vault.exists(VAULT_KEY_ORGS_INDEX)
        if _completed(exists) and (exists.get("data") or {}).get("exists"):
            result = vault.rotate(VAULT_KEY_ORGS_INDEX, value)
        else:
            result = vault.store(key=VAULT_KEY_ORGS_INDEX, value=value, tags=[VAULT_TAG_ORGS_INDEX], metadata={})
        if not _completed(result):
            raise SalesforceServiceError(ERROR_ORG_REGISTRY_FAILED, "The Salesforce org index could not be written to the vault.")

    def _require_vault(self) -> Any:
        if self._vault is None:
            raise SalesforceServiceError(ERROR_VAULT_NOT_AVAILABLE, "The vault service is not available to salesforce_plugin, so bound orgs cannot be read or written.")
        return self._vault


def _parse_index(value: str) -> OrgIndexState:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        raise SalesforceServiceError(ERROR_ORG_REGISTRY_FAILED, "The Salesforce org index in the vault is not valid JSON.") from None
    if not isinstance(payload, dict):
        raise SalesforceServiceError(ERROR_ORG_REGISTRY_FAILED, "The Salesforce org index in the vault is not an object.")
    rows = payload.get("orgs")
    bindings = tuple(_binding_from(row) for row in rows if isinstance(row, dict)) if isinstance(rows, list) else ()
    return OrgIndexState(bindings=bindings, default_org_id=_text(payload, "default_org_id"))


def _completed(result: Any) -> bool:
    return isinstance(result, dict) and result.get("action_status") == "completed"


def org_selector(params: dict[str, Any]) -> str | None:
    """The caller's optional `org` (alias, username or org id)."""
    value = params.get("org")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TypedFamilyError("org must be a non-empty string: an alias, username or org id of a bound org")
    return value.strip()
