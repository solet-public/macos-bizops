"""Session-ledger source registration operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from .installation_state_doctor import partition_session_roots, session_retrieval
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, planned_action, result
from .setup_adapter_runtime import Runtime, read_json_object
from .setup_operations import (
    _SESSION_ROOTS,
    _blocked,
    _failed_outcome,
    _kickstart,
    solet_call,
    solet_call_succeeded,
    solet_data_string,
)


def session_source(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    source_rows = _SESSION_ROOTS[request.operation_ref]
    absolute_rows = [(kind, runtime.home / relative) for kind, relative in source_rows]
    config_path = request.target / "profile/config/plugins/session_ledger_service.json"
    if request.phase == "probe":
        return _session_source_preview(request, runtime, absolute_rows, config_path)
    return _apply_session_source(request, runtime, absolute_rows, config_path)


def _session_source_preview(
    request: AdapterRequest,
    runtime: Runtime,
    absolute_rows: list[tuple[str, Path]],
    config_path: Path,
) -> JsonObject:
    # Absent roots fall through deliberately: the agent CLIs create these trees on
    # first use, and registering a not-yet-existing root is a supported ingest
    # case. Only a root that exists and cannot be read is a permission problem.
    _absent, unreadable = partition_session_roots([path for _kind, path in absolute_rows])
    if unreadable:
        return _blocked(
            request, "session_roots_unreadable", "Approved session roots are not readable."
        )
    roots = sorted({str(path) for _kind, path in absolute_rows})
    configured = read_json_object(config_path)
    if configured is not None and configured.get("ledger_allowed_roots") == roots:
        qualification = session_retrieval(request, runtime)
        if qualification["checkpoint_status"] == "verified":
            return qualification
    actions = [
        planned_action(
            action_id="sessions.write_allowed_roots",
            title="Write only the approved session-ledger roots",
            mutation_kind="config_write",
            target=str(config_path),
            evidence_ref="session_source_not_verified",
        ),
        planned_action(
            action_id="sessions.restart_target",
            title="Restart the target through its LaunchAgent owner",
            mutation_kind="service_restart",
            target=f"launchagent:local.solet.{request.name}",
            evidence_ref="session_config_requires_restart",
        ),
        planned_action(
            action_id="sessions.register_and_backfill",
            title="Register approved sources and run one bounded backfill",
            mutation_kind="session_ingestion",
            target=",".join(roots),
            evidence_ref="session_source_not_verified",
        ),
    ]
    return result(
        request, status="pending", actions=actions, repair="Approve only the displayed roots."
    )


def _apply_session_source(
    request: AdapterRequest,
    runtime: Runtime,
    absolute_rows: list[tuple[str, Path]],
    config_path: Path,
) -> JsonObject:
    current = read_json_object(config_path) or {}
    roots = sorted({str(path) for _kind, path in absolute_rows})
    updated = dict(current)
    updated["ledger_allowed_roots"] = cast(JsonValue, roots)
    runtime.atomic_write(
        config_path, json.dumps(updated, indent=2, sort_keys=True) + "\n", mode=0o600
    )
    started = _kickstart(request, runtime)
    if not started.ok:
        return _failed_outcome(request, started, "session_source_restart_failed")
    source_ids: list[str] = []
    for source_kind, root in absolute_rows:
        registered = solet_call(
            request,
            runtime,
            "service_interface::session_ledger_service::register_source",
            {"source_kind": source_kind, "root_uri": str(root)},
        )
        source_id = solet_data_string(registered, "source_id")
        if source_id is None:
            return _failed_outcome(request, registered, "session_source_register_failed")
        source_ids.append(source_id)
    for source_id in source_ids:
        backfill = solet_call(
            request,
            runtime,
            "service_interface::session_ledger_service::poll_source",
            {"source_id": source_id},
        )
        if not solet_call_succeeded(backfill):
            return _failed_outcome(request, backfill, "session_backfill_failed")
    return result(request, status="applied", retry_safe=True)
