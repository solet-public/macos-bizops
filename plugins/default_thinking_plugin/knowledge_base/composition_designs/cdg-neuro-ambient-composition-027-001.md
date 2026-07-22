# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-027-001
WORK_MANIFEST: wmf-neuro-ambient-composition-027-001
Status: ready

## Artistic Reading

- User image / premise: A deep, evolving journey through layered harmonic fields and breath-like instrumental resonance, evoking the sensation of neural quiet settling into a state of non-attentional absorption
- Musical problem: Creating 24-minute hypnotic architecture without vocal narration while maintaining warm, slightly dark sonic identity and avoiding static harmonic behavior
- Listener stance: inward_deep_listening (broad environmental → internal absorptive → gentle reorientation)
- What this is not: vocal narration, speech samples, voice cloning, foreground singer, choir, static pedal tone, foreground melodies
- Style-family fit: Sequenced ambient in Carbon Based Lifeforms/Solar Fields tradition with structured hypnotic architecture

## Chosen Musical Premise

- Premise sentence: A 24-minute harmonic journey where a voice-adjacent carrier guides listeners through hypnotic stages via breath-length phrases, with harmonic evolution through arpeggiated sequences rather than rhythmic pulse
- Primary dramatic motion: establish / deepen / dissolve
- Sound-world metaphor: "Neural static settling into a warm, dark pool"
- Musical consequence: Harmonic color evolves through arpeggiated sequences (F2 home → F#2 contrast), texture shifts from sparse to dense then thin, register descends during deepening, spatial width widens then contracts
- Duration rationale: 24 minutes earned through seven hypnotic stages with sufficient time for harmonic evolution (7 stages × ~3.5 minutes) and micro-structural development

## Hypnotic Function Architecture

- Target use: music-only self-hypnosis for deep absorption
- Stage map:
  - Orientation (0:00-3:00): baseline_to_light, settle_and_establish_safety
  - Induction (3:00-7:00): light_to_medium, pace_and_lead
  - Deepening (7:00-13:00): medium_to_deep, reduce_orienting_load
  - Core absorptive work (13:00-20:00): deep, sustain_absorption
  - Fractionation pocket (20:00-22:00): deep_to_light_medium_to_deep, briefly_lighten_then_return_deeper
  - Integration (22:00-23:30): medium_to_light, stabilize
  - Return (23:30-24:00): light_to_baseline, reorient
- Depth trajectory: Brightness (2800Hz → 1500Hz), density (sparse → dense → thin), spatial width (60° → 120° → 80°)
- Pacing-and-leading plan: First pace broad listening with slow ground and sparse carrier, then lengthen rests (0.75s → 1.8s), lower register (F2 → D2), darken brightness, widen space
- Ratification plan: Carrier phrase with downward settling contour returns at induction threshold (3:15), deepening stability (7:45), and fractionation re-deepening (20:15)
- Suggestion-compatible windows: 10:30, 15:45 (quietest moments with harmonic simplification)
- Reset avoidance rule: no_abrupt_cuts_no_hard_transients_no_alerting_cues_no_sudden_brightness_spikes

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: 3-note falling interval cell (minor third + minor second) in low-mid register
- First form: Single instance at 0:45 (duration 1.8s, rest 2.3s)
- Later forms: Transposed up minor third (induction), slowed by 30% (deepening), fragmented into two notes (core work), brightened/accelerated for fractionation lightening

### Material Inventory - Harmonic Signature
- Home color: F2 root (87.31 Hz) + [0, 3, 10] interval stack (F2-A2-D3)
- Contrast color: F#2 root (93.96 Hz) + [0, 4, 7] interval stack (F#2-A#2-D#3)
- Return color: F2 root + [0, 3, 9] interval stack (F2-A2-C#3)

### Material Inventory - Texture Family
- Continuous layer behavior: Ground drone (F2) with 0.1 Hz amplitude modulation; harmonic body evolves through stage durations
- Event layer behavior: Micro-events (motif fragments, anchor cues) every 15-45 seconds
- Noise / air behavior: Pink noise high-passed at 2800 Hz with slow spectral motion (15-60 seconds)

### Material Inventory - Spatial Material
- Width behavior: Narrow (60°) → wide (120°) during deepening → medium-wide (80°) in integration
- Motion behavior: Slow drifting movement (15-30 seconds per drift) in air layer and carrier
- Depth behavior: Reverb decay increases (1.2s → 3.5s) during deepening, decreases in integration

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.1 Hz) for ground drone; asymmetric shimmer (0.15 Hz + 0.25 Hz) for harmonic body
- Change rule: Modulation rates slow during deepening/core work, speed up slightly in fractionation

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1
- Reference pitch-contour profile: falling_settling (from reference_voice_pitch_contours_v1)
- Carrier instrument / layer: Soft cello SoundFont patch (low-mid register focus)
- Cadence behavior: Breath-length phrases (1.5s avg), permissive pauses (0.75s avg)
- Contour behavior: Predominantly falling_settling with occasional scoop_then_settle
- Weighting behavior: Emphasis through register warmth and harmonic reinforcement
- Return behavior: Returns at thresholds with more submerged contour (e.g., deepening stability)
- Salience limit: Carrier audible at 10dB below harmonic body, not salient enough to monitor

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: 50ms spectral unsealing (3200Hz burst) with soft attack/release
- First pairing: At 3:15 (carrier phrase settles downward → field widens to 80°)
- Withholding plan: Absent during orientation/core work
- Return / trigger plan: 7:45 (deepening stability), 18:00 (fractionation lightening), 21:30 (integration)
- Salience policy: Anchor at -15dB relative to harmonic body

## Development Grammar

- Establishment rule: Introduce ground drone, harmonic body (home color), carrier seed in orientation
- Variation rule: Harmonic color shifts between home/contrast during induction/deepening; carrier phrases vary in duration/register
- Contrast rule: Brightness reduced (2800Hz → 1500Hz) during deepening, briefly increased in fractionation
- Recurrence rule: Primary motif returns transformed at each stage (register, density, filtering)
- Foreground rule: Only carrier and anchor cues become perceptible
- Withholding rule: Primary motif withheld until induction; anchor withheld until deepening
- Fractionation rule: Brightness +3dB, density rise for 45s (18:00-18:45), then return to deepening levels over 15s
- Trigger conditioning rule: Anchor paired with carrier settling downward to associate with comfort/deepening
- Dissolution / cadence rule: End via harmonic simplification (home color), spatial contraction, event density reduction
- Anti-padding rule: No material repeated merely to fill time; every event has developmental purpose

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish sonic world and first carrier pairing
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline_to_light
- Material state: ground drone, harmonic body (home color), carrier seed
- Development action: establish
- Hypnotic function: settle_and_establish_safety
- Energy / density posture: low (sparse carrier, no events)
- Harmonic / spectral posture: home color, brightness 2800Hz
- Spatial posture: narrow (60°), centered ground, harmonic body wide stable
- Transition condition: carrier_established_and_anchor_first_paired (0:45)
- Motion source commitment: parameter_group_sequence — harmonic body evolves from home color to subtle contrast

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace and lead attention through carrier phrasing
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light_to_medium
- Material state: ground drone, harmonic body (home → contrast), carrier variants
- Development action: vary
- Hypnotic function: pace_and_lead
- Energy / density posture: medium (increasing carrier density)
- Harmonic / spectral posture: home color → contrast color
- Spatial posture: narrow (60°) to medium-wide (85°)
- Transition condition: attention_narrowed_and_space_prepared_to_widen
- Motion source commitment: foreground: cadence_carrier — continuous carrier phrasing

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium_to_deep
- Material state: ground drone, harmonic body (contrast → home), air layer, low carrier returns
- Development action: thicken
- Hypnotic function: reduce_orienting_load
- Energy / density posture: high (dense harmonic body)
- Harmonic / spectral posture: contrast color → home color with darker filter
- Spatial posture: wide (120°), reverb decay 3.5s
- Transition condition: depth_stable_and_core_can_withhold_carrier
- Motion source commitment: parameter_group_sequence — harmonic body shifts between contrast and home colors

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with non-inert microdevelopment
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core_absorptive_work
- Target depth: deep
- Material state: ground drone, harmonic body (home), rare carrier, anchor reassurance
- Development action: withhold
- Hypnotic function: sustain_absorption
- Energy / density posture: low (sparse events)
- Harmonic / spectral posture: home color, brightness 1500Hz
- Spatial posture: wide (120°), reverb decay 3.5s
- Transition condition: field_thinned_and_fractionation_pre_echo_seeded
- Motion source commitment: event_schedule — rare motif fragments (10:20) and anchor returns

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper without reset
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation_pocket
- Target depth: deep_to_light_medium_to_deep
- Material state: ground drone, harmonic body (home), fractionation brightness
- Development action: answer
- Hypnotic function: briefly_lighten_then_return_deeper
- Energy / density posture: medium (slight density rise)
- Harmonic / spectral posture: home color + brightness increase
- Spatial posture: medium-wide (80°), reverb decay 2.5s
- Transition condition: fractionation_lightening_complete_and_re-deepening_begins
- Motion source commitment: none — short unit (120s exactly)

### Formal Unit 6 - Integration
- Name / role: Stabilize calm while reducing depth pressure
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium_to_light
- Material state: ground drone, harmonic body (home), carrier return
- Development action: dissolve
- Hypnotic function: stabilize
- Energy / density posture: low (sparse carrier)
- Harmonic / spectral posture: home color, brightness 2000Hz
- Spatial posture: medium-wide (80°) to narrow (65°)
- Transition condition: integration_complete_and_return_ready
- Motion source commitment: none — short unit (90s < 120s)

### Formal Unit 7 - Return
- Name / role: Reorient attention without abrupt arousal
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light_to_baseline
- Material state: ground drone, harmonic body (home), carrier fade
- Development action: dissolve
- Hypnotic function: reorient
- Energy / density posture: very low (minimal events)
- Harmonic / spectral posture: home color, brightness 2500Hz
- Spatial posture: narrow (60°)
- Transition condition: return_complete_and_field_settled
- Motion source commitment: none — short unit (30s < 120s)

## Orchestration Map

- Root / ground function: F2 drone (87.31 Hz) with 0.1 Hz amplitude modulation
- Harmonic color function: harmonic body layer (with evolving interval stacks)
- Motion function: carrier layer (voice-adjacent) and air layer (spectral motion)
- Foreground / event function: primary motif fragments and anchor cues
- Voice-adjacent induction function: soft cello carrier with weighted cadence
- Anchor / trigger function: spectral unsealing cue (anchor)
- Air / bloom function: pink noise high-passed at 2800 Hz
- Spatial / depth function: stereo width and reverb decay control spatial arc
- Silence / restraint function: withholding of primary motif until induction

## Temporal Behavior

- Micro-event rate: every 15-45 seconds (motif fragments, anchor cues)
- Scene-change rate: every 75-150 seconds (harmonic region shifts, density changes)
- Long-wave change: harmonic signature evolves from home → contrast → return
- Maximum stasis tolerance: 45 seconds without deliberate change (e.g., no events >45s)
- Repetition policy: Recurrence transformed each time; no identical repetition
- Fractionation timing: 18:00-18:45 (lightening), 18:45-20:00 (re-deepening)
- Anchor recurrence spacing: 7:45, 18:00, 21:30 (in playback order)

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow (60°) → wide (120°) during deepening → medium-wide (80°) in integration → narrow (60°)
- Layer placement principles: ground centered, harmonic body wide stable, carrier near center low motion, air at edges/rear
- Motion limits: no fast autopan; drifts take 15-30 seconds to complete
- Verification obligation: verify not mono, width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: no perceptible change for >45 seconds in core work (13:00-20:00)
  - Revision target: add micro-event (e.g., motif fragment at 15:20) or harmonic shift

- Failure mode: too generic
  - Evidence: piece sounds like standard ambient drone without specific harmonic signature
  - Revision target: add distinct home/contrast colors and primary motif

- Failure mode: too busy
  - Evidence: more than one event per minute in core work (e.g., >10 events)
  - Revision target: reduce event density to one every 60-90 seconds

- Failure mode: weak recurrence
  - Evidence: primary motif appears once and vanishes (no transformed returns)
  - Revision target: transform motif at each return (e.g., register, filtering)

- Failure mode: weak ending
  - Evidence: piece fades out without harmonic simplification or spatial contraction
  - Revision target: add slow return to home color and spatial narrowing

- Failure mode: weak hypnotic architecture
  - Evidence: deepening stage doesn't feel deeper (no brightness reduction, spatial narrowing)
  - Revision target: adjust harmonic color, brightness, and spatial width to match depth targets

- Failure mode: weak fractionation
  - Evidence: lightening feels like reset (bright and busy)
  - Revision target: reduce fractionation salience (keep brightness increase minimal, no new events)

- Failure mode: weak anchors / triggers
  - Evidence: anchor cue too obvious (0dB) or absent at thresholds
  - Revision target: soften to -15dB and ensure returns at 7:45, 18:00, 21:30

- Failure mode: weak voice-adjacent induction
  - Evidence: carrier has mechanical timing or bright register
  - Revision target: use reference cadence profile with downward settling

## Sketch Handoff

- Required sketch style family: sequenced ambient with hypnotic architecture
- Required formal units: 7 stages as defined in stage map (orientation to return)
- Required material to preserve: primary motif, harmonic signature (home/contrast/return), texture behavior
- Required hypnotic architecture to preserve: stage map, depth trajectory, fractionation rule
- Required voice-adjacent induction behavior to preserve: carrier (soft cello), cadence (breath-length, downward settling), salience ceiling
- Required development grammar to preserve: recurrence rule, contrast rule, fractionation rule
- Required revision criteria to preserve: all listed above
- Explicit exclusions: no percussion, no transient attacks, no pop hooks