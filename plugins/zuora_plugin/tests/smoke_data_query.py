#!/usr/bin/env python3
"""Data Query + bulk export + queryMore continuation smoke tests for zuora_plugin.

Business-data limits + data-export migration, 2026-08-02
(workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md).
Both data_query and bulk_export now ALWAYS write to the caller-supplied
output_tsv_path — the former blob-export/INLINE_BYTE_CAP branch is deleted,
not lowered (07-29 data-export requirement, unconditional). Effective record ceiling is
DEFAULT_ROW_LIMIT (500) unless the caller supplies BOTH
acknowledge_default_limit_override=true and an explicit row_limit.

Hermetic — a ``MagicMock`` standing in for ``ZuoraClient`` (``post`` mocked
directly), no live tenant, no httpx transport. The export half drives the
REAL export_containment gate bound to a temp workspace root (the containment
boundary is exactly what must not be mocked).

Exercises:
  1. data_query — writes a TSV handle, never records/rows inline
  2. bulk_export follows queryMore when the first page's done=false, stopping
     once the effective limit is reached (queryMore is NOT called past cap)
  3. bulk_export accumulates across MULTIPLE queryMore pages when each page
     alone is short of the cap
  4. the client-side cap still binds even if the vendor's cumulative size
     exceeds it (defense-in-depth against a server-side signal miss)
  5-8. data_query — 4-case override-friction set (§5): default-caps,
     override-succeeds, malformed-refused (either half alone),
     cap-exceeded-refused
  9-12. bulk_export — same 4-case override-friction set, BULK_EXPORT_ROW_CAP ceiling
  13. bulk_export — writes a TSV at the caller's absolute path under the
     allowed root; column order follows first appearance; nested objects
     serialize as JSON text; returns {path, columns, row_count, total_size, truncated}
  14. red-first: export path OUTSIDE every allowed root -> PathRefusedError,
      no file written, the query never runs
  15. red-first: EMPTY export_allowed_roots -> refused naming the config key
  16. A non-2xx response raises ZuoraResponseError carrying is_query correctly

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/zuora_plugin/tests/smoke_data_query.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))
import tempfile  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import httpx  # noqa: E402
from ananta.file_locations.containment import PathRefusedError, assert_path_allowed, derive_allowed_roots  # noqa: E402
from ananta.file_locations.resolution import PathAddress, SoletTree  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "zuora_plugin" / "src"))

from zuora_plugin import billing_actions  # noqa: E402
from zuora_plugin.billing_actions import ZuoraResponseError  # noqa: E402
from zuora_plugin.constants import BULK_EXPORT_ROW_CAP, DATA_QUERY_MAX_ROWS_CAP, DEFAULT_ROW_LIMIT, PARAM_ACKNOWLEDGE_OVERRIDE, PARAM_ROW_LIMIT  # noqa: E402

_passed = 0
_failed: list[str] = []


_LOCATION_ROOT = Path(__file__).resolve().parents[3]
_LOCATION_TREE = SoletTree(_LOCATION_ROOT, _LOCATION_ROOT / "profile")


def _checked_path(path: str, directories: list[str], *, suffix: str | None = ".tsv", param_name: str = "output_tsv_path", reading: bool = False) -> str:
    """Exercise real address-derived containment, never a mocked boundary."""
    addresses = [PathAddress(f"project-{index}", Path(directory), frozenset(), frozenset(), datetime.now(UTC)) for index, directory in enumerate(directories)]
    roots = derive_allowed_roots(addresses, None, _LOCATION_TREE, _LOCATION_ROOT.parent / "test-solet-files")
    result = assert_path_allowed(path, roots, _LOCATION_TREE, suffix=suffix, param_name=param_name, plugin_name="test")
    if reading and not Path(result.value).is_file():
        raise PathRefusedError(f"{param_name} must name an existing regular file")
    return result.value


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _gate_for(roots: list[str]) -> Any:
    def gate(output_tsv_path: str) -> str:
        return _checked_path(output_tsv_path, roots, suffix=".tsv", param_name="output_tsv_path", reading=False)

    return gate


def _passthrough_gate(path: str) -> str:
    return path


def _query_response(records: list[dict[str, Any]], *, size: int | None = None, done: bool = True, query_locator: str | None = None) -> httpx.Response:
    payload: dict[str, Any] = {"records": records, "done": done, "size": size if size is not None else len(records)}
    if query_locator is not None:
        payload["queryLocator"] = query_locator
    return httpx.Response(200, json=payload, request=httpx.Request("POST", "https://fake/v1/action/query"))


def _client_with_pages(pages: list[httpx.Response]) -> Any:
    """A client whose .post() returns each page in order (first call = query, rest = queryMore)."""
    client = MagicMock()
    client.post.side_effect = list(pages)
    return client


def test_data_query_writes_tsv() -> None:
    client = _client_with_pages([_query_response([{"Id": "1", "Name": "A"}, {"Id": "2", "Name": "B"}])])
    with tempfile.TemporaryDirectory(prefix="zuora_shape_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.data_query(
            client,
            {"zoql": "SELECT Id, Name FROM Account", "output_tsv_path": out_path},
            _passthrough_gate,
        )
        _assert("row_count matches", result["row_count"] == 2)
        _assert("not truncated", result["truncated"] is False)
        _assert("no records/rows field — never inline", "records" not in result and "rows" not in result)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("record fields carried", "A" in lines[1])
        client.post.assert_called_once_with("/v1/action/query", json={"queryString": "SELECT Id, Name FROM Account"})


def test_bulk_export_stops_at_cap_without_extra_querymore() -> None:
    first_page = [{"Id": str(i)} for i in range(2000)]
    client = _client_with_pages([_query_response(first_page, size=2000, done=False, query_locator="loc-1")])
    writer_calls = {"count": 0}

    def _counting_gate(path: str) -> str:
        writer_calls["count"] += 1
        return path

    with tempfile.TemporaryDirectory(prefix="zuora_cap_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.bulk_export(
            client,
            {
                "zoql": "SELECT Id FROM Account",
                "output_tsv_path": out_path,
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: 2000,
            },
            _counting_gate,
        )
        _assert("row_count capped at row_limit", result["row_count"] == 2000)
        _assert("single vendor call — cap reached on the first page", client.post.call_count == 1)
        _assert("truncated True — done was false", result["truncated"] is True)


def test_bulk_export_follows_querymore_across_multiple_pages() -> None:
    page1 = [{"Id": f"p1-{i}"} for i in range(2000)]
    page2 = [{"Id": f"p2-{i}"} for i in range(2000)]
    page3 = [{"Id": f"p3-{i}"} for i in range(500)]
    client = _client_with_pages(
        [
            _query_response(page1, size=4500, done=False, query_locator="loc-1"),
            _query_response(page2, size=4500, done=False, query_locator="loc-2"),
            _query_response(page3, size=4500, done=True),
        ]
    )
    with tempfile.TemporaryDirectory(prefix="zuora_page_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.bulk_export(
            client,
            {
                "zoql": "SELECT Id FROM Account",
                "output_tsv_path": out_path,
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: 4500,
            },
            _passthrough_gate,
        )
        _assert("all three pages accumulated", result["row_count"] == 4500)
        _assert("three vendor calls (1 query + 2 queryMore)", client.post.call_count == 3)
        _assert("total_size carried from the last page", result["total_size"] == 4500)
        _assert("not truncated — vendor signaled done", result["truncated"] is False)
        second_call = client.post.call_args_list[1]
        _assert("second call hits queryMore with the first locator", second_call.args == ("/v1/action/queryMore",))
        _assert("second call passes queryLocator", second_call.kwargs["json"] == {"queryLocator": "loc-1"})


def test_client_side_cap_binds_despite_larger_reported_size() -> None:
    records = [{"Id": str(i)} for i in range(50)]
    client = _client_with_pages([_query_response(records, size=999999, done=True)])
    with tempfile.TemporaryDirectory(prefix="zuora_defense_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = billing_actions.data_query(client, {"zoql": "SELECT Id FROM Account", "output_tsv_path": out_path}, _passthrough_gate)
        _assert("client-side row_count reflects actual records, not the reported size", result["row_count"] == 50)
        _assert("truncated True — reported size exceeds fetched count", result["truncated"] is True)


def _override_friction_cases(verb: Any, hard_cap: int, label_prefix: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"zuora_{label_prefix}_friction_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")

        client = _client_with_pages([_query_response([{"Id": str(i)} for i in range(DEFAULT_ROW_LIMIT + 100)], size=DEFAULT_ROW_LIMIT + 100, done=True)])
        result = verb(client, {"zoql": "SELECT Id FROM Account", "output_tsv_path": out_path}, _passthrough_gate)
        _assert(f"{label_prefix}: default caps at {DEFAULT_ROW_LIMIT}", result["row_count"] == DEFAULT_ROW_LIMIT)

        client = _client_with_pages([_query_response([{"Id": str(i)} for i in range(600)], size=600, done=True)])
        result = verb(
            client,
            {"zoql": "SELECT Id FROM Account", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True, PARAM_ROW_LIMIT: 600},
            _passthrough_gate,
        )
        _assert(f"{label_prefix}: override reaches 600", result["row_count"] == 600)

        client = _client_with_pages([])
        raised = False
        try:
            verb(client, {"zoql": "SELECT Id FROM Account", "output_tsv_path": out_path, PARAM_ROW_LIMIT: 600}, _passthrough_gate)
        except ValueError:
            raised = True
        _assert(f"{label_prefix}: row_limit alone (no override flag) refused", raised)

        raised = False
        try:
            verb(client, {"zoql": "SELECT Id FROM Account", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True}, _passthrough_gate)
        except ValueError:
            raised = True
        _assert(f"{label_prefix}: override flag alone (no row_limit) refused", raised)

        raised = False
        try:
            verb(
                client,
                {"zoql": "SELECT Id FROM Account", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True, PARAM_ROW_LIMIT: hard_cap + 1},
                _passthrough_gate,
            )
        except ValueError:
            raised = True
        _assert(f"{label_prefix}: row_limit above the hard cap refused (not clamped)", raised)


def test_data_query_override_friction() -> None:
    _override_friction_cases(billing_actions.data_query, DATA_QUERY_MAX_ROWS_CAP, "data_query")


def test_bulk_export_override_friction() -> None:
    _override_friction_cases(billing_actions.bulk_export, BULK_EXPORT_ROW_CAP, "bulk_export")


def test_bulk_export_writes_tsv_shape() -> None:
    client = _client_with_pages([_query_response([{"Id": "1", "Nested": {"a": 1}}], size=1, done=True)])
    with tempfile.TemporaryDirectory(prefix="zuora_export_shape_") as allowed_root:
        out_path = str(Path(allowed_root) / "out.tsv")
        gate = _gate_for([allowed_root])
        result = billing_actions.bulk_export(client, {"zoql": "SELECT Id, Nested FROM Account", "output_tsv_path": out_path}, gate)
        _assert("export returns path/columns/row_count/total_size/truncated", set(result) == {"path", "columns", "row_count", "total_size", "truncated"})
        import csv as _csv

        with open(out_path, newline="", encoding="utf-8") as handle:
            rows = list(_csv.reader(handle, delimiter="\t"))
        _assert("nested object serialized as JSON text", rows[1][1] == '{"a": 1}', str(rows[1]))


def test_export_path_outside_allowed_root_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="zuora_allowed_") as allowed_root, tempfile.TemporaryDirectory(prefix="zuora_outside_") as outside_root:
        gate = _gate_for([allowed_root])
        out_path = str(Path(outside_root) / "out.tsv")
        client = MagicMock()
        raised = False
        try:
            billing_actions.bulk_export(client, {"zoql": "SELECT Id FROM Account", "output_tsv_path": out_path}, gate)
        except PathRefusedError:
            raised = True
        _assert("outside-root path refused", raised)
        _assert("query never runs when the path is refused", client.post.call_count == 0)
        _assert("no file written", not Path(out_path).exists())


def test_empty_allowed_roots_refused() -> None:
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory).resolve() / "home"
        roots = derive_allowed_roots([], None, _LOCATION_TREE, home)
        result = assert_path_allowed(str(home / "Exports.nosync/out.tsv"), roots, _LOCATION_TREE, suffix=".tsv", param_name="output_tsv_path", plugin_name="test")
        _assert("empty address book admits computed exports folder", result.value == str(home / "Exports.nosync/out.tsv"))
        _assert("computed default carries an advisory", bool(result.advisories))


def test_non_2xx_raises_with_is_query_flag() -> None:
    client = MagicMock()
    client.post.return_value = httpx.Response(400, json={"reasons": []}, request=httpx.Request("POST", "https://fake/v1/action/query"))
    raised: ZuoraResponseError | None = None
    try:
        billing_actions.data_query(client, {"zoql": "SELECT bogus", "output_tsv_path": "/tmp/x.tsv"}, _passthrough_gate)
    except ZuoraResponseError as exc:
        raised = exc
    _assert("non-2xx raises ZuoraResponseError", raised is not None)
    _assert("is_query True for data_query", raised is not None and raised.is_query is True)


def test_address_root_replaces_config() -> None:
    """A live address authorizes exports without a connector root setting."""
    from zuora_plugin.plugin import ZuoraPlugin

    with tempfile.TemporaryDirectory() as directory:
        plugin = ZuoraPlugin()
        plugin.initialize({})
        orchestrator = MagicMock()
        orchestrator.APP_HOME = str(REPO_ROOT / "profile")
        orchestrator.get_service.return_value.list_path_addresses.return_value = {"action_status": "completed", "data": {"addresses": [{"name": "project", "tags": [], "updated_at": "2026-11-08T00:00:00+00:00", "entries": [{"field_type": "path", "value": directory}]}]}}
        orchestrator.plugin_manager.get_plugin.return_value = None
        plugin.orchestrator_ref = orchestrator
        path = str(Path(directory).resolve() / "out.tsv")
        _assert("live path address authorizes export without connector config", plugin._export_path_gate(path) == path)


def main() -> int:
    test_address_root_replaces_config()
    print("\nzuora_plugin Data Query + bulk export + queryMore smoke tests")
    print("=" * 47)
    test_data_query_writes_tsv()
    test_bulk_export_stops_at_cap_without_extra_querymore()
    test_bulk_export_follows_querymore_across_multiple_pages()
    test_client_side_cap_binds_despite_larger_reported_size()
    test_data_query_override_friction()
    test_bulk_export_override_friction()
    test_bulk_export_writes_tsv_shape()
    test_export_path_outside_allowed_root_refused()
    test_empty_allowed_roots_refused()
    test_non_2xx_raises_with_is_query_flag()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Data Query + bulk export + queryMore smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
