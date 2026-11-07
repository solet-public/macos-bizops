"""Exact single-line Codex draft preservation around an owned delivery doorbell.

Part 1 of the composer preserve/restore Change (``chg_3319b6c7``): a fully
visible, idle, SINGLE-LINE human draft in a Codex composer survives a delivery
doorbell byte for byte. :class:`ComposerTransaction` saves the draft into a
uniquely named private tmux buffer, verifies it, clears it, bracketed-pastes the
owned notice, lets the inherited two-Enter submission press Enter only while
the composer shows exactly that notice, and once the notice has left the
composer pastes the draft back WITHOUT Enter and verifies the exact bytes.

Lossless capture premises, measured against tmux 3.7b on 2026-10-18:
``capture-pane -N`` keeps a row's trailing spaces up to the row's used extent
(not the pane width), so a typed trailing space and screen padding are the
same cells, and ``-T`` trims both. The runtime cursor separates them: after an
``End`` key the cursor sits exactly after the last draft character, so the
draft is the cells between the prompt and the cursor, and every cell after it
must be a space. Cell columns use tab stops of 8 and East Asian wide/fullwidth
= 2, everything else 1 (tmux measured: accented Latin and the em dash are one
cell); any zero-width or control character leaves the draft unproven. The
restore is verified by the same extraction against the saved bytes, so a
width disagreement surfaces as a recovery alarm, never as silent drift. The
structural placeholder renders dim (SGR 2) while identical text typed by a
human does not, and after ``End`` an unstyled placeholder leaves the cursor at
the prompt while typed text moves it.

Busy, multiline, wrapped, popup, copy-mode, unproven and unknown composers get
zero destructive or submitting keys and a pending advisory; their preservation
is Part 2. Real Codex rendering of these premises is UNMEASURED here: the
shipped proof is a deterministic fixture driven through a real tmux server.
"""

from __future__ import annotations

import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from .codex_common import (
    _ANSI_ESCAPE_RE,
    _CODEX_COMPOSER_PROMPT,
    _CODEX_IDLE_COMPOSER_PLACEHOLDER,
    _command_succeeded,
    codex_busy_reason,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

COMPOSER_RECOVERY_NEEDED: Final[str] = "COMPOSER_RECOVERY_NEEDED"
PLACEHOLDER_TEXT: Final[str] = _CODEX_IDLE_COMPOSER_PLACEHOLDER.removeprefix(_CODEX_COMPOSER_PROMPT)
_FACTS_FORMAT: Final[str] = "#{pid}:#{session_id}:#{pane_id} #{cursor_x} #{cursor_y} #{pane_width} #{pane_in_mode}"
_SGR_RE: Final = re.compile(r"\x1b\[([0-9;:]*)m")
_COLOR_ARGUMENTS: Final[dict[str, int]] = {"5": 1, "2": 3}
_PANE_LOCKS: Final[dict[str, threading.Lock]] = {}
_PANE_LOCKS_GUARD: Final[threading.Lock] = threading.Lock()


@dataclass(frozen=True, slots=True)
class DrainReceipt:
    """One doorbell's submission evidence and what happened to a human draft.

    ``evidence`` keeps the landed vocabulary (``turn_observed``,
    ``submitted``, ``submitted_no_turn``, ``pending_*``). ``composer`` is empty
    when no draft was involved, else ``restored_exact``, ``unchanged`` (no
    destructive key reached it) or ``recovery_needed``. ``alarm`` is non-empty
    only for recovery and carries the exact saved draft; it is never built on a
    routine path.
    """

    evidence: str
    composer: str = ""
    alarm: str = ""
    phases: tuple[str, ...] = ()

    def detail(self) -> str:
        """The one delivery detail line; the draft appears only in an alarm."""
        if self.evidence == "submitted_no_turn":
            detail = f"doorbell={self.evidence}; submission unconfirmed after two Enter attempts; inbox return not established"
        else:
            detail = f"doorbell={self.evidence}; inbox return not established by pane evidence"
        if self.evidence in DEFERRED_COMPOSER_EVIDENCE:
            detail += f"; {DEFERRED_COMPOSER_ADVISORY}"
        if self.composer:
            detail += f"; composer={self.composer}"
        return f"{detail}; {self.alarm}" if self.alarm else detail


DEFERRED_COMPOSER_EVIDENCE: Final[frozenset[str]] = frozenset(
    {"pending_composer_shape", "pending_composer_unknown", "pending_composer_unproven"},
)
DEFERRED_COMPOSER_ADVISORY: Final[str] = "draft preservation pending: only a fully visible idle single-line draft is preserved today; multiline, popup, offscreen, mode and unproven composers are Part 2, so mail is retained and no key was sent"


@dataclass(frozen=True, slots=True)
class _Cell:
    char: str
    column: int
    width: int
    dim: bool


@dataclass(frozen=True, slots=True)
class PaneFacts:
    """Act-time pane identity and cursor, read in one ``display-message``."""

    pane_key: str
    cursor_x: int
    cursor_y: int
    width: int
    in_mode: bool


@dataclass(frozen=True, slots=True)
class PaneView:
    """One padded, styled capture bracketed by two identical fact reads."""

    facts: PaneFacts
    lines: tuple[str, ...]
    cells: tuple[tuple[_Cell, ...] | None, ...]

    @property
    def visible(self) -> str:
        return "\n".join(self.lines)

    def composer_row(self) -> int | None:
        for index in range(len(self.lines) - 1, -1, -1):
            if self.lines[index].lstrip(" ").startswith(_CODEX_COMPOSER_PROMPT):
                return index
        return None


def pane_lock(pane_key: str) -> threading.Lock:
    """The process-wide lock serializing doorbells on one actual pane."""
    with _PANE_LOCKS_GUARD:
        return _PANE_LOCKS.setdefault(pane_key, threading.Lock())


def _char_width(char: str, column: int) -> int | None:
    """Cells ``char`` occupies at ``column``; ``None`` where tmux may disagree."""
    if char == "\t":
        return 8 - column % 8
    if not char.isprintable() or unicodedata.category(char) in {"Mn", "Me", "Cf"}:
        return None
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _dim_after(dim: bool, params: str) -> bool:
    """Apply one SGR parameter list to the dim attribute only."""
    parts: Iterator[str] = iter(params.replace(":", ";").split(";"))
    for part in parts:
        if part in {"38", "48", "58"}:
            for _skip in range(_COLOR_ARGUMENTS.get(next(parts, ""), 0)):
                next(parts, "")
        elif part in {"", "0", "22"}:
            dim = False
        elif part == "2":
            dim = True
    return dim


def _cells(styled: str) -> tuple[_Cell, ...] | None:
    """Styled row to positioned cells, or ``None`` if any width is uncertain."""
    cells: list[_Cell] = []
    column, dim, index = 0, False, 0
    while index < len(styled):
        escape = _ANSI_ESCAPE_RE.match(styled, index)
        if escape is not None:
            sgr = _SGR_RE.fullmatch(escape.group(0))
            dim = _dim_after(dim, sgr.group(1)) if sgr is not None else dim
            index = escape.end()
            continue
        width = _char_width(styled[index], column)
        if width is None:
            return None
        cells.append(_Cell(styled[index], column, width, dim))
        column += width
        index += 1
    return tuple(cells)


def _content(cells: tuple[_Cell, ...]) -> tuple[int, tuple[_Cell, ...]] | None:
    """Composer content start column and cells after the ``› `` prompt."""
    lead = 0
    while lead < len(cells) and cells[lead].char == " ":
        lead += 1
    if "".join(cell.char for cell in cells[lead : lead + 2]) != _CODEX_COMPOSER_PROMPT:
        return None
    start = cells[lead].column + len(_CODEX_COMPOSER_PROMPT)
    return start, cells[lead + 2 :]


def _text(cells: tuple[_Cell, ...] | list[_Cell]) -> str:
    return "".join(cell.char for cell in cells)


def _rstrip(cells: tuple[_Cell, ...]) -> tuple[_Cell, ...]:
    end = len(cells)
    while end and cells[end - 1].char == " ":
        end -= 1
    return cells[:end]


def placeholder_shown(styled: str) -> bool:
    """Whether the active prompt row shows the DIM structural placeholder.

    Text-only equality cannot tell the placeholder from a human who typed the
    same words; the dim attribute can, so plain placeholder text is a draft.
    """
    for line in reversed(styled.split("\n")):
        if not _ANSI_ESCAPE_RE.sub("", line).lstrip(" ").startswith(_CODEX_COMPOSER_PROMPT):
            continue
        cells = _cells(line)
        content = None if cells is None else _content(cells)
        if content is None:
            return False
        shown = _rstrip(content[1])
        return _text(shown) == PLACEHOLDER_TEXT and all(cell.dim for cell in shown if cell.char != " ")
    return False


def _continues(line: str) -> bool:
    return line.startswith("  ") and bool(line.strip())


def _single_line_content(view: PaneView) -> tuple[int, tuple[_Cell, ...]] | None:
    """The cursor's composer row content when it is an exact single row."""
    row = view.composer_row()
    if row is None or view.facts.in_mode or view.facts.cursor_y != row:
        return None
    if row + 1 < len(view.lines) and _continues(view.lines[row + 1]):
        return None
    cells = view.cells[row]
    return None if cells is None else _content(cells)


def exact_draft(view: PaneView) -> str | None:
    """Exact single-line draft bytes, valid only with the cursor at its end."""
    content = _single_line_content(view)
    if content is None:
        return None
    start, cells = content
    cursor = view.facts.cursor_x
    head = [cell for cell in cells if cell.column < cursor]
    end = head[-1].column + head[-1].width if head else start
    if end != cursor or any(cell.char != " " for cell in cells if cell.column >= cursor):
        return None
    return _text(head)


def composer_empty(view: PaneView) -> bool:
    """A provably empty single-line composer: blank, dim placeholder, or an
    unstyled placeholder the cursor cannot move past (the caller sends End)."""
    content = _single_line_content(view)
    if content is None or view.facts.cursor_x != content[0]:
        return False
    shown = _rstrip(content[1])
    return not shown or _text(shown) == PLACEHOLDER_TEXT


def owned_notice(view: PaneView, notice: str) -> bool:
    """Exact on-screen ownership of ``notice``, cursor at its end.

    Soft-wrapped continuation rows are joined at their single break space; no
    other whitespace is normalized. Residual: a space typed exactly at a wrap
    boundary followed by moving the cursor back to the end draws identical
    cells, like the OS race between this read and the next key.
    """
    composer = _composer_segments(view)
    if composer is None:
        return False
    last, start, segments = composer
    tail = segments[-1]
    end = tail[-1].column + tail[-1].width if tail else start
    joined = " ".join(_text(segment) for segment in segments)
    return joined == notice and view.facts.cursor_y == last and view.facts.cursor_x == end


def _composer_segments(view: PaneView) -> tuple[int, int, list[tuple[_Cell, ...]]] | None:
    """Last row, content start column and right-trimmed rows of the active composer."""
    prompt = _prompt_row(view)
    if prompt is None:
        return None
    last, start, first = prompt
    segments = [_rstrip(first)]
    while last + 1 < len(view.lines) and _continues(view.lines[last + 1]):
        last += 1
        cells = view.cells[last]
        if cells is None:
            return None
        segments.append(_rstrip(tuple(cell for cell in cells if cell.column >= start)))
    return last, start, segments


def _prompt_row(view: PaneView) -> tuple[int, int, tuple[_Cell, ...]] | None:
    """Active prompt row index, content start column and content cells."""
    row = view.composer_row()
    if row is None or view.facts.in_mode:
        return None
    cells = view.cells[row]
    content = None if cells is None else _content(cells)
    return None if content is None else (row, content[0], content[1])


def _parse_facts(stdout: str) -> PaneFacts | None:
    fields = stdout.strip().split(" ")
    if len(fields) != 5 or not all(field.isdigit() for field in fields[1:]):
        return None
    return PaneFacts(fields[0], int(fields[1]), int(fields[2]), int(fields[3]), fields[4] != "0")


class TmuxComposerPort:
    """The tmux commands one composer transaction needs, on one target."""

    def __init__(self, run: Callable[[list[str]], Any], tmux_bin: str, session: str) -> None:
        self._run = run
        self._tmux = tmux_bin
        self.session = session

    def facts(self) -> PaneFacts | None:
        result = self._run([self._tmux, "display-message", "-p", "-t", self.session, _FACTS_FORMAT])
        return _parse_facts(str(getattr(result, "stdout", "") or "")) if _command_succeeded(result) else None

    def observe(self) -> PaneView | None:
        """Capture between two fact reads; a moved cursor voids the capture."""
        before = self.facts()
        captured = self._run([self._tmux, "capture-pane", "-p", "-e", "-N", "-t", self.session])
        after = self.facts()
        if before is None or before != after or not _command_succeeded(captured):
            return None
        styled = str(getattr(captured, "stdout", "") or "").removesuffix("\n").split("\n")
        lines = tuple(_ANSI_ESCAPE_RE.sub("", line) for line in styled)
        return PaneView(before, lines, tuple(_cells(line) for line in styled))

    def key(self, name: str) -> bool:
        return _command_succeeded(self._run([self._tmux, "send-keys", "-t", self.session, name]))

    def save(self, buffer: str, text: str) -> bool:
        """Store ``text`` in a private buffer and read it back byte for byte."""
        if not _command_succeeded(self._run([self._tmux, "set-buffer", "-b", buffer, "--", text])):
            return False
        shown = self._run([self._tmux, "show-buffer", "-b", buffer])
        return _command_succeeded(shown) and str(getattr(shown, "stdout", "")) == text

    def paste(self, buffer: str, *, retire: bool) -> bool:
        argv = [self._tmux, "paste-buffer", "-p", "-b", buffer, "-t", self.session]
        return _command_succeeded(self._run([*argv, "-d"] if retire else argv))

    def retire(self, buffer: str) -> bool:
        return _command_succeeded(self._run([self._tmux, "delete-buffer", "-b", buffer]))


def _refusal(view: PaneView | None) -> str | None:
    """Zero-key pending reason for anything but an idle exact single row."""
    if view is None:
        return "pending_unknown_pane"
    if codex_busy_reason(view.visible) is not None:
        return "pending_busy"
    if view.composer_row() is None or view.facts.in_mode:
        return "pending_composer_unknown"
    if _single_line_content(view) is None:
        return "pending_composer_shape"
    return None


def _proven_draft(first: PaneView, view: PaneView) -> str | DrainReceipt:
    """The post-End exact draft, if it is the text seen before End."""
    draft = exact_draft(view)
    before = _single_line_content(first)
    if draft is None or before is None:
        return DrainReceipt("pending_composer_unproven")
    if _text(_rstrip(before[1])) != draft.rstrip(" "):
        return DrainReceipt("pending_composer_changed")
    return draft


class ComposerTransaction:
    """One draft-preserving doorbell: captured -> cleared -> owned_composed ->
    submitted/turn_observed -> restored, or recovery_needed with the exact
    saved draft and its retained buffer. Callers hold :func:`pane_lock`."""

    def __init__(
        self,
        port: TmuxComposerPort,
        *,
        submit: Callable[[Callable[[], str | None]], str],
        sleep: Callable[[float], None],
        now: Callable[[], float],
        poll_seconds: float,
        timeout_seconds: float,
    ) -> None:
        self._port = port
        self._submit = submit
        self._sleep = sleep
        self._now = now
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds
        self._draft: str | None = None
        self._buffer = ""
        self._phases: list[str] = []

    def run(self, notice: str) -> DrainReceipt:
        try:
            return self._ring(notice)
        except Exception as exc:  # noqa: BLE001 -- a saved draft must surface as an alarm, never vanish in a traceback
            if self._draft is None:
                raise
            return self._alarm("pending_transaction_error", f"transaction raised {exc!r}")

    def _ring(self, notice: str) -> DrainReceipt:
        first = self._port.observe()
        if first is not None and owned_notice(first, notice):
            return self._submitted(notice, self._submit(lambda: self._before_enter(notice)))
        draft = self._capture(first)
        if isinstance(draft, DrainReceipt):
            return draft
        refused = self._save_and_clear(draft) if draft else None
        if refused is not None:
            return refused
        pending = self._compose(notice)
        if pending is not None:
            return self._reverse(notice, pending)
        return self._submitted(notice, self._submit(lambda: self._before_enter(notice)))

    def _capture(self, first: PaneView | None) -> str | DrainReceipt:
        """The exact draft, ``""`` for a provably empty composer, or a refusal."""
        pending = _refusal(first)
        if pending is not None or first is None:
            return DrainReceipt(pending or "pending_unknown_pane")
        view = self._at_end()
        if isinstance(view, DrainReceipt):
            return view
        return "" if composer_empty(view) else _proven_draft(first, view)

    def _at_end(self) -> PaneView | DrainReceipt:
        """Move the cursor to the end of the draft: the only proof of its last byte."""
        if not self._port.key("End"):
            return DrainReceipt("pending_end_failed")
        view = self._port.observe()
        pending = _refusal(view)
        if pending is not None or view is None:
            return DrainReceipt(pending or "pending_unknown_pane")
        return view

    def _save_and_clear(self, draft: str) -> DrainReceipt | None:
        self._draft = draft
        self._buffer = f"solet-composer-{uuid.uuid4().hex}"
        if not self._port.save(self._buffer, draft):
            self._port.retire(self._buffer)
            self._draft = None
            return DrainReceipt("pending_save_failed", composer="unchanged")
        self._phases.append("captured")
        view = self._port.observe()
        if view is None or exact_draft(view) != draft:
            return self._alarm("pending_composer_changed", "composer changed after capture; nothing cleared")
        if not self._port.key("C-u") or not self._await(self._empty_now):
            return self._failed_clear()
        self._phases.append("cleared")
        return None

    def _failed_clear(self) -> DrainReceipt:
        view = self._port.observe()
        if view is not None and exact_draft(view) == self._draft:
            self._port.retire(self._buffer)
            return DrainReceipt("pending_clear_failed", composer="unchanged", phases=tuple(self._phases))
        return self._alarm("pending_clear_failed", "clear left the composer neither empty nor the saved draft")

    def _compose(self, notice: str) -> str | None:
        """Bracketed-paste ``notice`` into a freshly verified empty composer."""
        if not self._empty_now():
            return "pending_composer_foreign"
        buffer = f"solet-doorbell-{uuid.uuid4().hex}"
        if not self._port.save(buffer, notice) or not self._port.paste(buffer, retire=True):
            self._port.retire(buffer)
            return "pending_paste_failed"
        if not self._await(lambda: self._owned_now(notice)):
            return "pending_paste_failed"
        self._phases.append("owned_composed")
        return None

    def _before_enter(self, notice: str) -> str | None:
        """Immediately before each inherited Enter: only exact owned bytes pass."""
        view = self._port.observe()
        if view is None:
            return "pending_unknown_pane"
        if codex_busy_reason(view.visible) is not None:
            return "turn_observed"
        if owned_notice(view, notice):
            return None
        return "submitted" if composer_empty(view) else "pending_composer_foreign"

    def _submitted(self, notice: str, evidence: str) -> DrainReceipt:
        if evidence in {"turn_observed", "submitted"}:
            self._phases.append(evidence)
            return self._restore(evidence)
        if evidence == "pending_composer_foreign" and self._draft is not None:
            return self._alarm(evidence, "composer changed after the owned notice; no Enter, no clear")
        return self._reverse(notice, evidence)

    def _reverse(self, notice: str, evidence: str) -> DrainReceipt:
        """Withdraw only an exactly owned notice, then restore any draft."""
        view = self._port.observe()
        if view is not None and owned_notice(view, notice):
            self._port.key("C-u")
        if self._draft is None:
            return DrainReceipt(evidence, phases=tuple(self._phases))
        return self._restore(evidence)

    def _restore(self, evidence: str) -> DrainReceipt:
        """Paste the saved draft WITHOUT Enter into a freshly empty composer."""
        if self._draft is None:
            return DrainReceipt(evidence, phases=tuple(self._phases))
        if not self._await(self._empty_now):
            return self._alarm(evidence, "composer not freshly empty for restore; nothing overwritten")
        if not self._port.paste(self._buffer, retire=False):
            return self._alarm(evidence, "restore paste failed")
        if not self._await(lambda: self._draft_shown()):
            return self._alarm(evidence, "restored bytes were not verified exact")
        self._port.retire(self._buffer)
        self._phases.append("restored")
        return DrainReceipt(evidence, composer="restored_exact", phases=tuple(self._phases))

    def _alarm(self, evidence: str, reason: str) -> DrainReceipt:
        phase = self._phases[-1] if self._phases else "capturing"
        self._phases.append("recovery_needed")
        alarm = f"{COMPOSER_RECOVERY_NEEDED} runtime=codex pane={self._port.session} phase={phase} reason={reason} recovery_buffer={self._buffer} saved_draft={self._draft!r}"
        return DrainReceipt(evidence, composer="recovery_needed", alarm=alarm, phases=tuple(self._phases))

    def _empty_now(self) -> bool:
        view = self._port.observe()
        if view is None or not composer_empty(view):
            return False
        content = _single_line_content(view)
        if content is None or all(cell.dim for cell in content[1] if cell.char != " "):
            return content is not None
        # Unstyled placeholder text: only the runtime cursor can prove it is
        # not a human's identical words, so ask it to move to the end.
        if not self._port.key("End"):
            return False
        moved = self._port.observe()
        return moved is not None and composer_empty(moved)

    def _owned_now(self, notice: str) -> bool:
        view = self._port.observe()
        return view is not None and owned_notice(view, notice)

    def _draft_shown(self) -> bool:
        view = self._port.observe()
        return view is not None and exact_draft(view) == self._draft

    def _await(self, condition: Callable[[], bool]) -> bool:
        deadline = self._now() + self._timeout_seconds
        while True:
            if condition():
                return True
            if self._now() > deadline:
                return False
            self._sleep(self._poll_seconds)


__all__ = [
    "COMPOSER_RECOVERY_NEEDED",
    "PLACEHOLDER_TEXT",
    "ComposerTransaction",
    "DrainReceipt",
    "PaneFacts",
    "PaneView",
    "TmuxComposerPort",
    "composer_empty",
    "exact_draft",
    "owned_notice",
    "pane_lock",
    "placeholder_shown",
]
