"""Core AI adapter, imported only when the runtime is explicitly prepared."""

import asyncio
import importlib
import logging
import math
import os
import sys
from collections.abc import Awaitable
from concurrent.futures import Future
from importlib import metadata
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import sleep
from typing import Any, TypeVar

import numpy as np

from .contracts import BUCKETS, DIMENSION, EmbeddingError, ErrorCode
from .tokenization import EncodedInput

logger = logging.getLogger(__name__)

#: GPU outputs Core AI may keep alive in one process before GPU inference is refused. coreai-core 1.0.0b3 keeps every
#: result dict alive through the Future it resolves; the dict holds the output, and a GPU output is an IOSurface that no
#: model or runtime close releases. About 16.3k of them trap the whole process (iss_f3e65e52). Clearing the result
#: releases the output; this budget, about half that ceiling, only bounds a runtime whose outputs stay held anyway.
PINNED_OUTPUT_BUDGET = 8000
T = TypeVar("T")


def _callback_setting(name: str, default: float, *, integral: bool = False) -> float:
    """Positive finite startup configuration; no ambiguous implicit queue policy."""
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be positive and finite, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0 or integral and not value.is_integer():
        raise ValueError(f"{name} must be positive, finite and {'integral' if integral else 'numeric'}, got {raw!r}")
    return value


DEFAULT_CALLBACK_QUEUE_CAPACITY = 256
DEFAULT_CALLBACK_DRAIN_BATCH = 32
CALLBACK_QUEUE_CAPACITY = int(_callback_setting("SOLET_COREAI_CALLBACK_QUEUE_CAPACITY", DEFAULT_CALLBACK_QUEUE_CAPACITY, integral=True))
CALLBACK_DRAIN_BATCH = int(_callback_setting("SOLET_COREAI_CALLBACK_DRAIN_BATCH", DEFAULT_CALLBACK_DRAIN_BATCH, integral=True))


class PinnedOutputs:
    """Process-wide count of GPU outputs still held after their result was cleared; they outlive every model."""

    count = 0

    @classmethod
    def exhausted(cls) -> bool:
        return cls.count >= PINNED_OUTPUT_BUDGET

    @classmethod
    def reason(cls) -> str:
        return f"Core AI kept {cls.count} GPU outputs alive (budget {PINNED_OUTPUT_BUDGET}); GPU inference refused"


def _binding_unavailable(detail: str) -> EmbeddingError:
    """The private SDK surface this module relies on is absent or changed; name the installed coreai-core."""
    try:
        version = metadata.version("coreai-core")
    except metadata.PackageNotFoundError:
        version = "not installed as a distribution"
    return EmbeddingError(
        ErrorCode.UNAVAILABLE,
        f"Installed coreai-core {version} lacks the binding surface this plugin needs ({detail}); the plugin is pinned to coreai-core 1.0.0b3",
    )


def _held_elsewhere(value: object) -> bool:
    """Whether anything beyond the caller's one name holds value; getrefcount counts its own argument as well."""
    anchor = object()
    return sys.getrefcount(value) > sys.getrefcount(anchor) + 1


class NativeModel:
    """One native model and its functions, confined to the runtime worker."""

    def __init__(self) -> None:
        self._model: Any = None
        self._path: Path | None = None
        self._functions: dict[int, Any] = {}
        self._profiler: Any = None
        self.compute_preference = "gpu"
        self.observed_compute_unit = "unknown"
        self.compute_evidence: set[str] = set()
        self.fallback_reason: str | None = None
        self._events: Queue[Any] = Queue(maxsize=CALLBACK_QUEUE_CAPACITY)
        # Set by every enqueue and by retirement: the consumer sleeps until there is work, never polls
        # (a 1 ms poll took the GIL ~1000 times a second for each 3-6 s inference, prb_f6964132).
        self._pending = Event()
        self._dropped_events = 0
        self._reported_drops = 0

    def _observe(self, event: Any) -> None:
        """Native/GCD entry: enqueue only; never inspect a native event here."""
        try:
            self._events.put_nowait(event)
        except Full:
            # Profiling is advisory. Never park a native compute thread or
            # refuse a vector because optional telemetry outran its consumer.
            self._dropped_events += 1
        self._pending.set()

    def _drain_events(self) -> int:
        drained = 0
        for _ in range(CALLBACK_DRAIN_BATCH):
            try:
                event = self._events.get_nowait()
            except Empty:
                break
            try:
                name = str(event.event_id)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning("Core AI profiler event unavailable; inference continues: %s", exc)
                drained += 1
                continue
            if name == "call.MPSGraph":
                self.compute_evidence.add(name)
                self.observed_compute_unit = "gpu"
            drained += 1
        dropped = self._dropped_events
        if dropped != self._reported_drops:
            logger.warning("Core AI profiler queue saturated: %d events omitted; compute evidence may be incomplete", dropped)
            self._reported_drops = dropped
        return drained

    def _consume_events(self, stop: Event, finished: Future[None]) -> None:
        """Plain worker: payload inspection never executes on an asyncio loop."""
        try:
            while not stop.is_set():
                self._pending.wait()
                self._pending.clear()
                self._drain_events()
                if not self._events.empty():
                    self._pending.set()
            # Cancelled native work may still emit. Bound the final flush even
            # if a producer continues, then retire this operation's worker.
            for _ in range((CALLBACK_QUEUE_CAPACITY + CALLBACK_DRAIN_BATCH - 1) // CALLBACK_DRAIN_BATCH):
                if not self._drain_events():
                    break
                sleep(0)
        except Exception:  # noqa: BLE001 — optional profiler evidence cannot replace the native result
            logger.exception("Core AI profiler worker failed; compute evidence may be incomplete")
        finally:
            finished.set_result(None)

    async def _await_native(self, operation: Awaitable[T]) -> T:
        """One plain consumer per serialized native call; never join it on a loop."""
        stop = Event()
        finished: Future[None] = Future()
        worker = Thread(target=self._consume_events, args=(stop, finished), name="coreai-profiler", daemon=True)
        try:
            worker.start()
        except RuntimeError as exc:
            logger.warning("Core AI profiler worker unavailable; inference continues with incomplete compute evidence: %s", exc)
            return await operation
        try:
            return await operation
        finally:
            stop.set()
            self._pending.set()
            await asyncio.shield(asyncio.wrap_future(finished))

    async def load(self, path: Path, preference: str) -> None:
        """Try GPU, with an explicit logged CPU-only fallback on load failure."""
        sdk = importlib.import_module("coreai.runtime")
        AIModel, ComputeUnitKind = sdk.AIModel, sdk.ComputeUnitKind
        Profiler, SpecializationOptions = sdk.Profiler, sdk.SpecializationOptions
        if not SpecializationOptions.is_supported():
            raise EmbeddingError(ErrorCode.UNAVAILABLE, "OS Core AI specialization is unavailable")

        self._path = path
        if preference == "gpu" and PinnedOutputs.exhausted():
            self.fallback_reason = PinnedOutputs.reason()
            logger.warning("%s; loading CPU-only specialization", self.fallback_reason)
            preference = "cpu"
        self.compute_preference = preference
        self._profiler = Profiler(
            on_log_event=self._observe,
            on_log_event_begin=lambda event: self._observe(event) or 0,
            on_log_event_end=lambda event, _interval: self._observe(event),
        )
        options = SpecializationOptions.cpu_only()
        if preference == "gpu":
            options = SpecializationOptions.from_preferred_compute_unit_kind(ComputeUnitKind.gpu())
        try:
            self._model = await self._await_native(AIModel.load(path, specialization_options=options))
            self._load_functions()
        except EmbeddingError:
            raise
        except RuntimeError as exc:
            if preference == "cpu":
                raise
            self.fallback_reason = str(exc)
            logger.warning("Core AI GPU load failed; using CPU-only specialization: %s", exc)
            self._model = await self._await_native(AIModel.load(path, specialization_options=SpecializationOptions.cpu_only()))
            self._load_functions()
            self.compute_preference = "cpu"
        if self.compute_preference == "cpu":
            self.compute_evidence.add("SpecializationOptions.cpu_only")

    def _load_functions(self) -> None:
        required = {f"seq{size}" for size in BUCKETS}
        if not required.issubset(self._model.function_names):
            raise EmbeddingError(ErrorCode.ASSET_CORRUPT, "Model lacks required bucket functions")
        functions = {size: self._model.load_function(f"seq{size}", profiler=self._profiler) for size in BUCKETS}
        # _infer calls these private members to release Core AI's retained results (iss_f3e65e52); a pin bump that
        # renames them must fail here, typed, never as an AttributeError on the first embedding.
        if not all(callable(getattr(function, "_function", None)) for function in functions.values()):
            raise _binding_unavailable("InferenceFunction._function")
        if not callable(getattr(importlib.import_module("coreai.runtime").NDArray, "_wrap", None)):
            raise _binding_unavailable("NDArray._wrap")
        self._functions = functions

    async def embed(self, encoded: EncodedInput) -> list[float]:
        """Validate shape, dtype, finiteness and norm before returning a vector."""
        NDArray = importlib.import_module("coreai.runtime").NDArray

        tensors = {
            "input_ids": NDArray(np.asarray([encoded.input_ids], dtype=np.int32)),
            "attention_mask": NDArray(np.asarray([encoded.attention_mask], dtype=np.int32)),
        }
        try:
            vector = await self._infer(encoded.bucket, tensors)
        except RuntimeError as exc:
            if self.compute_preference != "gpu" or self._path is None:
                raise
            reason = str(exc)
            logger.warning("Core AI GPU inference failed; retrying CPU-only: %s", reason)
            await self.load(self._path, "cpu")
            self.fallback_reason = reason
            self.compute_evidence.clear()
            self.compute_evidence.add("SpecializationOptions.cpu_only")
            vector = await self._infer(encoded.bucket, tensors)
        if vector.shape != (1, DIMENSION) or vector.dtype != np.float32:
            raise EmbeddingError(ErrorCode.INVALID_OUTPUT, "Expected float32 embedding [1,768]")
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(vector).all() or abs(norm - 1.0) > 0.01:
            raise EmbeddingError(ErrorCode.INVALID_OUTPUT, "Embedding is nonfinite or not normalized")
        if self.compute_preference == "cpu":
            self.observed_compute_unit = "cpu"
        return [float(value) for value in vector[0] / norm]

    async def _infer(self, bucket: int, tensors: dict[str, Any]) -> np.ndarray[Any, np.dtype[Any]]:
        """Copy the output out, then clear the result so Core AI's retained Future no longer holds it (iss_f3e65e52).

        Apple's ``InferenceFunction`` wrapper drops the raw result dict, which the binding keeps alive, so the raw
        function is called here: only the caller of the raw function can clear that dict.
        """
        on_gpu = self.compute_preference == "gpu"
        if on_gpu and PinnedOutputs.exhausted():
            raise EmbeddingError(ErrorCode.UNAVAILABLE, PinnedOutputs.reason())
        NDArray = importlib.import_module("coreai.runtime").NDArray
        try:
            inputs = {name: tensor._tensor for name, tensor in tensors.items()}
            result = await self._await_native(self._functions[bucket]._function(inputs=inputs, state={}))
        except (AttributeError, TypeError) as exc:
            raise _binding_unavailable(f"raw inference call: {exc}") from exc
        try:
            output = result["embedding"]
            vector = np.array(NDArray._wrap(output).numpy(), copy=True)
        finally:
            result.clear()
        if on_gpu and _held_elsewhere(output):
            PinnedOutputs.count += 1
            if PinnedOutputs.count == 1:
                logger.warning("Core AI kept a GPU output alive after its result was cleared; counting toward %d", PINNED_OUTPUT_BUDGET)
        return vector
