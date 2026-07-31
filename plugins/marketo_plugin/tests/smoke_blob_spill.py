#!/usr/bin/env python3
"""Blob-spill service-resolution smoke tests for marketo_plugin.

Hermetic — a MagicMock orchestrator/blob service, no live platform.

Regression under test: the platform constructs blob_storage_service in the
init_service_manager startup step, AFTER every plugin's prepare_for_readiness.
The pre-fix template resolved the service at readiness and cached the None
miss forever, so every over-cap result spill hard-failed with
blob_storage_service_not_available even though the blob plugin was ready
(field-verified on a live deployment). The fix
resolves the service lazily at first spill and makes the unavailable error
self-describing (observed payload bytes + the inline-return cap).

Exercises:
  1. _store_blob resolves blob_storage_service at point of use when nothing
     was cached at readiness (the exact broken ordering), and succeeds
  2. the resolved service is cached — one get_service call across two spills
  3. service genuinely unavailable -> error message carries the observed
     payload size and the INLINE_BYTE_CAP value so a caller can compute a
     working batch size without bisecting

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 \
        plugins/marketo_plugin/tests/smoke_blob_spill.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))

from marketo_plugin.constants import INLINE_BYTE_CAP  # noqa: E402
from marketo_plugin.errors import MarketoServiceError  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}  {msg}")


def _orchestrator_with_blob_service() -> tuple[MagicMock, MagicMock]:
    blob_service = MagicMock()
    blob_service.store_blob.return_value = {
        "action_status": "completed",
        "data": {"blob_id": "blob-spill-1"},
    }
    orch = MagicMock()
    orch.get_service.return_value = blob_service
    return orch, blob_service


def test_spill_resolves_service_at_point_of_use() -> None:
    """§20.1 regression: nothing cached at readiness, service exists at use time."""
    plugin = MarketoPlugin()
    orch, blob_service = _orchestrator_with_blob_service()
    plugin.orchestrator_ref = orch
    # _blob_storage_service is None, exactly as after the (fixed) readiness path
    blob_id = plugin._store_blob(b"x" * 64, "get_leads_results.json", "application/json")
    _assert("spill succeeds via point-of-use resolution", blob_id == "blob-spill-1")
    _assert(
        "resolution asked for blob_storage_service",
        orch.get_service.call_args is not None
        and orch.get_service.call_args.args == ("blob_storage_service",),
        str(orch.get_service.call_args),
    )
    _assert("store_blob received the payload", blob_service.store_blob.called)


def test_resolved_service_is_cached() -> None:
    plugin = MarketoPlugin()
    orch, _ = _orchestrator_with_blob_service()
    plugin.orchestrator_ref = orch
    plugin._store_blob(b"a", "one.json", "application/json")
    plugin._store_blob(b"b", "two.json", "application/json")
    _assert(
        "one get_service call across two spills",
        orch.get_service.call_count == 1,
        str(orch.get_service.call_count),
    )


def test_unavailable_error_is_self_describing() -> None:
    plugin = MarketoPlugin()
    orch = MagicMock()
    orch.get_service.return_value = None
    plugin.orchestrator_ref = orch
    payload = b"z" * 12345
    raised: MarketoServiceError | None = None
    try:
        plugin._store_blob(payload, "get_leads_results.json", "application/json")
    except MarketoServiceError as exc:
        raised = exc
    _assert("unavailable spill raises the typed error", raised is not None)
    message = str(raised)
    _assert("error names the observed payload size", "12345" in message, message)
    _assert("error names the inline-return cap", str(INLINE_BYTE_CAP) in message, message)


def main() -> int:
    print("\nmarketo_plugin blob-spill service-resolution smoke tests")
    print("=" * 66)
    test_spill_resolves_service_at_point_of_use()
    test_resolved_service_is_cached()
    test_unavailable_error_is_self_describing()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All blob-spill service-resolution smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
