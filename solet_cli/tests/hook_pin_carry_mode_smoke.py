"""Resume refuses later chmod, including permission bits invisible to Git."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hook_pin_carry_fixture import MANIFESTS as _MANIFESTS  # noqa: E402
from _hook_pin_carry_fixture import fixture_home as _home  # noqa: E402
from hook_pin_carry_crash_smoke import InjectedCrashError, _crash_at, _fixture  # noqa: E402
from solet_manager import update_execution as execution  # noqa: E402
from solet_manager.errors import StateError, UpdateBlockedError  # noqa: E402
from solet_manager.update_reconcile import abandon_update  # noqa: E402


def _snapshot(target: Path) -> dict[str, tuple[bytes, int]]:
    return {path: ((target / path).read_bytes(), stat.S_IMODE((target / path).lstat().st_mode)) for path in _MANIFESTS}


def _one(root: Path, *, mode: int, index: int, boundary: str, abandon: bool) -> None:
    fixture = _fixture(root)
    with _home(fixture):
        preview = execution.preview_update_instance(fixture.request)
        if preview.status != "preview_ready":
            raise AssertionError(preview)
        fingerprint = cast(str, preview.data["approval_fingerprint"])
        try:
            with _crash_at(boundary):
                execution.apply_update(fixture.request, fingerprint)
        except InjectedCrashError:
            pass
        else:
            raise AssertionError("mode-drift fixture did not reach its crash boundary")
        (fixture.target / _MANIFESTS[index]).chmod(mode)
        before = _snapshot(fixture.target)
        try:
            if abandon:
                abandon_update(fixture.request)
            else:
                execution.apply_update(fixture.request, fingerprint)
        except (UpdateBlockedError, StateError):
            pass
        else:
            raise AssertionError("hook-pin re-entry must refuse later permission changes")
        if _snapshot(fixture.target) != before:
            raise AssertionError("mode-drift refusal must preserve the entire byte-and-mode batch")
    print(f"PASS {'abandon' if abandon else 'resume'} {boundary} path={index} mode={mode:o}")


def main(*, abandon: bool = False) -> int:
    cases = [(mode, index, "before-merge:1") for mode in (0o755, 0o600) for index in (0, 1)]
    # Already-pinned bytes must still get the complete mode preflight.
    cases.append((0o600, 1, "backup:2"))
    if not abandon:
        # A post-fast-forward resume must not partially re-pin the first path.
        cases.append((0o600, 1, "after-merge:1"))
    with TemporaryDirectory(prefix="pin-carry-mode-") as temporary:
        root = Path(temporary).resolve()
        for number, (mode, index, boundary) in enumerate(cases):
            _one(root / str(number), mode=mode, index=index, boundary=boundary, abandon=abandon)
    print(f"hook_pin_carry_mode_smoke OK: {len(cases)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
