# Composition Sketch Packet 06: Source Timbre Design
PACKET ID: cskp-na3-06-source-timbre-design
SKETCH ID: na3
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 06

## Source Selection Per Layer
ground:
  source_family: drone_synthesis
  source_subtype: "additive partials over harmonic series with fundamental at F2 (87.31 Hz), 4 harmonics, moderate rolloff (-5 dB per harmonic)"
  generator_process_hint: generate_drone
  spectral_shape: even_harmonics
  brightness: dark
  attack_profile: breath
  sustain_profile: held
harmonic_body:
  source_family: soundfont
  source_subtype: "musescore_general program 89 (warm pad) in low register"
  generator_process_hint: generate_chord_pad
  spectral_shape: even_harmonics
  brightness: low-mid
  attack_profile: soft
  sustain_profile: held
carrier:
  source_family: soundfont
  source_subtype: "musescore_general program 10 (reed organ) in low register"
  generator_process_hint: render_note_sequence
  spectral_shape: even_harmonics
  brightness: low-mid
  attack_profile: breath
  sustain_profile: held
air:
  source_family: noise_shape
  source_subtype: "pink noise filtered to 2000-8000 Hz band"
  generator_process_hint: generate_noise
  spectral_shape: broadband_filtered
  brightness: upper-mid
  attack_profile: swell
  sustain_profile: decaying

Hard exclusions audit: all source families and subtypes avoid hard_plucks, bright_bells, phone_notification_shapes, percussion_groove, foreground_pop_melody, alarm_like_transients.

## Spectral Allocation
spectral_lanes:
  sub: []
  low: [ground]
  low-mid: [harmonic_body, carrier]
  mid: []
  upper-mid: []
  high: []
  air: [air]
collision_audit: contained
collision_rationale: harmonic_body and carrier share low-mid lane but are both background layers

## Static Processing Chain Per Layer
ground:
  highpass_hz: 40
  lowpass_hz: 1200
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: 0
  static_pan: 0.0
  pre_modulation_chain: [ffmpeg_highpass, ffmpeg_lowpass]
harmonic_body:
  highpass_hz: 100
  lowpass_hz: 2500
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: 0
  static_pan: 0.0
  pre_modulation_chain: [ffmpeg_highpass, ffmpeg_lowpass]
carrier:
  highpass_hz: 150
  lowpass_hz: 2800
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: 0
  static_pan: -0.1
  pre_modulation_chain: [ffmpeg_highpass, ffmpeg_lowpass]
air:
  highpass_hz: 2000
  lowpass_hz: 8000
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: -6
  static_pan: 0.0
  pre_modulation_chain: [ffmpeg_highpass, ffmpeg_lowpass]

Static processing is invariant across stages; stage-varying elements belong to Pass 07.

## Anchor Timbre Signature
anchor_layer: carrier
anchor_timbre_phrase: "musescore_general program 10 (reed organ) in low register with breath attack profile"
anchor_register_match: verified
anchor_brightness_match: verified