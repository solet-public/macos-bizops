"""One frozen selection/run/proof contract shared by library, CLI and service.

The existing selector's rules remain a union. Base edges cover deletions;
candidate edges add resource and dynamic-import consumers. Unknown inputs are
disclosed and run without cache. No request-supplied argv is executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from quality_gates.focused_smoke_selector import REGISTER_RELPATH, select_focused_smokes
from quality_gates.gate_smoke_register import load_from_sources as _load_register_sources
from quality_gates.smoke_identity import (
    REPOSITORY_SCAN,
    RULE_VERSION,
    SCHEMA,
    FrozenTree,
    closure,
    dependency_graph,
    digest,
    freeze_request,
    read_sources,
    safe_path,
    tool_profile,
    validate_request,
    verify_worktree,
)
from quality_gates.smoke_pass_store import SmokePassStore

# The runner starts a retained stream with this marker when its collector
# kept only the stream's tail. Skip detection cannot read omitted bytes, so
# such an observation is partial and never reused.
TRUNCATED_OUTPUT_MARKER = "OUTPUT-TRUNCATED:"


def _rules(root: Path, sources: Mapping[str, bytes], changed: list[str], named: list[str]) -> dict[str, list[dict[str, str]]]:
    selection = select_focused_smokes(root, changed, named, source_files=sources)
    return {smoke: [{"rule": reason.rule, "detail": reason.detail} for reason in reasons] for smoke, reasons in selection.reasons.items()}


def _transitive_reasons(graph: Mapping[str, set[str]], smoke: str, changed: set[str], source_tree: str = "candidate") -> list[dict[str, Any]]:
    parents = _input_parents(graph, smoke)
    inputs = set(parents)
    affected = changed if REPOSITORY_SCAN in inputs else changed.intersection(inputs)
    return [
        {"rule": "input_closure", "detail": f"{smoke} transitively consumes {path}", "changed_path": path, "source_tree": source_tree, "import_chain": _input_chain(parents, path if path in parents else REPOSITORY_SCAN), "input_kind": "closure" if path in parents else "repository_manifest"} for path in sorted(affected)
    ]


def _input_parents(graph: Mapping[str, set[str]], smoke: str) -> dict[str, str | None]:
    parents: dict[str, str | None] = {smoke: None}
    pending = deque([smoke])
    while pending:
        owner = pending.popleft()
        for path in sorted(graph.get(owner, set())):
            if path not in parents:
                parents[path] = owner
                pending.append(path)
    return parents


def _input_chain(parents: Mapping[str, str | None], path: str) -> list[str]:
    chain: list[str] = []
    current: str | None = path
    while current is not None:
        chain.append(current)
        current = parents[current]
    return list(reversed(chain))


def _identity_row(smoke: str, tree: FrozenTree, graph: Mapping[str, set[str]], unknown: Mapping[str, list[str]], profile: Mapping[str, Any]) -> dict[str, Any]:
    paths = closure(graph, smoke)
    inputs = {path: tree.entries[path] for path in paths if path in tree.entries}
    notices = [f"{path}: {reason}" for path in paths for reason in unknown.get(path, [])]
    notices.extend(_opaque_mode_advisories(inputs, profile))
    repository_scan = REPOSITORY_SCAN in paths
    manifest = tree.manifest_sha256 if repository_scan else None
    if repository_scan:
        notices.append("repository scanner external/dynamic resource identity is unmeasured; complete tree manifest retained")
    if any("postgres" in path.lower() for path in paths) and not profile.get("external_fixture_identity"):
        notices.append("external PostgreSQL fixture identity unmeasured")
    key = digest({"schema": SCHEMA, "smoke": smoke, "inputs": inputs, "repository_manifest_sha256": manifest, "profile": profile})
    return {"smoke": smoke, "identity": tree.entries[smoke], "inputs": inputs, "repository_manifest_sha256": manifest, "profile_sha256": digest(profile), "key": key, "identity_complete": not notices, "identity_advisories": notices}


def _opaque_mode_advisories(inputs: Mapping[str, Any], profile: Mapping[str, Any]) -> list[str]:
    return sorted({f"{path}: opaque symlink/submodule target identity is unmeasured; execute uncached" for source in (inputs, profile.get("inputs", {})) for path, row in source.items() if row.get("mode") not in ("100644", "100755")})


def select_smokes(root: Path, request: Mapping[str, Any], *, store: SmokePassStore | None = None) -> dict[str, Any]:
    """Compute frozen union with exact key/reuse reasons; executes no smoke."""
    base, candidate = validate_request(root, request)
    old, sources = read_sources(root, base), read_sources(root, candidate)
    changed = [entry["path"] for entry in request["delta_entries"]]
    named = [safe_path(path) for path in request.get("named_smokes", [])]
    graph, unknown = dependency_graph(sources, candidate.entries)
    old_graph, _ = dependency_graph(old, base.entries)
    # The EFFECTIVE register: gate_smokes.txt plus every fragment in the frozen
    # tree, so a fragment-registered smoke is part of the selection.
    registered = _load_register_sources(sources, REGISTER_RELPATH).entries
    reasons = _selection_reasons(root, sources, old, changed, named, registered, graph, old_graph)
    profile = tool_profile(root, candidate)
    profile["repository_id"] = request["repository_id"]
    rows = _selected_rows(registered, reasons, candidate, graph, unknown, profile, store or SmokePassStore(), request.get("known_failures", {}))
    register = "".join(row["smoke"] + "\n" for row in rows)
    mapped = _mapped_paths(rows)
    response = {
        "schema": SCHEMA,
        "rule_version": RULE_VERSION,
        "request": dict(request),
        "profile": profile,
        "profile_sha256": digest(profile),
        "register": register,
        "register_sha256": hashlib.sha256(register.encode()).hexdigest(),
        "selected": rows,
        "repository_inputs": _repository_inputs(rows, candidate),
        "unmapped": sorted(set(changed) - mapped),
        "advisories": [f"{path}: {notice}" for path, notices in unknown.items() for notice in notices],
        "hub_full_required": _hub_changed(changed),
    }
    response["selection_sha256"] = digest(response)
    return response


def _mapped_paths(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    return {row["changed_path"] for selected in rows for row in selected["reasons"] if "changed_path" in row}


def _repository_inputs(rows: Sequence[Mapping[str, Any]], candidate: FrozenTree) -> dict[str, Any]:
    return candidate.entries if any(row["repository_manifest_sha256"] for row in rows) else {}


def _executing_paths(selection: Mapping[str, Any]) -> list[str]:
    return sorted({path for row in selection["selected"] for path in row["inputs"]} | set(selection["profile"]["inputs"]) | set(selection["repository_inputs"]))


def _selection_reasons(root: Path, sources: Mapping[str, bytes], old: Mapping[str, bytes], changed: list[str], named: list[str], registered: Sequence[str], graph: Mapping[str, set[str]], old_graph: Mapping[str, set[str]]) -> dict[str, list[dict[str, str]]]:
    reasons = _rules(root, sources, changed, named)
    for smoke, previous in _rules(root, old, changed, named).items():
        reasons.setdefault(smoke, []).extend(previous)
    for smoke in registered:
        additions = _transitive_reasons(graph, smoke, set(changed)) + _transitive_reasons(old_graph, smoke, set(changed), "base")
        if additions:
            reasons.setdefault(smoke, []).extend(additions)
    return reasons


def _selected_rows(registered: Sequence[str], reasons: Mapping[str, list[dict[str, str]]], candidate: FrozenTree, graph: Mapping[str, set[str]], unknown: Mapping[str, list[str]], profile: Mapping[str, Any], store: SmokePassStore, known_failures: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for smoke in sorted(set(registered) & reasons.keys()):
        if smoke not in candidate.entries:
            continue
        row = _identity_row(smoke, candidate, graph, unknown, profile)
        proof = store.get(row["key"])
        reusable = _reusable(row, proof, smoke in known_failures)
        row["known_failure_ref"] = known_failures.get(smoke)
        row.update(reasons=sorted({json.dumps(reason, sort_keys=True) for reason in reasons[smoke]}), reusable=reusable, reuse_reason="exact clean content key" if reusable else "no current clean fully identified observation", proof_version=proof["version"], proof_event_id=proof["observation_event_id"])
        row["reasons"] = [json.loads(reason) for reason in row["reasons"]]
        rows.append(row)
    return rows


def _reusable(row: Mapping[str, Any], proof: Mapping[str, Any], known_failure: bool) -> bool:
    return row["identity_complete"] and proof["reusable"] and not proof["retired"] and not known_failure


def _hub_changed(paths: Sequence[str]) -> bool:
    return any(path in ("quality_gates/run_smokes.py", "quality_gates/run_smokes_scratch.py") or "provision" in path or "schema_engine" in path or "schema_apply" in path for path in paths)


def _observation(row: Mapping[str, Any], result: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    attempts = result.get("attempts", [])
    output = "\n".join(str(attempt.get("output", "")) for attempt in attempts)
    skipped = result.get("verdict") == "skipped" or bool(re.search(r"\bSKIP(?:PED)?\b|\b0 (?:passed|tests? run)\b|\bRan 0 tests?\b|\bcollected 0 items\b|\bno tests ran\b", output, re.IGNORECASE))
    return {
        "key": row["key"],
        "smoke": row["smoke"],
        "identity": row["identity"],
        "inputs": row["inputs"],
        "profile_sha256": row["profile_sha256"],
        "repository_manifest_sha256": row["repository_manifest_sha256"],
        "started_event_id": row.get("started_event_id"),
        "verdict": result.get("verdict", "not_run"),
        "exit_code": 0 if result.get("verdict") == "passed" else None,
        "complete": bool(attempts) and result.get("verdict") != "not_run",
        "identity_complete": row["identity_complete"] and (result.get("import_environment") or {}).get("state") == "verified",
        "attempt_count": len(attempts),
        "skipped": skipped,
        "harness_defect": result.get("harness_defect"),
        "partial": TRUNCATED_OUTPUT_MARKER in output,
        "timed_out": "timed out" in output.lower(),
        "known_failure": row["smoke"] in request.get("known_failures", {}),
        "attempts": attempts,
        "output_sha256": digest(output),
        "attribution": request.get("attribution", {}),
        "candidate_commit": request["candidate_commit"],
        "candidate_tree": request["candidate_tree"],
        "seconds": result.get("seconds"),
    }


def run_selected_smokes(root: Path, request: Mapping[str, Any], *, principal: str, store: SmokePassStore | None = None, executor: Callable[[list[str]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Recompute selection, consume current clean rows, run every miss, append."""
    from quality_gates.run_smokes import execute_selected

    proofs = store or SmokePassStore()
    proofs._authorize(principal)
    selection = select_smokes(root, request, store=proofs)
    _, candidate = validate_request(root, request)
    paths = _executing_paths(selection)
    verify_worktree(root, candidate, paths)
    pending = [row["smoke"] for row in selection["selected"] if not row["reusable"]]
    runtime_rows = [dict(row) for row in selection["selected"]]
    _begin_proofs(proofs, runtime_rows, principal)
    observed = (executor or execute_selected)(pending)
    verify_worktree(root, candidate, paths)
    current_profile = {**tool_profile(root, candidate), "repository_id": request["repository_id"]}
    if digest(current_profile) != selection["profile_sha256"]:
        raise ValueError("effective tool/import profile changed before proof publication")
    rows = _publish_proofs(proofs, runtime_rows, observed, request, principal)
    return {
        "schema": SCHEMA,
        "selection": selection,
        "status": "no_tests" if not rows else "completed",
        "reason": "empty computed selection reached runner" if not rows else "computed union executed with clean-only reuse",
        "selected_count": len(rows),
        "executed_count": len(pending),
        "reused_count": len(rows) - len(pending),
        "run": observed,
        "proofs": rows,
        "counts": _proof_counts(rows),
    }


def _proof_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    observations = [row["proof"].get("observation") or {} for row in rows]
    return {"known_failure": sum(bool(row.get("known_failure")) for row in observations), "skipped": sum(bool(row.get("skipped")) for row in observations), "harness_defect": sum(bool(row.get("harness_defect")) for row in observations), "clean_reusable": sum(bool(row["proof"]["reusable"]) for row in rows)}


def _publish_proofs(proofs: SmokePassStore, selected: Sequence[Mapping[str, Any]], observed: Mapping[str, Any], request: Mapping[str, Any], principal: str) -> list[dict[str, Any]]:
    """Each observation says whether its run listed it in ``harness_defects``; null when the run's list is unknown."""
    harness = observed.get("harness_defects")
    rows: list[dict[str, Any]] = []
    for row in selected:
        if row["reusable"]:
            current = proofs.get(row["key"])
            if current["version"] != row["proof_version"] or not current["reusable"]:
                raise ValueError("proof changed during consumption; recompute selection")
            rows.append({"smoke": row["smoke"], "reused": True, "proof": current})
        else:
            result = {**observed.get("smoke_results", {}).get(row["smoke"], {}), "import_environment": observed.get("import_environment", {}), "harness_defect": None if harness is None else row["smoke"] in harness}
            observation = _observation(row, result, request)
            observation["raw_attempt_path"] = proofs.retain_attempt(observation, principal=principal)
            proof = proofs.observe(row["key"], observation, expected_version=row["proof_version"], principal=principal)
            rows.append({"smoke": row["smoke"], "reused": False, "proof": proof})
    return rows


def _begin_proofs(proofs: SmokePassStore, selected: Sequence[dict[str, Any]], principal: str) -> None:
    for row in selected:
        if not row["reusable"]:
            started = proofs.begin(row["key"], {"smoke": row["smoke"], "inputs": row["inputs"], "profile_sha256": row["profile_sha256"]}, expected_version=row["proof_version"], principal=principal)
            row.update(proof_version=started["version"], started_event_id=started["active_attempt"])


def prepare_full_proofs(root: Path, commit: str, *, principal: str, attribution: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze every register row and begin attempts before scheduled Tier F.

    Existing clean pointers are deliberately ignored: this is physical full
    sampling. The normal candidate/full-battery receipt remains independent.
    """
    from quality_gates.smoke_identity import read_tree

    tree = read_tree(root, commit)
    sources = read_sources(root, tree)
    names = _load_register_sources(sources, REGISTER_RELPATH).entries
    request = freeze_request(root, tree.commit, tree.commit, repository_id="rep_7111540c-ac4a-468c-8f84-b67d8756d1d0", named_smokes=names, attribution=attribution)
    selection = select_smokes(root, request)
    runtime_rows = [{**row, "reusable": False} for row in selection["selected"]]
    if {row["smoke"] for row in runtime_rows} != set(names):
        raise ValueError("full register includes an unmeasurable smoke identity")
    _begin_proofs(SmokePassStore(), runtime_rows, principal)
    return {"request": request, "selection": selection, "runtime_rows": runtime_rows, "cache_ignored": True}


def finish_full_proofs(root: Path, context: Mapping[str, Any], result: Mapping[str, Any], import_environment: Mapping[str, Any], *, principal: str) -> list[dict[str, Any]]:
    """Publish measured full rows only after final bytes/profile revalidation."""
    _, tree = validate_request(root, context["request"])
    verify_worktree(root, tree, list(tree.entries))
    profile = {**tool_profile(root, tree), "repository_id": context["request"]["repository_id"]}
    if digest(profile) != context["selection"]["profile_sha256"]:
        raise ValueError("full campaign tool profile changed during execution")
    observed = {"smoke_results": {row["smoke"]: row for row in result.get("smoke_results", [])}, "import_environment": dict(import_environment), "harness_defects": result.get("harness_defects")}
    return _publish_proofs(SmokePassStore(), context["runtime_rows"], observed, context["request"], principal)


def main(argv: Sequence[str] | None = None) -> int:
    """Read/select/run and guarded proof lifecycle over exactly the same store."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("select", "run", "list", "get", "history", "correct", "clear", "retire", "reinstate"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--request-json", type=Path)
    parser.add_argument("--store-root", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--as-of", type=int)
    parser.add_argument("--expected-version", type=int)
    parser.add_argument("--principal")
    parser.add_argument("--reason", default="")
    parser.add_argument("--metadata", default="{}")
    parser.add_argument("--fields", default="[]")
    parser.add_argument("--clear-reuse", action="store_true")
    args = parser.parse_args(argv)
    store = SmokePassStore(args.store_root)
    request = json.loads(args.request_json.read_bytes()) if args.request_json else None
    if args.operation in ("select", "run"):
        if request is None:
            parser.error("selection/run requires --request-json")
        result = _request_operation(args, request, store)
    else:
        result = proof_operation(store, args.operation, key=args.key, as_of=args.as_of, metadata=json.loads(args.metadata), fields=json.loads(args.fields), clear_reuse=args.clear_reuse, expected_version=args.expected_version, principal=args.principal or "", reason=args.reason, request=request, root=args.repo_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(result["run"]["exit_code"]) if args.operation == "run" else 0


def _request_operation(args: argparse.Namespace, request: Mapping[str, Any], store: SmokePassStore) -> dict[str, Any]:
    if args.operation == "select":
        return select_smokes(args.repo_root, request, store=store)
    with redirect_stdout(sys.stderr):
        return run_selected_smokes(args.repo_root, request, principal=args.principal or "", store=store)


def proof_operation(
    store: SmokePassStore,
    operation: str,
    *,
    key: str | None = None,
    as_of: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    fields: list[str] | None = None,
    clear_reuse: bool = False,
    expected_version: int | None = None,
    principal: str = "",
    reason: str = "",
    request: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> Any:
    """Closed operation dispatch; lifecycle writes cannot mutate observations."""
    if operation == "list":
        return store.list()
    if key is None:
        raise ValueError("proof key is required")
    if operation == "get":
        return store.get(key, as_of=as_of)
    if operation == "history":
        return store.history(key, as_of=as_of)
    if expected_version is None:
        raise ValueError("proof mutation requires expected_version")
    common: dict[str, Any] = {"expected_version": expected_version, "principal": principal, "reason": reason}
    return _mutate_proof(store, operation, key, metadata, fields, clear_reuse, common, request, root)


def _mutate_proof(store: SmokePassStore, operation: str, key: str, metadata: Mapping[str, Any] | None, fields: list[str] | None, clear_reuse: bool, common: dict[str, Any], request: Mapping[str, Any] | None, root: Path | None) -> dict[str, Any]:
    if operation == "correct":
        return store.correct(key, metadata or {}, **common)
    if operation == "clear":
        return store.clear(key, fields or [], clear_reuse=clear_reuse, **common)
    if operation == "retire":
        return store.retire(key, **common)
    if operation != "reinstate" or request is None or root is None:
        raise ValueError("reinstate requires a current frozen request and root")
    return _reinstate(store, key, request, root, common)


def _reinstate(store: SmokePassStore, key: str, request: Mapping[str, Any], root: Path, common: dict[str, Any]) -> dict[str, Any]:
    current = select_smokes(root, request, store=store)
    matched = next((row for row in current["selected"] if row["key"] == key and row["identity_complete"]), None)
    if matched is None:
        raise ValueError("obsolete or incomplete proof identity cannot be reinstated")
    _, tree = validate_request(root, request)
    verify_worktree(root, tree, _executing_paths(current))
    return store.reinstate(key, current_key=matched["key"], **common)


def observations_for_run(entries: list[str], results_output: Path | None, initial: dict[str, Any]) -> dict[str, Any]:
    return SmokeObservations(entries, initial) if results_output else initial


def campaign_results(path: Path | None, results: dict[str, Any], verdict: str, code: int | None, runner: Any, *, harness: list[str]) -> None:
    """Write the campaign file with the same ordered ``harness_defects`` as the receipt."""
    if path is None:
        return
    attempts = results.attempts if isinstance(results, SmokeObservations) else {}
    runner._write_receipt(path, {"result": verdict, "exit_code": code, "summary": runner.build_summary(results, verdict, code), "harness_defects": list(harness), "smoke_results": [{**dict(row), "attempts": attempts.get(entry, [])} for entry, row in results.items()]})


class SmokeObservations(dict[str, Any]):
    """Keep complete original attempts without changing legacy receipt rows."""

    def __init__(self, entries: list[str], initial: dict[str, Any]) -> None:
        super().__init__(initial)
        self.attempts: dict[str, list[dict[str, object]]] = {entry: [] for entry in entries}

    def record_attempt(self, entry: str, verdict: str, output: str, elapsed: float | None, exit_code: int | None) -> None:
        from datetime import UTC, datetime

        attempt: dict[str, object] = {"smoke": entry, "verdict": verdict, "exit_code": exit_code, "output": output, "seconds": elapsed, "recorded_at": datetime.now(UTC).isoformat()}
        attempt["raw_attempt_path"] = SmokePassStore().retain_attempt(attempt, principal=f"local-uid:{os.getuid()}")
        self.attempts[entry].append(attempt)


def run_request_cli(args: argparse.Namespace, root: Path) -> int:
    with redirect_stdout(sys.stderr):
        proof = run_selected_smokes(root, json.loads(args.proof_request.read_bytes()), principal=f"local-uid:{os.getuid()}")
    encoded = json.dumps(proof, indent=2, sort_keys=True) + "\n"
    if args.proof_output:
        args.proof_output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded)
    return int(proof["run"]["exit_code"])


_PROOF_RUN_LOCK = threading.Lock()


def execute_registered(entries: list[str], runner: Any) -> dict[str, Any]:
    """Run registered misses through the real runner, including explicit empty.

    Focused runs do not consume full-battery receipts. Serial host exclusion
    and the private import environment are retained; each smoke runs
    to its natural exit with no execution deadline. Every attempt's retained
    output is returned, with omitted earlier bytes disclosed.
    """
    allowed = set(runner._read_register(runner._DEFAULT_REGISTER))
    if len(entries) != len(set(entries)) or not set(entries) <= allowed:
        raise ValueError("selected entries must be unique registered smokes")
    if not entries:
        return {"status": "no_tests", "reason": "empty computed selection reached canonical runner", "exit_code": 0, "smoke_results": {}, "selected_count": 0, "harness_defects": []}
    runner._require_solet_name()
    args = argparse.Namespace(jobs=runner._DEFAULT_JOBS, fail_on_skip=False)
    results = SmokeObservations(entries, runner._initial_smoke_results(entries))
    serial_only = runner._read_serial_only(runner._DEFAULT_SERIAL_ONLY)
    concurrent_at_start = runner.contention_signal.count_concurrent_live_batteries(runner._receipt_directory())
    with runner.host_load.HostLoadMonitor() as load_monitor, _PROOF_RUN_LOCK, runner._scratch_runtime()(runner._SmokeProcessSupervisor(runner._MAX_SMOKE_STARTS_PER_WINDOW, runner._SMOKE_START_WINDOW_SECONDS), runner._terminate_process_group):
        skipped, failures, missing, harness, waited = runner._run_suite_with_environment(entries, args, serial_only, smoke_results=results)
    concurrent_at_finish = runner.contention_signal.count_concurrent_live_batteries(runner._receipt_directory())
    contention = runner.contention_signal.measure(concurrent_live_batteries_start=concurrent_at_start, concurrent_live_batteries_finish=concurrent_at_finish, host_lock_wait_seconds=waited, host_load=load_monitor.snapshot()).as_dict()
    code = runner._summarize(len(entries), skipped, failures, missing, fail_on_skip=False, harness=harness)
    observed = {entry: {**dict(row), "attempts": results.attempts[entry]} for entry, row in results.items()}
    return {"status": "completed", "exit_code": code, "smoke_results": observed, "selected_count": len(entries), "host_lock_wait_seconds": waited, "contention": contention, "harness_defects": harness, "import_environment": getattr(args, "import_environment", None)}


if __name__ == "__main__":
    raise SystemExit(main())
