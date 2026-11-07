#!/usr/bin/env python3
"""Deterministic Codex-like composer for real-tmux composer tests; NOT Codex.

Run as a script inside a tmux pane, it draws a transcript, an optional busy
status line, a ``› `` composer (dim placeholder when empty, word-wrapped
continuation rows indented two cells) and a ``? for shortcuts`` footer, places
the terminal cursor in the composer, and appends every key, paste and
submission to a JSON-lines log with the exact composer text afterwards. It
requests bracketed paste, so ``paste-buffer -p`` arrives bracketed exactly as a
real paste-aware TUI receives it.

Imported, :class:`FixtureSession` owns one uniquely named
``composer-test-<uuid>`` session on a PRIVATE tmux server socket of the
same name. :meth:`FixtureSession.run` is the production channel's subprocess
seam (it only adds ``-L <own socket> -u``), and :meth:`FixtureSession.close`
kills only that exact session and removes only its own socket and log. No
fleet or operator session or buffer is ever addressed.

This proves tmux buffer, key, cursor and capture behavior around a known
renderer; it is not evidence of real Codex UI compatibility.

A host with no tmux binary cannot measure the real-tmux leg. A smoke that
uses this fixture then calls :func:`report_tmux_unmeasured`, which prints the
born-clone gate's skip witness and returns the dedicated skip exit, 77, so
the gap is disclosed and never counted as a pass.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

PLACEHOLDER = "Ask Codex to do anything"
BUSY_LINE = "• Working (1s · esc to interrupt)"
FOOTER = "? for shortcuts"
_END_KEYS = {"\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~", "\x05"}
_HOME_KEYS = {"\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~", "\x01"}
_PASTE_START = "\x1b[200~"
_MOVES: dict[str, Callable[[int, int], int]] = {
    **dict.fromkeys(_END_KEYS, lambda _cursor, length: length),
    **dict.fromkeys(_HOME_KEYS, lambda _cursor, _length: 0),
    **dict.fromkeys(("\x1b[D", "\x1bOD"), lambda cursor, _length: max(0, cursor - 1)),
    **dict.fromkeys(("\x1b[C", "\x1bOC"), lambda cursor, length: min(length, cursor + 1)),
}
_PASTE_END = "\x1b[201~"


def _width(char: str, column: int) -> int:
    if char == "\t":
        return 8 - column % 8
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _wrap(text: str, width: int) -> list[tuple[int, int]]:
    """Greedy word wrap into (start, end) spans; a break space is dropped."""
    spans: list[tuple[int, int]] = []
    start = 0
    while len(text) - start > width:
        cut = text.rfind(" ", start, start + width + 1)
        if cut <= start:
            spans.append((start, start + width))
            start += width
        else:
            spans.append((start, cut))
            start = cut + 1
    spans.append((start, len(text)))
    return spans


class FixtureApp:
    """The composer state machine plus its renderer and event log."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.text = ""
        self.cursor = 0
        self.history: list[str] = []
        self.busy_until = 0.0
        self.lost_enters = int(args.lose_enters)
        self.busy_seconds = float(args.busy_seconds)
        self.dim = args.placeholder_style == "dim"
        self.popup = bool(args.popup)
        self.claude_like = bool(args.claude_like)
        self.text_sgr = str(args.claude_text_sgr)
        self.log = Path(args.log).open("a", encoding="utf-8")  # noqa: SIM115 -- one handle for the program's lifetime
        self.seq = 0

    def record(self, kind: str, **fields: Any) -> None:
        self.seq += 1
        entry = {"seq": self.seq, "kind": kind, "text": self.text, "cursor": self.cursor, "busy": self.busy, **fields}
        self.log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.log.flush()

    @property
    def busy(self) -> bool:
        return time.monotonic() < self.busy_until

    def insert(self, chunk: str) -> None:
        self.text = self.text[: self.cursor] + chunk + self.text[self.cursor :]
        self.cursor += len(chunk)

    def key(self, sequence: str) -> None:
        if sequence in {"\r", "\n"}:
            self.enter()
            return
        move = _MOVES.get(sequence)
        if move is not None:
            self.cursor = move(self.cursor, len(self.text))
        elif sequence == "\x15":
            self.text, self.cursor = self.text[self.cursor :], 0
        elif sequence in {"\x7f", "\x08"} and self.cursor:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1
        elif sequence == "\t" or (len(sequence) == 1 and sequence.isprintable()):
            self.insert(sequence)
        self.record("key", key=sequence)

    def enter(self) -> None:
        if self.lost_enters > 0:
            self.lost_enters -= 1
            self.record("enter_lost")
            return
        submitted, self.text, self.cursor = self.text, "", 0
        self.history.append(submitted)
        self.busy_until = time.monotonic() + self.busy_seconds
        self.record("submit", submitted=submitted)

    def rows(self, width: int) -> tuple[list[str], tuple[int, int]]:
        rows = [f"› {line}"[:width] for line in self.history[-3:]] + [""]
        if self.busy:
            rows.append(BUSY_LINE)
        composer, cursor = self.composer_rows(width, len(rows))
        rows += composer
        if self.popup and self.text.startswith("/"):
            rows.append("  /status   show current session status")
        rows.append(FOOTER)
        return rows, cursor

    def composer_rows(self, width: int, top: int) -> tuple[list[str], tuple[int, int]]:
        """Prompt and continuation rows, with the cursor's (row, column)."""
        if self.claude_like:
            return self.claude_composer_rows(width, top)
        if not self.text:
            placeholder = f"\x1b[2m{PLACEHOLDER}\x1b[0m" if self.dim else PLACEHOLDER
            return [f"› {placeholder}"], (top, 2)
        rows: list[str] = []
        cursor = (top, 2)
        for index, (start, end) in enumerate(_wrap(self.text, width - 3)):
            rows.append(("› " if index == 0 else "  ") + self.text[start:end])
            if start <= self.cursor <= end:
                column = 2
                for char in self.text[start : self.cursor]:
                    column += _width(char, column)
                cursor = (top + index, column)
        return rows, cursor

    def claude_composer_rows(self, width: int, top: int) -> tuple[list[str], tuple[int, int]]:
        """The Claude Code composer shape the production channel samples.

        Measured on live Claude panes: the empty row is a dim ``❯`` plus a
        NBSP (it strips to the bare glyph, the channel's cleared signature);
        text in the composer carries the bright-white SGR 97 stranded-input
        signature unless ``--claude-text-sgr`` says otherwise. Continuation rows are indented two cells."""
        if not self.text:
            return ["\x1b[2m❯\xa0\x1b[0m"], (top, 2)
        rows: list[str] = []
        cursor = (top, 2)
        for index, (start, end) in enumerate(_wrap(self.text, width - 3)):
            rows.append(("❯\xa0" if index == 0 else "  ") + f"\x1b[{self.text_sgr}m{self.text[start:end]}\x1b[0m")
            if start <= self.cursor <= end:
                cursor = (top + index, 2 + sum(_width(char, 2) for char in self.text[start : self.cursor]))
        return rows, cursor

    def render(self) -> None:
        width = os.get_terminal_size(sys.stdout.fileno()).columns
        rows, (row, column) = self.rows(width)
        sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(rows) + f"\x1b[{row + 1};{column + 1}H")
        sys.stdout.flush()


def _sequences(data: str) -> list[str]:
    """Split decoded input into keys, CSI/SS3 sequences and bracketed pastes."""
    out: list[str] = []
    index = 0
    while index < len(data):
        if data.startswith(_PASTE_START, index):
            end = data.find(_PASTE_END, index)
            stop = len(data) if end < 0 else end
            out.append(data[index:stop])
            index = stop + len(_PASTE_END)
        elif data.startswith("\x1b[", index) or data.startswith("\x1bO", index):
            end = index + 2
            while end < len(data) and not ("@" <= data[end] <= "~"):
                end += 1
            out.append(data[index : end + 1])
            index = end + 1
        else:
            out.append(data[index])
            index += 1
    return out


def _serve(app: FixtureApp) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    was_busy = False
    app.render()
    app.record("ready")
    while True:
        timeout = max(0.0, app.busy_until - time.monotonic()) + 0.01 if app.busy else None
        readable, _w, _x = select.select([sys.stdin], [], [], timeout)
        if readable:
            for sequence in _sequences(decoder.decode(os.read(sys.stdin.fileno(), 65536))):
                if sequence.startswith(_PASTE_START):
                    app.insert(sequence[len(_PASTE_START) :])
                    app.record("paste", pasted=sequence[len(_PASTE_START) :])
                else:
                    app.key(sequence)
        if was_busy and not app.busy:
            app.record("idle")
        was_busy = app.busy
        app.render()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True)
    parser.add_argument("--busy-seconds", default="1.0")
    parser.add_argument("--lose-enters", default="0")
    parser.add_argument("--placeholder-style", choices=("dim", "plain"), default="dim")
    parser.add_argument("--popup", action="store_true")
    parser.add_argument("--claude-like", action="store_true")
    parser.add_argument("--claude-text-sgr", default="97", help="SGR for composer text in --claude-like mode; 97 is the stranded-input signature")
    app = FixtureApp(parser.parse_args())
    saved = termios.tcgetattr(sys.stdin.fileno())
    tty.setraw(sys.stdin.fileno())
    sys.stdout.write("\x1b[?2004h")
    try:
        _serve(app)
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
    return 0


SKIP_EXIT_CODE = 77
TMUX_SKIP_WITNESS = 'BORN_CLONE_SKIP_WITNESS={"reason": "ambient_executable_missing", "executable": "tmux"}'


def tmux_missing() -> bool:
    """True when no tmux binary is on PATH, so a real-tmux leg cannot run."""
    return shutil.which("tmux") is None


def report_tmux_unmeasured(detail: str) -> int:
    """Disclose an unmeasured real-tmux leg as the dedicated skip exit, never a pass."""
    print(TMUX_SKIP_WITNESS)
    print(f"SKIP: no tmux binary on PATH -- {detail}; the real-tmux leg is UNMEASURED, not passed.")
    return SKIP_EXIT_CODE


class FixtureSession:
    """One disposable fixture pane on its own private tmux server."""

    def __init__(self, *, width: int = 200, height: int = 24, options: tuple[str, ...] = ()) -> None:
        tmux = shutil.which("tmux")
        if tmux is None:
            raise RuntimeError("REAL TMUX leg UNMEASURED: no tmux binary on PATH; this is a failure, not a pass")
        self.tmux = tmux
        self.name = f"composer-test-{uuid.uuid4().hex}"
        self.workdir = Path(tempfile.mkdtemp(prefix="composer-test-"))
        self.log = self.workdir / "fixture.jsonl"
        self.socket_path = ""
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), "--log", str(self.log), *options])
        started = self.run([self.tmux, "new-session", "-d", "-s", self.name, "-x", str(width), "-y", str(height), command])
        if started.returncode != 0:
            raise RuntimeError(f"fixture session failed to start: {started.stderr}")
        self.socket_path = self.run([self.tmux, "display-message", "-p", "-t", self.name, "#{socket_path}"]).stdout.strip()
        self.wait_for(lambda events: any(event["kind"] == "ready" for event in events), "fixture ready")
        time.sleep(0.1)

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """The production channel's subprocess seam, bound to this socket only."""
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("timeout", 10)
        return subprocess.run([argv[0], "-L", self.name, "-u", *argv[1:]], check=False, **kwargs)

    def events(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def state(self) -> dict[str, Any]:
        return self.events()[-1]

    def submissions(self) -> list[str]:
        return [str(event["submitted"]) for event in self.events() if event["kind"] == "submit"]

    def wait_for(self, predicate: Any, what: str, timeout: float = 5.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.events()
            if predicate(events):
                return events
            time.sleep(0.02)
        raise AssertionError(f"fixture never reached: {what}; last={self.events()[-3:]}")

    def human(self, text: str) -> None:
        """Type ``text`` as a human at the current cursor and wait for it."""
        before = len(self.events())
        self.run([self.tmux, "send-keys", "-t", self.name, "-l", "--", text])
        self.wait_for(lambda events: sum(event["kind"] == "key" for event in events[before:]) >= len(text), f"human typed {text!r}")
        time.sleep(0.05)

    def human_key(self, key: str) -> None:
        before = len(self.events())
        self.run([self.tmux, "send-keys", "-t", self.name, key])
        self.wait_for(lambda events: len(events) > before, f"human key {key}")
        time.sleep(0.05)

    def buffers(self) -> list[str]:
        listed = self.run([self.tmux, "list-buffers", "-F", "#{buffer_name}"])
        return [name for name in listed.stdout.splitlines() if name] if listed.returncode == 0 else []

    def buffer(self, name: str) -> str:
        return self.run([self.tmux, "show-buffer", "-b", name]).stdout

    def close(self) -> None:
        """Kill only this exact session, then remove only this socket and log."""
        for name in self.buffers():
            self.run([self.tmux, "delete-buffer", "-b", name])
        self.run([self.tmux, "kill-session", "-t", self.name])
        if self.socket_path and Path(self.socket_path).name == self.name:
            Path(self.socket_path).unlink(missing_ok=True)
        shutil.rmtree(self.workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
