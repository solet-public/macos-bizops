"""Vetting CLI loop progress with slow Git/filesystem stubs."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "code_vetting_plugin" / "src"))

from code_vetting_plugin import pipeline, render_sample  # noqa: E402


class VettingResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def responsive[T](self, action: Callable[[], Awaitable[T]]) -> T:
        gaps: list[float] = []
        stop = asyncio.Event()

        async def ticker() -> None:
            previous = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - previous)
                previous = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        try:
            return await action()
        finally:
            await asyncio.sleep(0.01)
            stop.set()
            await task
            gap = max(gaps)
            print(f"ticker {self.id()}: {gap:.4f}s")
            self.assertLess(gap, 0.1, "slow backend blocked the same-loop ticker")

    async def test_cli_entrypoints(self) -> None:
        for module in (pipeline, render_sample):
            for mode in ("fast", "slow", "failure"):
                for phase in ("git", "files"):
                    with self.subTest(module=module.__name__, mode=mode, phase=phase):
                        await self._check_cli(module, mode, phase)

    async def _check_cli(self, module: Any, mode: str, phase: str) -> None:
        state: Any = SimpleNamespace(rows={"vetting_runs": {"vr-stub": {"ok": True}, "vr-sample-stub": {"ok": True}}})
        driver = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(report="report", findings=[])))
        original = Path.write_text

        def backend(which: str) -> None:
            if phase == which and mode != "fast":
                time.sleep(0.3)
                if mode == "failure":
                    raise OSError("backend failed")

        def git(_root: Path) -> str:
            backend("git")
            return "stub"

        def write(path: Path, value: str, **kwargs: Any) -> int:
            backend("files")
            return original(path, value, **kwargs)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(module, "InMemoryStateWriter", return_value=state),
            patch.object(module, "git_head", git),
            patch.object(module, "allowlist_totals", return_value={}),
            patch.object(module, "_REPORT_PATH", Path(directory) / "report"),
            patch.object(module, "_ROW_PATH", Path(directory) / "row"),
            patch.object(module, "_print_summary"),
            patch.object(Path, "write_text", write),
        ):
            builder = patch.object(pipeline, "_build_driver", return_value=driver) if module is pipeline else patch.object(render_sample, "VettingDriver", return_value=driver)
            with builder, patch.object(render_sample, "_demo_retention", AsyncMock(return_value="retained")):
                if mode == "failure":
                    with self.assertRaisesRegex(OSError, "backend failed"):
                        await self.responsive(module.main)
                    self.assertEqual(driver.run.await_count, 0 if phase == "git" else 1)
                else:
                    await self.responsive(module.main)
                    self.assertEqual(module._REPORT_PATH.read_text(), "report")
                    self.assertEqual(module._ROW_PATH.read_text(), '{\n  "ok": true\n}\n')
                    self.assertEqual(driver.run.await_count, 1)


if __name__ == "__main__":
    unittest.main()
