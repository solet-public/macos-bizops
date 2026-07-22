# Composition Sketch Packet 06: Source Timbre Design
PACKET ID: cskp-nd3-06-source-timbre-design
SKETCH ID: nd3
MANIFEST ID: wmf-neuro-ambient-nd3-001
PASS NUMBER: 06

## Source Selection Per Layer
substrate:
  source_family: drone_synthesis
  source_subtype: "additive partials over harmonic series (fundamental F2, 3 harmonics with -6dB rolloff)"
  generator_process_hint: "generate_drone"
  spectral_shape: harmonic_series_subset
  brightness: dark
  attack_profile: breath
  sustain_profile: held
harmonic_bed:
  source_family: soundfont
  source_subtype: "musescore_general SoundFont program 89 (warm pad)"
  generator_process_hint: "generate_chord_pad"
  spectral_shape: even_harmonics
  brightness: low-mid
  attack_profile: soft
  sustain_profile: held
entrainment:
  source_family: noise_shape
  source_subtype: "pink noise filtered to mid register"
  generator_process_hint: "generate_noise"
  spectral_shape: broadband_filtered
  brightness: mid
  attack_profile: breath
  sustain_profile: looped
air:
  source_family: noise_shape
  source_subtype: "pink noise filtered to upper-mid/high register"
  generator_process_hint: "generate_noise"
  spectral_shape: broadband_filtered
  brightness: air-only
  attack_profile: breath
  sustain_profile: looped
motif:
  source_family: soundfont
  source_subtype: "generaluser_gs SoundFont program 89 (warm pad)"
  generator_process_hint: "generate_chord_pad"
  spectral_shape: even_harmonics
  brightness: low-mid
  attack_profile: breath
  sustain_profile: held

Hard exclusions audit: All sources avoid percussion, transients, and groove implication per Pass 01.

## Spectral Allocation
spectral_lanes:
  sub: [substrate]
  low-mid: [harmonic_bed]
  mid: [entrainment]
  high: [air]
collision_audit: clean
collision_rationale:

## Static Processing Chain Per Layer
substrate:
  highpass_hz: null
  lowpass_hz: 1200
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: 0.0
  static_pan: 0.0
  pre_modulation_chain: ["ffmpeg_lowpass", "ffmpeg_lowshelf"]
harmonic_bed:
  highpass_hz: null
  lowpass_hz: 2500
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: 0.0
  static_pan: 0.0
  pre_modulation_chain: ["ffmpeg_lowpass", "ffmpeg_atilt"]
entrainment:
  highpass_hz: 800
  lowpass_hz: 2500
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: 0.0
  static_pan: 0.0
  pre_modulation_chain: ["ffmpeg_highpass", "ffmpeg_lowpass"]
air:
  highpass_hz: 2000
  lowpass_hz: 8000
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: 0.0
  static_pan: 0.0
  pre_modulation_chain: ["ffmpeg_highpass", "ffmpeg_lowpass"]
motif:
  highpass_hz: 100
  lowpass_hz: null
  bandpass_center_hz: null
  bandpass_q: null
  static_gain_db: 0.0
  static_pan: 0.0
  pre_modulation_chain: ["ffmpeg_highpass", "ffmpeg_lowshelf"]

Static processing is invariant across stages. Stage-varying chain elements (modulation, spatial) are reserved for Pass 07 and Pass 09.

## Anchor Timbre Signature
anchor_layer: motif
anchor_timbre_phrase: "warm breathy synth pad with soft attack and long release (as per Pass 03 anchor identity, using generaluser_gs SoundFont program 89)"
anchor_register_match: verified
anchor_brightness_match: verified