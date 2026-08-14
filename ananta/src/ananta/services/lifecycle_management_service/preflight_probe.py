"""L2 fresh-source manifest-preflight probe — subprocess entrypoint (GTE-06).

Executed by the deployment plugin's probe harness as::

    <candidate>/venv/bin/python3 -m ananta.services.lifecycle_management_service.preflight_probe

with the manifest JSON on stdin. Because the module ships INSIDE every
materialized release's ``code/`` copy and runs under the candidate's own
interpreter, the imports it performs resolve against the RELEASE source
with a fresh ``sys.modules`` and fresh ``importlib.metadata`` — closing
the in-process preflight's cache-poisoning limit (see the
:mod:`manifest_preflight` module docstring) and the entry-point-staleness
false-reject (the 2026-07-06 "L1.1 boot-stale" specimen).

Contract (design ``workbench/2026-07-06_gte06_fresh_source_preflight_probe_design.md`` §5):

* stdin — one JSON object, the effective manifest (``{"plugins": [...]}``).
* stdout — exactly one JSON envelope::

      {"probe_version": 1, "release_id": "...", "interpreter": "...",
       "ok": bool, "failures": [{check, plugin, message, error_class}, ...],
       "duration_ms": int}

* exit code — ``0`` (ran, ``ok=true``), ``3`` (ran, preflight failures
  listed), anything else = harness/environment error (traceback on
  stderr; the harness classifies it RED).

The probe deliberately reuses :func:`run_manifest_preflight` verbatim —
one validation function, two execution contexts, zero drift surface.
Unlike the L1 call site, the probe context NEVER defers
``EntryPointMissingError`` (design §4/A2): a fresh interpreter's
entry-point scan is authoritative, so a missing entry point here is a
real rejection.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Final

from ananta.services.lifecycle_management_service.manifest_preflight import (
    run_manifest_preflight,
)

PROBE_VERSION: Final[int] = 1
EXIT_OK: Final[int] = 0
EXIT_PREFLIGHT_FAILURES: Final[int] = 3

_ENV_RELEASE_ID: Final[str] = "SOLET_RELEASE_ID"


def build_probe_envelope(
    manifest: dict[str, Any], *, release_id: str
) -> tuple[int, dict[str, Any]]:
    """Run the preflight in THIS interpreter; return ``(exit_code, envelope)``."""
    started = time.monotonic()
    result = run_manifest_preflight(manifest)
    envelope: dict[str, Any] = {
        "probe_version": PROBE_VERSION,
        "release_id": release_id,
        "interpreter": sys.executable,
        "ok": result.ok,
        "failures": [
            {
                "check": failure.check,
                "plugin": failure.plugin,
                "message": failure.message,
                "error_class": failure.error_class,
            }
            for failure in result.failures
        ],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    return (EXIT_OK if result.ok else EXIT_PREFLIGHT_FAILURES), envelope


def main() -> int:
    """Read manifest JSON from stdin, emit the envelope on stdout.

    Any exception (unparseable stdin, non-object manifest, an unexpected
    crash inside the preflight machinery) propagates: traceback on
    stderr + a non-{0,3} exit code, which the harness classifies as a
    RED ``ProbeHarnessError`` — fail-LOUD, never degrade to warn.
    """
    manifest_raw: Any = json.loads(sys.stdin.read())
    if not isinstance(manifest_raw, dict):
        raise TypeError(
            "manifest on stdin must be a JSON object; "
            f"got {type(manifest_raw).__name__}"
        )
    exit_code, envelope = build_probe_envelope(
        manifest_raw, release_id=os.environ.get(_ENV_RELEASE_ID, "")
    )
    sys.stdout.write(json.dumps(envelope))
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
