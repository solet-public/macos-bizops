"""Publish the coordination ownership receipt for a development checkout.

A solet's setup publishes the receipt only after a ``claude plugin install``
(``setup_plugin_operations._publish_claude_receipt``). A development checkout
never runs that step, yet every managed session spawned from it is exported
``AGENT_COORDINATION_RECEIPT_PATH`` and every coordination hook then refuses
ownership before doing any work. This module publishes the same receipt for
the checkout's own hooks, with no Claude CLI call and no plugin-cache write.

The receipt is built, never written by hand: ``build_receipt`` hashes the
hook bytes and the manifest it binds, so a receipt for bytes that are not on
disk cannot be produced. It is also a snapshot of those bytes, so it must be
published again after any change to the four coordination hooks or to the
shipped ``hooks.json``; until then the hooks refuse it as drifted.

Run: ``python3 -m github_midwife_plugin.checkout_receipt --repo-root <clone>
--solet-name <name>`` with ``plugins/github_midwife_plugin/src`` on PYTHONPATH.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .coordination_hook_installation import (
    ReceiptSurface,
    build_receipt,
    publish_receipt,
    receipt_has_plugin_cache_surface,
    receipt_path,
)

_SHIPPED_HOOKS = Path("plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks")
_CHECKOUT_HOOKS = Path(".claude/hooks")


def checkout_hook_root(repo_root: Path) -> Path:
    """The hook directory the spawn adapters designate for this checkout.

    The same two-rung choice as ``headless_adapter._resolve_worker_hook_path``:
    the checkout's own ``.claude/hooks`` when present, else the shipped copy."""
    own = repo_root / _CHECKOUT_HOOKS
    return own if own.is_dir() else repo_root / _SHIPPED_HOOKS


class InstalledReceiptError(ValueError):
    """The receipt at the target binds an installed plugin cache."""


def publish_checkout_receipt(repo_root: Path, solet_name: str, app_home: Path) -> Path:
    """Publish the receipt binding ``repo_root``'s hooks to ``app_home``.

    Refuses when the receipt already there binds an installed plugin cache:
    this publisher writes a checkout-only receipt, and overwriting a setup
    receipt with it would make every hook running from that cache refuse, to
    stderr only. A cache that is installed but has no receipt yet is NOT a
    reason to refuse -- those hooks already refuse without one, and the
    development checkout (which keeps a stale installed cache of its own) is
    exactly the caller that needs this."""
    if receipt_has_plugin_cache_surface(receipt_path(app_home)):
        raise InstalledReceiptError(
            f"{receipt_path(app_home)} binds an installed plugin cache, so this development-checkout "
            f"publisher would break that install's hooks; run `solet-manager update {solet_name}` "
            "(its claude.publish_coordination_receipt step) instead",
        )
    root = checkout_hook_root(repo_root)
    surface = ReceiptSurface(
        "checkout",
        root,
        repo_root / ".venv/bin/python3",
        repo_root / _SHIPPED_HOOKS / "hooks.json",
    )
    return publish_receipt(build_receipt(
        solet_name=solet_name,
        app_home=app_home,
        plugin_selector=f"coordination-hooks@{solet_name.replace('_', '-')}",
        default_hook_root=root,
        surfaces=(surface,),
    ))


def publish_command(repo_root: Path, solet_name: str) -> str:
    """The exact command that publishes this checkout's receipt."""
    return (
        f"PYTHONPATH={repo_root}/plugins/github_midwife_plugin/src "
        f"{repo_root}/.venv/bin/python3 -m github_midwife_plugin.checkout_receipt "
        f"--repo-root {repo_root} --solet-name {solet_name}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish the coordination ownership receipt for a development checkout.")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--solet-name", required=True)
    parser.add_argument("--app-home", type=Path, default=None, help="defaults to <repo-root>/profile")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve(strict=True)
    app_home = (args.app_home or repo_root / "profile").resolve(strict=True)
    try:
        published = publish_checkout_receipt(repo_root, args.solet_name, app_home)
    except InstalledReceiptError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"published {published}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
