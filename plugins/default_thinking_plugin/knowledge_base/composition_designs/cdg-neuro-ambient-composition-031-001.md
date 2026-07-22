# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-031-001
WORK_MANIFEST: wmf-neuro-ambient-composition-031-001
Status: ready

## Artistic Reading

- User image / premise: Continuous breath-length prosodic lead voice guiding self-hypnotic journey through seven stages
- Musical problem: Creating a non-melodic, voice-adjacent carrier that guides without becoming foreground melody or breaking trance
- Listener stance: Following the continuous lead voice as it shapes attention inward and outward
- What this is not: Pop hook, dramatic shift, alerting cue, or lead that becomes the focus
- Style-family fit: Hypnotic Stage Architecture with Prosodic Lead Voice (directly implemented)

## Chosen Musical Premise

- Premise sentence: A sarangi-like carrier layer guides the listener through seven hypnotic stages via breath-length phrases, harmonic shifts, and subtle spatial unfolding without a single transient or groove
- Primary dramatic motion: establish → deepen → fractionate → stabilize → return
- Sound-world metaphor: A slow, warm river flowing through a forest of low-mid resonance
- Musical consequence: Harmony shifts from F2 to G2 (F-centered open voicings), texture evolves from substrate-only to air blooms, register descends during deepening, space narrows then widens
- Duration rationale: 24 minutes allows for seven stages with sufficient development time (deepening 6min, core absorptive work 7min) and micro-structural evolution to feel earned

## Hypnotic Function Architecture

- Target use: music-only self-hypnosis
- Stage map:
  - Orientation / settling: 0:00-3:00, baseline-to-light, settle and establish safety
  - Induction / narrowing: 3:00-7:00, light-to-medium, pace and lead attention inward
  - Deepening: 7:00-13:00, medium-to-deep, reduce orienting load and widen space
  - Core absorptive work: 13:00-20:00, deep, sustain absorption with micro-developments
  - Fractionation: 20:00-22:00, deep-to-light-medium-to-deep, briefly lighten then return deeper
  - Integration: 22:00-23:30, medium-to-light, stabilize and reduce depth pressure
  - Return: 23:30-24:00, light-to-baseline, reorient without alerting
- Depth trajectory: F2 (87.31Hz) → F#2 (92.50Hz) → G2 (103.83Hz) → A2 (110.00Hz) → G2 (103.83Hz) → F#2 (92.50Hz) → F2 (87.31Hz), with harmonic density increasing to A2 then decreasing
- Pacing-and-leading plan: Carrier phrase arcs (6-14s) become more regular during induction, then deepen with longer decay in deepening
- Ratification plan: Carrier's downward settling contour (84.5% falling_settling) returns at end of each stage to ratify depth
- Suggestion-compatible windows: Air blooms (15-45s) occur during core absorptive work and fractionation
- Reset avoidance rule: All transitions crossfaded over 15s with harmonic/spectral motion; no abrupt changes

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Two-note minor third (3 semitones) followed by falling second
- First form: F2-A2 minor third → G#2 (3s duration) in orientation
- Later forms: Shifted to F#2-A#2 minor third during deepening; filtered fragment (10% amplitude) in core absorptive work

### Material Inventory - Harmonic Signature
- Home color: F2 + G#2 (minor third) + C3 (perfect fifth), 1.0:2.0:3.0 gain
- Contrast color: F#2 + A#2 (minor third) + D3 (perfect fifth), shifted semitone
- Return color: F2 + G#2 + C3 with minor third reduced to 0.7 gain, fifth at 1.2

### Material Inventory - Texture Family
- Continuous layer: Substrate drone (F2) with slow beating (0.1Hz), attack 8-45s, release 20-60s
- Event layer: Air blooms (high-frequency noise) every 15-45s, lasting 2-8s
- Noise / air: Filtered high air (10kHz) opening slowly during deepening, closing in integration

### Material Inventory - Spatial Material
- Width behavior: Narrow (10%) → wide (85%) → narrow (20%)
- Motion behavior: Slow drift (1-3s per degree) during deepening, no fast autopan
- Depth behavior: Reverb tail increases to 2.5s during deepening, decreases to 0.8s by return

### Material Inventory - Modulation Identity
- Rate family: Long breath (0.07-0.13Hz) for substrate, secondary shimmer (0.25-0.45Hz)
- Change rule: Breath rate slows 10% during deepening, speeds slightly in core absorptive work

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: v1 empirical (6-14s phrases)
- Reference pitch-contour profile: v1 (84.5% falling_settling, 8.2% rising_opening)
- Carrier instrument: Sarangi-like bowed string (warm, low-mid resonance)
- Cadence behavior: Breath-length phrases with 1-3s inter-phrase breaths, downward settling
- Contour behavior: Primarily falling_settling (84.5%), occasional rising_opening in fractionation
- Weighting behavior: Slight sustain increase (0.2s) at phrase end during deepening
- Return behavior: Carrier contour returns to orientation form in integration and return
- Salience limit: 6dB above harmonic bed, 10dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Low-mid warmth bloom (F2+G#2, 0.5s)
- First pairing: At 0:45 (orientation), paired with slight field widening
- Withholding plan: Absent during induction to build expectation
- Return / trigger plan: Deepening stability (10:30), fractionation re-deepening (19:45), integration confirmation (22:15)
- Salience policy: Audible enough to learn (0.5s, 1dB above bed), not salient enough to monitor

## Development Grammar

- Establishment rule: Introduce carrier and substrate in orientation with minimal harmonic color
- Variation rule: Shift harmonic root by semitone during stage transitions (F2→F#2)
- Contrast rule: Use contrast color (F#2) for induction and deepening, return to home in integration
- Recurrence rule: Primary motif returns transformed (shifted root, reduced amplitude) each stage
- Foreground rule: Carrier remains foreground throughout; no other layer becomes foreground
- Withholding rule: Anchor cues withheld during induction and fractionation lightening phase
- Fractionation rule: Carrier contour briefly brightens (register lift) during fractionation lightening, returns deeper with darker contour
- Trigger conditioning rule: Anchor bloom paired with carrier's downward settling to create conditioned deepening association
- Dissolution / cadence rule: Ends with carrier contour returning to orientation form, followed by 10s air layer fade
- Anti-padding rule: No material repeated without transformation; all events spaced ≥15s apart

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish world and first carrier pairing
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline-to-light
- Material state: ground, harmonic_body (home color), carrier_seed
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: low (substrate only)
- Harmonic / spectral posture: F2 home color
- Spatial posture: narrow (10% width), centered
- Transition condition: carrier established and anchor first paired at 0:45
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace and lead attention inward
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light-to-medium
- Material state: ground, harmonic_body (contrast color), carrier_variants
- Development action: vary
- Hypnotic function: pace and lead attention inward
- Energy / density posture: medium (subtle carrier variations)
- Harmonic / spectral posture: F#2 contrast color
- Spatial posture: medium (35% width), slight widening
- Transition condition: attention narrowed and space prepared to widen at 7:00
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium-to-deep
- Material state: ground, harmonic_body (contrast color), air, low_carrier_returns
- Development action: deepen
- Hypnotic function: reduce orienting load and widen space
- Energy / density posture: medium-high (air blooms introduced)
- Harmonic / spectral posture: G2 contrast color
- Spatial posture: wide (85% width), slow drift
- Transition condition: depth stable and core can withhold carrier at 13:00
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with micro-developments
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: ground, harmonic_body (home color), rare_carrier, anchor_reassurance
- Development action: sustain
- Hypnotic function: sustain absorption with non-inert microdevelopment
- Energy / density posture: high (core depth)
- Harmonic / spectral posture: A2 home color
- Spatial posture: wide (80% width), stable
- Transition condition: field thinned and fractionation pre-echo seeded at 20:00
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper without reset
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep-to-light-medium-to-deep
- Material state: ground, harmonic_body (contrast color), carrier_lightening
- Development action: fractionate
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: medium (lightening phase)
- Harmonic / spectral posture: G2 contrast color
- Spatial posture: medium-wide (65% width), slight contraction
- Transition condition: lightening complete and re-deepening begins at 21:45
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 6 - Integration / Stabilization
- Name / role: Stabilize and reduce depth pressure
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium-to-light
- Material state: ground, harmonic_body (contrast color), carrier_reassurance
- Development action: stabilize
- Hypnotic function: stabilize and reduce depth pressure
- Energy / density posture: medium (reduced)
- Harmonic / spectral posture: F#2 contrast color
- Spatial posture: medium (40% width), gentle contraction
- Transition condition: depth pressure reduced at 23:30
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 7 - Return / Reorientation
- Name / role: Reorient without alerting cues
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light-to-baseline
- Material state: ground, harmonic_body (home color), carrier_final
- Development action: dissolve
- Hypnotic function: reorient without alerting
- Energy / density posture: low (fade)
- Harmonic / spectral posture: F2 home color
- Spatial posture: narrow (15% width), centered
- Transition condition: depth pressure reduced at 23:30
- Motion source commitment: foreground: voice_adjacent

## Orchestration Map

- Root / ground function: substrate drone (F2)
- Harmonic color function: harmonic_body layer
- Motion function: carrier (voice-adjacent)
- Foreground / event function: air blooms
- Voice-adjacent induction function: carrier (sarangi-like, prosody)
- Anchor / trigger function: low-mid warmth bloom
- Air / bloom function: filtered high air layer
- Spatial / depth function: harmonic_body (wide) and carrier (centered)
- Silence / restraint function: withholding anchor during induction

## Temporal Behavior

- Micro-event rate: every 15-45 seconds (air blooms)
- Scene-change rate: every 75-150 seconds (harmonic shifts at stage boundaries)
- Long-wave change: harmonic root shift across piece
- Maximum stasis tolerance: 45 seconds (satisfied by air blooms)
- Repetition policy: motif returns transformed every 2-3 stages
- Fractionation timing: lightening at 20:30, re-deepening at 21:45
- Anchor recurrence spacing: 0:45, 10:30, 19:45, 22:15

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow (10%) → wide (85%) → narrow (20%)
- Layer placement principles: ground centered, harmonic_body wide stable, carrier near center low motion
- Motion limits: no fast autopan (max 3s per degree)
- Verification obligation: verify not mono, width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: no air blooms for >45s, harmonic color unchanged in deepening
  - Revision target: add air blooms at 15-45s intervals and introduce harmonic shift in deepening

- Failure mode: weak hypnotic architecture
  - Evidence: no clear stage progression, depth trajectory flat
  - Revision target: adjust harmonic root shifts to match specified depth trajectory

- Failure mode: weak fractionation
  - Evidence: lightening creates reset, return not deeper
  - Revision target: reduce fractionation salience (brightness/density), strengthen re-deepening with darker contour

- Failure mode: weak voice-adjacent induction
  - Evidence: carrier not continuous, contour mechanical
  - Revision target: ensure carrier present in all sections with empirical pitch-contour profile

- Failure mode: weak anchors
  - Evidence: cues random, absent, or too obvious
  - Revision target: repair cue identity and first pairing; add withholding

## Sketch Handoff

- Required sketch style family: Hypnotic Stage Architecture with Prosodic Lead Voice
- Required formal units: 7 (as defined)
- Required material to preserve: primary motif, harmonic signature colors, texture behavior
- Required hypnotic architecture to preserve: stage map, depth trajectory, fractionation, anchors
- Required voice-adjacent induction behavior to preserve: carrier (sarangi), cadence, weighting, salience
- Required development grammar to preserve: rules for recurrence, contrast, fractionation
- Required revision criteria to preserve: failure modes and targets above
- Explicit exclusions: no percussion, transient attacks, pop hooks, abrupt changes