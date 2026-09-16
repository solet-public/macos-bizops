"""Step-1-restricted ``solet-manager`` executable surface.

This intentionally does not delegate to :mod:`solet_manager.cli`: that module
already owns operational commands which are outside the contract-foundation
packet.  The restricted binary is useful for packaging verification only.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .models import MANAGER_VERSION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solet-manager", description="Solet Manager contract foundation."
    )
    parser.add_argument("--version", action="version", version=f"solet-manager {MANAGER_VERSION}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
