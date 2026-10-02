"""Result helper constructors for PostgreSQL state plugin."""

from datetime import UTC, datetime

from ananta.core.domain.enums import ActionStatus, ErrorSeverity
from ananta.core.domain.types import ActionResult, ErrorDetail


def create_error_result(
    message: str,
    error_code: str = "plugin.error",
    details: dict[str, object] | None = None,
) -> ActionResult:
    stamp = datetime.now(UTC).isoformat()
    error: ErrorDetail = {
        "type": "plugin_error",
        "code": error_code,
        "message": message,
        "details": details or {},
        "severity": ErrorSeverity.ERROR.value,
        "timestamp": stamp,
    }
    return {
        "action_status": ActionStatus.ERROR.value,
        "data": {},
        "actions": [],
        "error": error,
        "timestamp": stamp,
    }


def create_success_result(data: dict[str, object]) -> ActionResult:
    return {
        "action_status": ActionStatus.COMPLETED.value,
        "data": data or {},
        "actions": [],
        "error": None,
        "timestamp": datetime.now(UTC).isoformat(),
    }
