#!/usr/bin/env python3
"""The roster probe waits, bounded, only while the inference plugin is still starting (prb_3207f577).

Rows come from the real producer (``LifecycleManagementService.list_plugins`` over ``PluginBase``
readiness), so the error text the doctor matches is the producer's, not a hand-written copy. Time
is a virtual clock injected through ``monotonic=`` and ``sleep=``: nothing here really sleeps.

Run::

    .venv/bin/python3 plugins/github_midwife_plugin/tests/plugin_roster_transient_wait_smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "github_midwife_plugin" / "src"))

from ananta.core.plugins.plugin_base import PluginBase  # noqa: E402
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)
from github_midwife_plugin import installation_plugin_doctor as doctor  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject, JsonValue  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402

INFERENCE = "default_inference_plugin"
EMBEDDINGS = "coreai_embeddings_plugin"
STARTING = "Inference server not available at http://127.0.0.1:18180/v1 after router registration"
PREWARM = "LM Studio pre-warm failed after becoming available: grammar compile timed out"
NONFINITE = "Embedding is nonfinite or not normalized"
PRODUCER = ROOT / "plugins" / "default_inference_plugin" / "src" / "default_inference_plugin" / "plugin.py"


class _Fixture(PluginBase):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name


def _output(inference: str | None, embeddings: str | None = None) -> CommandOutcome:
    """One ``list_plugins`` answer; a ``None`` error means that plugin is ready."""
    plugins = (_Fixture(INFERENCE), _Fixture(EMBEDDINGS))
    for plugin, error in zip(plugins, (inference, embeddings), strict=True):
        plugin.set_ready() if error is None else plugin.set_error(error)
    orchestrator = SimpleNamespace(
        plugin_manager=SimpleNamespace(plugins={plugin.name: plugin for plugin in plugins}),
        config=SimpleNamespace(get_plugin_config=lambda _name: {"priority": 100, "enabled": True}),
    )
    rows = LifecycleManagementService(orchestrator).list_plugins()["data"]["plugins"]
    body = {"result": {"success": True, "error": None, "data": {"plugins": rows}}}
    return CommandOutcome(0, False, 1, json.dumps(body), "")


class _Runtime:
    """Serves the scripted ``list_plugins`` answers in order, then repeats the last one."""

    def __init__(self, *answers: CommandOutcome) -> None:
        self.home = Path("/fixture/home")
        self.answers = list(answers)
        self.calls = 0

    def run(self, argv: tuple[str, ...], **_ignored: object) -> CommandOutcome:
        assert argv[1:3] == ("call", "service_interface::lifecycle_management_service::list_plugins"), argv
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def http_json(self, url: str, **_ignored: object) -> tuple[int, JsonValue]:
        raise AssertionError(f"unexpected HTTP call {url}")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _roster(runtime: _Runtime, timeout_seconds: int) -> tuple[JsonObject, _Clock]:
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw)
        manifest = target / "profile" / "config" / "manifest.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(f"profile_name: fixture\nplugins:\n- {INFERENCE}\n- {EMBEDDINGS}\n", encoding="utf-8")
        request = AdapterRequest(
            request_id="8f2f3ed3-03fc-4f58-915e-eb400a172a67",
            operation_id="plugin_roster_matches_plan",
            operation_ref="service_interface::lifecycle_management_service.list_plugins",
            phase="probe",
            probe_purpose="completion",
            attempt=1,
            name="iris",
            target=target,
            flow_source_revision="a" * 40,
            answers_fingerprint="sha256:" + "b" * 64,
            approval_fingerprint=None,
            dry_run=True,
            timeout_seconds=timeout_seconds,
            public_inputs={},
        )
        clock = _Clock()
        return doctor.plugin_roster(request, runtime, monotonic=clock.monotonic, sleep=clock.sleep), clock  # type: ignore[arg-type]


def _items(result: JsonObject) -> list[dict[str, object]]:
    """Evidence items without their wall-clock ``captured_at``, which differs between any two runs."""
    return [{k: v for k, v in item.items() if k != "captured_at"} for item in result["evidence"] if isinstance(item, dict)]  # type: ignore[union-attr]


def _evidence(result: JsonObject, evidence_id: str) -> object:
    items = [item for item in _items(result) if item["id"] == evidence_id]
    return items[0]["observed"] if len(items) == 1 else None


class RosterWaitsForTheInferencePlugin(unittest.TestCase):
    def test_t1_unready_twice_then_ready_is_verified_after_three_calls(self) -> None:
        runtime = _Runtime(_output(STARTING), _output(STARTING), _output(None))
        result, clock = _roster(runtime, 390)
        self.assertEqual(result["checkpoint_status"], "verified")
        self.assertIsNone(result["error_kind"])
        self.assertEqual(runtime.calls, 3)
        self.assertEqual(clock.sleeps, [5.0, 5.0])

    def test_t2_unready_forever_blocks_unchanged_within_the_budget(self) -> None:
        runtime = _Runtime(_output(STARTING))
        result, clock = _roster(runtime, 390)
        single, _ = _roster(_Runtime(_output(STARTING)), 30)
        self.assertEqual(result["checkpoint_status"], "blocked")
        self.assertEqual(result["error_kind"], "plugin_roster_unready")
        self.assertEqual(_evidence(result, "plugin_roster_unready"), [f"{INFERENCE}: error: {STARTING}"])
        self.assertEqual(_items(result), _items(single))
        self.assertEqual(result["repair"], single["repair"])
        self.assertTrue(295 <= sum(clock.sleeps) <= 300, sum(clock.sleeps))
        self.assertGreaterEqual(runtime.calls, 2)

    def test_t3_everything_else_is_judged_at_once(self) -> None:
        cases = {
            "an unrelated plugin is unready": (_output(None, NONFINITE), 390),
            "the inference plugin has another error": (_output(PREWARM), 390),
            "the starting row plus an unrelated unready plugin": (_output(STARTING, NONFINITE), 390),
            "the starting row with the default 30 s budget": (_output(STARTING), 30),
        }
        for label, (answer, timeout) in cases.items():
            with self.subTest(label):
                runtime = _Runtime(answer)
                result, clock = _roster(runtime, timeout)
                self.assertEqual(result["error_kind"], "plugin_roster_unready")
                self.assertEqual((runtime.calls, clock.sleeps), (1, []))

    def test_t4_the_producer_still_says_what_the_doctor_matches(self) -> None:
        if not PRODUCER.is_file():
            self.skipTest("default_inference_plugin is not in this tree (a seed without it), so there is no producer message to pin")
        text = PRODUCER.read_text(encoding="utf-8")
        self.assertIn(f'self.name = "{doctor._TRANSIENT_PLUGIN}"', text)  # pyright: ignore[reportPrivateUsage]
        self.assertIn(f"not available at {{endpoint}} {doctor._TRANSIENT_ERROR}", text)  # pyright: ignore[reportPrivateUsage]


if __name__ == "__main__":
    unittest.main(verbosity=2)
