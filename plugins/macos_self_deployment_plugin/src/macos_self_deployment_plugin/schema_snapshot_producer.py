"""§3 schema-snapshot producer (B1) — snapshot the declared schema of a code tree.

Extracted from :class:`~.swap_orchestrator.SwapOrchestrator` so that class stays
under the god-class LOC bound (coherent submodules, not allowlist additions, per
the operator's 2026-05-25 framing). The orchestrator wires the returned closure
into ``ReleaseManager.build_candidate(schema_snapshot_fn=...)`` for the forward
cutover AND calls it directly to DERIVE the current release's snapshot when the
current ``VERSION`` predates the producer (the B1·1 baseline derive).

The closure runs ``schema_snapshot_collector`` as a SUBPROCESS, **by FILE PATH**
(the SOURCE/new collector — NOT ``-m``), with ``PYTHONPATH`` pointed at the
target code tree ``T`` it is handed:

- B1·2 candidate snapshot — ``T = <staging>/code`` (the FROZEN CoW clone).
  Honoring ``code_root`` (vs the round-1 bug of snapshotting live source) makes
  the snapshot immune to a concurrent source edit between clone and snapshot.
- B1·1 baseline derive — ``T = <current>/code`` (an old release's tree). The
  SOURCE collector logic introspects the OLD tree's declared schemas; the old
  bundle never runs the collector itself (it may not have one).

``PYTHONPATH`` precedes the source venv's site ``.pth``, so ``import ananta`` and
every plugin module resolve to ``T``. The collector's provenance assert
(``EXPECT_ROOT = T``) fails closed if anything loaded from elsewhere. ANY failure
raises :class:`ReleaseManagerError` — the orchestrator's ``except
ReleaseManagerError`` turns it into a refused deploy. The closure NEVER returns
``None`` (a ``None`` reaching the gate alongside an existing current release is a
distinct fail-closed cell): it returns the parsed snapshot dict or raises.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final

from macos_self_deployment_plugin.release_manager import ReleaseManagerError

# The DB-free plugin-set collection measured ~2s; 120s is a generous ceiling.
_TIMEOUT_SECONDS: Final[float] = 120.0
_SOURCE_VENV_DIRNAME: Final[str] = ".venv"
# The collector file, relative to the SOURCE repo root (run-by-path target).
_COLLECTOR_RELPATH: Final[tuple[str, ...]] = (
    "plugins", "macos_self_deployment_plugin", "src",
    "macos_self_deployment_plugin", "schema_snapshot_collector.py",
)


def _pythonpath_for_tree(code_root: Path) -> str:
    """``PYTHONPATH`` that resolves ``ananta`` + every plugin to ``code_root``.

    The ``plugins/*/src`` glob is expanded HERE in Python — ``subprocess.run``
    takes no shell, so a literal ``*`` would not glob. ``ananta/src`` is a fixed
    path. An empty/malformed tree yields a thin path; the subprocess then fails
    its imports / completeness assert → fail-closed, never a silent source read.
    """
    entries = [str(code_root / "ananta" / "src")]
    entries.extend(str(src) for src in sorted((code_root / "plugins").glob("*/src")))
    return os.pathsep.join(entries)


def build_schema_snapshot_fn(
    *, solet_name: str, app_home: Path, source_root: Path,
) -> Callable[[Path], dict[str, object]]:
    """Return a ``(code_root) -> snapshot`` collector closure (candidate + derive)."""
    venv_python = source_root / _SOURCE_VENV_DIRNAME / "bin" / "python3"
    collector_path = source_root.joinpath(*_COLLECTOR_RELPATH)

    def _snapshot(code_root: Path) -> dict[str, object]:
        env = dict(os.environ)
        env["SOLET_NAME"] = solet_name
        env["APP_HOME"] = str(app_home)
        env["EXPECT_ROOT"] = str(code_root.resolve())
        env["PYTHONPATH"] = _pythonpath_for_tree(code_root)
        try:
            result = subprocess.run(
                [str(venv_python), str(collector_path)],
                capture_output=True, text=True, env=env, cwd=str(source_root),
                timeout=_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            msg = f"§3 schema-snapshot collector failed to launch: {exc}"
            raise ReleaseManagerError(msg) from exc
        if result.returncode != 0:
            msg = (
                f"§3 schema-snapshot collector exited {result.returncode} for "
                f"tree {code_root}: {result.stderr.strip()[:500]}"
            )
            raise ReleaseManagerError(msg)
        try:
            snapshot = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            msg = f"§3 schema-snapshot collector emitted invalid JSON: {exc}"
            raise ReleaseManagerError(msg) from exc
        if not isinstance(snapshot, dict):
            msg = "§3 schema-snapshot collector did not emit a JSON object"
            raise ReleaseManagerError(msg)
        return snapshot

    return _snapshot


__all__ = ["build_schema_snapshot_fn"]
