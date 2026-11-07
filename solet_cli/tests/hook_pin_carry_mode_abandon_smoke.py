"""Abandon refuses later chmod before restoring any hook-pin manifest."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_pin_carry_mode_smoke import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(abandon=True))
