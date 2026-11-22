"""The single org binding that predates build 6, read from the address book.

Before build 6 the plugin talked to exactly one org: the address-book entry
named ``salesforce_org`` holds its ``target_org`` (the sf CLI alias or
username every verb was invoked against, via `--target-org`) and its
``instance_host`` (the pinned my-domain host that alias must resolve to). Both
fields are literals; the entry holds no secret, so it is read with ordinary
``resolve`` and nothing is taken from the vault.

Build 6 binds many orgs through the vault index in ``orgs.py``. This entry is
now only the source of the first binding: it is migrated into the index on
first use and kept until that org is disconnected, so an existing solet keeps
working without any operator step.

``instance_host`` is the foreign-target invariant made explicit: the CLI's
alias cache can hold many orgs, and the pin guarantees the plugin only ever
talks to the org that was registered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import (
    ADDRESS_BOOK_ENTRY_NAME,
    ADDRESS_BOOK_FIELD_INSTANCE_HOST,
    ADDRESS_BOOK_FIELD_TARGET_ORG,
    ADDRESS_BOOK_NOT_FOUND_CODE,
    ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
    ERROR_ADDRESS_BOOK_ENTRY_MISSING,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
)


class AppConfigError(RuntimeError):
    """Raised when the ``salesforce_org`` address-book entry cannot supply config."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SalesforceOrgConfig:
    """One org an executor talks to — no secrets, both fields literal."""

    target_org: str
    instance_host: str


class AppConfigLoader:
    """Resolve the pre-build-6 org binding from ``service_interface::address_book_service``.

    The service is duck-typed so this plugin never imports from
    ``default_address_book_plugin``.
    """

    def __init__(self, address_book_service: Any) -> None:
        if address_book_service is None:
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
                "address_book_service is required for AppConfigLoader",
            )
        self._address_book = address_book_service

    def load(self) -> SalesforceOrgConfig:
        """Return the registered binding; a missing entry is an error."""
        config = self.find()
        if config is None:
            raise AppConfigError(ERROR_ADDRESS_BOOK_ENTRY_MISSING, _missing_entry_message())
        return config

    def find(self) -> SalesforceOrgConfig | None:
        """Return the registered binding, or None when no ``salesforce_org`` entry exists.

        Only the address book's own not-found answer means "no entry"; any
        other failure to read it is raised, never taken as absence.
        """
        result = self._address_book.resolve(name=ADDRESS_BOOK_ENTRY_NAME)
        if isinstance(result, dict) and result.get("action_status") == "completed":
            return self._build_config(self._extract_entries(result))
        failure = result.get("error") if isinstance(result, dict) else None
        if isinstance(failure, dict) and failure.get("code") == ADDRESS_BOOK_NOT_FOUND_CODE:
            return None
        code = failure.get("code") if isinstance(failure, dict) else None
        raise AppConfigError(ERROR_ADDRESS_BOOK_NOT_AVAILABLE, f"The address book could not be read for '{ADDRESS_BOOK_ENTRY_NAME}' ({code or 'no result'}).")

    def delete(self) -> None:
        """Remove the entry once the org it names is disconnected."""
        self._address_book.delete(name=ADDRESS_BOOK_ENTRY_NAME)

    def _extract_entries(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        data = result.get("data") or {}
        if not isinstance(data, dict):
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
                f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' returned no data",
            )
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
                f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' has no entries list",
            )
        return [entry for entry in entries if isinstance(entry, dict)]

    def _build_config(self, entries: list[dict[str, Any]]) -> SalesforceOrgConfig:
        target_org = _first_value(entries, ADDRESS_BOOK_FIELD_TARGET_ORG)
        instance_host = _first_value(entries, ADDRESS_BOOK_FIELD_INSTANCE_HOST)
        missing = [
            label
            for label, value in (
                (ADDRESS_BOOK_FIELD_TARGET_ORG, target_org),
                (ADDRESS_BOOK_FIELD_INSTANCE_HOST, instance_host),
            )
            if not value
        ]
        if missing:
            raise AppConfigError(
                ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
                (f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' is missing field_type values: {missing}. It needs target_org (the sf CLI alias or username) and instance_host (the org's my-domain host); or bind the org with bind_org instead."),
            )
        return SalesforceOrgConfig(target_org=target_org, instance_host=instance_host)


def _first_value(entries: list[dict[str, Any]], field_type: str) -> str:
    for entry in entries:
        if entry.get("field_type") == field_type:
            value = entry.get("value", "")
            return value if isinstance(value, str) else ""
    return ""


def _missing_entry_message() -> str:
    return f"Address book entry '{ADDRESS_BOOK_ENTRY_NAME}' not found. Bind an org with bind_org (an org the sf CLI is logged in to) or login."
