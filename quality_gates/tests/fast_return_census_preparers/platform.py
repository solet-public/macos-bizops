"""Platform fixture ownership from NM-2 section 3.6; no vendor substitutes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

PREPARER_NAME = "platform-v1"
PLUGINS = frozenset(
    {
        "default_address_book_plugin",
        "default_blob_storage_plugin",
        "default_scheduling_plugin",
        "code_vetting_plugin",
        "comfyui_image_generation_plugin",
        "postgres_state_management_plugin",
        "platform_health_plugin",
        "claude_code_cloud_session_source_plugin",
        "codex_cloud_session_source_plugin",
        "claude_code_filesystem_session_source_plugin",
        "codex_filesystem_session_source_plugin",
        "actr_memory_plugin",
        "pgvector_service_plugin",
        "openai_embeddings_plugin",
        "titanv2_embeddings_plugin",
        "fleet_maintenance_plugin",
    }
)


class CensusContextView(Protocol):
    app_home: Path
    export_root: Path

    def get_service(self, name: str) -> Any: ...

    def store_blob(self, namespace: str, filename: str, content: bytes, mime_type: str) -> str: ...


def configure(plugin: str, home: Path, export_root: Path) -> dict[str, object]:
    if plugin == "code_vetting_plugin":
        # The real locator needs a worktree shape, while all scanning targets
        # remain parked. Never let it find a containing host checkout instead.
        (home / "quality_gates").mkdir()
        (home / ".git").mkdir()
    if plugin == "comfyui_image_generation_plugin":
        workflow = home / "census-workflow.json"
        workflow.write_text(json.dumps({"1": {"class_type": "EmptyLatentImage", "inputs": {"width": 64, "height": 64, "batch_size": 1}}}))
        return {"workflow_path": str(workflow), "export_allowed_roots": [str(export_root)]}
    return {"export_allowed_roots": [str(export_root)], "ledger_allowed_roots": [str(home)], "auto_ingest": False}


def seed(plugin: str, context: CensusContextView) -> dict[str, str]:
    """Publish through the real blob service before the handler timer starts."""
    builders = {"pgvector_service_plugin": _vector_rows, "actr_memory_plugin": _memory_rows, "default_address_book_plugin": _address_rows, "postgres_state_management_plugin": _state_rows, "default_blob_storage_plugin": _blob_rows, "code_vetting_plugin": _vetting_rows}
    builder = builders.get(plugin)
    seeded = builder(context) if builder is not None else {}
    if plugin in {"claude_code_cloud_session_source_plugin", "codex_cloud_session_source_plugin", "comfyui_image_generation_plugin"}:
        seeded.update(_job_rows(plugin, context))
    return seeded


def _vetting_rows(context: CensusContextView) -> dict[str, str]:
    """Seed an actual store record for the read verb, not a completed scan."""
    result = context.get_service("state_service").write_state(
        "vetting_runs", {"table": "vetting_runs", "record": {"run_id": "census-owned-run", "target": {"repo": str(context.app_home), "ref": "synthetic", "scope": "fixture only"}, "started": "2025-01-01T00:00:00+00:00", "substrate": "heuristic", "report": "Synthetic census readback fixture; no scan executed."}}
    )
    if result.get("action_status") != "completed":
        raise RuntimeError(f"real vetting record seed failed: {result}")
    return {"run_id": "census-owned-run"}


def _vector_rows(context: CensusContextView) -> dict[str, str]:
    """Use only the vector namespace already installed by the census."""
    seeded: dict[str, str] = {}
    for verb in ("get_namespace_stats_action", "get_vector_action", "update_metadata_action", "delete_vectors_action"):
        vector_id = f"census-{verb}"
        result = context.get_service("vector_service").store_vectors("pgvector_service_plugin", [{"id": vector_id, "vector": [1.0] + [0.0] * 1535, "dimension": 1536, "metadata": {"source": "census-fixture"}}])
        if result.get("action_status") != "completed":
            raise RuntimeError(f"real vector seed failed: {result}")
        seeded[verb] = vector_id
    return seeded


def _state_rows(context: CensusContextView) -> dict[str, str]:
    state = context.get_service("state_service")
    result = state.write_state("core", {"table": "key_value_store", "record": {"key": "census-read", "value": "owned"}})
    if result.get("action_status") != "completed":
        raise RuntimeError(f"real state seed failed: {result}")
    return {}


def _blob_rows(context: CensusContextView) -> dict[str, str]:
    target = context.app_home / "fixture.txt"
    target.write_bytes(b"census synthetic fixture\n")
    return {"blob_id": context.store_blob("census", "fixture.txt", b"census synthetic fixture\n", "text/plain"), "delete_blob_id": context.store_blob("census", "delete.txt", b"disposable delete fixture\n", "text/plain"), "file_path": str(target)}


def _job_rows(plugin: str, context: CensusContextView) -> dict[str, str]:
    existing = getattr(context, "seeded", {}).get("status_job_id")
    if plugin in {"claude_code_cloud_session_source_plugin", "codex_cloud_session_source_plugin"} and existing:
        return {"job_id": str(existing)}
    action = "generate_image" if plugin == "comfyui_image_generation_plugin" else "backfill_from_cloud"
    result = context.get_service("async_job_manager").create_job(plugin_name=plugin, action_name=action, request_data={"notes": "synthetic census status fixture"})
    if result.get("action_status") != "completed":
        raise RuntimeError(f"real canonical job seed failed: {result}")
    return {"job_id": str(result["data"]["job_id"])}


def _memory_rows(context: CensusContextView) -> dict[str, str]:
    """Reserve distinct objects for destructive verbs and seed real focus."""
    memory = context.get_service("memory_service")
    seeded: dict[str, str] = {}
    for verb in ("forget_action", "reinforce_action", "focus_action", "unfocus_action", "memorize_action", "stop_memorizing_action"):
        result = memory.remember(content=f"owned census memory for {verb}", tags=["agent_memory"], embed=False)
        memory_id = str(result["memory_id"])
        seeded[verb] = memory_id
        if verb == "unfocus_action":
            memory.focus(memory_id=memory_id, session_id="census-session")
        if verb == "stop_memorizing_action":
            memory.memorize(memory_id=memory_id)
    return seeded


def _address_rows(context: CensusContextView) -> dict[str, str]:
    """Reserve separate real addresses and entries across delete ordering."""
    address = context.get_service("address_book_service")
    seeded: dict[str, str] = {}
    for verb in ("add_entry", "delete", "delete_entry", "resolve", "resolve_with_secrets", "update", "update_entry"):
        name = f"census-{verb}"
        result = address.register(name=name, address_type="service", description="owned census address", entries=[{"field_type": "text", "description": "endpoint", "value": "https://census.invalid"}])
        if result.get("action_status") != "completed":
            raise RuntimeError(f"real address seed failed: {result}")
        resolved = address.resolve(name=name)
        seeded[verb] = name
        seeded[f"{verb}_entry"] = str(resolved["data"]["entries"][0]["id"])
    return seeded


def _specific_parameters(plugin: str, verb: str, seeded: Mapping[str, str]) -> dict[str, object]:
    """Supply optional handler inputs absent from decorator-required fields."""
    if verb in {"backfill_status", "check_generation_status"} and "job_id" in seeded:
        return {"job_id": seeded.get("status_job_id") or seeded["job_id"]}
    if plugin == "default_blob_storage_plugin":
        return _blob_parameters(verb, seeded)
    if plugin == "code_vetting_plugin" and verb == "get_vetting_run":
        return {"run_id": seeded["run_id"]}
    if plugin == "pgvector_service_plugin":
        vector = [1.0] + [0.0] * 1535
        vector_id = seeded.get(verb, "census-fixture")
        return {"namespace": plugin, "vector_id": vector_id, "vector_ids": [vector_id], "query_vector": vector, "vectors": [{"vector": vector, "dimension": 1536, "metadata": {"source": "census-fixture"}}]}
    if plugin == "postgres_state_management_plugin":
        return {"namespace": "core", "query": {"table": "key_value_store", "filters": {"key": "census-read"}}, "data": {"table": "key_value_store", "record": {"id": "kv-census-native", "name": "census-native", "namespace": "core", "key": "census-write", "value": "owned"}}}
    return _seeded_object_parameters(plugin, verb, seeded)


def _blob_parameters(verb: str, seeded: Mapping[str, str]) -> dict[str, object]:
    return {"namespace": "census", **({"name": seeded["blob_id"]} if verb == "get_blob" else {}), **({"blob_id": seeded["delete_blob_id"]} if verb == "delete_blob" else {})}


def _seeded_object_parameters(plugin: str, verb: str, seeded: Mapping[str, str]) -> dict[str, object]:
    if plugin == "actr_memory_plugin" and verb in seeded:
        return {"memory_id": seeded[verb]}
    if plugin == "default_address_book_plugin" and verb in seeded:
        return {"name": seeded[verb], "entry_id": seeded[f"{verb}_entry"]}
    if plugin == "fleet_maintenance_plugin" and verb == "maintenance_duty_run":
        return {"slug": "model-catalog-sync", "wait": False}
    return {}


def parameters(plugin: str, verb: str, params: dict[str, object], seeded: Mapping[str, str], home: Path) -> dict[str, object]:
    """Supply real local inputs; unmet second-layer prerequisites stay visible."""
    result = dict(params)
    result.setdefault("notes", "synthetic census observation")
    values: dict[str, object] = {
        "namespace": "census",
        "command": "ls",
        "cron_expression": "0 0 * * *",
        "cron": "0 0 * * *",
        "vector_ids": ["census-fixture"],
        "vector": [0.0] * 1536,
        "text": "synthetic census fixture",
        "content": "synthetic census fixture",
        "metadata": {"filename": "fixture.txt", "mime_type": "text/plain"},
        "entries": [{"field_type": "text", "description": "endpoint", "value": "https://census.invalid"}],
        "address_type": "service",
        "description": "synthetic census fixture",
    }
    for name in result.keys() & values.keys():
        result[name] = values[name]
    for name, value in seeded.items():
        if name in result:
            result[name] = value
    result.update(_specific_parameters(plugin, verb, seeded))
    if plugin == "default_scheduling_plugin" and (verb.startswith("create_") or verb == "execute_in_seconds"):
        result["action_definitions"] = [{"process_key": "plugin::default_blob_storage_plugin::file_command", "arguments": {"command": "ls"}}]
        result.pop("memory_tag", None)
    if "output_path" in result:
        result["output_path"] = str(home / "census-output.json")
    return result
