#!/usr/bin/env python3
"""Smoke for the one helper that builds an exact tmux target.

A plain ``-t NAME`` is a prefix match, so a recorded reference can reach a
session it never named. This smoke runs a real tmux server on a PRIVATE socket
(never the default server) and shows that the exact forms reach only the named
session: ``=NAME`` for session verbs, ``=NAME:`` for pane verbs.

Checks T1-T4, one test method each. Skipped with a printed SKIP line when tmux
is not installed.

Run:
    SOLET_NAME=<name>-test PYTHONPATH=plugins/agent_messaging_plugin/src:ananta/src \
        .venv/bin/python3 plugins/agent_messaging_plugin/tests/tmux_target_smoke.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.tmux_target import pane_target, session_target  # noqa: E402

_TMUX = shutil.which("tmux")
_ENV_KEY = "TT_KEY"
_ENV_VALUE = "tt-value"


class TmuxTargetSmoke(unittest.TestCase):
    """Exact targets against a real tmux server on a private socket."""

    def setUp(self) -> None:
        if _TMUX is None:
            print("SKIP: tmux is not installed")
            self.skipTest("tmux is not installed")
        self.socket = f"s7t-{os.getpid()}-{time.time_ns()}"
        # A private server only: never inherit a surrounding default-server client.
        self.env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        self.socket_path = ""
        self.addCleanup(self._kill_server)
        for name in ("tt-alpha-1", "tt-alpha-12", "tt-beta-77"):
            started = self._tmux("new-session", "-d", "-s", name, "sleep 60")
            self.assertEqual(started.returncode, 0, started.stderr)
        self.socket_path = self._tmux("display-message", "-p", "#{socket_path}").stdout.strip()

    def _kill_server(self) -> None:
        self._tmux("kill-server")
        # kill-server leaves the socket file behind; this smoke made it, so it removes it.
        if self.socket_path and Path(self.socket_path).name == self.socket:
            Path(self.socket_path).unlink(missing_ok=True)

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(_TMUX), "-L", self.socket, *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
            check=False,
        )

    def _sessions(self) -> set[str]:
        listed = self._tmux("list-sessions", "-F", "#{session_name}")
        return set(listed.stdout.split())

    def test_t1_validation(self) -> None:
        self.assertEqual(session_target("a"), "=a")
        self.assertEqual(pane_target("a"), "=a:")
        for bad in ("", "a:b", "a.b", "=a"):
            for build in (session_target, pane_target):
                with self.subTest(ref=bad, build=build.__name__):
                    with self.assertRaises(ValueError) as caught:
                        build(bad)
                    self.assertIn("is not an exact session name", str(caught.exception))

    def test_t2_session_verbs(self) -> None:
        exact = session_target("tt-beta-77")
        prefix = session_target("tt-beta")
        verbs: dict[str, tuple[str, ...]] = {
            "has-session": ("has-session", "-t"),
            "set-environment": ("set-environment", "-t"),
            "show-environment": ("show-environment", "-t"),
        }
        tails: dict[str, tuple[str, ...]] = {
            "has-session": (),
            "set-environment": (_ENV_KEY, _ENV_VALUE),
            "show-environment": (_ENV_KEY,),
        }
        for verb, head in verbs.items():
            with self.subTest(verb=verb):
                hit = self._tmux(*head, exact, *tails[verb])
                self.assertEqual(hit.returncode, 0, hit.stderr)
                miss = self._tmux(*head, prefix, *tails[verb])
                self.assertNotEqual(miss.returncode, 0, f"{verb} reached a session by prefix")
        shown = self._tmux("show-environment", "-t", exact, _ENV_KEY)
        self.assertEqual(shown.stdout.strip(), f"{_ENV_KEY}={_ENV_VALUE}")
        # list-panes takes the colon form: exact reaches only its own session.
        listed = self._tmux("list-panes", "-t", pane_target("tt-beta-77"), "-F", "#{session_name}")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.split(), ["tt-beta-77"])
        listed_prefix = self._tmux("list-panes", "-t", pane_target("tt-beta"), "-F", "#{session_name}")
        self.assertNotEqual(listed_prefix.returncode, 0, "list-panes reached a session by prefix")
        # Documents a tmux 3.7b hazard: list-panes with the colonless session form prefix-matches.
        # A tmux that fixes this turns the assertion red; then revise the session_target docstring.
        leaked = self._tmux("list-panes", "-t", prefix, "-F", "#{session_name}")
        self.assertEqual(leaked.returncode, 0, "list-panes =NAME no longer prefix-matches")
        self.assertEqual(leaked.stdout.split(), ["tt-beta-77"])
        # Documents hazard F1: the plain colonless prefix form reaches a session it never named.
        plain = self._tmux("has-session", "-t", "tt-beta")
        self.assertEqual(plain.returncode, 0, "plain -t NAME is expected to prefix-match")
        # kill-session last: it removes the session the other verbs used.
        refused = self._tmux("kill-session", "-t", prefix)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("tt-beta-77", self._sessions())
        killed = self._tmux("kill-session", "-t", exact)
        self.assertEqual(killed.returncode, 0, killed.stderr)
        self.assertNotIn("tt-beta-77", self._sessions())

    def test_t3_pane_verbs(self) -> None:
        exact = pane_target("tt-beta-77")
        prefix = pane_target("tt-beta")
        pid = self._tmux("list-panes", "-t", session_target("tt-beta-77"), "-F", "#{pane_pid}")
        self.assertEqual(pid.returncode, 0, pid.stderr)
        self.assertTrue(pid.stdout.strip().isdigit())
        for target, label in ((exact, "exact"), (prefix, "prefix")):
            self.assertEqual(self._tmux("set-buffer", "-b", "tt-buf", "x").returncode, 0, "set-buffer failed")
            results = {
                "send-keys": self._tmux("send-keys", "-t", target, "-l", "x"),
                "capture-pane": self._tmux("capture-pane", "-p", "-t", target),
                "display-message": self._tmux("display-message", "-p", "-t", target, "#{pane_pid}"),
                "paste-buffer": self._tmux("paste-buffer", "-b", "tt-buf", "-t", target),
            }
            for verb, result in results.items():
                with self.subTest(verb=verb, target=label):
                    if label == "exact":
                        self.assertEqual(result.returncode, 0, result.stderr)
                    elif verb == "display-message":
                        # tmux 3.7b exits 0 here; the contract is an empty line, not the longer session's pid.
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), "")
                    else:
                        self.assertNotEqual(result.returncode, 0, f"{verb} reached a session by prefix")
            if label == "exact":
                self.assertEqual(results["display-message"].stdout.strip(), pid.stdout.strip())
        # Documents F3: the colonless exact form is a silent wrong answer for a pane verb.
        colonless = self._tmux("display-message", "-p", "-t", session_target("tt-beta-77"), "#{pane_pid}")
        self.assertEqual(colonless.stdout.strip(), "")

    def test_t4_neighbours(self) -> None:
        killed = self._tmux("kill-session", "-t", session_target("tt-alpha-1"))
        self.assertEqual(killed.returncode, 0, killed.stderr)
        remaining = self._sessions()
        self.assertNotIn("tt-alpha-1", remaining)
        self.assertIn("tt-alpha-12", remaining)


if __name__ == "__main__":
    unittest.main()
