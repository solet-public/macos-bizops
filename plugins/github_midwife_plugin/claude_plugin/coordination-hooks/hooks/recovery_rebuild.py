"""Recovery rebuild: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from pathlib import Path

import custody_store as store
from recovery_git import Git, index_info
from recovery_packets import Recovery
from recovery_types import Json, RecoveryError


def rebuild(recovery: Recovery, git: Git, reference: Json, entries: list[Json]) -> str:
    with ExitStack() as stack:
        store.open_store_directory(recovery.root, ("tmp",), stack, create=True)
    path = recovery.root / "tmp" / ("index-" + uuid.uuid4().hex)
    environment = {"GIT_INDEX_FILE": str(path), "GIT_AUTHOR_NAME": "Recovered work", "GIT_AUTHOR_EMAIL": "recovery@localhost", "GIT_COMMITTER_NAME": "Recovered work", "GIT_COMMITTER_EMAIL": "recovery@localhost"}
    temporary_git = Git(git.folder, git.deadline, environment)

    def tree(layer: str, base: str) -> str:
        temporary_git.read(["read-tree", base] if base else ["read-tree", "--empty"])
        selected = [entry for entry in entries if entry["layer"] == layer]
        selected.extend({"path": path, "mode": None, "oid": None, "stage": 0} for path in reference.get("omitted", []))
        if selected:
            temporary_git.read(["update-index", "-z", "--index-info"], request=index_info(selected, "sha256" if len(reference["tip"]) == 64 else "sha1"))
        return temporary_git.text(["write-tree"])

    def commit(tree_id: str, parents: list[str], message: str) -> str:
        return temporary_git.text(["commit-tree", tree_id, *(argument for parent in parents for argument in ("-p", parent)), "-m", message])

    try:
        if reference["kind"] == "branch":
            return commit(tree("ref_branch", reference["base"]), [reference["base"]] if reference["base"] else [], reference["message"])
        if len(reference["parents"]) not in {2, 3}:
            raise RecoveryError("stash_shape_unrebuildable")
        base = reference["base"]
        index = commit(tree("ref_index", base), [base], "Recovered stash index")
        parents = [base, index]
        if len(reference["parents"]) == 3:
            parents.append(commit(tree("ref_untracked", ""), [], "Recovered stash untracked paths"))
        return commit(tree("ref_worktree", base), parents, reference["message"])
    finally:
        path.unlink(missing_ok=True)
        Path(str(path) + ".lock").unlink(missing_ok=True)
