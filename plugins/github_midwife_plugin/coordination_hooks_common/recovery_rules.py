"""Recovery rules: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

from recovery_metadata import physically_inside
from recovery_types import RecoveryError


class RuleWorkspace:
    """Ephemeral ref ignore rules, always physically outside the store."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def rule_candidates(self) -> tuple[Path, ...]:
        candidates = [Path(tempfile.gettempdir())]
        if sys.platform == "darwin":
            with suppress(ValueError, OSError):
                value = os.confstr(65537)
                if value:
                    candidates.append(Path(value))
            candidates.append(Path.home() / "Library/Caches/Solet/recovery-rules")
        elif os.environ.get("XDG_RUNTIME_DIR"):
            candidates.append(Path(os.environ["XDG_RUNTIME_DIR"]))
        return tuple(candidates)

    @contextmanager
    def rules_directory(self) -> Generator[Path]:
        for candidate in self.rule_candidates():
            try:
                # Reject before creation too: never sweep another user's tree.
                if candidate.exists() and physically_inside(candidate, self.root):
                    continue
                candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.sweep_rules(candidate)
                directory = Path(tempfile.mkdtemp(prefix=f"solet-recovery-rules-{os.getpid()}-", dir=candidate))
                os.chmod(directory, 0o700)
            except OSError:
                continue
            if physically_inside(directory, self.root):
                directory.rmdir()
                continue
            try:
                yield directory
            finally:
                shutil.rmtree(directory)
            return
        raise RecoveryError("no_safe_rules_directory")

    @staticmethod
    def sweep_rules(candidate: Path) -> None:
        for path in candidate.glob("solet-recovery-rules-*"):
            info = path.lstat()
            match = re.fullmatch(r"solet-recovery-rules-(\d+)-.+", path.name)
            if not match or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or time.time() - info.st_mtime < 600:
                continue
            try:
                os.kill(int(match.group(1)), 0)
            except ProcessLookupError:
                shutil.rmtree(path)
            except PermissionError:
                pass
