#!/usr/bin/env python3
"""Regression smoke for iss_f3e65e52 and iss_9c150bba: Core AI outputs are released, and warnings are true.

Measured cause (standalone harness, macOS 27, coreai-core 1.0.0b3): the binding keeps every result dict alive through
the asyncio Future it resolves, and the dict holds the output ``_NDArray``. On GPU that output is an IOSurface, so one
surface leaked per embedding until about 16.3k of them trapped the solet (SIGTRAP in CoreAIRuntime). The fix calls the
raw function, copies the vector out and clears the dict. Measured: 20,480 GPU embeddings through the plugin hold
IOSurface regions flat, where base grows one region per embedding.

The fake raw function here keeps every result dict it returns, exactly as the binding does, and Apple's real
``InferenceFunction`` wrapper and real ``_NDArray`` outputs sit around it, so base and fix run the same SDK code. Checks:
outputs are released after their result is cleared (base keeps all of them), and the returned vector owns its memory;
an output held anyway counts toward the pinned budget, loudly, and an exhausted budget refuses GPU with a typed
UNAVAILABLE that falls back to CPU instead of allocating towards the trap; the private members the release needs
(``InferenceFunction._function``, ``NDArray._wrap``) are proved at load, and a missing or changed one is a typed
UNAVAILABLE naming the installed coreai-core, never an AttributeError out of ``initialize()``; and the asset_root
warning fires only for a config the plugin was given. Offline; needs the coreai-core SDK import on macOS 27, no model
asset and no inference.

Run::

    .venv/bin/python3 plugins/coreai_embeddings_plugin/tests/gpu_output_release_smoke.py
"""

from __future__ import annotations

import asyncio
import gc
import importlib
import sys
import unittest
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))

import numpy as np  # noqa: E402
from coreai_embeddings_plugin import native as native_module  # noqa: E402
from coreai_embeddings_plugin.contracts import DIMENSION, EmbeddingError, ErrorCode  # noqa: E402
from coreai_embeddings_plugin.native import NativeModel, PinnedOutputs  # noqa: E402
from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin  # noqa: E402
from coreai_embeddings_plugin.runtime import EmbeddingRuntime  # noqa: E402
from coreai_embeddings_plugin.tokenization import EncodedInput  # noqa: E402

SDK: Any = importlib.import_module("coreai.runtime")
ENCODED = EncodedInput(256, [1] * 256, [1] * 256)
PLUGIN_LOGGER = "coreai_embeddings_plugin.plugin"
NATIVE_LOGGER = "coreai_embeddings_plugin.native"


def _unit_vector() -> Any:
    vector = np.zeros((1, DIMENSION), dtype=np.float32)
    vector[0, 0] = 1.0
    return vector


class RetainingFunction:
    """A raw Core AI function that keeps every result dict alive, as the binding's leaked Future does."""

    def __init__(self, *, pin_outputs: bool = False) -> None:
        self.results: list[dict[str, Any]] = []
        self.pinned: list[Any] = []
        self.pin_outputs = pin_outputs
        self.calls = 0

    async def __call__(self, inputs: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        del inputs, state
        self.calls += 1
        output = SDK._NDArray(_unit_vector(), "bytes")
        if self.pin_outputs:
            self.pinned.append(output)
        result = {"embedding": output}
        self.results.append(result)
        return result


def _live_outputs() -> int:
    gc.collect()
    return sum(1 for item in gc.get_objects()
               if type(item).__name__ == "_NDArray" and tuple(item.shape) == (1, DIMENSION))


def _native(function: RetainingFunction, preference: str) -> NativeModel:
    native = NativeModel()
    native.compute_preference = preference
    native._path = Path("/fixture.aimodel")
    native._functions = {256: SDK.InferenceFunction._wrap(function)}
    return native


class OutputReleaseTests(unittest.TestCase):
    """The leak's mechanism, against Apple's real wrapper and real output arrays."""

    def setUp(self) -> None:
        PinnedOutputs.count = 0

    def tearDown(self) -> None:
        PinnedOutputs.count = 0

    def test_cleared_results_release_every_output(self) -> None:
        function = RetainingFunction()
        native = _native(function, "gpu")
        before = _live_outputs()
        for _ in range(300):
            vector = asyncio.run(native.embed(ENCODED))
            self.assertEqual(vector[0], 1.0)
        self.assertEqual(len(function.results), 300, "control: the fake keeps every result, as the binding does")
        self.assertLessEqual(_live_outputs() - before, 0, "outputs stay alive after 300 embeddings (the leak)")
        self.assertEqual(PinnedOutputs.count, 0, "a released output was counted as pinned")
        copied = asyncio.run(native._infer(256, {}))
        self.assertTrue(copied.flags.owndata and copied.base is None,
                        "the vector views Core AI's output buffer, keeping its storage alive for as long as a caller holds it")

    def test_pinned_outputs_refuse_gpu_with_a_typed_error_and_fall_back(self) -> None:
        gpu = RetainingFunction(pin_outputs=True)
        cpu = RetainingFunction()
        native = _native(gpu, "gpu")

        async def load(_path: Path, preference: str) -> None:
            native.compute_preference = preference
            native._functions = {256: SDK.InferenceFunction._wrap(cpu)}

        with patch.object(native_module, "PINNED_OUTPUT_BUDGET", 3), patch.object(native, "load", side_effect=load):
            with self.assertLogs(NATIVE_LOGGER, "WARNING") as logged:
                vectors = [asyncio.run(native.embed(ENCODED)) for _ in range(5)]
            reason = PinnedOutputs.reason()
            with self.assertRaises(EmbeddingError) as refused:
                asyncio.run(_native(gpu, "gpu")._infer(256, {}))
        self.assertEqual(len(vectors), 5, "every call returned a vector; none killed or failed the caller")
        self.assertEqual(gpu.calls, 3, "GPU kept allocating past the pinned budget")
        self.assertEqual(PinnedOutputs.count, 3)
        self.assertEqual(native.compute_preference, "cpu")
        self.assertEqual(native.fallback_reason, reason)
        self.assertEqual(cpu.calls, 2)
        self.assertIs(refused.exception.code, ErrorCode.UNAVAILABLE)
        lines = "\n".join(logged.output)
        self.assertEqual(lines.count("Core AI kept a GPU output alive after its result was cleared; counting toward 3"), 1,
                         "the first pinned output was not logged loudly, exactly once")
        self.assertIn("Core AI GPU inference failed; retrying CPU-only: " + reason, lines)

    def test_held_cpu_outputs_never_count_against_the_gpu_budget(self) -> None:
        cpu = RetainingFunction(pin_outputs=True)
        native = _native(cpu, "cpu")
        for _ in range(4):
            asyncio.run(native.embed(ENCODED))
        self.assertEqual(len(cpu.pinned), 4, "control: the fake held every CPU output")
        self.assertEqual(PinnedOutputs.count, 0, "a CPU output, which holds no IOSurface, was counted as a pinned GPU output")

    def test_exhausted_budget_loads_cpu_only(self) -> None:
        model = MagicMock()
        model.function_names = ["seq256", "seq512", "seq1024", "seq2048"]
        sdk = MagicMock()
        sdk.AIModel.load = AsyncMock(return_value=model)
        native = NativeModel()
        PinnedOutputs.count = native_module.PINNED_OUTPUT_BUDGET
        with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=sdk):
            with self.assertLogs(NATIVE_LOGGER, "WARNING") as logged:
                asyncio.run(native.load(Path("/fixture.aimodel"), "gpu"))
        self.assertEqual(native.compute_preference, "cpu")
        self.assertEqual(native.fallback_reason, PinnedOutputs.reason())
        self.assertIn(PinnedOutputs.reason() + "; loading CPU-only specialization", "\n".join(logged.output))
        sdk.SpecializationOptions.from_preferred_compute_unit_kind.assert_not_called()
        self.assertEqual(sdk.AIModel.load.await_args.kwargs["specialization_options"],
                         sdk.SpecializationOptions.cpu_only.return_value)

    def test_runtime_diagnostics_report_the_pinned_count(self) -> None:
        PinnedOutputs.count = 2
        diagnostics = EmbeddingRuntime(Path("/fixture"), "gpu").diagnostics()
        self.assertEqual(diagnostics["pinned_gpu_outputs"], 2)
        self.assertEqual(diagnostics["pinned_gpu_output_budget"], native_module.PINNED_OUTPUT_BUDGET)


def _sdk(function: object, ndarray: object) -> MagicMock:
    model = MagicMock()
    model.function_names = ["seq256", "seq512", "seq1024", "seq2048"]
    model.load_function.return_value = function
    sdk = MagicMock()
    sdk.AIModel.load = AsyncMock(return_value=model)
    sdk.NDArray = ndarray
    return sdk


class BindingSurfaceTests(unittest.TestCase):
    """The private members _infer relies on are proved at load; a change fails typed, naming coreai-core."""

    version = metadata.version("coreai-core")

    def _load_error(self, sdk: MagicMock) -> EmbeddingError:
        with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=sdk):
            with self.assertRaises(EmbeddingError) as raised:
                asyncio.run(NativeModel().load(Path("/fixture.aimodel"), "cpu"))
        self.assertIs(raised.exception.code, ErrorCode.UNAVAILABLE)
        self.assertIn(f"Installed coreai-core {self.version}", str(raised.exception))
        return raised.exception

    def test_a_binding_without_the_raw_function_fails_typed_at_load(self) -> None:
        error = self._load_error(_sdk(SimpleNamespace(), SDK.NDArray))
        self.assertIn("InferenceFunction._function", str(error))

    def test_an_ndarray_without_wrap_fails_typed_at_load(self) -> None:
        error = self._load_error(_sdk(SimpleNamespace(_function=AsyncMock()), SimpleNamespace()))
        self.assertIn("NDArray._wrap", str(error))

    def test_the_real_binding_passes_the_load_check(self) -> None:
        """Control: the pinned SDK's own objects satisfy the check the two failures above trip."""
        with patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=_sdk(
                SDK.InferenceFunction._wrap(RetainingFunction()), SDK.NDArray)):
            native = NativeModel()
            asyncio.run(native.load(Path("/fixture.aimodel"), "cpu"))
        self.assertEqual(sorted(native._functions), [256, 512, 1024, 2048])

    def test_a_changed_raw_call_fails_typed(self) -> None:
        for error in (TypeError("unexpected keyword argument 'state'"), AttributeError("_tensor")):
            with self.subTest(error=type(error).__name__):
                native = _native(RetainingFunction(), "cpu")
                native._functions = {256: SimpleNamespace(_function=AsyncMock(side_effect=error))}
                with self.assertRaises(EmbeddingError) as raised:
                    asyncio.run(native.embed(ENCODED))
                self.assertIs(raised.exception.code, ErrorCode.UNAVAILABLE)
                self.assertIn(f"Installed coreai-core {self.version}", str(raised.exception))

    def test_initialize_reports_a_missing_binding_member_as_unavailable(self) -> None:
        """Boot order: the typed error reaches the plugin's warning, never an AttributeError out of initialize()."""
        sdk = _sdk(SimpleNamespace(), SDK.NDArray)

        class LoadingRuntime(_FakeRuntime):
            def prepare(self) -> None:
                asyncio.run(NativeModel().load(Path("/fixture.aimodel"), "cpu"))

        with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", LoadingRuntime), \
                patch("coreai_embeddings_plugin.native.importlib.import_module", return_value=sdk):
            plugin = CoreAIEmbeddingsPlugin()
            plugin.prepare_for_readiness()
            with self.assertLogs(PLUGIN_LOGGER, "WARNING") as logged:
                plugin.initialize({"asset_root": "/fixture/assets", "compute_preference": "cpu"})
        self.assertFalse(plugin.is_ready())
        self.assertIn("InferenceFunction._function", plugin.get_readiness_error() or "")
        self.assertIn(f"Installed coreai-core {self.version}", "\n".join(logged.output))


class _FakeRuntime:
    """Fault-free runtime at the model boundary; preparation reads no asset."""

    def __init__(self, _root: Path, _preference: str) -> None:
        pass

    def prepare(self) -> None:
        pass

    def close(self) -> None:
        pass


class AssetRootWarningTests(unittest.TestCase):
    """iss_9c150bba: startup Phase 1 prepares before initialize() delivers config."""

    def test_no_warning_before_config_and_none_for_a_valid_config(self) -> None:
        with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", _FakeRuntime):
            plugin = CoreAIEmbeddingsPlugin()
            with self.assertNoLogs(PLUGIN_LOGGER, level="WARNING"):
                plugin.prepare_for_readiness()
            self.assertFalse(plugin.is_ready())
            self.assertIn("not yet delivered", plugin.get_readiness_error() or "")
            with self.assertNoLogs(PLUGIN_LOGGER, level="WARNING"):
                plugin.initialize({"asset_root": "/fixture/assets", "compute_preference": "cpu"})
            self.assertTrue(plugin.is_ready())

    def test_a_delivered_invalid_config_still_warns(self) -> None:
        for config in ({"asset_root": "relative/assets"}, {}):
            with self.subTest(config=config), patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", _FakeRuntime):
                plugin = CoreAIEmbeddingsPlugin()
                with self.assertLogs(PLUGIN_LOGGER, "WARNING") as captured:
                    plugin.initialize(config)
                self.assertIn("asset_root must be an absolute directory path", "\n".join(captured.output))
                self.assertFalse(plugin.is_ready())
        with self.assertLogs(PLUGIN_LOGGER, "WARNING"):
            CoreAIEmbeddingsPlugin({}).prepare_for_readiness()


if __name__ == "__main__":
    unittest.main()
