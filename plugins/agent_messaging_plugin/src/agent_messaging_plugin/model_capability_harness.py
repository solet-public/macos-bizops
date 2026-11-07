"""Harness model lists for the model capability catalog (iss_0e805222, iss_52fdb8e5; rul_473f772f).

The catalog learns which models exist from the providers' own lists, not from
our table: the harness decides existence, the exact launch id and the offered
efforts; the leaderboard supplies score and cost only. This module reads the
two harness lists and holds the few facts that join them to leaderboard rows.
It is pure readers plus a thin process runner: no state-layer access, and
nothing in it changes any cache or state outside the process.

* Codex: runs the LAUNCHING binary's ``debug models`` listing. The binary path
  is a named parameter (``HarnessContext.codex_binary``), never resolved from
  PATH, because "launchable" is a property of the binary that launches lanes
  (two Codex clients on one host listed different models). ``debug models``
  refreshes Codex's own cache file as it renders; that is the harness
  refreshing itself locally, not this module writing. When the command cannot
  be run, the cache file is read instead as a DEGRADED list that reports its
  ``fetched_at`` and ``client_version``: the file is last-writer-wins between
  client versions, so it can say a model exists but can never prove one absent.
* Claude Code: reads the local ``~/.claude/cache/model-catalog/*.json`` files,
  keeps those whose ``catalog.surface`` is ``cc`` (newest ``fetchedAt``) and
  reports every other surface it ignored. An age past the file's own
  ``staleAt`` (or older than the supplied policy window) is an advisory, never a refusal.

Statuses (what a consumer may conclude):

* ``ok``         the list was read from its authoritative source and offers models.
* ``degraded``   models were read, but from a weaker source (the Codex cache
                 file); they may create cells, but the list cannot prove absence.
* ``unreadable`` nothing could be read (missing, failed, bad JSON, version not
                 understood); ``detail`` names the cause. Zero models.
* ``empty``      the source was read and offers no model: a harness that offers
                 none cannot launch anything, so a consumer treats zero as a
                 read fault, not a fact.

No status refuses anything here: every fault is returned as data with a
``detail`` and ``advisories``, and the consumer decides, loudly.

``RUNTIME_ADAPTERS``, ``JOIN_ALIASES`` and ``join_key`` are the only places the
catalog's join facts live. ``JOIN_ALIASES`` is the sole row that names a model:
one reviewed alias for a leaderboard name whose word order differs from the
harness id. Adding any other model needs no code change.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

HarnessStatus = Literal["ok", "degraded", "unreadable", "empty"]

STATUS_OK: Final[HarnessStatus] = "ok"
STATUS_DEGRADED: Final[HarnessStatus] = "degraded"
STATUS_UNREADABLE: Final[HarnessStatus] = "unreadable"
STATUS_EMPTY: Final[HarnessStatus] = "empty"
HARNESS_STATUSES: Final[tuple[HarnessStatus, ...]] = (STATUS_OK, STATUS_DEGRADED, STATUS_UNREADABLE, STATUS_EMPTY)

RUNTIME_CLAUDE_CODE: Final = "claude_code"
RUNTIME_CODEX: Final = "codex"

PROCESS_TIMEOUT_SECONDS: Final = 60.0
VERSION_TIMEOUT_SECONDS: Final = 10.0
CLAUDE_CACHE_VERSION: Final = 2
CLAUDE_CODE_SURFACE: Final = "cc"
NON_REASONING_EFFORT: Final = "non_reasoning"
CODEX_OFFERED_VISIBILITY: Final = "list"

_NON_ALNUM: Final = re.compile(r"[^a-z0-9]+")
_DATE_STAMP: Final = re.compile(r"-\d{8}$")


class HarnessError(ValueError):
    """A harness source could not be read, or a runtime/adapter lookup was unknown."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


ProcessRunner = Callable[[Sequence[str], float], ProcessResult]


def run_process(argv: Sequence[str], timeout_seconds: float) -> ProcessResult:
    """Default runner: one subprocess, bounded, never through a shell or PATH lookup of a bare name."""
    completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_seconds, check=False)
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class HarnessContext:
    """Everything a reader needs, injected so the readers are testable offline."""

    home: Path
    codex_binary: Path
    run: ProcessRunner
    now: datetime
    list_stale_after: timedelta | None = None


@dataclass(frozen=True, slots=True)
class HarnessModel:
    model_id: str  # the exact launch id, verbatim
    efforts: tuple[str, ...]  # as the harness offers them; ``non_reasoning`` when there is no effort axis
    offered: bool  # Codex visibility == "list"; every Claude entry
    visibility: str  # Codex visibility, or the Claude section ("main", "overflow")


@dataclass(frozen=True, slots=True)
class HarnessList:
    runtime: str
    status: HarnessStatus
    models: tuple[HarnessModel, ...]
    source: str  # command or file the models came from
    detail: str  # why the status is what it is; names the fault for anything but ok
    client_version: str  # Codex ``--version`` / cache ``client_version``; "" when unknown
    fetched_at: str  # ISO UTC the list was fetched; "" when unknown
    advisories: tuple[str, ...] = ()
    ignored_surfaces: tuple[str, ...] = ()  # Claude cache files of another surface (or an older cc file)

    @property
    def offered_models(self) -> tuple[HarnessModel, ...]:
        return tuple(model for model in self.models if model.offered)


def _unreadable(runtime: str, source: str, detail: str, advisories: tuple[str, ...] = (), ignored_surfaces: tuple[str, ...] = ()) -> HarnessList:
    return HarnessList(runtime, STATUS_UNREADABLE, (), source, detail, "", "", advisories, ignored_surfaces)


def _as_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _as_objects(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    return [entry for entry in (_as_object(item) for item in value) if entry is not None]


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"{path}: {exc}") from exc
    payload = _as_object(parsed)
    if payload is None:
        raise HarnessError(f"{path}: top level is not a JSON object")
    return payload


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: object) -> datetime | None:
    """Parse a cache timestamp: epoch milliseconds (Claude) or an ISO string (Codex)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _age_advisories(label: str, fetched: datetime | None, stale_at: datetime | None, now: datetime, window: timedelta | None) -> tuple[str, ...]:
    notes: list[str] = []
    if fetched is None or window is None:
        notes.append(f"{label} age or policy window is unknown; used as the best list available")
    elif now - fetched > window:
        notes.append(f"{label} was fetched {_iso(fetched)}, older than {window}; used as the best list available")
    if stale_at is not None and now > stale_at:
        notes.append(f"{label} is past its own staleAt {_iso(stale_at)}; Claude Code refreshes it only while a session runs; used as the best list available")
    return tuple(notes)


# --------------------------------------------------------------------------- Codex


def _codex_binary_fault(path: Path) -> str:
    if not path.is_absolute():
        return f"codex binary path is not absolute (it is never resolved from PATH): {path}"
    if not path.is_file() or not os.access(path, os.X_OK):
        return f"codex binary is not an executable file: {path}"
    return ""


def _run_codex(ctx: HarnessContext, args: tuple[str, ...], timeout_seconds: float) -> str:
    fault = _codex_binary_fault(ctx.codex_binary)
    if fault:
        raise HarnessError(fault)
    command = " ".join((str(ctx.codex_binary), *args))
    try:
        result = ctx.run((str(ctx.codex_binary), *args), timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"`{command}` could not run: {exc}") from exc
    if result.returncode != 0:
        raise HarnessError(f"`{command}` exited {result.returncode}: {result.stderr.strip()[:200]}")
    return result.stdout


def _codex_version(ctx: HarnessContext) -> str:
    try:
        return _run_codex(ctx, ("--version",), VERSION_TIMEOUT_SECONDS).strip()
    except HarnessError:
        return ""


def _codex_efforts(entry: Mapping[str, object]) -> tuple[str, ...]:
    levels = _as_objects(entry.get("supported_reasoning_levels")) or []
    return tuple(str(level["effort"]) for level in levels if isinstance(level.get("effort"), str))


def _codex_models(payload: Mapping[str, object]) -> tuple[tuple[HarnessModel, ...], tuple[str, ...]]:
    entries = _as_objects(payload.get("models"))
    if entries is None:
        raise HarnessError("no `models` array in the Codex listing")
    models: list[HarnessModel] = []
    skipped = 0
    for entry in entries:
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            skipped += 1
            continue
        visibility = str(entry.get("visibility", ""))
        models.append(HarnessModel(slug, _codex_efforts(entry), visibility == CODEX_OFFERED_VISIBILITY, visibility))
    notes = (f"{skipped} Codex model entries had no slug and were skipped",) if skipped else ()
    return tuple(models), notes


def _status_for(models: Sequence[HarnessModel], authoritative: bool) -> HarnessStatus:
    if not any(model.offered for model in models):
        return STATUS_EMPTY
    return STATUS_OK if authoritative else STATUS_DEGRADED


def _codex_from_command(ctx: HarnessContext) -> HarnessList:
    version = _codex_version(ctx)
    stdout = _run_codex(ctx, ("debug", "models"), PROCESS_TIMEOUT_SECONDS)
    try:
        payload = _as_object(json.loads(stdout))
    except ValueError as exc:
        raise HarnessError(f"`debug models` output is not JSON: {exc}") from exc
    if payload is None:
        raise HarnessError("`debug models` output is not a JSON object")
    models, notes = _codex_models(payload)
    advisories = notes if version else (*notes, "codex --version did not answer; client version unknown")
    status = _status_for(models, True)
    detail = f"{len(models)} models from {ctx.codex_binary} debug models (client {version or 'unknown'})"
    return HarnessList(RUNTIME_CODEX, status, models, f"{ctx.codex_binary} debug models", detail, version, _iso(ctx.now), advisories)


def _codex_from_cache(ctx: HarnessContext, command_fault: str) -> HarnessList:
    path = ctx.home / ".codex" / "models_cache.json"
    source = str(path)
    try:
        payload = _load_json_object(path)
        models, notes = _codex_models(payload)
    except HarnessError as exc:
        return _unreadable(RUNTIME_CODEX, source, f"{command_fault}; cache file also unreadable: {exc}")
    fetched = _timestamp(payload.get("fetched_at"))
    fetched_text = _iso(fetched) if fetched is not None else ""
    client_version = str(payload.get("client_version", ""))
    status = _status_for(models, False)
    detail = f"{command_fault}; read the cache file {source} instead (fetched_at {fetched_text or 'unknown'}, client_version {client_version or 'unknown'}); the file is last-writer-wins between Codex client versions, so this list cannot prove a model absent"
    advisories = (*notes, *_age_advisories("Codex cache file", fetched, None, ctx.now, ctx.list_stale_after))
    return HarnessList(RUNTIME_CODEX, status, models, source, detail, client_version, fetched_text, advisories)


def read_codex_harness(ctx: HarnessContext) -> HarnessList:
    """List the models the launching Codex binary offers; the cache file only as a degraded read."""
    try:
        return _codex_from_command(ctx)
    except HarnessError as exc:
        return _codex_from_cache(ctx, str(exc))


# --------------------------------------------------------------------------- Claude Code


@dataclass(frozen=True, slots=True)
class _ClaudeFile:
    path: Path
    payload: dict[str, object]
    surface: str
    fetched: datetime | None


def _claude_files(directory: Path) -> tuple[list[_ClaudeFile], list[str]]:
    files: list[_ClaudeFile] = []
    faults: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = _load_json_object(path)
        except HarnessError as exc:
            faults.append(f"cache file skipped: {exc}")
            continue
        catalog = _as_object(payload.get("catalog")) or {}
        files.append(_ClaudeFile(path, payload, str(catalog.get("surface", "")), _timestamp(payload.get("fetchedAt"))))
    return files, faults


def _claude_efforts(entry: Mapping[str, object]) -> tuple[tuple[str, ...], str]:
    thinking = _as_object(entry.get("thinking")) or {}
    kind = thinking.get("type")
    if kind == "none":
        return (NON_REASONING_EFFORT,), ""
    options = _as_objects(thinking.get("effort_options"))
    if kind == "effort" and options is not None:
        return tuple(str(option["id"]) for option in options if isinstance(option.get("id"), str)), ""
    return (), f"thinking type {kind!r} is not understood"


def _claude_models(chosen: _ClaudeFile) -> tuple[tuple[HarnessModel, ...], tuple[str, ...]]:
    catalog = _as_object(chosen.payload.get("catalog")) or {}
    config = _as_object(catalog.get("config")) or {}
    entries = _as_objects(config.get("models"))
    if entries is None:
        raise HarnessError(f"{chosen.path}: no catalog.config.models array")
    models: list[HarnessModel] = []
    notes: list[str] = []
    for entry in entries:
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            notes.append("a Claude model entry had no id and was skipped")
            continue
        efforts, fault = _claude_efforts(entry)
        if fault:
            notes.append(f"{model_id}: {fault}; no efforts recorded")
        models.append(HarnessModel(model_id, efforts, True, str(entry.get("section", ""))))
    return tuple(models), tuple(notes)


def _claude_pick(files: Sequence[_ClaudeFile]) -> tuple[_ClaudeFile | None, tuple[str, ...]]:
    surfaces = [item for item in files if item.surface == CLAUDE_CODE_SURFACE]
    ignored = [f"{item.surface or 'unknown'}:{item.path.name}" for item in files if item.surface != CLAUDE_CODE_SURFACE]
    if not surfaces:
        return None, tuple(ignored)
    epoch = datetime.fromtimestamp(0, tz=UTC)
    ordered = sorted(surfaces, key=lambda item: item.fetched or epoch, reverse=True)
    ignored.extend(f"{item.surface}:{item.path.name} (older)" for item in ordered[1:])
    return ordered[0], tuple(ignored)


def read_claude_harness(ctx: HarnessContext) -> HarnessList:
    """List the models Claude Code's local cache offers on the ``cc`` surface."""
    directory = ctx.home / ".claude" / "cache" / "model-catalog"
    source = str(directory)
    files, faults = _claude_files(directory)
    chosen, ignored = _claude_pick(files)
    if chosen is None:
        seen = f"; ignored surfaces: {', '.join(ignored)}" if ignored else ""
        return _unreadable(RUNTIME_CLAUDE_CODE, source, f"no `{CLAUDE_CODE_SURFACE}` surface cache file under {source}{seen}", tuple(faults), ignored)
    version = chosen.payload.get("version")
    if version != CLAUDE_CACHE_VERSION:
        detail = f"{chosen.path}: cache version {version!r} is not understood (expected {CLAUDE_CACHE_VERSION})"
        return _unreadable(RUNTIME_CLAUDE_CODE, str(chosen.path), detail, tuple(faults), ignored)
    try:
        models, notes = _claude_models(chosen)
    except HarnessError as exc:
        return _unreadable(RUNTIME_CLAUDE_CODE, str(chosen.path), str(exc), tuple(faults), ignored)
    fetched_text = _iso(chosen.fetched) if chosen.fetched is not None else ""
    age = _age_advisories("Claude Code model cache", chosen.fetched, _timestamp(chosen.payload.get("staleAt")), ctx.now, ctx.list_stale_after)
    detail = f"{len(models)} models from {chosen.path} (fetched {fetched_text or 'unknown'})"
    return HarnessList(RUNTIME_CLAUDE_CODE, _status_for(models, True), models, str(chosen.path), detail, "", fetched_text, (*faults, *notes, *age), ignored)


# --------------------------------------------------------------------------- runtime adapters


@dataclass(frozen=True, slots=True)
class RuntimeAdapter:
    """Installed launch implementation; provider registrations do not create runtimes."""

    runtime: str
    provider: str
    creator_labels: frozenset[str]  # lowercased leaderboard creator names this runtime serves
    read_harness: Callable[[HarnessContext], HarnessList]
    id_shape: re.Pattern[str]  # launch-id shape; used only when a harness list is unavailable
    derive_id: Callable[[str], str]  # leaderboard base name -> launch id; used only when a harness list is unavailable

    @property
    def implementation_key(self) -> tuple[str, str]:
        """Versioned implementation identity selectable by a CatalogSource registration."""
        return (f"harness.{self.runtime}", "1")


def _derive_dotless_id(base_name: str) -> str:
    return join_key(base_name)


def _derive_dotted_id(base_name: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", base_name.lower()).strip("-")


RUNTIME_ADAPTERS: Final[Mapping[str, RuntimeAdapter]] = MappingProxyType(
    {
        RUNTIME_CLAUDE_CODE: RuntimeAdapter(
            RUNTIME_CLAUDE_CODE,
            "anthropic",
            frozenset({"anthropic"}),
            read_claude_harness,
            re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$"),  # dots are hyphens in Anthropic launch ids
            _derive_dotless_id,
        ),
        RUNTIME_CODEX: RuntimeAdapter(
            RUNTIME_CODEX,
            "openai",
            frozenset({"openai"}),
            read_codex_harness,
            re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$"),  # dots are kept in OpenAI launch ids
            _derive_dotted_id,
        ),
    }
)


def adapter_for_runtime(runtime: str) -> RuntimeAdapter:
    """The adapter for a runtime; an unknown runtime raises, it never defaults to a provider."""
    adapter = RUNTIME_ADAPTERS.get(runtime)
    if adapter is None:
        raise HarnessError(f"unknown runtime {runtime!r}; known: {sorted(RUNTIME_ADAPTERS)}")
    return adapter


def adapter_for_creator(creator_label: str) -> RuntimeAdapter | None:
    """The adapter serving a leaderboard creator label, or None: a creator with no runtime is listed, not dispatchable."""
    label = creator_label.strip().lower()
    return next((adapter for adapter in RUNTIME_ADAPTERS.values() if label in adapter.creator_labels), None)


# --------------------------------------------------------------------------- the join


JOIN_ALIASES: Final[Mapping[tuple[str, str], str]] = MappingProxyType(
    {
        # (creator, leaderboard base name) -> launch id. Added only when a harness model and a leaderboard
        # name each have no match and their join-key tokens are equal (reported as an alias candidate).
        ("anthropic", "Claude 4.5 Haiku"): "claude-haiku-4-5-20251001",
    }
)


def join_key(text: str) -> str:
    """Lowercase, every run of non-alphanumeric characters becomes ``-``, leading and trailing ``-`` stripped."""
    return _NON_ALNUM.sub("-", text.lower()).strip("-")


def harness_join_keys(model_id: str) -> tuple[str, ...]:
    """The join key of a launch id, plus the key of its stem when it ends in an eight-digit date stamp."""
    full = join_key(model_id)
    stem = _DATE_STAMP.sub("", full)
    return (full,) if stem == full else (full, stem)


@dataclass(frozen=True, slots=True)
class JoinAmbiguity:
    key: str
    names: tuple[str, ...]
    model_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AliasCandidate:
    name: str
    model_id: str


@dataclass(frozen=True, slots=True)
class JoinResult:
    joined: dict[str, str]  # leaderboard base name -> harness launch id
    via_alias: tuple[str, ...]  # the joined names that were joined by JOIN_ALIASES
    ambiguous: tuple[JoinAmbiguity, ...]  # never joined by a guess
    unmatched_names: tuple[str, ...]  # listed on the leaderboard, not launchable
    unmatched_model_ids: tuple[str, ...]  # offered by the harness, no leaderboard row
    alias_candidates: tuple[AliasCandidate, ...]  # reported only; a sorted-token match can collide


def _signature(key: str) -> tuple[str, ...]:
    return tuple(sorted(key.split("-")))


def _key_indexes(model_ids: Sequence[str]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    exact: dict[str, set[str]] = {}
    stems: dict[str, set[str]] = {}
    for model_id in model_ids:
        keys = harness_join_keys(model_id)
        exact.setdefault(keys[0], set()).add(model_id)
        for stem in keys[1:]:
            stems.setdefault(stem, set()).add(model_id)
    return exact, stems


def _alias_target(creator: str, name: str, aliases: Mapping[tuple[str, str], str], offered: frozenset[str]) -> str | None:
    target = aliases.get((creator.strip().lower(), name.strip()))
    return target if target in offered else None


def _alias_candidates(names: Sequence[str], model_ids: Sequence[str]) -> tuple[AliasCandidate, ...]:
    found: list[AliasCandidate] = []
    for name in names:
        wanted = _signature(join_key(name))
        found.extend(AliasCandidate(name, model_id) for model_id in model_ids if wanted in {_signature(key) for key in harness_join_keys(model_id)})
    return tuple(found)


@dataclass(slots=True)
class _JoinTally:
    joined: dict[str, str]
    via_alias: list[str]
    ambiguous: list[JoinAmbiguity]
    unmatched: list[str]


def _join_group(tally: _JoinTally, creator: str, key: str, names: list[str], ids: list[str], aliases: Mapping[tuple[str, str], str], offered: frozenset[str]) -> None:
    if ids and (len(ids) > 1 or len(names) > 1):
        tally.ambiguous.append(JoinAmbiguity(key, tuple(names), tuple(ids)))
    elif ids:
        tally.joined[names[0]] = ids[0]
    else:
        for name in names:
            target = _alias_target(creator, name, aliases, offered)
            if target is None:
                tally.unmatched.append(name)
            else:
                tally.joined[name] = target
                tally.via_alias.append(name)


def _name_groups(base_names: Sequence[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name in dict.fromkeys(base_names):
        groups.setdefault(join_key(name), []).append(name)
    return groups


def join_models(creator: str, base_names: Sequence[str], models: Sequence[HarnessModel], aliases: Mapping[tuple[str, str], str] = JOIN_ALIASES) -> JoinResult:
    """Join leaderboard base names (effort suffix already removed) to the harness's offered launch ids.

    An exact join key wins over a date-stem key. A key that matches several
    models, or several names that share one key, is reported as ambiguous and
    joined to nothing. A name with no match uses its alias row; unmatched pairs
    whose sorted tokens agree are reported as alias candidates, never joined.
    """
    offered_ids = tuple(dict.fromkeys(model.model_id for model in models if model.offered))
    offered = frozenset(offered_ids)
    exact, stems = _key_indexes(offered_ids)
    tally = _JoinTally({}, [], [], [])
    for key, names in _name_groups(base_names).items():
        _join_group(tally, creator, key, names, sorted(exact.get(key) or stems.get(key) or ()), aliases, offered)
    taken = set(tally.joined.values()) | {model_id for item in tally.ambiguous for model_id in item.model_ids}
    free_ids = tuple(model_id for model_id in offered_ids if model_id not in taken)
    return JoinResult(tally.joined, tuple(tally.via_alias), tuple(tally.ambiguous), tuple(tally.unmatched), free_ids, _alias_candidates(tally.unmatched, free_ids))
