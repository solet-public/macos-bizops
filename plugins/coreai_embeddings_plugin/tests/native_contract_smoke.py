"""Native adapter fault controls, independent of measured hardware evidence."""

import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))

import numpy as np  # noqa: E402
from coreai_embeddings_plugin.contracts import EmbeddingError  # noqa: E402
from coreai_embeddings_plugin.native import NativeModel  # noqa: E402
from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin  # noqa: E402
from coreai_embeddings_plugin.tokenization import EncodedInput  # noqa: E402


def _function(vector: object, error: Exception | None = None) -> SimpleNamespace:
    """A raw Core AI function: a fresh result dict per call, which native.py clears after reading."""

    async def call(**_kwargs: object) -> dict[str, object]:
        if error is not None:
            raise error
        return {"embedding": vector}

    return SimpleNamespace(_function=AsyncMock(side_effect=call))


class _NDArray:
    """The SDK's NDArray surface native.py uses: construct around a value, wrap a raw output."""

    def __init__(self, value: object) -> None:
        self._tensor = value

    @staticmethod
    def _wrap(tensor: object) -> SimpleNamespace:
        return SimpleNamespace(numpy=lambda: tensor)


_SDK = SimpleNamespace(NDArray=_NDArray)


class NativeTests(unittest.TestCase):
    """Exercise failure paths which real healthy hardware cannot deterministically trigger."""

    def test_invalid_outputs_fail(self) -> None:
        cases = [np.zeros((1, 768), dtype=np.float32), np.ones((1, 10), dtype=np.float32), np.full((1, 768), np.nan, dtype=np.float32), np.ones((1, 768), dtype=np.float64)]
        for vector in cases:
            with self.subTest(shape=vector.shape):
                native = NativeModel()
                native._functions = {256: _function(vector)}
                with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=_SDK):
                    with self.assertRaises(EmbeddingError):
                        asyncio.run(native.embed(EncodedInput(256, [1] * 256, [1] * 256)))

    def test_every_default_is_cpu(self) -> None:
        """iss_f3e65e52: GPU inference leaks one IOSurface per embed, so CPU is the default everywhere."""
        plugin = CoreAIEmbeddingsPlugin({"asset_root": "/fixture"})
        self.assertEqual(plugin._settings(), (Path("/fixture"), "cpu"))
        self.assertEqual(plugin.get_config_schema()["properties"]["compute_preference"]["default"], "cpu")  # type: ignore[index]
        manifest = (ROOT / "plugins" / "coreai_embeddings_plugin" / "plugin.yaml").read_text(encoding="utf-8")
        self.assertRegex(manifest, r"compute_preference:\n\s+type: string\n\s+default: cpu\n")
        self.assertEqual(CoreAIEmbeddingsPlugin({"asset_root": "/fixture", "compute_preference": "gpu"})._settings()[1], "gpu")

    def test_gpu_load_failure_falls_back_to_cpu(self) -> None:
        model = MagicMock()
        model.function_names = ["seq256", "seq512", "seq1024", "seq2048"]
        sdk = MagicMock()
        sdk.AIModel.load = AsyncMock(side_effect=[RuntimeError("GPU unavailable"), model])
        native = NativeModel()
        with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=sdk):
            asyncio.run(native.load(Path("/fixture.aimodel"), "gpu"))
        self.assertEqual(native.compute_preference, "cpu")
        self.assertEqual(native.fallback_reason, "GPU unavailable")
        self.assertEqual(native.observed_compute_unit, "unknown")
        self.assertEqual(sdk.AIModel.load.await_count, 2)

    def test_gpu_inference_failure_retries_once(self) -> None:
        vector = np.zeros((1, 768), dtype=np.float32)
        vector[0, 0] = 1
        native = NativeModel()
        native._path = Path("/fixture.aimodel")
        native._functions = {256: _function(vector, RuntimeError("GPU execution failed"))}

        async def load(_path: Path, preference: str) -> None:
            native.compute_preference = preference
            native._functions = {256: _function(vector)}

        with patch.object(native, "load", side_effect=load) as loader:
            with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=_SDK):
                output = asyncio.run(native.embed(EncodedInput(256, [1] * 256, [1] * 256)))
        loader.assert_called_once_with(Path("/fixture.aimodel"), "cpu")
        self.assertEqual(output[0], 1)
        self.assertEqual(native.observed_compute_unit, "cpu")
        self.assertEqual(native.fallback_reason, "GPU execution failed")

    def test_missing_bucket_is_asset_error_without_fallback(self) -> None:
        sdk = MagicMock()
        model = MagicMock()
        model.function_names = ["seq256"]
        sdk.AIModel.load = AsyncMock(return_value=model)
        with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=sdk):
            with self.assertRaises(EmbeddingError):
                asyncio.run(NativeModel().load(Path("/fixture.aimodel"), "gpu"))
        self.assertEqual(sdk.AIModel.load.await_count, 1)


class CallbackTests(unittest.TestCase):
    """A native callback may enqueue an event but must never inspect it on that thread."""

    def test_setting_set_correct_clear_and_invalid(self) -> None:
        from coreai_embeddings_plugin import native as module

        name = "SOLET_COREAI_CALLBACK_QUEUE_CAPACITY"
        with patch.dict(os.environ, {name: "64"}):
            self.assertEqual(module._callback_setting(name, 256, integral=True), 64)
            os.environ[name] = "32"
            self.assertEqual(module._callback_setting(name, 256, integral=True), 32)
            for invalid in ("0", "-1", "nan", "inf", "1.5", "unknown"):
                os.environ[name] = invalid
                with self.assertRaisesRegex(ValueError, name):
                    module._callback_setting(name, 256, integral=True)
            del os.environ[name]
            self.assertEqual(module._callback_setting(name, 256, integral=True), 256)

    def test_foreign_callback_only_enqueues(self) -> None:
        owner = threading.get_ident()
        inspected: list[int] = []
        names: list[str] = []
        loop_threads: list[int] = []
        errors: list[BaseException] = []

        class Event:
            @property
            def event_id(self) -> str:
                inspected.append(threading.get_ident())
                names.append(threading.current_thread().name)
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    loop_threads.append(threading.get_ident())
                return "call.MPSGraph"

        native = NativeModel()

        def callback() -> None:
            try:
                native._observe(Event())
            except BaseException as exc:  # noqa: BLE001 — return foreign-thread failures to the test
                errors.append(exc)

        thread = threading.Thread(target=callback, daemon=True)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive(), "native callback blocked")
        self.assertFalse(errors, str(errors))
        self.assertEqual(inspected, [], "CALLBACK_ENQUEUE_ONLY: event inspection ran on the native thread")
        self.assertNotEqual(thread.ident, owner)

        async def operation() -> int:
            return 42

        self.assertEqual(asyncio.run(native._await_native(operation())), 42)
        self.assertEqual(len(inspected), 1)
        self.assertNotEqual(inspected[0], owner, "COREAI_OFF_LOOP: payload inspection ran on the owner loop")
        self.assertEqual(names, ["coreai-profiler"])
        self.assertEqual(loop_threads, [], "profiler payloads must run on a plain worker without a running loop")
        self.assertEqual(native.observed_compute_unit, "gpu")

    def test_full_queue_is_advisory_and_drain_is_bounded(self) -> None:
        from coreai_embeddings_plugin import native as module

        native = NativeModel()
        for _ in range(module.CALLBACK_QUEUE_CAPACITY + 3):
            native._observe(SimpleNamespace(event_id="call.MPSGraph"))
        self.assertEqual(native._events.qsize(), module.CALLBACK_QUEUE_CAPACITY)
        with self.assertLogs(module.logger, level="WARNING") as captured:
            self.assertEqual(native._drain_events(), module.CALLBACK_DRAIN_BATCH)
        self.assertEqual(native._events.qsize(), module.CALLBACK_QUEUE_CAPACITY - module.CALLBACK_DRAIN_BATCH)
        self.assertIn("3 events omitted", captured.output[0])

        async def operation() -> str:
            return "native result"

        self.assertEqual(asyncio.run(native._await_native(operation())), "native result")
        self.assertEqual(native._events.qsize(), 0)

    def test_failure_and_cancellation_retire_consumer_and_allow_next_call(self) -> None:
        from coreai_embeddings_plugin import native as module

        native = NativeModel()
        workers: list[threading.Thread] = []

        def make_worker(**kwargs: Any) -> threading.Thread:
            worker = threading.Thread(**kwargs)
            workers.append(worker)
            return worker

        async def exercise() -> None:
            async def fail() -> None:
                raise RuntimeError("native failure")

            with self.assertRaisesRegex(RuntimeError, "native failure"):
                await native._await_native(fail())
            entered = asyncio.Event()

            async def pending() -> None:
                entered.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(native._await_native(pending()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            native._observe(SimpleNamespace(event_id="call.MPSGraph"))
            self.assertEqual(await native._await_native(asyncio.sleep(0, result=7)), 7)
            self.assertEqual(native.observed_compute_unit, "gpu", "a later callback must reach a fresh consumer")

        with patch.object(module, "Thread", side_effect=make_worker):
            asyncio.run(exercise())
        self.assertEqual(len(workers), 3)
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive(), "native failure/cancellation/success must retire the profiler worker")

    def test_profiler_worker_sleeps_until_an_event_or_the_end_of_the_call(self) -> None:
        """PROFILER_NO_POLL (prb_f6964132): a 1 ms poll took the GIL ~1000 times a second per inference."""
        native = NativeModel()
        drains: list[int] = []
        original = native._drain_events

        def counted() -> int:
            drains.append(1)
            return original()

        async def slow_native() -> int:
            await asyncio.sleep(0.2)
            return 5

        with patch.object(native, "_drain_events", side_effect=counted):
            self.assertEqual(asyncio.run(native._await_native(slow_native())), 5)
        self.assertLessEqual(len(drains), 4, f"profiler worker woke {len(drains)} times in 0.2 s with no events")
        native._observe(SimpleNamespace(event_id="call.MPSGraph"))
        self.assertEqual(asyncio.run(native._await_native(asyncio.sleep(0, result=1))), 1)
        self.assertEqual(native.observed_compute_unit, "gpu", "an enqueued event still wakes the next consumer")

    def test_worker_unavailable_is_advisory_and_a_later_call_recovers(self) -> None:
        from coreai_embeddings_plugin import native as module

        native = NativeModel()
        native._observe(SimpleNamespace(event_id="call.MPSGraph"))
        with patch.object(module, "Thread") as thread, self.assertLogs(module.logger, level="WARNING") as captured:
            thread.return_value.start.side_effect = RuntimeError("thread unavailable")
            self.assertEqual(asyncio.run(native._await_native(asyncio.sleep(0, result=7))), 7)
        self.assertIn("inference continues", captured.output[0])
        self.assertEqual(native.observed_compute_unit, "unknown", "the owner loop must not inspect even when no worker starts")
        self.assertEqual(asyncio.run(native._await_native(asyncio.sleep(0, result=9))), 9)
        self.assertEqual(native.observed_compute_unit, "gpu")


if __name__ == "__main__":
    unittest.main()
