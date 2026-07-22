"""Job Service Implementation.

Provides asynchronous job tracking and retrieval operations.
"""

import logging
from datetime import datetime
from typing import Any, cast

from ananta.core.domain.constants import (
    KEY_ACTION_STATUS,
    KEY_DATA,
    KEY_RESULT,
    STATUS_COMPLETED,
    STATUS_ERROR,
)
from ananta.core.domain.timestamps import to_naive_utc
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.services.job_service.interfaces.public import JobServiceAPI

logger = logging.getLogger(__name__)


class JobService(JobServiceAPI):
    """Job service for tracking and retrieving asynchronous jobs."""

    def __init__(self, state_service: StateServiceProtocol):
        """Initialize job service.

        Args:
            state_service: State service instance for database access
        """
        self.state_service = state_service
        self.namespace = "core"

    def get_latest_job(
        self,
        plugin_name: str | None = None,
        action_name: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve the most recently created asynchronous job.

        Service interface methods receive individual kwargs (action_processor pattern).
        Accepts optional filters for plugin_name, action_name, and status.

        Args:
            plugin_name: Filter to jobs from specific plugin
            action_name: Filter to jobs from specific action
            status: Filter to jobs with specific status

        Returns:
            ActionResult dict with job record or None if not found
        """
        try:
            job = self._query_latest_job(plugin_name, action_name, status)

            return {
                KEY_ACTION_STATUS: STATUS_COMPLETED,
                KEY_DATA: {
                    KEY_RESULT: {
                        "job": job,
                    }
                },
                "actions": [],
            }
        except Exception as e:
            logger.error(f"Failed to retrieve latest job: {e}", exc_info=True)
            return {
                KEY_ACTION_STATUS: STATUS_ERROR,
                KEY_DATA: {
                    KEY_RESULT: {
                        "error": str(e),
                        "job": None,
                    }
                },
                "actions": [],
            }

    def _query_latest_job(
        self,
        plugin_name: str | None,
        action_name: str | None,
        status: str | None,
    ) -> dict[str, Any] | None:
        """Most-recently-created job matching the filters, via read-then-route.

        The exact ``provider_name`` and ``status`` filters are equality and go to
        ``query_state``; the ``provider_name LIKE 'plugin.%'`` prefix-match is NOT
        expressible in the equality/``=ANY``/``is_null`` grammar, so it is applied
        in Python after the read. ``ORDER BY created_at DESC LIMIT 1`` becomes a
        Python max by ``(created_at, id)`` — ``created_at`` coerced to a VALUE
        (never compared as an ISO spelling), with ``id`` as a deterministic
        tie-break (the raw query had no secondary sort, so equal-``created_at``
        ties were nondeterministic). No ``is_deleted`` filter — the raw query had
        none, so the behavior is preserved.
        """
        filters, provider_prefix = self._build_job_filters(plugin_name, action_name, status)
        rows = self._read_jobs(filters)
        if provider_prefix is not None:
            rows = [
                r for r in rows
                if str(r.get("provider_name", "")).startswith(provider_prefix)
            ]
        if not rows:
            return None
        rows.sort(key=lambda r: (to_naive_utc(r["created_at"]), str(r["id"])), reverse=True)
        return rows[0]

    @staticmethod
    def _build_job_filters(
        plugin_name: str | None, action_name: str | None, status: str | None
    ) -> tuple[dict[str, object], str | None]:
        """Split the request into equality filters + an optional Python prefix.

        Returns ``(filters, provider_prefix)``: ``filters`` are grammar-expressible
        equality predicates; ``provider_prefix`` (when set) is the ``plugin.``
        prefix the caller applies in Python (the LIKE branch).
        """
        filters: dict[str, object] = {}
        if status:
            filters["status"] = status
        if plugin_name and action_name:
            filters["provider_name"] = f"{plugin_name}.{action_name}"
            return filters, None
        if plugin_name:
            return filters, f"{plugin_name}."
        return filters, None

    def _read_jobs(self, filters: dict[str, object]) -> list[dict[str, Any]]:
        """Read ``core__job`` rows matching the equality filters (uncapped).

        FAIL-FAST: a non-completed or malformed envelope (or a non-dict record)
        RAISES — ``get_latest_job`` catches it into a ``STATUS_ERROR`` result, so a
        DB error can never masquerade as a valid "no job found". An empty list is
        returned ONLY for a valid completed result with zero matching rows.
        """
        result = self.state_service.query_state(
            self.namespace, {"table": "job", "filters": filters}
        )
        if result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
            raise RuntimeError(f"job query did not complete: {result.get('error')!r}")
        data_obj = result.get(KEY_DATA)
        if not isinstance(data_obj, dict):
            raise RuntimeError(f"job query returned malformed data: {data_obj!r}")
        records = data_obj.get("records")
        if not isinstance(records, list):
            raise RuntimeError(f"job query returned malformed records: {records!r}")
        return [self._validated_job_row(r) for r in records]

    @staticmethod
    def _validated_job_row(record: object) -> dict[str, Any]:
        """Require the fields the prefix-filter + ``(created_at, id)`` sort read.

        FAIL-FAST: a record missing a string ``id`` / string ``provider_name`` /
        coercible ``created_at`` RAISES — it must not be silently dropped by the
        downstream ``provider_name`` prefix-filter (which would read as a valid
        "no job found").
        """
        if not isinstance(record, dict):
            raise RuntimeError(f"job query returned a non-dict record: {record!r}")
        if not isinstance(record.get("id"), str):
            raise RuntimeError(f"job record missing a string 'id': {record!r}")
        if not isinstance(record.get("provider_name"), str):
            raise RuntimeError(f"job record missing a string 'provider_name': {record!r}")
        if not isinstance(record.get("created_at"), (str, datetime)):
            raise RuntimeError(f"job record has a non-coercible 'created_at': {record!r}")
        return cast(dict[str, Any], record)
