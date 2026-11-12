"""Offline media preparer contract and real-format parser checks.

Run: env -u HOMUNCULUS_NAME PYTHONPATH=src .venv/bin/python -I
quality_gates/tests/fast_return_census_media_smoke.py


The census discovers the module with pkgutil. Its public minimal-parameter
caller is checked with real ActionMetadata loaded from shipped process metadata.
A recording context checks seed's structural contract. When the private-PG
manifest is present, the real context also proves seeded-name lookup and
successful real dispatch or a measured I/O boundary. Timing stays advisory;
a successful call between timing classes is not a fixture failure. The integration leg skips explicitly
without that fixture. Parser/loader calls use shipped plugin code. Workers,
model downloads, microphone and audio devices are never opened.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import io
import json
import os
import pkgutil
import struct
import sys
import unittest
import wave
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / "ananta/src"), *(str(path) for path in sorted(ROOT.glob("plugins/*/src")))]

import fast_return_census as census  # noqa: E402
from ananta.core.actions.action_metadata import ActionExample, ActionMetadata, ParameterMetadata, ParameterType  # noqa: E402
from fast_return_census import minimal_parameters  # noqa: E402
from fast_return_census_context import CensusContext  # noqa: E402
from fast_return_census_fixtures import UnsafeAttempt, audited_setup, citation_definition, isolated_boundaries, library_roots, private_pg_environment, smoke_scratch, temporary_app_home  # noqa: E402


class _Probe:
    """No host transport is part of fixture generation or format parsing."""

    def __init__(self) -> None:
        self.starts: list[str] = []

    def boundary(self, name: str, *, socket_error: bool = False) -> Any:
        del socket_error

        def unexpected(*_args: object, **_kwargs: object) -> None:
            raise AssertionError(f"media preparation reached host boundary {name}")

        return unexpected


class _RecordingContext:
    """Record the structural blob contract; does not stand in for a handler."""

    def __init__(self, home: Path, export_root: Path) -> None:
        self.app_home = home
        self.export_root = export_root
        self.calls: list[tuple[str, str, bytes, str]] = []
        self.state_calls: list[tuple[str, dict[str, Any]]] = []

    def get_service(self, name: str) -> object | None:
        return self if name == "state_service" else None

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        self.state_calls.append((namespace, copy.deepcopy(data)))
        return {"action_status": "completed", "data": {"id": f"recording-{data['record']['external_id']}"}}

    def store_blob(self, namespace: str, filename: str, content: bytes, mime_type: str) -> str:
        self.calls.append((namespace, filename, content, mime_type))
        return f"fixture-{hashlib.sha256(namespace.encode() + filename.encode() + content).hexdigest()}"


def _discover() -> Any:
    package = importlib.import_module("fast_return_census_preparers")
    modules = {item.name: importlib.import_module(f"fast_return_census_preparers.{item.name}") for item in pkgutil.iter_modules(package.__path__)}
    media = modules["media"]
    assert media.PREPARER_NAME == "media"
    return media


def _declared_names(node: ast.AST) -> set[str]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    names: set[str] = set()
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name) and decorator.func.id == "platform_process":
            names.add(_name_argument(decorator, node.name))
    return names


def _name_argument(call: ast.Call, default: str) -> str:
    for keyword in call.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return default


def _source_names(path: Path, audio: bool) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names.update(_declared_names(node))
        if audio and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "FFmpegFilterProcess":
            names.add(_name_argument(node, ""))
    return names


def _expected_population() -> set[tuple[str, str]]:
    """Independent source census of the eight assigned plugin populations."""
    stems = ("audio_processing", "musical_synthesis", "generative_composition", "pedalboard_effects", "ssml", "voice_embeddings", "chatgpt_export_session_source", "claude_ai_export_session_source")
    population: set[tuple[str, str]] = set()
    for stem in stems:
        plugin = f"{stem}_plugin"
        base = ROOT / "plugins" / plugin / "src" / plugin
        if not base.is_dir():
            print(f"SKIP {plugin} handler source census: plugin not present in this profile")
            continue
        names = set().union(*(_source_names(path, stem == "audio_processing") for path in base.rglob("*.py")))
        names.difference_update({"audition_play", "audition_play_set", "audition_stop"})
        population.update((plugin, name) for name in names)
    return population


def _check_population(media: Any) -> set[str]:
    expected = _expected_population()
    present = {plugin for plugin in media.PLUGINS if (ROOT / "plugins" / plugin / "src" / plugin).is_dir()}
    assert {(plugin, verb) for plugin, verb in media.PARAMETER_TABLE if plugin in present} == expected
    assert len(media.PARAMETER_TABLE) == 259
    assert Counter(plugin for plugin, _verb in media.PARAMETER_TABLE) == {
        "audio_processing_plugin": 223,
        "musical_synthesis_plugin": 13,
        "generative_composition_plugin": 8,
        "pedalboard_effects_plugin": 3,
        "ssml_plugin": 3,
        "voice_embeddings_plugin": 7,
        "chatgpt_export_session_source_plugin": 1,
        "claude_ai_export_session_source_plugin": 1,
    }
    return present


def _check_table(media: Any, home: Path, seeded: dict[str, str], owner: str) -> None:
    present = _check_population(media)
    snapshot = copy.deepcopy(media.PARAMETER_TABLE)
    blob_verbs = 0
    filename_verbs = 0
    for (plugin, verb), (citation, _overrides) in media.PARAMETER_TABLE.items():
        if plugin != owner:
            continue
        _check_parameters(media, plugin, verb, home, seeded)
        if plugin in present:
            _check_citation(citation)
            blob_input, filename_input = _check_public_parameters(media, plugin, verb, home, seeded)
            blob_verbs += blob_input
            filename_verbs += filename_input
    assert media.PARAMETER_TABLE == snapshot
    assert {verb for plugin, verb in media.RESIDUAL if plugin == "musical_synthesis_plugin"} == {"generate_chord_pad", "render_note_sequence", "render_arpeggio_pattern", "render_score", "sound_preset_render"}
    assert media.parameters("other", "verb", {"input": 1}, {}, home) == {"input": 1}
    print(f"PASS 259 table entries; {owner}: {blob_verbs} public metadata-derived blob-ID verbs and {filename_verbs} nested filename verb bound to seeded inputs; semantic input citations; five source-only residuals")


def _check_citation(citation: str) -> ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef:
    """Keep table and blob checks on the same strict source-symbol seam."""
    return citation_definition(ROOT, citation)


def _check_parameters(media: Any, plugin: str, verb: str, home: Path, seeded: dict[str, str]) -> None:
    params: dict[str, object] = {"untouched": ["caller"]}
    before = copy.deepcopy(params)
    first = media.parameters(plugin, verb, params, seeded, home)
    second = media.parameters(plugin, verb, params, seeded, home)
    assert first == second, verb
    first["untouched"].append("private copy")
    if plugin == "musical_synthesis_plugin" and verb == "render_sfz_midi":
        assert Path(first["sfz_path"]).is_file()
        assert "instrument_name" not in first
    assert params == before, verb


def _action_metadata(plugin: str, verb: str) -> ActionMetadata:
    """Use the frozen process metadata and production metadata types."""
    path = ROOT / "plugins" / plugin / "knowledge_base/processes" / f"{verb}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    parameters = {
        name: ParameterMetadata(
            type=ParameterType(value["type"]),
            required=value.get("required", False),
            description=value.get("description", ""),
            default=value.get("default"),
            validation=value.get("validation"),
            examples=value.get("examples", []),
        )
        for name, value in raw.get("parameters", {}).items()
    }
    examples = [ActionExample(description=row.get("description", ""), parameters=row["parameters"], expected_output=row.get("expected_output", "")) for row in raw.get("examples", [])]
    return ActionMetadata(name=verb, display_name=raw.get("display_name", verb), description=raw["description"], plugin=plugin, function=verb, parameters=parameters, examples=examples)


def _blob_seed_key(name: str) -> str:
    """Blob inputs are distinct from catalogue, output and stateful record IDs."""
    return {"blob_id": "audio", "blob_id_a": "audio", "blob_id_b": "audio", "audio_blob_id": "audio", "pitch_blob_id": "pitch", "onsets_blob_id": "onsets", "segments_blob_id": "segments", "segments_blob_ids": "segments"}[name]


def _check_public_parameters(media: Any, plugin: str, verb: str, home: Path, seeded: dict[str, str]) -> tuple[bool, bool]:
    action = _action_metadata(plugin, verb)
    incoming = minimal_parameters(action, home)
    before = copy.deepcopy(incoming)
    actual = media.parameters(plugin, verb, incoming, seeded, home)
    inputs = {name for name, metadata in action.parameters.items() if "blob_id" in name and (metadata.required or name in actual)}
    _assert_blob_bindings(plugin, verb, inputs, incoming, actual, seeded)
    if verb == "audition_create":
        assert actual["variants"] == [{"name": "census_fixture", "audio_file": seeded["audio_file"]}]
        _assert_blob_citation(media.PARAMETER_TABLE[(plugin, verb)][0], verb, {"audio_file"})
    assert incoming == before, verb
    if inputs:
        _assert_blob_citation(media.PARAMETER_TABLE[(plugin, verb)][0], verb, inputs)
    return bool(inputs), verb == "audition_create"


def _assert_blob_bindings(plugin: str, verb: str, inputs: set[str], incoming: dict[str, Any], actual: dict[str, Any], seeded: dict[str, str]) -> None:
    for name in sorted(inputs):
        expected: object = seeded[_blob_seed_key(name)]
        if name.endswith("_ids"):
            expected = [expected]
        assert actual.get(name) == expected, (plugin, verb, name, incoming, actual, expected)


def _assert_blob_citation(citation: str, verb: str, inputs: set[str]) -> None:
    cited = ast.unparse(_check_citation(citation))
    assert any(name in cited for name in inputs), (verb, citation, inputs, cited)


def _plugin_present(plugin: str, leg: str) -> bool:
    if (ROOT / "plugins" / plugin / "src" / plugin).is_dir():
        return True
    print(f"SKIP {leg}: {plugin} plugin not present in this profile")
    return False


def _check_stdlib_formats(media: Any) -> None:
    content = media.wav_bytes()
    assert content == media.wav_bytes()
    with wave.open(io.BytesIO(content), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) == (1, 2, 44100, 44100)
        assert any(audio.readframes(44100))
    midi = media.midi_bytes()
    assert midi[:4] == b"MThd" and midi[14:18] == b"MTrk"
    assert struct.unpack(">I", midi[18:22])[0] == len(midi) - 22
    assert ET.fromstring(media.ssml_bytes()).tag.endswith("speak")
    print("PASS unconditional standard-library WAV, MIDI structure and SSML XML checks")


def _check_midi_format(media: Any) -> None:
    if not _plugin_present("musical_synthesis_plugin", "MIDI production parser"):
        return
    import pretty_midi

    midi = pretty_midi.PrettyMIDI(io.BytesIO(media.midi_bytes()))
    assert len(midi.instruments) == 1
    assert len(midi.instruments[0].notes) == 1
    assert midi.instruments[0].notes[0].pitch == 69
    assert midi.get_end_time() == 0.5
    print("PASS production MIDI parser")


def _check_ssml_format(media: Any) -> None:
    if not _plugin_present("ssml_plugin", "SSML production parser and validator"):
        return
    from ssml_plugin.parser import SSMLParser
    from ssml_plugin.validator import SSMLValidator

    valid, errors = SSMLValidator().validate(media.ssml_bytes().decode())
    assert valid, errors
    segments, _metadata = SSMLParser().parse(media.ssml_bytes().decode())
    assert any("Census fixture" in segment.text for segment in segments)
    print("PASS production SSML parser and validator")


def _check_soundfonts(home: Path) -> None:
    if not _plugin_present("musical_synthesis_plugin", "SF2 production loader"):
        return
    from musical_synthesis_plugin.synthesizers import FluidSynthBackend

    directory = home / "data/plugin_data/musical_synthesis_plugin/instruments"
    sf2 = FluidSynthBackend(44100)
    try:
        assert sf2.load(str(directory / "census.sf2")) >= 0
    finally:
        sf2.close()
    print("PASS generated SF2 loads through the production backend")


def _check_archives(media: Any, home: Path) -> None:
    from ananta.llm.session_ledger.vendor import chatgpt_export, claude_ai_export

    for vendor in ("chatgpt", "claude"):
        archive = home / f"{vendor}.zip"
        archive.write_bytes(media.export_archive(vendor))
        assert media.export_archive(vendor) == archive.read_bytes()
        if vendor == "chatgpt":
            conversations = list(chatgpt_export.walk_conversations(archive))
            assert len(conversations) == 1
            assert len(list(chatgpt_export.iter_events_for_conversation(conversations[0]))) == 1
        else:
            payloads = list(claude_ai_export.parse_export_zip(archive))
            assert len(payloads) == 1
            assert len(payloads[0].messages) == 1
    print("PASS both generated archives parse through production vendor readers")


def _check_seed(media: Any, plugin: str, home: Path, export_root: Path) -> None:
    context = _RecordingContext(home, export_root)
    seeded = media.seed(plugin, context)
    assert len(context.calls) == 8
    assert {namespace for namespace, _name, _content, _mime in context.calls} == {plugin}
    assert all(content and mime and name for _namespace, name, content, mime in context.calls)
    assert Path(seeded["export_root"]).is_relative_to(export_root)
    assert Path(seeded["local_audio"]).read_bytes() == media.wav_bytes()
    assert media.seed(plugin, _RecordingContext(home, export_root)) == seeded
    if plugin == "voice_embeddings_plugin":
        assert len(context.state_calls) == 3
        assert len({seeded[f"voice_{purpose}_id"] for purpose in ("read", "delete_name", "delete_id")}) == 3
    _check_table(media, home, seeded, plugin)


def _writes_within(rows: list[dict[str, Any]], scratch: Path) -> bool:
    for row in rows:
        if row["write"] and not all(Path(value).is_relative_to(scratch) for value in row["paths"]):
            return False
    return True


def _expect_refused(path: Path, mode: str) -> None:
    try:
        with path.open(mode):
            pass
    except UnsafeAttempt:
        return
    raise AssertionError(f"undeclared path or read permission was admitted: {path} {mode}")


def _check_device_sink(scratch: Path) -> None:
    """Exercise native open audit: exactly one declared write-only device."""
    home = scratch / "device-home"
    home.mkdir()
    alias = home / "device-alias"
    alias.symlink_to(os.devnull)
    foreign = Path(scratch.anchor) / f"census-forbidden-{uuid4().hex}"
    with isolated_boundaries(ROOT, home, _Probe(), devnull_write=True) as guard:
        refusal = ""
        try:
            with Path(os.devnull).open("wb") as sink:
                sink.write(b"synthetic discarded census output")
        except UnsafeAttempt as error:
            refusal = str(error)
        assert not refusal, f"declared write-only /dev/null sink must be admitted: {refusal}"
        assert guard.files[-1].get("device_sink") is True
        for path, mode in ((foreign, "wb"), (foreign, "rb"), (Path(os.devnull), "rb"), (Path(os.devnull), "w+b"), (Path("/dev/zero"), "wb"), (alias, "wb")):
            _expect_refused(path, mode)
        with (home / "owned").open("wb") as owned:
            owned.write(b"owned")
    with isolated_boundaries(ROOT, home, _Probe()):
        _expect_refused(Path(os.devnull), "wb")
    assert not foreign.exists()
    print("PASS exact declared /dev/null write sink; unrelated writes/reads, device reads, aliases and undeclared sink refused")


def _check_provisioned_media(scratch: Path) -> None:
    """Prove named inputs and real dispatch without a latency pass condition."""
    from quality_gates.private_test_pg import MissingFixtureError, load_fixture

    try:
        load_fixture("fast_return_census")
    except MissingFixtureError:
        print("SKIP real media lookup and dispatch: private PostgreSQL fixture absent")
        return
    required = ("postgres_state_management_plugin", "default_blob_storage_plugin", "default_address_book_plugin", "default_scheduling_plugin", "openai_embeddings_plugin", "pgvector_service_plugin", "actr_memory_plugin")
    if not all(_plugin_present(name, "real media service graph") for name in required):
        return
    cases = {
        "audio_processing_plugin": ("apply_fade", "concatenate_audio", "generate_tone", "import_tagged_file_to_blob"),
        "generative_composition_plugin": ("transform_pattern",),
        "pedalboard_effects_plugin": ("apply_envelope_curve",),
        "ssml_plugin": ("parse_ssml",),
        "voice_embeddings_plugin": ("create_voice_profile",),
    }
    inventory = census._census_inventory(ROOT, scratch, census.Settings())
    for plugin in inventory:
        if plugin.entry.plugin in cases and plugin.plugin_class is not None:
            _check_media_context(plugin, cases[plugin.entry.plugin], scratch)
    if _plugin_present("musical_synthesis_plugin", "real SFZ dispatch and soundfont proofs"):
        _check_synthesis_census(scratch)


@contextmanager
def _media_context(root: Path, scratch: Path, plugin: str, plugin_class: type[Any], probe: Any, modules: list[Any]) -> Generator[CensusContext]:
    """Use real discovery with a distinct schema for each concurrent consumer."""
    with temporary_app_home(scratch) as home:
        context = CensusContext(root, home, plugin, plugin_class, probe, modules)
        context.schema = f"media_{uuid4().hex}"
        context.app_home, context.export_root = home / "application", home / "exports"
        context.app_home.mkdir()
        context.export_root.mkdir()
        environment = {"SOLET_NAME": context.identity, "APP_HOME": str(context.app_home), "HOME": str(home), "TMPDIR": str(home), "TZ": "UTC", "PATH": os.environ.get("PATH", ""), **private_pg_environment(context.config)}
        with patch.dict(os.environ, environment, clear=True), audited_setup(root, home):
            try:
                context.start()
                yield context
            finally:
                context.stop()
    context.receipt["fixture_removed"] = not home.exists()


def _check_synthesis_census(scratch: Path) -> None:
    """The real census owns residual discovery, guards and every timed call."""
    report = census.run_census(ROOT, scratch, census.Settings(), measure=True, only={"musical_synthesis_plugin"}, context_factory=_media_context)
    plugin = next(item for item in report["plugins"] if item["plugin"] == "musical_synthesis_plugin")
    print("MEDIA_SYNTHESIS_CENSUS_REPORT=" + json.dumps(plugin, default=str), flush=True)
    media = _discover()
    rows = {row["verb"]: row for row in plugin["verbs"]}
    for _name, verb in media.RESIDUAL:
        _assert_soundfont_proof(rows[verb])
    sfz = rows["render_sfz_midi"]
    assert sfz["succeeded"] and not sfz["blocked_effects"], sfz
    assert any(item.get("device_sink") and item["allowed"] for item in sfz["file_touches"]), sfz
    assert rows["transform_midi_monophonic"]["succeeded"]
    assert plugin["context_run"]["stopped"] and plugin["context_run"]["fixture_removed"] and report["fixture_removed"]
    print(f"PASS real synthesis census: {plugin['counts']}; SFZ={sfz['class']}; five source-only RESIDUAL proofs remain inconclusive at the async handoff")


def _assert_soundfont_proof(row: dict[str, Any]) -> None:
    """Retain unresolved source evidence at the current asynchronous handoff."""
    assert row["class"] == "RESIDUAL" and row["wall_ms"] is None, row
    assert row["proof"]["result"] == "inconclusive" and not row["proof"]["inline"], row
    assert any(edge.startswith("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:") and edge.endswith(":self._render_jobs.submit") for edge in row["proof"]["dynamic_edges"]), row
    assert "catalog-selected" in row["residual_reason"]


def _check_soundfont_proof_guard() -> None:
    """An actual inline finding or a missing handoff edge must still fail."""
    proof = {"result": "inconclusive", "inline": [], "dynamic_edges": ["plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:1:self._render_jobs.submit"]}
    row = {"class": "RESIDUAL", "wall_ms": None, "proof": proof, "residual_reason": "catalog-selected soundfont remains unresolved"}
    _assert_soundfont_proof(row)
    for broken in ({**proof, "result": "inline_path", "inline": ["subprocess.Popen"]}, {**proof, "dynamic_edges": []}):
        with unittest.TestCase().assertRaises(AssertionError):
            _assert_soundfont_proof({**row, "proof": broken})
    print("PASS residual proof rejects actual inline evidence and missing render-job edge; unresolved never means fast-return")


def _check_media_context(plugin: Any, verbs: tuple[str, ...], scratch: Path) -> None:
    """Own a distinct schema so parallel smoke consumers cannot share state."""
    with _media_context(ROOT, scratch, plugin.entry.plugin, plugin.plugin_class, census.Probe(census.Settings()), census.discover_preparers()) as context:
        _check_real_lookup(context, plugin.entry.plugin)
        for verb in verbs:
            action = next(action for action in plugin.actions if action.name == verb)
            row = census.measure_verb(ROOT, plugin, action, context.app_home, census.Settings(), context)
            assert row["succeeded"] or row["class"] in {"INLINE_IO", "CPU_INLINE"}, (plugin.entry.plugin, verb, row["missing_piece"])
            print(f"PASS real media dispatch {plugin.entry.plugin}.{verb}: {row['class']}; {row['missing_piece']}")


def _check_real_lookup(context: Any, plugin: str) -> None:
    from ananta.utils.naming import resolve_file_by_name

    service = context.get_service("blob_storage_service")
    for key in ("audio", "midi", "ssml"):
        filename = context.seeded[f"{key}_file"]
        actual = resolve_file_by_name(service, filename)
        assert actual == (context.seeded[key], plugin), (filename, actual, context.seeded[key])
    print(f"PASS real seeded WAV/MIDI/SSML naming lookup: {plugin}")


def main() -> int:
    """Discover and exercise the public contract under the owned-file audit."""
    # Editable installations retain other checkouts on sys.path. Metadata
    # discovery walks every entry, so bind import roots before the audit too.
    allowed = (ROOT, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), *library_roots())
    sys.path[:] = [value for value in sys.path if any(Path(value).resolve().is_relative_to(base) for base in allowed)]
    media = _discover()
    _check_soundfont_proof_guard()
    with smoke_scratch(ROOT) as scratch:
        _check_device_sink(scratch)
        home, export_root = scratch / "home", scratch / "exports"
        home.mkdir()
        export_root.mkdir()
        with isolated_boundaries(ROOT, scratch, _Probe()) as guard:
            config = media.configure("musical_synthesis_plugin", home, export_root)
            assert Path(config["sf2_path"]).is_relative_to(home)
            _check_stdlib_formats(media)
            _check_midi_format(media)
            _check_ssml_format(media)
            _check_soundfonts(home)
            _check_archives(media, home)
            for plugin in sorted(media.PLUGINS):
                _check_seed(media, plugin, home, export_root)
            assert not guard.blocked
            assert _writes_within(guard.files, scratch)
        print("PASS public configure/seed/parameters discovery, blob calls and scratch containment")
        _check_provisioned_media(scratch)
    assert not scratch.exists()
    print("PASS owned fixture cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
