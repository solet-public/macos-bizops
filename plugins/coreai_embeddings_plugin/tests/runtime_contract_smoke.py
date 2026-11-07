"""Focused boundary/lifecycle tests; native numeric proof is a separate real test."""

import asyncio
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

_ROOT = Path(__file__).resolve().parents[3]
assert _ROOT.is_dir()
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))

from coreai_embeddings_plugin.contracts import DIMENSION, MODEL_ID, EmbeddingError, ErrorCode  # noqa: E402
from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin  # noqa: E402
from coreai_embeddings_plugin.runtime import EmbeddingRuntime  # noqa: E402
from coreai_embeddings_plugin.tokenization import NomicTokenizer, validate_inputs  # noqa: E402
from tokenizers import Tokenizer, models, pre_tokenizers, processors  # noqa: E402


class TokenBoundaryTests(unittest.TestCase):
    """An actual tokenizer fixture exercises special tokens and padding masks."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "tokenizer.json"
        tokenizer = Tokenizer(models.WordLevel({"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, "a": 4}, unk_token="[UNK]"))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        tokenizer.post_processor = processors.TemplateProcessing(
            single="[CLS] $A [SEP]",
            special_tokens=[("[CLS]", 2), ("[SEP]", 3)],
        )
        tokenizer.save(str(path))
        self.tokenizer = NomicTokenizer(path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_all_boundaries_include_special_tokens(self) -> None:
        for length, bucket in [(254, 256), (255, 512), (510, 512), (511, 1024), (1022, 1024), (1023, 2048), (2046, 2048)]:
            with self.subTest(length=length):
                encoded = self.tokenizer.encode(" ".join(["a"] * length))
                self.assertEqual(encoded.bucket, bucket)
                self.assertEqual(len(encoded.input_ids), bucket)
                self.assertEqual(sum(encoded.attention_mask), length + 2)
                self.assertEqual(encoded.input_ids[:2], [2, 4])
                self.assertEqual(encoded.input_ids[length + 1], 3)
                self.assertTrue(all(value == 0 for value in encoded.input_ids[length + 2 :]))
        with self.assertRaises(EmbeddingError) as caught:
            self.tokenizer.encode(" ".join(["a"] * 2047))
        self.assertEqual(caught.exception.code, ErrorCode.INPUT_TOO_LONG)

    def test_input_contract(self) -> None:
        for inputs in ([], "text", [None], [""], ["   "]):
            with self.subTest(inputs=inputs), self.assertRaises(EmbeddingError):
                validate_inputs(inputs, None, "text")
        with self.assertRaises(EmbeddingError):
            validate_inputs(["a"], "unknown", "text")
        with self.assertRaises(EmbeddingError):
            validate_inputs(["a"], None, "image")
        validate_inputs(["search_query: a"], MODEL_ID, "text")


class FakeRuntime:
    """Fault-injection fixture; never used for numeric evidence."""

    fail_prepare = False
    fail_generate = False

    def __init__(self, _root: Path, _preference: str) -> None:
        self.closed = False
        self.inputs: list[str] = []

    def prepare(self) -> None:
        if self.fail_prepare:
            raise EmbeddingError(ErrorCode.ASSET_MISSING, "asset absent")

    def generate(self, inputs: list[str]) -> list[list[float]]:
        if self.fail_generate:
            raise EmbeddingError(ErrorCode.INVALID_OUTPUT, "bad output")
        self.inputs = inputs
        return [[1.0] + [0.0] * (DIMENSION - 1) for _ in inputs]

    def close(self) -> None:
        self.closed = True

    def diagnostics(self) -> dict[str, object]:
        return {"observed_compute_unit": "fixture"}


class LifecycleTests(unittest.TestCase):
    """Missing, repaired, corrected, closed and reopened runtime transitions."""

    def test_missing_repair_and_reopen(self) -> None:
        plugin = CoreAIEmbeddingsPlugin({"asset_root": "/fixture"})
        with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", FakeRuntime):
            FakeRuntime.fail_prepare = True
            plugin.prepare_for_readiness()
            self.assertFalse(plugin.is_ready())
            self.assertEqual(plugin.generate_embeddings(["hello"])["error"]["code"], ErrorCode.ASSET_MISSING)
            self.assertEqual(plugin.get_runtime_status()["severity"], "warning")
            FakeRuntime.fail_prepare = False
            plugin.prepare_for_readiness()
            self.assertTrue(plugin.is_ready())
            self.assertEqual(plugin.generate_embeddings(["search_query: hello"])["action_status"], "completed")
            asyncio.run(plugin.cleanup())
            asyncio.run(plugin.cleanup())
            self.assertFalse(plugin.is_ready())
            plugin.prepare_for_readiness()
            self.assertTrue(plugin.is_ready())
            plugin.initialize({"asset_root": "relative"})
            self.assertFalse(plugin.is_ready())
            plugin.prepare_for_readiness()
            self.assertFalse(plugin.is_ready())

    def test_bad_output_revokes_readiness(self) -> None:
        plugin = CoreAIEmbeddingsPlugin({"asset_root": "/fixture"})
        with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", FakeRuntime):
            plugin.prepare_for_readiness()
            FakeRuntime.fail_generate = True
            try:
                self.assertEqual(plugin.generate_embeddings(["hello"])["action_status"], "error")
                self.assertFalse(plugin.is_ready())
            finally:
                FakeRuntime.fail_generate = False

    def test_metadata_is_not_readiness(self) -> None:
        plugin = CoreAIEmbeddingsPlugin()
        self.assertEqual(plugin.get_default_dimensions(), 768)
        self.assertEqual(plugin.get_embedding_dimension()["data"]["result"]["dimension"], 768)
        self.assertFalse(plugin.list_models()["data"]["result"]["models"][0]["available"])
        self.assertEqual(plugin.get_embedding_dimension("wrong")["action_status"], "error")
        self.assertEqual(plugin.generate_embeddings(["x"])["action_status"], "error")

    def test_sync_worker_inside_running_loop(self) -> None:
        runtime = EmbeddingRuntime(Path("/fixture"), "cpu")

        async def invoke() -> Any:
            return runtime._call(lambda: asyncio.run(asyncio.sleep(0, result=42)))

        try:
            self.assertEqual(asyncio.run(invoke()), 42)
        finally:
            runtime.close()
        with self.assertRaises(EmbeddingError):
            runtime.prepare()


class RuntimeSubmissionTests(unittest.TestCase):
    """Submission refusal retires the runtime without misclassifying operations."""

    def test_submit_refusal_is_typed(self) -> None:
        runtime = EmbeddingRuntime(Path("/fixture"), "cpu")
        self.assertFalse(runtime._closed)
        self.assertFalse(runtime._failed)
        runtime._executor.shutdown(wait=True)

        outcome: list[Exception | None] = []

        def call_count_tokens() -> None:
            try:
                runtime.count_tokens("text")
            except Exception as exc:
                outcome.append(exc)
            else:
                outcome.append(None)

        with patch.object(runtime._executor, "submit", wraps=runtime._executor.submit) as submit:
            with patch.object(runtime, "_count") as count:
                caller = threading.Thread(target=call_count_tokens, daemon=True)
                caller.start()
                caller.join(timeout=2)
                self.assertFalse(caller.is_alive(), "executor refusal did not return before the watchdog")
                self.assertEqual(len(outcome), 1)
                caught = outcome[0]
                self.assertIs(type(caught), EmbeddingError)
                assert isinstance(caught, EmbeddingError)
                self.assertEqual(caught.code, ErrorCode.UNAVAILABLE)
                self.assertIn("executor rejected operation submission", str(caught))
                self.assertIsInstance(caught.__cause__, RuntimeError)
                submit.assert_called_once()
                count.assert_not_called()
                self.assertTrue(runtime._failed)
                with self.assertRaises(EmbeddingError) as repeated:
                    runtime.count_tokens("text")
                self.assertEqual(repeated.exception.code, ErrorCode.UNAVAILABLE)
                self.assertEqual(submit.call_count, 1)
                with patch.object(runtime, "_close_native", wraps=runtime._close_native) as close_native:
                    close_error: Exception | None = None
                    try:
                        runtime.close()
                    except Exception as exc:
                        close_error = exc
                    runtime.close()
                    self.assertIsNone(close_error, "close must tolerate executor submission refusal")
                self.assertEqual(close_native.call_count, 0)
                self.assertEqual(submit.call_count, 2)
                self.assertTrue(runtime._closed)

    def test_submit_refusal_revokes_token_budget_readiness(self) -> None:
        class CountingTokenizer:
            def count(self, text: str) -> int:
                return len(text.split())

        runtime = EmbeddingRuntime(Path("/fixture"), "cpu")
        cast(Any, runtime)._tokenizer = CountingTokenizer()
        runtime._executor.shutdown(wait=True)
        plugin = CoreAIEmbeddingsPlugin({"asset_root": "/fixture"})
        plugin._runtime = runtime
        plugin.set_ready()
        budget = plugin.input_token_budget()

        prepared: list[EmbeddingRuntime] = []
        submit_spies: list[Mock] = []
        prepare_steps: list[tuple[Mock, Mock, Mock]] = []
        canary_check = Mock()

        def create_runtime(asset_root: Path, preference: str) -> EmbeddingRuntime:
            fresh_runtime = EmbeddingRuntime(asset_root, preference)
            cast(Any, fresh_runtime)._tokenizer = CountingTokenizer()
            submit_spy = Mock(wraps=fresh_runtime._executor.submit)
            cast(Any, fresh_runtime._executor).submit = submit_spy
            asset_check = Mock(return_value=object())
            model_load = Mock()
            cast(Any, fresh_runtime)._verified_asset = asset_check
            cast(Any, fresh_runtime)._load = model_load
            prepared.append(fresh_runtime)
            submit_spies.append(submit_spy)
            prepare_steps.append((asset_check, canary_check, model_load))
            return fresh_runtime

        with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", side_effect=create_runtime) as runtime_factory:
            with patch.object(runtime._executor, "submit", wraps=runtime._executor.submit) as submit:
                with patch.object(runtime, "_close_native", wraps=runtime._close_native) as close_native:
                    with self.assertRaises(EmbeddingError) as caught:
                        budget.count("text")
                    self.assertEqual(caught.exception.code, ErrorCode.UNAVAILABLE)
                    self.assertFalse(plugin.is_ready())
                    self.assertIsNone(plugin._runtime)
                    self.assertEqual(submit.call_count, 2)
                    self.assertEqual(close_native.call_count, 0)
                    with self.assertRaises(EmbeddingError) as repeated:
                        budget.count("text")
                    self.assertEqual(repeated.exception.code, ErrorCode.UNAVAILABLE)
                    self.assertEqual(submit.call_count, 2)
                    self.assertFalse(plugin.is_ready())
                    self.assertIsNone(plugin._runtime)
            runtime_factory.assert_not_called()

        with patch("coreai_embeddings_plugin.runtime.CacheCanary.ensure_healthy", canary_check):
            with patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", side_effect=create_runtime) as runtime_factory:
                plugin.prepare_for_readiness()
                self.assertEqual(len(prepared), 1)
                self.assertEqual(submit_spies[0].call_count, 1)
                asset_check, expected_canary_check, model_load = prepare_steps[0]
                asset_check.assert_called_once_with()
                expected_canary_check.assert_called_once_with()
                model_load.assert_called_once()
                self.assertTrue(plugin.is_ready())
                self.assertEqual(budget.count("one two"), 2)
                self.assertEqual(submit_spies[0].call_count, 2)
                asyncio.run(plugin.cleanup())
                runtime_factory.assert_called_once()

    def test_submitted_operation_runtime_error_is_preserved(self) -> None:
        runtime = EmbeddingRuntime(Path("/fixture"), "cpu")

        operation_error = RuntimeError("operation failed")

        def operation() -> int:
            raise operation_error

        self.assertEqual(runtime._call(lambda: 42), 42)
        with self.assertRaises(RuntimeError) as caught:
            runtime._call(operation)
        self.assertIs(type(caught.exception), RuntimeError)
        self.assertIs(caught.exception, operation_error)
        self.assertFalse(runtime._failed)
        runtime.close()
        runtime.close()


class _Call[T]:
    """Surface thread exceptions and bound every join in the calling test."""

    def __init__(self, operation: Callable[[], T], name: str) -> None:
        self.done = threading.Event()
        self.values: list[T] = []
        self.errors: list[BaseException] = []

        def run() -> None:
            try:
                self.values.append(operation())
            except BaseException as exc:
                self.errors.append(exc)
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, name=name, daemon=True)
        self.thread.start()

    def finish(self, case: unittest.TestCase) -> T:
        self.thread.join(timeout=2)
        case.assertFalse(self.thread.is_alive(), f"{self.thread.name} did not finish")
        if self.errors:
            raise self.errors[0]
        case.assertEqual(len(self.values), 1)
        return self.values[0]


class LeaseRuntime:
    """A serialized fake operation boundary with an explicit gap between items."""

    def __init__(self, root: Path, preference: str) -> None:
        self.root = root
        self.preference = preference
        self.operation_lock = threading.Lock()
        self.first_item = threading.Event()
        self.batch_release = threading.Event()
        self.count_entered = threading.Event()
        self.count_release = threading.Event()
        self.batch_release.set()
        self.count_release.set()
        self.close_count = 0
        self.inputs: list[str] = []
        self.generate_error: EmbeddingError | None = None
        self.count_error: EmbeddingError | None = None

    def prepare(self) -> None:
        pass

    def generate(self, inputs: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for index, text in enumerate(inputs):
            with self.operation_lock:
                assert self.close_count == 0, "generation used a closed runtime"
                self.inputs.append(text)
                vectors.append([float(index + 1)] + [0.0] * (DIMENSION - 1))
            if index == 0:
                self.first_item.set()
                assert self.batch_release.wait(5), "fixture batch barrier expired"
                if self.generate_error is not None:
                    raise self.generate_error
        return vectors

    def count_tokens(self, text: str) -> int:
        with self.operation_lock:
            assert self.close_count == 0, "count used a closed runtime"
            self.count_entered.set()
            assert self.count_release.wait(5), "fixture count barrier expired"
            if self.count_error is not None:
                raise self.count_error
            return len(text.split())

    def close(self) -> None:
        # Deliberately no runtime lock: only plugin lifecycle drain can protect the lease.
        self.close_count += 1


def _count_error(plugin: CoreAIEmbeddingsPlugin) -> EmbeddingError:
    try:
        plugin.input_token_budget().count("two words")
    except EmbeddingError as exc:
        return exc
    raise AssertionError("the armed count failure did not execute")


@contextmanager
def _transition_probe(
    plugin: CoreAIEmbeddingsPlugin,
    runtime: LeaseRuntime,
    admission_wait: threading.Event | None = None,
) -> Generator[threading.Event]:
    """Signal either a real drain wait or an early close, without scheduling sleeps."""
    progress = threading.Event()
    condition = plugin._condition
    original_wait = condition.wait
    original_close = runtime.close

    def wait(timeout: float | None = None) -> bool:
        if admission_wait is not None and threading.current_thread().name == "lease-later":
            admission_wait.set()
        progress.set()
        return original_wait(timeout)

    def close() -> None:
        original_close()
        progress.set()

    with patch.object(condition, "wait", wait), patch.object(runtime, "close", close):
        yield progress


class _LeaseTestCase(unittest.TestCase):
    """Each race has a fresh prepared fake and a tracked replacement factory."""

    def setUp(self) -> None:
        self.prepared: list[LeaseRuntime] = []

        def create(root: Path, preference: str) -> LeaseRuntime:
            runtime = LeaseRuntime(root, preference)
            self.prepared.append(runtime)
            return runtime

        self.factory = Mock(side_effect=create)
        patcher = patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plugin = CoreAIEmbeddingsPlugin({"asset_root": "/fixture"})
        self.plugin.prepare_for_readiness()
        self.runtime = self.prepared[0]


def _release(runtime: LeaseRuntime, *calls: _Call[Any] | None) -> None:
    """Always release both fixture barriers and give every caller a bounded join."""
    runtime.batch_release.set()
    runtime.count_release.set()
    for call in calls:
        if call is not None:
            call.thread.join(timeout=2)


class LeaseAdmissionTests(_LeaseTestCase):
    """Batch gaps admit counts; the runtime still owns serialized native operations."""

    def test_count_between_items(self) -> None:
        """Keep the batch parked outside its operation lock until a count has completed."""
        plugin = self.plugin
        runtime = self.runtime
        runtime.batch_release.clear()
        inputs = ["search_query: first", "clustering: second", " raw third "]
        batch = _Call(lambda: plugin.generate_embeddings(inputs), "lease-batch")
        count: _Call[int] | None = None
        try:
            self.assertTrue(runtime.first_item.wait(2), "batch did not reach its unlocked item gap")
            count = _Call(lambda: plugin.input_token_budget().count("two words"), "lease-count")
            self.assertTrue(count.done.wait(2), "count_done must precede batch_release")
            self.assertFalse(runtime.batch_release.is_set())
            self.assertEqual(count.finish(self), 2)
            self.assertEqual(runtime.inputs, inputs[:1])
        finally:
            _release(runtime, batch, count)
            asyncio.run(plugin.cleanup())
        outcome = batch.finish(self)
        self.assertEqual(outcome["action_status"], "completed", outcome["error"])
        expected = [[float(index + 1)] + [0.0] * (DIMENSION - 1) for index in range(len(inputs))]
        self.assertEqual(outcome["data"]["result"]["embeddings"], expected)
        self.assertEqual(runtime.inputs, inputs)

    def test_all_inputs_before_embed(self) -> None:
        """A last-input token failure must happen before the first native embedding."""
        runtime = EmbeddingRuntime(Path("/fixture"), "cpu")
        tokenizer = Mock()
        tokenizer.encode.side_effect = [object(), EmbeddingError(ErrorCode.INPUT_TOO_LONG, "last input too long")]
        cast(Any, runtime)._tokenizer = tokenizer
        try:
            with patch.object(runtime, "_embed") as embed, self.assertRaises(EmbeddingError) as raised:
                runtime.generate(["first", "last"])
            self.assertEqual(raised.exception.code, ErrorCode.INPUT_TOO_LONG)
            self.assertEqual(tokenizer.encode.call_count, 2)
            embed.assert_not_called()
        finally:
            runtime.close()

    def test_midbatch_has_no_partial_result(self) -> None:
        """A second-operation failure discards the first vector and never retries the batch."""
        runtime = EmbeddingRuntime(Path("/fixture"), "cpu")
        tokenizer = Mock()
        tokenizer.encode.side_effect = [object(), object(), object()]
        cast(Any, runtime)._tokenizer = tokenizer
        plugin = CoreAIEmbeddingsPlugin({"asset_root": "/fixture"})
        plugin._runtime = runtime
        plugin.set_ready()
        try:
            with patch.object(runtime, "_embed", side_effect=[[1.0], EmbeddingError(ErrorCode.INVALID_OUTPUT, "second")]) as embed:
                outcome = plugin.generate_embeddings(["first", "second", "third"])
            self.assertEqual(outcome["error"]["code"], ErrorCode.INVALID_OUTPUT)
            self.assertNotIn("result", outcome["data"] or {})
            self.assertEqual(tokenizer.encode.call_count, 3)
            self.assertEqual(embed.call_count, 2, "a failed batch is neither completed nor retried")
            self.assertFalse(plugin.is_ready())
        finally:
            asyncio.run(plugin.cleanup())


class LeaseLifecycleTests(_LeaseTestCase):
    """Transitions drain both kinds of lease and block new operation admission."""

    def test_cleanup_drains_batch(self) -> None:
        """Observe a drain wait or an early close before releasing the active batch."""
        plugin = self.plugin
        runtime = self.runtime
        runtime.batch_release.clear()
        batch = _Call(lambda: plugin.generate_embeddings(["first", "second"]), "lease-batch")
        cleanup: _Call[None] | None = None
        later: _Call[EmbeddingError] | None = None
        admission_wait = threading.Event()
        try:
            self.assertTrue(runtime.first_item.wait(2))
            with _transition_probe(plugin, runtime, admission_wait) as progress:
                cleanup = _Call(lambda: asyncio.run(plugin.cleanup()), "lease-cleanup")
                self.assertTrue(progress.wait(2), "cleanup neither drained nor closed")
                self.assertEqual(runtime.close_count, 0, "close must follow batch lease release")
                self.assertFalse(cleanup.done.is_set())
                later = _Call(lambda: _count_error(plugin), "lease-later")
                self.assertTrue(admission_wait.wait(2), "new admission must wait behind cleanup")
                runtime.batch_release.set()
                self.assertEqual(batch.finish(self)["action_status"], "completed")
                cleanup.finish(self)
                self.assertEqual(str(later.finish(self)), "Plugin closed")
            self.assertEqual(runtime.close_count, 1)
            asyncio.run(plugin.cleanup())
            plugin.prepare_for_readiness()
            self.assertTrue(plugin.is_ready())
            self.assertIsNot(plugin._runtime, runtime)
        finally:
            _release(runtime, batch, cleanup, later)

    def test_reload_drains_count(self) -> None:
        """A queued count must observe the corrected runtime after the active count drains."""
        plugin = self.plugin
        runtime = self.runtime
        runtime.count_release.clear()
        count = _Call(lambda: plugin.input_token_budget().count("two words"), "lease-count")
        reload_call: _Call[None] | None = None
        later: _Call[int] | None = None
        admission_wait = threading.Event()
        try:
            self.assertTrue(runtime.count_entered.wait(2))
            with _transition_probe(plugin, runtime, admission_wait) as progress:
                reload_call = _Call(lambda: plugin.initialize({"asset_root": "/corrected", "compute_preference": "gpu"}), "lease-reload")
                self.assertTrue(progress.wait(2), "reload neither drained nor closed")
                self.assertEqual(runtime.close_count, 0, "close must follow count lease release")
                later = _Call(lambda: plugin.input_token_budget().count("post transition count"), "lease-later")
                self.assertTrue(admission_wait.wait(2), "new admission must wait behind reload")
                runtime.count_release.set()
                self.assertEqual(count.finish(self), 2)
                reload_call.finish(self)
                self.assertEqual(later.finish(self), 3)
            successor = cast(LeaseRuntime, cast(object, plugin._runtime))
            self.assertEqual(successor.root, Path("/corrected"))
            self.assertEqual(successor.preference, "gpu")
            self.assertTrue(successor.count_entered.is_set())
            self.assertEqual(runtime.close_count, 1)
        finally:
            _release(runtime, count, reload_call, later)

    def test_exception_release(self) -> None:
        """A non-recovering exception must leave no lease that can strand later cleanup."""
        plugin = self.plugin
        runtime = self.runtime
        runtime.generate_error = EmbeddingError(ErrorCode.INPUT_TOO_LONG, "lease fails without recovery")
        outcome = plugin.generate_embeddings(["first"])
        self.assertEqual(outcome["error"]["code"], ErrorCode.INPUT_TOO_LONG)
        self.assertEqual(runtime.inputs, ["first"], "the exception must originate inside the lease")
        cleanup = _Call(lambda: asyncio.run(plugin.cleanup()), "lease-cleanup")
        self.assertTrue(cleanup.done.wait(2), "cleanup_done requires finally release on exception")
        cleanup.finish(self)
        self.assertEqual(runtime.close_count, 1)


class LeaseFailureTests(_LeaseTestCase):
    """Failure recovery runs after lease release and applies only to the current runtime."""

    def test_timeout_release(self) -> None:
        """A reached timeout must finish recovery without waiting for its own lease."""
        plugin = self.plugin
        runtime = self.runtime
        factory = self.factory
        runtime.generate_error = EmbeddingError(ErrorCode.TIMEOUT, "armed timeout")
        recovery = _Call(lambda: plugin.generate_embeddings(["first"]), "lease-timeout")
        self.assertTrue(runtime.first_item.wait(2), "timeout path was not reached")
        self.assertTrue(recovery.done.wait(2), "recovery_done requires release before recovery")
        self.assertEqual(recovery.finish(self)["error"]["code"], ErrorCode.TIMEOUT)
        self.assertEqual(factory.call_count, 2)
        self.assertIsNot(plugin._runtime, runtime)
        self.assertTrue(plugin.is_ready())

    def test_failure_pair(self) -> None:
        """Both leases reach one runtime before failing, so only one replacement is valid."""
        plugin = self.plugin
        runtime = self.runtime
        factory = self.factory
        runtime.batch_release.clear()
        runtime.count_release.clear()
        error = EmbeddingError(ErrorCode.TIMEOUT, "two calls fail on the same runtime")
        runtime.generate_error = error
        runtime.count_error = error
        batch = _Call(lambda: plugin.generate_embeddings(["first"]), "lease-batch")
        count: _Call[EmbeddingError] | None = None
        try:
            self.assertTrue(runtime.first_item.wait(2))
            count = _Call(lambda: _count_error(plugin), "lease-count")
            self.assertTrue(runtime.count_entered.wait(2), "both leases must reach the failing runtime")
            runtime.batch_release.set()
            runtime.count_release.set()
            self.assertEqual(batch.finish(self)["error"]["code"], ErrorCode.TIMEOUT)
            self.assertIs(count.finish(self), error)
            self.assertEqual(factory.call_count, 2, "one runtime permits only one replacement preparation")
            self.assertTrue(plugin.is_ready())
            self.assertEqual(runtime.close_count, 1)
        finally:
            _release(runtime, batch, count)

    def test_stale_failure(self) -> None:
        """Delay failure handling after lease release until a healthy successor is installed."""
        plugin = self.plugin
        runtime = self.runtime
        error = EmbeddingError(ErrorCode.UNAVAILABLE, "late retired runtime failure")
        runtime.generate_error = error
        failure_entered = threading.Event()
        failure_release = threading.Event()
        handle_failure = plugin._handle_failure

        def delayed_failure(failed: EmbeddingRuntime, failure: EmbeddingError) -> None:
            failure_entered.set()
            assert failure_release.wait(5), "fixture failure barrier expired"
            handle_failure(failed, failure)

        with patch.object(plugin, "_handle_failure", delayed_failure):
            batch = _Call(lambda: plugin.generate_embeddings(["first"]), "lease-stale")
            try:
                self.assertTrue(failure_entered.wait(2), "failure did not release its lease")
                plugin.prepare_for_readiness()
                successor = cast(LeaseRuntime, cast(object, plugin._runtime))
                self.assertIsNot(successor, runtime)
                failure_release.set()
                self.assertEqual(batch.finish(self)["error"]["code"], error.code)
                self.assertIs(plugin._runtime, successor, "stale failure must preserve successor identity")
                self.assertEqual(successor.close_count, 0, "stale failure must not close the successor")
                self.assertTrue(plugin.is_ready())
            finally:
                failure_release.set()
                _release(runtime, batch)

    def test_failure_cleanup(self) -> None:
        """A timeout reported after external cleanup must preserve the closed state."""
        plugin = self.plugin
        runtime = self.runtime
        factory = self.factory
        runtime.generate_error = EmbeddingError(ErrorCode.TIMEOUT, "late failure after cleanup")
        failure_entered = threading.Event()
        failure_release = threading.Event()
        handle_failure = plugin._handle_failure

        def delayed_failure(failed: EmbeddingRuntime, failure: EmbeddingError) -> None:
            failure_entered.set()
            assert failure_release.wait(5), "fixture failure barrier expired"
            handle_failure(failed, failure)

        with patch.object(plugin, "_handle_failure", delayed_failure):
            batch = _Call(lambda: plugin.generate_embeddings(["first"]), "lease-cleanup-failure")
            try:
                self.assertTrue(failure_entered.wait(2), "failure did not release its lease")
                asyncio.run(plugin.cleanup())
                failure_release.set()
                self.assertEqual(batch.finish(self)["error"]["code"], ErrorCode.TIMEOUT)
                self.assertIsNone(plugin._runtime, "late timeout must not reverse cleanup")
                self.assertFalse(plugin.is_ready())
                self.assertEqual(factory.call_count, 1)
            finally:
                failure_release.set()
                _release(runtime, batch)

    def test_failure_reload(self) -> None:
        """Hold a count lease until both reload and failure handling are observed waiting."""
        plugin = self.plugin
        runtime = self.runtime
        prepared = self.prepared
        runtime.batch_release.clear()
        runtime.count_release.clear()
        runtime.generate_error = EmbeddingError(ErrorCode.TIMEOUT, "failure during external reload")
        waiting = {name: threading.Event() for name in ("lease-reload", "lease-failure")}
        original_wait = plugin._condition.wait

        def wait(timeout: float | None = None) -> bool:
            event = waiting.get(threading.current_thread().name)
            if event is not None:
                event.set()
            return original_wait(timeout)

        batch = _Call(lambda: plugin.generate_embeddings(["first"]), "lease-failure")
        count: _Call[int] | None = None
        reload_call: _Call[None] | None = None
        try:
            self.assertTrue(runtime.first_item.wait(2))
            count = _Call(lambda: plugin.input_token_budget().count("two words"), "lease-count")
            self.assertTrue(runtime.count_entered.wait(2))
            with patch.object(plugin._condition, "wait", wait):
                reload_call = _Call(lambda: plugin.initialize({"asset_root": "/corrected"}), "lease-reload")
                self.assertTrue(waiting["lease-reload"].wait(2), "reload did not begin its drain")
                runtime.batch_release.set()
                self.assertTrue(waiting["lease-failure"].wait(2), "failure did not wait behind reload")
                runtime.count_release.set()
                self.assertEqual(count.finish(self), 2)
                reload_call.finish(self)
                successor = prepared[1]
                self.assertEqual(successor.root, Path("/corrected"))
                self.assertEqual(batch.finish(self)["error"]["code"], ErrorCode.TIMEOUT)
            self.assertIs(plugin._runtime, successor, "late failure must preserve the external successor")
            self.assertEqual(len(prepared), 2, "late failure must not prepare another runtime")
            self.assertEqual(successor.close_count, 0)
            self.assertTrue(plugin.is_ready())
        finally:
            _release(runtime, batch, count)
            if reload_call is not None:
                reload_call.thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
