#!/usr/bin/env python3
"""Git-Controller gate smoke (two-layer) — the git-mutation guard's blocking rules.

Scope: mistake-prevention. Per operator directive 2026-06-13, the gate
prevents trusted Claude peer sessions from accidentally invoking git
mutations directly. Smoke coverage matches that scope — direct git
invocations, bash -c / eval / $(...) recursion, .git/ writes, Task tool
spawning. NO adversarial-obfuscation case coverage (no ANSI-C, locale,
here-string, runtime-script-source, etc.).

Layer A — in-process unit tests of ``check_bash`` / ``check_file_path`` /
``check_task`` / ``is_invocation_allowed`` / ``walk_git_invocations``.

Layer B — subprocess fixtures driving the hook entrypoint with synthetic
stdin and a temp HOME containing a session file.

Project policy: stdlib-only, no pytest. Run with::

    python3 tests/git_controller_gate_smoke.py

Exit 0 on success, 1 on first failure (the runner keeps going and reports
the full tally at the end).
"""

from __future__ import annotations

import sys

# Must precede any other import: CPython caches a module's bytecode when it
# first LOADS the module, so setting this later would still let earlier
# imports leave a .pyc inside the artifact under review.
sys.dont_write_bytecode = True

import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

_DEFAULT_HOOK_DIR = Path(__file__).resolve().parent.parent / "hooks"

# The gate exists in TWO copies: this one, shipped inside the plugin
# (`coordination-hooks/hooks/`) — the only copy that reaches a seed or a
# corporate security review, since `.claude/` is absent from
# seed_manifest.yaml's `copy:` list — and a local, unshipped twin at
# `.claude/hooks/` in the repository this plugin is developed in. Which copy
# a run exercises is chosen by GATE_SMOKE_HOOK_DIR; unset means this file's
# own sibling `hooks/`, i.e. the shipped copy. The local copy is driven by
# `.claude/hooks/tests/git_controller_gate_smoke.py`, which re-runs this file
# with GATE_SMOKE_HOOK_DIR pointed back at `.claude/hooks/`, so the 143 cases
# are single-sourced across both rather than forked. This file must resolve
# every path relative to itself (never via `.claude/` or `parents[N]`) so it
# stands alone when this plugin directory is handed to a reviewer on its own
# — enforced by this suite's own manifest_consistency_smoke.py.
_HOOK_DIR = Path(os.environ.get("GATE_SMOKE_HOOK_DIR") or _DEFAULT_HOOK_DIR).resolve()
if not (_HOOK_DIR / "git_controller_gate.py").is_file():
    raise SystemExit(f"no git_controller_gate.py under {_HOOK_DIR}")
sys.path.insert(0, str(_HOOK_DIR))

# The two copies deliberately read DIFFERENT env var names. Every other case
# here is parameterized on gate.GIT_CONTROLLER_ENV, so it would pass against
# either copy no matter which name that constant holds — blind to exactly the
# drift that matters, because the gate is fail-OPEN when its var is unset.
# case_env_contract_literal pins the literal per copy.
_EXPECTED_ENV = os.environ.get("GATE_SMOKE_EXPECTED_ENV") or "GIT_CONTROLLER_NAME"

# The operator-facing refusal names the authority to escalate to, and the two
# copies word it differently on purpose: this shipped copy says "the
# designated controller session" (it speaks to whoever installs it), the
# local copy says "a Coordinator session" (it speaks to a known fleet). That
# noun is a per-copy binding, not part of the shared behavioural contract —
# so it is parameterized rather than asserted as one shared literal, and each
# copy still has to carry SOME authority noun.
_EXPECTED_AUTHORITY_NOUN = os.environ.get("GATE_SMOKE_AUTHORITY_NOUN") or "controller"

# ruff: noqa: I001, E402
# pyright: reportMissingImports=false
import git_controller_gate as gate
from _git_controller_walker import walk_git_invocations

GC = "Git-Controller"  # the git-controller role name the fleet uses
SOME = "Architect"  # any non-Git-Controller name
# The gate is opt-in + nameable: it enforces ONLY when the neutral controller
# env var names the controller role. Set it for the enforcing Layer-A/B cases
# below; the gate-off + nameable cases save-and-restore it.
os.environ[gate.GIT_CONTROLLER_ENV] = GC


_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
    else:
        _failed.append(label)


def _expect_allow(
    func: object, args: tuple[object, ...], label: str,
) -> None:
    block, reason = func(*args)  # type: ignore[operator]
    if block:
        _failed.append(f"ALLOW expected, BLOCKED: {label} (got {reason!r})")
        return
    global _passed
    _passed += 1


def _expect_block(
    func: object, args: tuple[object, ...], label: str, contains: str = "",
) -> None:
    block, reason = func(*args)  # type: ignore[operator]
    if not block:
        _failed.append(f"BLOCK expected, ALLOWED: {label}")
        return
    if contains and contains not in reason:
        _failed.append(f"BLOCK reason missing {contains!r}: {label} (got {reason!r})")
        return
    global _passed
    _passed += 1


# ---------------------------------------------------------------------------
# Layer A — direct git invocations
# ---------------------------------------------------------------------------

def case_allowlist_no_flag_check() -> None:
    """ALLOWED_NO_FLAG_CHECK: every entry allowed for any session."""
    for sub in gate.ALLOWED_NO_FLAG_CHECK:
        _expect_allow(
            gate.check_bash,
            ({"command": f"git {sub}"}, SOME),
            f"allowlist git {sub}",
        )


def case_dual_mode_positives() -> None:
    """DUAL_MODE_ALLOWED: read-only first-arg + no-args read-only."""
    _expect_allow(gate.check_bash, ({"command": "git branch -v"}, SOME), "branch -v")
    _expect_allow(gate.check_bash, ({"command": "git branch --show-current"}, SOME), "branch --show-current")
    _expect_allow(gate.check_bash, ({"command": "git tag -l"}, SOME), "tag -l")
    _expect_allow(gate.check_bash, ({"command": "git stash list"}, SOME), "stash list")
    _expect_allow(gate.check_bash, ({"command": "git remote -v"}, SOME), "remote -v")
    _expect_allow(gate.check_bash, ({"command": "git submodule status"}, SOME), "submodule status")
    _expect_allow(gate.check_bash, ({"command": "git worktree list"}, SOME), "worktree list")
    _expect_allow(gate.check_bash, ({"command": "git bisect log"}, SOME), "bisect log")
    _expect_allow(gate.check_bash, ({"command": "git config --get user.email"}, SOME), "config --get")


def case_banned_subcommands() -> None:
    """Mutating verbs: every entry blocked for non-GC."""
    banned = (
        "commit", "push", "pull", "fetch", "reset", "merge", "rebase",
        "cherry-pick", "am", "revert", "restore", "checkout", "add", "rm",
        "mv", "clean", "init", "clone", "gc", "prune", "replace", "notes",
        "switch", "sparse-checkout", "maintenance",
    )
    for sub in banned:
        _expect_block(
            gate.check_bash,
            ({"command": f"git {sub}"}, SOME),
            f"banned git {sub}",
            contains=sub,
        )


def case_dual_mode_banned_flags() -> None:
    """DUAL_MODE_ALLOWED: mutating-flag combos blocked."""
    _expect_block(gate.check_bash, ({"command": "git branch -d foo"}, SOME), "branch -d")
    _expect_block(gate.check_bash, ({"command": "git branch -D foo"}, SOME), "branch -D")
    _expect_block(gate.check_bash, ({"command": "git tag -d v1"}, SOME), "tag -d")
    _expect_block(gate.check_bash, ({"command": "git stash"}, SOME), "bare git stash", contains="stash")
    _expect_block(gate.check_bash, ({"command": "git stash push"}, SOME), "stash push")
    _expect_block(gate.check_bash, ({"command": "git stash pop"}, SOME), "stash pop")
    _expect_block(gate.check_bash, ({"command": "git remote add o url"}, SOME), "remote add")
    _expect_block(gate.check_bash, ({"command": "git remote remove o"}, SOME), "remote remove")


def case_universal_banned_flags() -> None:
    """UNIVERSAL_BANNED_FLAGS + DANGEROUS_C_KEY_PREFIXES + DANGEROUS_GIT_GLOBALS.

    The "--no-verify" case below pairs the flag with `git commit`, which is
    independently banned as a mutating subcommand — that pairing is BLOCKED
    whether or not UNIVERSAL_BANNED_FLAGS exists, so it cannot verify the flag
    rule specifically (deleting the rule leaves this suite green). The second
    case pairs the same flag with `status`, an otherwise-allowed subcommand,
    so it discriminates: BLOCKED only because of the flag rule, ALLOWED if
    that rule is removed. Found by mutation, not by reading — see
    reference_a_test_whose_outcome_is_over_determined_cannot_verify_the_rule.
    """
    _expect_block(gate.check_bash, ({"command": "git commit --no-verify"}, SOME), "--no-verify")
    _expect_block(gate.check_bash, ({"command": "git status --no-verify"}, SOME), "--no-verify on an allowed sub")
    _expect_block(gate.check_bash, ({"command": "git -c commit.gpgsign=false commit"}, SOME), "gpgsign=false commit")
    _expect_block(gate.check_bash, ({"command": "git -c core.hooksPath=/tmp commit"}, SOME), "hooksPath override")
    _expect_block(gate.check_bash, ({"command": "git -c alias.foo='!rm -rf /' foo"}, SOME), "shell alias", contains="alias.")
    _expect_block(gate.check_bash, ({"command": "git --git-dir=/tmp/.git status"}, SOME), "--git-dir override")
    _expect_block(gate.check_bash, ({"command": "git --work-tree=/tmp status"}, SOME), "--work-tree override")


def case_identity_routing() -> None:
    """Identity routing: GC allowed, others blocked, on the same command."""
    _expect_allow(gate.check_bash, ({"command": "git stash"}, GC), "GC can stash")
    _expect_allow(gate.check_bash, ({"command": "git commit -m foo"}, GC), "GC can commit")
    _expect_allow(gate.check_bash, ({"command": "git push origin master"}, GC), "GC can push")
    _expect_block(gate.check_bash, ({"command": "git stash"}, SOME), "Architect cannot stash")
    _expect_allow(gate.check_bash, ({"command": "ls -la"}, SOME), "non-git command allowed")
    _expect_allow(gate.check_bash, ({"command": "echo hello"}, SOME), "echo allowed")


def case_gate_disabled_allows_everything() -> None:
    """Opt-in default-OFF: with the controller env unset, EVERY session may
    run mutating git AND spawn subagents — the safe single-session default
    (a fresh seed-born homunculus wires no gate at all)."""
    saved = os.environ.pop(gate.GIT_CONTROLLER_ENV, None)
    try:
        _expect_allow(gate.check_bash, ({"command": "git stash"}, SOME), "gate-off: git stash allowed")
        _expect_allow(gate.check_bash, ({"command": "git push origin master"}, SOME), "gate-off: git push allowed")
        _expect_allow(gate.check_task, ({"subagent_type": "general-purpose"}, SOME), "gate-off: subagents allowed")
    finally:
        if saved is not None:
            os.environ[gate.GIT_CONTROLLER_ENV] = saved


def case_gate_nameable() -> None:
    """Nameable: the controller role can be ANY name. With the env set to a
    custom name, that session is the sole git-mutator; others are blocked."""
    saved = os.environ.get(gate.GIT_CONTROLLER_ENV)
    os.environ[gate.GIT_CONTROLLER_ENV] = "Boss"
    try:
        _expect_allow(gate.check_bash, ({"command": "git stash"}, "Boss"), "nameable: named controller can stash")
        _expect_block(gate.check_bash, ({"command": "git stash"}, SOME), "nameable: non-controller blocked")
        _expect_allow(gate.check_task, ({"subagent_type": "general-purpose"}, "Boss"), "nameable: controller can spawn")
    finally:
        if saved is not None:
            os.environ[gate.GIT_CONTROLLER_ENV] = saved
        else:
            os.environ.pop(gate.GIT_CONTROLLER_ENV, None)


# ---------------------------------------------------------------------------
# Layer A — wrapped invocations (shell -c / eval / substitution)
# ---------------------------------------------------------------------------

def case_shell_eval_recursion() -> None:
    """bash -c / sh -c / eval recursion catches the wrapped git invocation."""
    _expect_block(gate.check_bash, ({"command": 'bash -c "git stash"'}, SOME), "bash -c git stash")
    _expect_block(gate.check_bash, ({"command": 'sh -c "git push"'}, SOME), "sh -c git push")
    _expect_block(gate.check_bash, ({"command": 'eval "git commit -m foo"'}, SOME), "eval git commit")
    _expect_block(gate.check_bash, ({"command": 'bash -lc "git stash"'}, SOME), "bash -lc")
    _expect_block(gate.check_bash, ({"command": 'sh -ec "git push"'}, SOME), "sh -ec")
    _expect_allow(gate.check_bash, ({"command": 'bash -c "git status"'}, SOME), "bash -c git status allowed")
    _expect_allow(gate.check_bash, ({"command": 'bash -c "echo hello"'}, SOME), "bash -c echo allowed")


def case_substitution_recursion() -> None:
    """$(...) and `...` substitution walked recursively."""
    _expect_block(gate.check_bash, ({"command": "echo $(git stash)"}, SOME), "$(git stash)")
    _expect_block(gate.check_bash, ({"command": "echo `git push`"}, SOME), "backtick git push")
    _expect_allow(gate.check_bash, ({"command": "echo $(git status)"}, SOME), "$(git status) allowed")
    _expect_allow(gate.check_bash, ({"command": "echo $(date)"}, SOME), "$(date) allowed")


def case_chain_separator_tokenization() -> None:
    """Chain separators (;, &&, ||, |) split tokens so each side is checked."""
    _expect_block(gate.check_bash, ({"command": "git status && git push"}, SOME), "&& git push")
    _expect_block(gate.check_bash, ({"command": "git status; git stash"}, SOME), "; git stash")
    _expect_block(gate.check_bash, ({"command": "false || git commit"}, SOME), "|| git commit")
    _expect_block(gate.check_bash, ({"command": "git status&&git stash"}, SOME), "unspaced &&")
    _expect_allow(gate.check_bash, ({"command": "git status && git log"}, SOME), "&& read-only allowed")


def case_path_qualified_git() -> None:
    """Path-qualified git via basename match."""
    _expect_block(gate.check_bash, ({"command": "/usr/bin/git stash"}, SOME), "/usr/bin/git stash")
    _expect_block(gate.check_bash, ({"command": "/usr/bin/git commit"}, SOME), "/usr/bin/git commit")
    _expect_allow(gate.check_bash, ({"command": "/usr/bin/git status"}, SOME), "/usr/bin/git status allowed")


# ---------------------------------------------------------------------------
# Layer A — .git/ direct mutation block
# ---------------------------------------------------------------------------

def case_dot_git_direct_mutation_bash() -> None:
    """fs-mutator targeting .git/ in Bash blocked for non-GC."""
    _expect_block(gate.check_bash, ({"command": "rm .git/index"}, SOME), "rm .git/index")
    _expect_block(gate.check_bash, ({"command": "rm -rf .git/"}, SOME), "rm -rf .git/")
    _expect_block(gate.check_bash, ({"command": "echo x > .git/HEAD"}, SOME), "echo > .git/HEAD")
    _expect_block(gate.check_bash, ({"command": "mv .git .git.bak"}, SOME), "mv .git")
    _expect_allow(gate.check_bash, ({"command": "ls .git/"}, SOME), "ls .git/ allowed (read-only)")
    _expect_allow(gate.check_bash, ({"command": "rm /tmp/.git"}, SOME), "rm /tmp/.git (different path) allowed")


def case_dot_git_edit_file_path() -> None:
    """Edit/Write into <repo_root>/.git/ blocked."""
    with tempfile.TemporaryDirectory() as repo:
        (Path(repo) / ".git").mkdir()
        os.environ["CLAUDE_PROJECT_DIR"] = repo
        try:
            _expect_block(
                gate.check_file_path,
                ({"file_path": str(Path(repo) / ".git" / "HEAD")}, SOME),
                "Edit .git/HEAD blocked",
            )
            _expect_block(
                gate.check_file_path,
                ({"file_path": str(Path(repo) / ".git" / "config")}, SOME),
                "Edit .git/config blocked",
            )
            _expect_allow(
                gate.check_file_path,
                ({"file_path": str(Path(repo) / "README.md")}, SOME),
                "Edit README.md allowed",
            )
            _expect_allow(
                gate.check_file_path,
                ({"file_path": str(Path(repo) / ".git" / "HEAD")}, GC),
                "GC can Edit .git/HEAD",
            )
        finally:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)


# ---------------------------------------------------------------------------
# Layer A — Task tool block
# ---------------------------------------------------------------------------

def case_task_tool_blocked() -> None:
    """Task tool blocked for non-GC unless subagent_type=Explore."""
    _expect_block(gate.check_task, ({"subagent_type": "general-purpose"}, SOME), "Task general-purpose blocked")
    _expect_block(gate.check_task, ({"subagent_type": "Plan"}, SOME), "Task Plan blocked")
    _expect_block(gate.check_task, ({"subagent_type": "claude"}, SOME), "Task claude blocked")
    _expect_block(gate.check_task, ({"subagent_type": ""}, SOME), "Task no-type blocked")
    _expect_block(gate.check_task, ({}, SOME), "Task no-input blocked")
    _expect_allow(gate.check_task, ({"subagent_type": "Explore"}, SOME), "Task Explore allowed")
    _expect_allow(gate.check_task, ({"subagent_type": "general-purpose"}, GC), "GC can spawn any")


# ---------------------------------------------------------------------------
# Layer A — walker / is_invocation_allowed basics
# ---------------------------------------------------------------------------

def case_env_contract_literal() -> None:
    """Pin the gate's env-var name for the copy under test.

    Not redundant with the parameterized cases: they read
    ``gate.GIT_CONTROLLER_ENV`` and so pass whatever it holds. A rename on
    either copy silently disables that copy's gate (unset -> gate OFF), so the
    literal is the thing worth asserting.
    """
    _check(
        gate.GIT_CONTROLLER_ENV == _EXPECTED_ENV,
        f"gate env contract is {_EXPECTED_ENV!r} "
        f"(reads {gate.GIT_CONTROLLER_ENV!r}) for {_HOOK_DIR}",
    )


def case_walker_basics() -> None:
    """walk_git_invocations returns the right shape on common inputs."""
    invs, ok = walk_git_invocations("git status")
    _check(ok and invs == [["git", "status"]], f"walk 'git status': {invs}")
    invs, ok = walk_git_invocations("ls -la && git push")
    _check(ok and ["git", "push"] in invs, f"walk chain: {invs}")
    invs, ok = walk_git_invocations('bash -c "git stash"')
    _check(ok and ["git", "stash"] in invs, f"walk bash -c: {invs}")
    invs, ok = walk_git_invocations("echo $(git commit)")
    _check(ok and ["git", "commit"] in invs, f"walk $(...): {invs}")


def case_is_invocation_allowed_basics() -> None:
    """is_invocation_allowed reads the allowlist correctly."""
    allowed, _ = gate.is_invocation_allowed(["git", "status"])
    _check(allowed, "is_invocation_allowed git status")
    allowed, _ = gate.is_invocation_allowed(["git", "log"])
    _check(allowed, "is_invocation_allowed git log")
    allowed, _ = gate.is_invocation_allowed(["git", "push"])
    _check(not allowed, "is_invocation_allowed git push (blocked)")
    allowed, _ = gate.is_invocation_allowed(["git", "commit"])
    _check(not allowed, "is_invocation_allowed git commit (blocked)")
    allowed, _ = gate.is_invocation_allowed(["git"])
    _check(not allowed, "is_invocation_allowed bare git")


def case_global_flags_before_subcommand() -> None:
    """A leading global git flag must not defeat subcommand identification.

    POSITIVE: `--no-pager` / `-c key=value` before a read-only subcommand
    still ALLOWS (argv[1]-only parsing used to misread the global flag
    itself as the subcommand and block every one of these).

    DISCRIMINATING NEGATIVE: `--no-pager` before `commit` still BLOCKS --
    but the reason must name the commit ban, not a parse failure. A block
    for the wrong reason is exactly the over-determined shape the
    --no-verify case already burned a cycle on: the mutation check below
    neuters the ban and confirms the case FLIPS to allowed, proving the
    original block genuinely depended on the ban rather than surviving it
    by accident.
    """
    allowed, reason = gate.is_invocation_allowed(["git", "--no-pager", "diff", "--stat"])
    _check(allowed, f"git --no-pager diff --stat allowed (got allowed={allowed} reason={reason!r})")
    allowed, reason = gate.is_invocation_allowed(["git", "--no-pager", "log", "-1"])
    _check(allowed, f"git --no-pager log -1 allowed (got allowed={allowed} reason={reason!r})")
    allowed, reason = gate.is_invocation_allowed(["git", "-c", "core.pager=cat", "status"])
    _check(allowed, f"git -c core.pager=cat status allowed (got allowed={allowed} reason={reason!r})")

    invocation = ["git", "--no-pager", "commit", "-m", "x"]
    allowed, reason = gate.is_invocation_allowed(invocation)
    _check(not allowed, f"git --no-pager commit blocked (got allowed={allowed})")
    _check(
        "commit" in reason and "not in the read-only allowlist" in reason,
        f"block reason names the commit subcommand ban, not a parse failure (got {reason!r})",
    )

    original_allowlist = gate.ALLOWED_NO_FLAG_CHECK
    try:
        gate.ALLOWED_NO_FLAG_CHECK = original_allowlist | frozenset({"commit"})
        mutated_allowed, _ = gate.is_invocation_allowed(invocation)
        _check(
            mutated_allowed,
            "MUTATION CHECK: neutering the commit ban must flip this case to "
            "allowed -- if it doesn't, the block above wasn't coming from the ban",
        )
    finally:
        gate.ALLOWED_NO_FLAG_CHECK = original_allowlist


# ---------------------------------------------------------------------------
# Layer B — subprocess fixtures
# ---------------------------------------------------------------------------

def _run_hook_subprocess(
    name: str | None,
    payload: dict[str, object] | None,
    *,
    malformed_stdin: bool = False,
    gate_disabled: bool = False,
) -> tuple[int, str]:
    """Drive the hook as a subprocess with synthetic stdin + session file.

    ``gate_disabled=True`` removes the controller env var from the subprocess env
    so the hook runs in its opt-in default-OFF state.
    """
    with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_repo:
        if name is not None:
            sessions_dir = Path(tmp_home) / ".claude" / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            session_file = sessions_dir / f"{os.getpid()}.json"
            session_file.write_text(json.dumps({"pid": os.getpid(), "name": name}))
        env = {
            **os.environ,
            "HOME": tmp_home,
            "CLAUDE_PROJECT_DIR": tmp_repo,
            "PYTHONPATH": str(_HOOK_DIR),
        }
        if gate_disabled:
            env.pop(gate.GIT_CONTROLLER_ENV, None)
        if malformed_stdin:
            stdin_text = "{not valid json"
        else:
            stdin_text = json.dumps(payload or {})
        proc = subprocess.run(
            [sys.executable, "-B", str(_HOOK_DIR / "git_controller_gate.py")],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=10.0,
            check=False,
        )
    return proc.returncode, proc.stderr


def case_subprocess_architect_git_stash_blocked() -> None:
    code, stderr = _run_hook_subprocess(
        name=SOME,
        payload={"tool_name": "Bash", "tool_input": {"command": "git stash"}},
    )
    _check(code == 2, f"subprocess Architect git stash: exit=2 (got {code})")
    _check(
        _EXPECTED_AUTHORITY_NOUN in stderr,
        f"stderr names the escalation authority {_EXPECTED_AUTHORITY_NOUN!r} "
        f"(got {stderr[:200]!r})",
    )
    _check("escalate" in stderr, f"stderr instructs escalation (got {stderr[:200]!r})")


def case_subprocess_gc_git_stash_allowed() -> None:
    code, _ = _run_hook_subprocess(
        name=GC,
        payload={"tool_name": "Bash", "tool_input": {"command": "git stash"}},
    )
    _check(code == 0, f"subprocess GC git stash: exit=0 (got {code})")


def case_subprocess_architect_git_status_allowed() -> None:
    code, _ = _run_hook_subprocess(
        name=SOME,
        payload={"tool_name": "Bash", "tool_input": {"command": "git status"}},
    )
    _check(code == 0, f"subprocess Architect git status: exit=0 (got {code})")


def case_subprocess_architect_edit_into_git_blocked() -> None:
    with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_repo:
        sessions_dir = Path(tmp_home) / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / ".git").mkdir()
        session_file = sessions_dir / f"{os.getpid()}.json"
        session_file.write_text(json.dumps({"pid": os.getpid(), "name": SOME}))
        env = {
            **os.environ,
            "HOME": tmp_home,
            "CLAUDE_PROJECT_DIR": tmp_repo,
            "PYTHONPATH": str(_HOOK_DIR),
        }
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(Path(tmp_repo) / ".git" / "HEAD")},
        }
        proc = subprocess.run(
            [sys.executable, "-B", str(_HOOK_DIR / "git_controller_gate.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=10.0,
            check=False,
        )
    _check(proc.returncode == 2, f"subprocess Architect Edit into .git/HEAD: exit=2 (got {proc.returncode})")


def case_subprocess_architect_task_blocked() -> None:
    code, stderr = _run_hook_subprocess(
        name=SOME,
        payload={
            "tool_name": "Task",
            "tool_input": {"subagent_type": "general-purpose", "description": "x", "prompt": "y"},
        },
    )
    _check(code == 2, f"subprocess Architect Task blocked: exit=2 (got {code})")
    _check("Task" in stderr, f"stderr mentions Task (got {stderr[:200]!r})")


def case_subprocess_architect_task_explore_allowed() -> None:
    code, _ = _run_hook_subprocess(
        name=SOME,
        payload={
            "tool_name": "Task",
            "tool_input": {"subagent_type": "Explore", "description": "x", "prompt": "y"},
        },
    )
    _check(code == 0, f"subprocess Architect Task Explore: exit=0 (got {code})")


def case_subprocess_unknown_tool_allowed() -> None:
    """Unknown tool names (MCP, etc.) are allowed by default — mistake-prevention scope."""
    code, _ = _run_hook_subprocess(
        name=SOME,
        payload={
            # an MCP tool from an unrelated server: outside the gated set, so ALLOW
            "tool_name": "mcp__example__some_tool",
            "tool_input": {"some_arg": "x"},
        },
    )
    _check(code == 0, f"subprocess unknown MCP tool allowed: exit=0 (got {code})")


def case_subprocess_read_tool_allowed() -> None:
    code, _ = _run_hook_subprocess(
        name=SOME,
        payload={"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}},
    )
    _check(code == 0, f"subprocess Architect Read: exit=0 (got {code})")


def case_subprocess_malformed_stdin_allowed() -> None:
    """Malformed JSON → exit 0 (cannot gate, allow per fail-open posture)."""
    code, _ = _run_hook_subprocess(
        name=SOME, payload=None, malformed_stdin=True,
    )
    _check(code == 0, f"subprocess malformed stdin: exit=0 (got {code})")


def case_subprocess_gate_disabled_allows_git_stash() -> None:
    """Opt-in default-OFF end-to-end: a non-controller session's git stash is
    ALLOWED (exit 0) when the controller env var is unset in the hook env."""
    code, _ = _run_hook_subprocess(
        name=SOME,
        payload={"tool_name": "Bash", "tool_input": {"command": "git stash"}},
        gate_disabled=True,
    )
    _check(code == 0, f"subprocess gate-off git stash: exit=0 (got {code})")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

LAYER_A_CASES = (
    case_allowlist_no_flag_check,
    case_dual_mode_positives,
    case_banned_subcommands,
    case_dual_mode_banned_flags,
    case_universal_banned_flags,
    case_identity_routing,
    case_gate_disabled_allows_everything,
    case_gate_nameable,
    case_shell_eval_recursion,
    case_substitution_recursion,
    case_chain_separator_tokenization,
    case_path_qualified_git,
    case_dot_git_direct_mutation_bash,
    case_dot_git_edit_file_path,
    case_task_tool_blocked,
    case_env_contract_literal,
    case_walker_basics,
    case_is_invocation_allowed_basics,
    case_global_flags_before_subcommand,
)

LAYER_B_CASES = (
    case_subprocess_architect_git_stash_blocked,
    case_subprocess_gc_git_stash_allowed,
    case_subprocess_architect_git_status_allowed,
    case_subprocess_architect_edit_into_git_blocked,
    case_subprocess_architect_task_blocked,
    case_subprocess_architect_task_explore_allowed,
    case_subprocess_unknown_tool_allowed,
    case_subprocess_read_tool_allowed,
    case_subprocess_malformed_stdin_allowed,
    case_subprocess_gate_disabled_allows_git_stash,
)


def main() -> int:
    print("Git-Controller gate smoke")
    print("=" * 60)
    print(f"Copy under test: {_HOOK_DIR}")
    print(f"Expected env contract: {_EXPECTED_ENV}")
    print(f"Layer A: {len(LAYER_A_CASES)} case-groups (mistake-prevention scope)")
    print(f"Layer B: {len(LAYER_B_CASES)} subprocess fixtures")
    print("=" * 60)
    for case in LAYER_A_CASES:
        case()
    for case in LAYER_B_CASES:
        case()
    if _failed:
        print(f"{_passed} passed, {len(_failed)} failed")
        for f in _failed[:20]:
            print(f"  FAIL  {f}")
        return 1
    print(f"{_passed} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
