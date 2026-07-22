# Composition Sketch Packet 06: Source Timbre Design
PACKET ID: cskp-003-06-source-timbre-design
SKETCH ID: 003
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 06

## Source Selection Per Layer
substrate:
  source_family: drone_synthesis
  source_subtype: "warm additive drone with fundamental at F2 (87.31 Hz) and 4 harmonics, steep rolloff (-10 dB per harmonic)"
  generator_process_hint: "generate_drone"
  spectral_shape: even_harmonics
  brightness: dark
  attack_profile: breath
  sustain_profile: held
harmonic_bed:
  source_family: soundfont
  source_subtype: "warm string pad (generaluser_gs program 89) with natural breath attack"
  generator_process_hint: "generate_chord_pad"
  spectral_shape: even_harmonics
  brightness: low-mid
  attack_profile: breath
  sustain_profile: held
entrainment:
  source_family: soundfont
  source_subtype: "reed-like MIDI layer (musescore_general program 89) with breath attack and long release"
  generator_process_hint: "render_score"
  spectral_shape: full_harmonic_series
  brightness: low-mid
  attack_profile: breath
  sustain_profile: decaying
air:
  source_family: noise_shape
  source_subtype: "pink noise filtered to 2000-8000 Hz with slow attack and release"
  generator_process_hint: "generate_noise"
  spectral_shape: broadband_filtered
  brightness: air-only
  attack_profile: breath
  sustain_profile: decaying
motif:
  source_family: soundfont
  source_subtype: "sparse melodic fragment (musescore_general program 89) with highpass filtering"
  generator_process_hint: "render_note_sequence"
  spectral_shape: harmonic_series_subset
  brightness: mid
  attack_profile: breath
  sustain_profile: decaying

## Spectral Allocation
spectral_lanes:
  sub: [substrate]
  low-mid: [substrate, harmonic_bed, entrainment]
  mid: [harmonic_bed, motif]
  upper-mid: [entrainment, motif]
  high: [air]
collision_audit: contained
collision_rationale: "Low-mid lane has three layers; harmonic_bed and entrainment filtered to avoid overlap in primary energy bands (harmonic_bed: 150-2000 Hz, entrainment: 100-2500 Hz)"

## Static Processing Chain Per Layer
substrate:
  highpass_hz: 40
  lowpass_hz: 1200
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: -3.0
  static_pan: 0.0
  pre_modulation_chain: [highpass, lowpass]
harmonic_bed:
  highpass_hz: 150
  lowpass_hz: 2000
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: -4.5
  static_pan: 0.0
  pre_modulation_chain: [highpass, lowpass]
entrainment:
  highpass_hz: 100
  lowpass_hz: 2500
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: -3.5
  static_pan: 0.1
  pre_modulation_chain: [highpass, lowpass]
air:
  highpass_hz: 2000
  lowpass_hz: 8000
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: -12.0
  static_pan: 0.5
  pre_modulation_chain: [highpass, lowpass]
motif:
  highpass_hz: 300
  lowpass_hz: 4500
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: -10.0
  static_pan: 0.3
  pre_modulation_chain: [highpass, lowpass]

## Anchor Timbre Signature
anchor_layer: entrainment
anchor_timbre_phrase: "reed-like MIDI layer (musescore_general program 89) with breath attack and long release, filtered to low-mid register"
anchor_register_match: verified
anchor_brightness_match: verified