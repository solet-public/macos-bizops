#!/usr/bin/env python3
"""Inline-cap + export smoke tests for snowflake_plugin.

Hermetic — a faked cursor for the run_query half (no live warehouse), and
the REAL export_containment gate bound to a temp workspace root for the
export half (the containment boundary is exactly what must not be mocked).
Red-first: the A4 fail-loud overflow contract (this construction SPILLED to
a blob before 2026-07-16) and the workspace-TSV export contract (A2) —
containment refusals fire BEFORE the statement executes, and the refuse-all
empty-config default is real, not vacuous.

Exercises:
  1. red-first: run_query over the effective ROW cap (driver over-returns the
     default 200) -> ResultTooLargeError naming export_query (no blob spill)
  1b. red-first: max_rows=1000 with 662 rows returns INLINE (live defect
     2026-07-16 — the row bound was wrongly the 200 default, not the caller's cap)
  1c. red-first: initialize() binds config_provider so a configured
     export_allowed_roots ADMITS its paths (live defect 2026-07-16 — provider
     was never bound; config silently ignored, refuse-all)
  1d. fast-fail: unbound config_provider (initialize skipped) raises a config
     fault naming initialize(), never silent refuse-all
  2. export_query writes a TSV at the caller's absolute path under the allowed
     root; returns {path, columns, row_count, truncated}
  3. red-first: path OUTSIDE every allowed root -> ExportPathRefusedError, no
     file written, no statement executed
  4. red-first: EMPTY export_allowed_roots -> refused, message names the config key
  5. red-first: RELATIVE or BLANK configured root -> refused as misconfigured
     (a relative/blank root would realpath against the server process cwd)
  6. red-first: relative path -> refused
  7. red-first: non-.tsv suffix -> refused
  8. missing parent directory -> ValueError (invalid_params class)
  9. export_query flags truncated when the row count hits EXPORT_ROW_CAP

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 \
        plugins/snowflake_plugin/tests/smoke_spill.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "snowflake_plugin" / "src"))

from snowflake_plugin import query_actions  # noqa: E402
from snowflake_plugin.constants import (  # noqa: E402
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    INLINE_ROW_CAP_DEFAULT,
)
from snowflake_plugin.export_containment import (  # noqa: E402
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


def _fake_conn(columns: list[str], rows: list[tuple[Any, ...]]) -> Any:
    cur = MagicMock()
    cur.description = [(c,) for c in columns]
    cur.fetchmany.return_value = rows
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn


def _gate_for(roots: list[str]) -> Any:
    """The REAL containment gate bound to the given allowed roots."""

    def gate(output_tsv_path: str) -> str:
        return assert_export_path_allowed(
            output_tsv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name="snowflake_plugin",
        )

    return gate


def test_run_query_over_row_cap_fails_loud() -> None:
    # A4 red-first: this construction SPILLED to a blob before 2026-07-16.
    # The row belt fires against the EFFECTIVE cap (default 200 here): the fake
    # driver over-returns fetchmany(200) with 201 rows.
    rows = [(i,) for i in range(INLINE_ROW_CAP_DEFAULT + 1)]
    conn = _fake_conn(["id"], rows)
    raised = None
    try:
        query_actions.run_query(conn, {"sql": "SELECT id FROM t"})
    except query_actions.ResultTooLargeError as exc:
        raised = exc
    _assert("over-row-cap read fails loud (no blob spill)", raised is not None)
    _assert(
        "overflow message points at export_query",
        raised is not None and "export_query" in str(raised),
    )


def test_run_query_max_rows_honored_inline() -> None:
    # Red-first (live defect 2026-07-16, Dusk repro ae-2mt6nqopuw5p7): a legal
    # max_rows=1000 with 662 fetched rows failed loud against the 200 DEFAULT.
    # The row bound must be the caller's effective max_rows.
    rows = [(i,) for i in range(662)]
    conn = _fake_conn(["id"], rows)
    result = query_actions.run_query(conn, {"sql": "SELECT id FROM t", "max_rows": 1000})
    _assert("662 rows inline under max_rows=1000", result["row_count"] == 662)
    _assert("not spilled", result["spilled"] is False)


def test_export_gate_binds_via_initialize() -> None:
    # Red-first (live defect 2026-07-16, Dusk repro ae-2mt8548ki6jb7): the plugin
    # never overrode initialize(), so config_provider stayed None and a
    # configured export_allowed_roots was silently ignored (refuse-all).
    from snowflake_plugin.plugin import SnowflakePlugin  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="snw_gate_smoke_") as workspace:
        plugin = SnowflakePlugin()
        plugin.initialize({CONFIG_KEY_EXPORT_ALLOWED_ROOTS: [workspace]})
        admitted = plugin._export_path_gate(str(Path(workspace) / "out.tsv"))
        _assert(
            "initialize() binds config; configured root admits the path",
            admitted.endswith("out.tsv"),
        )


def test_export_gate_unbound_provider_fails_loud() -> None:
    # Fast-fail: a plugin whose lifecycle skipped initialize() must raise a
    # config fault naming the missing binding — never silently refuse-all
    # (the pre-2026-07-16 'or {}' behavior this replaces).
    from snowflake_plugin.app_config import SnowflakeConfigError  # noqa: PLC0415
    from snowflake_plugin.plugin import SnowflakePlugin  # noqa: PLC0415

    plugin = SnowflakePlugin()
    raised = None
    try:
        plugin._export_path_gate("/tmp/out.tsv")
    except SnowflakeConfigError as exc:
        raised = exc
    _assert("unbound config_provider raises a config fault", raised is not None)
    _assert(
        "fault names initialize()",
        raised is not None and "initialize" in str(raised),
    )


def test_export_tsv_to_workspace() -> None:
    with tempfile.TemporaryDirectory(prefix="snw_export_smoke_") as workspace:
        conn = _fake_conn(["id", "name"], [(1, "alice"), (2, "bob")])
        out_path = str(Path(workspace) / "warehouse_rows.tsv")
        result = query_actions.export_query(
            conn,
            {"sql": "SELECT id, name FROM t", "output_tsv_path": out_path},
            _gate_for([workspace]),
        )
        _assert("export returns the written path", result["path"] == str(Path(out_path).resolve()))
        _assert("export row_count", result["row_count"] == 2)
        _assert("export columns", result["columns"] == ["id", "name"])
        _assert("export not truncated", result["truncated"] is False)
        lines = Path(result["path"]).read_text(encoding="utf-8").splitlines()
        _assert("tsv header row is tab-separated", lines[0] == "id\tname")
        _assert("tsv data rows", "1\talice" in lines and "2\tbob" in lines)
        _assert("no blob key in the result", "result_blob_key" not in result)


def test_export_refused_outside_root() -> None:
    with (
        tempfile.TemporaryDirectory(prefix="snw_root_") as workspace,
        tempfile.TemporaryDirectory(prefix="snw_outside_") as outside,
    ):
        conn = _fake_conn(["id"], [(1,)])
        out_path = str(Path(outside) / "escape.tsv")
        raised = None
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": out_path},
                _gate_for([workspace]),
            )
        except ExportPathRefusedError as exc:
            raised = exc
        _assert("outside-root path is refused", raised is not None)
        _assert("no file written on refusal", not Path(out_path).exists())
        _assert(
            "statement never executed on refusal (gate runs first)",
            not conn.cursor.called,
        )


def test_export_refused_empty_roots() -> None:
    conn = _fake_conn(["id"], [(1,)])
    raised = None
    try:
        query_actions.export_query(
            conn,
            {"sql": "SELECT id FROM t", "output_tsv_path": "/tmp/anywhere.tsv"},
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
    # Codex-proven hole (2026-07-15 Wave-2 review): a RELATIVE configured root
    # realpaths against the SERVER process cwd and admitted paths under it; a
    # BLANK root realpaths TO the cwd. Both must be refused as config faults.
    conn = _fake_conn(["id"], [(1,)])
    cwd_relative_target = str(Path.cwd() / "relative-root" / "out.tsv")
    for bad_roots, label in (
        (["relative-root"], "relative configured root is refused"),
        ([""], "blank configured root is refused"),
    ):
        raised = None
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": cwd_relative_target},
                _gate_for(bad_roots),
            )
        except ExportPathRefusedError as exc:
            raised = exc
        _assert(label, raised is not None)
        _assert(
            f"{label}: message names the offending entry as misconfigured",
            raised is not None and "misconfigured" in str(raised),
        )


def test_export_refused_relative_path() -> None:
    with tempfile.TemporaryDirectory(prefix="snw_root_") as workspace:
        conn = _fake_conn(["id"], [(1,)])
        raised = False
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": "exports/out.tsv"},
                _gate_for([workspace]),
            )
        except ExportPathRefusedError:
            raised = True
        _assert("relative output_tsv_path is refused", raised)


def test_export_refused_wrong_suffix() -> None:
    with tempfile.TemporaryDirectory(prefix="snw_root_") as workspace:
        conn = _fake_conn(["id"], [(1,)])
        raised = False
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": str(Path(workspace) / "out.csv")},
                _gate_for([workspace]),
            )
        except ExportPathRefusedError:
            raised = True
        _assert("non-.tsv suffix is refused", raised)


def test_export_missing_parent_raises() -> None:
    with tempfile.TemporaryDirectory(prefix="snw_root_") as workspace:
        conn = _fake_conn(["id"], [(1,)])
        out_path = str(Path(workspace) / "no_such_dir" / "out.tsv")
        raised = False
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": out_path},
                _gate_for([workspace]),
            )
        except ValueError:
            raised = True
        _assert("missing parent directory raises ValueError", raised)


def test_export_truncated_flag() -> None:
    original = query_actions.EXPORT_ROW_CAP
    query_actions.EXPORT_ROW_CAP = 2  # type: ignore[misc]
    try:
        with tempfile.TemporaryDirectory(prefix="snw_root_") as workspace:
            conn = _fake_conn(["id"], [(1,), (2,)])  # fetch returns exactly the cap
            out_path = str(Path(workspace) / "capped.tsv")
            result = query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": out_path},
                _gate_for([workspace]),
            )
            _assert("truncated True when the cap is hit", result["truncated"] is True)
    finally:
        query_actions.EXPORT_ROW_CAP = original  # type: ignore[misc]


def main() -> int:
    print("\nsnowflake_plugin spill/export smoke tests")
    print("=" * 42)
    test_run_query_over_row_cap_fails_loud()
    test_run_query_max_rows_honored_inline()
    test_export_gate_binds_via_initialize()
    test_export_gate_unbound_provider_fails_loud()
    test_export_tsv_to_workspace()
    test_export_refused_outside_root()
    test_export_refused_empty_roots()
    test_export_refused_misconfigured_roots()
    test_export_refused_relative_path()
    test_export_refused_wrong_suffix()
    test_export_missing_parent_raises()
    test_export_truncated_flag()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All spill/export smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
