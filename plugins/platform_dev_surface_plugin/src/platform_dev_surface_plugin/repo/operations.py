"""repo_service verb bodies — read-only repo primitives (design §2).

Every path argument passes through :func:`confine` (repo-root confinement +
denylist) before any read. Returned bytes pass through the secret scrub
(refuse for read_file, redact for search). ``rg``/``git`` run via
:func:`run_bounded` with server-fixed argv — no caller flag ever reaches the
shell. NOTHING here mutates the repo: ``git_status``/``git_diff`` and
``git apply --check`` are the read-only git surface the GIT-CONTROLLER policy
permits, and propose_patch is artifact-only (no apply verb exists).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from platform_dev_surface_plugin.bounded_subprocess import SubprocessResult, run_bounded
from platform_dev_surface_plugin.repo.errors import (
    RepoPathError,
    RepoSecretError,
    RepoServiceError,
    RepoToolError,
)
from platform_dev_surface_plugin.repo.patch_store import PatchStore
from platform_dev_surface_plugin.repo.path_security import assert_not_denylisted, confine
from platform_dev_surface_plugin.repo.secret_scrub import contains_secret, redact_secrets

# Bounds (no silent caps — every trim reports truncated=True + the true total).
_MAX_SEARCH_RESULTS = 200
_MAX_FILE_BYTES = 262_144
_MAX_FILE_LINES = 4_000
_MAX_LIST_ENTRIES = 2_000
_MAX_DIFF_CHARS = 200_000
_MAX_PATCH_INPUT_CHARS = 500_000
_MAX_PATCH_PATHS = 100
_GIT_TIMEOUT = 60
_RG_TIMEOUT = 60

# rg exclusion globs mirroring the path-security denylist (belt-and-suspenders;
# hits are ALSO post-filtered through assert_not_denylisted).
_RG_EXCLUDE_GLOBS: tuple[str, ...] = (
    "!.git", "!profile", "!.ananta", "!node_modules", "!.venv*", "!venv_*",
    "!*.key", "!*.pem", "!*.p12", "!*.pfx", "!.env", "!.env.*",
    "!id_rsa*", "!id_ed25519*", "!*.keychain*",
)


class RepoOperations:
    """Bind the read-only repo verbs to a concrete worktree root + patch store."""

    def __init__(self, root: Path, patch_store: PatchStore) -> None:
        self._root = root.resolve()
        self._patch_store = patch_store

    def _rel(self, resolved: Path) -> str:
        return str(resolved.relative_to(self._root))

    def _git(self, args: list[str], *, max_output_chars: int = _MAX_DIFF_CHARS) -> SubprocessResult:
        return run_bounded(
            ["git", *args], cwd=self._root, timeout=_GIT_TIMEOUT,
            max_output_chars=max_output_chars,
        )

    # --- search ---------------------------------------------------------

    def search(self, query: str, path_glob: str | None = None, max_results: int = 50) -> dict[str, Any]:
        rg = shutil.which("rg")
        if rg is None:
            raise RepoToolError("ripgrep (rg) is not installed; search requires it (no silent fallback)")
        cap = min(max(max_results, 1), _MAX_SEARCH_RESULTS)
        argv = [rg, "--line-number", "--no-heading", "--color=never", "--no-messages"]
        for glob in _RG_EXCLUDE_GLOBS:
            argv += ["--glob", glob]
        if path_glob is not None:
            argv += ["--glob", path_glob]
        argv += ["--regexp", query, "."]
        result = run_bounded(argv, cwd=self._root, timeout=_RG_TIMEOUT)
        if result.timed_out:
            raise RepoToolError(f"search timed out after {_RG_TIMEOUT}s")
        # rg exit: 0 = matches, 1 = no matches (empty hits — a valid result),
        # >=2 = ERROR (e.g. malformed regex). Fail LOUD on error, never swallow
        # it into empty hits (B-N2: rg errors are typed, never silent).
        if result.exit_code >= 2:
            raise RepoToolError(
                f"ripgrep failed (exit {result.exit_code}): {result.output[:300].strip()}"
            )
        hits = self._parse_rg_hits(result.output, cap)
        return {"query": query, "hits": hits, "truncated": len(hits) >= cap, "hit_count": len(hits)}

    def _parse_rg_hits(self, output: str, cap: int) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for line in output.splitlines():
            parsed = self._parse_rg_line(line)
            if parsed is None:
                continue
            hits.append(parsed)
            if len(hits) >= cap:
                break
        return hits

    def _parse_rg_line(self, line: str) -> dict[str, Any] | None:
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            return None
        rel_path = parts[0].removeprefix("./")
        line_no, snippet = parts[1], parts[2]
        try:
            confine(self._root, rel_path)  # denylisted/escaping hits are skipped, not errored
        except RepoServiceError:
            return None
        return {"path": rel_path, "line": int(line_no), "snippet": redact_secrets(snippet)[:400]}

    # --- read_file ------------------------------------------------------

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
        resolved = confine(self._root, path)
        if not resolved.is_file():
            raise RepoPathError(f"{path!r} is not a readable file inside the repo root")
        raw = resolved.read_text(encoding="utf-8", errors="replace")
        all_lines = raw.splitlines()
        total_lines = len(all_lines)
        selected, line_truncated = self._slice_lines(all_lines, start_line, end_line)
        content = "\n".join(selected)
        byte_truncated = len(content) > _MAX_FILE_BYTES
        if byte_truncated:
            content = content[:_MAX_FILE_BYTES]
        if contains_secret(content):
            raise RepoSecretError(
                f"{path!r} contains a credential-shaped token; read refused (Q2). "
                "Use search for a redacted view or narrow the line range."
            )
        return {
            "path": self._rel(resolved), "content": content,
            "truncated": line_truncated or byte_truncated, "total_lines": total_lines,
        }

    def _slice_lines(self, lines: list[str], start: int | None, end: int | None) -> tuple[list[str], bool]:
        if start is None and end is None:
            capped = lines[:_MAX_FILE_LINES]
            return capped, len(lines) > _MAX_FILE_LINES
        lo = max((start or 1) - 1, 0)
        hi = end if end is not None else lo + _MAX_FILE_LINES
        window = lines[lo:hi]
        capped = window[:_MAX_FILE_LINES]
        return capped, len(window) > _MAX_FILE_LINES

    # --- list_files -----------------------------------------------------

    def list_files(self, path: str | None = None, depth: int = 1, glob: str | None = None) -> dict[str, Any]:
        base = confine(self._root, path or ".")
        if not base.is_dir():
            raise RepoPathError(f"{path!r} is not a listable directory inside the repo root")
        entries: list[dict[str, str]] = []
        truncated = self._walk(base, max(depth, 1), glob, entries)
        return {"base": self._rel(base) if base != self._root else ".", "entries": entries, "truncated": truncated}

    def _walk(self, base: Path, depth: int, glob: str | None, out: list[dict[str, str]]) -> bool:
        frontier: list[tuple[Path, int]] = [(base, 0)]
        while frontier:
            current, level = frontier.pop(0)
            for child in sorted(current.iterdir()):
                if self._emit_child(child, level, depth, glob, out, frontier):
                    return True  # entry cap hit
        return False

    def _emit_child(
        self, child: Path, level: int, depth: int, glob: str | None,
        out: list[dict[str, str]], frontier: list[tuple[Path, int]],
    ) -> bool:
        """Record one entry (unless denylisted/glob-filtered) + enqueue in-depth subdirs.

        Returns True iff the entry cap was hit (signalling the walk to stop)."""
        if self._denylisted(child):
            return False
        is_dir = child.is_dir()
        if glob is not None and not is_dir and not child.match(glob):
            return False
        out.append({"path": self._rel(child), "type": "dir" if is_dir else "file"})
        if len(out) >= _MAX_LIST_ENTRIES:
            return True
        if is_dir and level + 1 < depth:
            frontier.append((child, level + 1))
        return False

    def _denylisted(self, child: Path) -> bool:
        try:
            resolved = child.resolve()
            if not resolved.is_relative_to(self._root):
                return True
            assert_not_denylisted(self._root, resolved)
            return False
        except (RepoServiceError, OSError, ValueError):
            return True

    # --- git_status / git_diff (read-only carve-out) --------------------

    def git_status(self) -> dict[str, Any]:
        result = self._git(["status", "--porcelain=v2", "--branch"])
        if result.timed_out or result.exit_code != 0:
            raise RepoToolError(f"git status failed (exit {result.exit_code}): {result.output[:400]}")
        return _parse_porcelain_v2(result.output)

    def git_diff(self, ref: str | None = None, path: str | None = None, staged: bool = False) -> dict[str, Any]:
        opts: list[str] = []
        if staged:
            opts.append("--cached")
        if ref is not None:
            # F3: a ref is a git revision, which NEVER starts with '-'. Option
            # injection always does (e.g. --output=<path> makes git WRITE that
            # file). Reject it before it reaches the shell — the `--` guards
            # only the pathspec position, not the option position.
            if ref.startswith("-"):
                raise RepoToolError(
                    f"ref {ref!r} starts with '-' (option injection); git revisions never do"
                )
            opts.append(ref)
        pathspec: list[str] = []
        if path is not None:
            confine(self._root, path)  # reject escapes before handing to git
            pathspec = ["--", path]
        diff = self._git(["diff", *opts, *pathspec])
        stat = self._git(["diff", *opts, "--stat", *pathspec])  # --stat BEFORE the `--` pathspec
        return {
            "diff": diff.output, "truncated": diff.truncated,
            "diff_chars_total": diff.output_chars_total, "stat": stat.output,
        }

    # --- propose_patch (artifact-only; NO apply verb) -------------------

    def propose_patch(self, unified_diff: str, *, principal: str) -> dict[str, Any]:
        if len(unified_diff) > _MAX_PATCH_INPUT_CHARS:
            raise RepoPathError(f"unified_diff exceeds {_MAX_PATCH_INPUT_CHARS}-char cap")
        paths = self._extract_and_confine_paths(unified_diff)
        applies_cleanly = self._git_apply_check(unified_diff)
        patch_id = self._patch_store.store(
            unified_diff=unified_diff, paths=paths, applies_cleanly=applies_cleanly, principal=principal,
        )
        return {"patch_id": patch_id, "applies_cleanly": applies_cleanly, "path_count": len(paths)}

    def _extract_and_confine_paths(self, unified_diff: str) -> list[str]:
        found: list[str] = []
        for line in unified_diff.splitlines():
            for rel in _diff_line_paths(line):
                if rel in found:
                    continue
                confine(self._root, rel)  # RepoPathError/RepoDenylistError on escape/denylist
                found.append(rel)
                if len(found) > _MAX_PATCH_PATHS:
                    raise RepoPathError(f"diff touches more than {_MAX_PATCH_PATHS} paths")
        if not found:
            raise RepoPathError("no valid repo-root-confined target paths found in the diff")
        return found

    def _git_apply_check(self, unified_diff: str) -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=True) as handle:
            handle.write(unified_diff)
            handle.flush()
            result = self._git(["apply", "--check", handle.name])
        return not result.timed_out and result.exit_code == 0


_DIFF_PATH_PREFIXES: tuple[str, ...] = (
    "--- a/", "+++ b/", "rename from ", "rename to ", "copy from ", "copy to ",
)


def _diff_line_paths(line: str) -> list[str]:
    """Every confinable path a diff header line references.

    The design mandate is "confine EVERY path in the diff" — so beyond --- a/ /
    +++ b/, this also covers rename from/to, copy from/to, and the
    ``diff --git a/X b/Y`` header (N2). The plugin's own confinement must be
    COMPLETE, not lean on ``git apply --check`` as the only guard."""
    for prefix in _DIFF_PATH_PREFIXES:
        if line.startswith(prefix):
            candidate = line[len(prefix):].strip()
            return [candidate] if candidate and candidate != "/dev/null" else []
    if line.startswith("diff --git "):
        return _git_header_paths(line[len("diff --git "):])
    return []


def _git_header_paths(rest: str) -> list[str]:
    """Extract [a-path, b-path] from a ``diff --git a/X b/Y`` remainder (best-effort)."""
    if " b/" not in rest:
        return []
    a_part, b_part = rest.split(" b/", 1)
    candidates = [a_part.removeprefix("a/").strip(), b_part.strip()]
    return [p for p in candidates if p and p != "/dev/null"]


def _parse_porcelain_v2(output: str) -> dict[str, Any]:
    """Parse ``git status --porcelain=v2 --branch`` into staged/unstaged/untracked."""
    branch = ""
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for line in output.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
        elif line.startswith(("1 ", "2 ")):
            _categorize_changed(line, staged, unstaged)
        elif line.startswith("u "):
            unstaged.append(line.split(" ")[-1])
        elif line.startswith("? "):
            untracked.append(line[2:])
    return {"branch": branch, "staged": staged, "unstaged": unstaged, "untracked": untracked}


def _categorize_changed(line: str, staged: list[str], unstaged: list[str]) -> None:
    """A porcelain-v2 '1'/'2' entry: XY status field + trailing path."""
    fields = line.split(" ")
    if len(fields) < 2:
        return
    xy = fields[1]
    path = line.split("\t")[-1].split(" ")[-1]
    if len(xy) == 2 and xy[0] != ".":
        staged.append(path)
    if len(xy) == 2 and xy[1] != ".":
        unstaged.append(path)
