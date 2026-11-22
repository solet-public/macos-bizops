"""Tell Claude Code's prompt suggestion apart from a human's draft in a captured composer row.

Claude Code shows a prompt suggestion in an empty composer as text after the prompt glyph drawn with
SGR faint (``ESC[2m``); a human's draft is not faint. ``tmux capture-pane -e`` keeps the SGR escapes,
so a captured row says which one it is. Pure functions over captured text: no tmux, no state.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
"""One SGR escape sequence: ``ESC[``, digits and semicolons, ``m``."""


def _sgr_faint_after(code: str, faint: bool) -> bool:
    """Whether SGR faint is on after one SGR escape, given it was ``faint`` before.

    ``code`` is the escape's parameter text (``0;2`` for ``ESC[0;2m``), applied in order: empty or ``0``
    clears faint, ``2`` sets it, ``22`` clears it. ``38``, ``48`` and ``58`` start a colour whose operands
    (``5;n`` or ``2;r;g;b``) are skipped, so the colour index of ``38;5;2`` is never read as faint. Any
    other parameter leaves the state as it was."""
    params = code.split(";")
    index = 0
    while index < len(params):
        param = params[index]
        if param in ("", "0", "22"):
            faint = False
        elif param == "2":
            faint = True
        elif param in ("38", "48", "58"):
            mode = params[index + 1] if index + 1 < len(params) else ""
            index += {"5": 2, "2": 4}.get(mode, 0)
        index += 1
    return faint


def _is_faint_suggestion_row(raw_line: str, signature: str) -> bool:
    """Whether ``raw_line`` is a composer row whose text after the ``signature`` glyph is entirely faint.

    True only when the SGR-stripped row starts with the glyph, at least one non-blank character follows
    it (a space or NBSP is blank), and EVERY non-blank character after the glyph is drawn with SGR faint
    on. The row is walked from its own start: ``capture-pane -e`` restates the style at the start of each
    row and ends it with a reset (measured on tmux 3.7b), so no state carries in from the row above."""
    row = raw_line.replace("\x00", "")
    cells: list[tuple[str, bool]] = []
    faint = False
    last = 0
    for escape in _SGR_RE.finditer(row):
        cells.extend((char, faint) for char in row[last : escape.start()])
        faint = _sgr_faint_after(escape.group(0)[2:-1], faint)
        last = escape.end()
    cells.extend((char, faint) for char in row[last:])
    plain = "".join(char for char, _ in cells)
    start = len(plain) - len(plain.lstrip())
    if not plain[start:].startswith(signature):
        return False
    text_faint = [is_faint for char, is_faint in cells[start + len(signature) :] if not char.isspace()]
    return bool(text_faint) and all(text_faint)


def faint_suggestion_row(lines: Sequence[str], signature: str) -> str | None:
    """The composer row of a captured pane when it shows a prompt suggestion, else ``None``.

    The composer is the LAST row whose SGR-stripped text starts with ``signature``: it sits below the
    transcript echoes of earlier prompts, so an earlier glyph row never decides. It is a suggestion only
    when every non-blank character after the glyph is faint; a normal-colour, mixed or unknown shape is
    not, and the caller leaves that composer occupied."""
    glyph_rows = [line for line in lines if _SGR_RE.sub("", line.replace("\x00", "")).strip().startswith(signature)]
    if glyph_rows and _is_faint_suggestion_row(glyph_rows[-1], signature):
        return glyph_rows[-1]
    return None
