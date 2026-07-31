"""Ad-hoc verification harness for pipeline_resolver.

Runs through the spec's verification checklist (steps 1-8 from the
"Verification" section of 2026-04-27_claude_generalized_daw.md) against
both the neuro-ambient and early-baroque schemas.

A7.3: Also verifies the motif chain uses MIDI I/O:
  generate_melodic_pattern  →  output_midi_file
  render_midi_file           →  input_midi_file → output_audio_file
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "plugins/default_thinking_plugin/src"))

from default_thinking_plugin.pipeline_resolver import (  # noqa: E402
    ProcessIOMap,
    generate_wbs,
    render_segment_table,
    resolve_pipeline,
)
from default_thinking_plugin.pipeline_spec import (  # noqa: E402
    LayerConfig,
    ModulationAssignment,
    ParameterArc,
    ParameterGroup,
    PipelineSpec,
    Segment,
    SegmentLayerOverride,
)

# Known MIDI-I/O processes — matches invocation schema argument names.
# In production these come from _build_process_io_map via the discovery
# service; here we hard-code the same contract so the verify script can
# run without a live platform.
KNOWN_PROCESS_IO_MAP: ProcessIOMap = {
    # input_key=None because this is a generative step (no audio input)
    "plugin::generative_composition_plugin::generate_melodic_pattern": (
        None, "output_midi_file", False,
    ),
    # MIDI render: consumes a MIDI file, produces an audio file
    "plugin::musical_synthesis_plugin::render_midi_file": (
        "input_midi_file", "output_audio_file", True,
    ),
}


_GENERATE_MELODIC_KEY = (
    "(plugin::generative_composition_plugin::generate_melodic_pattern)"
)
_RENDER_MIDI_KEY = "(plugin::musical_synthesis_plugin::render_midi_file)"


def _check_motif_chain(wbs: str, label: str) -> list[str]:
    """Return a list of error strings if the motif I/O chain is wrong.

    Only matches lines that contain the full process key in parentheses,
    so filenames that happen to include a process short-name are not
    mistakenly flagged.
    """
    errors: list[str] = []
    lines = wbs.splitlines()
    for i, line in enumerate(lines):
        if _GENERATE_MELODIC_KEY in line:
            args_block = _next_args_block(lines, i)
            if '"output_audio_file"' in args_block:
                errors.append(
                    f"[{label}] generate_melodic_pattern emits"
                    f" output_audio_file — expected output_midi_file"
                )
            if '"output_midi_file"' not in args_block:
                errors.append(
                    f"[{label}] generate_melodic_pattern missing"
                    f" output_midi_file in Arguments"
                )
        elif _RENDER_MIDI_KEY in line:
            args_block = _next_args_block(lines, i)
            if '"input_audio_file"' in args_block:
                errors.append(
                    f"[{label}] render_midi_file uses input_audio_file"
                    f" — expected input_midi_file"
                )
            if '"input_midi_file"' not in args_block:
                errors.append(
                    f"[{label}] render_midi_file missing"
                    f" input_midi_file in Arguments"
                )
    return errors


def _next_args_block(lines: list[str], start: int) -> str:
    """Return the JSON string from the first Arguments: block after start."""
    for j in range(start, min(start + 6, len(lines))):
        if lines[j].strip().startswith("{"):
            return lines[j]
    return ""


def neuro_spec() -> PipelineSpec:
    sections = ["orientation", "induction", "deepening",
                "core_absorptive_work", "fractionation",
                "integration", "return"]
    parameter_groups = [
        ParameterGroup(label="region_a", root_hz=87.31,
                       intervals_semitones=[0, 3, 7],
                       properties={"register": "low"}),
        ParameterGroup(label="region_b", root_hz=98.00,
                       intervals_semitones=[0, 5, 7, 10],
                       properties={"register": "low_mid"}),
    ]
    region_for = {
        "orientation": "region_a", "induction": "region_a",
        "deepening": "region_a", "core_absorptive_work": "region_b",
        "fractionation": "region_b", "integration": "region_a",
        "return": "region_a",
    }
    energy_for = {
        "orientation": "low", "induction": "low",
        "deepening": "very_low", "core_absorptive_work": "very_low",
        "fractionation": "medium_low", "integration": "low",
        "return": "very_low",
    }
    density_for = {
        "orientation": "sparse_moderate", "induction": "sparse",
        "deepening": "very_sparse", "core_absorptive_work": "moderate",
        "fractionation": "moderate", "integration": "sparse_moderate",
        "return": "very_sparse",
    }
    width_for = {
        "orientation": 0.4, "induction": 0.5, "deepening": 0.6,
        "core_absorptive_work": 0.8, "fractionation": 0.7,
        "integration": 0.5, "return": 0.4,
    }
    brightness_for = {
        "orientation": 0.6, "induction": 0.5, "deepening": 0.4,
        "core_absorptive_work": 0.55, "fractionation": 0.65,
        "integration": 0.45, "return": 0.35,
    }
    ir_for = {
        "orientation": "lady_chapel_st_albans",
        "induction": "lady_chapel_st_albans",
        "deepening": "york_minster",
        "core_absorptive_work": "voxengo_deep_space",
        "fractionation": "voxengo_large_long_echo_hall",
        "integration": "lady_chapel_st_albans",
        "return": "lady_chapel_st_albans",
    }
    ir_wet_for = {
        "orientation": 0.30, "induction": 0.35, "deepening": 0.45,
        "core_absorptive_work": 0.55, "fractionation": 0.50,
        "integration": 0.35, "return": 0.30,
    }
    duration_for = {
        "orientation": 180, "induction": 240, "deepening": 360,
        "core_absorptive_work": 300, "fractionation": 240,
        "integration": 180, "return": 180,
    }
    foreground_for = {
        "core_absorptive_work": "motif", "fractionation": "motif",
    }
    arcs = [
        ParameterArc(name="energy", values=energy_for),
        ParameterArc(name="density", values=density_for),
        ParameterArc(name="width", values=width_for),
        ParameterArc(name="brightness", values=brightness_for),
        ParameterArc(name="ir_name", values=ir_for),
        ParameterArc(name="ir_wet", values=ir_wet_for),
    ]
    layer_configs = [
        LayerConfig(layer_type="substrate", properties={
            "harmonics": [
                {"ratio": 1.0, "gain_db": 0.0},
                {"ratio": 2.0, "gain_db": -6.0},
                {"ratio": 3.0, "gain_db": -12.0},
                {"ratio": 4.0, "gain_db": -18.0},
            ],
        }),
        LayerConfig(layer_type="harmonic_bed", properties={
            "velocity": 78,
            "program": 89,
            "filter_breakpoints": [
                {"time_s": 0.0, "cutoff_hz": 800.0},
                {"time_s": 90.0, "cutoff_hz": 1400.0},
                {"time_s": 180.0, "cutoff_hz": 1000.0},
            ],
        }),
        LayerConfig(layer_type="entrainment", properties={
            "carrier_hz": 174.61,
            "beat_hz": 6.0,
            "level_db": "-20dB",
        }),
        LayerConfig(layer_type="air", properties={
            "amplitude": 0.18,
            "highpass_hz": 3000,
        }),
        LayerConfig(layer_type="motif", properties={
            "scale_root": "F3",
            "scale_type": "minor",
            "note_count": 32,
            "duration_per_note_s": 0.6,
            "pitch_range": [48, 72],
            "program_overrides": {"0": 11},
            "delay_tempo_bpm": 60,
            "delay_feedback": 0.35,
            "delay_cutoff_hz": 2400,
            "delay_mix": 0.3,
        }),
    ]
    section_objs = []
    per_section_beat = {
        "orientation": 9.0, "induction": 8.0, "deepening": 5.0,
        "core_absorptive_work": 5.5, "fractionation": 8.5,
        "integration": 7.0, "return": 9.0,
    }
    stagger_for = {
        "orientation": [
            ("substrate", 0, 25),
            ("harmonic_bed", 8, 22),
            ("entrainment", 12, 20),
        ],
        "core_absorptive_work": [
            ("substrate", 0, 30),
            ("harmonic_bed", 6, 25),
            ("air", 10, 25),
            ("entrainment", 14, 22),
            ("motif", 25, 20),
        ],
        "fractionation": [
            ("substrate", 0, 20),
            ("harmonic_bed", 5, 20),
            ("air", 8, 20),
            ("entrainment", 10, 18),
            ("motif", 18, 15),
        ],
    }
    filter_breakpoints_for = {
        "orientation": [
            {"time_s": 0, "cutoff_hz": 800},
            {"time_s": 90, "cutoff_hz": 1400},
            {"time_s": 180, "cutoff_hz": 1000},
        ],
    }
    for sec in sections:
        properties = {
            "duration_s": duration_for[sec],
            "parameter_group": region_for[sec],
        }
        if sec in foreground_for:
            properties["foreground"] = foreground_for[sec]
        properties["movement_name"] = sec
        overrides = []
        if sec in per_section_beat:
            overrides.append(SegmentLayerOverride(
                layer_type="entrainment",
                properties={"beat_hz": per_section_beat[sec]},
            ))
        if sec in filter_breakpoints_for:
            overrides.append(SegmentLayerOverride(
                layer_type="harmonic_bed",
                properties={"filter_breakpoints":
                            filter_breakpoints_for[sec]},
            ))
        for layer_type, delay, fade in stagger_for.get(sec, []):
            overrides.append(SegmentLayerOverride(
                layer_type=layer_type,
                properties={
                    "entrance_delay_s": delay,
                    "fade_in_duration_s": fade,
                },
            ))
        section_objs.append(Segment(name=sec, properties=properties,
                                    layer_overrides=overrides))
    modulation_assignments = [
        ModulationAssignment(section_name="orientation",
                             layer_type="harmonic_bed",
                             process="plugin::audio_processing_plugin::"
                                     "ffmpeg_aphaser",
                             params={"speed": 0.18, "decay": 0.6,
                                     "delay": 1.5}),
        ModulationAssignment(section_name="orientation",
                             layer_type="substrate",
                             process="plugin::audio_processing_plugin::"
                                     "apply_tremolo",
                             params={"rate_hz": 0.3, "depth": 0.05}),
        ModulationAssignment(section_name="core_absorptive_work",
                             layer_type="substrate",
                             process="plugin::audio_processing_plugin::"
                                     "ffmpeg_vibrato",
                             params={"f": 0.4, "d": 0.25}),
        ModulationAssignment(section_name="core_absorptive_work",
                             layer_type="harmonic_bed",
                             process="plugin::audio_processing_plugin::"
                                     "ffmpeg_chorus",
                             params={"in_gain": 0.7, "out_gain": 0.7,
                                     "delays": "55", "decays": "0.5",
                                     "speeds": "0.4", "depths": "2"}),
    ]
    return PipelineSpec(
        schema_id="neuro_ambient_v1",
        piece={"tonal_center_hz": 87.31, "style_family": "neuro_ambient",
               "duration_s": 1680, "stage_model": "ericksonian"},
        parameter_groups=parameter_groups,
        arcs=arcs,
        layer_configs=layer_configs,
        sections=section_objs,
        modulation_assignments=modulation_assignments,
    )


def baroque_spec() -> PipelineSpec:
    movements = [
        ("toccata", "through_composed", 240, "lady_chapel_st_albans"),
        ("allemande", "binary", 300, "lady_chapel_st_albans"),
        ("sarabande", "binary", 240, "lady_chapel_st_albans"),
        ("gigue", "binary", 180, "lady_chapel_st_albans"),
    ]
    section_objs = []
    for name, form, dur, ir in movements:
        properties = {
            "duration_s": dur,
            "form": form,
            "movement_name": name,
            "ir_name": ir,
            "ir_wet": 0.20,
        }
        overrides = []
        if form == "binary":
            overrides.append(SegmentLayerOverride(
                layer_type="binary_form_assembly",
                properties={"input_files": [
                    f"<artifact_prefix>_{name}_a_section_wav",
                    f"<artifact_prefix>_{name}_a_section_wav",
                    f"<artifact_prefix>_{name}_b_section_wav",
                    f"<artifact_prefix>_{name}_b_section_wav",
                ]},
            ))
        else:
            overrides.append(SegmentLayerOverride(
                layer_type="through_composed_reference",
                properties={"input_file":
                            f"<artifact_prefix>_{name}_section_wav"},
            ))
        section_objs.append(Segment(
            name=name, properties=properties,
            layer_overrides=overrides,
        ))
    return PipelineSpec(
        schema_id="early_baroque_keyboard_v1",
        piece={"home_key": "C minor", "duration_s": 960},
        parameter_groups=[ParameterGroup(label="default", root_hz=261.63,
                                         intervals_semitones=[0])],
        arcs=[],
        layer_configs=[],
        sections=section_objs,
        modulation_assignments=[],
    )


def main() -> int:
    rc = 0
    for label, spec_fn, schema_path, prefix, phase, name in [
        ("neuro-ambient", neuro_spec,
         REPO / "knowledge_bases/neuro_ambient/03_templates/"
                "pipeline_schema.json",
         "example_run", 2,"Section Stem Construction"),
        ("baroque", baroque_spec,
         REPO / "knowledge_bases/early_baroque/03_templates/"
                "pipeline_schema.json",
         "example_run", 2,"Movement Assembly"),
    ]:
        print(f"=== {label} ===")
        schema = json.loads(schema_path.read_text())
        spec = spec_fn()
        try:
            resolved = resolve_pipeline(spec, schema)
        except ValueError as exc:
            print(f"  RESOLVE FAILED: {exc}")
            rc = 1
            continue
        for section in resolved.sections:
            n_layers = len(section.layers)
            n_pp = sum(len(layer.post_processing) for layer in section.layers)
            print(f"  section {section.name}: {n_layers} layers,"
                  f" {n_pp} total post-processing entries")
        table = render_segment_table(resolved, schema)
        print(f"  segment table rows: {len(table.splitlines()) - 2}")
        wbs = generate_wbs(
            resolved, schema, wbs_id=f"wbs-{label}",
            manifest_id="mfst-test", phase_number=phase,
            phase_name=name, artifact_prefix=prefix,
            process_io_map=KNOWN_PROCESS_IO_MAP,
        )
        out_path = Path(f"/tmp/{label.replace('-', '_')}_phase2_wbs.md")
        out_path.write_text(wbs)
        n_steps = sum(1 for line in wbs.splitlines()
                      if line.startswith("[ ] "))
        print(f"  WBS lines: {len(wbs.splitlines())},"
              f" steps: {n_steps}, file: {out_path}")
        # A7.3: verify motif chain uses MIDI I/O
        chain_errors = _check_motif_chain(wbs, label)
        if chain_errors:
            for err in chain_errors:
                print(f"  FAIL {err}")
            rc = 1
        else:
            print("  PASS motif chain I/O")
    return rc


if __name__ == "__main__":
    sys.exit(main())
