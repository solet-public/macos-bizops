"""A fresh setup chooses its inference route from the measured Apple model, not the macOS version alone (prb_66bfd36b, rul_becb4bed).

Drives the shipped create-flow contract through ``host_bound`` with the host
measurement replaced by a fixed reading:

* T1: a macOS 27 arm64 Mac whose Apple model is unavailable is offered llama.cpp
  and not Apple Foundation Models, exactly as a Mac below macOS 27 is, and the
  withheld option says it is the model, not the macOS version;
* T2: an available model keeps the Apple route;
* T3: an unmeasurable model withholds neither Apple option: every inference
  option is offered, none is preselected, one advisory says so, and nothing
  refuses (rul_becb4bed); a recorded llama.cpp answer is accepted;
* T4 and T5: the reader turns each answer of the bounded Swift probe into that
  fact, a probe that cannot run gives unknown, and it is read only where the
  model can exist; T4d bounds the Swift probe at 60 seconds;
* T6: only the ``apple_fm`` profile reads the model fact.

Offline: no host command runs.  Run from the repository root with
``.venv/bin/python3 solet_cli/tests/host_model_availability_route_smoke.py``.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import host_option_binding, host_platform  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.errors import ContractError  # noqa: E402
from solet_manager.host_platform import HostPlatform, ModelAvailability  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.plan_builder import SetupPlan, build_setup_plan  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402

_KB = Path(__file__).resolve().parents[2] / "plugins" / "github_midwife_plugin" / "knowledge_base"
_REVISION = "a" * 40
_LOGGER = "solet_manager.host_option_binding"
_INFERENCE = "inference_implementation"
_NOT_ELIGIBLE = ModelAvailability(False, "DEVICE_NOT_ELIGIBLE")
_AVAILABLE = ModelAvailability(True, "AVAILABLE")


def _bound(bundle: ContractBundle, host: HostPlatform) -> ContractBundle:
    return host_option_binding.host_bound(bundle, {"setup_profile": "macos-bizops"}, {}, read=lambda: host)


def _unknown_plan(bundle: ContractBundle, host: HostPlatform, selections: dict[str, JsonValue] | None = None) -> SetupPlan:
    """The real plan builder on ``host``, as the wizard smoke drives it."""
    with patch.object(host_option_binding, "read_host_platform", lambda: host):
        return build_setup_plan(
            bundle=bundle,
            config=CreateConfig(name="unknown-model", target=Path("/tmp/unknown-model"), autostart=True),
            seed=SeedLock(
                repository="example/seed",
                release_tag="v1.0.0",
                commit=_REVISION,
                tree_hash="b" * 40,
                archive_sha256="c" * 64,
                profile="macos-bizops",
            ),
            journal_path=Path("/tmp/unknown-model.json"),
            prospective_consents=True,
            decision_selections=selections,
            operation_stage_ids={"genesis"},
        )


def _decision_options(bound: ContractBundle, decision_id: str) -> dict[str, dict[str, JsonValue]]:
    source = cast(dict[str, JsonValue], bound.decisions[decision_id]["option_source"])
    return cast(dict[str, dict[str, JsonValue]], source["options"])


def _options(bundle: ContractBundle, host: HostPlatform, decision_id: str) -> dict[str, dict[str, JsonValue]]:
    return _decision_options(_bound(bundle, host), decision_id)


def _supported(options: dict[str, dict[str, JsonValue]]) -> set[str]:
    return {option_id for option_id, option in options.items() if option.get("availability") == "supported"}


def _read(
    swift: tuple[int, str] | Exception,
    *,
    version: str = "27.0",
    machine: str = "arm64",
    xcode_select_code: int = 0,
    sw_vers_code: int = 0,
) -> tuple[HostPlatform, list[str]]:
    """``read_host_platform`` with every host command faked; also the commands it ran, in order."""
    answers: dict[str, tuple[int, str] | Exception] = {
        "/usr/bin/sw_vers": (sw_vers_code, version),
        "/usr/bin/uname": (0, machine),
        "/usr/bin/xcode-select": (xcode_select_code, "/Applications/Xcode.app/Contents/Developer"),
        "/usr/bin/swift": swift,
    }
    ran: list[str] = []

    def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        ran.append(argv[0])
        answer = answers[argv[0]]
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(argv, answer[0], answer[1], "")

    with patch.object(host_platform.subprocess, "run", run):
        return host_platform.read_host_platform(), ran


class HostModelAvailabilityRouteSmoke(unittest.TestCase):
    bundle: ContractBundle

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = ContractBundle.load(source_revision=_REVISION, directory=_KB)

    def test_t1_unavailable_model_on_macos_27_withholds_apple_and_offers_llama_cpp(self) -> None:
        host = HostPlatform("27.0", "arm64", _NOT_ELIGIBLE)
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            options = _options(self.bundle, host, _INFERENCE)
        offered = _supported(options)
        self.assertNotIn("apple_foundation_models", offered)
        self.assertIn("llama_cpp", offered)
        reason = str(options["apple_foundation_models"]["unavailable_reason"])
        self.assertIn("DEVICE_NOT_ELIGIBLE", reason)
        self.assertIn("macOS 27.0", reason)
        bound = _bound(self.bundle, host)
        host_option_binding.require_host_available(bound, {_INFERENCE: "llama_cpp"})
        with self.assertRaises(ContractError) as refused:
            host_option_binding.require_host_available(bound, {_INFERENCE: "apple_foundation_models"})
        self.assertIn("DEVICE_NOT_ELIGIBLE", str(refused.exception))

    def test_t1b_embeddings_stay_on_core_ai_when_only_the_model_is_unavailable(self) -> None:
        offered = _supported(_options(self.bundle, HostPlatform("27.0", "arm64", _NOT_ELIGIBLE), "embeddings_implementation"))
        self.assertIn("coreai", offered)
        self.assertNotIn("llama_cpp", offered)

    def test_t2_available_model_on_macos_27_keeps_the_apple_route(self) -> None:
        host = HostPlatform("27.0", "arm64", _AVAILABLE)
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            offered = _supported(_options(self.bundle, host, _INFERENCE))
        self.assertIn("apple_foundation_models", offered)
        self.assertNotIn("llama_cpp", offered)
        host_option_binding.require_host_available(_bound(self.bundle, host), {_INFERENCE: "apple_foundation_models"})

    def test_t3_unknown_model_offers_every_option_with_no_default(self) -> None:
        with self.assertLogs(_LOGGER, level="WARNING") as logged:
            bound = _bound(self.bundle, HostPlatform("27.0", "arm64"))
        offered = _supported(_decision_options(bound, _INFERENCE))
        self.assertTrue({"apple_foundation_models", "llama_cpp", "lm_studio"} <= offered, offered)
        recommended = cast(list[str], bound.decisions[_INFERENCE]["recommended_option_refs"])
        self.assertIn("apple_foundation_models", recommended)
        self.assertIn("llama_cpp", recommended)
        self.assertEqual(len(logged.records), 1)
        advisory = logged.output[0]
        for fragment in ("unknown", "apple_fm", "all offered", "no option is preselected"):
            self.assertIn(fragment, advisory)
        for option_id in ("apple_foundation_models", "llama_cpp", "lm_studio"):
            host_option_binding.require_host_available(bound, {_INFERENCE: option_id})
        plan = _unknown_plan(self.bundle, HostPlatform("27.0", "arm64"))
        decisions = cast(dict[str, JsonValue], plan.answers["decisions"])
        self.assertIn(_INFERENCE, plan.unresolved_decisions)
        self.assertNotIn(_INFERENCE, decisions)
        chosen = _unknown_plan(self.bundle, HostPlatform("27.0", "arm64"), {_INFERENCE: "llama_cpp"})
        self.assertEqual(cast(dict[str, JsonValue], chosen.answers["decisions"]).get(_INFERENCE), "llama_cpp")

    def test_t3b_no_advisory_when_the_macos_version_already_decides(self) -> None:
        for host in (HostPlatform("26.4", "arm64"), HostPlatform("27.0", "x86_64")):
            with self.subTest(host=host), self.assertNoLogs(_LOGGER, level="WARNING"):
                offered = _supported(_options(self.bundle, host, _INFERENCE))
            self.assertIn("llama_cpp", offered)
            self.assertNotIn("apple_foundation_models", offered)

    def test_t4_reader_turns_each_swift_answer_into_the_model_fact(self) -> None:
        cases: tuple[tuple[str, ModelAvailability | None], ...] = (
            ("AVAILABLE", _AVAILABLE),
            ("UNAVAILABLE deviceNotEligible", _NOT_ELIGIBLE),
            ("UNAVAILABLE appleIntelligenceNotEnabled", ModelAvailability(False, "APPLE_INTELLIGENCE_NOT_ENABLED")),
            ("UNAVAILABLE modelNotReady", ModelAvailability(False, "MODEL_NOT_READY")),
            ("UNAVAILABLE", ModelAvailability(False, "UNSPECIFIED")),
            ("UNAVAILABLE unknown(1)", ModelAvailability(False, "UNSPECIFIED")),
            ("warning: a note\nAVAILABLE", _AVAILABLE),
            ("", None),
            ("gibberish", None),
            ("AVAILABLE maybe", None),
        )
        for answer, expected in cases:
            with self.subTest(answer=answer):
                host, ran = _read((0, answer))
                self.assertEqual(host.model, expected)
                self.assertEqual(ran, ["/usr/bin/sw_vers", "/usr/bin/uname", "/usr/bin/xcode-select", "/usr/bin/swift"])
                self.assertEqual((host.product_version, host.machine), ("27.0", "arm64"))

    def test_t4b_a_probe_that_cannot_run_gives_unknown_never_an_error(self) -> None:
        failures: tuple[tuple[str, tuple[int, str] | Exception], ...] = (
            ("swift exits non-zero", (1, "")),
            ("swift times out", subprocess.TimeoutExpired("/usr/bin/swift", 20)),
            ("swift is missing", FileNotFoundError("/usr/bin/swift")),
        )
        for label, swift in failures:
            with self.subTest(label):
                host, _ = _read(swift)
                self.assertIsNone(host.model)
                self.assertEqual((host.product_version, host.machine), ("27.0", "arm64"))

    def test_t4c_no_developer_tools_means_swift_is_never_started(self) -> None:
        host, ran = _read((0, "AVAILABLE"), xcode_select_code=2)
        self.assertIsNone(host.model)
        self.assertNotIn("/usr/bin/swift", ran)

    def test_t4d_the_swift_probe_is_bounded_at_sixty_seconds(self) -> None:
        seen: list[tuple[str, object]] = []
        answers = {
            "/usr/bin/sw_vers": (0, "27.0"),
            "/usr/bin/uname": (0, "arm64"),
            "/usr/bin/xcode-select": (0, "/Applications/Xcode.app/Contents/Developer"),
            "/usr/bin/swift": (0, "AVAILABLE"),
        }

        def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.append((argv[0], kwargs.get("timeout")))
            code, out = answers[argv[0]]
            return subprocess.CompletedProcess(argv, code, out, "")

        with patch.object(host_platform.subprocess, "run", run):
            host = host_platform.read_host_platform()
        self.assertEqual(host.model, _AVAILABLE)
        self.assertIn(("/usr/bin/swift", 60), seen)
        for command in ("/usr/bin/xcode-select", "/usr/bin/sw_vers", "/usr/bin/uname"):
            self.assertIn((command, 5), seen)

    def test_t5_the_model_is_measured_only_where_it_can_exist(self) -> None:
        for version, machine in (("15.7", "arm64"), ("27.0", "x86_64")):
            with self.subTest(version=version, machine=machine):
                host, ran = _read((0, "AVAILABLE"), version=version, machine=machine)
                self.assertIsNone(host.model)
                self.assertEqual(ran, ["/usr/bin/sw_vers", "/usr/bin/uname"])
        host, ran = _read((0, "AVAILABLE"), version="26.4")
        self.assertEqual(host.model, _AVAILABLE)
        self.assertIn("/usr/bin/swift", ran)

    def test_t5b_an_unreadable_macos_version_still_raises_and_starts_no_probe(self) -> None:
        with self.assertRaises(host_platform.HostPlatformError):
            _read((0, "AVAILABLE"), sw_vers_code=1)

    def test_t6_only_the_model_profile_reads_the_model(self) -> None:
        model_profile = host_option_binding.HostProfile("apple_fm", 27, "arm64")
        plain_profile = host_option_binding.HostProfile("apple_embeddings", 27, "arm64")
        unavailable, known, unknown = HostPlatform("27.0", "arm64", _NOT_ELIGIBLE), HostPlatform("27.0", "arm64", _AVAILABLE), HostPlatform("27.0", "arm64")
        self.assertFalse(model_profile.met_by(unavailable))
        self.assertTrue(model_profile.platform_met_by(unavailable))
        self.assertFalse(model_profile.model_unknown_on(unavailable))
        self.assertTrue(model_profile.met_by(known))
        self.assertTrue(model_profile.met_by(unknown))
        self.assertTrue(model_profile.model_unknown_on(unknown))
        self.assertFalse(model_profile.model_unknown_on(HostPlatform("26.4", "arm64")))
        self.assertTrue(plain_profile.met_by(unavailable))
        self.assertFalse(plain_profile.model_unknown_on(unknown))


if __name__ == "__main__":
    unittest.main(verbosity=2)
