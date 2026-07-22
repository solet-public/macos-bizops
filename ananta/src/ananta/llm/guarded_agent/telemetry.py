"""Session telemetry writer for agent plugins.

TelemetryWriter provides consistent session artifact storage for both
Claude Code and Codex agent plugins. Neither SDK persists session
artifacts in our required format, so this component handles:

- Text output chunks (chunks.jsonl)
- Raw SDK events (events.jsonl)
- Guard alerts (alerts.jsonl)
- Tool invocations (tool_calls.jsonl)
- Session manifest (manifest.json)
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO


def _now_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(UTC).isoformat()


class TelemetryWriter:
    """Session telemetry writer for all agent plugins.

    Creates and manages session artifact files in a structured directory
    layout under APP_HOME/data/plugin_data/<plugin_name>/<session_id>/.

    Files are written incrementally using JSONL format for streaming writes.
    """

    def __init__(
        self,
        session_id: str,
        plugin_name: str,
        app_home: str,
        backend_session_id: str | None = None,
    ) -> None:
        """Initialize telemetry writer for a session.

        Args:
            session_id: Ananta-generated session identifier
            plugin_name: Name of the agent plugin
            app_home: Ananta home directory path
            backend_session_id: Optional SDK-specific session ID
        """
        self._session_id = session_id
        self._plugin_name = plugin_name
        self._backend_session_id = backend_session_id
        self._app_home = Path(app_home)

        # Create session directory
        self._data_dir = self._app_home / "data" / "plugin_data" / plugin_name / session_id
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Open JSONL files for streaming writes
        # These are managed as instance attributes because:
        # 1. Files are written incrementally throughout session lifetime
        # 2. Cleanup is handled by finalize() or __del__ as failsafe
        self._chunks_file: TextIO = (self._data_dir / "chunks.jsonl").open("a")
        self._events_file: TextIO = (self._data_dir / "events.jsonl").open("a")
        self._alerts_file: TextIO = (self._data_dir / "alerts.jsonl").open("a")
        self._tool_calls_file: TextIO = (self._data_dir / "tool_calls.jsonl").open("a")
        self._files_closed = False
        self._chunk_seq = 0
        self._event_seq = 0

        # Initialize manifest
        self._manifest: dict[str, Any] = {
            "session_id": session_id,
            "backend_session_id": backend_session_id,
            "plugin": plugin_name,
            "started_at": _now_iso(),
            "completed_at": None,
        }

    def write_chunk(self, text: str) -> None:
        """Write a text chunk.

        Args:
            text: Text content to record
        """
        entry = {"seq": self._chunk_seq, "ts": _now_iso(), "text": text}
        self._chunks_file.write(json.dumps(entry) + "\n")
        self._chunks_file.flush()
        self._chunk_seq += 1

    def write_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Write a raw SDK event.

        Used primarily for Codex JSON Lines events.

        Args:
            event_type: Type of event (e.g., "item.completed")
            data: Full event data dictionary
        """
        entry = {
            "seq": self._event_seq,
            "ts": _now_iso(),
            "type": event_type,
            "data": data,
        }
        self._events_file.write(json.dumps(entry) + "\n")
        self._events_file.flush()
        self._event_seq += 1

    def write_alert(self, phrase: str, context: str, severity: str) -> None:
        """Write a guard alert.

        Args:
            phrase: The watch phrase that triggered the alert
            context: Surrounding content where phrase was found
            severity: Alert severity ("warn" or "terminate")
        """
        entry = {
            "phrase": phrase,
            "context": context,
            "severity": severity,
            "ts": _now_iso(),
        }
        self._alerts_file.write(json.dumps(entry) + "\n")
        self._alerts_file.flush()

    def write_tool_call(
        self,
        name: str,
        input_data: dict[str, Any],
        tool_id: str | None = None,
    ) -> None:
        """Write a tool invocation.

        Args:
            name: Name of the tool invoked
            input_data: Tool input parameters
            tool_id: Optional SDK-provided tool ID
        """
        entry = {
            "name": name,
            "input": input_data,
            "tool_id": tool_id,
            "ts": _now_iso(),
        }
        self._tool_calls_file.write(json.dumps(entry) + "\n")
        self._tool_calls_file.flush()

    def _close_files(self) -> None:
        """Close all streaming files if not already closed."""
        if self._files_closed:
            return
        self._chunks_file.close()
        self._events_file.close()
        self._alerts_file.close()
        self._tool_calls_file.close()
        self._files_closed = True

    def __del__(self) -> None:
        """Cleanup failsafe - close files on garbage collection."""
        self._close_files()

    def __enter__(self) -> "TelemetryWriter":
        """Context manager entry - returns self."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Context manager exit - close files."""
        self._close_files()

    def finalize(
        self,
        metrics: dict[str, Any] | None = None,
        interrupted: bool = False,
        interrupted_on: str | None = None,
        error: str | None = None,
    ) -> None:
        """Finalize session telemetry.

        Closes all streaming files and writes the final manifest.

        Args:
            metrics: Execution metrics to record
            interrupted: Whether execution was interrupted
            interrupted_on: Reason for interruption
            error: Error message if execution failed
        """
        # Close streaming files
        self._close_files()

        # Update manifest
        self._manifest["completed_at"] = _now_iso()
        self._manifest["interrupted"] = interrupted
        self._manifest["interrupted_on"] = interrupted_on
        self._manifest["error"] = error
        self._manifest["metrics"] = metrics or {}

        # Write manifest
        manifest_path = self._data_dir / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(self._manifest, f, indent=2)

    def get_data_dir(self) -> Path:
        """Get the session data directory path."""
        return self._data_dir

    @property
    def backend_session_id(self) -> str | None:
        """Get the backend session ID."""
        return self._backend_session_id

    @backend_session_id.setter
    def backend_session_id(self, value: str) -> None:
        """Set the backend session ID.

        Updates both the instance and manifest.

        Args:
            value: Backend session ID
        """
        self._backend_session_id = value
        self._manifest["backend_session_id"] = value
