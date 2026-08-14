#!/usr/bin/env python3
"""Four-copy consistency smoke for the git gate's policy text (A5 / D-5a.3).

Binding source: ``workbench/2026-08-01_architect_solo_session_exemption_mechanism_ruling.md``
bindings (1) and (2) — the single-active-session exemption clause lands in ALL
FOUR copies of the gate text as a byte-comparable SECTION, and the three
shipped copies stay byte-identical files.

WHY THIS FILE LIVES HERE, and not in either plugin's own ``tests/``: it is the
only leg that must reach the origin-repo-only ``.claude/hooks/`` copy. Each
shipped plugin suite is required to resolve every path relative to itself and
never via ``.claude/`` or ``parents[N]`` — that standalone property is enforced
by those suites' own ``manifest_consistency_smoke.py``, and a cross-copy
assertion placed inside one of them would break it. So the cross-copy claim
lives at plugin-repo scope, where reaching all four is legitimate.

WHAT THIS PROVES, AND WHAT IT DOES NOT. It proves CONSISTENCY: the same clause
sits in four places, and the three shipped files are byte-equal. It does NOT
prove EFFICACY — that a blocked session ever receives those words. That is a
separate POSITIVE leg (the "3a" ruling) asserted on the real block path by
``case_subprocess_architect_git_stash_blocked`` in the claude_plugin gate smoke
and ``case_peer_and_missing_role_block_mutation`` in the codex_plugin one. A
consistency-only green over a gate that never renders its message is the
absent-gate-allows-like-disarmed family; both legs are required, neither
substitutes for the other.

The mechanical-unchanged legs from binding (3) — armed gate still blocks a
non-controller mutation, unarmed gate stays silent — are already carried by the
three gate suites and are NOT duplicated here.

Project policy: stdlib-only, no pytest. Run with::

    python3 plugins/github_midwife_plugin/tests/git_policy_clause_consistency_smoke.py

Exit 0 on success, 1 on any failure (all cases run; full tally at the end).
"""

from __future__ import annotations

import sys

# Must precede any other import: CPython caches a module's bytecode when it
# first LOADS the module, so setting this later would still let earlier imports
# leave a .pyc inside an artifact under review.
sys.dont_write_bytecode = True

import ast  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402
from types import ModuleType  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# The canonical body and its two vendored copies. These three are ONE file
# maintained in three places; the WS-4b.1 design invariant is that they stay
# byte-identical, and binding (2) makes that a leg rather than a comment so a
# future drift reds a test instead of being found by the next person who greps.
_SHIPPED_POLICY_COPIES: tuple[Path, ...] = (
    _PLUGIN_ROOT / "coordination_hooks_common" / "_git_policy.py",
    _PLUGIN_ROOT / "claude_plugin" / "coordination-hooks" / "hooks" / "_git_policy.py",
    _PLUGIN_ROOT / "codex_plugin" / "coordination-hooks" / "hooks" / "_git_policy.py",
)

# The fourth copy. It is a structurally DIFFERENT standalone gate — its own
# entrypoint, its own controller-env constant, its own POLICY_MESSAGE
# definition — so whole-file identity was never required of it and is not
# asserted. Only the clause SECTION must match.
_ORIGIN_GATE_COPY = _REPO_ROOT / ".claude" / "hooks" / "git_controller_gate.py"

def clause_sites(origin_copy: Path = _ORIGIN_GATE_COPY) -> tuple[Path, ...]:
    """The clause sites to assert over — the shipped three, plus the origin copy
    IF THIS TREE HAS ONE.

    The fourth copy lives under `.claude/`, which ``root_manifest.yaml`` declares
    is never shipped, so it is absent from every born clone BY CONSTRUCTION — out
    of scope there, not missing. Requiring it unconditionally made this smoke
    unpassable on any born clone while the three copies that DO ship were
    perfectly consistent; an external adopter reported it failing on a clean
    install for exactly that reason.

    This WIDENS the site set at origin and never narrows the invariant: the three
    shipped copies are required everywhere (see
    :func:`case_the_shipped_three_are_always_required`), so a born clone still
    proves byte-identity across every copy it actually carries. A tree that lost a
    shipped copy still reds.
    """
    extra = (origin_copy,) if origin_copy.is_file() else ()
    return (*_SHIPPED_POLICY_COPIES, *extra)

# Wording the clause must NOT contain. The ruling is explicit that the clause
# names no mechanism the gate has — the gate detects nothing new — because a
# claim outrunning its mechanism, inside a control built to prevent exactly
# that, is the one failure this text cannot carry.
_FORBIDDEN_MECHANISM_CLAIMS: tuple[str, ...] = (
    "the gate detects",
    "the gate checks",
    "the gate determines",
    "automatically detect",
    "the hook detects",
    "the hook checks",
)

_passed = 0
_failed: list[str] = []


def _check(condition: bool, label: str) -> None:
    """Record one assertion. Never raises — the tally is the verdict."""
    global _passed
    if condition:
        _passed += 1
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _load(path: Path) -> ModuleType:
    """Import a gate/policy file by path, without polluting sys.modules names."""
    spec = importlib.util.spec_from_file_location(f"_a5_probe_{path.stem}_{id(path)}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case_every_copy_exists() -> None:
    for path in _SHIPPED_POLICY_COPIES:
        _check(path.is_file(), f"shipped copy present: {path.relative_to(_REPO_ROOT)}")
    sites = clause_sites()
    mode = "origin (4 sites)" if len(sites) == 4 else "bundle (3 shipped sites)"
    print(f"  clause-site mode: {mode}")


def case_the_shipped_three_are_always_required() -> None:
    """The bundle path is under test, not merely tolerated.

    Drives the resolver with an origin copy that cannot exist, and asserts it
    still yields exactly the three shipped sites. Without this leg, a resolver
    that quietly returned fewer sites would look identical to a healthy bundle
    run — which is the failure shape this whole change exists to avoid.
    """
    bundle_sites = clause_sites(_REPO_ROOT / ".claude" / "does-not-exist.py")
    _check(len(bundle_sites) == 3, f"bundle mode resolves exactly the 3 shipped copies (got {len(bundle_sites)})")
    _check(
        tuple(bundle_sites) == _SHIPPED_POLICY_COPIES,
        "bundle mode resolves the shipped copies themselves, not some other set",
    )


def case_clause_section_byte_equal_across_four_copies() -> None:
    """Binding (1)+(2): the clause SECTION is byte-equal across every copy THIS
    TREE carries — four at origin, the shipped three in a bundle."""
    sites = clause_sites()
    clauses: dict[Path, str] = {}
    for path in sites:
        if not path.is_file():
            continue
        module = _load(path)
        clause = getattr(module, "EXEMPTION_CLAUSE", None)
        _check(
            isinstance(clause, str) and bool(clause),
            f"EXEMPTION_CLAUSE defined and non-empty in {path.relative_to(_REPO_ROOT)}",
        )
        if isinstance(clause, str):
            clauses[path] = clause

    _check(
        len(clauses) == len(sites),
        f"all {len(sites)} copies present in this tree exposed a clause (got {len(clauses)})",
    )
    distinct = set(clauses.values())
    _check(
        len(distinct) <= 1,
        "clause section is byte-equal across every copy in this tree "
        f"(got {len(distinct)} distinct variants: "
        f"{sorted(hashlib.sha256(c.encode()).hexdigest()[:12] for c in distinct)})",
    )


def case_clause_is_actually_carried_by_the_shipped_message() -> None:
    """A clause defined but never concatenated in would pass a naive equality leg."""
    for path in clause_sites():
        if not path.is_file():
            continue
        module = _load(path)
        clause = getattr(module, "EXEMPTION_CLAUSE", None)
        message = getattr(module, "POLICY_MESSAGE", None)
        _check(
            isinstance(clause, str)
            and isinstance(message, str)
            and clause in message,
            f"POLICY_MESSAGE carries the clause in {path.relative_to(_REPO_ROOT)}",
        )


def case_clause_names_no_mechanism_the_gate_lacks() -> None:
    """The exemption is language-level; the gate implements no part of it."""
    module = _load(_SHIPPED_POLICY_COPIES[0])
    clause = getattr(module, "EXEMPTION_CLAUSE", "")
    lowered = clause.lower() if isinstance(clause, str) else ""
    for phrase in _FORBIDDEN_MECHANISM_CLAIMS:
        _check(
            phrase not in lowered,
            f"clause claims no mechanism the gate lacks (found {phrase!r})",
        )


def case_shipped_policy_copies_are_byte_identical() -> None:
    """Binding (2) / WS-4b.1: the three shipped copies are ONE file, verbatim."""
    digests: dict[Path, str] = {}
    for path in _SHIPPED_POLICY_COPIES:
        if not path.is_file():
            continue
        digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    _check(
        len(digests) == len(_SHIPPED_POLICY_COPIES),
        f"all {len(_SHIPPED_POLICY_COPIES)} shipped policy copies readable",
    )
    _check(
        len(set(digests.values())) <= 1,
        "the three shipped policy copies are byte-identical files "
        f"(digests: { {str(p.relative_to(_REPO_ROOT)): d[:12] for p, d in digests.items()} })",
    )


def case_env_var_divergence_is_preserved_not_unified() -> None:
    """A DELIBERATE PRESERVED DIVERGENCE, asserted so nobody 'tidies' it away.

    The shipped copies read ``GIT_CONTROLLER_NAME``; the origin-repo-only copy
    alone reads ``SOLET_GIT_CONTROLLER_NAME``. The A5 commit changes
    NOTHING here — unifying by deletion is the standing tested-copy-is-not-
    shipped-copy failure, and the gate is fail-OPEN when its var is unset, so a
    copy silently reading the wrong name arms nothing while believed armed.
    """
    # The origin half of the divergence can only be asserted where the origin copy
    # exists. In a bundle it is absent by construction, so asserting it there was
    # not a weaker check — it was an impossible one. The SHIPPED half below is
    # required unconditionally, which is what keeps the divergence pinned from
    # both ends wherever both ends are present.
    if _ORIGIN_GATE_COPY.is_file():
        origin = _load(_ORIGIN_GATE_COPY)
        _check(
            getattr(origin, "GIT_CONTROLLER_ENV", None) == "SOLET_GIT_CONTROLLER_NAME",
            "origin .claude/ copy still reads SOLET_GIT_CONTROLLER_NAME",
        )
    shipped_gate = (
        _PLUGIN_ROOT / "claude_plugin" / "coordination-hooks" / "hooks" / "git_controller_gate.py"
    )
    if shipped_gate.is_file():
        source = shipped_gate.read_text(encoding="utf-8")
        _check(
            'GIT_CONTROLLER_ENV = "GIT_CONTROLLER_NAME"' in source,
            "shipped claude_plugin copy still reads GIT_CONTROLLER_NAME",
        )


def case_policy_module_adds_no_platform_call_or_network() -> None:
    """Binding (3), structural: the policy body stays inert data.

    Same property family the manifest smoke proves for the hooks. A PreToolUse
    hook that grew a platform call, subprocess spawn, or network touch would put
    latency and a fail-direction dilemma inside the blocking path — which is
    precisely the probe design the ruling REJECTED.
    """
    banned = {"subprocess", "socket", "http", "httpx", "requests", "urllib", "asyncio"}
    tree = ast.parse(_SHIPPED_POLICY_COPIES[0].read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    offending = sorted(imported & banned)
    _check(
        not offending,
        f"policy body imports no platform/subprocess/network module (found {offending})",
    )


_CASES = (
    case_every_copy_exists,
    case_the_shipped_three_are_always_required,
    case_clause_section_byte_equal_across_four_copies,
    case_clause_is_actually_carried_by_the_shipped_message,
    case_clause_names_no_mechanism_the_gate_lacks,
    case_shipped_policy_copies_are_byte_identical,
    case_env_var_divergence_is_preserved_not_unified,
    case_policy_module_adds_no_platform_call_or_network,
)


def main() -> int:
    print("A5 / D-5a.3 policy-text consistency smoke (origin: 4 sites; bundle: the shipped 3)")
    print("=" * 60)
    for case in _CASES:
        case()
    print("=" * 60)
    if _failed:
        print(f"FAIL: {_passed} passed, {len(_failed)} failed")
        return 1
    print(f"PASS: {_passed} policy-text checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
