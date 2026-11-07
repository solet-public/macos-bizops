"""Changed hook manifests survive every restore/ff/repin journal boundary."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hook_pin_carry_fixture import MANIFESTS as _MANIFESTS  # noqa: E402
from _hook_pin_carry_fixture import NAME, pinned_fixture  # noqa: E402
from _hook_pin_carry_fixture import fixture_home as _home  # noqa: E402
from _step5_support import Fixture  # noqa: E402
from solet_manager import update_execution as execution  # noqa: E402
from solet_manager import update_hook_pin_carry as carry  # noqa: E402
from solet_manager.managed_artifact_backup import BackupRecord  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.update_journal import read_update_journal  # noqa: E402
from solet_manager.update_reconcile import abandon_update  # noqa: E402
from solet_setup_contracts.hook_interpreter_pin import instance_interpreter, pin_hook_interpreter  # noqa: E402


class InjectedCrashError(RuntimeError):
    """An effect happened but its caller did not complete."""


def _fixture(root: Path) -> Fixture:
    return pinned_fixture(root)


def _journal(fixture: Fixture) -> dict[str, JsonValue]:
    active = fixture.record().active_operation
    assert active is not None
    return read_update_journal(fixture.paths.operation_path(fixture.record().instance_id, active.operation_id))


def _assert_pinned(fixture: Fixture) -> None:
    journal = _journal(fixture)
    local = cast(dict[str, JsonValue], journal["local_state"])
    current = cast(dict[str, JsonValue], local["current"])
    rows = cast(list[dict[str, JsonValue]], current["preserved_tracked_paths"])
    revisions = cast(list[dict[str, JsonValue]], local["revisions"])
    assert sum(row["operation_id"] == carry.ACTION for row in revisions if "operation_id" in row) == 1
    for path in _MANIFESTS:
        raw = carry.read_target_blob(fixture.target)(f"HEAD:{path}")
        expected = pin_hook_interpreter(raw, instance_interpreter(fixture.target))
        assert expected is not None and (fixture.target / path).read_bytes() == expected
        row = next(row for row in rows if row["path"] == path)
        assert row["sha256"] == hashlib.sha256(expected).hexdigest(), "pin digest was not rebaselined"


@contextmanager
def _crash_at(boundary: str) -> Generator[None]:
    counts: dict[str, int] = {}

    def tick(kind: str) -> None:
        counts[kind] = counts.get(kind, 0) + 1
        if f"{kind}:{counts[kind]}" == boundary:
            raise InjectedCrashError(boundary)

    real_backup, real_write, real_git, real_journal = carry.write_backup, carry.atomic_replace_bytes, execution._run_git, carry.write_update_journal

    def backup(paths: ManagerPaths, instance: str, operation: str, artifact: str, destination: Path) -> BackupRecord:
        result = real_backup(paths, instance, operation, artifact, destination)
        tick("backup")
        return result

    def write(destination: Path, raw: bytes, *, mode: int) -> None:
        real_write(destination, raw, mode=mode)
        tick("file")

    def git(cwd: Path, args: tuple[str, ...], *, hooks_dir: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        if args[0] == "merge":
            tick("before-merge")
        result = real_git(cwd, args, hooks_dir=hooks_dir)
        if args[0] == "merge":
            tick("after-merge")
        return result

    def journal(path: Path, previous: dict[str, JsonValue] | None, value: dict[str, JsonValue]) -> None:
        tick("before-journal")
        real_journal(path, previous, value)
        tick("after-journal")

    with patch.object(carry, "write_backup", backup), patch.object(carry, "atomic_replace_bytes", write), patch.object(execution, "_run_git", git), patch.object(carry, "write_update_journal", journal):
        yield


def _one(root: Path, boundary: str, *, abandon: bool = False) -> None:
    fixture = _fixture(root)
    cache = fixture.home / ".claude/plugins/cache" / NAME / "coordination-hooks/1.0.0/hooks/hooks.json"
    cached = cache.read_bytes()
    original = {path: (fixture.target / path).read_bytes() for path in _MANIFESTS}
    with _home(fixture):
        preview = execution.preview_update_instance(fixture.request)
        assert preview.status == "preview_ready", preview
        fingerprint = cast(str, preview.data["approval_fingerprint"])
        try:
            with _crash_at(boundary):
                execution.apply_update(fixture.request, fingerprint)
        except InjectedCrashError:
            pass
        else:
            raise AssertionError(f"crash point {boundary} never fired")
        assert cache.read_bytes() == cached, "cached hooks changed inside source transition"
        if abandon:
            result = abandon_update(replace(fixture.request))
            assert result.status == "abandoned", result
            assert all((fixture.target / path).read_bytes() == original[path] for path in _MANIFESTS)
            return
        _resume(fixture, fingerprint, boundary, cache, cached)
    print(f"PASS {boundary} fresh resume and idempotent re-entry")


def _resume(fixture: Fixture, fingerprint: str, boundary: str, cache: Path, cached: bytes) -> None:
    result = execution.apply_update(replace(fixture.request), fingerprint)
    assert result.status == "source_advanced", (boundary, result.status, result.message)
    _assert_pinned(fixture)
    repeated = execution.apply_update(replace(fixture.request), fingerprint)
    assert repeated.status == "source_advanced", repeated
    _assert_pinned(fixture)
    assert cache.read_bytes() == cached


def main(part: int = 1) -> int:
    boundaries = ("backup:1", "backup:2", "file:1", "file:2", "before-merge:1", "after-merge:1", "file:3", "file:4", "before-journal:1", "after-journal:1")
    with TemporaryDirectory(prefix="pin-carry-crash-") as temporary:
        root = Path(temporary).resolve()
        selected = boundaries[:5] if part == 1 else boundaries[5:]
        for index, boundary in enumerate(selected):
            _one(root / str(index), boundary)
        if part == 1:
            _one(root / "abandon", "before-merge:1", abandon=True)
    print(f"hook_pin_carry_crash_smoke OK: part {part}, five boundaries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
