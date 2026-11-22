"""One frozen selection/run/proof contract shared by library, CLI and service.

The existing selector's rules remain a union. Base edges cover deletions;
candidate edges add resource and dynamic-import consumers; a path a smoke's
certificate recorded as read, probed or listed adds that smoke too. A smoke has
one key: its input closure when that is provable, else the whole-tree manifest,
each with the tool profile; when a certificate of the reads recorded by a clean
real run still holds on the tree, its key replaces both. A smoke on the
never-reuse list has no key and no proof. A row and an event carry the closure's
digest and count, not the closure, and raw output is kept only for a smoke that
did not pass cleanly. No request-supplied argv is executed.

``freeze`` is the one command that produces a request. A run may be limited to
the smokes a caller's own register names. Run paths share the proof store with
other writers: they follow a key that moved, and claim a reuse only while the
key still holds the observation the selection saw.
The store is pruned after each run that writes proofs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from quality_gates.focused_smoke_selector import REGISTER_RELPATH, select_focused_smokes
from quality_gates.gate_smoke_register import load_from_sources as _load_register_sources
from quality_gates.gate_smoke_register import parse_register_text
from quality_gates.smoke_identity import (
    NEVER_REUSE_RELPATH,
    RECORDED_SCOPE,
    REPOSITORY_SCAN,
    RULE_VERSION,
    SCHEMA,
    FrozenTree,
    canonical,
    closure,
    dependency_graph,
    digest,
    freeze_request,
    git,
    parse_never_reuse,
    read_sources,
    safe_path,
    tool_profile,
    validate_request,
    verify_worktree,
)
from quality_gates.smoke_pass_store import SmokePassStore, StaleProofVersionError, clean_observation
from quality_gates.smoke_read_store import Misses, ReadCapture, RecordedReadStore, RecordedReuse, capture_directory, capture_plan, current_keys, decline

# The runner starts a retained stream with this marker when its collector
# kept only the stream's tail. Skip detection cannot read omitted bytes, so
# such an observation is partial and never reused.
TRUNCATED_OUTPUT_MARKER = "OUTPUT-TRUNCATED:"

# A run path's write follows a key that another writer moved. It lands whenever
# fewer than five writers interleave on that key inside the one write (a campaign
# and a landing are two); after five stale attempts the last error is raised.
_STALE_ATTEMPTS = 5


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


_REGULAR_MODES = ("100644", "100755")
# What a notice says it could not prove, and the limit it names. A notice this table does not name is treated as unparsed: it cannot be listed either.
_NOTICE_LIMITS = (("resource target", "file-read"), ("external process", "external-process"), ("dynamic", "dynamic-load"))


def _notice_limit(notice: str) -> str:
    if not notice.startswith("unparsed"):
        for words, limit in _NOTICE_LIMITS:
            if words in notice:
                return limit
    return "unparsed"


def _scope_limits(paths: Sequence[str], inputs: Mapping[str, Mapping[str, str]], unknown: Mapping[str, list[str]]) -> list[str]:
    """What keeps a closure from being proven complete; any one of them widens the key to the whole tree."""
    limits = {_notice_limit(notice) for path in paths for notice in unknown.get(path, [])}
    if REPOSITORY_SCAN in paths:
        limits.add("repository-scan")
    if any(row["mode"] not in _REGULAR_MODES for row in inputs.values()):
        limits.add("opaque-mode")
    return sorted(limits)


def _identity_row(smoke: str, tree: FrozenTree, graph: Mapping[str, set[str]], unknown: Mapping[str, list[str]], profile: Mapping[str, Any], never: Mapping[str, str]) -> dict[str, Any]:
    """One key per smoke: ``closure`` when its inputs are provable, ``tree`` when they are not, none when it is never reused.

    A smoke whose own entry or whose profile input is opaque (a symlink or submodule)
    is incomplete and is never reused, whatever its scope.
    """
    paths = closure(graph, smoke)
    inputs = {path: tree.entries[path] for path in paths if path in tree.entries}
    limits = _scope_limits(paths, inputs, unknown)
    scope = "never" if smoke in never else "tree" if limits else "closure"
    content: dict[str, Any] = {"manifest": tree.manifest_sha256} if limits else {"inputs": inputs}
    key = None if scope == "never" else digest({"schema": SCHEMA, "smoke": smoke, "scope": scope, **content, "profile": profile})
    advisories = _opaque_mode_advisories({smoke: tree.entries[smoke]}, profile)
    held = {} if limits else inputs
    return {
        "smoke": smoke,
        "identity": tree.entries[smoke],
        "key_scope": scope,
        "scope_limits": limits,
        "inputs": held,
        "inputs_sha256": digest(held),
        "inputs_count": len(held),
        "repository_manifest_sha256": tree.manifest_sha256 if limits else None,
        "profile_sha256": digest(profile),
        "key": key,
        "identity_complete": not advisories,
        "identity_advisories": advisories,
    }


def _opaque_mode_advisories(inputs: Mapping[str, Any], profile: Mapping[str, Any]) -> list[str]:
    return sorted({f"{path}: opaque symlink/submodule target identity is not proven" for source in (inputs, profile.get("inputs", {})) for path, row in source.items() if row.get("mode") not in _REGULAR_MODES})


def select_smokes(root: Path, request: Mapping[str, Any], *, store: SmokePassStore | None = None, only: Sequence[str] | None = None, recorded: bool = True) -> dict[str, Any]:
    """Compute frozen union with exact key/reuse reasons; executes no smoke and writes no certificate.

    ``only`` holds the smokes a caller's own register names. The selection then
    keeps just those rows and reports what that dropped; without it nothing is
    restricted and the response is the whole union. A row carries the digest and
    the count of its closure, not the closure map. ``recorded=False`` ignores
    every recorded-read certificate, as physical full sampling does.
    """
    return _select(root, request, store or SmokePassStore(), only, recorded)[0]


def _select(root: Path, request: Mapping[str, Any], store: SmokePassStore, only: Sequence[str] | None, recorded: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The selection response and the full rows behind it; the run path verifies the closure maps only the full rows keep."""
    base, candidate = validate_request(root, request)
    old, sources = read_sources(root, base), read_sources(root, candidate)
    changed = [entry["path"] for entry in request["delta_entries"]]
    named = [safe_path(path) for path in request.get("named_smokes", [])]
    graph, unknown = dependency_graph(sources, candidate.entries)
    never = parse_never_reuse(sources.get(NEVER_REUSE_RELPATH))
    old_graph, _ = dependency_graph(old, base.entries)
    # The EFFECTIVE register: gate_smokes.txt plus every fragment in the frozen
    # tree, so a fragment-registered smoke is part of the selection.
    registered = _load_register_sources(sources, REGISTER_RELPATH).entries
    reasons = _selection_reasons(root, sources, old, changed, named, registered, graph, old_graph)
    profile = tool_profile(root, candidate, sources)
    reuse = RecordedReuse.widened(recorded, store, root, candidate, profile, reasons, registered, changed)
    computed = _selected_rows(registered, reasons, candidate, graph, unknown, profile, store, request.get("known_failures", {}), never, reuse)
    rows, restriction = _restrict(computed, only)
    register = "".join(row["smoke"] + "\n" for row in rows)
    # A path only a dropped smoke consumes is still mapped: dropping is reported below, not hidden as a coverage gap.
    mapped = _mapped_paths(computed)
    response = {
        "schema": SCHEMA,
        "rule_version": RULE_VERSION,
        "request": dict(request),
        "profile": profile,
        "profile_sha256": digest(profile),
        "register": register,
        "register_sha256": hashlib.sha256(register.encode()).hexdigest(),
        "selected": [{name: value for name, value in row.items() if name != "inputs"} for row in rows],
        "unmapped": sorted(set(changed) - mapped),
        "advisories": [f"{limit}: {count} selected rows" for limit, count in sorted(Counter(limit for row in rows for limit in row["scope_limits"]).items())],
        "hub_full_required": _hub_changed(changed),
        **restriction,
    }
    response["selection_sha256"] = digest(response)
    return response, rows


def _restrict(rows: list[dict[str, Any]], only: Sequence[str] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep the rows a caller's register names, and report the cut so the selection hash covers it.

    The register is hashed as its entries, sorted and one per line, so two files that
    name the same smokes restrict, and hash, alike. An entry the computed union lacks
    is reported, not refused: the request's named smokes already forced what mattered.
    """
    if only is None:
        return rows, {}
    wanted, computed = set(only), {row["smoke"] for row in rows}
    register = "".join(f"{smoke}\n" for smoke in sorted(wanted))
    report = {"restricted_out": sorted(computed - wanted), "restricted_missing": sorted(wanted - computed), "restricted_to_register_sha256": hashlib.sha256(register.encode()).hexdigest()}
    return [row for row in rows if row["smoke"] in wanted], report


def _mapped_paths(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    return {row["changed_path"] for selected in rows for row in selected["reasons"] if "changed_path" in row}


def _repository_inputs(rows: Sequence[Mapping[str, Any]], candidate: FrozenTree) -> dict[str, Any]:
    """The whole tree when any row could still fall back to a whole-tree key, a licensed row included."""
    return candidate.entries if any((row.get("fallback") or row)["repository_manifest_sha256"] for row in rows) else {}


def _executing_paths(rows: Sequence[Mapping[str, Any]], profile: Mapping[str, Any], candidate: FrozenTree) -> list[str]:
    """The files a run executes: the rows' closures, the tool profile's inputs and, for a row keyed by the tree, every tracked file."""
    return sorted({path for row in rows for path in row["inputs"]} | set(profile["inputs"]) | set(_repository_inputs(rows, candidate)))


def _selection_reasons(root: Path, sources: Mapping[str, bytes], old: Mapping[str, bytes], changed: list[str], named: list[str], registered: Sequence[str], graph: Mapping[str, set[str]], old_graph: Mapping[str, set[str]]) -> dict[str, list[dict[str, str]]]:
    reasons = _rules(root, sources, changed, named)
    for smoke, previous in _rules(root, old, changed, named).items():
        reasons.setdefault(smoke, []).extend(previous)
    for smoke in registered:
        additions = _transitive_reasons(graph, smoke, set(changed)) + _transitive_reasons(old_graph, smoke, set(changed), "base")
        if additions:
            reasons.setdefault(smoke, []).extend(additions)
    return reasons


def _selected_rows(
    registered: Sequence[str], reasons: Mapping[str, list[dict[str, str]]], candidate: FrozenTree, graph: Mapping[str, set[str]], unknown: Mapping[str, list[str]], profile: Mapping[str, Any], store: SmokePassStore, known_failures: Mapping[str, Any], never: Mapping[str, str], reuse: RecordedReuse | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for smoke in sorted(set(registered) & reasons.keys()):
        if smoke not in candidate.entries:
            continue
        row = _identity_row(smoke, candidate, graph, unknown, profile, never)
        row["known_failure_ref"] = known_failures.get(smoke)
        row.update(reasons=sorted({json.dumps(reason, sort_keys=True) for reason in reasons[smoke]}), **_reuse_state(row, store, never.get(smoke), smoke in known_failures))
        row["reasons"] = [json.loads(reason) for reason in row["reasons"]]
        if reuse is not None:
            reuse.settle(row, smoke in known_failures)
        rows.append(row)
    return rows


def _reuse_state(row: Mapping[str, Any], store: SmokePassStore, never_reason: str | None, known_failure: bool) -> dict[str, Any]:
    """Whether the row's pass is reused, and why not. A never-reused smoke has no key, so the store is not asked."""
    if never_reason is not None:
        return {"reusable": False, "reuse_reason": f"never reused: {never_reason}", "proof_version": None, "proof_event_id": None}
    proof = store.get(row["key"])
    reusable = _reusable(row, proof, known_failure)
    return {"reusable": reusable, "reuse_reason": "exact clean content key" if reusable else "no current clean fully identified observation", "proof_version": proof["version"], "proof_event_id": proof["observation_event_id"]}


def _reusable(row: Mapping[str, Any], proof: Mapping[str, Any], known_failure: bool) -> bool:
    return row["identity_complete"] and proof["reusable"] and not proof["retired"] and not known_failure


def _hub_changed(paths: Sequence[str]) -> bool:
    return any(path in ("quality_gates/run_smokes.py", "quality_gates/run_smokes_scratch.py") or "provision" in path or "schema_engine" in path or "schema_apply" in path for path in paths)


def _observation(row: Mapping[str, Any], result: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    attempts = result.get("attempts", [])
    output = "\n".join(str(attempt.get("output", "")) for attempt in attempts)
    skipped = result.get("verdict") == "skipped" or bool(re.search(r"\bSKIP(?:PED)?\b|\b0 (?:passed|tests? run)\b|\bRan 0 tests?\b|\bcollected 0 items\b|\bno tests ran\b", output, re.IGNORECASE))
    observation: dict[str, Any] = {
        "key": row["key"],
        "smoke": row["smoke"],
        "key_scope": row["key_scope"],
        "inputs_sha256": row["inputs_sha256"],
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
    if clean_observation(observation):
        observation["attempts"] = [_without_output(attempt) for attempt in attempts]
    return observation


def _without_output(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """An attempt without its output, which it replaces by the output's size in bytes."""
    return {**{name: value for name, value in attempt.items() if name != "output"}, "output_bytes": len(str(attempt.get("output", "")).encode())}


def _run_without_passed_output(observed: Mapping[str, Any]) -> dict[str, Any]:
    """The run result with each passed smoke's attempts stripped of output; the observations were built from the full output first."""
    results = observed.get("smoke_results")
    if results is None:
        return dict(observed)
    return {**observed, "smoke_results": {smoke: {**row, "attempts": [_without_output(attempt) for attempt in row.get("attempts", [])]} if row.get("verdict") == "passed" else row for smoke, row in results.items()}}


def _open_trees(root: Path) -> set[str]:
    listing = git(root, "worktree", "list", "--porcelain").decode()
    heads = sorted({line.split()[1] for line in listing.splitlines() if line.startswith("HEAD ")})
    return set(git(root, "rev-parse", *(f"{head}^{{tree}}" for head in heads)).decode().split())


def _end_of_proofs(proofs: SmokePassStore, root: Path, principal: str) -> None:
    try:
        trees = _open_trees(root)
    except (ValueError, OSError) as error:
        trees = None
        print(f"ADVISORY proof store: open candidates unreadable ({error}); only the 7-day limit applies", file=sys.stderr, flush=True)
    try:
        result = proofs.prune(trees, principal=principal)
        reads = RecordedReadStore.beside(proofs).prune(proofs, principal=principal)
    except OSError as error:
        print(f"ADVISORY proof store: not pruned ({error})", file=sys.stderr, flush=True)
        return
    print(f"proof store: kept {result['kept']} keys, removed {result['removed_keys']} keys and {result['removed_attempts']} attempts", file=sys.stderr, flush=True)
    print(f"recorded reads: kept {reads['kept']} certificates, removed {reads['removed']}", file=sys.stderr, flush=True)


def run_selected_smokes(root: Path, request: Mapping[str, Any], *, principal: str, store: SmokePassStore | None = None, executor: Callable[[list[str]], dict[str, Any]] | None = None, only: Sequence[str] | None = None, record_reads: bool = False) -> dict[str, Any]:
    """Recompute selection, claim current clean rows, run every miss, append.

    Using a certificate is always on. Making one is the caller's choice (``record_reads``):
    each miss then runs under the recorder's worker, whose per-call instrumentation made a
    two-minute CPU-bound smoke run for more than fourteen minutes without finishing when
    measured, so a check-in does not pay it until its cost is measured. A miss run by an
    injected executor records nothing and leaves no certificate either way.
    """
    from quality_gates.run_smokes import execute_selected

    proofs = store or SmokePassStore()
    proofs._authorize(principal)
    selection, full_rows = _select(root, request, proofs, only)
    _, candidate = validate_request(root, request)
    paths = _executing_paths(full_rows, selection["profile"], candidate)
    verify_worktree(root, candidate, paths)
    reuse = RecordedReuse.open(proofs, root, candidate, selection["profile"])
    runtime_rows = [dict(row) for row in selection["selected"]]
    _claim_reuse(runtime_rows, reuse)
    pending = [row["smoke"] for row in runtime_rows if not row["reusable"]]
    _begin_proofs(proofs, runtime_rows, principal)
    with capture_directory(record_reads) as scratch:
        capture = capture_plan(runtime_rows, scratch, selection["profile_sha256"], record=record_reads)
        observed = (executor or execute_selected)(Misses(pending, capture))
        verify_worktree(root, candidate, paths)
        current_profile = tool_profile(root, candidate, read_sources(root, candidate))
        if digest(current_profile) != selection["profile_sha256"]:
            raise ValueError("effective tool/import profile changed before proof publication")
        rows = _publish_proofs(proofs, runtime_rows, observed, request, principal, reuse, capture)
    _end_of_proofs(proofs, root, principal)
    return {
        "schema": SCHEMA,
        "selection": selection,
        "status": "no_tests" if not rows else "completed",
        "reason": "empty computed selection reached runner" if not rows else "computed union executed with clean-only reuse",
        "selected_count": len(rows),
        "executed_count": len(pending),
        "reused_count": len(rows) - len(pending),
        "run": _run_without_passed_output(observed),
        "proofs": rows,
        "counts": _proof_counts(rows),
    }


def _proof_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    proven = [row for row in rows if row["proof"] is not None]
    observations = [row["proof"].get("observation") or {} for row in proven]
    return {"known_failure": sum(bool(row.get("known_failure")) for row in observations), "skipped": sum(bool(row.get("skipped")) for row in observations), "harness_defect": sum(bool(row.get("harness_defect")) for row in observations), "clean_reusable": sum(bool(row["proof"]["reusable"]) for row in proven)}


def _claim_reuse(rows: list[dict[str, Any]], reuse: RecordedReuse) -> None:
    """Consume a reusable row only while its key still holds the observation the selection saw.

    Another writer may have moved the key since the selection. A key whose observation
    changed, or that is retired or no longer clean, cannot be claimed: that never refuses,
    the row executes. A row licensed by a certificate is revalidated against the tree
    first, and one that fails goes back to the key it had. A later begin on a claimed
    key does not undo the claim; a retire before publication does, and
    ``_publish_proofs`` refuses it.
    """
    for row in rows:
        if row["reusable"] and (reason := reuse.refusal(row)) is not None:
            decline(row, reason)
            print(f"ADVISORY {row['smoke']}: {reason}; executing instead of reusing", file=sys.stderr, flush=True)


def _publish_proofs(proofs: SmokePassStore, selected: Sequence[Mapping[str, Any]], observed: Mapping[str, Any], request: Mapping[str, Any], principal: str, reuse: RecordedReuse, capture: Mapping[str, ReadCapture] | None = None) -> list[dict[str, Any]]:
    """Each observation says whether its run listed it in ``harness_defects``; null when the run's list is unknown."""
    harness = observed.get("harness_defects")
    rows: list[dict[str, Any]] = []
    for row in selected:
        if row["key_scope"] == "never":
            rows.append({"smoke": row["smoke"], "reused": False, "proof": None})
        elif row["reusable"]:
            rows.append(_consumed(proofs, row))
        else:
            result = {**observed.get("smoke_results", {}).get(row["smoke"], {}), "import_environment": observed.get("import_environment", {}), "harness_defect": None if harness is None else row["smoke"] in harness}
            rows.append(_executed(proofs, row, result, request, principal, reuse, capture))
    return rows


def _consumed(proofs: SmokePassStore, row: Mapping[str, Any]) -> dict[str, Any]:
    current = proofs.get(row["key"])
    if current["observation_event_id"] != row["proof_event_id"] or current["retired"]:
        raise ValueError("proof changed during consumption; recompute selection")
    consumed: dict[str, Any] = {"smoke": row["smoke"], "reused": True, "proof": current}
    if row["key_scope"] == RECORDED_SCOPE:
        consumed["recorded"] = row["recorded"]
    return consumed


def _executed(proofs: SmokePassStore, row: Mapping[str, Any], result: Mapping[str, Any], request: Mapping[str, Any], principal: str, reuse: RecordedReuse, capture: Mapping[str, ReadCapture] | None) -> dict[str, Any]:
    """The observation under the row's key, then, for an attempt that was captured, the certificate its trace may be."""
    observation = _observation(row, result, request)
    executed: dict[str, Any] = {"smoke": row["smoke"], "reused": False, "proof": _observe_current(proofs, row, observation, principal)}
    if capture:

        def publish(key: str, recorded_row: dict[str, Any]) -> None:
            recorded = _observation(recorded_row, result, request)
            _write_current(lambda version: proofs.observe(key, recorded, expected_version=version, principal=principal), proofs.get(key)["version"])

        origin = {"origin_commit": request["candidate_commit"], "origin_tree": request["candidate_tree"]}
        executed["recorded"] = reuse.certify(row, capture, clean=clean_observation(observation), origin=origin, publish=publish, principal=principal)
    return executed


def _write_current(write: Callable[[int], dict[str, Any]], version: int) -> dict[str, Any]:
    """Run a run-path write that follows its key: on a stale version, repeat the same write at the version the store reports.

    A run's begin and observe measure one content key and its history is append-only, so
    another writer's event stays and this one lands after it. After the fifth stale
    attempt the last error is raised. The lifecycle writes (correct, clear, retire,
    reinstate) never come through here: they keep the strict expected-version refusal.
    """
    attempt = 1
    while True:
        try:
            return write(version)
        except StaleProofVersionError as stale:
            if attempt >= _STALE_ATTEMPTS:
                raise
            attempt, version = attempt + 1, stale.current_version


def _observe_current(proofs: SmokePassStore, row: Mapping[str, Any], observation: Mapping[str, Any], principal: str) -> dict[str, Any]:
    return _write_current(lambda version: proofs.observe(row["key"], observation, expected_version=version, principal=principal), row["proof_version"])


def _begin_proofs(proofs: SmokePassStore, selected: Sequence[dict[str, Any]], principal: str) -> None:
    for row in selected:
        if not row["reusable"] and row["key_scope"] != "never":
            started = _begin_current(proofs, row, principal)
            row.update(proof_version=started["version"], started_event_id=started["active_attempt"])


def _begin_current(proofs: SmokePassStore, row: Mapping[str, Any], principal: str) -> dict[str, Any]:
    identity = {"smoke": row["smoke"], "key_scope": row["key_scope"], "inputs_sha256": row["inputs_sha256"], "profile_sha256": row["profile_sha256"]}
    return _write_current(lambda version: proofs.begin(row["key"], identity, expected_version=version, principal=principal), row["proof_version"])


def prepare_full_proofs(root: Path, commit: str, *, principal: str, attribution: Mapping[str, Any], capture_dir: Path | None = None) -> dict[str, Any]:
    """Freeze every register row and begin attempts before scheduled Tier F.

    Existing clean pointers and recorded certificates are deliberately ignored: this
    is physical full sampling. The normal candidate/full-battery receipt remains
    independent. A caller that runs the rows under the recorder's worker passes the
    directory their traces go to; the context then carries the captures, and the
    finish publishes a certificate for each clean row whose trace validates.
    """
    from quality_gates.smoke_identity import read_tree

    tree = read_tree(root, commit)
    sources = read_sources(root, tree)
    names = _load_register_sources(sources, REGISTER_RELPATH).entries
    request = freeze_request(root, tree.commit, tree.commit, repository_id="rep_7111540c-ac4a-468c-8f84-b67d8756d1d0", named_smokes=names, attribution=attribution)
    selection = select_smokes(root, request, recorded=False)
    runtime_rows = [{**row, "reusable": False} for row in selection["selected"]]
    if {row["smoke"] for row in runtime_rows} != set(names):
        raise ValueError("full register includes an unmeasurable smoke identity")
    _begin_proofs(SmokePassStore(), runtime_rows, principal)
    context: dict[str, Any] = {"request": request, "selection": selection, "runtime_rows": runtime_rows, "cache_ignored": True}
    if capture_dir is not None:
        context["capture"] = capture_plan(runtime_rows, capture_dir, selection["profile_sha256"], record=True)
    return context


def finish_full_proofs(root: Path, context: Mapping[str, Any], result: Mapping[str, Any], import_environment: Mapping[str, Any], *, principal: str) -> list[dict[str, Any]]:
    """Publish measured full rows only after final bytes/profile revalidation."""
    _, tree = validate_request(root, context["request"])
    verify_worktree(root, tree, list(tree.entries))
    profile = tool_profile(root, tree, read_sources(root, tree))
    if digest(profile) != context["selection"]["profile_sha256"]:
        raise ValueError("full campaign tool profile changed during execution")
    observed = {"smoke_results": {row["smoke"]: row for row in result.get("smoke_results", [])}, "import_environment": dict(import_environment), "harness_defects": result.get("harness_defects")}
    proofs = SmokePassStore()
    rows = _publish_proofs(proofs, context["runtime_rows"], observed, context["request"], principal, RecordedReuse.open(proofs, root, tree, profile), context.get("capture"))
    _end_of_proofs(proofs, root, principal)
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    """Freeze a request, read/select/run, and guarded proof lifecycle over exactly the same store."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("freeze", "select", "run", "list", "get", "history", "correct", "clear", "retire", "reinstate"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base-commit")
    parser.add_argument("--candidate-commit")
    parser.add_argument("--repository-id")
    parser.add_argument("--named-file", type=Path)
    parser.add_argument("--request-out", type=Path)
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
    parser.add_argument("--record-reads", action="store_true", help="Run each miss under the read recorder so a clean pass can leave a certificate (slower).")
    args = parser.parse_args(argv)
    if args.operation == "freeze":
        return _freeze_cli(args)
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


def _freeze_cli(args: argparse.Namespace) -> int:
    """Write the frozen request the caller names; when it cannot be produced, say why in one line and write nothing."""
    required = {"--base-commit": args.base_commit, "--candidate-commit": args.candidate_commit, "--repository-id": args.repository_id, "--request-out": args.request_out}
    try:
        if missing := [flag for flag, value in required.items() if value is None]:
            raise ValueError(f"freeze requires {', '.join(missing)}")
        summary = _write_frozen_request(args)
    except (ValueError, OSError) as error:
        print("freeze refused: " + " ".join(str(error).split()), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _write_frozen_request(args: argparse.Namespace) -> dict[str, Any]:
    """The producer: ``freeze_request`` for two commits and the smokes a register-grammar file names, written as canonical JSON.

    A named smoke the candidate does not register is refused here: the selection would
    ignore it, and a request that quietly names nothing is worse than none.
    """
    from quality_gates.smoke_identity import read_tree

    named = parse_register_text(args.named_file.read_text(encoding="utf-8")) if args.named_file else ()
    request = freeze_request(args.repo_root, args.base_commit, args.candidate_commit, repository_id=args.repository_id, named_smokes=named)
    if named:
        sources = read_sources(args.repo_root, read_tree(args.repo_root, request["candidate_commit"]))
        registered = set(_load_register_sources(sources, REGISTER_RELPATH).entries) if REGISTER_RELPATH in sources else set()
        if unregistered := sorted(set(named) - registered):
            raise ValueError(f"not registered in {request['candidate_commit']}: {', '.join(unregistered)}")
    args.request_out.write_bytes(canonical(request))
    return {"request_out": str(args.request_out), "request_sha256": digest(request), "base_commit": request["base_commit"], "candidate_commit": request["candidate_commit"], "named_smokes": request["named_smokes"]}


def _request_operation(args: argparse.Namespace, request: Mapping[str, Any], store: SmokePassStore) -> dict[str, Any]:
    if args.operation == "select":
        return select_smokes(args.repo_root, request, store=store)
    with redirect_stdout(sys.stderr):
        return run_selected_smokes(args.repo_root, request, principal=args.principal or "", store=store, record_reads=args.record_reads)


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
    current, rows = _select(root, request, store, None)
    matched = next((row for row in current["selected"] if row["identity_complete"] and key in current_keys(row)), None)
    if matched is None:
        raise ValueError("obsolete or incomplete proof identity cannot be reinstated")
    _, tree = validate_request(root, request)
    verify_worktree(root, tree, _executing_paths(rows, current["profile"], tree))
    return store.reinstate(key, current_key=key, **common)


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
        self.attempts[entry].append(attempt)


def _register_restriction(register: Path | None) -> tuple[str, ...] | None:
    """The smokes a caller's own register names. No register, or the default one, names everything, so nothing is restricted."""
    if register is None:
        return None
    from quality_gates.run_smokes import _DEFAULT_REGISTER

    if register.resolve() == _DEFAULT_REGISTER.resolve():
        return None
    return parse_register_text(register.read_text(encoding="utf-8"))


def run_request_cli(args: argparse.Namespace, root: Path) -> int:
    only = _register_restriction(getattr(args, "register", None))
    with redirect_stdout(sys.stderr):
        proof = run_selected_smokes(root, json.loads(args.proof_request.read_bytes()), principal=f"local-uid:{os.getuid()}", only=only, record_reads=getattr(args, "record_reads", False))
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
    output is returned, with omitted earlier bytes disclosed. ``entries`` that
    carry a capture (``Misses``) run those attempts under the recorder's worker,
    which writes each one's read trace to the path it is given.
    """
    allowed = set(runner._read_register(runner._DEFAULT_REGISTER))
    if len(entries) != len(set(entries)) or not set(entries) <= allowed:
        raise ValueError("selected entries must be unique registered smokes")
    if not entries:
        return {"status": "no_tests", "reason": "empty computed selection reached canonical runner", "exit_code": 0, "smoke_results": {}, "selected_count": 0, "harness_defects": []}
    runner._require_solet_name()
    args = argparse.Namespace(jobs=runner._DEFAULT_JOBS, fail_on_skip=False)
    if capture := getattr(entries, "capture", None):
        args.read_capture = capture
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
