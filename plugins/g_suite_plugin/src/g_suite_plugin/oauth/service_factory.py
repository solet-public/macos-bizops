"""Per-product Google API client builders.

The token store is the single owner of refresh logic: :meth:`GoogleServiceFactory`
asks it for a fresh access token on every build and wraps it in a minimal
``Credentials``. No refresh_token is handed to the API client, so the client
never refreshes behind the token store's back — one refresh owner, no drift.

``cache_discovery=False`` avoids the legacy file-cache path (which reaches for
the deprecated ``oauth2client``); ``static_discovery=True`` uses the discovery
documents bundled inside ``google-api-python-client`` so no discovery HTTP call
is made at build time.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build, build_from_document

from .. import calendar_discovery
from ..constants import ACCOUNT_TIER_CONSUMER, ACCOUNT_TIER_UNKNOWN, ACCOUNT_TIER_WORKSPACE_USER, CONSUMER_GMAIL_DOMAINS
from ..workspace_registry import discovery_document
from .token_store import TokenStore

_logger = logging.getLogger(__name__)


class GoogleServiceFactory:
    """Builds Gmail / Drive / Sheets service clients from vaulted tokens."""

    def __init__(self, token_store: TokenStore) -> None:
        self._tokens = token_store

    def _credentials(self) -> Credentials:
        return Credentials(token=self._tokens.get_access_token())

    def _build(self, service_name: str, version: str) -> Any:
        return build(
            service_name,
            version,
            credentials=self._credentials(),
            cache_discovery=False,
            static_discovery=True,
        )

    def credential_facts(self) -> dict[str, Any]:
        return self._tokens.credential_facts()

    def workspace_api(self, product: str) -> Any:
        return build_from_document(json.dumps(discovery_document(product)), credentials=self._credentials())

    def gmail(self) -> Any:
        return self._build("gmail", "v1")

    def calendar(self) -> Any:
        """Calendar client, with the shipped discovery overlay and this account's token."""
        return calendar_discovery.build_service(self._tokens.get_access_token())

    def drive(self) -> Any:
        return self._build("drive", "v3")

    def sheets(self) -> Any:
        return self._build("sheets", "v4")

    def docs(self) -> Any:
        return self._build("docs", "v1")

    def slides(self) -> Any:
        return self._build("slides", "v1")


def account_tier(email: str) -> str:
    """consumer for gmail.com / googlemail.com, else workspace_user.

    Every Gmail mailbox address is either a consumer Gmail address or an
    address on a Workspace domain, so the Gmail profile address alone tells the
    tier with no extra OAuth scope. Workspace-admin detection needs the Admin
    SDK scope and arrives with an admin build; until then admins read as
    workspace_user.
    """
    domain = email.rpartition("@")[2].lower()
    if not domain:
        return ACCOUNT_TIER_UNKNOWN
    return ACCOUNT_TIER_CONSUMER if domain in CONSUMER_GMAIL_DOMAINS else ACCOUNT_TIER_WORKSPACE_USER


def probe_account(access_token: str) -> tuple[str, str]:
    """Read (lower-cased email, tier) for a fresh token.

    Gmail users.getProfile first; its address also gives the tier. When that
    fails (for example Gmail unticked on granular consent) the Drive about.get
    user email identifies the account and the tier is recorded 'unknown', so a
    failed Gmail probe never refuses a connect or a legacy migration that
    another granted product can identify.
    """
    credentials = Credentials(token=access_token)
    try:
        gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False, static_discovery=True)
        email = str(gmail.users().getProfile(userId="me").execute().get("emailAddress") or "").lower()
    except Exception as exc:  # noqa: BLE001 — the Drive fallback below identifies the account; the cause is logged
        _logger.warning("Gmail profile probe failed (%s); identifying the account through Drive, tier unknown", type(exc).__name__)
        email = ""
    if email:
        return email, account_tier(email)
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False, static_discovery=True)
    about = drive.about().get(fields="user(emailAddress)").execute()
    email = str((about.get("user") or {}).get("emailAddress") or "").lower()
    if not email:
        raise ValueError("Neither the Gmail profile nor the Drive account reported an email address")
    return email, ACCOUNT_TIER_UNKNOWN
