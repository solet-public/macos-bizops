"""Synthetic ACTR and blob inputs; existing platform seeds retain ownership.

The ordered census's purge deletes later memorization state. A discovered
before-verb hook recreates it through the current real service, before clocks
and parameter binding; no service handle survives the context.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

PREPARER_NAME = "stores-v1"
PLUGINS = frozenset({"actr_memory_plugin", "default_blob_storage_plugin"})
_CONTENT = b"Synthetic owned census stores fixture.\n"


class CensusContextView(Protocol):
    app_home: Path
    export_root: Path
    scratch: Path
    seeded: dict[str, str]
    receipt: dict[str, Any]

    def get_service(self, name: str) -> Any: ...


def before_verb(plugin: str, verb: str, context: CensusContextView) -> None:
    """Recreate stop's actual memory and memorization after the ordered purge."""
    if plugin != "actr_memory_plugin" or verb != "stop_memorizing_action":
        return
    memory = context.get_service("memory_service")
    state = context.get_service("state_service")
    first = len(state.calls)
    result = memory.remember(content="Synthetic owned census stop-memorizing fixture.", tags=["agent_memory", "census-fixture"], embed=False)
    memory_id = str(result["memory_id"])
    memory.memorize(memory_id=memory_id)
    previous = context.seeded.get(verb)
    context.seeded[verb] = memory_id
    context.receipt.setdefault("before_verb", []).append({"preparer": PREPARER_NAME, "verb": verb, "previous_memory_id": previous, "memory_id": memory_id, "state_calls": list(state.calls[first:])})


def seed(plugin: str, context: CensusContextView) -> dict[str, str]:
    """Write only owned inputs before clocks start; never contact a provider."""
    if plugin == "actr_memory_plugin":
        # isolated_boundaries binds the timed handler's APP_HOME to scratch.
        identity = context.scratch / "config" / "identity.json"
        identity.parent.mkdir(exist_ok=True)
        identity.write_text(json.dumps({"identity": ["I am a synthetic census fixture."]}) + "\n", encoding="utf-8")
        learn = context.app_home / "stores-learn.json"
        learn.write_text(json.dumps({"content": _CONTENT.decode()}) + "\n", encoding="utf-8")
        imported = context.export_root / "stores-import.json"
        imported.write_text(
            json.dumps({"memories": [{"id": "census-stores-import", "content": _CONTENT.decode(), "tags": ["census-fixture"], "memory_type": "semantic", "status": "active", "created_at": "2026-01-01T00:00:00+00:00"}]}) + "\n",
            encoding="utf-8",
        )
        return {"stores_learn": str(learn), "stores_import": str(imported)}
    return {}


def parameters(plugin: str, verb: str, params: dict[str, object], seeded: Mapping[str, str], home: Path) -> dict[str, object]:
    """Supply supported caller arguments after the platform parameter preparer."""
    del home
    result = dict(params)
    if plugin == "actr_memory_plugin":
        if verb == "learn_action":
            result.update(path=seeded["stores_learn"], memorize=False)
        elif verb == "import_memories_action":
            result["file_path"] = seeded["stores_import"]
        elif verb == "purge_memories_action":
            result["confirm"] = True
    elif plugin == "default_blob_storage_plugin" and verb == "store_blob":
        result.update(namespace="census", content=_CONTENT, metadata={"filename": "stores-bytes.txt", "original_name": "stores-bytes.txt", "mime_type": "text/plain"})
    return result
