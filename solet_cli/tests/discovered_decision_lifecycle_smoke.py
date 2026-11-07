"""Registered aggregate smoke for discovered decisions and resumable lifecycle."""

from __future__ import annotations

import sys

import discovered_decision_support as support
from discovered_decision_scenarios import run_scenarios


def main() -> int:
    run_scenarios()
    print(f"discovered_decision_lifecycle_smoke OK: {support._checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
