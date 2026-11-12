"""D0.3 deferred-completion machinery for external_postgres_plugin.

Ms-scale dispatch (the plugin's ``@platform_process`` handlers) validates
params, creates a job, and returns a ``{"job_id", "status": "queued"}``
envelope immediately; a single lazily-started background daemon thread does
the real connect + query I/O off the action-dispatch path and completes the
job via ``AsyncJobManager.update_job``, which — via the job's
``completion_handlers`` metadata — submits the follow-up action into the
originating flow. This is the SAME shape traced end-to-end in
``comfyui_image_generation_plugin::generate_image``
(workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md
§5) — no new primitive invented.

ONE worker thread, never more, by design: ``FlowManager._sequence_cache`` is
an unlocked ``dict`` (doctrine §2) — two callers submitting completion
actions into the SAME flow concurrently can lose an update. comfyui stays
safe because it runs exactly one worker thread; this plugin does the same
rather than firing a thread per dispatch, which would reintroduce that named
platform gap on the very first concurrent pair of calls into one flow.

The AsyncJobManager reference is acquired LAZILY (mirrors comfyui's
``_try_acquire_job_manager``), not at ``prepare_for_readiness`` — plugin
boot order does not guarantee ``orchestrator_ref.async_job_manager`` is set
by the time this plugin's readiness hook runs, but it is always set by the
time any verb is actually dispatched.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ananta.constants import CONTEXT_KEY_FLOW_ID, CONTEXT_KEY_SESSION_ID
from ananta.file_locations.containment import job_location_session, location_session

from . import (
    admin_actions,
    client_tools,
    completion_templates,
    copy_actions,
    query_actions,
    script_actions,
)
from .constants import ERROR_API_ERROR, PLUGIN_NAME

if TYPE_CHECKING:
    from .plugin import ExternalPostgresPlugin

logger = logging.getLogger(PLUGIN_NAME)

POLL_INTERVAL_SECONDS = 2.0
_JOB_LIST_LIMIT = 10
_WITHHELD_FAILURE = "the job failed before its result could be checked; its output was withheld"
_PAYLOAD_FAILURE = "could not load the job's request payload"


@dataclass(frozen=True)
class ActionHandler:
    """How the worker runs one verb.

    ``gate``: ``none``; ``tsv`` (the TSV export gate, ``output_tsv_path``); or
    ``file`` (the general workspace file gate, any contained path).
    ``target``: ``connection`` (an opened psycopg connection) or ``dsn`` (the
    resolved, platform-guarded connection target, for the client programs).
    """

    fn: Callable[..., dict[str, Any]]
    gate: Literal["none", "tsv", "file"] = "none"
    target: Literal["connection", "dsn"] = "connection"


ACTION_HANDLERS: dict[str, ActionHandler] = {
    "list_schemas": ActionHandler(query_actions.list_schemas),
    "list_tables": ActionHandler(query_actions.list_tables),
    "describe_table": ActionHandler(query_actions.describe_table),
    "test_connection": ActionHandler(query_actions.test_connection),
    "run_query": ActionHandler(query_actions.run_query, gate="tsv"),
    "export_query": ActionHandler(query_actions.export_query, gate="tsv"),
    "run_statement": ActionHandler(query_actions.run_statement, gate="tsv"),
    "run_script": ActionHandler(script_actions.run_script),
    "copy": ActionHandler(copy_actions.run_copy, gate="file"),
    "activity": ActionHandler(admin_actions.run_activity),
    "statistics": ActionHandler(admin_actions.run_statistics),
    "dump_database": ActionHandler(client_tools.dump_database, gate="file", target="dsn"),
    "restore_database": ActionHandler(client_tools.restore_database, gate="file", target="dsn"),
}


def _validate_state_context(state: dict[str, Any]) -> tuple[str, str]:
    """Dispatch-time required step (doctrine §1): fail loud, not at completion time."""
    session_id = state.get(CONTEXT_KEY_SESSION_ID)
    flow_id = state.get(CONTEXT_KEY_FLOW_ID)
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(f"'{CONTEXT_KEY_SESSION_ID}' missing from state context")
    if not isinstance(flow_id, str) or not flow_id:
        raise ValueError(f"'{CONTEXT_KEY_FLOW_ID}' missing from state context")
    return session_id, flow_id


def _acquire_async_job_manager(plugin: ExternalPostgresPlugin) -> Any:
    if plugin._async_job_manager is not None:
        return plugin._async_job_manager
    orchestrator_ref = plugin.orchestrator_ref
    if orchestrator_ref is None:
        raise RuntimeError(f"{PLUGIN_NAME}: orchestrator_ref not injected")
    manager = getattr(orchestrator_ref, "async_job_manager", None)
    if manager is None:
        raise RuntimeError(f"{PLUGIN_NAME}: async_job_manager not available from orchestrator")
    plugin._async_job_manager = manager
    return manager


def ensure_worker_started(plugin: ExternalPostgresPlugin) -> None:
    """Idempotently start the ONE background worker thread. Thread-safe."""
    with plugin._worker_lock:
        if plugin._worker_thread is not None and plugin._worker_thread.is_alive():
            return
        thread = threading.Thread(
            target=_worker_loop,
            args=(plugin,),
            name=f"{PLUGIN_NAME}-async-worker",
            daemon=True,
        )
        plugin._worker_thread = thread
        thread.start()


def create_job(
    plugin: ExternalPostgresPlugin,
    *,
    action_name: str,
    params: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Ms-scale dispatch: validate, write the job row, start the worker, return.

    No connection is opened and no query runs here — that is entirely the
    background worker's job, off this call's return path.
    """
    session_id, flow_id = _validate_state_context(state)
    connection_name = params.get("connection_name")
    if not isinstance(connection_name, str) or not connection_name.strip():
        raise ValueError("'connection_name' is required")
    async_job_manager = _acquire_async_job_manager(plugin)
    ensure_worker_started(plugin)
    return async_job_manager.create_job(
        plugin_name=PLUGIN_NAME,
        action_name=action_name,
        request_data={"notes": f"{PLUGIN_NAME}.{action_name}", **params},
        job_metadata={
            "session_id": session_id,
            "flow_id": flow_id,
            "completion_handlers": completion_templates.build_completion_handlers(action_name),
        },
    )


def _worker_loop(plugin: ExternalPostgresPlugin) -> None:
    while True:
        try:
            _drain_queued_jobs(plugin)
        except Exception:  # the loop must never die — a bad pass still yields the next one
            logger.exception("%s async worker: drain pass failed", PLUGIN_NAME)
        time.sleep(POLL_INTERVAL_SECONDS)


def _drain_queued_jobs(plugin: ExternalPostgresPlugin) -> None:
    async_job_manager = _acquire_async_job_manager(plugin)
    for action_name in ACTION_HANDLERS:
        listing = async_job_manager.list_jobs(
            status="queued",
            provider_name=f"{PLUGIN_NAME}.{action_name}",
            limit=_JOB_LIST_LIMIT,
        )
        if listing.get("action_status") != "completed":
            continue
        jobs = listing.get("data", {}).get("jobs", [])
        for job in jobs:
            job_id = job.get("id") if isinstance(job, dict) else None
            if isinstance(job_id, str) and job_id:
                _process_job(plugin, async_job_manager, job_id, action_name)


def _process_job(plugin: ExternalPostgresPlugin, async_job_manager: Any, job_id: str, action_name: str) -> None:
    """Bind only this job's originating session; absent metadata is advisory."""
    try:
        record = async_job_manager.get_job(job_id)
    except Exception:
        record = None
    with location_session(job_location_session(record)):
        _process_job_in_context(plugin, async_job_manager, job_id, action_name)


def _process_job_in_context(
    plugin: ExternalPostgresPlugin,
    async_job_manager: Any,
    job_id: str,
    action_name: str,
) -> None:
    """Run one job's real I/O and complete it. Runs on the ONE worker thread only."""
    async_job_manager.update_job(job_id, {"status": "processing"})
    payload_result = async_job_manager.get_job_payload(job_id, "request")
    if payload_result.get("action_status") != "completed":
        logger.error("%s async worker: %s job %s: %s", PLUGIN_NAME, action_name, job_id, _PAYLOAD_FAILURE)
        outcome = plugin._error(ERROR_API_ERROR, _PAYLOAD_FAILURE)
    else:
        params = payload_result.get("data", {}).get("payload", {})
        try:
            outcome = run_action(plugin, ACTION_HANDLERS[action_name], action_name, params)
        except Exception:  # never leave a job 'processing'; its text could carry anything, so none is kept
            logger.error("%s async worker: %s job %s failed before its result was checked", PLUGIN_NAME, action_name, job_id)
            outcome = plugin._error(ERROR_API_ERROR, _WITHHELD_FAILURE)
    if outcome.get("action_status") == "completed":
        async_job_manager.update_job(job_id, {"status": "completed", "result": outcome.get("data", {})})
    else:
        async_job_manager.update_job(job_id, {"status": "error", "error": outcome.get("error", {})})


def run_action(
    plugin: ExternalPostgresPlugin,
    handler: ActionHandler,
    action_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run one verb's real I/O through the plugin's typed-error envelope."""
    gate: Any = {
        "none": None,
        "tsv": plugin._export_path_gate,
        "file": plugin._file_path_gate,
    }[handler.gate]
    if handler.target == "dsn":
        return plugin._run_with_dsn(
            params,
            lambda dsn: handler.fn(dsn, params, gate, plugin._tool_settings()),
            action_name,
        )
    if gate is None:
        return plugin._run_on_connection(params, lambda conn: handler.fn(conn, params), action_name)
    return plugin._run_on_connection(
        params,
        lambda conn: handler.fn(conn, params, gate),
        action_name,
    )
