# Composition Sketch Packet 06: Source Timbre Design

PACKET ID: cskp-004-06-source-timbre-design
SKETCH ID: 004
MANIFEST ID: wmf-neuro-ambient-composition-004
PASS NUMBER: 06

## Source Selection Per Layer
substrate:
  source_family: drone_synthesis
  source_subtype: "additive partials over harmonic series with fundamental at 87.307 Hz, 4 harmonics (1st, 3rd, 5th, 7th) with -6 dB rolloff"
  generator_process_hint: "generate_drone"
  spectral_shape: harmonic_series_subset
  brightness: low-mid
  attack_profile: breath
  sustain_profile: held

harmonic_bed:
  source_family: soundfont
  source_subtype: "Warm Pad (GM Program 89) from musescore_general"
  generator_process_hint: "generate_chord_pad"
  spectral_shape: even_harmonics
  brightness: low-mid
  attack_profile: soft
  sustain_profile: held

motif:
  source_family: soundfont
  source_subtype: "Warm Pad (GM Program 89) from musescore_general"
  generator_process_hint: "generate_chord_pad"
  spectral_shape: even_harmonics
  brightness: low-mid
  attack_profile: soft
  sustain_profile: held

air:
  source_family: noise
  source_subtype: "pink noise filtered to 2000-8000 Hz band"
  generator_process_hint: "generate_noise"
  spectral_shape: broadband_filtered
  brightness: upper-mid
  attack_profile: breath
  sustain_profile: evolving

Hard exclusions: all sources are non-percussive with soft attack profiles (no transients), satisfying Pass 01 exclusions.

## Spectral Allocation
spectral_lanes:
  sub: [substrate]
  low-mid: [harmonic_bed]
  mid: [motif]
  high: [air]
collision_audit: clean
collision_rationale: 

## Static Processing Chain Per Layer
substrate:
  highpass_hz: 40
  lowpass_hz: 1200
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: 0.0
  static_pan: 0.0
  pre_modulation_chain: [ffmpeg_highpass, ffmpeg_lowpass]

harmonic_bed:
  highpass_hz: 150
  lowpass_hz: 2500
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: -1.5
  static_pan: 0.0
  pre_modulation_chain: [ffmpeg_highpass, ffmpeg_lowpass]

motif:
  highpass_hz: 300
  lowpass_hz: 2500
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: -3.0
  static_pan: 0.0
  pre_modulation_chain: [ffmpeg_highpass, ffmpeg_lowpass]

air:
  highpass_hz: 2000
  lowpass_hz: 8000
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: -6.0
  static_pan: 0.0
  pre_modulation_chain: [ffmpeg_highpass, ffmpeg_lowpass]

Static processing is invariant across stages; stage-varying elements belong to Pass 07.

## Anchor Timbre Signature
anchor_layer: motif
anchor_timbre_phrase: "warm, slightly dark reed-like texture with soft attack (satisfies Pass 03 anchor identity: 'warm, slightly dark reed-like texture with soft attack')"
anchor_register_match: verified
anchor_brightness_match: verified