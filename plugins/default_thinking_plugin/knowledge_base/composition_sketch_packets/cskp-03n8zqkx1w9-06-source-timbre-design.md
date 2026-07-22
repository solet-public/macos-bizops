# Composition Sketch Packet 06: Source Timbre Design
PACKET ID: cskp-03n8zqkx1w9-06-source-timbre-design
SKETCH ID: 03n8zqkx1w9
MANIFEST ID: wmf-neuro-ambient-composition-03-001
PASS NUMBER: 06

## Source Selection Per Layer
substrate:
  source_family: drone_synthesis
  source_subtype: "additive partials over harmonic series with fundamental at F2 (87.307 Hz)"
  generator_process_hint: "generate_drone"
  spectral_shape: even_harmonics
  brightness: dark
  attack_profile: breath
  sustain_profile: held

harmonic_bed:
  source_family: soundfont
  source_subtype: "generaluser_gs program 89 (warm pad with natural harmonic body)"
  generator_process_hint: "render_score with soundfont"
  spectral_shape: even_harmonics
  brightness: low-mid
  attack_profile: breath
  sustain_profile: held

air:
  source_family: noise_shape
  source_subtype: "pink noise filtered to upper-mid band (2000-8000 Hz)"
  generator_process_hint: "generate_noise"
  spectral_shape: broadband_filtered
  brightness: air-only
  attack_profile: breath
  sustain_profile: evolving

motif:
  source_family: soundfont
  source_subtype: "generaluser_gs program 89 (warm reed-like MIDI pad with soft attack)"
  generator_process_hint: "render_score with soundfont"
  spectral_shape: even_harmonics
  brightness: low-mid
  attack_profile: breath
  sustain_profile: held

All sources avoid hard exclusions (no bright_bells, no percussion_groove, no sparkling textures).

## Spectral Allocation
spectral_lanes:
  sub: []
  low: [substrate]
  low-mid: [harmonic_bed, motif]
  mid: []
  upper-mid: []
  high: [air]
  air: []

collision_audit: clean
collision_rationale: "All primary spectral lanes occupied by single layers; motif filtered to 200-800 Hz (low-mid) avoiding harmonic_bed overlap at 150-2000 Hz."

## Static Processing Chain Per Layer
substrate:
  highpass_hz: 30
  lowpass_hz: 1200
  static_gain_db: 0.0
  static_pan: 0.0
  pre_modulation_chain: [lowpass, highpass]

harmonic_bed:
  highpass_hz: 150
  lowpass_hz: 2500
  static_gain_db: -3.0
  static_pan: 0.2
  pre_modulation_chain: [lowpass, highpass]

air:
  highpass_hz: 2000
  lowpass_hz: 10000
  static_gain_db: -12.0
  static_pan: 0.5
  pre_modulation_chain: [highpass, lowpass]

motif:
  highpass_hz: 200
  lowpass_hz: 800
  static_gain_db: -6.0
  static_pan: 0.0
  pre_modulation_chain: [highpass, lowpass]

Static processing is invariant across stages; stage-varying modulation belongs to Pass 07.

## Anchor Timbre Signature
anchor_layer: motif
anchor_timbre_phrase: "warm reed-like MIDI pad (generaluser_gs program 89) with soft attack and long release as committed in Packet 03"
anchor_register_match: verified
anchor_brightness_match: verified