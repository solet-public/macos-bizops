"""kb_git smoke — the KB-management branch is 'managed' (operator-identity-free,
2026-07-11), created-if-missing, and self-migrates from a legacy 'example' branch
without deleting it.

`git_setup_branch` / `fetch_and_merge_git` are pure subprocess git wrappers, so
this drives them against REAL temporary git repos (the no-mock real-substrate
bar) rather than mocking subprocess. Offline: everything is local tmp repos, no
network.

Cases:
  1. Fresh clone (HEAD on default, no 'example', no 'managed') -> 'managed' created
     from the default tip.
  2. Self-migration (legacy repo on 'example' ahead of default, no 'managed') ->
     'managed' forks from the 'example' TIP (history preserved), and the old 'example'
     branch still EXISTS (never-delete-branches invariant).
  3. Idempotent ('managed' already exists) -> it is checked out, no new branch
     forked.
  4. `fetch_and_merge_git` on 'managed' still merges origin/<default> cleanly
     (the rename did not break the update path).

Run directly: ``.venv/bin/python3
plugins/default_knowledge_plugin/tests/kb_git_smoke.py``.
"""

from __future__ import annotations

import base64
import functools
import http.server

# ruff: noqa: E402
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from default_knowledge_plugin import kb_lifecycle
from default_knowledge_plugin.kb_git import (
    _GIT_BRANCH,
    fetch_and_merge_git,
    git_clone,
    git_setup_branch,
)
from default_knowledge_plugin.kb_git_credential import GitTransportError
from default_knowledge_plugin.models import Manifest

_CHECKS_RUN: list[str] = []


def authenticated_transport(root: Path) -> None:
    """Real Git HTTP authentication against an owned loopback-only repository."""
    fake = "fake-only-http-git-password"
    source = root / "http-source"
    _init_repo(source)
    _commit(source, "auth.md", "# Authenticated local fixture\n")
    _git(source, "update-server-info")
    target = root / "http-clone"
    expected = "Basic " + base64.b64encode(f"x-access-token:{fake}".encode()).decode()
    observations: list[bool] = []
    authenticated: list[str] = []
    reject = threading.Event()
    cancelling, entered, release = threading.Event(), threading.Event(), threading.Event()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            configuration = target / ".git/config"
            observations.append(not configuration.exists() or fake not in configuration.read_text())
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="owned"')
                self.end_headers()
                return
            authenticated.append(self.path)
            if cancelling.is_set():
                entered.set()
                release.wait(5)
                return
            if reject.is_set():
                self.send_error(403)
                return
            super().do_GET()

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}/http-source/.git"
    native_run = subprocess.run
    commands: list[list[str]] = []

    def observed_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        commands.append(argv)
        assert fake not in str(argv) and fake not in str(kwargs.get("env", {})), "credential entered child argv/environment"
        return native_run(argv, **kwargs)

    try:
        with patch("default_knowledge_plugin.kb_git_credential.subprocess.run", side_effect=observed_run):
            git_clone(origin, target, fake)
            assert _git(target, "remote", "get-url", "origin").stdout.strip() == origin
            git_setup_branch(target)
            before = _tip(target, "HEAD")
            _commit(source, "next.md", "# Update\n")
            _git(source, "update-server-info")
            configuration = target / ".git/config"
            configuration.write_text(configuration.read_text().replace(origin, origin.replace("http://", f"http://{fake}@")))
            record = {"source": origin, "last_indexed_commit": before}
            changed = fetch_and_merge_git(target, record, fake, Manifest(name="owned-auth"), None)
            assert changed == [target / "next.md"]
            reject.set()
            try:
                fetch_and_merge_git(target, record, fake, Manifest(name="owned-auth"), None)
            except GitTransportError as exc:
                assert str(exc) == "git fetch failed (exit 128)" and exc.__cause__ is None
            else:
                raise AssertionError("authenticated rejection must fail native fetch")
            reject.clear()
            cancelling.set()
            cancelled_fetch(target, record, fake, entered, release)
        assert authenticated and observations and all(observations)
        assert fake not in (target / ".git/config").read_text()
        assert _git(target, "remote", "get-url", "origin").stdout.strip() == origin
        assert commands and all(fake not in str(command) for command in commands)
        print("PASS native authenticated clone/fetch, public origin during requests, pipe credential and categorical failure")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def cancelled_fetch(target: Path, record: dict[str, str], fake: str, entered: threading.Event, release: threading.Event) -> None:
    """Interrupt only after a real Git child reaches the authenticated peer."""

    class InterruptedProcess(subprocess.Popen[bytes]):
        def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
            arguments: Any = self.args
            if "fetch" not in arguments:
                return super().communicate(input, timeout)
            assert entered.wait(5), "native Git must reach authenticated request before interruption"
            raise KeyboardInterrupt

    try:
        with patch("subprocess.Popen", InterruptedProcess):
            try:
                fetch_and_merge_git(target, record, fake, Manifest(name="owned-cancel"), None)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("cancellation must propagate")
    finally:
        release.set()
    assert fake not in (target / ".git/config").read_text()


def explicit_source(root: Path) -> None:
    """A real explicit-URL clone must never read a name-bound credential."""
    origin = root / "explicit-origin"
    _init_repo(origin)
    _commit(origin, "a.md", "# Explicit source\n")
    lookups: list[str] = []

    def credential(name: str) -> dict[str, Any]:
        lookups.append(name)
        return {"action_status": "completed", "data": {"entries": [{"field_type": "token", "value": "fake-only-bound-to-another-source"}]}}

    class CloneReachedError(RuntimeError):
        """Owned stop after native clone, before installer indexing/persistence."""

    with patch.object(kb_lifecycle, "git_setup_branch", side_effect=CloneReachedError):
        try:
            kb_lifecycle.install_kb("explicit-target", origin.as_uri(), root, None, None, SimpleNamespace(resolve_with_secrets=credential), budget=None)
        except CloneReachedError:
            pass
        else:
            raise AssertionError("explicit source must reach post-clone branch setup")
    assert (root / "explicit-target/.git").is_dir(), "native clone must complete before checking non-use"
    assert lookups == [], "explicit caller URL must never consume a stored credential"
    assert _git(root / "explicit-target", "remote", "get-url", "origin").stdout.strip() == origin.as_uri()
    print("PASS actual explicit-source clone never resolves a name-bound credential")


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "kb-git-smoke@example.com")
    _git(path, "config", "user.name", "kb-git-smoke")


def _commit(path: Path, filename: str, content: str) -> str:
    (path / filename).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", f"add {filename}")
    return _tip(path, "HEAD")


def _current_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _branch_exists(path: Path, name: str) -> bool:
    return bool(_git(path, "branch", "--list", name).stdout.strip())


def _tip(path: Path, ref: str) -> str:
    return _git(path, "rev-parse", ref).stdout.strip()


def _check_branch_constant_is_managed() -> None:
    """RED-FIRST (operator-identity parameterization, 2026-07-11): the internal
    KB-management branch label must be the operator-identity-free 'managed', not
    a name derived from this solet's own identity.
    """
    observed_branch: str = _GIT_BRANCH  # widen off the inferred Literal so the check reads as str==str
    _check(
        "_GIT_BRANCH is 'managed' (operator-identity-free, not derived from the solet's name)",
        observed_branch == "managed",
        f"got {observed_branch!r}",
    )


def _check_fresh_creates_managed_from_default(root: Path) -> None:
    repo = root / "fresh"
    _init_repo(repo)
    main_tip = _commit(repo, "a.md", "one")

    git_setup_branch(repo)

    _check("fresh: 'managed' branch was created", _branch_exists(repo, "managed"))
    _check("fresh: 'managed' is checked out", _current_branch(repo) == "managed", _current_branch(repo))
    _check(
        "fresh: 'managed' forks from the default (main) tip",
        _tip(repo, "managed") == main_tip,
        f"managed={_tip(repo, 'managed')} main={main_tip}",
    )


def _check_self_migration_forks_from_example_tip(root: Path) -> None:
    """The load-bearing verification (Architect-flagged): on a legacy repo whose
    HEAD is on 'example', `git checkout -b managed` MUST fork from example's tip so no
    example-only KB commit is silently dropped, and the old 'example' branch survives.
    """
    repo = root / "legacy"
    _init_repo(repo)
    main_tip = _commit(repo, "a.md", "one")
    _git(repo, "checkout", "-b", "example")
    example_tip = _commit(repo, "b.md", "two")  # example is now one commit ahead of main
    _check("precondition: HEAD is on the legacy 'example' branch", _current_branch(repo) == "example")

    git_setup_branch(repo)

    _check("self-migration: 'managed' branch was created", _branch_exists(repo, "managed"))
    _check(
        "self-migration: 'managed' tip == 'example' tip (example-only history preserved)",
        _tip(repo, "managed") == example_tip,
        f"managed={_tip(repo, 'managed')} example={example_tip}",
    )
    _check(
        "self-migration: 'managed' forked from example, NOT the default tip",
        _tip(repo, "managed") != main_tip,
        f"managed unexpectedly == main tip {main_tip}",
    )
    _check(
        "self-migration: the old 'example' branch still EXISTS (never-delete-branches invariant)",
        _branch_exists(repo, "example"),
        "the legacy 'example' branch was deleted",
    )
    _check("self-migration: 'managed' is checked out", _current_branch(repo) == "managed")


def _check_idempotent_checks_out_existing_managed(root: Path) -> None:
    repo = root / "idem"
    _init_repo(repo)
    _commit(repo, "a.md", "one")
    _git(repo, "checkout", "-b", "managed")
    managed_tip = _commit(repo, "b.md", "two")
    _git(repo, "checkout", "main")  # leave HEAD off managed, as a re-entry would

    git_setup_branch(repo)

    _check("idempotent: existing 'managed' is checked out", _current_branch(repo) == "managed")
    _check(
        "idempotent: 'managed' tip is unchanged (no new branch forked over it)",
        _tip(repo, "managed") == managed_tip,
        f"managed={_tip(repo, 'managed')} expected={managed_tip}",
    )


def _check_fetch_and_merge_on_managed_still_works(root: Path) -> None:
    """Architect-requested regression: the rename must not break the update
    path -- fetch_and_merge_git merges origin/<default> INTO the checked-out
    'managed' branch cleanly.
    """
    origin = root / "origin"
    _init_repo(origin)
    _commit(origin, "a.md", "one")

    clone = root / "clone"
    _git(root, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "kb-git-smoke@example.com")
    _git(clone, "config", "user.name", "kb-git-smoke")
    git_setup_branch(clone)  # forks 'managed' from the cloned default; now on managed
    _check("fetch-merge precondition: on 'managed' after setup", _current_branch(clone) == "managed")
    pre_merge_tip = _tip(clone, "managed")

    origin_new_tip = _commit(origin, "b.md", "two")  # advance origin's default branch

    record = {"source": str(origin), "last_indexed_commit": pre_merge_tip}
    # manifest is unused on the last_indexed_commit path (fetch_and_merge_git
    # returns git_changed_files, not collect_files_fn), but pass a real one.
    result = fetch_and_merge_git(clone, record, None, Manifest(name="kb-git-smoke"), None)

    _check(
        "fetch_and_merge returns a changed-file list (clean merge), not an error dict",
        isinstance(result, list),
        f"got {result!r}",
    )
    _check(
        "the merge brought origin's new file onto 'managed'",
        (clone / "b.md").is_file() and _tip(clone, "managed") == origin_new_tip,
        f"managed={_tip(clone, 'managed')} origin={origin_new_tip}",
    )
    _check(
        "the changed-file list names the merged file",
        any("b.md" in str(p) for p in result),
        f"got {result!r}",
    )


def main() -> int:
    try:
        _check_branch_constant_is_managed()
        with tempfile.TemporaryDirectory() as tmp:
            authenticated_transport(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            explicit_source(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_fresh_creates_managed_from_default(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_self_migration_forks_from_example_tip(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_idempotent_checks_out_existing_managed(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_fetch_and_merge_on_managed_still_works(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"kb_git_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
