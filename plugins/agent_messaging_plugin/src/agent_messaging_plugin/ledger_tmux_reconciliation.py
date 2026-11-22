"""Bounded, advisory-only ledger/native metadata observations.

The reads are sequential snapshots, never atomic or proof of productive work.
No driver, lifecycle writer, uptake store or host-adoption API is used here.
Reports are derived values: correction is a fresh observation; historical
reports/logs remain unchanged. There is no persistent report lifecycle.
"""

from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .session_lifecycle_store import read_managed_sessions_page
from .tmux_target import pane_target

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

MAX_LEDGER_ROWS = 500
MAX_NATIVE_ROWS = 2000
MAX_OUTPUT_BYTES = 262144
PROBE_TIMEOUT_SECONDS = 5
_FORMAT = "\t".join(f"#{{{name}}}" for name in ("session_id", "session_name", "pane_id", "pane_pid", "pane_dead", "pane_current_command"))
MetadataRunner = Callable[[list[str]], tuple[int, bytes, str]]


@dataclass(frozen=True)
class Pane:
    """Exact metadata fields; a PID is evidence, never an identity join."""

    session_id: str
    session_name: str
    pane_id: str
    pane_pid: str
    pane_dead: str
    pane_current_command: str


@dataclass
class NativeInventory:
    started_at: str
    ended_at: str = ""
    status: str = "ok"
    complete: bool = True
    panes: list[Pane] = field(default_factory=list)
    rejected_rows: list[str] = field(default_factory=list)
    detail: str = ""
    returncode: int | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run_metadata(argv: list[str]) -> tuple[int, bytes, str]:
    """Capture the outcome of one metadata-only reference probe."""
    result = subprocess.run(  # noqa: S603 -- metadata-only argv with an exact target
        argv,
        capture_output=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr.decode("utf-8", errors="replace")


def _unknown(inventory: NativeInventory, detail: str) -> None:
    inventory.status = "probe_unknown"
    inventory.complete = False
    inventory.detail = detail


def _parse_metadata(inventory: NativeInventory, raw: bytes) -> None:
    truncated = len(raw) > MAX_OUTPUT_BYTES
    text = _decode_metadata(inventory, raw[:MAX_OUTPUT_BYTES])
    lines = text.splitlines()
    remaining = MAX_NATIVE_ROWS - len(inventory.panes)
    if truncated or len(lines) > remaining:
        _unknown(inventory, "metadata output/row limit reached; partial_coverage")
    for line in lines[:remaining]:
        cells = line.split("\t")
        if len(cells) != 6:
            inventory.rejected_rows.append(line)
            _unknown(inventory, "malformed metadata; retained bounded raw rows")
            continue
        pane = Pane(*cells)
        inventory.panes.append(pane)
        if not _valid_pane(pane):
            _unknown(inventory, "invalid metadata including pane_dead; retained observed fields")
    keys = [(pane.session_id, pane.pane_id) for pane in inventory.panes]
    if len(keys) != len(set(keys)):
        _unknown(inventory, "duplicate session/pane metadata")


def _decode_metadata(inventory: NativeInventory, raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _unknown(inventory, "malformed UTF-8 metadata; replacement text retained")
        return raw.decode("utf-8", errors="replace")


def _valid_pane(pane: Pane) -> bool:
    return pane.session_id.startswith("$") and pane.session_id[1:].isdigit() and bool(pane.session_name) and pane.pane_id.startswith("%") and pane.pane_id[1:].isdigit() and pane.pane_pid.isdigit() and pane.pane_dead in {"0", "1"}


def _probe_reference(inventory: NativeInventory, ref: str, label: str, runner: MetadataRunner) -> None:
    """Keep the recorded name intact, constructing a target only for tmux's argv."""
    try:
        target = pane_target(ref)
    except ValueError:
        _unknown(inventory, f"{label}: reference is not an exact session name")
        return
    try:
        returncode, raw, stderr = runner(["tmux", "list-panes", "-t", target, "-F", _FORMAT])
    except (OSError, subprocess.TimeoutExpired) as exc:
        if inventory.returncode == 0:
            inventory.returncode = None
        _unknown(inventory, f"{label}: {type(exc).__name__}: metadata probe unavailable")
        return
    _consume_probe(inventory, label, returncode, raw, stderr)


def _consume_probe(inventory: NativeInventory, label: str, returncode: int, raw: bytes, stderr: str) -> None:
    """Known absence preserves complete coverage; other failures retain uncertainty."""
    if returncode == 1 and stderr.startswith(("can't find session", "error connecting to", "no server running")):
        return
    if returncode != 0:
        if inventory.returncode in {0, None}:
            inventory.returncode = returncode
        _unknown(inventory, f"{label}: tmux metadata exited {returncode}")
        return
    previous_detail = inventory.detail
    _parse_metadata(inventory, raw)
    if inventory.detail != previous_detail:
        inventory.detail = f"{label}: {inventory.detail}"


def read_native_inventory(refs: list[str], runner: MetadataRunner | None = None) -> NativeInventory:
    """Probe only recorded references; an empty reference list needs no host read."""
    inventory = NativeInventory(started_at=_now(), returncode=0)
    for index, ref in enumerate(refs, start=1):
        _probe_reference(inventory, ref, f"reference {index}/{len(refs)} {ref!r}", runner or _run_metadata)
    inventory.ended_at = _now()
    return inventory


def _read_host(
    state: StateManagementInterface,
    host: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {
        "host": host,
        "started_at": _now(),
        "complete": False,
        "status": "ok",
        "continuation": None,
        "rows_read": 0,
    }
    # Reserve half the shared budget for each explicit host domain.
    budget = MAX_LEDGER_ROWS // 2
    try:
        while len(rows) < budget:
            page, more = read_managed_sessions_page(
                state,
                {"host": host},
                limit=min(100, budget - len(rows)),
                after=coverage["continuation"],
            )
            rows.extend(page)
            if not more:
                coverage["complete"] = True
                coverage["continuation"] = None
                break
            cursor = [str(page[-1]["created_at"]), str(page[-1]["id"])]
            if cursor == coverage["continuation"]:
                raise ValueError("non-advancing ledger cursor")
            coverage["continuation"] = cursor
    except Exception as exc:  # noqa: BLE001 -- read faults remain advisory evidence
        coverage["status"] = "probe_unknown"
        coverage["detail"] = f"{type(exc).__name__}: {exc}"
    coverage["rows_read"] = len(rows)
    coverage["ended_at"] = _now()
    return rows, coverage


def _session_groups(inventory: NativeInventory) -> dict[str, list[Pane]]:
    sessions: dict[str, list[Pane]] = defaultdict(list)
    for pane in inventory.panes:
        sessions[pane.session_id].append(pane)
    return dict(sorted(sessions.items()))


def _matches(ref: str, panes: list[Pane]) -> bool:
    return any(ref in {pane.session_id, pane.session_name} for pane in panes)


def _row_reason(
    row: dict[str, Any],
    candidates: list[str],
    ambiguous: bool,
    panes: list[Pane],
    inventory: NativeInventory,
    ledger_complete: bool,
) -> str:
    if not row.get("host_ref"):
        return "host_ref_missing"
    if ambiguous:
        return "duplicate_host_ref"
    if not inventory.complete:
        return "probe_unknown"
    if not ledger_complete:
        return "partial_coverage"
    if not candidates:
        return "ledger_tmux_missing"
    if row.get("lifecycle_state") in {"terminated", "retired"}:
        return "terminal_ledger_pane_present"
    if any(pane.pane_dead == "0" for pane in panes):
        return "matched_process_present"
    return "matched_native_pane_dead"


def classify_observations(
    rows: list[dict[str, Any]],
    inventory: NativeInventory,
    *,
    ledger_complete: bool,
) -> list[dict[str, Any]]:
    """Pure exact identity join, retaining ambiguity and every observed pane."""
    sessions = _session_groups(inventory)
    joins = [_join_candidates(row, sessions) for row in rows]
    counts = Counter(sid for candidates in joins for sid in candidates)
    refs = Counter(str(row.get("host_ref") or "") for row in rows)
    observations = []
    for row, candidates in zip(rows, joins, strict=True):
        observations.append(_joined_observation(row, candidates, sessions, counts, refs, inventory, ledger_complete))
    return observations


def _join_candidates(row: dict[str, Any], sessions: dict[str, list[Pane]]) -> list[str]:
    ref = str(row.get("host_ref") or "")
    return [sid for sid, panes in sessions.items() if ref and _matches(ref, panes)]


def _joined_observation(
    row: dict[str, Any],
    candidates: list[str],
    sessions: dict[str, list[Pane]],
    counts: Counter[str],
    refs: Counter[str],
    inventory: NativeInventory,
    ledger_complete: bool,
) -> dict[str, Any]:
    panes = [pane for sid in candidates for pane in sessions[sid]]
    ambiguous = refs[str(row.get("host_ref") or "")] > 1 or len(candidates) > 1 or any(counts[sid] > 1 for sid in candidates)
    return {
        "reason": _row_reason(row, candidates, ambiguous, panes, inventory, ledger_complete),
        "ledger_row": dict(row),
        "session_ids": candidates,
        "panes": [asdict(pane) for pane in panes],
    }


def collect_ledger_tmux_report(
    state: StateManagementInterface | None,
    *,
    runner: MetadataRunner | None = None,
) -> dict[str, Any]:
    """Fresh bounded observations; missing services never create a refusal."""
    started_at = _now()
    rows: list[dict[str, Any]] = []
    coverage = []
    for host in ("tmux", "operator"):
        if state is None:
            page, source = [], {"host": host, "complete": False, "status": "probe_unknown", "detail": "state_service unavailable"}
        else:
            page, source = _read_host(state, host)
        rows.extend(page)
        coverage.append(source)
    refs = list(dict.fromkeys(str(row["host_ref"]) for row in rows if row.get("host_ref")))
    inventory = read_native_inventory(refs, runner)
    complete = all(source["complete"] for source in coverage)
    observations = classify_observations(rows, inventory, ledger_complete=complete)
    advisories = _advisories(coverage, inventory)
    return {
        "started_at": started_at,
        "ended_at": _now(),
        "atomic_snapshot": False,
        "ledger_sources": coverage,
        "native_source": asdict(inventory),
        "observations": observations,
        "advisories": advisories,
        "complete": complete and inventory.complete,
        "limits": {"ledger_rows": MAX_LEDGER_ROWS, "native_rows": MAX_NATIVE_ROWS, "output_bytes": MAX_OUTPUT_BYTES, "timeout_seconds": PROBE_TIMEOUT_SECONDS},
    }


def _advisories(coverage: list[dict[str, Any]], inventory: NativeInventory) -> list[str]:
    reasons = []
    if not inventory.complete or any(source["status"] == "probe_unknown" for source in coverage):
        reasons.append("probe_unknown")
    if not inventory.complete or not all(source["complete"] for source in coverage):
        reasons.append("partial_coverage")
    return reasons
