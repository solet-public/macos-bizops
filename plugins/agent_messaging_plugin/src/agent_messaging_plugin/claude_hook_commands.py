"""Render self-contained optional Claude hook commands at settings generation.

Only absence while opening the initial target is a successful no-op. Hook
execution is outside that boundary: runtime failures and SystemExit propagate.
The captured command imports no repository code when Claude fires it.
"""

from __future__ import annotations

import shlex
from pathlib import Path

# Keep the embedded program short: tmux bounds the entire spawn command.
# Execute in python -c's actual __main__ module so dataclasses and other
# module lookups see the script globals, just as with python <script>.
OPTIONAL_HOOK_PROGRAM = """import os,sys
e,p=sys.argv[1:3]
try:f=open(p,"rb")
except FileNotFoundError:
 print(f"[optional-hook] missing {p} ({e})",file=sys.stderr);sys.exit(0)
with f:c=compile(f.read(),p,"exec")
sys.argv=[p,*sys.argv[3:]]
sys.path[0]=os.path.dirname(os.path.abspath(p))
__file__=p
exec(c)
"""


def optional_hook_argv(path: str | Path, event: str) -> list[str]:
    """Plugin args-style and shell command-style entries share this contract."""
    return ["python3", "-c", OPTIONAL_HOOK_PROGRAM, event, str(path)]


def optional_hook_command(path: str | Path, event: str) -> str:
    """Quote all shell arguments, including paths with whitespace or quotes."""
    return shlex.join(optional_hook_argv(path, event))
