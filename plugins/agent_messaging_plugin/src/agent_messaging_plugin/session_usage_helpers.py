"""Shared normalization, session-id, and usage-note helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard, cast

from .session_claude_mapping_store import list_session_claude_mappings
from .session_context_status_store import read_session_context_status
from .usage_economics import UsageVector
from .usage_economics_profiles import (
    MeteredApiProfile,
    UsageEconomicsProfileCatalog,
    UsageEconomicsProfileValidationError,
    load_usage_economics_profile_catalog,
)
from .usage_pricing import usage_from_ledger

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_PROFILE_ROOT = Path(__file__).resolve().parents[2] / "model_profiles"
USAGE_ECONOMICS_PROFILE_PATH = _PROFILE_ROOT / "usage_economics.v1.json"

_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "tool_calls",
)
_TOKEN_FIELDS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    },
)
_MISSING = object()

__all__ = [
    "USAGE_ECONOMICS_PROFILE_PATH",
    "UsageVerbError",
    "_USAGE_FIELDS",
    "_TOKEN_FIELDS",
    "_is_number",
    "_stamp",
    "_text",
    "_load_catalog",
    "_try_read",
    "provider_for_runtime",
    "_metered_profile",
    "_usage_dict",
    "_merge_usage",
    "_merge_model_totals",
    "_pricing_entry",
    "_serialize_unpriced_categories",
    "_known_unpriced_tokens",
    "_runtime_session_id",
    "_managed_runtime",
    "_session_ids",
    "_session_total_rows",
    "_response_advisories",
]


class UsageVerbError(ValueError):
    """A named caller error for the read-only usage verbs."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _stamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _load_catalog(now: datetime) -> tuple[UsageEconomicsProfileCatalog | None, str | None]:
    try:
        return load_usage_economics_profile_catalog(
            USAGE_ECONOMICS_PROFILE_PATH,
            as_of=now,
        ), None
    except (OSError, UsageEconomicsProfileValidationError) as exc:
        return None, f"pricing_catalog_unavailable:{type(exc).__name__}"


def _try_read(read: Callable[[], Any]) -> tuple[Any, Exception | None]:
    """Keep optional state and ledger reads advisory."""
    try:
        return read(), None
    except Exception as exc:  # noqa: BLE001 — read failures preserve available data
        return None, exc


def provider_for_runtime(
    catalog: UsageEconomicsProfileCatalog | None,
    runtime: str,
) -> str | None:
    """Return a provider only when current profile data names exactly one."""
    if catalog is None:
        return None
    providers = {profile.provider for profile in catalog.profiles if profile.refresh_status == "current" and (isinstance(profile, MeteredApiProfile) and profile.runtime == runtime and not profile.fixture_only or not isinstance(profile, MeteredApiProfile) and runtime in profile.included_runtimes)}
    return next(iter(providers)) if len(providers) == 1 else None


def _matches_metered_profile(
    profile: object,
    provider: str,
    runtime: str,
    model: str,
    as_of: datetime,
) -> bool:
    return isinstance(profile, MeteredApiProfile) and profile.provider == provider and profile.runtime == runtime and profile.model == model and profile.refresh_status == "current" and not profile.fixture_only and profile.effective_at <= as_of


def _metered_profile(
    catalog: UsageEconomicsProfileCatalog | None,
    provider: str,
    runtime: str,
    model: str,
    as_of: datetime,
) -> MeteredApiProfile | None:
    if catalog is None:
        return None
    matches = [profile for profile in catalog.profiles if _matches_metered_profile(profile, provider, runtime, model, as_of)]
    return (
        cast(
            MeteredApiProfile,
            max(matches, key=lambda row: (row.effective_at, row.profile_id)),
        )
        if matches
        else None
    )


def _usage_dict(entry: Mapping[str, Any]) -> dict[str, int | float | None]:
    return {field: entry.get(field) if _is_number(entry.get(field)) else None for field in _USAGE_FIELDS}


def _merge_breakdown(target: dict[str, Any], incoming: object) -> None:
    if not isinstance(incoming, Mapping):
        return
    for name, value in incoming.items():
        key = str(name)
        if _is_number(value):
            current = target.get(key)
            target[key] = (float(current) if _is_number(current) else 0.0) + float(value)
        elif isinstance(value, Mapping):
            nested = target.setdefault(key, {})
            if isinstance(nested, dict):
                _merge_breakdown(nested, value)
        elif key not in target:
            target[key] = value


def _merge_usage(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    usage = incoming.get("usage")
    usage_mapping = usage if isinstance(usage, Mapping) else incoming
    missing = target.setdefault("missing_fields", set())
    for field in _USAGE_FIELDS:
        value = usage_mapping.get(field)
        if _is_number(value):
            current = target.get(field)
            current_total = float(current) if _is_number(current) else 0.0
            target[field] = current_total + float(value)
        else:
            missing.add(field)
    _merge_breakdown(target.setdefault("breakdown", {}), usage_mapping.get("breakdown"))


def _merge_model_totals(
    target: dict[str, dict[str, Any]],
    by_model: object,
) -> None:
    if not isinstance(by_model, Mapping):
        return
    for raw_model, raw_usage in by_model.items():
        if not isinstance(raw_model, str) or not isinstance(raw_usage, Mapping):
            continue
        model_total = target.setdefault(
            raw_model,
            dict.fromkeys(_USAGE_FIELDS) | {"breakdown": {}, "missing_fields": set()},
        )
        _merge_usage(model_total, raw_usage)


def _pricing_entry(model_total: Mapping[str, Any]) -> tuple[UsageVector, dict[str, Any]]:
    raw: dict[str, Any] = {field: model_total.get(field) for field in _USAGE_FIELDS}
    missing = model_total.get("missing_fields")
    if isinstance(missing, set):
        for field in missing:
            raw[str(field)] = None
    breakdown = model_total.get("breakdown")
    raw["breakdown"] = dict(breakdown) if isinstance(breakdown, Mapping) else {}
    return usage_from_ledger(raw), raw["breakdown"]


def _serialize_unpriced_categories(categories: Sequence[Any]) -> list[dict[str, Any]]:
    return [{"name": category.name, "count": category.count} for category in categories]


def _known_unpriced_tokens(categories: Sequence[Any], *, model: str) -> tuple[int | float, bool]:
    total = _unpriced_token_total(categories, model=model)
    lower_bound = any(category.count is None for category in categories if category.name in _TOKEN_FIELDS)
    return total, lower_bound


def _unpriced_token_total(categories: Sequence[Any], *, model: str) -> int | float:
    counts = {category.name: category.count for category in categories if category.name in _TOKEN_FIELDS}
    known = {name: count for name, count in counts.items() if _is_number(count)}
    if model == "unknown":
        known.pop("reasoning_output_tokens", None)
    return sum(known.values()) + _unknown_model_reasoning(counts, model)


def _unknown_model_reasoning(counts: Mapping[str, Any], model: str) -> int | float:
    if model != "unknown" or "output_tokens" in counts:
        return 0
    reasoning = counts.get("reasoning_output_tokens")
    return reasoning if _is_number(reasoning) else 0


def _runtime_session_id(context: Mapping[str, Any] | None) -> str | None:
    if context is None:
        return None
    return _text(context.get("runtime_session_id")) or _text(context.get("claude_session_id"))


def _managed_runtime(session: Mapping[str, Any]) -> str:
    return _text(session.get("agent_runtime")) or "unknown"


def _session_ids(
    state: StateManagementInterface,
    session: Mapping[str, Any],
) -> tuple[
    list[str],
    Mapping[str, Any] | None,
    dict[str, int],
    list[str],
    bool,
]:
    instance_id = _text(session.get("agent_instance_id")) or ""
    advisories: list[str] = []
    mapping_ids, mapping_read_failed, mapping_warning = _mapping_ids_for_session(state, instance_id)
    if mapping_warning:
        advisories.append(mapping_warning)
    context, context_error = _try_read(lambda: read_session_context_status(state, instance_id)) if instance_id else (None, None)
    if context_error is not None:
        advisories.append(f"session_context_status_unavailable:{type(context_error).__name__}:{instance_id}")
    context_id = _runtime_session_id(context)
    context_ids = {context_id} if context_id is not None else set()
    return (
        sorted(mapping_ids | context_ids),
        context,
        {"session_claude_mapping": len(mapping_ids), "session_context_status": len(context_ids)},
        advisories,
        mapping_read_failed,
    )


def _mapping_ids_for_session(
    state: StateManagementInterface,
    instance_id: str,
) -> tuple[set[str], bool, str | None]:
    if not instance_id:
        return set(), False, None
    rows, error = _try_read(lambda: list_session_claude_mappings(state, instance_id))
    if error is not None:
        warning = f"session_claude_mapping_unavailable:{type(error).__name__}:{instance_id}"
        return set(), True, warning
    if not isinstance(rows, list):
        return set(), True, f"session_claude_mapping_unavailable:invalid_response:{instance_id}"
    return _mapping_ids(rows), False, None


def _mapping_ids(rows: Sequence[Any]) -> set[str]:
    mapping_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        mapping_id = _text(row.get("claude_session_id"))
        if mapping_id is not None:
            mapping_ids.add(mapping_id)
    return mapping_ids


def _session_total_rows(response: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_sessions = response.get("sessions")
    if not isinstance(raw_sessions, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for row in raw_sessions:
        if not isinstance(row, Mapping):
            continue
        runtime_id = _text(row.get("runtime_session_id"))
        if runtime_id is not None and runtime_id not in result:
            result[runtime_id] = row
    return result


def _response_advisories(response: Mapping[str, Any]) -> list[str]:
    raw = response.get("advisories")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]
