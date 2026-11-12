"""Follow a known job receipt without waiting or repeating the operation."""

from typing import Any, Protocol

from .local_cli.client import queued_job_id


def job_to_follow(payload: dict[str, Any], follow_job: bool) -> str | None:
    return queued_job_id(payload) if follow_job else None


class JobReader(Protocol):
    def get_job(self, job_id: str) -> dict[str, object]: ...

    def get_job_payload(self, job_id: str, payload_type: str = "request") -> dict[str, object]: ...


def _data(response: object, key: str) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("action_status") != "completed":
        return {}
    data = response.get("data")
    if not isinstance(data, dict):
        return {}
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _outcomes(reader: JobReader, job_id: str, status: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        outcome = _data(reader.get_job_payload(job_id, "result"), "payload")
        error = _data(reader.get_job_payload(job_id, "error"), "payload") if status != "completed" else {}
        return outcome, error
    except Exception:
        return {}, {}


def follow_job_receipt(payload: dict[str, Any], job_id: str, reader: JobReader | None) -> dict[str, Any]:
    """Return the current job outcome or an advisory if it cannot be read."""
    result = payload.copy()
    job_read = {"process_key": "service_interface::job_service::get_job", "arguments": {"job_id": job_id}}
    try:
        job = _data(reader.get_job(job_id), "job") if reader is not None else {}
    except Exception:
        job = {}
    status = job.get("status")
    if not isinstance(status, str) or not status:
        result.update(job={"job_id": job_id, "status": "unknown"}, job_advisory="job_row_unreadable", next_read=job_read)
        return result
    result["job"] = {"job_id": job_id, "status": status}
    result["status"] = status
    if status in {"queued", "processing"}:
        result["next_read"] = {"tool": "process_result", "arguments": {"action_id": payload["action_id"]}}
        return result
    assert reader is not None
    outcome, error = _outcomes(reader, job_id, status)
    if outcome:
        result["result"] = outcome
    elif status == "completed":
        result.update(job_advisory="job_result_unreadable", next_read=job_read)
    if status != "completed":
        result["error"] = error
        result["error_message"] = error.get("message")
    return result
