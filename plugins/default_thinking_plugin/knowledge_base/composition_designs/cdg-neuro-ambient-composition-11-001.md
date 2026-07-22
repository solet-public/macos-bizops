# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-11-001
WORK_MANIFEST: wmf-neuro-ambient-composition-11-001
Status: ready

## Artistic Reading

- User image / premise: Slow descent into deep absorption through structured hypnotic stages without vocal content
- Musical problem: Creating continuous depth progression that feels inevitable rather than staged, avoiding trance-breaking abruptness
- Listener stance: Passive absorption with attention gently guided inward through harmonic and spatial continuity
- What this is not: A vocal hypnosis session, EDM track, or generic ambient drone; no foreground melodies or rhythmic pulses
- Style-family fit: Sequenced Ambient (Carbon Based Lifeforms, Solar Fields) with emphasis on harmonic continuity over rhythmic variation

## Chosen Musical Premise

- Premise sentence: A 24-minute descent through stable harmonic anchor (F2), evolving arpeggio figures, and voice-adjacent carrier that guides listeners through hypnotic stages without vocal content.
- Primary dramatic motion: Deepen (throughout the piece with structured arc)
- Sound-world metaphor: Slow descent into ocean trench where water pressure increases gradually, light becomes subtle memory
- Musical consequence: Harmony shifts from F2 anchor with [0,3,10] (F-A-C#) to G#2/A2 contrast color [0,4,10] (G#-B#-E), then returns to F2 with [0,3]. Texture evolves from sparse to dense (core work) then back. Spatial width narrows after fractionation.
- Duration rationale: 24 minutes earned through multi-stage development (7 hypnotic stages) with each stage's duration supporting depth progression without rushing.

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for absorption and trance induction
- Stage map:
  - Orientation (3:00): baseline to light, settle and establish safety
  - Induction (4:00): light to medium, pace and lead attention inward
  - Deepening (6:00): medium to deep, reduce orienting load through widening
  - Core absorptive work (7:00): deep, sustain absorption with microdevelopment
  - Fractionation (2:00): deep to light-medium to deep, brief lightening then deeper return
  - Integration (1:30): medium to light, stabilize with calmer harmonic support
  - Return (0:30): light to baseline, reorient through spatial contraction
- Depth trajectory: Increases via harmonic stability (F2 anchor), widening stereo field, and register descent. Decreases through narrowing width, brighter harmonic shifts (fractionation), then return to anchor.
- Pacing-and-leading plan: First pace broad listening with slow ground and sparse carrier, then lengthen rests (carrier phrases), lower register (shift to G#2/A2 in middle section), darken brightness, widen space.
- Ratification plan: Carrier returns at key depth thresholds (induction threshold, deepening stability point) with transformed contour to ratify state.
- Suggestion-compatible windows: Fractionation pocket (20:00-21:00) and integration section (22:00-23:30) for potential spoken guidance.
- Reset avoidance rule: No abrupt cuts, hard transients, alerting cues, or sudden brightness spikes. Transitions use 12+ second crossfades.

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Falling minor third (F4 to D4) followed by descending second (D4 to C#4), processed with delay and low-pass filtering
- First form: Sparse 0.9s phrase in carrier layer at 3:00, soft attack (15ms), long release (2.5s)
- Later forms: Transposed down by minor third in deepening (G#3 to E3), filtered and widened in core work, briefly brighter during fractionation lightening

### Material Inventory - Harmonic Signature
- Home color: F2 (87.31 Hz) root with interval stack [0, 3, 10] (F-A-C#)
- Contrast color: G#2 (98.00 Hz) root with interval stack [0, 4, 10] (G#-B#-E)
- Return color: F2 root with interval stack [0, 3] (F-A), missing C# for open stability

### Material Inventory - Texture Family
- Continuous layer behavior: Warm low-mid drone (ground) with slow beating at 0.1 Hz
- Event layer behavior: Submerged motif fragments appearing every 15-45 seconds, always processed with delay
- Noise / air behavior: Filtered pink noise (high-pass at 2800 Hz) opening slowly with long release

### Material Inventory - Spatial Material
- Width behavior: Narrow (0.5) → medium-wide (0.8) → narrow (0.4)
- Motion behavior: Slow drift of air layer from left to right over 120 seconds
- Depth behavior: Harmonic body layer reverb decay increases from 1.5s to 3.0s in core work, then contracts

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) for ground drone, asymmetric shimmer (multiple incommensurable rates)
- Change rule: Ground modulation rate gradually slows from 0.13 Hz to 0.07 Hz over first 12 minutes, then speeds slightly in fractionation

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1
- Reference pitch-contour profile: falling_settling (from reference_voice_pitch_contours_v1)
- Carrier instrument / layer: GM 89 (Warm Pad) with delay processing
- Cadence behavior: Breath-length phrases of 1.5s (median) with permissive pauses of 0.75s
- Contour behavior: Primarily falling_settling, occasional scoop_then_settle in induction
- Weighting behavior: Emphasis via register warmth (low-mid) and longer decay, not gain
- Return behavior: Returns at induction threshold (3:00), deepening stability point (7:00), core reassurance (13:00), fractionation re-deepening (21:00), integration (22:30)
- Salience limit: Carrier at -15 dB relative to harmonic bed

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Tiny low-mid warmth bloom (0.2s, 15 Hz modulation)
- First pairing: At 3:00 (end of first carrier phrase), paired with harmonic bed widening to medium
- Withholding plan: Absent for 90 seconds after first pairing (until deepening stage)
- Return / trigger plan: Returns at induction threshold, deepening stability point, core reassurance, fractionation re-deepening, integration
- Salience policy: Audible at -18 dB but not salient enough to monitor

## Development Grammar

- Establishment rule: Introduce F2 anchor and sparse carrier in orientation
- Variation rule: Shift harmonic color (F3 to G#2/A2) and widen stereo in deepening
- Contrast rule: Use brighter harmonic color (G#2/A2) for contrast in middle section
- Recurrence rule: Primary motif returns transformed (transposed down) in deepening and core work
- Foreground rule: Event layer only appears as submerged fragments (never foreground)
- Withholding rule: Carrier absent in core work except for rare returns as anchors
- Fractionation rule: Brief lightening (20:00-21:00) via brighter harmonic color and wider stereo, then re-deepening (21:00-22:00) via darker color and narrower stereo
- Trigger conditioning rule: Anchor cue paired with harmonic bed widening at first pairing, associating it with comfort
- Dissolution / cadence rule: Final fade includes harmonic simplification (F2 [0,3]) and spatial contraction
- Anti-padding rule: No material looped merely to fill time; every event has purpose

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish world and first carrier pairing
- Approximate duration range: 0:00 - 3:00
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: ground, harmonic body (F3), carrier_seed
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: sparse, low energy
- Harmonic / spectral posture: F2 anchor with [0,3,10] (F-A-C#)
- Spatial posture: narrow width (0.5), centered
- Transition condition: carrier established and anchor first paired

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace and lead attention inward
- Approximate duration range: 3:00 - 7:00
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: ground, harmonic body (F3), carrier_variants
- Development action: vary and lead
- Hypnotic function: pace and lead attention
- Energy / density posture: slightly denser, medium energy
- Harmonic / spectral posture: F2 anchor with [0,3,10] (F-A-C#)
- Spatial posture: medium width (0.6), slow drift
- Transition condition: attention narrowed and space prepared to widen

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory
- Approximate duration range: 7:00 - 13:00
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: ground, harmonic body (F3), air
- Development action: widen and stabilize
- Hypnotic function: reduce orienting load
- Energy / density posture: medium, with air layer added
- Harmonic / spectral posture: F2 anchor with [0,3,10] (F-A-C#)
- Spatial posture: wide to medium-wide (0.8), slow drift
- Transition condition: depth stable and core can withhold carrier

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with microdevelopment
- Approximate duration range: 13:00 - 20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: ground, harmonic body (F3), rare_carrier
- Development action: withhold and sustain
- Hypnotic function: sustain absorption
- Energy / density posture: low, sparse (only rare carrier returns)
- Harmonic / spectral posture: F2 anchor with [0,3] (F-A)
- Spatial posture: medium-wide (0.7), slow drift
- Transition condition: field thinned and fractionation pre-echo seeded

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper
- Approximate duration range: 20:00 - 22:00
- Hypnotic stage role: fractionation
- Target depth: deep to light-medium to deep
- Material state: ground, harmonic body (G#2/A2), brief_brightness
- Development action: lightening and re-deepening
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: medium (lightening), then deep (re-deepening)
- Harmonic / spectral posture: G#2/A2 with [0,4,10] (G#-B#-E) for lightening; returns to F2
- Spatial posture: wide (0.8) during lightening, contracts to medium-wide (0.6) for re-deepening
- Transition condition: brightness closed and deeper return established

### Formal Unit 6 - Integration / Stabilization
- Name / role: Return familiar material in clearer calm form
- Approximate duration range: 22:00 - 23:30
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: ground, harmonic body (F2)
- Development action: return and clarify
- Hypnotic function: stabilize
- Energy / density posture: sparse, calm
- Harmonic / spectral posture: F2 with [0,3] (F-A)
- Spatial posture: medium width (0.6), slow contraction
- Transition condition: integration cadence prepared

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve and cadence to baseline
- Approximate duration range: 23:30 - 24:00
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: ground_residue, air_fade
- Development action: dissolve and cadence
- Hypnotic function: reorient
- Energy / density posture: very sparse, fading
- Harmonic / spectral posture: F2 with [0] (just root)
- Spatial posture: narrow to intimate (0.4), fading
- Transition condition: final public delivery tail complete

## Orchestration Map

- Root / ground function: Ground layer (warm drone with slow beating)
- Harmonic color function: Harmonic body layer (F2 anchor, evolving intervals)
- Motion function: Air layer and modulation identity
- Foreground / event function: Primary motif (submerged fragments)
- Voice-adjacent induction function: Carrier layer (GM 89 with delay, -15 dB)
- Anchor / trigger function: Low-mid warmth bloom (0.2s, 15 Hz modulation)
- Air / bloom function: Filtered pink noise (high-pass at 2800 Hz)
- Spatial / depth function: Stereo width and reverb decay
- Silence / restraint function: Carrier withholding in core work

## Temporal Behavior

- Micro-event rate: One event (motif fragment) every 15-45 seconds
- Scene-change rate: Every 7 minutes (at stage transitions)
- Long-wave change: Harmonic color shifts from F2 to G#2/A2 and back
- Maximum stasis tolerance: 45 seconds without change (e.g., in core work)
- Repetition policy: Primary motif returns transformed, not identical
- Fractionation timing: Lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: Every major depth threshold (3:00, 7:00, 13:00, 21:00, 22:30)

## Spatial Composition

- Stereo requirement: stereo (not mono)
- Spatial arc: narrow → medium → wide → medium-wide → narrow
- Layer placement principles:
  - Ground: centered
  - Harmonic body: wide stable (0.8)
  - Carrier: near center, low motion
  - Air: edges and rear (0.9)
  - Anchor: local but soft (centered, low volume)
- Motion limits: No fast autopan; motion over 120 seconds
- Verification obligation: Verify not mono, width arc audible, motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No change in harmonic color or spatial width for >45 seconds.
  - Revision target: Add micro-event (motif fragment) or adjust modulation rate.

- Failure mode: too generic
  - Evidence: Sounds like any ambient drone without specific motif or harmonic signature.
  - Revision target: Strengthen primary motif and harmonic contrast (F2 vs G#2/A2).

- Failure mode: weak recurrence
  - Evidence: Primary motif appears once but doesn't return.
  - Revision target: Ensure primary motif returns in deepening and core work, transformed.

- Failure mode: weak fractionation
  - Evidence: Fractionation lightening feels like a reset (e.g., bright and wide, then abrupt return).
  - Revision target: Ensure fractionation lightening is subtle (only slightly brighter) and re-deepening uses same anchor bed.

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier is too bright or has mechanical phrasing.
  - Revision target: Use GM 89 with delay, ensure phrases are breath-length (1.5s) with downward settling.

## Sketch Handoff

- Required sketch style family: Sequenced Ambient (Carbon Based Lifeforms, Solar Fields)
- Required formal units: 7 units as defined above
- Required material to preserve: Primary motif, harmonic signature (home/contrast/return), texture behavior
- Required hypnotic architecture to preserve: Stage map, depth trajectory, fractionation rule, anchor return plan
- Required voice-adjacent induction behavior to preserve: Carrier (GM 89), cadence profile, salience limit
- Required development grammar to preserve: Establishment, Variation, Contrast, Recurrence rules
- Required revision criteria to preserve: All listed above
- Explicit exclusions: No percussion, no hard transients, no abrupt transitions