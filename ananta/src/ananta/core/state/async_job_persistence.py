"""Atomic request and outcome writes for the existing job ledger."""

import json

from ananta.constants import FRAMEWORK_ASYNC_JOBS_TABLE, FRAMEWORK_NAMESPACE
from ananta.interfaces.state_management_interface import StateTransaction
from ananta.interfaces.state_service_protocol import StateServiceProtocol


def create_job_and_request(
    state: StateServiceProtocol,
    record: dict[str, object],
    request: dict[str, object],
) -> str:
    """Return the canonical id only after both inserts have committed."""
    encoded = json.dumps(request)
    job_record = state.prepare_record(FRAMEWORK_NAMESPACE, FRAMEWORK_ASYNC_JOBS_TABLE, record)
    with state.transactional() as transaction:
        job_id = transaction.write_state(
            FRAMEWORK_NAMESPACE, {"table": FRAMEWORK_ASYNC_JOBS_TABLE, "record": job_record},
        )
        if not job_id.strip():
            raise RuntimeError("Job insert returned no canonical id")
        _write_payload(state, transaction, job_id, "request", encoded, 1)
    return job_id


def _write_payload(
    state: StateServiceProtocol,
    transaction: StateTransaction,
    job_id: str,
    kind: str,
    encoded: str,
    sequence: int,
) -> None:
    payload_id = transaction.write_state(FRAMEWORK_NAMESPACE, {
        "table": "job_payload",
        "record": state.prepare_record(FRAMEWORK_NAMESPACE, "job_payload", {
            "job_id": job_id, "payload_type": kind,
            "payload_data": encoded, "sequence": sequence,
        }),
    })
    if not payload_id.strip():
        raise RuntimeError("Job payload insert returned no canonical id")


def update_job_and_payloads(
    state: StateServiceProtocol,
    job_id: str,
    updates: dict[str, object],
    result: dict[str, object] | None,
    error: dict[str, object] | None,
) -> None:
    """Commit one outcome, or raise without any writes or completion delivery.

    The conditional UPDATE locks the job row through payload insertion. A
    terminal job cannot acquire a second outcome, including progress writes
    racing completion. Payload sequence allocation occurs under the same lock.
    """
    payloads = [(kind, json.dumps(value)) for kind, value in
                (("result", result), ("error", error)) if value is not None]
    with state.transactional() as transaction:
        ledger_updates = updates or _current_status_update(transaction, job_id)
        # A payload-only write must not restore a stale status after a race.
        statuses = ["queued", "processing"] if updates else [ledger_updates["status"]]
        affected = transaction.update_state(FRAMEWORK_NAMESPACE, {
            "table": FRAMEWORK_ASYNC_JOBS_TABLE,
            "filters": {"id": job_id, "status": statuses},
        }, ledger_updates)
        if affected != 1:
            raise RuntimeError(f"Job {job_id} is missing or already terminal; update refused")
        if payloads:
            sequence = _next_payload_sequence(transaction, job_id)
            for kind, encoded in payloads:
                _write_payload(state, transaction, job_id, kind, encoded, sequence)
                sequence += 1


def _current_status_update(transaction: StateTransaction, job_id: str) -> dict[str, object]:
    rows = transaction.query_state(FRAMEWORK_NAMESPACE, {
        "table": FRAMEWORK_ASYNC_JOBS_TABLE, "filters": {"id": job_id}, "limit": 1,
    })
    if not rows or rows[0].get("status") not in {"queued", "processing"}:
        raise RuntimeError(f"Job {job_id} is missing or already terminal")
    return {"status": rows[0]["status"]}


def _next_payload_sequence(transaction: StateTransaction, job_id: str) -> int:
    rows = transaction.query_state(FRAMEWORK_NAMESPACE, {
        "table": "job_payload", "filters": {"job_id": job_id},
        "order_by": "sequence DESC", "limit": 1,
    })
    if not rows:
        return 1
    sequence = rows[0].get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError(f"Invalid payload sequence for job {job_id}: {sequence!r}")
    return sequence + 1
