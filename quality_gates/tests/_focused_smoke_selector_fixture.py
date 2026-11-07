"""Fixture repositories for the focused smoke selector smoke.

Every tree is built under its own ``tempfile.TemporaryDirectory`` bound in the
function that writes it, so no path here names a file of the real checkout.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path

_GIT_ROUTING_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)

# Registered smoke paths of the fixture tree, by the rule each one exercises.
LITERAL_PATH_SMOKE = "lit/tests/path_smoke.py"
LITERAL_BASENAME_SMOKE = "lit/tests/basename_smoke.py"
LITERAL_DOTTED_SMOKE = "lit/tests/dotted_smoke.py"
LITERAL_SRC_DOTTED_SMOKE = "lit/tests/src_dotted_smoke.py"
SAME_ROOT_SCANNER_SMOKE = "docs/tests/scan_smoke.py"
NAMED_ROOT_SCANNER_SMOKE = "other/tests/scan_names_smoke.py"
CROSS_ROOT_SCANNER_SMOKE = "solet_cli/tests/real_style_tree_2_smoke.py"
REAL_ROOT_SMOKE = "rr/tests/real_root_smoke.py"
ENTRY_POINT_SMOKE = "ep/tests/loader_smoke.py"
SIBLING_IMPORT_SMOKE = "imp/tests/sibling_smoke.py"
RELATIVE_IMPORT_SMOKE = "plugins/demo_plugin/tests/core_smoke.py"
BROKEN_SMOKE = "bad/tests/broken_smoke.py"
MISSING_SMOKE = "missing/tests/absent_smoke.py"
UNREGISTERED_SMOKE = "lit/tests/unregistered_smoke.py"
# Registered ONLY in a fragment file of its own under gate_smokes.d/, never in
# gate_smokes.txt (prb_ae6d81ea). It names a path no other fixture smoke
# mentions, so it is selected for FRAGMENT_PATH and for nothing else -- the
# other routines' selections are unchanged by its presence.
FRAGMENT_REGISTERED_SMOKE = "frag/tests/fragment_smoke.py"
FRAGMENT_REGISTER_RELPATH = "quality_gates/gate_smokes.d/one-candidate.txt"

# Changed paths the routines feed to the selector.
DATA_PATH = "lit/data/table.csv"
FRAGMENT_PATH = "frag/data/payload.bin"
DOCS_PATH = "docs/beta.txt"
DOTTED_PATH = "tools/delta.py"
SRC_DOTTED_PATH = "pkg/src/pkg/gamma.py"
NAMED_ROOT_PATH = "pkgroot/notes.txt"
PLUGINS_DATA_PATH = "plugins/demo_plugin/data/seed.txt"
TOP_LEVEL_PATH = "README.md"
SIBLING_FIXTURE_PATH = "imp/tests/_helper.py"
DELETED_MODULE_PATH = "plugins/demo_plugin/src/demo_plugin/gone.py"
UNKNOWN_PATH = "nowhere/unknown.bin"

SCANNER_SMOKES = (SAME_ROOT_SCANNER_SMOKE, NAMED_ROOT_SCANNER_SMOKE, CROSS_ROOT_SCANNER_SMOKE)

_FILES: dict[str, str] = {
    LITERAL_PATH_SMOKE: 'TABLE = "lit/data/table.csv"\n',
    LITERAL_BASENAME_SMOKE: 'NAME = "beta.txt"\n',
    LITERAL_DOTTED_SMOKE: 'MODULE = "tools.delta"\n',
    LITERAL_SRC_DOTTED_SMOKE: "import pkg.gamma\n",
    SAME_ROOT_SCANNER_SMOKE: "from pathlib import Path\n\nFILES = sorted(Path('.').rglob('*.txt'))\n",
    NAMED_ROOT_SCANNER_SMOKE: "from pathlib import Path\n\nENTRIES = list(Path('pkgroot').iterdir())\n",
    CROSS_ROOT_SCANNER_SMOKE: "from pathlib import Path\n\nFILES = sorted(Path('.').glob('*/x'))\n",
    REAL_ROOT_SMOKE: "_REPO_ROOT = None\nscan_repository(\n    _REPO_ROOT,\n)\n",
    ENTRY_POINT_SMOKE: 'from importlib.metadata import entry_points\n\nFOUND = entry_points(group="ananta.plugins")\n',
    SIBLING_IMPORT_SMOKE: "from _helper import VALUE\n",
    "imp/tests/_helper.py": "VALUE = 1\n",
    RELATIVE_IMPORT_SMOKE: "from demo_plugin.core import run\n",
    "plugins/demo_plugin/src/demo_plugin/__init__.py": "",
    "plugins/demo_plugin/src/demo_plugin/core.py": "from .gone import thing\n\n\ndef run() -> object:\n    return thing\n",
    BROKEN_SMOKE: 'TABLE = "lit/data/table.csv"\ndef (:\n',
    UNREGISTERED_SMOKE: 'TABLE = "lit/data/table.csv"\n',
    FRAGMENT_REGISTERED_SMOKE: 'PAYLOAD = "frag/data/payload.bin"\n',
    "frag/data/payload.bin": "payload\n",
    "tools/delta.py": "",
    "pkg/src/pkg/gamma.py": "",
    "docs/beta.txt": "beta\n",
}

_REGISTERED = (
    LITERAL_PATH_SMOKE,
    LITERAL_BASENAME_SMOKE,
    LITERAL_DOTTED_SMOKE,
    LITERAL_SRC_DOTTED_SMOKE,
    SAME_ROOT_SCANNER_SMOKE,
    NAMED_ROOT_SCANNER_SMOKE,
    CROSS_ROOT_SCANNER_SMOKE,
    REAL_ROOT_SMOKE,
    ENTRY_POINT_SMOKE,
    SIBLING_IMPORT_SMOKE,
    RELATIVE_IMPORT_SMOKE,
    BROKEN_SMOKE,
    MISSING_SMOKE,
)


def git_environment() -> dict[str, str]:
    """The caller's environment without variables that would route git elsewhere."""
    return {key: value for key, value in os.environ.items() if key not in _GIT_ROUTING_VARIABLES}


def _write_tree(root: Path) -> None:
    files = dict(_FILES)
    files["quality_gates/gate_smokes.txt"] = "# fixture register\n" + "".join(f"{smoke}  # registered\n" for smoke in (*_REGISTERED, LITERAL_PATH_SMOKE))
    # One candidate's own fragment: the ONLY registration of
    # FRAGMENT_REGISTERED_SMOKE, and it shares no path with the register above.
    files[FRAGMENT_REGISTER_RELPATH] = f"# one candidate's own registration\n{FRAGMENT_REGISTERED_SMOKE}\n"
    for relative, text in files.items():
        target = root.joinpath(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def build_repository() -> tempfile.TemporaryDirectory[str]:
    """A git fixture tree whose python files are tracked; the caller owns cleanup."""
    fixture = tempfile.TemporaryDirectory(prefix="focused_smoke_selector_")
    root = Path(fixture.name)
    _write_tree(root)
    environment = git_environment()
    for arguments in (("init", "-q"), ("add", "-A")):
        subprocess.run(("git", *arguments), cwd=root, env=environment, check=True, capture_output=True)
    return fixture


def build_plain_tree() -> tempfile.TemporaryDirectory[str]:
    """The same tree with no git metadata, so the import rule has no file list."""
    fixture = tempfile.TemporaryDirectory(prefix="focused_smoke_selector_nogit_")
    _write_tree(Path(fixture.name))
    return fixture


class FrozenProofFixture:
    """Hermetic immutable-object boundary, shared by proof contract smokes.

    Only Git object reads are substituted. Selection, closure, identity keys,
    filesystem revalidation, store lifecycle and runner dispatch remain real.
    No subprocess or fixture Git mutation is needed.
    """

    def __init__(self, root: Path) -> None:
        from quality_gates.smoke_identity import FrozenTree

        self.root = root
        self.sources = {
            "suite/check_smoke.py": b"from suite.helper import VALUE\nassert VALUE\n",
            "suite/helper.py": b"from suite.leaf import VALUE\n",
            "suite/leaf.py": b"VALUE = 'data/input.csv'\n",
            "data/input.csv": b"one\n",
            "notes.txt": b"unrelated\n",
            "quality_gates/gate_smokes.txt": b"suite/check_smoke.py\n",
        }
        self.old_sources = {**self.sources, "data/input.csv": b"old\n"}
        self.base = FrozenTree("a" * 40, "c" * 40, self._entries(self.old_sources))
        self.candidate = FrozenTree("b" * 40, "d" * 40, self._entries(self.sources))
        self.delta = [{"path": "data/input.csv", "status": "M", "base": self.base.entries["data/input.csv"], "candidate": self.candidate.entries["data/input.csv"]}]
        self._write()

    @staticmethod
    def _entries(sources: dict[str, bytes]) -> dict[str, dict[str, str]]:
        import hashlib

        return {path: {"mode": "100644", "kind": "blob", "blob": hashlib.sha1(f"blob {len(body)}\0".encode() + body, usedforsecurity=False).hexdigest()} for path, body in sources.items()}

    def _write(self) -> None:
        for name, body in self.sources.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            path.chmod(0o644)

    def patches(self) -> ExitStack:
        from contextlib import ExitStack
        from unittest.mock import patch

        stack = ExitStack()
        stack.enter_context(patch("quality_gates.smoke_identity.read_tree", side_effect=lambda _root, ref: self.base if ref == self.base.commit else self.candidate))
        stack.enter_context(patch("quality_gates.smoke_identity.delta_entries", side_effect=lambda *_: self.delta))
        stack.enter_context(patch("quality_gates.smoke_proof.read_sources", side_effect=lambda _root, tree: self.old_sources if tree.commit == self.base.commit else self.sources))
        stack.enter_context(patch("quality_gates.smoke_proof.tool_profile", return_value={"rule_version": "fixture-v1", "inputs": {}}))
        return stack

    def request(self, *, empty: bool = False) -> dict[str, object]:
        from quality_gates.smoke_identity import digest

        delta = [] if empty else self.delta
        return {
            "schema": "platform-smoke-proof/v1",
            "repository_id": "fixture-repository",
            "base_commit": self.base.commit,
            "base_tree": self.base.tree,
            "candidate_commit": self.candidate.commit,
            "candidate_tree": self.candidate.tree,
            "manifest_sha256": self.candidate.manifest_sha256,
            "delta_entries": delta,
            "delta_sha256": digest(delta),
            "named_smokes": [],
            "attribution": {"unit_id": "fixture-unit"},
            "known_failures": {},
        }
