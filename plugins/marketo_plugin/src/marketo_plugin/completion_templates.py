"""Completion-handler template builders for AsyncJobManager routing.

Mirrors comfyui_image_generation_plugin's completion_templates.py — the one
production instance of this shape (D0.3 doctrine §5). AsyncJobManager stamps
``params.job_payload``/``job_error``/``job_id``/``job_status`` onto the
built arguments itself (``_build_completion_arguments``); this module only
supplies the inference prompt template naming what to do with them.
"""

from __future__ import annotations

from typing import Literal

from .constants import REQUEST_VERB_LABEL

# What a finished job's result carries, which picks its completion instructions:
# export (workspace TSV handle), write (per-record results + tallies), asset
# (vendor Asset envelope), bulk (vendor Bulk API envelope), action (one
# irreversible vendor action: success + request_id) and diagnostic (a
# test_connection/check_setup report).
type ResultKind = Literal["export", "write", "asset", "bulk", "action", "diagnostic"]

_STATE_CHANGING_KINDS: frozenset[str] = frozenset({"write", "asset", "bulk", "action"})


def build_output_schema() -> dict[str, object]:
    """Structured schema for instructing inference service to call post_message."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["reasoning", "actions"],
        "properties": {
            "reasoning": {"type": "string"},
            "actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "required": ["process_key", "reason", "arguments"],
                    "properties": {
                        "process_key": {
                            "type": "string",
                            "description": ("The active IO plugin's post_message process key (plugin::<io_plugin>::post_message)"),
                            "pattern": "^plugin::.+::post_message$",
                        },
                        "reason": {"type": "string"},
                        "arguments": {
                            "type": "object",
                            "required": ["session_id", "message"],
                            "properties": {
                                "session_id": {"type": "string"},
                                "message": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    }


_REPLY = "Reply with action using the active IO plugin's post_message (process_key: plugin::<io_plugin>::post_message)."

# Per result kind: the headline (formatted with the verb label) and the fixed
# instructions that follow it on a completed job.
_RESULT_INSTRUCTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bulk": (
        "A Marketo {verb_label} platform job completed one vendor Bulk API operation.",
        (
            "Report the vendor envelope faithfully, preserving batchId/exportId, requestId, status and row failure/warning counts. Platform completion is not vendor bulk-job completion: poll the corresponding typed status verb until Complete/Completed, Failed or Canceled/Cancelled.",
            "List results retain nextPageToken: continue with next_page_token when present. Downloads retain the body plus body_base64 bytes and content_range for partial downloads. Do not invent a TSV path or per-record tallies.",
            "Read back before retrying any uncertain upload/create/enqueue/cancel. A vendor Failed result is a failed vendor job even when the HTTP operation succeeded.",
        ),
    ),
    "asset": (
        "A Marketo {verb_label} job completed with a vendor Asset envelope.",
        ("Report the vendor result, requestId and warnings faithfully. This operation may have changed vendor state; no per-record tallies are synthesized.",),
    ),
    "write": (
        "A Marketo {verb_label} job completed; it changed vendor state.",
        (
            "The job result carries Marketo's per-record results (seq, id or marketoGUID, status, reasons), row_count and tallies.",
            "Report the tallies plainly (e.g. '3 created, 1 updated, 1 skipped'). A skipped record and its reasons are normal per-record data, not a job failure.",
        ),
    ),
    "action": (
        "A Marketo {verb_label} job completed; it changed vendor state and cannot be undone.",
        (
            "The job result carries success and Marketo's request_id; there are no per-record results or tallies.",
            "Report success and the request_id plainly. Do not claim downstream effects the result does not show: a triggered campaign's flow runs later inside Marketo.",
        ),
    ),
    "diagnostic": (
        "A Marketo {verb_label} job completed with a diagnostic report.",
        (
            "Report every field of the result plainly. A connection test carries ok, base_url and client_id. A setup check carries reads_verified, per-probe checks and writes_unverified: relay each failed check's error_code and guidance.",
            "reads_verified covers READ capabilities only: name the writes_unverified verbs as still unproven rather than calling the setup ready.",
        ),
    ),
    "export": (
        "A Marketo {verb_label} job completed successfully.",
        (
            "The job result carries a workspace TSV file path, row_count, columns, and a truncated flag (true when more records may exist beyond what was written).",
            "Confirm the export: state the file path and row_count plainly — this is an internal workspace path the operator already has access to, not a secret to withhold.",
            "If truncated is true, say the pull is INCOMPLETE and more records may exist beyond what was written. Asset exports include next_offset: continue with that offset and identical filters.",
        ),
    ),
}


def _request_instructions(handler_type: Literal["result", "error"]) -> list[str]:
    return [
        f"A Marketo authenticated request job produced a {handler_type} outcome.",
        "The call may have changed vendor state. Report HTTP status and body faithfully, including success/errors, nextPageToken and retry_after when present. Job completion is not vendor success: inspect the HTTP status and API envelope.",
        "A transport error may follow a vendor mutation; do not retry a write automatically. This result is not a TSV export. Preserve any vendor Bulk job id for subsequent request polling.",
        _REPLY,
    ]


def _error_instructions(verb_label: str, result_kind: ResultKind) -> list[str]:
    return [
        f"A Marketo {verb_label} job ended with an error.",
        "Explain what went wrong in plain terms using the error code and message.",
        "Suggest concrete remediation for known Marketo error classes: "
        "marketo.auth_failed (check OAuth credentials), marketo.permission_denied "
        "or marketo.partition_access_denied (run check_setup for the exact gap "
        "and which admin screen fixes it), marketo.rate_limited (retry after a "
        "short delay), marketo.invalid_params (fix the request and retry).",
        *(["This was a write: a transport fault may follow a vendor mutation, so read the records back before any retry."] if result_kind in _STATE_CHANGING_KINDS else []),
        _REPLY,
    ]


def build_completion_template(
    handler_type: Literal["result", "error"],
    verb_label: str,
    *,
    result_kind: ResultKind = "export",
) -> dict[str, object]:
    """Create the inference template AsyncJobManager routes a job's outcome through."""
    if verb_label == REQUEST_VERB_LABEL:
        instructions = _request_instructions(handler_type)
    elif handler_type == "result":
        headline, details = _RESULT_INSTRUCTIONS[result_kind]
        instructions = [headline.format(verb_label=verb_label), *details, _REPLY]
    else:
        instructions = _error_instructions(verb_label, result_kind)

    return {
        "params": {
            "model": {"temperature": 0.4, "max_tokens": 2048},
            "prompt": {
                "user": {
                    "instructions": instructions,
                    "output_schema": build_output_schema(),
                    "result_data": {},
                    "action_result": {},
                    "flow_input": {"original_input": ""},
                },
            },
        },
    }


def build_job_metadata(
    session_id: str,
    flow_id: str,
    verb_label: str,
    *,
    result_kind: ResultKind = "export",
) -> dict[str, object]:
    """Metadata stored with an AsyncJob for automatic completion routing."""
    return {
        "session_id": session_id,
        "flow_id": flow_id,
        "completion_handlers": {
            "result": {
                "process_key": "service_interface::inference_service::process_results",
                "template": build_completion_template("result", verb_label, result_kind=result_kind),
                "notes": f"Marketo {verb_label} completed",
            },
            "error": {
                "process_key": "service_interface::inference_service::process_error",
                "template": build_completion_template("error", verb_label, result_kind=result_kind),
                "notes": f"Marketo {verb_label} failed",
            },
        },
    }
