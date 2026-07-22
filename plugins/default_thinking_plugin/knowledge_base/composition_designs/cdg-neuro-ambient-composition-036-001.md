# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-036-001
WORK_MANIFEST: wmf-neuro-ambient-composition-036-001
Status: ready

## Artistic Reading

- User image / premise: A continuous, breath-led instrumental journey through hypnotic stages for self-hypnosis
- Musical problem: Creating nonverbal stage transitions that feel like weather changes rather than edits while maintaining continuous vocal-adjacent carrier
- Listener stance: Following the breath-length phrase arcs without active concentration
- What this is not: No vocal narration, no foreground melodies, no percussion, no abrupt transitions
- Style-family fit: Neuro-ambient with instrumental voice-adjacent carrier (foreground vocal-led prosody mode)

## Chosen Musical Premise

- Premise sentence: A continuous breath-led instrumental carrier guides the listener through a structured hypnotic journey using evolving harmonic bed and subtle spatial shifts to articulate stage transitions without interruption
- Primary dramatic motion: Deepening (through the core) and return (to baseline)
- Sound-world metaphor: A slow, deepening breath in a warm cave that gradually opens to the outside world
- Musical consequence: Harmonic bed shifts from F2-centered open voicings to darker resonance; carrier's phrase arcs lengthen and settle downward during deepening; spatial width expands then contracts
- Duration rationale: 24 minutes allows sufficient time for deepening (6 minutes) and core absorptive work (8:45) to feel earned with fractionation as a brief lightening within the core

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for focused immersive listening
- Stage map:
  - Orientation (0:00-3:00): baseline_to_light, establish safety and first carrier pairing
  - Induction (3:00-7:00): light_to_medium, pace attention inward with carrier phrasing
  - Deepening (7:00-13:00): medium_to_deep, reduce orienting load and widen space
  - Core Absorptive Work (13:00-21:45): deep, sustain absorption with micro-developments
  - Fractionation (21:45-22:00): deep_to_light_medium_to_deep, briefly lighten then return deeper
  - Integration (22:00-23:30): medium_to_light, stabilize and reduce depth pressure
  - Return (23:30-24:00): light_to_baseline, reorient with gentle contraction
- Depth trajectory: Baseline → light (induction) → medium (deepening start) → deep (core and fractionation return) → light-medium (fractionation lightening) → medium-light (integration) → baseline
- Pacing-and-leading plan: Start with slow ground and sparse carrier, then lengthen rests, lower register, darken brightness, widen space
- Ratification plan: Carrier's downward settling contour and anchor cue recur at depth thresholds to ratify acceptance
- Suggestion-compatible windows: Quiet spaces between carrier phrases (core and integration)
- Reset avoidance rule: No abrupt cuts, hard transients, alerting cues, or sudden brightness spikes

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Two-note interval cell (minor third) followed by falling second, breath-length phrasing
- First form: Orientation - F4 to D4 (2.5s phrase), repeated at E4 to C#4 after 10s
- Later forms: Deepening - D4 to B3 (transposed down minor third), Core - fragment at 15s intervals, Fractionation - F#4 to D#4 (brightened)

### Material Inventory - Harmonic Signature
- Home color: F2 root, interval stack [0,3,10] (F2, A2, F3) - open voicing
- Contrast color: [0,4,9] (F2, Bb2, E3) - darker and wider than home
- Return color: Home with missing fifth (F2, A2) - returns in integration and return

### Material Inventory - Texture Family
- Continuous layer behavior: Ground drone (F2) with slow beating (0.1 Hz), long attack (3s), release (45s)
- Event layer behavior: Rare motif fragments (every 15-45s) and soft partial blooms
- Noise / air behavior: Air bloom layer (filtered high noise) opening slowly at stereo edges

### Material Inventory - Spatial Material
- Width behavior: Narrow (orientation) → medium (induction) → wide (deepening/core) → medium-wide (fractionation) → narrow (integration/return)
- Motion behavior: Slow drifting in air layer; carrier near center with minimal movement
- Depth behavior: Reverb tail gradually shortens during return

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) for ground; asymmetric shimmer (0.15, 0.25, 0.35 Hz) for harmonic bed
- Change rule: Modulation rates slow during deepening/core, accelerate slightly in fractionation

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1 (foreground vocal-led mode)
- Reference pitch-contour profile: reference_voice_pitch_contours_v1 (84.5% falling_settling)
- Carrier instrument / layer: Sarangi-like bowed string (warm, low-mid register)
- Cadence behavior: Breath-length phrase arcs (6-14s) with 1-3s inter-phrase breaths
- Contour behavior: Primarily falling_settling (84.5%), occasional rising_opening in fractionation
- Weighting behavior: Slight emphasis on second note via longer sustain
- Return behavior: Motif returns transformed at depth thresholds (deepening stability, fractionation return)
- Salience limit: 6dB above harmonic bed and 10dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Low-mid warmth bloom (0.5s spectral unsealing at 300-400Hz)
- First pairing: Orientation (1:30) - carrier phrase settles downward, anchor appears at release
- Withholding plan: Anchor absent during induction and deepening
- Return / trigger plan: Deepening stability (10:00), Fractionation re-deepening (21:50), Integration (22:45)
- Salience policy: Audible enough to learn (-12dB relative to carrier), not salient enough to monitor

## Development Grammar

- Establishment rule: Introduce carrier, ground drone, and first anchor pairing in orientation
- Variation rule: Transform motif by register (downward) and density (thickening in deepening)
- Contrast rule: Shift harmonic color from home to contrast during deepening
- Recurrence rule: Motif returns at 15s intervals in core; anchor returns transformed
- Foreground rule: Carrier always foreground (6dB above bed, 10dB above substrate)
- Withholding rule: Anchor withheld during induction and deepening
- Fractionation rule: Lighten brightness (10%) and density for 5s, then return darker
- Trigger conditioning rule: Anchor paired with carrier's downward settling in orientation
- Dissolution / cadence rule: Final fade includes harmonic simplification (return color) and spatial contraction
- Anti-padding rule: No loops without transformation; every event must be micro-developed

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish world and first carrier pairing
- Approximate duration range: 0:00-3:00 (180s)
- Hypnotic stage role: orientation
- Target depth: baseline_to_light
- Material state: ground, harmonic_body (home), carrier_seed
- Development action: establish
- Hypnotic function: settle_and_establish_safety
- Energy / density posture: sparse, low activity
- Harmonic / spectral posture: home color (F2 [0,3,10]), recessed brightness
- Spatial posture: narrow
- Transition condition: carrier_established_and_anchor_first_paired
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace attention inward
- Approximate duration range: 3:00-7:00 (240s)
- Hypnotic stage role: induction
- Target depth: light_to_medium
- Material state: ground, harmonic_body (home), carrier_variants
- Development action: vary, answer
- Hypnotic function: pace_and_lead_attention
- Energy / density posture: moderate, slightly more active than orientation
- Harmonic / spectral posture: home color with slight brightening (10%)
- Spatial posture: medium width
- Transition condition: attention_narrowed_and_space_prepared_to_widen
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory
- Approximate duration range: 7:00-13:00 (360s)
- Hypnotic stage role: deepening
- Target depth: medium_to_deep
- Material state: ground, harmonic_body (contrast), air, low_carrier_returns
- Development action: thicken, transform
- Hypnotic function: reduce_orienting_load
- Energy / density posture: dense but soft (more event layer)
- Harmonic / spectral posture: contrast color (F2 [0,4,9]), darker
- Spatial posture: wide
- Transition condition: depth_stable_and_core_can_withhold_carrier
- Motion source commitment: foreground: voice_adjacent, parameter_group_sequence

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with micro-developments
- Approximate duration range: 13:00-21:45 (525s)
- Hypnotic stage role: core_absorptive_work
- Target depth: deep
- Material state: ground, harmonic_body (contrast), rare_carrier, rare_motif_fragment
- Development action: withhold, recur
- Hypnotic function: sustain_absorption
- Energy / density posture: dense but with micro-thinning (event layer sparse)
- Harmonic / spectral posture: contrast color, very dark
- Spatial posture: wide to medium-wide (slight contraction)
- Transition condition: field_thinned_and_fractionation_pre_echo_seeded
- Motion source commitment: foreground: voice_adjacent, event_schedule

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper
- Approximate duration range: 21:45-22:00 (15s)
- Hypnotic stage role: fractionation
- Target depth: deep_to_light_medium_to_deep
- Material state: ground, harmonic_body (contrast with brightening), brief_brightness, clearer_carrier
- Development action: answer (lighten), transform (return)
- Hypnotic function: briefly_lighten_then_return_deeper
- Energy / density posture: thinned (lightening) then dense again (return)
- Harmonic / spectral posture: contrast color with +10% brightness for 5s
- Spatial posture: medium-wide (lightening) → wide (return)
- Transition condition: brightness_closed_and_deeper_return_established
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 6 - Integration
- Name / role: Stabilize and reduce depth pressure
- Approximate duration range: 22:00-23:30 (90s)
- Hypnotic stage role: integration
- Target depth: medium_to_light
- Material state: ground, harmonic_body (return color), anchor_reassurance
- Development action: dissolve, recur
- Hypnotic function: stabilize
- Energy / density posture: thinning, moderate activity
- Harmonic / spectral posture: return color (F2 [0,3]), dark
- Spatial posture: medium to narrow
- Transition condition: integration_complete (harmonic simplification)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 7 - Return
- Name / role: Reorient with gentle contraction
- Approximate duration range: 23:30-24:00 (30s)
- Hypnotic stage role: return
- Target depth: light_to_baseline
- Material state: ground, harmonic_body (return color), carrier_fadeout
- Development action: dissolve, cadence
- Hypnotic function: reorient
- Energy / density posture: very sparse, fading
- Harmonic / spectral posture: return color (F2 [0,3]), dark
- Spatial posture: narrow to baseline
- Transition condition: cadence_complete
- Motion source commitment: foreground: voice_adjacent

## Orchestration Map

- Root / ground function: Ground drone (F2) - stable anchor
- Harmonic color function: Harmonic body layer - pitch identity and harmonic shift
- Motion function: Carrier (voice-adjacent) - time perceptibility through phrase arcs
- Foreground / event function: Motif fragments (rare) - occasional legibility
- Voice-adjacent induction function: Carrier layer (sarangi-like) - hypnotist cadence
- Anchor / trigger function: Low-mid warmth bloom (anchor layer) - learned reassurance
- Air / bloom function: Air bloom layer - openness at stereo edges
- Spatial / depth function: Stereo placement of layers - space as musical dimension
- Silence / restraint function: Anchor withholding during induction/deepening - meaningful absence

## Temporal Behavior

- Micro-event rate: Motif fragments every 15-45 seconds (core and fractionation)
- Scene-change rate: Harmonic region changes every 75-150 seconds
- Long-wave change: Overall harmonic shift from home to contrast and back
- Maximum stasis tolerance: 45 seconds without meaningful change
- Repetition policy: Motif fragments recur transformed; no identical repetition
- Fractionation timing: Lightening 21:45-21:50, re-deepening 21:50-22:00
- Anchor recurrence spacing: First pairing (1:30), Deepening stability (10:00), Fractionation re-deepening (21:50), Integration (22:45)

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: Narrow → medium → wide → medium-wide → narrow
- Layer placement principles: Ground centered, harmonic_body wide_stable, carrier near_center_low_motion, air edges_and_rear
- Motion limits: No fast autopan (max 20s for any spatial movement)
- Verification obligation: Verify not mono, not dual-mono; width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No perceptible change for >45s in core or deepening
  - Revision target: Add micro-event (motif fragment) and harmonic shift within next 30s
- Failure mode: too generic
  - Evidence: Sounds like any ambient drone; no distinct motif or harmonic signature
  - Revision target: Strengthen two-note interval cell and define home/contrast colors more clearly
- Failure mode: too busy
  - Evidence: Too many motif fragments (>1 per 15s) or bright events
  - Revision target: Reduce event rate to max one per 30s and darken brightness in core
- Failure mode: weak recurrence
  - Evidence: Motif appears once and vanishes; anchor returns without transformation
  - Revision target: Transform motif by register in each return; anchor must be brighter on first return
- Failure mode: weak ending
  - Evidence: Piece fades out without harmonic simplification or spatial contraction
  - Revision target: Add return color (F2 [0,3]) and contract width to narrow in last 15s
- Failure mode: weak spatial composition
  - Evidence: Output mono or width doesn't change
  - Revision target: Reconstruct stereo with ground centered, harmonic_body wide, air at edges
- Failure mode: weak hypnotic architecture
  - Evidence: No clear stage progression; depth changes not audible
  - Revision target: Map harmonic color, density, and spatial width to each stage as defined
- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset; return not deeper
  - Revision target: Ensure fractionation lightening (5s) and return is darker than core
- Failure mode: weak anchors / triggers
  - Evidence: Anchor cue at 10:00 too bright or obvious
  - Revision target: Reduce anchor level to -12dB relative to carrier and make it 0.5s bloom
- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier too bright or mechanical; phrases metrical
  - Revision target: Apply empirical profile (84.5% falling_settling) and soften attacks

## Sketch Handoff

- Required sketch style family: neuro_ambient_foreground_vocal_led_prosody
- Required formal units: 7 (as defined in Formal Architecture)
- Required material to preserve: Primary motif, harmonic signature colors, texture behavior
- Required hypnotic architecture to preserve: Stage map with concrete parameters (harmonic color, density, width)
- Required voice-adjacent induction behavior to preserve: Carrier (sarangi-like), phrase timing, contour proportions
- Required development grammar to preserve: Establishment, variation, contrast rules
- Required revision criteria to preserve: All 10 failure modes and targets
- Explicit exclusions: No percussion, no abrupt transitions, no bright events in core