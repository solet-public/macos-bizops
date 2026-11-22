"""Expectations: the promises that hold for every hook call, and the per-step checks of expected saves, notices and bytes."""

from __future__ import annotations

import json
from typing import Any, cast

from scenario_model import ALLOWED_SPECIFIC, ALLOWED_TOP, FORBIDDEN_KEYS, SILENT_EVENTS, STOP_AND_TELL, CellError, Env, HookRun, Json


def _find_keys(value: Any, names: frozenset[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in cast(Json, value).items():
            found.extend([key] if key in names else [])
            found.extend(_find_keys(item, names))
    elif isinstance(value, list):
        for item in cast("list[Any]", value):
            found.extend(_find_keys(item, names))
    return found


def _output_objects(run: HookRun) -> list[Json]:
    objects: list[Json] = []
    for line in run.stdout.splitlines():
        if not line.strip():
            continue
        try:
            value: Any = json.loads(line)
        except ValueError as exc:
            raise CellError(f"{run.event}: stdout is not JSON ({exc}): {line[:200]!r}") from exc
        if not isinstance(value, dict):
            raise CellError(f"{run.event}: stdout is not a JSON object: {line[:200]!r}")
        objects.append(cast(Json, value))
    return objects


def check_invariants(run: HookRun) -> list[Json]:
    """The promises that hold for every hook call of every scenario: never block, say only the allowed keys."""
    if not run.registered:
        return []
    if run.timed_out:
        raise CellError(f"{run.event}: the hook did not finish inside its declared timeout")
    if run.exit_code != 0:
        raise CellError(f"{run.event}: exit code {run.exit_code}, expected 0 (stderr: {run.stderr.strip()[:200]!r})")
    objects = _output_objects(run)
    for found in _find_keys(objects, FORBIDDEN_KEYS):
        raise CellError(f"{run.event}: output carries the forbidden key {found!r}: {run.stdout.strip()[:200]!r}")
    for obj in objects:
        _check_keys(run, obj)
    return objects


def _check_keys(run: HookRun, obj: Json) -> None:
    extra = set(obj) - ALLOWED_TOP
    specific = cast(Json, obj.get("hookSpecificOutput", {}))
    inner = set(specific) - ALLOWED_SPECIFIC
    if extra or inner:
        raise CellError(f"{run.event}: output keys outside the allowed set: {sorted(extra | inner)}")
    if run.event in SILENT_EVENTS:
        raise CellError(f"{run.event}: the hook must print nothing, it printed {run.stdout.strip()[:200]!r}")
    if run.event == "SessionStart" and "systemMessage" in obj:
        raise CellError("SessionStart: only additionalContext may be printed, found systemMessage")
    if specific and specific.get("hookEventName") != run.event:
        raise CellError(f"{run.event}: hookEventName is {specific.get('hookEventName')!r}")


def _speech(objects: list[Json]) -> tuple[str, str]:
    message = " ".join(str(obj.get("systemMessage", "")) for obj in objects)
    context = " ".join(str(cast(Json, obj.get("hookSpecificOutput", {})).get("additionalContext", "")) for obj in objects)
    return message, context


def _check_kind(run: HookRun, kind: str, message: str, context: str) -> None:
    """The shape of the output: nothing, a notice (person and model), or model context alone."""
    shown = run.stdout.strip()[:300]
    if kind == "empty" and run.stdout.strip():
        raise CellError(f"{run.event}: expected no output, got {shown!r}")
    if kind == "notice" and not (message and context):
        raise CellError(f"{run.event}: expected a notice (systemMessage and additionalContext), got {shown!r}")
    if kind == "context" and not context:
        raise CellError(f"{run.event}: expected additionalContext, got {shown!r}")


def _check_fragments(run: HookRun, expect: Json, message: str, context: str) -> None:
    for fragment in cast("list[str]", expect.get("notice_contains", [])):
        if fragment not in message:
            raise CellError(f"{run.event}: the notice does not contain {fragment!r}: {message[:400]!r}")
    for fragment in cast("list[str]", expect.get("context_contains", [])):
        if fragment not in context:
            raise CellError(f"{run.event}: the context does not contain {fragment!r}: {context[:400]!r}")


def _check_stdout(run: HookRun, expect: Json, objects: list[Json]) -> None:
    kind = str(expect.get("stdout", "any"))
    message, context = _speech(objects)
    _check_kind(run, kind, message, context)
    _check_fragments(run, expect, message, context)
    if kind == "notice" and int(expect.get("saves", 0)) >= 1 and "foreign" in expect and STOP_AND_TELL not in context:
        raise CellError(f"{run.event}: the model context lacks the stop-and-tell sentence: {context[-200:]!r}")


def _check_manifest(env: Env, expect: Json, manifests: list[Json]) -> None:
    if not manifests:
        return
    first = manifests[0]
    saved = {entry["path"] for manifest in manifests for entry in manifest["entries"]}
    for path in cast("list[str]", expect.get("saved", [])):
        if path not in saved:
            raise CellError(f"the save does not hold {path!r}; it holds {sorted(saved)[:12]}")
    _check_sets(expect, set(first["foreign"]), first)
    _check_binding(expect, first)
    _check_unsaved(env, expect, first)


def _check_binding(expect: Json, manifest: Json) -> None:
    for field_name, key in (("binding_folder", "folder"), ("binding_branch", "branch")):
        if field_name in expect and manifest["binding"][key] != expect[field_name]:
            raise CellError(f"{field_name}: expected {expect[field_name]!r}, the save records {manifest['binding'][key]!r}")
    if "trigger" in expect and manifest["trigger"] != expect["trigger"]:
        raise CellError(f"trigger: expected {expect['trigger']!r}, the save records {manifest['trigger']!r}")


def _check_sets(expect: Json, foreign: set[str], manifest: Json) -> None:
    for path in cast("list[str]", expect.get("foreign", [])):
        if path not in foreign:
            raise CellError(f"expected {path!r} among the changes made outside this session; the save lists {sorted(foreign)[:12]}")
    for path in cast("list[str]", expect.get("foreign_excludes", [])):
        if path in foreign:
            raise CellError(f"{path!r} is this session's own change but the save lists it as foreign")
    if expect.get("foreign_empty") and foreign:
        raise CellError(f"expected only this session's own changes; the save lists foreign {sorted(foreign)[:12]}")
    kinds = {reference["kind"] for reference in manifest["refs"]}
    for kind in cast("list[str]", expect.get("refs", [])):
        if kind not in kinds:
            raise CellError(f"expected a saved {kind} reference; the save holds {sorted(kinds)}")


def _check_unsaved(env: Env, expect: Json, manifest: Json) -> None:
    pairs = {f"{item['path']}:{item['reason']}" for item in manifest["unsaved"]}
    for item in cast("list[str]", expect.get("unsaved", [])):
        if item not in pairs:
            raise CellError(f"expected {item!r} among the unsaved paths; the save lists {sorted(pairs)[:8]}")
    reduced = env.options.bulk_files is not None
    if expect.get("unsaved_any") and not reduced and not sum(manifest["unsaved_counts"].values()):
        raise CellError("expected the untracked remainder to be named unsaved with a count; the save names none")


def effective_expect(env: Env, step: Json) -> Json:
    expect = cast(Json, env.expand(step.get("expect", {})))
    for difference, tools in cast(Json, step.get("known_differences", {})).items():
        override = cast(Json, tools).get(env.tool)
        if override is not None:
            env.applied.add(difference)
            expect = {**expect, **cast(Json, env.expand(override))}
    return expect


def check_hook(env: Env, step: Json, run: HookRun, new_ids: list[str]) -> None:
    expect = effective_expect(env, step)
    if not run.registered and expect.get("saves", 0):
        raise CellError(f"no recovery hook is registered for {run.event} in {env.root / 'hooks' / 'hooks.json'}")
    objects = check_invariants(run)
    _check_stdout(run, expect, objects)
    if "saves" in expect and len(new_ids) != int(expect["saves"]):
        raise CellError(f"{run.event}: expected {expect['saves']} new save(s), found {len(new_ids)}")
    if "max_seconds" in expect and run.seconds > float(expect["max_seconds"]):
        raise CellError(f"{run.event}: the hook took {run.seconds:.1f}s, the limit is {expect['max_seconds']}s")
    _check_manifest(env, expect, [env.manifest(save_id) for save_id in new_ids])
    if step.get("as") and new_ids:
        env.variables[str(step["as"])] = new_ids[-1]
