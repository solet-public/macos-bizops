#!/usr/bin/env python3
"""Standalone smoke for AutostartManager._render_plist (no pytest).

Validates the Option-B LaunchAgent plist shape (2026-06-28) + the §5
CWD-hygiene fix (design ``2026-06-27_true_local_blue_green_materialized_
artifacts_design.md``):

* ``ProgramArguments`` launches the colour-agnostic supervisor module
  (``-m macos_self_deployment_plugin.supervisor``), NOT ``ananta.cli``
  directly — the launchd-managed process is never a homunculus colour.
* ``KeepAlive`` is an unconditional ``<true/>`` (a bool, not a dict): the
  supervisor is an infinite loop, so any exit while loaded means restart.
  The interim ``Crashed``/``SuccessfulExit`` dict (and the earlier Slice-4
  ``PathState`` predicate) are gone — no homunculus colour is launchd-managed, so
  the ghost-respawn class is structurally impossible. The ``.draining``
  sentinel does NOT appear in the plist (it gates the *supervisor*, not
  launchd).
* ``WorkingDirectory`` is the out-of-tree runtime dir
  (``get_runtime_dir(name)``), NOT the repo root — a managed process
  must never have its CWD set to a code tree, or a stray relative-path
  write would mutate it (§5).
* ``RunAtLoad=true`` retained — cold-start-at-login.

Also asserts the plist is valid XML and parseable via the stdlib
``plistlib``.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/autostart_plist_render_smoke.py
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.runtime import get_runtime_dir  # noqa: E402
from macos_self_deployment_plugin.autostart_manager import AutostartManager  # noqa: E402

# A non-existent, non-/tmp scratch path. Since the §4.5 role-1 fix
# (2026-06-28) the rendered interpreter is the baked LITERAL ``current``
# symlink path (from the default releases_root), no longer derived from
# ``project_root``'s ``.venv`` — so ``project_root`` now only feeds the
# profile path here. Per the operator no-/tmp rule, scratch lives under
# ``~/.ananta/``, never ``/tmp``. (The interpreter-path behavior itself is
# covered by ``autostart_seed_smoke.py``.)
_FAKE_PROJECT_ROOT = (
    Path.home() / ".ananta" / "_smoke_scratch" / "autostart-render-fake-root"
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _render() -> tuple[bytes, str]:
    mgr = AutostartManager(
        homunculus_name="example",
        project_root=_FAKE_PROJECT_ROOT,
    )
    raw = mgr._render_plist()  # noqa: SLF001
    return raw, raw.decode("utf-8")


def test_render_plist_xml_strings_present() -> None:
    _, body = _render()
    for assertion, label in (
        ("<key>KeepAlive</key>" in body,
         "KeepAlive key present in plist body"),
        ("<key>KeepAlive</key>\n  <true/>" in body,
         "KeepAlive is an unconditional <true/> (bool, not a dict)"),
        ("<key>KeepAlive</key>\n  <dict>" not in body,
         "KeepAlive is NOT a dict (Crashed/SuccessfulExit dance is gone)"),
        ("<key>Crashed</key>" not in body,
         "no Crashed key (no homunculus colour is launchd-managed)"),
        ("<key>SuccessfulExit</key>" not in body,
         "no SuccessfulExit key (exit-code dance obsolete)"),
        ("<string>macos_self_deployment_plugin.supervisor</string>" in body,
         "ProgramArguments runs the colour-agnostic supervisor module"),
        ("<string>ananta.cli</string>" not in body,
         "ProgramArguments does NOT launch ananta.cli directly"),
        ("<key>PathState</key>" not in body,
         "no PathState entry"),
        (".draining" not in body,
         "drain sentinel not referenced in plist (gates the supervisor, not launchd)"),
        ("<key>RunAtLoad</key>\n  <true/>" in body,
         "RunAtLoad=true retained (cold-start-at-login)"),
    ):
        _check(assertion, label)


def test_render_plist_parses_as_dict() -> None:
    raw, _ = _render()
    parsed = plistlib.loads(raw)
    _check(isinstance(parsed, dict), "plist parses as a dict")
    if not isinstance(parsed, dict):
        return
    _check(
        parsed.get("Label") == "local.homunculus.example",
        f"Label = 'local.homunculus.example' (got {parsed.get('Label')!r})",
    )


def test_parsed_keepalive_is_unconditional_true() -> None:
    raw, _ = _render()
    parsed = plistlib.loads(raw)
    keep_alive = parsed.get("KeepAlive") if isinstance(parsed, dict) else None
    _check(
        keep_alive is True,
        f"parsed KeepAlive is an unconditional bool True (got {keep_alive!r})",
    )


def test_parsed_program_arguments_launch_supervisor() -> None:
    raw, _ = _render()
    parsed = plistlib.loads(raw)
    args = parsed.get("ProgramArguments") if isinstance(parsed, dict) else None
    if not isinstance(args, list):
        _check(False, "parsed ProgramArguments is a list")
        return
    _check(
        "macos_self_deployment_plugin.supervisor" in args,
        f"ProgramArguments includes the supervisor module (got {args!r})",
    )
    _check(
        "ananta.cli" not in args,
        "ProgramArguments does NOT launch ananta.cli directly",
    )
    _check(
        args[:2] == [args[0], "-m"] and args[0].endswith("/current/venv/bin/python3"),
        f"interpreter is the literal current/venv/bin/python3, run with -m (got {args[:3]!r})",
    )


def test_working_directory_out_of_tree() -> None:
    """§5: WorkingDirectory must be the out-of-tree runtime dir, never a code tree."""
    raw, _ = _render()
    parsed = plistlib.loads(raw)
    working_dir = parsed.get("WorkingDirectory") if isinstance(parsed, dict) else None
    expected = str(get_runtime_dir("example"))
    _check(
        working_dir == expected,
        f"WorkingDirectory == get_runtime_dir('example') "
        f"(expected {expected!r}, got {working_dir!r})",
    )
    _check(
        working_dir != str(_FAKE_PROJECT_ROOT),
        "WorkingDirectory is NOT the project_root (no code-tree CWD)",
    )
    _check(
        isinstance(working_dir, str) and str(REPO_ROOT) not in working_dir,
        f"WorkingDirectory is outside the repo tree (got {working_dir!r})",
    )


def main() -> int:
    print("=== autostart_plist_render_smoke (Option-B supervisor KeepAlive + §5 CWD) ===")
    test_render_plist_xml_strings_present()
    test_render_plist_parses_as_dict()
    test_parsed_keepalive_is_unconditional_true()
    test_parsed_program_arguments_launch_supervisor()
    test_working_directory_out_of_tree()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
