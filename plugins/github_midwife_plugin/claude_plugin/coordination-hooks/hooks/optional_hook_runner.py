#!/usr/bin/env python3
"""Run an optional plugin hook without making its availability a prompt gate.

Lifetime contract: this runner is never deleted or renamed. Its lifetime is
that of the installed plugin version, which plugin refresh replaces as one
tree. Changes reach live sessions only after plugin refresh/next cutover.
An unconfigured invocation is inert; configured targets keep their own arming
rules. Only initial target read errors advise and skip. Hook runtime errors
and exits propagate. Security hooks must always retain their direct route.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Sequence
from pathlib import Path

_SECURITY_HOOKS = frozenset({"headless_tool_allowlist_gate.py", "lane_write_boundary.py", "remote_control_guard.py"})


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    if len(args) < 2:
        print("[optional-hook] target and hook event are required", file=sys.stderr)
        return 2
    target, event, *extra = args
    if Path(target).name in _SECURITY_HOOKS:
        print(f"[optional-hook] security hook {target} must use its direct route", file=sys.stderr)
        return 2
    try:
        with Path(target).open("rb") as source:
            code = source.read()
    except OSError as exc:
        print(f"[optional-hook] cannot read {target} ({event}): {exc}; skipping", file=sys.stderr)
        return 0
    sys.argv = [target, *extra]
    sys.path[0] = str(Path(target).absolute().parent)
    module = types.ModuleType("__main__")
    module.__file__ = target
    sys.modules["__main__"] = module
    exec(compile(code, target, "exec"), vars(module))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
