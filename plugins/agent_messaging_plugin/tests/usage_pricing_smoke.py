#!/usr/bin/env python3
"""Offline conformance checks for pure usage pricing and pool projections."""

from __future__ import annotations

import importlib
import json
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
PROFILE_PATH = ROOT / "plugins/agent_messaging_plugin/model_profiles/usage_economics.v1.json"


def check(condition: object, name: str) -> None:
    assert condition, name
    print("PASS", name)


def load_catalog(path: Path = PROFILE_PATH) -> Any:
    from agent_messaging_plugin.usage_economics_profiles import load_usage_economics_profile_catalog

    return load_usage_economics_profile_catalog(path, as_of=NOW)


def load_pricing() -> Any:
    try:
        return importlib.import_module("agent_messaging_plugin.usage_pricing")
    except ImportError as exc:
        raise AssertionError("MODULE_PRESENT_AND_EXPORTS_PUBLIC_NAMES") from exc


def metered_profile(catalog: Any) -> Any:
    return next(row for row in catalog.profiles if row.__class__.__name__ == "MeteredApiProfile" and row.provider == "openai" and row.runtime == "codex" and row.model == "gpt-5.6-sol")


def check_module_exports(pricing: Any) -> None:
    public_names = {
        "usage_from_ledger",
        "choose_basis",
        "price_usage",
        "attach_pools",
        "executor_mismatch",
        "estimate_for",
        "as_of_and_lag",
        "PriceResult",
        "PoolAttachment",
    }
    check(public_names.issubset(set(pricing.__all__)), "MODULE_PRESENT_AND_EXPORTS_PUBLIC_NAMES")


def check_usage_keeps_none(pricing: Any) -> None:
    usage = pricing.usage_from_ledger(
        {
            "input_tokens": 10,
            "cached_input_tokens": None,
            "cache_write_input_tokens": 3,
            "output_tokens": None,
            "reasoning_output_tokens": None,
            "tool_calls": 2,
        }
    )
    check(
        usage.input_tokens == 10 and usage.cached_input_tokens is None and usage.cache_write_input_tokens == 3 and usage.output_tokens is None and usage.reasoning_output_tokens is None and usage.tool_calls == 2,
        "USAGE_FROM_LEDGER_KEEPS_NONE",
    )


def check_metered_price(pricing: Any, catalog: Any) -> None:
    metered = metered_profile(catalog)
    usage = pricing.usage_from_ledger(
        {
            "input_tokens": 1_000_000,
            "cached_input_tokens": None,
            "cache_write_input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "reasoning_output_tokens": None,
            "tool_calls": 1_000,
        }
    )
    priced = pricing.price_usage(usage, {}, metered)
    expected = float(metered.input_per_mtok or 0) + float(metered.cache_write_per_mtok or 0) + float(metered.output_per_mtok or 0) + float(metered.tool_call_per_1k or 0) / 1_000
    check(
        priced.usd == expected and any(item.name == "cached_input_tokens" and item.count is None for item in priced.unpriced_categories),
        "PRICE_METERED_SUMS_NON_NONE_CATEGORIES",
    )


def check_flat_rate_precedence(pricing: Any, catalog: Any) -> None:
    choice = pricing.choose_basis(catalog, "anthropic", "claude_code", "claude-opus-5-5", NOW)
    check(choice == "list_equivalent:anthropic-max-20x-2026-08-22", "PRICE_LIST_EQUIVALENT_FOR_FLAT_RATE_RUNTIME")


def unknown_model_token_categories_are_counted(unknown_price: Any) -> bool:
    return any(item.name == "input_tokens" and item.count == 12 for item in unknown_price.unpriced_categories) and any(item.name == "cache_write_1h_input_tokens_ttl_premium" and item.count == 7 for item in unknown_price.unpriced_categories)


def unknown_model_breakdown_categories_are_counted(unknown_price: Any) -> bool:
    return any(item.name == "cache_write_5m_input_tokens_ttl_premium" and item.count == 4 for item in unknown_price.unpriced_categories) and any(item.name == "speed:fast" and item.count == 3 for item in unknown_price.unpriced_categories)


def unknown_model_price_is_counted(pricing: Any, catalog: Any) -> bool:
    unknown = pricing.choose_basis(catalog, "anthropic", "claude_code", "unknown", NOW)
    usage = pricing.usage_from_ledger(
        {
            "input_tokens": 12,
            "cached_input_tokens": None,
            "cache_write_input_tokens": None,
            "output_tokens": None,
            "reasoning_output_tokens": None,
            "tool_calls": None,
        }
    )
    breakdown = {
        "cache_write_1h_input_tokens": 7,
        "cache_write_5m_input_tokens": 4,
        "speed": {"standard": 2, "fast": 3},
    }
    unknown_price = pricing.price_usage(usage, breakdown, None, model="unknown")
    return unknown == "unpriced:model_unknown" and unknown_price.usd is None and unknown_price.reason == "model_unknown" and unknown_model_token_categories_are_counted(unknown_price) and unknown_model_breakdown_categories_are_counted(unknown_price)


def check_unpriced_models(pricing: Any, catalog: Any) -> None:
    no_model = pricing.choose_basis(catalog, "anthropic", "claude_code", "claude-sonnet-5-5", NOW)
    check(
        no_model == "unpriced:no_metered_profile_for_model" and unknown_model_price_is_counted(pricing, catalog),
        "PRICE_UNPRICED_WHEN_NO_PROFILE",
    )


def check_reasoning_breakdown(pricing: Any, catalog: Any) -> None:
    profile = replace(metered_profile(catalog), output_per_mtok=2.0, reasoning_output_per_mtok=10.0)
    usage = pricing.usage_from_ledger(
        {
            "input_tokens": None,
            "cached_input_tokens": None,
            "cache_write_input_tokens": None,
            "output_tokens": 100,
            "reasoning_output_tokens": 40,
            "tool_calls": None,
        }
    )
    result = pricing.price_usage(usage, {}, profile)
    check(
        result.usd is not None and abs(result.usd - 0.00052) < 1e-12,
        "PRICE_REASONING_IS_BREAKDOWN_OF_OUTPUT_NOT_ADDED",
    )


def check_ttl_and_speed(pricing: Any, catalog: Any) -> None:
    usage = pricing.usage_from_ledger(
        {
            "input_tokens": 10,
            "cached_input_tokens": None,
            "cache_write_input_tokens": 3,
            "output_tokens": None,
            "reasoning_output_tokens": None,
            "tool_calls": 2,
        }
    )
    breakdown = {
        "cache_write_1h_input_tokens": 8,
        "cache_write_5m_input_tokens": 3,
        "speed": {"standard": 4, "fast": 2},
    }
    result = pricing.price_usage(usage, breakdown, metered_profile(catalog))
    names = {item.name for item in result.unpriced_categories}
    check(
        "cache_write_1h_input_tokens_ttl_premium" in names and "cache_write_5m_input_tokens_ttl_premium" in names and "speed:fast" in names and "speed:standard" not in names,
        "PRICE_REPORTS_TTL_AND_SPEED_AS_UNPRICED_CATEGORIES",
    )


def check_stale_profiles(pricing: Any, catalog: Any) -> None:
    stale = next(row for row in catalog.profiles if row.__class__.__name__ == "MeteredApiProfile" and (row.fixture_only or row.refresh_status != "current"))
    stale_choice = pricing.choose_basis(replace(catalog, profiles=(stale,)), stale.provider, stale.runtime, stale.model, NOW)
    raw_profiles = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["profiles"]
    fixture_raw = next(row for row in raw_profiles if row["profile_id"] == stale.profile_id)
    fixture_raw["fixture_only"] = True
    with tempfile.TemporaryDirectory() as directory:
        fixture_path = Path(directory) / "economics.json"
        fixture_path.write_text(json.dumps({"schema_version": 1, "profiles": [fixture_raw]}), encoding="utf-8")
        fixture_choice = pricing.choose_basis(load_catalog(fixture_path), stale.provider, stale.runtime, stale.model, NOW)
    check(
        stale_choice.startswith("unpriced:") and fixture_choice.startswith("unpriced:"),
        "PRICE_STALE_AND_FIXTURE_PROFILES_NEVER_CHOSEN",
    )


def check_ambiguous_plan(pricing: Any, catalog: Any) -> None:
    flat = next(row for row in catalog.profiles if row.__class__.__name__ == "FlatRateQuotaProfile")
    ambiguous = replace(catalog, profiles=(*catalog.profiles, replace(flat, profile_id=f"{flat.profile_id}-second")))
    choice = pricing.choose_basis(ambiguous, flat.provider, flat.included_runtimes[0], "claude-opus-5-5", NOW)
    check(choice == "unpriced:ambiguous_flat_rate_plan", "CHOOSE_BASIS_NEVER_RAISES_ON_AMBIGUOUS_PLAN")


def check_latest_pool_reading(pricing: Any, catalog: Any) -> None:
    from agent_messaging_plugin.allowance_pool_readings import PoolReading

    flat = next(row for row in catalog.profiles if row.__class__.__name__ == "FlatRateQuotaProfile")
    pool = next(row for row in flat.allowance_pools if row.applies(runtime="claude_code", model="claude-opus-5-5"))
    old = PoolReading(
        profile_id=flat.profile_id,
        pool_id=pool.pool_id,
        consumed=10,
        remaining=90,
        as_of=NOW - timedelta(hours=3),
        next_reset_at=NOW + timedelta(hours=2),
        expires_at=NOW + timedelta(hours=3),
        source="smoke",
        recorded_by="smoke",
        recorded_at=NOW - timedelta(hours=3),
    )
    latest = replace(
        old,
        consumed=80,
        remaining=20,
        as_of=NOW - timedelta(minutes=20),
        next_reset_at=NOW - timedelta(minutes=10),
        recorded_at=NOW - timedelta(minutes=20),
    )
    attached = pricing.attach_pools(catalog, (old, latest), "claude_code", "claude-opus-5-5", NOW)
    result = next(row for row in attached.pools if row.pool_id == pool.pool_id)
    check(
        result.as_of == latest.as_of and result.expired and result.percent_consumed == 80,
        "POOLS_ATTACHED_WITH_LATEST_READING_AND_EXPIRED_FLAG",
    )


def check_unread_pools(pricing: Any, catalog: Any) -> None:
    pools = pricing.attach_pools(catalog, (), "claude_code", "claude-opus-5-5", NOW)
    check(
        bool(pools.pools) and all(row.reading_status == "unread" and row.percent_consumed is None for row in pools.pools),
        "POOL_WITHOUT_READING_REPORTED_UNREAD_NOT_REFUSED",
    )


def check_mismatch_detected(pricing: Any) -> None:
    result = pricing.executor_mismatch(
        {"selected": {"runtime": "codex", "model": "gpt-6-sol", "effort": "medium"}},
        {"runtime": "codex", "model": "gpt-5.6-sol", "effort": "high"},
    )
    check(result.mismatch and result.model_mismatch and result.effort_mismatch, "MISMATCH_FLAGGED_WHEN_OBSERVED_DIFFERS")


def check_unknown_mismatch_ignored(pricing: Any) -> None:
    result = pricing.executor_mismatch(
        {"selected": {"runtime": "codex", "model": "gpt-6-sol", "effort": "medium"}},
        {"runtime": "codex", "model": "unknown", "effort": "unknown"},
    )
    check(
        not result.mismatch and result.unknown_model_count == 1 and result.unknown_effort_count == 1,
        "MISMATCH_IGNORES_UNKNOWN_EFFORT_AND_COUNTS_IT",
    )


def check_receipt_estimate(pricing: Any) -> None:
    receipt = {"selected": {"runtime": "codex", "model": "gpt-6-sol", "effort": "medium", "cost_per_task_usd": 0.23}}
    result = pricing.estimate_for(receipt, (), ())
    check(result.usd == 0.23 and result.basis == "receipt", "ESTIMATE_FROM_RECEIPT")


def check_catalog_estimate(pricing: Any) -> None:
    receipt = {"selected": {"runtime": "codex", "model": "gpt-6-sol", "effort": "medium", "cost_per_task_usd": 0.23}}
    cell = {**receipt["selected"], "cost_per_task_usd": 0.41, "acceptance": "accepted"}
    result = pricing.estimate_for(None, ({"runtime": "codex", "model": "gpt-6-sol", "effort": "medium"},), (cell,))
    check(result.usd == 0.41 and result.basis == "catalog_lookup", "ESTIMATE_CATALOG_LOOKUP_WHEN_RECEIPT_NULL")


def check_no_estimate(pricing: Any) -> None:
    result = pricing.estimate_for(None, (), ())
    check(result.usd is None and result.basis == "none", "ESTIMATE_NONE_WHEN_NEITHER")


def check_as_of_bounds(pricing: Any) -> None:
    older = NOW - timedelta(minutes=10)
    newer = NOW - timedelta(minutes=2)
    result = pricing.as_of_and_lag(newer, older, NOW)
    check(result.as_of == older and result.lag_seconds == 600, "AS_OF_IS_MIN_OF_POLL_AND_LAST_EVENT")


def check_fixture_provider(pricing: Any, catalog: Any) -> None:
    profiles = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["profiles"]
    profile = next(row for row in profiles if row.get("kind") == "metered_api" and row.get("provider") == "openai")
    profile.update(
        {
            "profile_id": "fixture-provider-metered",
            "provider": "fixture_provider",
            "runtime": "fixture_runtime",
            "model": "fixture_model",
            "fixture_only": False,
            "refresh_status": "current",
        }
    )
    without = pricing.choose_basis(catalog, "fixture_provider", "fixture_runtime", "fixture_model", NOW)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "economics.json"
        path.write_text(json.dumps({"schema_version": 1, "profiles": [profile]}), encoding="utf-8")
        with_profile = pricing.choose_basis(load_catalog(path), "fixture_provider", "fixture_runtime", "fixture_model", NOW)
    check(
        without == "unpriced:no_metered_profile_for_model" and with_profile == "metered:fixture-provider-metered",
        "FIXTURE_PROVIDER_PRICES_UNPRICED_THEN_METERED",
    )


def run() -> None:
    pricing = load_pricing()
    catalog = load_catalog()
    check_module_exports(pricing)
    check_usage_keeps_none(pricing)
    check_metered_price(pricing, catalog)
    check_flat_rate_precedence(pricing, catalog)
    check_unpriced_models(pricing, catalog)
    check_reasoning_breakdown(pricing, catalog)
    check_ttl_and_speed(pricing, catalog)
    check_stale_profiles(pricing, catalog)
    check_ambiguous_plan(pricing, catalog)
    check_latest_pool_reading(pricing, catalog)
    check_unread_pools(pricing, catalog)
    check_mismatch_detected(pricing)
    check_unknown_mismatch_ignored(pricing)
    check_receipt_estimate(pricing)
    check_catalog_estimate(pricing)
    check_no_estimate(pricing)
    check_as_of_bounds(pricing)
    check_fixture_provider(pricing, catalog)


if __name__ == "__main__":
    run()
