"""Regression coverage for explicit session-source boundary selection."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager import stage_boundaries  # noqa: E402
from solet_manager.answer_validation import validate_decision_selection  # noqa: E402
from solet_manager.cli_commands import parse_decisions  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.decision_resolution import unresolved_required_decisions  # noqa: E402
from solet_manager.errors import ContractError  # noqa: E402
from solet_manager.flow import initial_stage_probe_statuses  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402
from solet_manager.transaction import canonical_sha256  # noqa: E402

_CONTRACTS = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "github_midwife_plugin"
    / "knowledge_base"
)


def _decisions(*, session_sources: list[str] | None) -> dict[str, object]:
    decisions: dict[str, object] = {
        "setup_profile": "macos-bizops",
        "autostart": "enabled",
        "embeddings_implementation": "lm_studio",
        "embedding_model": "fixture-embedding",
        "inference_implementation": "none",
        "coding_agents": ["codex"],
        "execution_topology": "solo",
        "connector_configuration_timing": "first_use",
    }
    if session_sources is not None:
        decisions["session_sources"] = session_sources
    return decisions


def _assert_explicit_answer_required(bundle: ContractBundle) -> None:
    definition = bundle.decisions["session_sources"]
    if definition.get("required") is not True:
        raise AssertionError("red: permit session-source omission to force-fire roots")
    omitted = _decisions(session_sources=None)
    unresolved = unresolved_required_decisions(
        bundle, omitted, resolution_stage_ids={"decision_review"}
    )
    if "session_sources" not in unresolved:
        raise AssertionError("red: advance without an explicit session-source choice")
    if parse_decisions(["session_sources="]) != {"session_sources": []}:
        raise AssertionError("red: CLI cannot submit an explicit empty session-source answer")
    required_empty = parse_decisions(["coding_agents="])
    try:
        validate_decision_selection(
            "coding_agents", required_empty["coding_agents"], bundle.decisions["coding_agents"]
        )
    except ContractError as exc:
        if "fewer than 1 selections" not in str(exc):
            raise AssertionError("red: required selections reject with an unrelated error") from exc
    else:
        raise AssertionError("red: CLI empty answer bypasses a minimum-one decision")


def _assert_probe_boundaries(bundle: ContractBundle) -> None:
    declined = initial_stage_probe_statuses(
        bundle, {"decisions": _decisions(session_sources=[])}
    )["session_sources"]["entry"]
    if not all(status is CheckpointStatus.NOT_APPLICABLE for status in declined.values()):
        raise AssertionError("red: probe roots after an explicit empty selection")
    selected = initial_stage_probe_statuses(
        bundle, {"decisions": _decisions(session_sources=["codex_local"])}
    )["session_sources"]["entry"]
    if (
        selected["codex_session_roots_readable"] is not CheckpointStatus.PENDING
        or selected["claude_session_roots_readable"]
        is not CheckpointStatus.NOT_APPLICABLE
    ):
        raise AssertionError("red: probe unselected session-source roots")


def _assert_boundary_uses_journal_answers_fingerprint() -> None:
    journal_answers: dict[str, object] = {
        "decisions": {"session_sources": []},
        "resolution_evidence": [{"source": "journal"}],
    }
    transient_answers: dict[str, object] = {
        "decisions": {"session_sources": []},
        "resolution_evidence": [{"source": "preview"}],
    }
    journal_fingerprint = canonical_sha256(journal_answers)
    transient_fingerprint = canonical_sha256(transient_answers)
    if journal_fingerprint == transient_fingerprint:
        raise AssertionError("fixture must differ only in transient resolution evidence")
    transaction = SimpleNamespace(
        name="session-source-fixture",
        target="/tmp/session-source-fixture",
        answers_fingerprint=journal_fingerprint,
    )
    bundle = SimpleNamespace(
        flow_id="macos.repository_setup",
        source_revision="a" * 40,
        probes={
            "session_sources_retrievable": {
                "runner": "service_interface",
                "probe_ref": "selected_session_sources",
            }
        }
    )
    with (
        patch.object(stage_boundaries, "invoke_adapter", return_value=object()) as invoke,
        patch.object(
            stage_boundaries,
            "startup_readiness_budget",
            return_value=SimpleNamespace(consumer_probe_refs=frozenset()),
        ),
    ):
        stage_boundaries._invoke_boundary_probe(
            bundle=bundle,
            transaction=transaction,
            registry=object(),
            stage_id="session_sources",
            boundary="exit",
            probe_id="session_sources_retrievable",
            answers=transient_answers,
            attempt=1,
        )
    request = invoke.call_args.kwargs["request"]
    if request.answers_fingerprint != journal_fingerprint:
        raise AssertionError("red: boundary hashes transient resolution evidence")
    if request.answers_fingerprint == transient_fingerprint:
        raise AssertionError("red: boundary request carries the transient fingerprint")
    tampered_journal_answers = {
        "decisions": {"session_sources": ["codex_local"]},
        "resolution_evidence": [{"source": "journal"}],
    }
    if canonical_sha256(tampered_journal_answers) == journal_fingerprint:
        raise AssertionError("red: altered journal answers preserve their fingerprint")


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    _assert_explicit_answer_required(bundle)
    _assert_probe_boundaries(bundle)
    _assert_boundary_uses_journal_answers_fingerprint()
    print("session_sources_boundary_smoke OK: 10 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
