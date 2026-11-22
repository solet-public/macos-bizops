"""Recovery refs: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

from collections.abc import Sequence

import custody_store as store
from recovery_content import ContentCapture
from recovery_git import Git, ignored_paths, object_sizes, read_tree
from recovery_metadata import entry_record, write_rule
from recovery_rules import RuleWorkspace
from recovery_types import Json, RecoveryError


class RefCapture(ContentCapture):
    """RefCapture: one responsibility in the shared recovery implementation."""

    def stashes(self, name: str) -> None:
        if name == "all":
            for stash_name in self.git.text(["stash", "list", "--format=%gd"]).splitlines():
                self.stashes(stash_name)
            return
        tip = self.git.text(["rev-parse", "--verify", "-q", name], allow=(0, 1, 128))
        if not tip:
            self.skip(name, "unreadable")
            return
        parents = self.git.text(["rev-list", "--parents", "-n", "1", tip]).split()[1:]
        reference: Json = {"kind": "stash", "name": name, "tip": tip, "parents": parents, "message": self.git.text(["log", "-1", "--format=%B", tip]), "base": parents[0] if parents else ""}
        self.refs.append(reference)
        worktree = read_tree(self.git, tip)
        base = read_tree(self.git, reference["base"])
        normal = len(parents) in {2, 3} and self.git.text(["rev-list", "--parents", "-n", "1", parents[1]]).split()[1:] == parents[:1]
        layers: list[tuple[str, dict[str, tuple[int, str]], bool]] = [("ref_worktree", worktree, not normal)]
        if normal:
            layers.append(("ref_index", read_tree(self.git, parents[1]), False))
            if len(parents) == 3:
                layers.append(("ref_untracked", read_tree(self.git, parents[2]), True))
        rules = [worktree, *(tree for layer, tree, _guarded in layers if layer == "ref_untracked")]
        self.ref_layers(reference, base, layers, rules)

    def branch(self, name: str) -> None:
        tip = self.git.text(["rev-parse", "--verify", "-q", f"refs/heads/{name}"], allow=(0, 1, 128))
        if not tip:
            self.skip(name, "unreadable")
            return
        base = self.git.text(["merge-base", tip, "HEAD"], allow=(0, 1, 128))
        reference: Json = {"kind": "branch", "name": name, "tip": tip, "base": base, "parents": [base] if base else [], "message": self.git.text(["log", "-1", "--format=%B", tip])}
        self.refs.append(reference)
        tree = read_tree(self.git, tip)
        self.ref_layers(reference, read_tree(self.git, base), [("ref_branch", tree, True)], [tree])

    def ref_layers(self, reference: Json, base: dict[str, tuple[int, str]], layers: list[tuple[str, dict[str, tuple[int, str]], bool]], rules: list[dict[str, tuple[int, str]]]) -> None:
        all_entries: list[Json] = []
        omitted: set[str] = set()
        for layer, tree, guarded in layers:
            paths = sorted(set(tree) | (set(base) if layer != "ref_untracked" else set()))
            changed = [path for path in paths if layer == "ref_untracked" or tree.get(path) != base.get(path)]
            admitted = self.visible(changed, rules) if guarded else dict.fromkeys(changed, True)
            for path in changed:
                if admitted.get(path) is not True:
                    omitted.add(path)
                    self.skip(path, "unclassified" if admitted.get(path) is None else "ignored_in_ref")
                    continue
                mode, oid = tree.get(path, (None, None))
                all_entries.append(entry_record(layer, path, mode, oid, base_oid=base.get(path, (0, None))[1], reference=reference["tip"]))
        reference["omitted"] = sorted(omitted)
        self.object_entries(all_entries)

    def rule_map(self, trees: list[dict[str, tuple[int, str]]]) -> dict[str, tuple[int, str]]:
        rules: dict[str, tuple[int, str]] = {}
        for tree in trees:
            rules.update({path: item for path, item in tree.items() if path.split("/")[-1] == ".gitignore"})
        return rules

    def rule_bytes(self, rules: dict[str, tuple[int, str]]) -> dict[str, bytes]:
        sizes = object_sizes(self.git, list(dict.fromkeys(oid for _mode, oid in rules.values())))
        valid_modes = {store.REGULAR_MODE, store.EXECUTABLE_MODE}
        for mode, oid in rules.values():
            if mode not in valid_modes or oid not in sizes or sizes[oid] > 1 << 20:
                raise RecoveryError("ref_rules_incomplete")
        if sum(sizes.values()) > self.recovery.limits.cap_bytes:
            raise RecoveryError("ref_rules_byte_cap")
        answer = self.git.blobs(sizes)
        if answer.timed_out or len(answer.objects) != len(sizes):
            raise RecoveryError("ref_rules_incomplete")
        return {path: answer.objects[oid] for path, (_mode, oid) in rules.items()}

    def carried_ignore(self, paths: Sequence[str], rules: dict[str, bytes]) -> dict[str, bool | None]:
        with RuleWorkspace(self.recovery.root).rules_directory() as directory:
            for path, data in rules.items():
                write_rule(directory, path, data)
            gitdir = self.git.text(["rev-parse", "--absolute-git-dir"])
            carried_git = Git(directory, self.git.deadline)
            return ignored_paths(carried_git, paths, globals_=[f"--git-dir={gitdir}", f"--work-tree={directory}", "check-ignore", "--no-index"])

    def visible(self, paths: Sequence[str], trees: list[dict[str, tuple[int, str]]]) -> dict[str, bool | None]:
        if not paths:
            return {}
        if len(paths) > self.recovery.limits.cap_files:
            return dict.fromkeys(paths, None)
        try:
            current = ignored_paths(self.git, paths)
            carried = self.carried_ignore(paths, self.rule_bytes(self.rule_map(trees)))
            return {path: None if current.get(path) is None or carried.get(path) is None else not (current[path] or carried[path]) for path in paths}
        except (OSError, RecoveryError) as exc:
            self.advisories.append(f"ref_rules_unclassified: {getattr(exc, 'code', type(exc).__name__)}")
            return dict.fromkeys(paths, None)
