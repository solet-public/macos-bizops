"""Owned media inputs for the advisory fast-return census.

This module implements design 4.2 without importing the context provider.
Seeding runs before measurement. No handler or platform service is replaced.
The parameter table records qualified source symbols independently of registry
metadata. Numeric line hints are advisory; the cited symbol must still exist.
A new handler requires an explicit table correction.
"""

from __future__ import annotations

import copy
import io
import json
import math
import struct
import wave
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

PREPARER_NAME = "media"
SOUNDFONT_RESIDUAL = "catalog-selected soundfont path under lane binary_libraries is absent in a lane checkout; no per-verb sf2_path exists, and the census may not download or read outside scratch; configured owned SF2 is bypassed by invocation catalog precedence"
RESIDUAL = {("musical_synthesis_plugin", verb): SOUNDFONT_RESIDUAL for verb in ("generate_chord_pad", "render_note_sequence", "render_arpeggio_pattern", "render_score", "sound_preset_render")}
DEVNULL_WRITE_PLUGINS = frozenset({"musical_synthesis_plugin"})

PLUGINS = frozenset(
    {
        "audio_processing_plugin",
        "musical_synthesis_plugin",
        "generative_composition_plugin",
        "pedalboard_effects_plugin",
        "ssml_plugin",
        "voice_embeddings_plugin",
        "chatgpt_export_session_source_plugin",
        "claude_ai_export_session_source_plugin",
    }
)


def prove(root: Path, plugin_class: type[object], function_name: str) -> dict[str, object]:
    """Retain the existing source prover's inline findings and unknown edges.

    This is not a measured fast-return result or an exemption for rendering.
    Catalog-selected SF2 inputs cannot be safely supplied by a lane fixture.
    """
    from fast_return_census_preparers.residual import prove as residual_proof

    return residual_proof(root, plugin_class, function_name)


class CensusContextView(Protocol):
    """The context's real blob service and owned paths (design 4.1)."""

    app_home: Path
    export_root: Path
    registry: Any

    def get_service(self, name: str) -> object | None: ...

    def store_blob(self, namespace: str, filename: str, content: bytes, mime_type: str) -> str: ...

    def ready(self, instance: Any) -> None: ...


class SeedState(Protocol):
    """Only the real state operation needed to create owned voice records."""

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]: ...


def wav_bytes() -> bytes:
    """One second of mono PCM16 at 44.1 kHz, with a deterministic 440 Hz tone."""
    buffer = io.BytesIO()
    frames = b"".join(struct.pack("<h", round(4096 * math.sin(2 * math.pi * 440 * i / 44100))) for i in range(44100))
    with wave.open(buffer, "wb") as output:
        output.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        output.writeframes(frames)
    return buffer.getvalue()


def ssml_bytes() -> bytes:
    """A complete document accepted by the plugin parser and validator."""
    return b'<?xml version="1.0"?><speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0" xml:lang="en-US">Census fixture.<break time="100ms"/></speak>'


def midi_bytes() -> bytes:
    """Format-zero MIDI: tempo, program zero, A4 for half a second, end."""
    track = bytes.fromhex("00ff510307a12000c00000904560836080450000ff2f00")
    return b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480) + b"MTrk" + struct.pack(">I", len(track)) + track


def _chunk(kind: bytes, data: bytes) -> bytes:
    return kind + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")


def soundfont_bytes() -> bytes:
    """A single-sample SF2 bank, generated without a binary asset dependency."""
    samples = 4410
    pcm = b"".join(struct.pack("<h", round(4096 * math.sin(2 * math.pi * 440 * i / 44100))) for i in range(samples))
    info = _chunk(b"LIST", b"INFO" + _chunk(b"ifil", struct.pack("<HH", 2, 1)) + _chunk(b"isng", b"EMU8000\0") + _chunk(b"INAM", b"Census\0\0"))
    sdta = _chunk(b"LIST", b"sdta" + _chunk(b"smpl", pcm + bytes(92)))
    phdr = struct.pack("<20sHHHIII", b"Census", 0, 0, 0, 0, 0, 0) + struct.pack("<20sHHHIII", b"EOP", 0, 0, 1, 0, 0, 0)
    inst = struct.pack("<20sH", b"Census", 0) + struct.pack("<20sH", b"EOI", 1)
    shdr = struct.pack("<20sIIIIIBbHH", b"Census", 0, samples, 100, samples - 100, 44100, 69, 0, 0, 1) + bytes(46)
    records = ((b"phdr", phdr), (b"pbag", struct.pack("<HHHH", 0, 0, 1, 0)), (b"pmod", bytes(10)), (b"pgen", struct.pack("<HH", 41, 0)), (b"inst", inst), (b"ibag", struct.pack("<HHHH", 0, 0, 3, 0)), (b"imod", bytes(10)), (b"igen", struct.pack("<HHHHHH", 54, 1, 58, 69, 53, 0)), (b"shdr", shdr))
    pdta = _chunk(b"LIST", b"pdta" + b"".join(_chunk(kind, data) for kind, data in records))
    return _chunk(b"RIFF", b"sfbk" + info + sdta + pdta)


def export_archive(vendor: str) -> bytes:
    """A deterministic ZIP containing one synthetic vendor conversation."""
    if vendor == "chatgpt":
        conversations: list[dict[str, object]] = [
            {
                "id": "census-conversation",
                "title": "Census",
                "create_time": 1735689600,
                "update_time": 1735689600,
                "current_node": "census-message",
                "mapping": {"census-message": {"id": "census-message", "parent": None, "children": [], "message": {"id": "census-message", "author": {"role": "user"}, "create_time": 1735689600, "content": {"content_type": "text", "parts": ["Census fixture"]}}}},
            }
        ]
    else:
        conversations = [
            {
                "uuid": "00000000-0000-4000-8000-000000000001",
                "name": "Census",
                "created_at": "2025-01-01T00:00:00Z",
                "updated_at": "2025-01-01T00:00:00Z",
                "chat_messages": [
                    {
                        "uuid": "00000000-0000-4000-8000-000000000002",
                        "sender": "human",
                        "text": "Census fixture",
                        "content": [{"type": "text", "text": "Census fixture"}],
                        "created_at": "2025-01-01T00:00:00Z",
                        "updated_at": "2025-01-01T00:00:00Z",
                        "parent_message_uuid": "00000000-0000-0000-0000-000000000000",
                        "attachments": [],
                        "files": [],
                    }
                ],
            }
        ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        entry = zipfile.ZipInfo("conversations.json", date_time=(2025, 1, 1, 0, 0, 0))
        archive.writestr(entry, json.dumps(conversations, sort_keys=True))
    return buffer.getvalue()


def configure(plugin: str, home: Path, export_root: Path) -> dict[str, object]:
    """Prepare files only in the supplied context; return pre-readiness config."""
    del export_root
    if plugin != "musical_synthesis_plugin":
        return {}
    directory = home / "data/plugin_data/musical_synthesis_plugin/instruments"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "census.sf2"
    path.write_bytes(soundfont_bytes())
    return {"sf2_path": str(path)}


def before_verb(plugin: str, verb: str, context: CensusContextView) -> None:
    """Ready the actual registry target of sweep's nested plugin dispatch."""
    if plugin == "audio_processing_plugin" and verb == "parameter_sweep":
        context.ready(context.registry.get_plugin(plugin))


def seed(plugin: str, context: CensusContextView) -> dict[str, str]:
    """Publish valid named inputs through the context's real store_blob."""
    if plugin not in PLUGINS:
        return {}
    payloads = {
        "audio": ("census_input.wav", wav_bytes(), "audio/wav"),
        "midi": ("census_input.mid", midi_bytes(), "audio/midi"),
        "ssml": ("census_input.ssml", ssml_bytes(), "application/ssml+xml"),
        # Exact formats consumed by audio_analysis._read_pitch_csv/_read_onsets
        # and voice_prosody._read_csv_blob, not generated analysis results.
        "pitch": ("census_pitch.csv", b"time_s,f0_hz,voiced_prob\n0,440,1\n0.25,440,1\n0.5,440,1\n0.75,440,1\n1,440,1\n", "text/csv"),
        "onsets": ("census_onsets.json", b'{"units":"time","onsets":[0,0.5]}', "application/json"),
        "segments": ("census_segments.csv", b"phrase_index,start_s,end_s,duration_s,peak_rms,mean_rms,attack_s,ending_shape\n0,0,1,1,0.1,0.08,0.02,flat\n", "text/csv"),
    }
    result: dict[str, str] = {}
    for key, (filename, content, mime) in payloads.items():
        result[key] = context.store_blob(plugin, filename, content, mime)
        result[f"{key}_file"] = filename
    local = context.export_root / plugin
    local.mkdir(parents=True, exist_ok=True)
    (local / "census_input.wav").write_bytes(wav_bytes())
    if plugin == "musical_synthesis_plugin":
        sfz = local / "census.sfz"
        sfz.write_text("<region> sample=census_input.wav key=69\n", encoding="utf-8")
        result["sfz_path"] = str(sfz)
    result["local_audio"] = str(local / "census_input.wav")
    (local / "census_input.wav.json").write_text(json.dumps({"name": "Census imported", "external_id": "census_imported_wav", "original_name": "census_imported.wav", "mime_type": "audio/wav"}), encoding="utf-8")
    if plugin == "voice_embeddings_plugin":
        result.update(_seed_voice_profiles(context))
    if plugin == "audio_processing_plugin":
        audition = context.app_home / "data/plugin_data/audio_processing_plugin/audition_sets/census_fixture"
        audition.mkdir(parents=True, exist_ok=True)
        (audition / "manifest.json").write_text(json.dumps({"audition_set_id": "census_fixture", "variants": [{"name": "census_fixture", "blob_id": result["audio"], "clip_path": result["local_audio"]}]}), encoding="utf-8")
    for vendor in ("chatgpt", "claude"):
        content = export_archive(vendor)
        filename = f"census_{vendor}.zip"
        result[vendor] = context.store_blob(plugin, filename, content, "application/zip")
        path = local / filename
        path.write_bytes(content)
        result[f"{vendor}_path"] = str(path)
    result["export_root"] = str(local)
    return result


def _seed_voice_profiles(context: CensusContextView) -> dict[str, str]:
    """Separate read/update and deletion records prevent census-order loss."""
    state = cast(SeedState, context.get_service("state_service"))
    result: dict[str, str] = {}
    for purpose in ("read", "delete_name", "delete_id"):
        name = f"census_{purpose}"
        reply = state.write_state("voice_embeddings_plugin", {"table": "voice_profile", "record": {"name": name, "external_id": name, "status": "active", "sample_count": 0, "total_duration_seconds": 0.0, "tags": [], "metadata": {"source": "owned census fixture"}}})
        data = reply.get("data", {})
        identifier = data.get("result", {}).get("generated_id") or data.get("id")
        if reply.get("action_status") != "completed" or not identifier:
            raise RuntimeError(f"real voice profile seed failed: {reply}")
        result[f"voice_{purpose}_name"] = name
        result[f"voice_{purpose}_id"] = str(identifier)
    return result


# Each entry below is (handler validation path:line, explicit parameter overrides).
# '$key' resolves an input produced by seed; '@home/...' resolves an owned path.
PARAMETER_TABLE: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}


def parameters(plugin: str, verb: str, params: dict[str, object], seeded: Mapping[str, str], home: Path) -> dict[str, object]:
    """Apply an explicit table entry without mutating caller or table objects."""
    result = copy.deepcopy(params)
    entry = PARAMETER_TABLE.get((plugin, verb))
    if entry is not None:
        result.update(_resolve(entry[1], seeded, home, verb))
    if plugin == "musical_synthesis_plugin" and verb == "render_sfz_midi":
        result.pop("instrument_name", None)
    return result


def _resolve(value: dict[str, object], seeded: Mapping[str, str], home: Path, verb: str) -> dict[str, object]:
    def resolve(item: object) -> object:
        if isinstance(item, str):
            if item.startswith("$"):
                return seeded[item[1:]]
            if item.startswith("@home/"):
                return str(home / item[6:])
            return item.replace("{verb}", verb)
        if isinstance(item, dict):
            return {str(k): resolve(v) for k, v in item.items()}
        if isinstance(item, list):
            return [resolve(v) for v in item]
        return item

    return {key: resolve(item) for key, item in value.items()}


# Frozen handler-derived entries; no runtime source or metadata inference.
PARAMETER_TABLE.update(
    {
        ("audio_processing_plugin", "apply_convolution_reverb"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:356#apply_convolution_reverb",
            {"input_audio_file": "$audio_file", "impulse_response_name": "census_ir", "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "apply_dynamic_eq_keyed"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/sidechain.py:337#apply_dynamic_eq_keyed",
            {"frequency_hz": 1000.0, "output_audio_format": "wav", "input_audio_file": "$audio_file", "sidechain_audio_file": "$audio_file", "output_audio_file": "census_{verb}"},
        ),
        ("audio_processing_plugin", "apply_fade"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:212#apply_fade", {"input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "apply_reverb_ducking"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:298#apply_reverb_ducking", {"input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "output_audio_format": "wav", "sidechain_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "apply_sidechain_ducking"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effects.py:344#apply_sidechain_ducking", {"input_audio_file": "$audio_file", "sidechain_audio_file": "$audio_file", "output_audio_file": "census_{verb}"}),
        ("audio_processing_plugin", "apply_tremolo"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:170#apply_tremolo", {"input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "output_audio_format": "wav", "rate_hz": 4.0}),
        ("audio_processing_plugin", "assert_clipping_gate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "assert_dc_offset_gate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "assert_dropouts_gate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "assert_glitch_gate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:592#detect_glitches", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "assert_hiss_gate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "assert_hum_gate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "assert_silence_gaps_gate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "assert_spectral_discontinuity_gate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "assert_transient_bursts_gate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "audition_create"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/audition/sets.py:97#_validate_variants", {"clip_seconds": 0.5, "variants": [{"name": "census_fixture", "audio_file": "$audio_file"}]}),
        ("audio_processing_plugin", "audition_verdict_log"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2552#AudioProcessingPlugin.audition_verdict_log", {"name": "census_fixture", "verdict": "keep", "audition_set_id": "census_fixture"}),
        ("audio_processing_plugin", "band_energy_ratio"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:1858#AudioProcessingPlugin.band_energy_ratio", {"blob_id": "$audio", "bands": [{"name": "tone", "low_hz": 200.0, "high_hz": 1000.0}]}),
        ("audio_processing_plugin", "build_voice_cadence_profile"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:3466#AudioProcessingPlugin.build_voice_cadence_profile", {"segments_blob_ids": ["$segments"], "profile_id": "census_profile"}),
        ("audio_processing_plugin", "classify_phrase_contour"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:3419#AudioProcessingPlugin.classify_phrase_contour", {"blob_id": "$audio", "segments_blob_id": "$segments"}),
        ("audio_processing_plugin", "compare_audio"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:1901#AudioProcessingPlugin.compare_audio", {"blob_id_a": "$audio", "blob_id_b": "$audio"}),
        ("audio_processing_plugin", "compute_spectrogram"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2856#AudioProcessingPlugin.compute_spectrogram", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "concatenate_audio"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:922#AudioProcessingPlugin.concatenate_audio", {"input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "corrupt_audio"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality_corruption.py:289#corrupt_audio", {"output_audio_format": "wav", "input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "corruption_kind": "clipping"}),
        ("audio_processing_plugin", "detect_clipping"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "detect_dc_offset"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "detect_dropouts"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "detect_glitches"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:592#detect_glitches", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "detect_hiss"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "detect_hum"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "detect_silence_gaps"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "detect_spectral_discontinuity"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "detect_transient_bursts"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality/_common.py:66#require_input_file", {"input_audio_file": "$audio_file"}),
        ("audio_processing_plugin", "estimate_key"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2778#AudioProcessingPlugin.estimate_key", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "estimate_tempo"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2660#AudioProcessingPlugin.estimate_tempo", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "evaluate_audio_quality"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality_actions.py:501#evaluate_audio_quality", {"input_audio_file": "$audio_file", "output_report_file": "census_{verb}_report"}),
        ("audio_processing_plugin", "execute_ffmpeg"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:1103#AudioProcessingPlugin.execute_ffmpeg", {"input_audio_files": ["$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav", "processing": "anull"}),
        ("audio_processing_plugin", "export_blob_to_tagged_file"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:1558#AudioProcessingPlugin.export_blob_to_tagged_file", {"blob_id": "$audio", "output_dir": "$export_root"}),
        ("audio_processing_plugin", "extract_audio_quality_features"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/quality_actions.py:435#extract_audio_quality_features", {"input_audio_file": "$audio_file", "output_report_file": "census_{verb}_report"}),
        ("audio_processing_plugin", "extract_chroma"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2739#AudioProcessingPlugin.extract_chroma", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "extract_macro_arc"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:1946#AudioProcessingPlugin.extract_macro_arc", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "extract_mfcc"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2817#AudioProcessingPlugin.extract_mfcc", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "extract_onsets"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:1733#AudioProcessingPlugin.extract_onsets", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "extract_per_phrase_cc_envelope"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:3507#AudioProcessingPlugin.extract_per_phrase_cc_envelope", {"blob_id": "$audio", "segments_blob_id": "$segments"}),
        ("audio_processing_plugin", "extract_pitch_track"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/audio_analysis.py:94#extract_pitch_track", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "extract_rms_envelope"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:1690#AudioProcessingPlugin.extract_rms_envelope", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "ffmpeg_a3dscope"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aap"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_abench"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_abitscope"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_acompressor"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_acontrast"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_acopy"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_acrossfade"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_acrossover"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_acrusher"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_acue"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_adeclick"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_adeclip"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_adecorrelate"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_adelay"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_adenorm"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aderivative"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_adrawgraph"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_adrc"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_adynamicequalizer"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_adynamicsmooth"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aecho"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_aemphasis"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aeval"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_aexciter"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_afade"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_afftdn"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_afftfilt"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_afir"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_aformat"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_afreqshift"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_afwtdn"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_agate"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_agraphmonitor"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_ahistogram"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aiir"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_aintegral"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_ainterleave"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_alatency"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_alimiter"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_allpass"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aloop"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_amerge"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_ametadata"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_amix"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_amultiply"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_anequalizer"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_anlmdn"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_anlmf"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_anlms"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_anull"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_apad"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_aperms"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_aphasemeter"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aphaser"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aphaseshift"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_apsnr"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_apsyclip"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_apulsator"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_arealtime"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aresample"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_areverse"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_arls"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_arnndn"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_asdr"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_asegment"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aselect"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asendcmd"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asetnsamples"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asetpts"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asetrate"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asettb"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_ashowinfo"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asidedata"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asisdr"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_asoftclip"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_aspectralstats"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asplit"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_astats"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_asubboost"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asubcut"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asupercut"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asuperpass"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_asuperstop"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_atempo"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_atilt"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_atrim"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_avectorscope"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_axcorrelate"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_bandpass"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_bandreject"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_bass"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_biquad"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_channelmap"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_channelsplit"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_chorus"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_compand"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_compensationdelay"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_crossfeed"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_crystalizer"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_dcshift"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_deesser"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_dialoguenhance"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_drmeter"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_dynaudnorm"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_earwax"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_ebur128"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_equalizer"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_extrastereo"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_firequalizer"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_flanger"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_haas"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_hdcd"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_headphone"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_highpass"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_highshelf"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_join"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_loudnorm"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_lowpass"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_lowshelf"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_mcompand"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_pan"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_replaygain"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_showcqt"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_showcwt"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_showfreqs"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_showspatial"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_showspectrum"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_showspectrumpic"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_showvolume"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_showwaves"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_showwavespic"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_sidechaincompress"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_sidechaingate"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_silencedetect"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_silenceremove"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_spectrumsynth"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_speechnorm"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_stereotools"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_stereowiden"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_superequalizer"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_surround"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_tiltshelf"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_treble"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_tremolo"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_vibrato"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_virtualbass"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "ffmpeg_volume"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter", {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "ffmpeg_volumedetect"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/ffmpeg_filters.py:507#execute_ffmpeg_filter",
            {"input_audio_file": "$audio_file", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}", "output_audio_format": "wav"},
        ),
        ("audio_processing_plugin", "find_spectral_peaks"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:1813#AudioProcessingPlugin.find_spectral_peaks", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "generate_binaural"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/synthesis.py:269#generate_binaural", {"beat_hz": 4.0, "carrier_hz": 440.0, "duration_s": 1.0, "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "generate_drone"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/synthesis.py:441#generate_drone", {"base_frequency": 110.0, "duration_s": 0.1, "amplitude": 0.1, "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "generate_heartbeat"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/synthesis.py:368#generate_heartbeat", {"bpm": 60, "duration_s": 0.1, "amplitude": 0.1, "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "generate_isochronic"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/synthesis.py:307#generate_isochronic", {"carrier_hz": 440.0, "duration_s": 1.0, "output_audio_file": "census_{verb}", "output_audio_format": "wav", "pulse_hz": 4.0}),
        ("audio_processing_plugin", "generate_monaural"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/synthesis.py:417#generate_monaural", {"duration_s": 1.0, "frequency_1": 440.0, "frequency_2": 444.0, "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "generate_noise"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/synthesis.py:333#generate_noise", {"noise_type": "white", "duration_s": 0.1, "seed": 1, "amplitude": 0.1, "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "generate_silence"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/synthesis.py:191#generate_silence", {"duration_s": 0.1, "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "generate_sweep"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/synthesis.py:219#generate_sweep", {"start_hz": 220.0, "end_hz": 440.0, "duration_s": 0.1, "amplitude": 0.1, "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "generate_tone"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/synthesis.py:156#generate_tone", {"frequency": 440.0, "duration_s": 0.1, "amplitude": 0.1, "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "import_tagged_file_to_blob"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/swatch_io.py:309#import_tagged_file_to_blob", {"file_path": "$local_audio", "prefer_sidecar": True}),
        ("audio_processing_plugin", "loop_crossfade"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/sample_authoring.py:387#loop_crossfade", {"output_audio_format": "wav", "target_loop_s": 1.0, "input_audio_file": "$audio_file", "output_audio_file": "census_{verb}"}),
        ("audio_processing_plugin", "normalize_audio"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:253#normalize_audio", {"input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "parameter_sweep"): (
            "plugins/audio_processing_plugin/src/audio_processing_plugin/sweep.py:209#parameter_sweep",
            {"base_recipe": {"process_key": "plugin::audio_processing_plugin::generate_silence", "arguments": {"duration_s": 0.1, "output_audio_file": "census_sweep"}}, "axes": [{"name": "duration_s", "values": [0.1]}]},
        ),
        ("audio_processing_plugin", "pitch_shift"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/sample_authoring.py:196#pitch_shift", {"output_audio_format": "wav", "input_audio_file": "$audio_file", "output_audio_file": "census_{verb}"}),
        ("audio_processing_plugin", "pitch_to_midi"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2073#AudioProcessingPlugin.pitch_to_midi", {"pitch_blob_id": "$pitch", "onsets_blob_id": "$onsets"}),
        ("audio_processing_plugin", "pitch_to_pitch_bend_midi"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2115#AudioProcessingPlugin.pitch_to_pitch_bend_midi", {"pitch_blob_id": "$pitch", "instrument_register_range": "A2-A5"}),
        ("audio_processing_plugin", "safe_pad_to"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:495#safe_pad_to", {"input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "output_audio_format": "wav", "target_duration_s": 1.2}),
        ("audio_processing_plugin", "safe_splice"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:450#safe_splice", {"output_audio_format": "wav", "input_audio_files": ["$audio_file", "$audio_file"], "output_audio_file": "census_{verb}"}),
        ("audio_processing_plugin", "search_samples"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/sample_authoring.py:745#search_samples", {"tag_filters": {"mime_type": "audio/wav"}}),
        ("audio_processing_plugin", "segment_phrases"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:3380#AudioProcessingPlugin.segment_phrases", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "slice_audio"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/effect_orchestration.py:549#slice_audio", {"duration_s": 1.0, "input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "output_audio_format": "wav"}),
        ("audio_processing_plugin", "tag_sample"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/sample_authoring.py:644#tag_sample", {"input_audio_file": "$audio_file", "tags": {"description": "Census fixture"}}),
        ("audio_processing_plugin", "time_stretch"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/sample_authoring.py:265#time_stretch", {"output_audio_format": "wav", "input_audio_file": "$audio_file", "output_audio_file": "census_{verb}"}),
        ("audio_processing_plugin", "track_beats"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2699#AudioProcessingPlugin.track_beats", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "transcribe_polyphonic_pitch"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:2896#AudioProcessingPlugin.transcribe_polyphonic_pitch", {"blob_id": "$audio"}),
        ("audio_processing_plugin", "trim_to_beats"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/sample_authoring.py:559#trim_to_beats", {"beat_count": 2, "bpm": 120.0, "output_audio_format": "wav", "input_audio_file": "$audio_file", "output_audio_file": "census_{verb}"}),
        ("audio_processing_plugin", "welch_psd"): ("plugins/audio_processing_plugin/src/audio_processing_plugin/plugin.py:1772#AudioProcessingPlugin.welch_psd", {"blob_id": "$audio"}),
        ("chatgpt_export_session_source_plugin", "ingest_export"): ("plugins/chatgpt_export_session_source_plugin/src/chatgpt_export_session_source_plugin/plugin.py:232#ChatGPTExportSessionSourcePlugin.ingest_export", {"file_path": "$chatgpt_path"}),
        ("claude_ai_export_session_source_plugin", "ingest_export"): ("plugins/claude_ai_export_session_source_plugin/src/claude_ai_export_session_source_plugin/plugin.py:189#ClaudeAiExportSessionSourcePlugin.ingest_export", {"file_path": "$claude_path"}),
        ("generative_composition_plugin", "compose_baroque_phrase"): ("plugins/generative_composition_plugin/src/generative_composition_plugin/plugin.py:2499#GenerativeCompositionPlugin.compose_baroque_phrase", {"output_phrase_id": "census_phrase", "wbs_id": "census_wbs"}),
        ("generative_composition_plugin", "generate_bass_pattern"): ("plugins/generative_composition_plugin/src/generative_composition_plugin/generators.py:509#generate_bass_pattern", {"pattern_type": "pedal", "output_midi_file": "census_{verb}"}),
        ("generative_composition_plugin", "generate_cadence_phrases"): (
            "plugins/generative_composition_plugin/src/generative_composition_plugin/generators.py:843#generate_cadence_phrases",
            {"scale_root": "C", "scale_type": "major", "pitch_range": [48, 72], "total_duration_s": 1.0, "phrase_duration_range_s": [0.5, 1.0], "inter_phrase_rest_s": [0.1, 0.2], "motif_cell": [0, 2, 4], "velocity_contour": [80, 80], "output_midi_file": "census_{verb}"},
        ),
        ("generative_composition_plugin", "generate_melodic_pattern"): ("plugins/generative_composition_plugin/src/generative_composition_plugin/generators.py:251#generate_melodic_pattern", {"pattern_type": "walk", "output_midi_file": "census_{verb}"}),
        ("generative_composition_plugin", "generate_musical_phrase"): ("plugins/generative_composition_plugin/src/generative_composition_plugin/plugin.py:1756#GenerativeCompositionPlugin.generate_musical_phrase", {"output_phrase_id": "census_phrase", "style": "baroque"}),
        ("generative_composition_plugin", "generate_rhythmic_pattern"): ("plugins/generative_composition_plugin/src/generative_composition_plugin/generators.py:661#generate_rhythmic_pattern", {"output_midi_file": "census_{verb}"}),
        ("generative_composition_plugin", "prepare_baroque_phrase"): (
            "plugins/generative_composition_plugin/src/generative_composition_plugin/plugin.py:1917#GenerativeCompositionPlugin.prepare_baroque_phrase",
            {"lh_abc_notation": "X:1\nM:4/4\nL:1/4\nK:C\nC,DEF|", "output_phrase_id": "census_phrase", "rh_abc_notation": "X:1\nM:4/4\nL:1/4\nK:C\nCDEF|"},
        ),
        ("generative_composition_plugin", "transform_pattern"): ("plugins/generative_composition_plugin/src/generative_composition_plugin/plugin.py:1667#GenerativeCompositionPlugin.transform_pattern", {"amount": 1, "input_midi_file": "$midi_file", "output_midi_file": "census_{verb}", "transform": "transpose"}),
        ("musical_synthesis_plugin", "describe_instrument"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:1104#MusicalSynthesisPlugin.describe_instrument", {"instrument_id": "cello_bowed"}),
        ("musical_synthesis_plugin", "generate_chord_pad"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/synthesizers.py:666#generate_chord_pad", {"program": 0}),
        ("musical_synthesis_plugin", "list_instruments"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:1054#MusicalSynthesisPlugin.list_instruments", {}),
        ("musical_synthesis_plugin", "render_arpeggio_pattern"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/synthesizers.py:844#render_arpeggio_pattern", {"program": 0}),
        ("musical_synthesis_plugin", "render_midi_file"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:911#MusicalSynthesisPlugin.render_midi_file", {"input_midi_file": "$midi_file", "output_audio_file": "census_{verb}"}),
        ("musical_synthesis_plugin", "render_note_sequence"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/synthesizers.py:732#render_note_sequence", {"program": 0}),
        ("musical_synthesis_plugin", "render_score"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:740#MusicalSynthesisPlugin.render_score", {"output_audio_file": "census_{verb}", "program": 0, "abc_notation": "X:1\nM:4/4\nL:1/4\nK:C\nCDEF|"}),
        ("musical_synthesis_plugin", "render_sfz_midi"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:1002#MusicalSynthesisPlugin.render_sfz_midi", {"input_midi_file": "$midi_file", "output_audio_file": "census_{verb}", "sfz_path": "$sfz_path"}),
        ("musical_synthesis_plugin", "sound_preset_create"): (
            "plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:1213#MusicalSynthesisPlugin.sound_preset_create",
            {"name": "census_fixture", "audio_blob_id": "$audio", "character_description": "Census", "recipe": {"target_process": "render_note_sequence", "arguments": {"notes": ["A4"], "program": 0, "output_audio_file": "census_preset"}}},
        ),
        ("musical_synthesis_plugin", "sound_preset_describe"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:1331#MusicalSynthesisPlugin.sound_preset_describe", {"name": "census_fixture"}),
        ("musical_synthesis_plugin", "sound_preset_list"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:1292#MusicalSynthesisPlugin.sound_preset_list", {}),
        ("musical_synthesis_plugin", "sound_preset_render"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:1256#MusicalSynthesisPlugin.sound_preset_render", {"name": "census_fixture"}),
        ("musical_synthesis_plugin", "transform_midi_monophonic"): ("plugins/musical_synthesis_plugin/src/musical_synthesis_plugin/plugin.py:1157#MusicalSynthesisPlugin.transform_midi_monophonic", {"input_midi_file": "$midi_file", "output_midi_file": "census_{verb}"}),
        ("pedalboard_effects_plugin", "apply_envelope_curve"): (
            "plugins/pedalboard_effects_plugin/src/pedalboard_effects_plugin/plugin.py:692#PedalboardEffectsPlugin._unpack_envelope_curve_params",
            {"input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "breakpoints": [{"time_s": 0.0, "amplitude": 0.5}, {"time_s": 1.0, "amplitude": 0.5}]},
        ),
        ("pedalboard_effects_plugin", "apply_filter_automation"): (
            "plugins/pedalboard_effects_plugin/src/pedalboard_effects_plugin/plugin.py:473#PedalboardEffectsPlugin._unpack_filter_automation_params",
            {"input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "filter_type": "lowpass", "breakpoints": [{"time_s": 0.0, "cutoff_hz": 1000.0}, {"time_s": 1.0, "cutoff_hz": 2000.0}]},
        ),
        ("pedalboard_effects_plugin", "apply_tempo_synced_delay"): ("plugins/pedalboard_effects_plugin/src/pedalboard_effects_plugin/plugin.py:878#PedalboardEffectsPlugin.apply_tempo_synced_delay", {"input_audio_file": "$audio_file", "output_audio_file": "census_{verb}", "tempo_bpm": 120.0, "subdivision": "quarter"}),
        ("ssml_plugin", "create_ssml_file"): ("plugins/ssml_plugin/src/ssml_plugin/plugin.py:328#SSMLPlugin.create_ssml_file", {"input_plain_text": "Census fixture.", "output_ssml_file": "census_output"}),
        ("ssml_plugin", "extract_metadata"): ("plugins/ssml_plugin/src/ssml_plugin/plugin.py:435#SSMLPlugin.extract_metadata", {"input_ssml_string": '<?xml version="1.0"?><speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0" xml:lang="en-US">Census fixture.</speak>'}),
        ("ssml_plugin", "parse_ssml"): ("plugins/ssml_plugin/src/ssml_plugin/plugin.py:219#SSMLPlugin.parse_ssml", {"input_ssml_file": None, "input_ssml_string": ssml_bytes().decode()}),
        ("voice_embeddings_plugin", "create_voice_profile"): ("plugins/voice_embeddings_plugin/src/voice_embeddings_plugin/plugin.py:250#VoiceEmbeddingsPlugin.create_voice_profile", {"audio_file": "$audio_file", "name": "census_fixture"}),
        ("voice_embeddings_plugin", "get_voice_profile_by_name"): ("plugins/voice_embeddings_plugin/src/voice_embeddings_plugin/plugin.py:729#VoiceEmbeddingsPlugin.get_voice_profile_by_name", {"name": "$voice_read_name"}),
        ("voice_embeddings_plugin", "get_voice_profile_by_id"): ("plugins/voice_embeddings_plugin/src/voice_embeddings_plugin/plugin.py:801#VoiceEmbeddingsPlugin.get_voice_profile_by_id", {"profile_id": "$voice_read_id"}),
        ("voice_embeddings_plugin", "delete_voice_profile_by_name"): ("plugins/voice_embeddings_plugin/src/voice_embeddings_plugin/plugin.py:987#VoiceEmbeddingsPlugin.delete_voice_profile_by_name", {"name": "$voice_delete_name_name"}),
        ("voice_embeddings_plugin", "delete_voice_profile_by_id"): ("plugins/voice_embeddings_plugin/src/voice_embeddings_plugin/plugin.py:1042#VoiceEmbeddingsPlugin.delete_voice_profile_by_id", {"profile_id": "$voice_delete_id_id"}),
        ("voice_embeddings_plugin", "list_voice_profiles"): ("plugins/voice_embeddings_plugin/src/voice_embeddings_plugin/plugin.py:887#VoiceEmbeddingsPlugin.list_voice_profiles", {}),
        ("voice_embeddings_plugin", "update_voice_profile"): ("plugins/voice_embeddings_plugin/src/voice_embeddings_plugin/plugin.py:601#VoiceEmbeddingsPlugin.update_voice_profile", {"name": "$voice_read_name", "tags": ["census"]}),
    }
)
