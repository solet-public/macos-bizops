"""Nomic v1.5 synchronous embedding service for macOS 27."""

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import Path
from typing import Any, cast

from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface, TokenBudget

from .contracts import BUCKETS, DIMENSION, MODEL_ID, EmbeddingError, ErrorCode, failure, result
from .runtime import EmbeddingRuntime
from .tokenization import validate_inputs

logger = logging.getLogger(__name__)


# A single running operation and FIFO async turn reservations preserve ordering.
# Waiting callers never occupy an executor worker or its submission queue.
# The process owns this pool across plugin stop/restart; Python retires it at exit.
_F5_WORKERS = 1
_F5_PENDING_BOUND = 1
_F5_EXECUTOR = ThreadPoolExecutor(max_workers=_F5_WORKERS, thread_name_prefix="coreai_embeddings-f5")
_F5_SUBMISSION_LOCK = threading.RLock()
_f5_in_flight: Future[object] | None = None
_f5_turn: Future[None] | None = None
_f5_waiters: deque[Future[None]] = deque()
logging.getLogger(__name__).info("F5 executor coreai_embeddings-f5: workers=%s pending=%s", _F5_WORKERS, _F5_PENDING_BOUND)


def _release_f5_turn(turn: Future[None]) -> None:
    global _f5_turn
    with _F5_SUBMISSION_LOCK:
        if _f5_turn is not turn:
            return
        if _f5_waiters:
            next_turn = _f5_waiters.popleft()
            _f5_turn = next_turn
            next_turn.set_result(None)
        else:
            _f5_turn = None


def _cancel_f5_waiter(turn: Future[None]) -> None:
    with _F5_SUBMISSION_LOCK:
        if _f5_turn is turn:
            _release_f5_turn(turn)
        elif turn in _f5_waiters:
            _f5_waiters.remove(turn)
            turn.cancel()


def _complete_f5_operation(turn: Future[None], done: Future[object]) -> None:
    global _f5_in_flight
    with _F5_SUBMISSION_LOCK:
        if _f5_in_flight is done:
            _f5_in_flight = None
        _release_f5_turn(turn)


def _report_f5_late_failure(done: Future[object]) -> None:
    error = done.exception()
    if error is not None:
        logging.getLogger(__name__).error("F5 operation failed after its waiter cancelled: %s", error)


async def _run_f5_blocking[T](operation: Callable[[], T]) -> T:
    """Reserve FIFO order before waiting; cancellation retains active ownership."""
    global _f5_in_flight, _f5_turn
    turn = Future[None]()
    with _F5_SUBMISSION_LOCK:
        if _f5_turn is None:
            _f5_turn = turn
            turn.set_result(None)
        else:
            _f5_waiters.append(turn)
    try:
        await asyncio.shield(asyncio.gather(asyncio.wrap_future(turn), return_exceptions=True))
    except asyncio.CancelledError:
        _cancel_f5_waiter(turn)
        raise
    try:
        with _F5_SUBMISSION_LOCK:
            future = _F5_EXECUTOR.submit(copy_context().run, operation)
            _f5_in_flight = cast(Future[object], future)
    except BaseException:
        _release_f5_turn(turn)
        raise
    future.add_done_callback(lambda done: _complete_f5_operation(turn, cast(Future[object], done)))
    try:
        await asyncio.shield(asyncio.gather(asyncio.wrap_future(future), return_exceptions=True))
    except asyncio.CancelledError:
        future.add_done_callback(lambda done: _report_f5_late_failure(cast(Future[object], done)))
        raise
    return future.result()


class CoreAIEmbeddingsPlugin(PluginBase, EmbeddingServiceInterface):
    """Keep model availability truthful and permit explicit preparation after repair."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.name = "coreai_embeddings_plugin"
        self.config: dict[str, Any] = dict(config or {})
        # Startup Phase 1 prepares a bare-constructed plugin before initialize() delivers its config (iss_9c150bba).
        self._config_delivered = config is not None
        self._runtime: EmbeddingRuntime | None = None
        self._runtime_timeout: EmbeddingError | None = None
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._active_calls = 0
        self._pending_transitions = 0
        self._transition_owner: int | None = None
        self._last_error = EmbeddingError(ErrorCode.UNAVAILABLE, "Plugin not prepared")
        self.set_error(str(self._last_error))

    @property
    def service_interfaces(self) -> tuple[type, ...]:
        return (EmbeddingServiceInterface,)

    @property
    def supported_interface_versions(self) -> dict[type, str]:
        return {EmbeddingServiceInterface: EmbeddingServiceInterface.INTERFACE_VERSION}

    def initialize(self, config: dict[str, object]) -> None:
        """Adopt new configuration and re-prepare with it immediately.

        Fresh boot runs ``prepare_for_readiness()`` (platform Phase 1) before
        this method ever sees the real config, and this plugin is not
        ``LifecycleManaged`` so nothing downstream re-invokes
        ``prepare_for_readiness()`` on its behalf afterward — the same is
        true of ``reload_plugin_config``, which also calls only this method.
        Re-preparing here, rather than only invalidating, is what lets the
        plugin actually reach ready on the config this method received.
        """
        with self._transition():
            self.config = dict(config)
            self._config_delivered = True
            self.prepare_for_readiness()

    def prepare_for_readiness(self) -> None:
        """Missing assets leave an honest warning and a retryable preparation path.

        Before any config is delivered there is nothing to validate: preparation waits for initialize(), so the
        configuration warning only ever describes a config the plugin was actually given (iss_9c150bba).
        """
        with self._transition():
            self._close()
            if not self._config_delivered:
                self._last_error = EmbeddingError(ErrorCode.UNAVAILABLE, "Plugin configuration not yet delivered")
                self.set_error(str(self._last_error))
                logger.debug("Core AI embedding preparation waits for initialize() to deliver configuration")
                return
            try:
                root, preference = self._settings()
                self._runtime = EmbeddingRuntime(root, preference)
                self._runtime.prepare()
            except EmbeddingError as exc:
                self._unavailable(exc)
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                self._unavailable(EmbeddingError(ErrorCode.UNAVAILABLE, str(exc)))
            else:
                self.set_ready()

    def _settings(self) -> tuple[Path, str]:
        root = self.config.get("asset_root")
        preference = self.config.get("compute_preference", "cpu")
        if not isinstance(root, str) or not root.strip() or not Path(root).is_absolute():
            raise EmbeddingError(ErrorCode.UNAVAILABLE, "asset_root must be an absolute directory path")
        if preference not in ("gpu", "cpu"):
            raise EmbeddingError(ErrorCode.UNAVAILABLE, "compute_preference must be gpu or cpu")
        return Path(root), preference

    def _unavailable(self, error: EmbeddingError) -> None:
        self._last_error = error
        self.set_error(str(error))
        self._close()
        logger.warning("Core AI embedding capability unavailable: %s. Repair assets/configuration and prepare again.", error)

    def _close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
        self._runtime_timeout = None

    @contextmanager
    def _transition(self) -> Generator[None]:
        """Reserve lifecycle admission before draining; initialize may reenter preparation."""
        owner = threading.get_ident()
        with self._condition:
            if self._transition_owner == owner:
                yield
                return
            self._pending_transitions += 1
            try:
                while self._transition_owner is not None:
                    self._condition.wait()
                self._transition_owner = owner
                try:
                    while self._active_calls:
                        self._condition.wait()
                    yield
                finally:
                    self._transition_owner = None
            finally:
                self._pending_transitions -= 1
                self._condition.notify_all()

    @contextmanager
    def _runtime_lease(self) -> Generator[EmbeddingRuntime]:
        """Snapshot one ready runtime without holding the plugin lock across execution."""
        with self._condition:
            while self._pending_transitions:
                self._condition.wait()
            if not self.is_ready() or self._runtime is None:
                raise self._last_error
            runtime = self._runtime
            self._active_calls += 1
        timeout: EmbeddingError | None = None
        try:
            yield runtime
        except EmbeddingError as error:
            if error.code is ErrorCode.TIMEOUT:
                timeout = error
            raise
        finally:
            with self._condition:
                # Publish timeout intent before the last lease wakes a competing failure transition.
                if timeout is not None:
                    self._runtime_timeout = timeout
                self._active_calls -= 1
                self._condition.notify_all()

    def _handle_failure(self, runtime: EmbeddingRuntime, error: EmbeddingError) -> None:
        """Retire only the failing current runtime, after releasing the caller's lease."""
        with self._condition:
            if self._runtime is not runtime:
                return
            with self._transition():
                if self._runtime is not runtime:
                    return
                if self._runtime_timeout is not None:
                    error = self._runtime_timeout
                if error.code is ErrorCode.TIMEOUT:
                    self._recover_from_timeout(error)
                else:
                    self._unavailable(error)

    def generate_embeddings(
        self,
        inputs: list[str],
        model: str | None = None,
        input_type: str = "text",
    ) -> ActionResult:
        """Preserve caller text and prefixes, and return no partial batch on error."""
        runtime: EmbeddingRuntime | None = None
        try:
            validate_inputs(inputs, model, input_type)
            if len(inputs) > 128:
                raise EmbeddingError(ErrorCode.INVALID_INPUT, "Maximum batch size is 128")
            with self._runtime_lease() as runtime:
                vectors = runtime.generate(inputs)
            return result({"embeddings": vectors, "dimension": DIMENSION, "model": MODEL_ID})
        except EmbeddingError as exc:
            # A stored error raised before admission has no runtime snapshot and must not start recovery.
            if runtime is not None and exc.code in (ErrorCode.TIMEOUT, ErrorCode.UNAVAILABLE, ErrorCode.INVALID_OUTPUT):
                self._handle_failure(runtime, exc)
            return failure(exc)
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            error = EmbeddingError(ErrorCode.INFERENCE_FAILED, str(exc))
            if runtime is not None:
                self._handle_failure(runtime, error)
            return failure(error)

    def input_token_budget(self) -> TokenBudget:
        """The model's largest bucket is the hard ceiling; inputs over it are refused, never truncated."""
        return TokenBudget(BUCKETS[-1], self._count_tokens)

    def _count_tokens(self, text: str) -> int:
        runtime: EmbeddingRuntime | None = None
        try:
            with self._runtime_lease() as runtime:
                return runtime.count_tokens(text)
        except EmbeddingError as exc:
            if runtime is not None and exc.code in (ErrorCode.TIMEOUT, ErrorCode.UNAVAILABLE):
                self._handle_failure(runtime, exc)
            raise

    def _recover_from_timeout(self, error: EmbeddingError) -> None:
        """Replace a runtime whose native call timed out; the failed call is reported, never retried (iss_a457a147).

        The timed-out call cannot be interrupted and still holds its worker, so that runtime is abandoned and a new one
        is prepared once. A failed re-preparation, a timed-out one included, leaves the plugin unavailable with its own
        error, which later calls report without preparing again, until a reload or an explicit preparation.
        """
        logger.warning("Core AI native call timed out (%s); preparing a new runtime once", error)
        self.prepare_for_readiness()
        if self.is_ready():
            logger.warning("Core AI runtime prepared again after a timed-out native call")

    def get_default_dimensions(self) -> int:
        """Static schema-init metadata does not assert runtime availability."""
        return DIMENSION

    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        if model is not None and model != MODEL_ID:
            return failure(EmbeddingError(ErrorCode.UNSUPPORTED_MODEL, f"Unsupported model: {model}"))
        return result({"dimension": DIMENSION, "model": MODEL_ID})

    def list_models(self) -> ActionResult:
        return result(
            {
                "models": [
                    {
                        "name": MODEL_ID,
                        "dimension": DIMENSION,
                        "max_input_length": BUCKETS[-1],
                        "input_types": ["text"],
                        "description": "Pinned Nomic v1.5, normalized vectors",
                        "available": self.is_ready(),
                    }
                ]
            }
        )

    def get_runtime_status(self) -> dict[str, object]:
        """Installer/doctor can distinguish capability absence from readiness."""
        with self._lock:
            details = self._runtime.diagnostics() if self._runtime else {}
            return {
                "ready": self.is_ready(),
                "reason": self.get_readiness_error(),
                "severity": "ok" if self.is_ready() else "warning",
                "repair": "Verify/install pinned assets at asset_root, then prepare the plugin again",
                **details,
            }

    async def cleanup(self) -> None:
        """Retire the runtime; preparation may reopen it after cleanup."""
        return await _run_f5_blocking(self._f5_cleanup)

    def _f5_cleanup(self) -> None:
        """Retire the runtime; preparation may reopen it after cleanup."""
        with self._transition():
            self._close()
            self._last_error = EmbeddingError(ErrorCode.UNAVAILABLE, "Plugin closed")
            self.set_error(str(self._last_error))

    def get_config_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["asset_root"],
            "properties": {
                "asset_root": {"type": "string", "description": "Absolute installed asset root"},
                "compute_preference": {"type": "string", "enum": ["gpu", "cpu"], "default": "cpu"},
            },
        }
