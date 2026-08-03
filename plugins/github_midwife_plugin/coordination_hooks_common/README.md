# Coordination hook common policy

This directory is the canonical source for the runner-neutral Git-Controller
decision layer. Claude Code and Codex cache their plugins independently, so
each shipped hook directory carries a materialized copy of these three files:

- `_git_controller_lex.py`
- `_git_controller_walker.py`
- `_git_policy.py`

Run `python3 materialize.py --check` to verify both plugin copies or
`python3 materialize.py --write` after changing the canonical source. Runner
identity, hook payload parsing, dispatch, and block presentation remain in the
runner-specific `git_controller_gate.py` adapters and are never materialized
from here.
