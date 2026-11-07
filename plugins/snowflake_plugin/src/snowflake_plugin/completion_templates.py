"""Completion-handler template builders for AsyncJobManager routing (D0.3 mechanic 1).

Mirrors external_postgres_plugin's completion_templates.py shape (itself
mirroring comfyui_image_generation_plugin's, the doctrine's traced worked
example) — same job_metadata/completion_handlers structure, routed through
the same ``service_interface::inference_service::process_results`` /
``process_error`` process keys. These verbs return raw structured data
(database/schema/table lists, column shapes, TSV handles) rather than a
generated image, so the instructions ask the model to relay the data rather
than compose a friendly caption.
"""

from __future__ import annotations

from typing import Literal

from .rest_families import FAMILIES

# Typed REST families share the request verb's completion contract; derived, never listed by hand.
_REST_FAMILY_ACTIONS = frozenset(FAMILIES)


def _instructions(action_name: str, outcome: Literal["result", "error"]) -> list[str]:
    if action_name == "request" and outcome == "result":
        return [
            "The Snowflake REST request completed; vendor state may have changed.",
            "Relay HTTP status, headers and body faithfully from job_payload, including vendor errors, Retry-After, SQL handles and pagination. Job completion does not prove mutation success.",
            "Do not silently retry mutations, poll SQL handles or follow pagination or redirects.",
        ]
    if action_name in _REST_FAMILY_ACTIONS and outcome == "result":
        return [
            f"The Snowflake '{action_name}' operation named in job_payload.operation completed at the HTTP level; vendor state may have changed.",
            "Relay status, error_code and body faithfully. A 202 means accepted, not finished: job_payload.pagination names the result_handler, statement_handle, next partition, next_from_name, next_begin_mark or next_start_time_inclusive for an explicit follow-up call.",
            "Do not silently retry mutations or follow pagination. A task execute response means the run was requested, not that it completed; a pipe insert_files SUCCESS means Snowpipe queued the files, not that they were loaded (insert_report or load_history_scan shows loads).",
            "A stage presigned_url is a time-limited bearer URL for that one file: hand it only to the caller that asked for it.",
        ]
    if outcome == "result":
        return [
            f"A snowflake_plugin '{action_name}' job completed successfully.",
            "The raw result is available as job_payload — relay it to the user (or use it to continue your reasoning) as returned; technical fields like row_count, columns, or path are meaningful to the caller and should not be paraphrased away.",
            "Reply with action using the active IO plugin's post_message (process_key: plugin::<io_plugin>::post_message) if the flow calls for messaging the user directly.",
        ]
    return [
        f"A snowflake_plugin '{action_name}' job ended with an error.",
        "The error message in job_error is already topology-safe (no account, user, or warehouse detail) and is safe to relay to the user verbatim.",
        "Reply with action using the active IO plugin's post_message (process_key: plugin::<io_plugin>::post_message) if the flow calls for messaging the user directly.",
    ]


def build_completion_template(
    action_name: str,
    outcome: Literal["result", "error"],
) -> dict[str, object]:
    """Inference-routing template used when AsyncJobManager routes a completion event."""
    return {
        "params": {
            "model": {"temperature": 0.2, "max_tokens": 1024},
            "prompt": {
                "user": {
                    "instructions": _instructions(action_name, outcome),
                    "result_data": {},
                    "action_result": {},
                    "flow_input": {"original_input": ""},
                },
            },
        },
    }


def build_completion_handlers(action_name: str) -> dict[str, object]:
    """The ``completion_handlers`` block stored in a job's ``job_metadata``."""
    return {
        "result": {
            "process_key": "service_interface::inference_service::process_results",
            "template": build_completion_template(action_name, "result"),
            "notes": f"snowflake_plugin.{action_name} completed",
        },
        "error": {
            "process_key": "service_interface::inference_service::process_error",
            "template": build_completion_template(action_name, "error"),
            "notes": f"snowflake_plugin.{action_name} failed",
        },
    }
