#!/usr/bin/env python3
"""A generic ``spawn_session`` on the claude_code tmux host carries its first
turn as the CLI's own launch prompt, so a composer that is slow to appear can
no longer cost the session its first turn.

All panes and tmux commands are fakes; the shared driver fixtures live in
tmux_adapter_smoke and the spawn fixture in generic_spawn_smoke.

Checks:
  (a) with ``first_turn`` in the spec, the argv ends
      ``--append-system-prompt <text> -- <first turn>`` on both transports;
      without it the argv is today's, unchanged.
  (b) a real TmuxHostDriver over a pane that never shows an empty composer,
      driven through spawn_generic_session: the first turn reads delivered,
      nothing is pasted or typed, no 10 s wait happens, and the launch
      command carries the first message after ``--``.
  (c1) fake panes: the dev-channels dialog until Enter, then the CLI's echo of a
      first message that quotes the dialog's own title: spawn returns, kills
      nothing and sends no key once the echo is on screen.
  (c2) the same for the third-party-provider argv (bypass dialog, no dev-channels
      flag) with a first message that quotes the bypass dialog.
  (c3) ``_pane_shows_launch_prompt`` on its own.
  (c4) the same as a property: no fragment of a dialog (8 characters or more) is an echo
      of itself with the dialog alone on screen, and a real echo of one always is.
  (d) real tmux on a private socket: first messages of 1 byte, 4 KiB, 22 KiB and
      200 KiB reach the claude argv byte for byte (sha256 compared); 22 KiB and
      more exceed tmux's own command-length limit and take the source-file retry.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import tmux_adapter_smoke as shared  # noqa: E402
from composer_tmux_fixture import report_tmux_unmeasured, tmux_missing  # noqa: E402
from generic_spawn_smoke import _CALLER, _FIRST_MESSAGE, _MODEL, _fixture, _install_driver  # noqa: E402
from tmux_adapter_smoke import (  # noqa: E402
    _check,
    _configured_driver,
    _confirm_flow_run_fn,
    _effective_env_marker,
    _executable_stub,
    _extract_claude_argv_from_pane_command,
    _FakeCompleted,
    _is_send_enter,
    _stub_worker_hook_files,
)

from agent_messaging_plugin import tmux_adapter  # noqa: E402
from agent_messaging_plugin.tmux_adapter import (  # noqa: E402
    TmuxHostDriver,
    _pane_shows_bypass_prompt,
    _pane_shows_dev_channels_prompt,
)

_MAX_WALL_SECONDS = 5.0


def _argvs(transport: str) -> tuple[list[str], list[str]]:
    """The argv without and with a first turn, built by one driver so that
    paths inside it (hooks, settings) are the same in both."""
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp))
        spec: dict[str, object] = {"agent_instance_id": "agi-launch-a"}
        plain = driver._spawn_command(spec, transport=transport, label="launch-a")  # noqa: SLF001 -- unit under test
        carried = driver._spawn_command({**spec, "first_turn": _FIRST_MESSAGE}, transport=transport, label="launch-a")  # noqa: SLF001
        return plain, carried


def test_launch_argv_carries_first_turn() -> None:
    for transport in ("watch", "mcp"):
        plain, carried = _argvs(transport)
        _check(carried[-2:] == ["--", _FIRST_MESSAGE], f"(a) {transport}: the argv ends with `--` and the first turn byte for byte")
        _check("--append-system-prompt" not in carried, f"(a) {transport}: launch with a first turn has no authority prompt option")
        _check(carried[:-2] == plain, f"(a) {transport}: everything before the `--` is the argv built without a first turn")
        _check("--" not in plain, f"(a) {transport}: without a first turn the argv has no `--`")
        _check("--append-system-prompt" not in plain, f"(a) {transport}: launch without a first turn has no authority prompt option")


def _spawn_over_silent_pane(calls: list[list[str]]) -> tuple[dict[str, Any], dict[str, Any], float]:
    """Run one generic spawn through a real TmuxHostDriver whose fake pane never
    shows an empty composer; return the receipt, the job envelope and the wall
    seconds. Every tmux command lands in ``calls``."""
    with _fixture() as fx, tempfile.TemporaryDirectory() as tmp:
        fx.seed_cell(_MODEL)
        # The fake pane answers every capture with startup or "booted" text and
        # never an empty composer, so the boundary channel could only time out.
        _install_driver(fx, _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls)))
        started = time.monotonic()
        try:
            receipt, envelope = fx.spawn(fx.generic_params(), caller=_CALLER)
        except TimeoutError:
            # The fixture gives the launch job 5 s; a spawn still waiting on the
            # composer boundary outlives it. Report that as a failed check.
            receipt, envelope = {}, {}
        return receipt, envelope, time.monotonic() - started


def test_slow_composer_still_receives_the_first_turn() -> None:
    calls: list[list[str]] = []
    receipt, envelope, wall = _spawn_over_silent_pane(calls)
    data = cast(dict[str, Any], envelope.get("data") or {})
    _check(receipt.get("action_status") == "completed" and envelope.get("action_status") == "completed", "(b) the spawn completes")
    _check(data.get("first_turn_delivered") is True and data.get("first_turn_error") == "", f"(b) first_turn_delivered is true with no error (got {data.get('first_turn_delivered')!r}, {data.get('first_turn_error')!r})")
    _check(not any("paste-buffer" in c or ("send-keys" in c and "-l" in c) for c in calls), "(b) no paste-buffer and no literal send-keys call was made")
    _check(wall < _MAX_WALL_SECONDS, f"(b) the spawn returned within {_MAX_WALL_SECONDS:.0f}s, without waiting out the 10 s composer boundary (took {wall:.2f}s)")
    launches = [c for c in calls if "new-session" in c]
    argv = _extract_claude_argv_from_pane_command(launches[0][-1]) if launches else []
    _check(argv[-2:] == ["--", _FIRST_MESSAGE], "(b) the launch command's claude argv ends with `--` and the first message")


_DEV_DIALOG = "  WARNING: Loading development channels\n  Channels: server:testhom\n  \u276f 1. I am using this for local development\n  2. Exit\n  Enter to confirm \u00b7 Esc to cancel\n"
_BYPASS_DIALOG = "  WARNING: Claude Code running in Bypass Permissions mode\n  \u276f 1. No, exit\n  2. Yes, I accept\n  Enter to confirm \u00b7 Esc to cancel\n"
_QUOTES_DEV_DIALOG = "Read the first message. The log said WARNING: Loading development channels and then nothing else happened, so look at why."
_QUOTES_BYPASS_DIALOG = "Explain why 'Bypass Permissions mode' with 'Yes, I accept' showed up in the log, then continue with the brief."


def _echo_of(message: str) -> str:
    """The CLI's echo of the launch prompt: wrapped, every row indented two spaces.
    At 80 columns the quoted dialog phrases of the two messages below stay whole."""
    rows = textwrap.wrap(message, width=80)
    return "  Welcome back\n" + "".join(f"  {row}\n" for row in rows)


class _DialogThenEcho:
    """A fake pane: ``dialog`` until ``keys_before_echo`` keys were sent, then the
    echo of the first message. Keys sent after the echo is on screen are recorded."""

    def __init__(self, calls: list[list[str]], dialog: str, message: str, keys_before_echo: int) -> None:
        self.calls = calls
        self.dialog = dialog
        self.echo = _echo_of(message)
        self.keys_before_echo = keys_before_echo
        self.keys: list[str] = []
        self.keys_after_echo: list[str] = []
        self.echoed = False

    def __call__(self, cmd: list[str], **_kw: Any) -> _FakeCompleted:
        self.calls.append(cmd)
        if "send-keys" in cmd and "-l" not in cmd:
            (self.keys_after_echo if self.echoed else self.keys).append(cmd[-1])
        if "capture-pane" in cmd:
            self.echoed = len(self.keys) >= self.keys_before_echo
            return _FakeCompleted(stdout=self.echo if self.echoed else self.dialog)
        return _FakeCompleted()


def _spawn_over_echo(pane: _DialogThenEcho, message: str, instance: str, *, provider: bool) -> tuple[str, str]:
    """Spawn once through a real TmuxHostDriver over ``pane``; return the host
    reference and the refusal text (each empty when it did not happen)."""
    from agent_messaging_plugin.session_hosts import HostCannotSpawnError  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp, _effective_env_marker("1" if provider else None):
        driver = _configured_driver(Path(tmp), run_fn=pane, confirm_timeout_seconds=0.05)
        spec = {"agent_instance_id": instance, "label": "echo", "first_turn": message}
        try:
            return driver.spawn(spec), ""
        except HostCannotSpawnError as exc:
            return "", str(exc)


def test_echoed_first_turn_clears_the_dev_channels_wait() -> None:
    calls: list[list[str]] = []
    pane = _DialogThenEcho(calls, _DEV_DIALOG, _QUOTES_DEV_DIALOG, keys_before_echo=1)
    _check(_pane_shows_dev_channels_prompt(pane.echo), "(c1) setup: the echo alone still carries the dev-channels dialog marker")
    host_ref, refusal = _spawn_over_echo(pane, _QUOTES_DEV_DIALOG, "agi-echo01", provider=False)
    _check(bool(host_ref) and not refusal, f"(c1) spawn returns the host reference when the first message quotes the dev-channels dialog ({refusal or 'ok'})")
    _check(pane.keys == ["Enter"] and not pane.keys_after_echo, f"(c1) exactly one Enter, and no key once the echo is on screen (got {pane.keys}, then {pane.keys_after_echo})")
    _check(not any("kill-session" in cmd for cmd in calls), "(c1) the healthy session is not killed")
    _check(sum(_is_send_enter(cmd) for cmd in calls) == 1, "(c1) the dialog received one literal Enter")


def test_echoed_first_turn_clears_the_bypass_wait() -> None:
    calls: list[list[str]] = []
    pane = _DialogThenEcho(calls, _BYPASS_DIALOG, _QUOTES_BYPASS_DIALOG, keys_before_echo=2)
    _check(_pane_shows_bypass_prompt(pane.echo), "(c2) setup: the echo alone still carries both bypass dialog markers")
    host_ref, refusal = _spawn_over_echo(pane, _QUOTES_BYPASS_DIALOG, "agi-echo02", provider=True)
    launch = next((cmd for cmd in calls if "new-session" in cmd), [])
    argv = _extract_claude_argv_from_pane_command(launch[-1]) if launch else []
    _check("--dangerously-load-development-channels" not in argv, "(c2) the provider argv has no dev-channels flag")
    _check(bool(host_ref) and not refusal, f"(c2) spawn returns the host reference when the first message quotes the bypass dialog ({refusal or 'ok'})")
    _check(pane.keys == ["Down", "Enter"] and not pane.keys_after_echo, f"(c2) Down and Enter, and no key once the echo is on screen (got {pane.keys}, then {pane.keys_after_echo})")
    _check(not any("kill-session" in cmd for cmd in calls), "(c2) the healthy session is not killed")


def test_launch_prompt_predicate() -> None:
    wrapped = _echo_of(_QUOTES_DEV_DIALOG)
    _check(len(wrapped.splitlines()) > 2 and all(row.startswith("  ") for row in wrapped.splitlines()), "(c3) the fixture echo spans several rows, each indented two spaces")
    _check(tmux_adapter._pane_shows_launch_prompt(wrapped, _QUOTES_DEV_DIALOG) is True, "(c3) an echo wrapped over rows with a two-space indent is recognised")  # noqa: SLF001
    _check(tmux_adapter._pane_shows_launch_prompt(_DEV_DIALOG, _QUOTES_DEV_DIALOG) is False, "(c3) the dialog alone is not a launch prompt echo")  # noqa: SLF001
    _check(tmux_adapter._pane_shows_launch_prompt(_DEV_DIALOG, "WARNING: Loading development channels") is False, "(c3) the dev-channels dialog alone is not an echo even when the first turn is exactly its marker")  # noqa: SLF001
    _check(tmux_adapter._pane_shows_launch_prompt(_echo_of("WARNING: Loading development channels"), "WARNING: Loading development channels") is True, "(c3) the echo of a first turn that is exactly the marker is recognised once the dialog is gone")  # noqa: SLF001
    _check(tmux_adapter._pane_shows_launch_prompt(wrapped, "") is False, "(c3) an empty first turn is never on screen")  # noqa: SLF001
    _check(tmux_adapter._pane_shows_launch_prompt(wrapped, " \n\t ") is False, "(c3) a whitespace-only first turn is never on screen")  # noqa: SLF001


_DIALOG_FOOTER = "Enter to confirm"
_DEV_TITLE = "WARNING: Loading development channels"
_BYPASS_TITLE = "Bypass Permissions mode"


def _fragments(dialog: str) -> list[str]:
    """Every contiguous substring of the dialog's text, 8 characters or longer, that
    is not blank once whitespace is dropped (the predicate ignores whitespace)."""
    return [dialog[i:j] for i in range(len(dialog)) for j in range(i + 8, len(dialog) + 1) if "".join(dialog[i:j].split())]


def _quotable_fragments(fragments: list[str]) -> list[str]:
    """The fragments a first turn can quote without being the whole dialog: one that
    holds a title and the footer is the whole dialog, which no pane can tell from the
    dialog, so it is left out of the positive leg."""
    return [f for f in fragments if not (_DIALOG_FOOTER in f and (_DEV_TITLE in f or _BYPASS_TITLE in f))]


def _check_dialog_alone_is_no_echo(name: str, dialog: str, fragments: list[str]) -> None:
    alone = [f for f in fragments if tmux_adapter._pane_shows_launch_prompt(dialog, f)]  # noqa: SLF001
    _check(len(fragments) > 1000 and not alone, f"(c4) {name}: the dialog alone is not an echo for any of its {len(fragments)} fragments (wrongly True: {[a[:30] for a in alone[:3]]})")


def _check_echo_is_proof(name: str, dialog: str, fragments: list[str]) -> None:
    """An echo with the dialog gone or the first turn also present beyond the dialog is proof."""
    quotable = _quotable_fragments(fragments)
    gone = [f for f in quotable if not tmux_adapter._pane_shows_launch_prompt(_echo_of(f), f)]  # noqa: SLF001
    beyond = [f for f in quotable if not tmux_adapter._pane_shows_launch_prompt(dialog + _echo_of(f), f)]  # noqa: SLF001
    _check(not gone, f"(c4) {name}: the echo alone is proof for every fragment (missed {len(gone)}: {[g[:30] for g in gone[:3]]})")
    _check(not beyond, f"(c4) {name}: the echo beside the dialog is proof for every fragment (missed {len(beyond)}: {[b[:30] for b in beyond[:3]]})")


def _check_footer_quotes() -> None:
    footer_twice = _DEV_DIALOG + "Enter to confirm\n"
    _check(tmux_adapter._pane_shows_launch_prompt(footer_twice, "Enter to confirm") is True, "(c4) a second copy of the footer beyond the dialog's own is an echo")  # noqa: SLF001
    for turn in ("Enter to confirm", "Enter to confirm \u00b7 Esc to cancel", "Enter to confirm\nEsc to cancel", "WARNING: Loading development channels"):
        _check(tmux_adapter._pane_shows_launch_prompt(_DEV_DIALOG, turn) is False, f"(c4) the dev dialog alone is not an echo when the first turn is {turn!r}")  # noqa: SLF001


def test_no_fragment_of_a_dialog_is_an_echo_of_itself() -> None:
    """Class check: whatever part of a dialog the first turn quotes, the dialog alone is
    not proof the CLI echoed it, and a real echo still is."""
    for name, dialog in (("dev-channels", _DEV_DIALOG), ("bypass", _BYPASS_DIALOG)):
        fragments = _fragments(dialog)
        _check_dialog_alone_is_no_echo(name, dialog, fragments)
        _check_echo_is_proof(name, dialog, fragments)
    _check_footer_quotes()


_BASE_TEXT = 'Line one: it\'s "quoted", has a `backtick`, $HOME, a back\\slash and \u00fcber-\u65e5\u672c\u8a9e.\n--not an option, just text\n'
_SIZES = (1, 4 * 1024, 22 * 1024, 200 * 1024)
_RECORDED = "recorded.sha256"
_STUB_CLAUDE = """#!{python}
import hashlib, os, sys, time
from pathlib import Path
print("WARNING: Loading development channels", flush=True)
print("Enter to confirm", flush=True)
sys.stdin.readline()
sys.stdout.write("\\x1b[2J\\x1b[H")
sys.stdout.flush()
Path(__file__).with_name("{recorded}").write_text(hashlib.sha256(os.fsencode(sys.argv[-1])).hexdigest())
time.sleep(2)
"""


def _message_of(size: int) -> str:
    if size == 1:
        return "x"
    repeated = (_BASE_TEXT * (size // len(_BASE_TEXT) + 1)).encode()
    return repeated[:size].decode(errors="ignore")


def _await_file(path: Path, seconds: float) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.1)
    return ""


def _real_tmux_driver(tmp: Path, sock: str) -> TmuxHostDriver:
    (tmp / "claude").write_text(_STUB_CLAUDE.format(python=sys.executable, recorded=_RECORDED))
    (tmp / "claude").chmod(0o755)
    (tmp / ".mcp.json").write_text("{}")
    _stub_worker_hook_files(tmp)

    def run_fn(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        """The production subprocess seam, bound to this private socket only."""
        kw.setdefault("capture_output", True)
        kw.setdefault("text", True)
        kw.setdefault("timeout", 10)
        return subprocess.run([argv[0], "-L", sock, "-u", *argv[1:]], check=False, **kw)

    return TmuxHostDriver(
        tmux_bin=str(shutil.which("tmux")),
        claude_bin=str(tmp / "claude"),
        solet_name="testhom",
        solet_bin=_executable_stub(tmp / "venv" / "bin", "solet-bridge"),
        permission_mode="bypassPermissions",
        mcp_config_path=tmp / ".mcp.json",
        cwd=tmp,
        python_executable=str(tmp / "venv" / "bin" / "python3"),
        run_fn=run_fn,
    )


def _spawn_and_read_sha(driver: TmuxHostDriver, tmp: Path, number: int, message: str) -> tuple[str, str]:
    """One real spawn; return the host reference and the sha256 the stub recorded."""
    (tmp / _RECORDED).unlink(missing_ok=True)
    spec = {"agent_instance_id": f"agi-ftl{number}", "label": "first-turn", "first_turn": message}
    host_ref = driver.spawn(spec)
    return host_ref, _await_file(tmp / _RECORDED, 10.0)


def test_large_first_turns_reach_the_argv_byte_for_byte() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="spawn-first-turn-"))
    sock = f"spawn-first-turn-{uuid.uuid4().hex}"
    socket_path = ""
    try:
        with _effective_env_marker(None):
            driver = _real_tmux_driver(tmp, sock)
            for number, size in enumerate(_SIZES):
                message = _message_of(size)
                expected = hashlib.sha256(message.encode()).hexdigest()
                host_ref, recorded = _spawn_and_read_sha(driver, tmp, number, message)
                socket_path = socket_path or driver._run_fn([driver._tmux_bin, "display-message", "-p", "-t", host_ref, "#{socket_path}"]).stdout.strip()  # noqa: SLF001
                print(f"  (d) bytes={len(message.encode())} sha256_message={expected} sha256_recorded={recorded or 'none'}")
                _check(recorded == expected, f"(d) a {len(message.encode())}-byte first message reaches the claude argv with an identical sha256")
    finally:
        subprocess.run([str(shutil.which("tmux")), "-L", sock, "kill-server"], capture_output=True, text=True, check=False)
        # tmux leaves the socket file behind once its server has exited (the stub
        # ends its own session), so the test removes the file it caused to exist.
        if socket_path:
            Path(socket_path).unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
    _check(bool(socket_path), "(d) the private socket path was read from the live server")
    _check(not Path(socket_path).exists() and not tmp.exists(), f"(d) cleanup: the private socket {socket_path} and the temp dir {tmp} no longer exist")


def main() -> int:
    if tmux_missing():
        return report_tmux_unmeasured("the (d) leg needs a real tmux server on a private socket")
    tests: list[Callable[[], None]] = [
        test_launch_argv_carries_first_turn,
        test_slow_composer_still_receives_the_first_turn,
        test_echoed_first_turn_clears_the_dev_channels_wait,
        test_echoed_first_turn_clears_the_bypass_wait,
        test_launch_prompt_predicate,
        test_no_fragment_of_a_dialog_is_an_echo_of_itself,
        test_large_first_turns_reach_the_argv_byte_for_byte,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 -- one crashing leg must not hide the others
            _check(False, f"{test.__name__} raised {type(exc).__name__}: {exc}")
    print(f"PASSED: {shared._passed}")  # noqa: SLF001
    print(f"FAILED: {len(shared._failed)}")  # noqa: SLF001
    for label in shared._failed:  # noqa: SLF001
        print(f"  FAILED: {label}")
    return int(bool(shared._failed))  # noqa: SLF001


if __name__ == "__main__":
    raise SystemExit(main())
