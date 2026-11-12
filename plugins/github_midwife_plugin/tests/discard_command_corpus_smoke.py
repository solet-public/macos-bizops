"""100-row design corpus; plugins/github_midwife_plugin/tests/discard_command_corpus_smoke.py.

Self-contained table, copied from the landed design section 1.3; no design-file
or developer-tool dependency at runtime. Classify is the public parser API.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from functools import partial
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins/github_midwife_plugin/coordination_hooks_common"))
from command_walk import Discard, classify  # noqa: E402  # pyright: ignore[reportMissingImports] - standalone sibling

CORPUS: tuple[tuple[int, str, str, str, dict[str, str]], ...] = (
    (1, "git status", "none", "R", {}),
    (2, "git log --oneline -5", "none", "R", {}),
    (3, "git diff HEAD", "none", "R", {}),
    (4, "git add -A", "none", "R", {}),
    (5, 'git commit -m "wip"', "none", "R", {}),
    (6, "git stash", "tree", "R", {}),
    (7, 'git stash push -m "wip"', "tree", "R", {}),
    (8, "git stash push -u", "tree+u", "R", {}),
    (9, "git stash push --keep-index --include-untracked", "tree+u", "R", {}),
    (10, "git stash -- src/a.py", "tree", "R", {}),
    (11, "git stash pop", "tree+stash(0)", "R", {}),
    (12, "git stash apply stash@{2}", "tree+stash(2)", "R", {}),
    (13, "git stash drop", "stash(0)", "R", {}),
    (14, "git stash clear", "stash(all)", "R", {}),
    (15, "git stash branch fix stash@{1}", "tree+stash(1)", "R", {}),
    (16, "git stash list", "none", "R", {}),
    (17, "git stash show -p stash@{0}", "none", "R", {}),
    (18, "git checkout -- src/a.py", "tree", "R", {}),
    (19, "git checkout .", "tree", "R", {}),
    (20, "git checkout main", "none", "R", {}),
    (21, "git checkout notes.txt", "tree", "R", {}),
    (22, "git checkout -b feature", "none", "R", {}),
    (23, "git checkout -f main", "tree", "R", {}),
    (24, "git checkout HEAD~1 -- a.py", "tree", "R", {}),
    (25, "git checkout -p", "tree", "R", {}),
    (26, "git restore a.py", "tree", "R", {}),
    (27, "git restore --staged a.py", "tree", "R", {}),
    (28, "git restore --source=HEAD~2 --worktree .", "tree", "R", {}),
    (29, "git reset --hard", "tree", "R", {}),
    (30, "git reset --hard origin/main", "tree", "R", {}),
    (31, "git reset --merge", "tree", "R", {}),
    (32, "git reset --keep HEAD~1", "tree", "R", {}),
    (33, "git reset", "tree", "R", {}),
    (34, "git reset HEAD a.py", "tree", "R", {}),
    (35, "git reset --soft HEAD~1", "none", "R", {}),
    (36, "git clean -fd", "victims", "R", {}),
    (37, "git clean -fdx", "victims", "R", {}),
    (38, "git clean -n -d", "none", "R", {}),
    (39, "git clean -i", "tree+u", "R", {}),
    (40, "git switch main", "none", "R", {}),
    (41, "git switch -f main", "tree", "R", {}),
    (42, "git switch --discard-changes main", "tree", "R", {}),
    (43, "git switch -c topic", "none", "R", {}),
    (44, "git rebase main", "tree", "R", {}),
    (45, "git rebase -i HEAD~3", "tree", "R", {}),
    (46, "git rebase --abort", "tree", "R", {}),
    (47, "git pull --rebase --autostash", "tree", "R", {}),
    (48, "git pull", "none", "R", {}),
    (49, "git branch -D old", "branch(old)", "R", {}),
    (50, "git branch --delete --force old", "branch(old)", "R", {}),
    (51, "git branch -d merged", "none", "R", {}),
    (52, "git worktree remove --force ../w2", "folder(../w2)", "../w2", {}),
    (53, "git worktree remove ../w2", "none", "../w2", {}),
    (54, "git worktree prune", "none", "R", {}),
    (55, "git merge --abort", "tree", "R", {}),
    (56, "git cherry-pick --abort", "tree", "R", {}),
    (57, "git rm -f a.py", "tree", "R", {}),
    (58, "git rm --cached a.py", "none", "R", {}),
    (59, "git -C ../other reset --hard", "tree", "../other", {}),
    (60, "git -C .. -C other stash pop", "tree+stash(0)", "../other", {}),
    (61, "git --git-dir=../o/.git --work-tree=../o checkout .", "tree", "../o", {}),
    (62, "cd ../other && git stash pop", "tree+stash(0)", "../other", {}),
    (63, "(cd ../w2 && git checkout -- .)", "tree", "../w2", {}),
    (64, "GIT_DIR=/x/.git GIT_WORK_TREE=/x git reset --hard", "tree", "/x", {}),
    (65, "env -C ../other git checkout .", "tree", "../other", {}),
    (66, "sudo -u alice git reset --hard", "tree", "R", {}),
    (67, "time git clean -fd", "victims", "R", {}),
    (68, "nice -n 10 git rebase main", "tree", "R", {}),
    (69, "timeout 30 git stash pop", "tree+stash(0)", "R", {}),
    (70, "bash -c 'git stash pop'", "tree+stash(0)", "R", {}),
    (71, 'sh -c "cd sub && git reset --hard"', "tree", "R/sub", {}),
    (72, 'eval "git checkout ."', "tree", "R", {}),
    (73, "echo $(git stash drop)", "stash(0)", "R", {}),
    (74, "{ git add -A; git reset --hard; }", "tree", "R", {}),
    (75, "git status && git reset --hard HEAD", "tree", "R", {}),
    (76, "git fetch || git reset --hard origin/main", "tree", "R", {}),
    (77, "make clean && git clean -fdx", "victims", "R", {}),
    (78, 'for f in a b; do git checkout -- "$f"; done', "tree", "R", {}),
    (79, 'cd "$TARGET" && git reset --hard', "tree", "R (cwd)", {}),
    (80, "git co -- a.py", "tree", "R", {"co": "checkout"}),
    (81, "git nuke", "tree+victims", "R", {"nuke": "!git reset --hard && git clean -fd"}),
    (82, "/usr/bin/git reset --hard", "tree", "R", {}),
    (83, "xargs git checkout -- < files.txt", "tree", "R", {}),
    (84, "bash <<'EOF'\ngit reset --hard\nEOF", "tree", "R", {}),
    (85, "cat <<'EOF' > doc.md\ngit reset --hard\nEOF", "none", "R", {}),
    (86, 'echo "git reset --hard"', "none", "R", {}),
    (87, 'grep -rn "git stash pop" docs/', "none", "R", {}),
    (88, "printf 'git clean -fd\\n' > notes.md", "none", "R", {}),
    (89, "gitk --all", "none", "R", {}),
    (90, "git lfs pull", "none", "R", {}),
    (91, "legit sync", "none", "R", {}),
    (92, "gh pr checkout 12", "none", "R", {}),
    (93, "./scripts/release.sh", "none", "R", {}),
    (94, "rm -rf build/", "none", "R", {}),
    (95, "git reset --help", "none", "R", {}),
    (96, "git help restore", "none", "R", {}),
    (97, "git push --force origin main", "none", "R", {}),
    (98, "git gc --prune=now", "none", "R", {}),
    (99, "git submodule update --force", "tree", "R", {}),
    (100, "git read-tree -u --reset HEAD", "tree", "R", {}),
)


def _query(aliases: dict[str, str], _folder: Path, arguments: Sequence[str]) -> bytes:
    if arguments[:2] == ["config", "--get-regexp"]:
        return "".join(f"alias.{name} {value}\n" for name, value in aliases.items()).encode()
    if arguments[:3] == ["rev-parse", "--verify", "-q"]:
        return b"a" * 40 + b"\n" if arguments[-1] == "main^{commit}" else b""
    return b""


def _target(cwd: Path, target: str) -> Path:
    if target == "R/sub":
        return cwd / "sub"
    return cwd if target.startswith("R") else (cwd / target).resolve()


def _check_advisory(number: int, actual: tuple[Discard, ...]) -> None:
    if number == 79:
        assert any("target_unresolved" in entry.advisories for entry in actual)


def _row(row: tuple[int, str, str, str, dict[str, str]], cwd: Path) -> float:
    number, command, expected, target, aliases = row
    started = time.monotonic()
    actual = classify(command, cwd, query=partial(_query, aliases))
    elapsed = time.monotonic() - started
    kinds = {kind for entry in actual for kind in entry.kinds}
    wanted = set() if expected == "none" else set(expected.split("+"))
    if expected.startswith("folder("):
        wanted = {"tree"}
    assert kinds == wanted, (number, command, expected, actual)
    if kinds:
        assert {entry.folder for entry in actual} == {_target(cwd, target)}, (number, actual, target)
    _check_advisory(number, actual)
    assert elapsed < 0.020, (number, elapsed)
    return elapsed


def main() -> int:
    longest = max(_row(row, Path("/recovery-corpus/R")) for row in CORPUS)
    print(f"PASS: 100 discard corpus rows; slowest {longest * 1000:.3f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
