# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-12-001
WORK_MANIFEST: wmf-neuro-ambient-composition-12-001
Status: ready

## Artistic Reading

- User image / premise: A slow descent into resonant inner space with gentle rhythmic suggestion through delay-processed arpeggios and breath-like carrier pulses
- Musical problem: How to create continuous harmonic evolution without abrupt transitions while maintaining hypnotic function architecture for self-hypnosis
- Listener stance: Inward deep listening with attention shifting from environmental to internal absorption
- What this is not: Pop hook-based, cinematic tension bed, hard percussion groove, or new age piano feature
- Style-family fit: Sequenced Ambient (Carbon Based Lifeforms, Solar Fields) with spectral color and process-and-phasing elements

## Chosen Musical Premise

- Premise sentence: A C4 minor harmonic foundation evolves through slow regional shifts while a breath-length carrier layer guides absorption without melodic foregrounding
- Primary dramatic motion: Deepening → fractionation → re-deepening with harmonic and spatial transformation
- Sound-world metaphor: A resonant cave system where water droplets (motif) echo through chambers of shifting stone (harmonic regions)
- Musical consequence: Harmonic regions shift via root movement while spatial width expands and contracts to mirror depth trajectory
- Duration rationale: 24 minutes earned through staged hypnotic architecture with precise duration for each depth transition phase

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis bed for deep absorption and gentle return
- Stage map:
  - Orientation / Settling: 3:00, baseline-to-light depth, establish world and first carrier pairing
  - Induction / Narrowing: 4:00, light-to-medium depth, pace and lead attention
  - Deepening: 6:00, medium-to-deep depth, widen space and lower salience
  - Core Absorptive Work: 7:00, deep depth, sustain absorption with non-inert microdevelopment
  - Fractionation Pocket: 2:00, deep-to-light-medium-to-deep depth, briefly lighten then return deeper
  - Integration / Stabilization: 1:30, medium-to-light depth, return familiar material in clearer calm form
  - Return / Reorientation: 0:30, light-to-baseline depth, dissolve and cadence
- Depth trajectory: Spectral centroid moves from 1800Hz (Orientation) to 500Hz (Deepening), then rises slightly during Fractionation before returning to 600Hz at Return
- Pacing-and-leading plan: Carrier phrases lengthen from 1.5s to 3.0s during Induction, then deepen with downward settling
- Ratification plan: Anchor glint returns at 10-minute mark (Deepening) and 21-minute mark (Integration), paired with carrier settling
- Suggestion-compatible windows: All transitions occur during natural pauses in the carrier phrase (no speech masking)
- Reset avoidance rule: No abrupt brightness spikes, hard cuts, or transient events; all changes follow natural harmonic evolution

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Descending minor third (C4→A3) followed by rising second (A3→B3), 5-note contour at C4-B3 register
- First form: Sparse, low-salience event (12 notes at 3.0s duration) in carrier layer, soft attack and long release
- Later forms: Transposed down one octave (A3→F3) in Deepening; transformed to minor sixth interval with slower phrasing in Core Absorptive Work

### Material Inventory - Harmonic Signature
- Home color: C4 minor (harmonic minor) with interval stack [0, 3, 7, 10] (Cm7) at root C5 (523.25 Hz)
- Contrast color: E♭4 minor with interval stack [0, 3, 7, 10] (E♭m7) at root E♭5 (698.46 Hz), introduced in Deepening
- Return color: C4 minor with interval stack [0, 3, 7] (Cm) at root C4 (261.63 Hz), used in Integration and Return

### Material Inventory - Texture Family
- Continuous layer behavior: Low-frequency drone at C4 (261.63 Hz) with harmonics [0, 2, 5] (fundamental + octave + fifth), evolving slowly
- Event layer behavior: Primary motif appears every 15-45 seconds, max one event per minute at peak density
- Noise / air behavior: Pink noise high-passed at 2800 Hz with slow filter automation (1700→2400→2200 Hz)

### Material Inventory - Spatial Material
- Width behavior: Narrow (centered) → medium → wide → medium-wide → narrow across stages
- Motion behavior: Very slow drift (0.02-0.05 Hz) in air layer, no fast motion
- Depth behavior: Ground drone dry/close; harmonic body medium reverb (maes_howe, 0.4 wet); air layer distant (york_minster, 0.7 wet)

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.1-0.3 Hz) for harmonic body, complementary rate 0.21 Hz for air layer (incommensurable)
- Change rule: Modulation rates gradually slow over time, harmonic body from 0.25 Hz → 0.1 Hz, air layer from 0.28 Hz → 0.15 Hz

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1
- Reference pitch-contour profile: falling_settling (from reference_voice_pitch_contours_v1)
- Carrier instrument / layer: Low-mid pad voice at C4, GM program 89 (Warm Pad)
- Cadence behavior: Breath-length phrases of 1.5-3 seconds with downward settling
- Contour behavior: Predominantly falling/settling, occasional scoop_then_settle for permissive gestures
- Weighting behavior: Emphasis through register warmth and longer decay tails, not gain
- Return behavior: Transposed down one octave in Deepening; transformed return in Integration
- Salience limit: Always below lead melody attention (max -12 dB relative to harmonic body)

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Tiny softened glint (10ms high-frequency bloom at 8kHz)
- First pairing: Appears at end of first carrier phrase (1.5s) in Orientation, paired with field widening
- Withholding plan: Withheld for 2 minutes after first appearance (until Deepening)
- Return / trigger plan: Returns at 10-minute mark (Deepening) and 21-minute mark (Integration), paired with deeper carrier settling
- Salience policy: Audible enough to learn (within 10dB of background) but not salient enough to monitor

## Development Grammar

- Establishment rule: Introduce ground drone and harmonic body in static state, then slowly introduce carrier layer
- Variation rule: Change harmonic region via root movement (C4 minor → E♭4 minor) in Deepening
- Contrast rule: Contrast color introduced as darker, lower-register harmonic region with wider spectral spread
- Recurrence rule: Primary motif returns transformed (transposed down) in Deepening and Core Absorptive Work
- Foreground rule: No foreground events; primary motif always below lead melody attention
- Withholding rule: Anchor trigger withheld for 2 minutes after first appearance to preserve meaning
- Fractionation rule: Brief brightness rise (lowpass cutoff 2000Hz→3500Hz) followed by return to darker state (cutoff 1800Hz over 45s)
- Trigger conditioning rule: Anchor glint paired with carrier settling in Orientation to create association
- Dissolution / cadence rule: End with slow spatial contraction and spectral darkening, ending on home color
- Anti-padding rule: No material repeated merely to fill time; every event has purpose

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish world and first carrier pairing
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: Orientation / Settling
- Target depth: baseline-to-light
- Material state: Ground drone, harmonic body (Cm7), carrier seed
- Development action: Establish ground and harmonic bed; introduce primary motif as sparse event
- Hypnotic function: Settle and establish safety
- Energy / density posture: Low (2 voices)
- Harmonic / spectral posture: Cm7 at C5, spectral centroid 1800Hz
- Spatial posture: Narrow (centered), dry ground, medium reverb body
- Transition condition: Carrier established and anchor first paired

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace and lead attention
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: Induction / Narrowing
- Target depth: light-to-medium
- Material state: Ground drone, harmonic body (Cm7), carrier variants, anchor return
- Development action: Lengthen carrier phrases; introduce harmonic body variation
- Hypnotic function: Pace and lead attention
- Energy / density posture: Medium (3 voices)
- Harmonic / spectral posture: Cm7 at C5, spectral centroid 1600Hz
- Spatial posture: Medium width (centered to slightly wide), medium reverb body
- Transition condition: Attention narrowed and space prepared to widen

### Formal Unit 3 - Deepening
- Name / role: Widen space and lower salience
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: Deepening
- Target depth: medium-to-deep
- Material state: Ground drone, harmonic body (E♭m7), air layer, low carrier returns
- Development action: Shift to E♭m7 harmonic region; introduce air layer and anchor trigger
- Hypnotic function: Reduce orienting load
- Energy / density posture: Medium (4 voices)
- Harmonic / spectral posture: E♭m7 at E♭5, spectral centroid 800Hz
- Spatial posture: Wide (medium-wide), distant air layer, medium reverb body
- Transition condition: Depth stable and core can withhold carrier

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with microdevelopment
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: Core Absorptive Work
- Target depth: deep
- Material state: Ground drone, harmonic body (E♭m7), rare carrier, motif fragment
- Development action: Thin harmonic density; introduce transformed primary motif
- Hypnotic function: Sustain absorption
- Energy / density posture: Low (3 voices)
- Harmonic / spectral posture: E♭m7 at E♭5, spectral centroid 600Hz
- Spatial posture: Wide (medium-wide), distant air layer, long reverb body
- Transition condition: Field thinned and fractionation pre-echo seeded

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: Fractionation Pocket
- Target depth: deep-to-light-medium-to-deep
- Material state: Ground drone, harmonic body (E♭m7), brief brightness, clearer carrier
- Development action: Lighten spectral centroid; introduce brighter carrier fragment
- Hypnotic function: Briefly lighten then return deeper
- Energy / density posture: Medium (4 voices)
- Harmonic / spectral posture: E♭m7 at E♭5, spectral centroid 1200Hz (lightening)
- Spatial posture: Medium-wide to wide, brighter air layer
- Transition condition: Brightness closed and deeper return established

### Formal Unit 6 - Integration / Stabilization
- Name / role: Return familiar material in clearer calm form
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: Integration / Stabilization
- Target depth: medium-to-light
- Material state: Ground drone, harmonic body (Cm), clearer carrier, anchor reassurance
- Development action: Return to home color; reintroduce transformed primary motif
- Hypnotic function: Stabilize
- Energy / density posture: Medium (3 voices)
- Harmonic / spectral posture: Cm at C4, spectral centroid 1000Hz
- Spatial posture: Medium width (contracting), medium reverb body
- Transition condition: Fractionation redeepened

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve and cadence
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: Return / Reorientation
- Target depth: light-to-baseline
- Material state: Ground residue, air fade
- Development action: Dissolve carrier and event layer; contract spatial width
- Hypnotic function: Reorient
- Energy / density posture: Very low (2 voices)
- Harmonic / spectral posture: Cm at C4, spectral centroid 700Hz
- Spatial posture: Narrow (contracting), dry ground, minimal reverb air
- Transition condition: Integration cadence prepared

## Orchestration Map

- Root / ground function: C4 drone with harmonics [0,2,5] (fundamental + octave + fifth)
- Harmonic color function: Cm7 at C5 → E♭m7 at E♭5 (harmonic body layer)
- Motion function: Air layer with slow drift (0.02-0.05 Hz) and spectral automation
- Foreground / event function: Primary motif (never foregrounded)
- Voice-adjacent induction function: Carrier layer with breath-length phrases
- Anchor / trigger function: Tiny glint (8kHz bloom) paired with carrier settling
- Air / bloom function: Pink noise high-passed at 2800 Hz with filter automation
- Spatial / depth function: Ground dry/close, harmonic body medium reverb, air distant
- Silence / restraint function: Anchor withheld for 2 minutes after first appearance

## Temporal Behavior

- Micro-event rate: Primary motif every 15-45 seconds (max one per minute)
- Scene-change rate: Harmonic region shift every 6 minutes (Deepening transition)
- Long-wave change: Spectral centroid moves from 1800Hz → 500Hz over Deepening
- Maximum stasis tolerance: 45 seconds without deliberate change or withholding
- Repetition policy: Primary motif returns transformed, not identical; no wallpaper repetition
- Fractionation timing: 20:00-20:15 lightening, 20:15-22:00 re-deepening
- Anchor recurrence spacing: First at 1.5s (Orientation), second at 10:00, third at 21:00

## Spatial Composition

- Stereo requirement: Stereo
- Spatial arc: Narrow → medium → wide → medium-wide → narrow across stages
- Layer placement principles: Ground centered, harmonic body wide stable, carrier near-center low motion, air edges/rear
- Motion limits: No fast autopan; max 0.05 Hz drift rate for air layer
- Verification obligation: Verify not mono, width arc audible, motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No harmonic region shift or motif recurrence beyond Orientation
  - Revision target: Add E♭m7 harmonic region in Deepening and transformed motif return

- Failure mode: too generic
  - Evidence: Identical harmonic region throughout, no spectral evolution
  - Revision target: Implement home/contrast/return colors with specific interval stacks

- Failure mode: too busy
  - Evidence: Primary motif appears more than once per minute, anchor glint too frequent
  - Revision target: Reduce motif to max one event per minute; increase anchor withholding

- Failure mode: weak recurrence
  - Evidence: Primary motif appears once and vanishes; anchor glint not returned
  - Revision target: Transform motif in Deepening (transposed down) and return anchor at 10:00

- Failure mode: weak ending
  - Evidence: Simple fade-out without spectral darkening or spatial contraction
  - Revision target: Add slow spatial contraction and spectral centroid to 700Hz at Return

- Failure mode: weak spatial composition
  - Evidence: Stereo width constant throughout, no layer placement differentiation
  - Revision target: Implement spatial arc with ground centered, air edges/rear

- Failure mode: weak hypnotic architecture
  - Evidence: No depth trajectory, no fractionation lightening/deeper-return behavior
  - Revision target: Implement spectral centroid arc and Fractionation Pocket with lightening

- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset; return not deeper or calmer
  - Revision target: Bind brightness to spectral centroid (2000Hz→3500Hz), return via 45s spectral darkening

- Failure mode: weak anchors / triggers
  - Evidence: Anchor glint random, too obvious (8kHz bloom), no first pairing
  - Revision target: Pair glint with carrier settling in Orientation; withhold for 2 minutes

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier phrases mechanical, no downward settling or breath-length phrasing
  - Revision target: Implement reference_voice_cadence_profile_v1 with falling_settling contour

## Sketch Handoff

- Required sketch style family: Sequenced Ambient (Carbon Based Lifeforms, Solar Fields)
- Required formal units: 7 stages with exact durations and transitions
- Required material to preserve: Primary motif, harmonic signature (home/contrast/return), texture behavior
- Required hypnotic architecture to preserve: Stage map, depth trajectory, fractionation rule
- Required voice-adjacent induction behavior to preserve: Carrier layer with breath-length phrases, downward settling
- Required development grammar to preserve: Recurrence rule (transformed return), withholding rule
- Required revision criteria to preserve: All 10 failure modes with evidence and targets
- Explicit exclusions: No percussion, no transient attacks, no hard cuts