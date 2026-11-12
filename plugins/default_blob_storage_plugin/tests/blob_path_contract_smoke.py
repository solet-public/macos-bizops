#!/usr/bin/env python3
"""Store a real blob and resolve both forms with physical path containment.

Run: .venv/bin/python plugins/default_blob_storage_plugin/tests/blob_path_contract_smoke.py
Only metadata persistence is in memory; storage and resolution use production
implementations. The public audio-caller proof is measured separately.
"""

from __future__ import annotations

import io
import math
import struct
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
for source in (
    REPO_ROOT / "ananta" / "src",
    REPO_ROOT / "plugins" / "default_blob_storage_plugin" / "src",
):
    sys.path.insert(0, str(source))

from ananta.interfaces.state_service_protocol import StateServiceProtocol  # noqa: E402

from default_blob_storage_plugin.plugin import DefaultBlobStoragePlugin  # noqa: E402
from default_blob_storage_plugin.providers.filesystem_provider import (  # noqa: E402
    FilesystemProvider,
)


class MetadataState:
    """Implement the metadata write/update envelopes used by real store_blob."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        assert namespace == "default_blob_storage_plugin"
        assert data["table"] == "metadata"
        blob_id = f"bmd-{len(self.records) + 1:013d}"
        self.records[blob_id] = {**data["record"], "id": blob_id}
        return {"action_status": "completed", "data": {"result": {"generated_id": blob_id}}}

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        assert namespace == "default_blob_storage_plugin"
        assert query["table"] == "metadata"
        self.records[query["filters"]["id"]].update(updates)
        return {"action_status": "completed", "data": {"result": {"updated": 1}}}


def audio_bytes() -> bytes:
    """A short PCM WAV exercises the real decoder without external fixtures."""
    content = io.BytesIO()
    with wave.open(content, "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(b"".join(struct.pack("<h", round(1000 * math.sin(2 * math.pi * 440 * n / 8000))) for n in range(1600)))
    return content.getvalue()


def exercise_paths(provider: FilesystemProvider, plugin: DefaultBlobStoragePlugin, blob_id: str, content: bytes) -> None:
    expected = provider.blobs_dir / blob_id
    for resolver in (provider.resolve_blob_path, plugin.resolve_blob_path):
        for reference in (blob_id, f"blob://{blob_id}"):
            path = resolver(reference)
            assert path is not None, reference
            assert Path(path).samefile(expected), path
            assert Path(path).read_bytes() == content, path
        assert resolver("bmd-9999999999999") is None
        assert resolver("blob://bmd-9999999999999") is None

    exercise_invalid_paths(provider, plugin)
    exercise_alias(provider, blob_id, expected)
    expected.unlink()
    assert provider.resolve_blob_path(blob_id) is None
    assert plugin.resolve_blob_path(f"blob://{blob_id}") is None
    print("PASS bare ID/URL and missing/deleted blobs")


def exercise_invalid_paths(provider: FilesystemProvider, plugin: DefaultBlobStoragePlugin) -> None:
    outside = provider.blobs_dir.parent / "outside"
    outside.write_bytes(b"outside sentinel")
    (provider.blobs_dir / "escape").symlink_to(outside)
    (provider.blobs_dir / "directory").mkdir()
    (provider.blobs_dir / "loop").symlink_to("loop")
    for resolver in (provider.resolve_blob_path, plugin.resolve_blob_path):
        for identifier in ("", ".", "..", "../outside", str(outside), "escape", "directory", "loop"):
            for reference in (identifier, f"blob://{identifier}"):
                assert resolver(reference) is None, reference
    assert outside.read_bytes() == b"outside sentinel"
    print("PASS traversal, directory, loop and escaping symlink containment")


def exercise_alias(provider: FilesystemProvider, blob_id: str, expected: Path) -> None:

    # A physically equivalent app-home alias remains valid; an escaping blob
    # symlink is still refused under that alias (macOS /var aliases included).
    alias = Path(provider.app_home).parent / "app-alias"
    alias.symlink_to(Path(provider.app_home), target_is_directory=True)
    aliased = FilesystemProvider(str(alias), {})
    assert Path(cast(str, aliased.resolve_blob_path(blob_id))).samefile(expected)
    assert aliased.resolve_blob_path("escape") is None
    print("PASS physical app-home alias resolves stored blob and refuses escaping symlink")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="blob-path-contract-") as directory:
        provider = FilesystemProvider(str(Path(directory) / "app"), {})
        provider.set_state_service(cast(StateServiceProtocol, MetadataState()))
        plugin = DefaultBlobStoragePlugin()
        plugin.provider = provider
        content = audio_bytes()
        stored = plugin.store_blob("audio_processing_plugin", content, {"filename": "tone.wav", "mime_type": "audio/wav"})
        assert stored.get("action_status") == "completed", stored
        stored_data = cast(dict[str, Any], stored.get("data"))
        blob_id = str(stored_data["blob_id"])
        assert (provider.blobs_dir / blob_id).read_bytes() == content
        assert str(stored_data["file_path"]).endswith(f"/blobs/{blob_id}")
        print(f"Stored through real provider: {blob_id}; filename equals blob_id", flush=True)
        exercise_paths(provider, plugin, blob_id, content)
    print("blob_path_contract_smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
