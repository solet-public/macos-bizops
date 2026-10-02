#!/usr/bin/env python3
"""Repo-level shipped-smoke source contract gate (iss_59385149, wgr_42e3a251).

WHAT THIS CLOSES. ``shipped_smoke_contract._checkout_only_violations`` (V1)
checks that every checkout-only smoke declaration agrees across three
surfaces — a ``# not shipped:`` marker in ``quality_gates/gate_smokes.txt``,
a ``checkout_only`` entry in ``shipped_smoke_contract.yaml``, and a
``seed_manifest.yaml`` ``exclude_paths`` line — but it previously ran ONLY
inside ``assemble()``. ``unt_85473403`` landed a smoke with only the marker;
the full pre-landing battery (840/845) and two reviews passed it, because
nothing at landing time read the contract at all, and the gap surfaced one
publish lap later when r50's assembly failed closed (``rrun_78ae868f``).
This gate runs the same V1 predicate at REPO scope, before a commit exists,
so that exact class goes red here instead of at a later assembly.

PER-PROFILE ROWS (wgr_42e3a251). The V1 predicate above is bundle-free: it
calls ``checkout_only_consistency_violations``, which passes an EMPTY retained
set on purpose. The per-profile half — a retained shipped smoke with an
undeclared repo-local dependency absent from the seed, or a checkout-only entry
still retained in some bundle — is ``check_source_tree_contract``, which
returns rows for every capability profile without assembling. Its only other
caller is a smoke that prints the rows and exits 0, so a violation reached
the publish-time assembly with nothing at landing time failing on it. This gate now also runs it for every profile and reports each row as
``[<profile>] <row>``. A row is a finding exactly like a V1 disagreement: it
blocks unless the allowlist carries it. The allowlist is the baseline, so
"candidate rows are a subset of the parent's rows" (Step 7.5) is "candidate rows
are a subset of the tracked baseline", with no second checkout. The detector
reads ``git ls-files --cached``, so it sees a candidate once staged, which is
where Git-Controller runs it.

WRONG-TREE DEFENCE. The installed ``seed_factory_plugin`` package resolves
through this checkout's ``.venv``, which in a lane worktree is a SYMLINK to
the shared checkout's ``.venv`` — its editable ``.pth`` pointer names the
SHARED checkout's source, not the worktree's. A naive
``from seed_factory_plugin import ...`` would therefore silently measure the
wrong tree's contract logic. This script inserts
``<repo-root>/plugins/seed_factory_plugin/src`` at the FRONT of ``sys.path``
before that import, so the module actually under test in ``--repo-root`` is
the one that runs (schema_init_gate.py's own "WRONG-TREE DEFENCE" note
documents the same defect class, iss_ec0db9c7 / iss_77fe09ad).

EXIT CODES. 0 clean, 2 non-allowlisted findings, 64 usage error, 70 the gate
raised and produced no verdict. 2 rather than 1 for the same reason
``shipped_doc_gate.py`` uses 2: this gate is wired into
``code_quality_check.py``, whose ``_classify_gate_exit`` treats exit 1 as
Python's own unhandled-exception code, so a 1-is-blocking gate would read a
crash as a violation count over code that was never measured.

ALLOWLIST. A three-surface disagreement IS the r50 defect class, not a style
preference with a legitimate exception — this mirrors the schema-init gate's
standing ruling (2026-09-20, under ``rul_367d8bd6``) that a live boot-crash is
never allowlisted — so no V1 entry is expected. Per-profile rows that master
already carried when the gate began blocking are tracked debt: each is one
allowlist line with owner, reason and expiry, and fixing the row deletes the
line.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist  # noqa: E402

EXIT_OK: Final[int] = 0
EXIT_BLOCKING: Final[int] = 2
EXIT_USAGE_ERROR: Final[int] = 64
EXIT_GATE_CRASH: Final[int] = 70

_KNOWLEDGE_BASE_RELPATH: Final[str] = "plugins/seed_factory_plugin/knowledge_base"
_CONTRACT_RELPATH: Final[str] = f"{_KNOWLEDGE_BASE_RELPATH}/shipped_smoke_contract.yaml"
_MANIFEST_RELPATH: Final[str] = f"{_KNOWLEDGE_BASE_RELPATH}/seed_manifest.yaml"
_BUNDLES_RELPATH: Final[str] = f"{_KNOWLEDGE_BASE_RELPATH}/capability_bundles.yaml"
_PLUGIN_SRC_RELPATH: Final[str] = "plugins/seed_factory_plugin/src"
_DEFAULT_ALLOWLIST_RELPATH: Final[str] = "quality_gates/smoke_contract_lint_gate_allowlist.txt"


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repo-level shipped-smoke source contract gate."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(),
                        help="checkout root to measure (default: cwd)")
    parser.add_argument("--allowlist", type=Path, default=None,
                        help=f"tracked-debt register (default: <repo-root>/{_DEFAULT_ALLOWLIST_RELPATH})")
    return parser.parse_args(list(argv))


def run(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    contract_path = repo_root / _CONTRACT_RELPATH
    allowlist_path: Path = (
        args.allowlist if args.allowlist is not None else repo_root / _DEFAULT_ALLOWLIST_RELPATH
    )

    if not contract_path.is_file():
        print(f"❌ usage: shipped-smoke contract not found: {contract_path}")
        return EXIT_USAGE_ERROR
    if not allowlist_path.is_file():
        print(f"❌ usage: allowlist not found: {allowlist_path}")
        return EXIT_USAGE_ERROR

    plugin_src = repo_root / _PLUGIN_SRC_RELPATH
    if str(plugin_src) not in sys.path:
        sys.path.insert(0, str(plugin_src))

    try:
        from seed_factory_plugin.shipped_smoke_contract import (
            ShippedSmokeContractError,
            check_source_tree_contract,
            checkout_only_consistency_violations,
        )
    except ImportError as exc:
        print(
            "🛑 GATE CRASH: smoke_contract_lint_gate cannot import "
            f"seed_factory_plugin from {plugin_src}: {exc}"
        )
        return EXIT_GATE_CRASH

    try:
        violations = checkout_only_consistency_violations(contract_path)
        reports = check_source_tree_contract(
            repo_root,
            manifest_path=repo_root / _MANIFEST_RELPATH,
            bundles_path=repo_root / _BUNDLES_RELPATH,
            contract_path=contract_path,
        )
        violations.extend(profile_row_findings((r.profile, r.rows) for r in reports))
        allowlist = load_allowlist(allowlist_path)
    except (ShippedSmokeContractError, OSError) as exc:
        print(f"🛑 GATE CRASH: smoke_contract_lint_gate produced NO VERDICT — {type(exc).__name__}: {exc}")
        return EXIT_GATE_CRASH

    return _verdict(violations, allowlist)


def profile_row_findings(reports: Iterable[tuple[str, Sequence[str]]]) -> list[str]:
    """One ``[<profile>] <row>`` finding per per-profile contract row.

    The profile prefix keeps the same row distinct across profiles, so an
    allowlist entry baselines one profile's row and not another's.
    """
    return [f"[{profile}] {row}" for profile, rows in reports for row in rows]


def _verdict(violations: Sequence[str], allowlist: frozenset[str]) -> int:
    blocking = [v for v in violations if v not in allowlist]
    allowed = [v for v in violations if v in allowlist]
    for violation in allowed:
        print(f"[allowlisted] {violation}")
    if blocking:
        print(f"\n❌ BLOCKING: shipped-smoke contract violation ({len(blocking)})")
        for violation in blocking:
            print(f"    {violation}")
        return EXIT_BLOCKING
    print(
        "✅ smoke_contract_lint_gate: shipped-smoke contract holds for every profile "
        f"({len(violations)} tolerated)"
    )
    return EXIT_OK


def main() -> int:
    try:
        return run(sys.argv[1:])
    except SystemExit as exc:  # argparse
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
