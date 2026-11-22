#!/usr/bin/env python3
"""Offline child-exit notice checks for both headless host drivers."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from codex_managed_session_smoke import _codex_home, _executable, _FakeClient  # noqa: E402
from headless_adapter_smoke import _configured_driver, _FakeProc  # noqa: E402

from agent_messaging_plugin import codex_app_server, headless_adapter, host_end_notice, session_hosts  # noqa: E402
from agent_messaging_plugin.codex_app_server import CodexAppServerHostDriver  # noqa: E402
from agent_messaging_plugin.headless_adapter import HeadlessHostDriver  # noqa: E402


class _WaitProc(_FakeProc):
    def wait(self) -> int:
        return 3


class _WaitClient(_FakeClient):
    def wait_exit(self) -> int:
        return 3


class HeadlessEndNoticeSmoke(unittest.TestCase):
    def _stub(self, folder: Path, *, exit_code: int = 0) -> tuple[str, Path]:
        log = folder / "calls.log"
        stub = folder / "solet-bridge"
        stub.write_text(f'#!/bin/sh\nprintf "%s %s\\n" "$SOLET_NAME" "$*" >> "{log}"\nexit {exit_code}\n')
        stub.chmod(0o755)
        return str(stub), log

    def test_h0_modules_come_from_this_worktree(self) -> None:
        for module in (host_end_notice, headless_adapter, codex_app_server):
            assert module.__file__ is not None
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(ROOT), module.__file__)

    def test_h1_real_child_exit_sends_exact_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub, log = self._stub(Path(tmp))
            child = subprocess.Popen(["/bin/sh", "-c", "sleep 0.2; exit 3"])
            started = time.monotonic()
            with self.assertLogs(host_end_notice.logger, level="INFO") as captured:
                thread = host_end_notice.notify_on_exit(
                    child.wait,
                    host="headless",
                    host_ref=str(child.pid),
                    solet_name="live-test",
                    solet_bin=stub,
                )
                assert thread is not None
                deadline = started + 10
                while not log.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(log.exists(), "child exit notice did not arrive within 10 seconds")
                thread.join(timeout=1)
            elapsed = time.monotonic() - started
            print(f"H1 notice_seconds={elapsed:.3f}", file=sys.stderr)
            self.assertEqual(
                log.read_text(),
                f'live-test call --no-wait {host_end_notice.NOTICE_PROCESS_KEY} {{"host": "headless", "host_ref": "{child.pid}"}}\n',
            )
            self.assertTrue(any("3" in line and str(child.pid) in line for line in captured.output))

    def test_h2_blank_identity_warns_without_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub, log = self._stub(Path(tmp))
            for solet_name, solet_bin in (("", stub), ("live-test", "")):
                with self.assertLogs(host_end_notice.logger, level="WARNING") as captured:
                    result = host_end_notice.notify_on_exit(
                        lambda: 0,
                        host="headless",
                        host_ref="42",
                        solet_name=solet_name,
                        solet_bin=solet_bin,
                    )
                self.assertIsNone(result)
                self.assertEqual(len(captured.output), 1)
            self.assertFalse(log.exists())

    def test_h3_failed_call_or_wait_warns_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub, log = self._stub(Path(tmp), exit_code=1)
            child = subprocess.Popen(["/bin/sh", "-c", "exit 3"])
            with self.assertLogs(host_end_notice.logger, level="WARNING") as captured:
                thread = host_end_notice.notify_on_exit(
                    child.wait,
                    host="headless",
                    host_ref=str(child.pid),
                    solet_name="live-test",
                    solet_bin=stub,
                )
                assert thread is not None
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(captured.output), 1)
            self.assertIn("exited 1", captured.output[0])
            self.assertTrue(log.exists())
            log.unlink()

            def raise_wait() -> int:
                raise OSError("wait failed")

            with self.assertLogs(host_end_notice.logger, level="WARNING") as captured:
                thread = host_end_notice.notify_on_exit(
                    raise_wait,
                    host="headless",
                    host_ref="42",
                    solet_name="live-test",
                    solet_bin=stub,
                )
                assert thread is not None
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(captured.output), 1)
            self.assertIn("wait failed", captured.output[0])
            self.assertFalse(log.exists())

    def test_h4_claude_driver_arms_enabled_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = _WaitProc(pid=54321)
            base = _configured_driver(Path(tmp), popen_fn=lambda *a, **k: proc)
            driver = HeadlessHostDriver(
                claude_bin=base._claude_bin,
                solet_name=base._solet_name,
                solet_bin=base._solet_bin,
                permission_mode=base._permission_mode,
                mcp_config_path=base._mcp_config_path,
                cwd=base._cwd,
                popen_fn=cast(Any, lambda *a, **k: proc),
                end_notice=True,
            )
            with patch.object(headless_adapter, "notify_on_exit") as notify:
                self.assertEqual(driver.spawn({"agent_instance_id": "agi-end-h4", "transport": "mcp"}), "54321")
            notify.assert_called_once()
            args, kwargs = notify.call_args
            self.assertEqual(args[0], proc.wait)
            self.assertEqual((kwargs["host"], kwargs["host_ref"]), ("headless", "54321"))

    def test_h5_claude_driver_default_does_not_arm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), popen_fn=lambda *a, **k: _FakeProc(pid=54322))
            with patch.object(headless_adapter, "notify_on_exit") as notify:
                driver.spawn({"agent_instance_id": "agi-end-h5", "transport": "mcp"})
            notify.assert_not_called()

    def test_h6_codex_driver_and_registry_arm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            driver = CodexAppServerHostDriver(
                codex_bin=_executable(root, "codex"),
                solet_bin=_executable(root / "venv" / "bin", "solet-bridge"),
                solet_name="testhom",
                codex_home=_codex_home(root),
                cwd=root,
                python_executable=str(root / "venv" / "bin" / "python3"),
                client_factory=_WaitClient,  # pyright: ignore[reportArgumentType]
                end_notice=True,
            )
            with patch.object(codex_app_server, "notify_on_exit") as notify:
                self.assertEqual(
                    driver.spawn({"agent_instance_id": "agi-end-h6", "transport": "mcp"}),
                    "4242",
                )
            notify.assert_called_once()
            args, kwargs = notify.call_args
            self.assertEqual(args[0], _WaitClient.instances[-1].wait_exit)
            self.assertEqual((kwargs["host"], kwargs["host_ref"]), ("headless", "4242"))
            registry = session_hosts._build_registry()
            self.assertTrue(registry[("claude_code", "headless")]._end_notice)
            self.assertTrue(registry[("codex", "headless")]._end_notice)


if __name__ == "__main__":
    unittest.main()
