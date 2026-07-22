import json
import logging
from datetime import UTC, datetime, timedelta

from ananta.core.domain.constants import SESSION_TIMEOUT_MINUTES
from ananta.core.domain.enums import SessionStatus
from ananta.core.domain.types import ActionResult
from ananta.error_handling import FrameworkError
from ananta.services.state_service import StateService

from ..interfaces import ISessionManager

logger = logging.getLogger(__name__)


class SessionManager(ISessionManager):
    def __init__(
        self,
        state_service: StateService,
        session_timeout_minutes: int = SESSION_TIMEOUT_MINUTES,
    ):
        self.state_service = state_service
        self.session_timeout_minutes = session_timeout_minutes
        self.current_session_id: str | None = None

    def create_session(
        self, namespace: str, context_type: str, metadata: dict[str, object] | None = None
    ) -> str:
        now = datetime.now(UTC)
        session_data = {
            "namespace": namespace,
            "context_type": context_type,
            "metadata": json.dumps(metadata or {}),
            "status": SessionStatus.ACTIVE.value,
            "started_at": now.isoformat(),
            "last_activity": now.isoformat(),
            "expires_at": (now + timedelta(minutes=self.session_timeout_minutes)).isoformat(),
        }

        result = self.state_service.write_state(
            namespace="core", data={"table": "sessions", "record": session_data}
        )

        if result.get("action_status") != "completed":
            raise FrameworkError(
                message="Failed to create session in StateService",
                error_code="session_manager.session_creation_failed",
                details={"result": result},
            )

        # Type narrow the nested data structure
        data = result.get("data")
        if not isinstance(data, dict):
            raise FrameworkError(
                message="StateService returned invalid or missing data",
                error_code="session_manager.invalid_result_structure",
                details={"result": result},
            )

        result_data = data.get("result")
        if not isinstance(result_data, dict):
            raise FrameworkError(
                message="StateService result data is not a dict",
                error_code="session_manager.invalid_result_data",
                details={"result": result},
            )

        session_id = result_data.get("generated_id")
        if not isinstance(session_id, str):
            raise FrameworkError(
                message="StateService did not return generated_id for session",
                error_code="session_manager.missing_generated_session_id",
                details={"result": result},
            )

        self.current_session_id = session_id
        logger.debug(
            f"Session created: id={session_id}, namespace={namespace}, context_type={context_type}"
        )
        return session_id

    def get_session_metadata(self, session_id: str) -> dict[str, object]:
        result = self.state_service.read_state(
            namespace="core", query={"table": "sessions", "filters": {"id": session_id}}
        )

        if result:
            metadata_str = result.get("metadata", "{}")
            try:
                # Ensure metadata_str is a string before JSON parsing
                if metadata_str is None:
                    metadata_str = "{}"
                elif not isinstance(metadata_str, str):
                    metadata_str = str(metadata_str)
                metadata_raw = json.loads(metadata_str)
                # Type narrow json.loads result to dict[str, object]
                if not isinstance(metadata_raw, dict):
                    return {}
                return metadata_raw
            except json.JSONDecodeError:
                return {}

        return {}

    def validate_session(self, session_id: str) -> bool:
        result = self.state_service.read_state(
            namespace="core", query={"table": "sessions", "filters": {"id": session_id}}
        )

        if not result:
            return False

        status = result.get("status")
        if status != SessionStatus.ACTIVE.value:
            return False

        expires_at_str = result.get("expires_at")
        if expires_at_str:
            try:
                # Ensure expires_at_str is a string before processing
                if expires_at_str is None:
                    return False
                expires_at_str_safe = (
                    str(expires_at_str) if not isinstance(expires_at_str, str) else expires_at_str
                )
                expires_at = datetime.fromisoformat(expires_at_str_safe.replace("Z", "+00:00"))
                if datetime.now(UTC) > expires_at:
                    return False
            except Exception:
                return False

        return True

    def _extract_sessions_list(self, result: ActionResult) -> list[object]:
        """Extract sessions list from ActionResult data structure."""
        data = result.get("data")
        if not isinstance(data, dict):
            return []

        sessions_raw = data.get("result") or data.get("results") or data
        if not isinstance(sessions_raw, list):
            return []

        return sessions_raw

    def _is_session_expired(self, expires_at_str: str, current_time: datetime) -> bool:
        """Check if session has expired based on expires_at timestamp."""
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        return current_time > expires_at

    def _mark_session_expired(self, session_id: object) -> None:
        """Mark a single session as expired in state service."""
        self.state_service.update_state(
            namespace="core",
            query={"table": "sessions", "filters": {"id": session_id}},
            updates={"status": SessionStatus.EXPIRED.value},
        )

    def cleanup_expired_sessions(self) -> int:
        result = self.state_service.read_state(
            namespace="core", query={"table": "sessions", "filters": {}}
        )

        sessions_list = self._extract_sessions_list(result)
        if not sessions_list:
            return 0

        expired_count = 0
        current_time = datetime.now(UTC)

        for session_obj in sessions_list:
            if not isinstance(session_obj, dict):
                continue

            expires_at_str = session_obj.get("expires_at")
            if not isinstance(expires_at_str, str):
                continue

            try:
                if self._is_session_expired(expires_at_str, current_time):
                    self._mark_session_expired(session_obj.get("id"))
                    expired_count += 1
            except Exception:
                pass

        return expired_count

    def update_session_activity(self, session_id: str) -> bool:
        """Update last_activity and extend expires_at for a session.

        Called on every interaction to keep session alive within timeout window.

        Args:
            session_id: Session ID to update

        Returns:
            True if update succeeded, False otherwise
        """
        now = datetime.now(UTC)
        new_expires_at = now + timedelta(minutes=self.session_timeout_minutes)

        result = self.state_service.update_state(
            namespace="core",
            query={"table": "sessions", "filters": {"id": session_id}},
            updates={
                "last_activity": now.isoformat(),
                "expires_at": new_expires_at.isoformat(),
            },
        )

        if result and result.get("action_status") == "completed":
            return True

        logger.error(f"Failed to update session activity: {session_id}")
        return False

    def get_active_session_for_namespace(self, namespace: str) -> str | None:
        """Find an active session for the given namespace.

        Used by IO interfaces to reuse existing sessions within timeout window.

        Args:
            namespace: Plugin namespace to find session for

        Returns:
            Session ID if active session exists, None otherwise
        """
        result = self.state_service.read_state(
            namespace="core",
            query={
                "table": "sessions",
                "filters": {
                    "namespace": namespace,
                    "status": SessionStatus.ACTIVE.value,
                },
            },
        )

        if result:
            session_id = result.get("id")
            if isinstance(session_id, str):
                # Verify it's not expired
                if self.validate_session(session_id):
                    return session_id

        return None

    def get_current_session_id(self) -> str | None:
        return self.current_session_id

    def set_current_session_id(self, session_id: str) -> None:
        self.current_session_id = session_id
