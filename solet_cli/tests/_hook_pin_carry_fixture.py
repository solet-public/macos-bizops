"""Factory-free hook-pin fixture shared by the hook_pin_carry_* smokes.

The born seed excludes ``hook_interpreter_pin_update_smoke`` and cannot run
``_step7_support`` (both need the origin-only seed factory). This fixture keeps
the same subject on the shipped ``_step5_support`` builder: two sealed releases
whose two coordination-hook manifests change between baseline and candidate,
an enrolled target, and both baseline manifests pinned by the real installer
transform before the update runs.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import patch

from _step5_support import Fixture, build_fixture, git
from github_midwife_plugin.setup_adapter_runtime import Runtime
from github_midwife_plugin.setup_plugin_operations import _patch_hook_manifest  # pyright: ignore[reportPrivateUsage]

NAME = "fixture"
MANIFESTS = (
    "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json",
    "plugins/github_midwife_plugin/codex_plugin/coordination-hooks/hooks/hooks.json",
)


class _InstallerRuntime:
    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)


def _manifest(description: str) -> str:
    hooks = {"PostToolUse": [{"hooks": [{"type": "command", "command": "python3 hooks/fixture.py"}]}]}
    return json.dumps({"description": description, "hooks": hooks}, indent=2) + "\n"


def pinned_fixture(root: Path) -> Fixture:
    """Enrolled baseline whose two hook manifests carry the installer pin; the candidate changes both."""
    baseline: dict[str, str | bytes] = {path: _manifest("baseline hook manifest") for path in MANIFESTS}
    candidate: dict[str, str | bytes] = {path: _manifest("new release manifest for crash injection") for path in MANIFESTS}
    fixture = build_fixture(root, baseline_extra=baseline, extra_candidate_files=candidate, truthful=True)
    assert fixture.target == fixture.target.resolve(), "the pinned target carries no symlinked component"
    for path in MANIFESTS:
        error = _patch_hook_manifest(fixture.target / path, fixture.target, cast(Runtime, _InstallerRuntime()))
        assert error is None, ("real installer hook pin", path, error)
    changed = git(fixture.target, "diff", "--name-only").splitlines()
    assert changed == list(MANIFESTS), f"both pinned manifests are the only tracked modifications: {changed!r}"
    cache = fixture.home / ".claude/plugins/cache" / NAME / "coordination-hooks/1.0.0/hooks/hooks.json"
    shutil.copyfile(fixture.target / MANIFESTS[0], cache)
    return fixture


@contextmanager
def fixture_home(fixture: Fixture) -> Generator[None]:
    with patch.object(Path, "home", classmethod(lambda cls: fixture.home)):
        yield
