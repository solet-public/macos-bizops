"""Guard: env_contract.py mentions 'homunculus' only as the legacy HOMUNCULUS_ prefix.

Every line of env_contract.py that contains 'homunculus' (case-insensitive) must
also contain the exact legacy token ``HOMUNCULUS_``, so the only remaining
'homunculus' text is the compatibility prefix the module detects and refuses.
The legacy token must also occur on at least four lines, so the detection
contract is not deleted by accident. Stdlib only; no database.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_CONTRACT = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src" / "agent_messaging_plugin" / "env_contract.py"
LEGACY_TOKEN = "HOMUNCULUS_"
MIN_LEGACY_LINES = 4


def main() -> int:
    lines = ENV_CONTRACT.read_text(encoding="utf-8").splitlines()
    failing: list[int] = []
    legacy_lines = 0
    for number, line in enumerate(lines, start=1):
        if LEGACY_TOKEN in line:
            legacy_lines += 1
        if "homunculus" in line.lower() and LEGACY_TOKEN not in line:
            failing.append(number)
    failed = False
    if failing:
        failed = True
        print(f"FAIL: prose 'homunculus' without legacy token on lines {failing}")
    if legacy_lines < MIN_LEGACY_LINES:
        failed = True
        print(f"FAIL: legacy token {LEGACY_TOKEN!r} on {legacy_lines} lines, need >= {MIN_LEGACY_LINES}")
    if failed:
        return 1
    print(f"OK: every 'homunculus' line carries {LEGACY_TOKEN!r}; {legacy_lines} legacy lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
