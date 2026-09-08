"""Fail-closed model and vendor policy for every managed spawn.

The policy is deliberately data-backed: changing an assignment rule means a
reviewable JSON edit, while this module owns parsing and rejection semantics.
No policy is cached. A missing or malformed file therefore refuses the first
spawn after an edit instead of leaving a stale, silently permissive process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_POLICY_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "model_dispatch_policy.v1.json"
_PROFILE_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "model_profiles"
_DISPATCH_KINDS: Final[frozenset[str]] = frozenset(
    {"diagnose", "design", "review", "fix", "infrastructure"}
)
_VENDORS: Final[frozenset[str]] = frozenset({"codex", "claude_code"})
ModelPair = tuple[str, str]


class DispatchPolicyError(Exception):
    """A deterministic refusal emitted before a managed-session write."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DispatchPolicy:
    allowed_pairs: dict[str, tuple[tuple[str, str], ...]]
    allow_any_kinds: frozenset[str]
    orchestrator_role_names: frozenset[str]
    orchestrator_models: tuple[str, ...]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DispatchPolicyError(
            "dispatch_policy_unavailable", f"model dispatch policy is missing: {path}",
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchPolicyError(
            "dispatch_policy_unavailable", f"model dispatch policy is unreadable: {path}: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise DispatchPolicyError("dispatch_policy_invalid", "model dispatch policy must be an object.")
    return raw


def _non_empty_strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise DispatchPolicyError("dispatch_policy_invalid", f"{field} must be a non-empty string list.")
    return tuple(value)


def _read_profile(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchPolicyError(
            "dispatch_policy_unavailable", f"model profile is unreadable: {path}: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise DispatchPolicyError("dispatch_policy_invalid", f"model profile must be an object: {path}")
    return raw


def _catalog_profile_pairs(raw: dict[str, Any]) -> set[ModelPair]:
    runtime, models = raw.get("runtime"), raw.get("models")
    if not isinstance(runtime, str) or not isinstance(models, list):
        return set()
    return {
        (runtime, model_id)
        for row in models
        if isinstance(row, dict) and isinstance((model_id := row.get("canonical_model_id")), str)
    }


def _cost_profile_pairs(raw: dict[str, Any]) -> set[ModelPair]:
    profiles = raw.get("profiles")
    if not isinstance(profiles, list):
        return set()
    return {
        (runtime, model)
        for row in profiles
        if isinstance(row, dict)
        and isinstance((runtime := row.get("runtime")), str)
        and isinstance((model := row.get("model")), str)
    }


def _profile_pairs() -> set[ModelPair]:
    paths = sorted(_PROFILE_ROOT.glob("*.v1.json"))
    if not paths:
        raise DispatchPolicyError(
            "dispatch_policy_unavailable", f"model profile directory is empty: {_PROFILE_ROOT}",
        )
    pairs: set[ModelPair] = set()
    for path in paths:
        raw = _read_profile(path)
        pairs.update(_catalog_profile_pairs(raw))
        pairs.update(_cost_profile_pairs(raw))
    return pairs


def _policy_sections(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if raw.get("schema_version") != 1 or not isinstance(raw.get("policy_version"), str):
        raise DispatchPolicyError("dispatch_policy_invalid", "policy schema_version=1 and policy_version are required.")
    kinds = raw.get("dispatch_kinds")
    orchestrator = raw.get("orchestrator")
    if not isinstance(kinds, dict) or set(kinds) != set(_DISPATCH_KINDS) or not isinstance(orchestrator, dict):
        raise DispatchPolicyError("dispatch_policy_invalid", "policy must declare exactly the supported dispatch kinds and orchestrator.")
    return kinds, orchestrator


def _allowed_rule_keys(kind: str, rule: dict[str, Any]) -> set[str]:
    allowed_keys = {"allowed_pairs"}
    if kind in {"diagnose", "design"}:
        allowed_keys.add("pair_requirement")
        if rule.get("pair_requirement") != "cross_vendor":
            raise DispatchPolicyError("dispatch_policy_invalid", f"dispatch_kinds.{kind} must require cross_vendor pairing.")
    if kind == "review":
        allowed_keys.add("review_requirement")
        if rule.get("review_requirement") != "cross_vendor":
            raise DispatchPolicyError("dispatch_policy_invalid", "review must require cross_vendor review.")
    return allowed_keys


def _parse_rule_pairs(kind: str, rule: dict[str, Any], profiles: set[ModelPair]) -> tuple[ModelPair, ...]:
    entries = rule.get("allowed_pairs")
    if not isinstance(entries, list) or not entries:
        raise DispatchPolicyError("dispatch_policy_invalid", f"dispatch_kinds.{kind}.allowed_pairs is required.")
    pairs: list[ModelPair] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"agent_runtime", "model"}:
            raise DispatchPolicyError("dispatch_policy_invalid", f"dispatch_kinds.{kind} has malformed allowed_pairs.")
        runtime, model = entry.get("agent_runtime"), entry.get("model")
        if not isinstance(runtime, str) or not isinstance(model, str) or (runtime, model) not in profiles:
            raise DispatchPolicyError(
                "dispatch_policy_invalid",
                f"dispatch_kinds.{kind} names unknown model pair ({runtime!r}, {model!r}); refresh model_profiles.",
            )
        pairs.append((runtime, model))
    if len(set(pairs)) != len(pairs):
        raise DispatchPolicyError("dispatch_policy_invalid", f"dispatch_kinds.{kind} repeats an allowed pair.")
    return tuple(pairs)


def _parse_dispatch_rule(kind: str, raw_rule: object, profiles: set[ModelPair]) -> tuple[bool, tuple[ModelPair, ...]]:
    if not isinstance(raw_rule, dict):
        raise DispatchPolicyError("dispatch_policy_invalid", f"dispatch_kinds.{kind} must be an object.")
    if raw_rule.get("allow_any_pair") is True:
        if set(raw_rule) != {"allow_any_pair"}:
            raise DispatchPolicyError("dispatch_policy_invalid", f"dispatch_kinds.{kind} has unknown keys.")
        return True, ()
    if set(raw_rule) != _allowed_rule_keys(kind, raw_rule):
        raise DispatchPolicyError("dispatch_policy_invalid", f"dispatch_kinds.{kind} has unknown keys.")
    return False, _parse_rule_pairs(kind, raw_rule, profiles)


def _parse_dispatch_rules(kinds: dict[str, Any], profiles: set[ModelPair]) -> tuple[dict[str, tuple[ModelPair, ...]], set[str]]:
    allowed_pairs: dict[str, tuple[ModelPair, ...]] = {}
    allow_any: set[str] = set()
    for kind in sorted(_DISPATCH_KINDS):
        accepts_any, pairs = _parse_dispatch_rule(kind, kinds[kind], profiles)
        if accepts_any:
            allow_any.add(kind)
        else:
            allowed_pairs[kind] = pairs
    return allowed_pairs, allow_any


def _parse_orchestrator(orchestrator: dict[str, Any], profiles: set[ModelPair]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    role_names = _non_empty_strings(orchestrator.get("role_names"), "orchestrator.role_names")
    models = _non_empty_strings(orchestrator.get("allowed_models"), "orchestrator.allowed_models")
    if any(("claude_code", model) not in profiles for model in models):
        raise DispatchPolicyError("dispatch_policy_invalid", "orchestrator names a model absent from claude_code profiles.")
    return role_names, models


def load_dispatch_policy() -> DispatchPolicy:
    """Load and validate the current policy and every named profile pair."""
    kinds, orchestrator = _policy_sections(_read_json(_POLICY_PATH))
    profiles = _profile_pairs()
    allowed_pairs, allow_any = _parse_dispatch_rules(kinds, profiles)
    role_names, models = _parse_orchestrator(orchestrator, profiles)
    return DispatchPolicy(
        allowed_pairs=allowed_pairs,
        allow_any_kinds=frozenset(allow_any),
        orchestrator_role_names=frozenset(role_names),
        orchestrator_models=models,
    )


def _validate_dispatch_kind(dispatch_kind: str) -> None:
    if not dispatch_kind:
        raise DispatchPolicyError("dispatch_kind_required", "spawn_session requires dispatch_kind.")
    if dispatch_kind not in _DISPATCH_KINDS:
        raise DispatchPolicyError("dispatch_policy_violation", f"unknown dispatch_kind {dispatch_kind!r}.")


def _validate_allowed_pair(
    policy: DispatchPolicy, dispatch_kind: str, agent_runtime: str, model: str,
) -> None:
    if dispatch_kind in policy.allow_any_kinds:
        return
    allowed = policy.allowed_pairs[dispatch_kind]
    if (agent_runtime, model) not in allowed:
        rendered = ", ".join(f"({runtime}, {allowed_model})" for runtime, allowed_model in allowed)
        raise DispatchPolicyError(
            "dispatch_policy_violation",
            f"dispatch_kind={dispatch_kind!r} received ({agent_runtime}, {model}); allowed: {rendered}.",
        )


def _validate_pair_id(dispatch_kind: str, pair_id: str) -> None:
    if dispatch_kind in {"diagnose", "design"} and not pair_id:
        raise DispatchPolicyError(
            "dispatch_policy_violation", f"dispatch_kind={dispatch_kind!r} requires pair_id.",
        )


def _validate_review_vendor(agent_runtime: str, reviewed_report_vendor: str) -> None:
    if reviewed_report_vendor not in _VENDORS:
        raise DispatchPolicyError(
            "dispatch_policy_violation", "review requires reviewed_report_vendor=codex or claude_code.",
        )
    if reviewed_report_vendor == agent_runtime:
        raise DispatchPolicyError(
            "dispatch_policy_violation",
            f"review requires the other vendor; reviewer={agent_runtime}, report={reviewed_report_vendor}.",
        )


def validate_spawn_dispatch(
    *, dispatch_kind: str, agent_runtime: str, model: str,
    reviewed_report_vendor: str, pair_id: str,
) -> None:
    """Raise a named refusal unless one spawn satisfies the policy."""
    _validate_dispatch_kind(dispatch_kind)
    policy = load_dispatch_policy()
    _validate_allowed_pair(policy, dispatch_kind, agent_runtime, model)
    _validate_pair_id(dispatch_kind, pair_id)
    if dispatch_kind == "review":
        _validate_review_vendor(agent_runtime, reviewed_report_vendor)


def orchestrator_model_verdict(*, role_label: str, model: str) -> tuple[bool, tuple[str, ...]]:
    """Return whether a policy-governed seat has an allowed transcript model."""
    policy = load_dispatch_policy()
    role_name = role_label.rsplit("-", maxsplit=1)[-1]
    if role_label not in policy.orchestrator_role_names and role_name not in policy.orchestrator_role_names:
        return True, policy.orchestrator_models
    return model in policy.orchestrator_models, policy.orchestrator_models


__all__ = [
    "DispatchPolicyError",
    "DispatchPolicy",
    "load_dispatch_policy",
    "orchestrator_model_verdict",
    "validate_spawn_dispatch",
]
