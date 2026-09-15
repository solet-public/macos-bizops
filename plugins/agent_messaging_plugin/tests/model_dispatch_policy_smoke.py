#!/usr/bin/env python3
"""Focused fail-closed tests for the declarative model dispatch policy."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import model_dispatch_policy as policy  # noqa: E402
from agent_messaging_plugin.dispatch_policy_pair_sweep import _unpaired_candidate  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _raises_code(callback: object) -> str:
    try:
        assert callable(callback)
        callback()
    except policy.DispatchPolicyError as exc:
        return exc.code
    return ""


def test_missing_and_malformed_policy_refuse() -> None:
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "missing.json"
        try:
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "dispatch_policy_unavailable",
                "missing policy refuses at first use",
            )
            path.write_text("{not json", encoding="utf-8")
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "dispatch_policy_unavailable",
                "malformed policy refuses at first use",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


def test_red_mutation_deleting_fix_row_makes_fix_refuse() -> None:
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    source = json.loads(original.read_text(encoding="utf-8"))
    del source["dispatch_kinds"]["fix"]
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "policy.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        try:
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            _check(
                _raises_code(
                    lambda: policy.validate_spawn_dispatch(
                        dispatch_kind="fix", agent_runtime="codex", model="gpt-5.6-terra",
                        reviewed_report_vendor="", pair_id="",
                    ),
                ) == "dispatch_policy_invalid",
                "red mutation deleting the fix row refuses fix dispatch",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


def test_orchestrator_model_requires_profile_pair() -> None:
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    source = json.loads(original.read_text(encoding="utf-8"))
    source["orchestrator"]["allowed_models"].append("claude-unprofiled-fixture")
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "policy.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        try:
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "dispatch_policy_invalid",
                "orchestrator model without a profile pair refuses policy loading",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


def test_fable_5_1_orchestrator_model_is_allowed() -> None:
    role_label = chr(65) + "da-Main"
    _check(
        policy.orchestrator_model_verdict(role_label=role_label, model="claude-fable-5-1") == (
            True,
            ("claude-sonnet-5", "claude-fable-5", "claude-fable-5-1"),
        ),
        "claude-fable-5-1 is allowed for the main role",
    )


def test_budget_vendor_override_preserves_default_and_relaxes_cited_kinds() -> None:
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    source = json.loads(original.read_text(encoding="utf-8"))
    candidate = {
        "agent_instance_id": "agi-lone-producer",
        "dispatch_kind": "diagnose",
        "pair_id": "pair-1",
        "agent_runtime": "codex",
        "created_at": (datetime.now(UTC) - timedelta(seconds=601)).isoformat(),
    }
    try:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "policy.json"
            source_without_override = dict(source)
            del source_without_override["budget_vendor_override"]
            path.write_text(json.dumps(source_without_override), encoding="utf-8")
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            _check(
                _raises_code(
                    lambda: policy.validate_spawn_dispatch(
                        dispatch_kind="review", agent_runtime="codex", model="gpt-5.6-terra",
                        reviewed_report_vendor="codex", pair_id="pair-1",
                    ),
                ) == "dispatch_policy_violation",
                "absent override keeps same-vendor review refusal",
            )
            _check(
                _unpaired_candidate(candidate, [candidate], datetime.now(UTC), policy.load_dispatch_policy())
                == "agi-lone-producer",
                "absent override keeps diagnose pairing notice active",
            )

            path.write_text(json.dumps(source), encoding="utf-8")
            _check(
                _raises_code(
                    lambda: policy.validate_spawn_dispatch(
                        dispatch_kind="review", agent_runtime="codex", model="gpt-5.6-terra",
                        reviewed_report_vendor="codex", pair_id="pair-1",
                    ),
                ) == "",
                "active ruling-scoped override permits same-vendor review",
            )
            _check(
                _unpaired_candidate(candidate, [candidate], datetime.now(UTC), policy.load_dispatch_policy()) is None,
                "active override suppresses diagnose pairing notice",
            )

            malformed = json.loads(json.dumps(source))
            malformed["budget_vendor_override"]["active"] = "yes"
            path.write_text(json.dumps(malformed), encoding="utf-8")
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "dispatch_policy_invalid",
                "malformed override fails closed with dispatch_policy_invalid",
            )
    finally:
        policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


if __name__ == "__main__":
    test_missing_and_malformed_policy_refuse()
    test_red_mutation_deleting_fix_row_makes_fix_refuse()
    test_orchestrator_model_requires_profile_pair()
    test_fable_5_1_orchestrator_model_is_allowed()
    test_budget_vendor_override_preserves_default_and_relaxes_cited_kinds()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    raise SystemExit(1 if _failed else 0)
