#!/usr/bin/env python3
"""Gate manager repair prose against the parser's registered verb surface."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_MANAGER_SOURCE = _REPO / "solet_cli" / "src" / "solet_manager"
sys.path.insert(0, str(_MANAGER_SOURCE.parent))

from solet_manager.cli import build_parser  # noqa: E402

_EXPLICIT_INVOCATION = re.compile(r"\bsolet\s+([a-z][a-z0-9-]*)\b")
_ACTION_WORD = re.compile(
    r"\b(abandon|resume|repair|inspect|reconcile|doctor|start|create|status|list)\b",
    re.IGNORECASE,
)
_ACTION_TO_VERBS: dict[str, frozenset[str]] = {
    "abandon": frozenset({"abandon"}),
    "repair": frozenset({"repair"}),
    "inspect": frozenset({"inspect"}),
    "reconcile": frozenset({"reconcile-contract", "reconcile-adapter", "reconcile-identity"}),
    "doctor": frozenset({"doctor"}),
    "start": frozenset({"start"}),
    "create": frozenset({"create"}),
    "status": frozenset({"status"}),
    "list": frozenset({"list"}),
}
_ACTION_EXEMPTIONS = {
    "resume": "Resume is a documented create re-invocation, not a standalone manager verb.",
}
# Dated bridge: landing 3 removes this after `solet abandon` is registered and
# transaction.py carries an explicit dry-run command (iss_be0ae962, 2026-09-08).
_SITE_ACTION_EXEMPTIONS = {
    ("transaction.py", 633): "abandon",
}
@dataclass(frozen=True)
class RepairSite:
    """One statically visible manager ``repair=`` carrier."""

    relative_path: str
    line: int
    expression: str

    @property
    def label(self) -> str:
        return f"{self.relative_path}:{self.line}"


def _registered_verbs() -> frozenset[str]:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return frozenset(subparsers.choices)


def _repair_sites(source_root: Path) -> list[RepairSite]:
    sites: list[RepairSite] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "repair":
                    sites.append(
                        RepairSite(
                            relative_path=str(path.relative_to(source_root)),
                            line=node.lineno,
                            expression=ast.unparse(keyword.value),
                        )
                    )
    return sites


def _check_repair_sites(
    sites: list[RepairSite],
    verbs: frozenset[str],
    site_action_exemptions: dict[tuple[str, int], str],
) -> None:
    failures: list[str] = []
    for label, action in site_action_exemptions.items():
        if _ACTION_TO_VERBS[action].intersection(verbs):
            failures.append(
                f"{label[0]}:{label[1]}: stale exemption for registered action "
                f"{action!r}; delete the exemption entry"
            )
    for site in sites:
        explicit = _EXPLICIT_INVOCATION.findall(site.expression)
        failures.extend(
            f"{site.label}: explicit invocation names unregistered verb {verb!r}"
            for verb in explicit
            if verb not in verbs
        )
        for match in _ACTION_WORD.finditer(site.expression):
            action = match.group(1).lower()
            if action in _ACTION_EXEMPTIONS or (
                site.relative_path,
                site.line,
            ) in site_action_exemptions:
                continue
            mapped_verbs = _ACTION_TO_VERBS[action]
            if not mapped_verbs.intersection(verbs):
                failures.append(
                    f"{site.label}: prose action {action!r} has no registered verb"
                )
    if failures:
        raise AssertionError("\n".join(failures))


def _assert_rejected(
    sites: list[RepairSite],
    verbs: frozenset[str],
    site_action_exemptions: dict[tuple[str, int], str],
    expected: str,
) -> None:
    try:
        _check_repair_sites(sites, verbs, site_action_exemptions)
    except AssertionError as error:
        if expected not in str(error):
            raise AssertionError(
                f"expected failure containing {expected!r}, got {error}"
            ) from error
    else:
        raise AssertionError(f"expected repair-text gate to reject {expected!r}")


def main() -> int:
    sites = _repair_sites(_MANAGER_SOURCE)
    if len(sites) < 66:
        raise AssertionError(f"repair-text audit unexpectedly shrank below 66 sites: {len(sites)}")
    verbs = _registered_verbs()
    current_defect = next(
        (
            site
            for site in sites
            if site.relative_path == "transaction.py"
            and "explicitly abandon the transaction later" in site.expression
        ),
        None,
    )
    if current_defect is None:
        raise AssertionError("present-check: expected current transaction abandon repair text")
    _assert_rejected(
        [current_defect],
        verbs,
        {},
        "prose action 'abandon' has no registered verb",
    )
    _assert_rejected(
        [RepairSite("fixture.py", 1, "'Use solet unregistered-verb --dry-run.'")],
        verbs,
        {},
        "explicit invocation names unregistered verb 'unregistered-verb'",
    )
    _check_repair_sites(sites, verbs, _SITE_ACTION_EXEMPTIONS)
    future_verbs = verbs | {"abandon"}
    _assert_rejected(
        [current_defect],
        future_verbs,
        _SITE_ACTION_EXEMPTIONS,
        "transaction.py:633: stale exemption for registered action 'abandon'; delete the exemption entry",
    )
    _check_repair_sites([current_defect], future_verbs, {})
    print(f"repair_text_verb_gate_smoke: {len(sites)} repair sites passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
