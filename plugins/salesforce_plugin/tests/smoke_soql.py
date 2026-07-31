#!/usr/bin/env python3
"""SOQL query + export smoke tests for salesforce_plugin.

Hermetic — a ``MagicMock`` standing in for ``SalesforceCliExecutor``
(``run_json`` mocked directly), no live org, no subprocess; the export half
drives the REAL export_containment gate bound to a temp workspace root (the
containment boundary is exactly what must not be mocked).

No manual pagination anymore: `sf data query` autoFetches internally
(verified by reading the CLI's own source — see `soql_actions.py`'s module
docstring), so these tests cover the query-file lifecycle, the
`SF_ORG_MAX_QUERY_LIMIT` env passthrough, the client-side cap logic, the A4
fail-loud overflow contract (the over-byte-cap construction SPILLED to a
blob before 2026-07-16), and the A3 workspace-TSV export contract.

Exercises:
  1. soql_query — inline records, attributes key stripped, total_size/row_count
  2. soql_query — writes the query text to a tempfile, passes `--file <path>`,
     and cleans the tempfile up afterward
  3. soql_query — passes SF_ORG_MAX_QUERY_LIMIT as an env override matching
     max_records (server-side cap)
  4. soql_query — client-side slice still caps at max_records even if the
     executor returns more (defense-in-depth against a server-side cap miss)
  5. soql_query — max_records clamped to the 1000 hard cap
  6. red-first: soql_query over the byte cap -> ResultTooLargeError naming
     export_soql (no blob spill)
  7. export_soql — writes a TSV at the caller's absolute path under the
     allowed root; column order follows first appearance; nested objects
     serialize as JSON text; returns {path, columns, row_count, total_size,
     truncated}
  8. red-first: export path OUTSIDE every allowed root -> ExportPathRefusedError,
     no file written, the query never runs
  9. red-first: EMPTY export_allowed_roots -> refused naming the config key
  10. red-first: RELATIVE or BLANK configured root -> refused as misconfigured
  11. red-first: export_soql truncated=True on done=false ALONE (totalSize equal
      to the fetched count — Salesforce's own incompleteness signal wins)
  12. export_soql truncated flag set when the org reports more rows than fetched

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 \
        plugins/salesforce_plugin/tests/smoke_soql.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "salesforce_plugin" / "src"))

from salesforce_plugin import soql_actions  # noqa: E402
from salesforce_plugin.constants import CONFIG_KEY_EXPORT_ALLOWED_ROOTS  # noqa: E402
from salesforce_plugin.export_containment import (  # noqa: E402
    ExportPathRefusedError,
    assert_export_path_allowed,
)

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _gate_for(roots: list[str]) -> Any:
    """The REAL containment gate bound to the given allowed roots."""

    def gate(output_tsv_path: str) -> str:
        return assert_export_path_allowed(
            output_tsv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name="salesforce_plugin",
        )

    return gate


def test_soql_query_inline() -> None:
    executor = MagicMock()
    executor.run_json.return_value = {
        "totalSize": 2,
        "done": True,
        "records": [
            {"attributes": {"type": "Account"}, "Id": "001x1", "Name": "Acme"},
            {"attributes": {"type": "Account"}, "Id": "001x2", "Name": "Globex"},
        ],
    }
    result = soql_actions.soql_query(executor, {"query": "SELECT Id, Name FROM Account"})
    _assert("attributes key stripped", "attributes" not in result["records"][0])
    _assert("record fields carried", result["records"][0]["Name"] == "Acme")
    _assert("total_size carried", result["total_size"] == 2)
    _assert("row_count matches", result["row_count"] == 2)
    _assert("not spilled", result["spilled"] is False)


def test_query_file_written_and_cleaned_up() -> None:
    executor = MagicMock()
    executor.run_json.return_value = {"totalSize": 0, "done": True, "records": []}
    written_path: str = ""

    def _capture_run_json(argv_tail: list[str], *, env_overrides: dict[str, str] | None = None) -> dict[str, Any]:
        nonlocal written_path
        file_flag_index = argv_tail.index("--file")
        written_path = argv_tail[file_flag_index + 1]
        _assert("query file exists during the call", Path(written_path).exists())
        _assert("query file carries the SOQL text", Path(written_path).read_text() == "SELECT Id FROM Account")
        return {"totalSize": 0, "done": True, "records": []}

    executor.run_json.side_effect = _capture_run_json
    soql_actions.soql_query(executor, {"query": "SELECT Id FROM Account"})
    _assert("query file cleaned up after the call", not Path(written_path).exists())


def test_max_query_limit_env_passthrough() -> None:
    executor = MagicMock()
    executor.run_json.return_value = {"totalSize": 0, "done": True, "records": []}
    soql_actions.soql_query(executor, {"query": "SELECT Id FROM Account", "max_records": 50})
    kwargs = executor.run_json.call_args.kwargs
    _assert(
        "SF_ORG_MAX_QUERY_LIMIT env override matches max_records",
        kwargs.get("env_overrides") == {"SF_ORG_MAX_QUERY_LIMIT": "50"},
        str(kwargs),
    )


def test_client_side_cap_defense_in_depth() -> None:
    executor = MagicMock()
    # Simulate the executor/CLI returning MORE than max_records (e.g. a server-side
    # cap miss) — the client-side slice must still enforce the cap.
    executor.run_json.return_value = {
        "totalSize": 10,
        "done": True,
        "records": [{"attributes": {}, "Id": str(i)} for i in range(10)],
    }
    result = soql_actions.soql_query(executor, {"query": "SELECT Id FROM Account", "max_records": 3})
    _assert("client-side slice caps at max_records", result["row_count"] == 3, result["row_count"])
    _assert("total_size still reflects the true total", result["total_size"] == 10, result["total_size"])


def test_max_records_clamp() -> None:
    executor = MagicMock()
    executor.run_json.return_value = {
        "totalSize": 10,
        "done": True,
        "records": [{"attributes": {}, "Id": str(i)} for i in range(10)],
    }
    soql_actions.soql_query(executor, {"query": "SELECT Id FROM Account", "max_records": 999999})
    kwargs = executor.run_json.call_args.kwargs
    _assert(
        "max_records clamps to the 1000 hard cap",
        kwargs.get("env_overrides") == {"SF_ORG_MAX_QUERY_LIMIT": "1000"},
        str(kwargs),
    )


def test_soql_query_over_byte_cap_fails_loud() -> None:
    # A4 red-first: this construction SPILLED to a blob before 2026-07-16.
    executor = MagicMock()
    big_value = "x" * 5000
    executor.run_json.return_value = {
        "totalSize": 100,
        "done": True,
        "records": [{"attributes": {}, "Id": str(i), "Blob": big_value} for i in range(100)],
    }
    raised = None
    try:
        soql_actions.soql_query(executor, {"query": "SELECT Id, Blob FROM Account", "max_records": 100})
    except soql_actions.ResultTooLargeError as exc:
        raised = exc
    _assert("over-byte-cap read fails loud (no blob spill)", raised is not None)
    _assert(
        "overflow message points at export_soql",
        raised is not None and "export_soql" in str(raised),
    )


def test_export_soql_to_workspace() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_export_smoke_") as workspace:
        executor = MagicMock()
        executor.run_json.return_value = {
            "totalSize": 2,
            "done": True,
            "records": [
                {"attributes": {}, "Id": "001x1", "Name": "Acme", "Owner": {"Name": "Pat"}},
                {"attributes": {}, "Id": "001x2", "Name": "Globex", "AnnualRevenue": 5},
            ],
        }
        out_path = str(Path(workspace) / "accounts.tsv")
        result = soql_actions.export_soql(
            executor,
            {"query": "SELECT Id, Name FROM Account", "output_tsv_path": out_path},
            _gate_for([workspace]),
        )
        _assert("export returns the written path", result["path"] == str(Path(out_path).resolve()))
        _assert("export row_count", result["row_count"] == 2)
        _assert(
            "columns follow first appearance across records",
            result["columns"] == ["Id", "Name", "Owner", "AnnualRevenue"],
            str(result["columns"]),
        )
        _assert("export not truncated", result["truncated"] is False)
        lines = Path(result["path"]).read_text(encoding="utf-8").splitlines()
        _assert("tsv header row is tab-separated", lines[0] == "Id\tName\tOwner\tAnnualRevenue")
        _assert(
            "nested relationship object serialized as JSON text",
            '""Name"": ""Pat""' in lines[1],  # csv-quoted JSON cell in the TSV
            lines[1],
        )
        _assert("export cap passed as the env override",
                executor.run_json.call_args.kwargs.get("env_overrides")
                == {"SF_ORG_MAX_QUERY_LIMIT": "50000"})


def test_export_refused_outside_root() -> None:
    with (
        tempfile.TemporaryDirectory(prefix="sf_root_") as workspace,
        tempfile.TemporaryDirectory(prefix="sf_outside_") as outside,
    ):
        executor = MagicMock()
        out_path = str(Path(outside) / "escape.tsv")
        raised = None
        try:
            soql_actions.export_soql(
                executor,
                {"query": "SELECT Id FROM Account", "output_tsv_path": out_path},
                _gate_for([workspace]),
            )
        except ExportPathRefusedError as exc:
            raised = exc
        _assert("outside-root path is refused", raised is not None)
        _assert("no file written on refusal", not Path(out_path).exists())
        _assert("query never runs on refusal (gate runs first)", not executor.run_json.called)


def test_export_refused_empty_roots() -> None:
    executor = MagicMock()
    raised = None
    try:
        soql_actions.export_soql(
            executor,
            {"query": "SELECT Id FROM Account", "output_tsv_path": "/tmp/anywhere.tsv"},
            _gate_for([]),
        )
    except ExportPathRefusedError as exc:
        raised = exc
    _assert("empty export_allowed_roots refuses (refuse-all default)", raised is not None)
    _assert(
        "refusal names the config key",
        raised is not None and CONFIG_KEY_EXPORT_ALLOWED_ROOTS in str(raised),
    )


def test_export_refused_misconfigured_roots() -> None:
    executor = MagicMock()
    cwd_relative_target = str(Path.cwd() / "relative-root" / "out.tsv")
    for bad_roots, label in (
        (["relative-root"], "relative configured root is refused"),
        ([""], "blank configured root is refused"),
    ):
        raised = None
        try:
            soql_actions.export_soql(
                executor,
                {"query": "SELECT Id FROM Account", "output_tsv_path": cwd_relative_target},
                _gate_for(bad_roots),
            )
        except ExportPathRefusedError as exc:
            raised = exc
        _assert(label, raised is not None)
        _assert(
            f"{label}: message names the offending entry as misconfigured",
            raised is not None and "misconfigured" in str(raised),
        )


def test_export_truncated_on_done_false_alone() -> None:
    # Codex Wave-3 A3-1 red-first: done=false with totalSize EQUAL to the
    # fetched count must still flag truncation — Salesforce's own
    # incompleteness signal wins even when the counts look complete.
    with tempfile.TemporaryDirectory(prefix="sf_root_") as workspace:
        executor = MagicMock()
        executor.run_json.return_value = {
            "totalSize": 1,
            "done": False,
            "records": [{"attributes": {}, "Id": "001x1"}],
        }
        out_path = str(Path(workspace) / "done_false.tsv")
        result = soql_actions.export_soql(
            executor,
            {"query": "SELECT Id FROM Account", "output_tsv_path": out_path},
            _gate_for([workspace]),
        )
        _assert("truncated True on done=false alone", result["truncated"] is True)


def test_export_truncated_when_org_reports_more() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_root_") as workspace:
        executor = MagicMock()
        executor.run_json.return_value = {
            "totalSize": 99999,  # org reports more rows than were fetched
            "done": False,
            "records": [{"attributes": {}, "Id": "001x1"}],
        }
        out_path = str(Path(workspace) / "partial.tsv")
        result = soql_actions.export_soql(
            executor,
            {"query": "SELECT Id FROM Account", "output_tsv_path": out_path},
            _gate_for([workspace]),
        )
        _assert("truncated True when total_size exceeds fetched rows", result["truncated"] is True)


def main() -> int:
    print("\nsalesforce_plugin SOQL query/export smoke tests")
    print("=" * 47)
    test_soql_query_inline()
    test_query_file_written_and_cleaned_up()
    test_max_query_limit_env_passthrough()
    test_client_side_cap_defense_in_depth()
    test_max_records_clamp()
    test_soql_query_over_byte_cap_fails_loud()
    test_export_soql_to_workspace()
    test_export_refused_outside_root()
    test_export_refused_empty_roots()
    test_export_refused_misconfigured_roots()
    test_export_truncated_on_done_false_alone()
    test_export_truncated_when_org_reports_more()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All SOQL query/export smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
