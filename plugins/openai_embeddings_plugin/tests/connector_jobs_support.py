"""Owned connector fixtures for the registered HTTP/job dispatch smoke.

Real plugin handlers and helpers run against synthetic vendor boundaries.
The caller, job ledger and JobService are supplied by http_jobs_smoke.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

CONNECTOR_CASES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("actr_memory_plugin", "learn_action", {}),
    ("actr_memory_plugin", "import_memories_action", {}),
    ("actr_memory_plugin", "purge_memories_action", {"confirm": True}),
    ("jira_plugin", "add_attachment", {"key": "FIX-1", "blob_key": "fixture-blob"}),
    ("jira_plugin", "download_attachment", {"attachment_id": "fixture-attachment"}),
    ("schwab_market_data_plugin", "start_interface", {"host": "127.0.0.1", "port": 58731}),
    ("s3_blob_storage_plugin", "store_blob", {"namespace": "fixture", "content": b"fixture bytes", "metadata": {"filename": "fixture.txt", "mime_type": "text/plain"}}),
    ("s3_blob_storage_plugin", "delete_blob", {"namespace": "fixture"}),
    ("titanv2_embeddings_plugin", "generate_embeddings_action", {"inputs": ["fixture text"]}),
    ("titanv2_embeddings_plugin", "get_embedding_dimension_action", {}),
    ("titanv2_embeddings_plugin", "list_models_action", {}),
)


def boundary(fixture: Any, result: Any) -> Any:
    """A slow external boundary with observable entry and caller identity."""
    fixture.touches.append(threading.get_ident())
    fixture.entered.set()
    fixture.release.wait(0.3)
    if fixture.failed:
        raise RuntimeError("fixture connector transport failure")
    return result


def configure(fixture: Any, name: str, verb: str, parameters: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Configure only owned files and synthetic services; never launch a server."""
    parameters = dict(parameters)
    root = Path(fixture.enter_context(tempfile.TemporaryDirectory(prefix="connector-jobs-", dir="/tmp")))
    if name == "actr_memory_plugin":
        return _memory(fixture, root, verb, parameters)
    module = importlib.import_module(name + ".plugin")
    if name == "jira_plugin":
        plugin = _jira(fixture, module)
    elif name == "schwab_market_data_plugin":
        plugin = module.SchwabMarketDataPlugin()
        plugin._token_store = SimpleNamespace()
        plugin._app_config_loader = SimpleNamespace()
        fixture.enter_context(patch.object(plugin._oauth_server, "start", side_effect=lambda **_kwargs: boundary(fixture, 58731)))
    elif name == "s3_blob_storage_plugin":
        plugin, parameters = _s3(fixture, root, module, verb, parameters)
    else:
        plugin = module.TitanV2EmbeddingsPlugin()
        plugin._initialized = True
        plugin._default_model = "fixture-model"
        plugin._runtime_client = SimpleNamespace(invoke_model=lambda **_kwargs: boundary(fixture, {"body": io.BytesIO(b'{"embedding": [0.25, 0.75]}')}))
        plugin._bedrock_client = SimpleNamespace(list_foundation_models=lambda **_kwargs: boundary(fixture, {"modelSummaries": [{"modelId": "fixture-model", "modelName": "Fixture", "outputModalities": ["EMBEDDING"]}]}))
    plugin.orchestrator_ref = SimpleNamespace(async_job_manager=fixture.manager)
    return plugin, parameters


def _memory(fixture: Any, root: Path, verb: str, parameters: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    backend = fixture.memory._backend
    backend._export_allowed_roots = [str(root)]
    fixture.enter_context(patch.dict(os.environ, {"APP_HOME": str(root)}))
    (root / "config").mkdir()
    (root / "config/identity.json").write_text(json.dumps({"identity": ["Synthetic fixture identity"]}))
    if verb == "learn_action":
        path = root / "input.md"
        path.write_text("Synthetic owned document for ingestion.")
        parameters["path"] = str(root / "missing.md" if fixture.failed else path)
    elif verb == "import_memories_action":
        path = root / "input.json"
        path.write_text(json.dumps({"memories": [{"id": "mem-ownedfixture", "content": "Synthetic imported memory", "tags": []}]}))
        parameters["file_path"] = str(root / "missing.json" if fixture.failed else path)
    return fixture.memory, parameters


def _jira(fixture: Any, module: Any) -> Any:
    plugin = module.JiraPlugin()
    attachment = SimpleNamespace(raw={"filename": "fixture.txt", "mimeType": "text/plain"}, get=lambda: boundary(fixture, b"fixture bytes"))
    client = SimpleNamespace(attachment=lambda _identifier: attachment, add_attachment=lambda **_kwargs: boundary(fixture, SimpleNamespace(id="fixture-attachment")))
    fixture.enter_context(patch.object(plugin, "_require_client", return_value=client))
    plugin._blob_storage_service = SimpleNamespace(
        retrieve_blob=lambda _identifier: {"action_status": "completed", "data": {"filename": "fixture.txt", "content": b"fixture bytes".hex(), "metadata": {"mime_type": "text/plain"}}},
        store_blob=lambda *_args: {"action_status": "completed", "data": {"blob_id": "fixture-blob"}},
    )
    return plugin


def _s3(fixture: Any, root: Path, module: Any, verb: str, parameters: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    support = importlib.import_module("plugins.s3_blob_storage_plugin.tests.s3_blob_storage_smoke")
    provider_module = importlib.import_module("s3_blob_storage_plugin.providers.s3_provider")
    client = support.FakeS3Client()
    config = {"region": "us-east-1", "bucket": "fixture", "solet_name": "fixture", "kms_key_id": "alias/fixture", "cache_dir": str(root / "cache")}
    with patch.object(provider_module.boto3, "client", return_value=client):
        provider = provider_module.S3Provider(str(root), config)
    provider.set_state_service(support.InMemoryMetadataState())
    if verb == "delete_blob":
        seeded = provider.store_blob("fixture", b"fixture bytes", {"filename": "fixture.txt"})
        assert seeded["action_status"] == "completed", seeded
        parameters["blob_id"] = "invalid" if fixture.failed else seeded["data"]["blob_id"]
    for method in ("put_object", "delete_object"):
        original = getattr(client, method)

        def invoke(_original: Any = original, **kwargs: Any) -> Any:
            boundary(fixture, None)
            return _original(**kwargs)

        fixture.enter_context(patch.object(client, method, side_effect=invoke))
    plugin = module.S3BlobStoragePlugin()
    plugin.provider = provider
    return plugin, parameters


def assert_result(name: str, verb: str, data: dict[str, Any]) -> None:
    """Assert the former payload, including nested legacy envelopes, survives."""
    validators = {
        "actr_memory_plugin": _memory_result,
        "jira_plugin": _jira_result,
        "schwab_market_data_plugin": _schwab_result,
        "s3_blob_storage_plugin": _s3_result,
        "titanv2_embeddings_plugin": _titan_result,
    }
    validators[name](verb, data)


def _memory_result(verb: str, data: dict[str, Any]) -> None:
    if verb == "learn_action":
        assert data["memories_created"] == 1, data
    elif verb == "import_memories_action":
        assert data == {"imported": 1, "skipped": 0, "total_in_file": 1}, data
    elif verb == "purge_memories_action":
        assert data["data"] == {"purged_count": 0, "identity_reseeded": 1}, data


def _jira_result(verb: str, data: dict[str, Any]) -> None:
    assert data["site"] == "legacy", data
    assert data["filename"] == "fixture.txt", data
    key = "attachment_id" if verb == "add_attachment" else "attachment_blob_key"
    assert data[key] == ("fixture-attachment" if verb == "add_attachment" else "fixture-blob"), data


def _schwab_result(_verb: str, data: dict[str, Any]) -> None:
    assert data["port"] == 58731 and data["host"] == "127.0.0.1", data
    assert data["callback_url"] == "http://127.0.0.1:58731/oauth/schwab/callback", data


def _s3_result(verb: str, data: dict[str, Any]) -> None:
    assert data["blob_id"].startswith("bmd-"), data
    if verb == "delete_blob":
        assert data["deleted"] is True, data


def _titan_result(verb: str, data: dict[str, Any]) -> None:
    if verb == "generate_embeddings_action":
        assert data["result"] == {"embeddings": [[0.25, 0.75]], "dimension": 2, "model": "fixture-model"}, data
    elif verb == "get_embedding_dimension_action":
        assert data["result"] == {"dimension": 2, "model": "fixture-model"}, data
    else:
        assert data["result"]["models"][0]["name"] == "fixture-model", data
