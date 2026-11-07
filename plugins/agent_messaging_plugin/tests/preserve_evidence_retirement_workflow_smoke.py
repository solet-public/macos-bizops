#!/usr/bin/env python3
"""Prove native reviewer evidence preservation and pinned retirement guidance."""

from __future__ import annotations

import json
import sys
import tempfile
from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lane_worktrees_smoke import _index_state, _tree_state  # noqa: E402
from retire_worktree_disposition_smoke import _contained_git, _packet_retire, _pinned  # noqa: E402
from session_lifecycle_transport_smoke import PacketLane, _reconstruct, packet_lane  # noqa: E402

import agent_messaging_plugin.session_lifecycle_control as session_lifecycle_control  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as lifecycle_verbs  # noqa: E402


def _process_documents() -> dict[str, dict[str, Any]]:
    process_root = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "knowledge_base" / "processes"
    expected = {
        "preserve_lane_evidence.json": "plugin::agent_messaging_plugin::preserve_lane_evidence",
        "read_lane_evidence.json": "plugin::agent_messaging_plugin::read_lane_evidence",
        "retire_session.json": "plugin::agent_messaging_plugin::retire_session",
    }
    documents: dict[str, dict[str, Any]] = {}
    for name, process_key in expected.items():
        document = json.loads((process_root / name).read_text(encoding="utf-8"))
        assert document["process_key"] == process_key, (name, document.get("process_key"))
        documents[name] = document
    return documents


def _assert_preserve_read_document_claims(documents: dict[str, dict[str, Any]]) -> None:
    for name in ("preserve_lane_evidence.json", "read_lane_evidence.json"):
        document = documents[name]
        text = " ".join((document["description"], document["error_processor_customizations"]["action_context"]))
        assert "Retirement does not consume these packets yet" not in text, name
        assert "retirement remains unchanged" not in text, name
        assert "manual" in text.lower() and "native" in text.lower() and "external" in text.lower(), name
        assert "landing" in text.lower() and "host death" in text.lower() and "deletion authority" in text.lower(), name


def _assert_process_authority_claims(documents: dict[str, dict[str, Any]]) -> None:
    preserve = documents["preserve_lane_evidence.json"]
    preserve_text = " ".join((preserve["description"], preserve["error_processor_customizations"]["action_context"]))
    assert "without terminating a host or removing a worktree" in preserve_text, "preserve has no host/removal authority"
    read = documents["read_lane_evidence.json"]
    read_text = " ".join((read["description"], read["error_processor_customizations"]["action_context"]))
    assert "No host termination or removal occurs" in read_text, "read has no host/removal authority"
    recovery = documents["retire_session.json"]["error_processor_customizations"]["recovery_guidance"]
    assert "preserve_lane_evidence" in recovery, "retirement recovery must name native preserve"


def _assert_process_document_contract() -> int:
    """Shipped process guidance matches the native packet retirement workflow."""
    documents = _process_documents()
    _assert_preserve_read_document_claims(documents)
    _assert_process_authority_claims(documents)
    return 11


@dataclass
class _ReviewCopyFixture:
    lane: PacketLane
    external_root: Path
    original: dict[str, bytes]
    modes: dict[str, int]
    external_before: dict[str, tuple[bytes, int]]
    tree_before: dict[str, tuple[str, bool, bytes]]
    index_before: tuple[str, str]
    tip: str


def _external_review_snapshot(external_root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        f"workbench/review-custody/file-{index:03d}.bin": (
            (external_root / f"workbench/review-custody/file-{index:03d}.bin").read_bytes(),
            (external_root / f"workbench/review-custody/file-{index:03d}.bin").stat().st_mode & 0o777,
        )
        for index in range(113)
    }


def _create_review_copy_fixture(root: Path) -> _ReviewCopyFixture:
    lane = packet_lane(root, "retire-review-copy", unique_state=False)
    external_root = root / "Controller copy"
    original: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    for index in range(113):
        relative = f"workbench/review-custody/file-{index:03d}.bin"
        payload = f"reviewer custody {index:03d}\r\n".encode() + bytes((index, 0, 255)) + (b"line\r\n" if index % 2 == 0 else b"no-final-newline")
        mode = 0o755 if index % 2 else 0o644
        path = lane.lane.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)
        copy = external_root / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(payload)
        copy.chmod(mode)
        original[relative] = payload
        modes[relative] = mode
    external_before = _external_review_snapshot(external_root)
    assert external_before == {relative: (payload, modes[relative]) for relative, payload in original.items()}
    return _ReviewCopyFixture(
        lane=lane,
        external_root=external_root,
        original=original,
        modes=modes,
        external_before=external_before,
        tree_before=_tree_state(lane.lane.path),
        index_before=_index_state(lane.lane, lane.environment),
        tip=lane.tip(),
    )


def _assert_unpinned_review_retirement(fixture: _ReviewCopyFixture) -> None:
    with (
        patch.object(session_lifecycle_control, "_terminate_host", wraps=session_lifecycle_control._terminate_host) as host,
        patch.object(lifecycle_verbs, "remove_lane_worktree", wraps=lifecycle_verbs.remove_lane_worktree) as remove,
    ):
        unpinned = fixture.lane.call("retire_session")
        assert (unpinned.get("error") or {}).get("code") == "lane_worktree_not_disposable", unpinned
        assert host.call_count == 0 and remove.call_count == 0, (host.call_count, remove.call_count)
    assert fixture.lane.lane.path.exists() and _external_review_snapshot(fixture.external_root) == fixture.external_before
    assert (fixture.lane.lane.path / "workbench" / "review-custody" / "file-000.bin").read_bytes() == fixture.original["workbench/review-custody/file-000.bin"]


def _capture_first_review_packet(fixture: _ReviewCopyFixture) -> dict[str, Any]:
    with (
        patch.object(session_lifecycle_control, "_terminate_host", wraps=session_lifecycle_control._terminate_host) as host,
        patch.object(lifecycle_verbs, "remove_lane_worktree", wraps=lifecycle_verbs.remove_lane_worktree) as remove,
    ):
        snapshot = fixture.lane.call("read_lane_evidence")
        snapshot_data = snapshot["data"]
        assert snapshot_data["snapshot_only"] is True and snapshot_data["custody_verified"] is False
        first = fixture.lane.call(
            "preserve_lane_evidence",
            expected_fingerprint=snapshot_data["fingerprint"],
            disposition="preserved",
            rationale="contained 113-file reviewer-copy workflow fixture",
        )
        assert first.get("action_status") == "completed" and first.get("error") is None, first
        first_packet = first["data"]
        assert first_packet["complete"] is True and first_packet["custody_verified"] is True, first_packet
        first_read = fixture.lane.call("read_lane_evidence", packet_id=first_packet["packet_id"])
        assert first_read["data"]["custody_verified"] is True and first_read["data"]["complete"] is True, first_read
        assert first_read["data"]["deletion_authority"] is False, first_read
        assert first_read["data"]["binding"]["unit_id"] == fixture.lane.identity["unit_id"], first_read
        assert first_read["data"]["provenance"]["disposition"] == "preserved", first_read
        assert host.call_count == 0 and remove.call_count == 0, (host.call_count, remove.call_count)
    assert _external_review_snapshot(fixture.external_root) == fixture.external_before
    return first_packet


def _assert_review_copy_drift_refusal(fixture: _ReviewCopyFixture, first_packet: dict[str, Any]) -> None:
    drift_path = fixture.lane.lane.path / "workbench/review-custody/file-000.bin"
    drift_path.write_bytes(b"changed after explicit capture\r\n")
    with (
        patch.object(session_lifecycle_control, "_terminate_host", wraps=session_lifecycle_control._terminate_host) as host,
        patch.object(lifecycle_verbs, "remove_lane_worktree", wraps=lifecycle_verbs.remove_lane_worktree) as remove,
    ):
        drift, _ = _packet_retire(fixture.lane, **_pinned(first_packet))
        assert host.call_count == 0 and remove.call_count == 0, (host.call_count, remove.call_count)
    drift_message = str((drift.get("error") or {}).get("message"))
    assert (drift.get("error") or {}).get("code") == "lane_worktree_not_disposable" and "custody_lane_changed" in drift_message, drift
    assert fixture.lane.lane.path.exists() and _external_review_snapshot(fixture.external_root) == fixture.external_before
    drift_path.write_bytes(fixture.original["workbench/review-custody/file-000.bin"])
    drift_path.chmod(fixture.modes["workbench/review-custody/file-000.bin"])


def _capture_successor_review_packet(fixture: _ReviewCopyFixture, first_packet: dict[str, Any]) -> dict[str, Any]:
    with (
        patch.object(session_lifecycle_control, "_terminate_host", wraps=session_lifecycle_control._terminate_host) as host,
        patch.object(lifecycle_verbs, "remove_lane_worktree", wraps=lifecycle_verbs.remove_lane_worktree) as remove,
    ):
        fresh = fixture.lane.call("read_lane_evidence")
        successor = fixture.lane.call(
            "preserve_lane_evidence",
            expected_fingerprint=fresh["data"]["fingerprint"],
            disposition="preserved",
            rationale="new explicit capture after the fixture drift check",
            predecessor_packet_id=first_packet["packet_id"],
        )
        assert successor.get("action_status") == "completed" and successor.get("error") is None, successor
        successor_packet = successor["data"]
        assert successor_packet["complete"] is True and successor_packet["custody_verified"] is True, successor_packet
        old_read = fixture.lane.call("read_lane_evidence", packet_id=first_packet["packet_id"])
        successor_read = fixture.lane.call("read_lane_evidence", packet_id=successor_packet["packet_id"])
        assert old_read["data"]["custody_verified"] is True and successor_read["data"]["custody_verified"] is True
        assert successor_read["data"]["deletion_authority"] is False, successor_read
        assert successor_read["data"]["provenance"]["predecessor"] == first_packet["packet_id"], successor_read
        assert host.call_count == 0 and remove.call_count == 0, (host.call_count, remove.call_count)
    return successor_packet


def _retire_review_copy(fixture: _ReviewCopyFixture, successor_packet: dict[str, Any]) -> None:
    retired, hosts = _packet_retire(fixture.lane, **_pinned(successor_packet))
    assert retired.get("action_status") == "completed" and retired.get("error") is None, retired
    assert hosts == 1 and retired["data"]["custody_packet"]["worktree_disposition"] == "removed", retired
    assert not fixture.lane.lane.path.exists() and fixture.lane.tip() == fixture.tip, (fixture.lane.tip(), fixture.tip)
    assert _external_review_snapshot(fixture.external_root) == fixture.external_before


def _verify_review_packet_payloads(fixture: _ReviewCopyFixture, successor_packet: dict[str, Any]) -> None:
    with (
        patch.object(session_lifecycle_control, "_terminate_host", wraps=session_lifecycle_control._terminate_host) as host,
        patch.object(lifecycle_verbs, "remove_lane_worktree", wraps=lifecycle_verbs.remove_lane_worktree) as remove,
    ):
        for relative, payload in fixture.original.items():
            read = fixture.lane.call(
                "read_lane_evidence",
                packet_id=successor_packet["packet_id"],
                payload_path=relative,
                payload_layer="worktree",
            )
            assert read.get("action_status") == "completed" and read.get("error") is None, (relative, read)
            assert b64decode(read["data"]["payload_base64"]) == payload, relative
        assert host.call_count == 0 and remove.call_count == 0, (host.call_count, remove.call_count)


def _verify_review_tree_reconstruction(fixture: _ReviewCopyFixture, successor_packet: dict[str, Any]) -> None:
    assert _reconstruct(fixture.lane, successor_packet["packet_id"]) == (fixture.tree_before, fixture.index_before), "verified reconstruction preserves every Git mode and index entry"
    reconstructed = _tree_state(fixture.lane.lane.path)
    for relative, mode in fixture.modes.items():
        assert reconstructed[relative][0] == "file" and reconstructed[relative][1] == bool(mode & 0o100), (relative, reconstructed.get(relative))
    assert _external_review_snapshot(fixture.external_root) == fixture.external_before
    assert len(fixture.original) == 113 and len(fixture.modes) == 113


def _assert_review_copy_workflow(root: Path) -> int:
    """Native custody recovers a synthetic Controller-copy set after pinned retirement."""
    fixture = _create_review_copy_fixture(root)
    _assert_unpinned_review_retirement(fixture)
    first_packet = _capture_first_review_packet(fixture)
    _assert_review_copy_drift_refusal(fixture, first_packet)
    successor_packet = _capture_successor_review_packet(fixture, first_packet)
    _retire_review_copy(fixture, successor_packet)
    _verify_review_packet_payloads(fixture, successor_packet)
    _verify_review_tree_reconstruction(fixture, successor_packet)
    return 17 + len(fixture.original)


def main() -> int:
    checks = _assert_process_document_contract()
    with tempfile.TemporaryDirectory(prefix="preserve-evidence-workflow-") as temporary:
        root = Path(temporary).resolve()
        with _contained_git(root):
            checks += _assert_review_copy_workflow(root)
    print(f"preserve_evidence_retirement_workflow_smoke OK: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
