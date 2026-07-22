# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-037-001
WORK_MANIFEST: wmf-neuro-ambient-composition-037-001
Status: ready

## Artistic Reading

- User image / premise: A continuous, breath-phrased lead voice guiding the listener through a self-hypnotic journey
- Musical problem: Creating continuous vocal-styled guidance without demanding attention or mimicking speech
- Listener stance: Following the lead's breath-length phrasing while absorbing into ambient texture
- What this is not: Pop song with melody, generic drone bed, or abrupt transition piece
- Style-family fit: Hypnotic Stage Architecture Journey with instrumental prosody and generative ambient texture

## Chosen Musical Premise

- Premise sentence: A continuous sarangi-like carrier guides the listener through a 24-minute hypnotic journey using breath-length phrasing and spectral deepening to evoke internal resonance without vocal mimicry
- Primary dramatic motion: Deepen and return (through the stages)
- Sound-world metaphor: A slow, deepening breath in a warm cave
- Musical consequence: Carrier pitch contour descends gradually; harmonic bed shifts from minor to suspended colors; spatial width expands then contracts
- Duration rationale: 24 minutes allows full hypnotic journey with sufficient time for breath-length phrasing (6-14s) and stage development without rushing

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for focused immersive listening
- Stage map:
  - Orientation / Settling: 0:00-3:00, baseline depth, establishes carrier and harmonic bed
  - Induction / Narrowing: 3:00-7:00, light depth, carrier begins pacing attention inward
  - Deepening: 7:00-13:00, medium depth, carrier ratifies state with downward settling
  - Core Absorptive Work: 13:00-20:00, deep depth, carrier carries continuous prosody at deepest register
  - Fractionation Pocket: 20:00-22:00, brief lightening (medium depth), carrier briefly brightens then returns deeper
  - Integration / Stabilization: 22:00-23:30, light depth, carrier returns to orientation motifs at clarified register
  - Return / Reorientation: 23:30-24:00, baseline depth, carrier clarifies phrase endings for gentle reorientation
- Depth trajectory: Baseline (0-3m) → Light (3-7m) → Medium (7-13m) → Deep (13-20m) → Brief Lightening (20-22m) → Medium (22-23.5m) → Baseline (23.5-24m)
- Pacing-and-leading plan: Carrier establishes 10-second phrase arc in orientation, gradually shortening to 8 seconds during induction
- Ratification plan: Carrier returns specific 3-note motif (F-A-C) at end of each deepening phrase
- Suggestion-compatible windows: Quietest moments (after carrier phrases) in orientation, induction, and deepening
- Reset avoidance rule: All transitions use slow filter automation (15-30s) and avoid brightness spikes

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: 3-note interval cell (F-A-C) with downward glide on C
- First form: Introduced at F3 (87.3 Hz) as 10-second phrase with 2-second rest
- Later forms: 
  - Induction: Transposed up minor third (A-C-E) with 1-second rest
  - Deepening: Transposed down minor third (D-F-A) with 3-second rest
  - Core: F-A-C at F2 with 4-second rest and slower decay
  - Fractionation: Brief upward scoop to F#-A-C (0.5s) then return

### Material Inventory - Harmonic Signature
- Home color: F minor (F, Ab, C) with suspended fourth (Bb)
- Contrast color: Bb major 7th (Bb, D, F, A) for brighter but dark color
- Return color: F minor with suspended fourth (F, Bb, C) and added fifth

### Material Inventory - Texture Family
- Continuous layer behavior: Low-mid drone (F2) with slow amplitude modulation (0.1 Hz)
- Event layer behavior: Soft glints (0.5s) at 1/4 of carrier phrase, filtered noise band
- Noise / air behavior: High-frequency air layer (10-12 kHz) that widens during deepening

### Material Inventory - Spatial Material
- Width behavior: Narrow (0-3m) → Medium (3-7m) → Wide (7-13m) → Medium-wide (13-20m) → Narrow (20-22m) → Medium (22-23.5m) → Narrow (23.5-24m)
- Motion behavior: Air layer drifts slowly left to right during deepening, returns center for core
- Depth behavior: Carrier at 30% depth (near center), harmonic bed at 50%, air layer at 70%

### Material Inventory - Modulation Identity
- Rate family: Slow breath-like modulation (0.1 Hz) with secondary shimmer (0.25 Hz)
- Change rule: Drone modulation slows from 0.12 to 0.08 Hz during deepening

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1
- Reference pitch-contour profile: reference_voice_pitch_contours_v1 (84.5% falling_settling)
- Carrier instrument / layer: Sarangi-like bowed string (GM program 52, "Warm Strings")
- Cadence behavior: Breath-length phrases (6-14s) with 1-3s inter-phrase rests
- Contour behavior: Primarily falling_settling with occasional rising_opening for fractionation
- Weighting behavior: Slight emphasis on second note (A in F-A-C)
- Return behavior: 3-note motif returns at end of deepening phrases
- Salience limit: Carrier 6dB above harmonic bed, 10dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Soft filter sweep (lowpass cutoff rising 10Hz over 2s)
- First pairing: At 1:45 (orientation), paired with harmonic bed widening
- Withholding plan: Absent for 2 minutes after first appearance (until deepening)
- Return / trigger plan: At 7:15 (deepening), 13:45 (core), and 20:15 (fractionation return)
- Salience policy: Anchor at 10dB below carrier, soft and low volume

## Development Grammar

- Establishment rule: Introduce carrier and harmonic bed in orientation with single motif
- Variation rule: Motif transposes up (induction) and down (deepening), rest expands
- Contrast rule: Contrast color appears during deepening for subtle shift without disruption
- Recurrence rule: 3-note motif returns in every stage, transformed by register and rest
- Foreground rule: Carrier always foreground (6dB above bed) in every section
- Withholding rule: Anchor withheld for 2 minutes after first appearance to maintain meaning
- Fractionation rule: Brief upward pitch scoop (0.5s) during fractionation, immediate return to deep state
- Trigger conditioning rule: Anchor paired with carrier's downward settling for comfort association
- Dissolution / cadence rule: Ends with orientation motif at clarified register, fades over 15s
- Anti-padding rule: No material repeated without transformation; each stage has new element

## Formal Architecture

### Formal Unit 1 - Orientation
- Name / role: Settling the field, establishing carrier and harmonic bed
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: Orientation / Settling
- Target depth: Baseline
- Material state: Carrier (F-A-C motif), harmonic bed (F minor)
- Development action: Establish
- Hypnotic function: Pace
- Energy / density posture: Low
- Harmonic / spectral posture: F minor (home color)
- Spatial posture: Narrow width, carrier near center
- Transition condition: After 3 minutes continuous presence
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction
- Name / role: Narrowing attention through carrier pacing
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: Induction / Narrowing
- Target depth: Light
- Material state: Carrier (A-C-E motif), harmonic bed (F minor with suspended fourth)
- Development action: Vary
- Hypnotic function: Lead
- Energy / density posture: Medium-low
- Harmonic / spectral posture: F minor with suspended fourth
- Spatial posture: Medium width, carrier near center
- Transition condition: After 4 minutes of induction pacing
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Ratifying the state with downward settling
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: Deepening
- Target depth: Medium
- Material state: Carrier (D-F-A motif), harmonic bed (Bb major 7th)
- Development action: Answer
- Hypnotic function: Ratify
- Energy / density posture: Medium
- Harmonic / spectral posture: Bb major 7th (contrast color)
- Spatial posture: Wide width, carrier near center
- Transition condition: After 6 minutes of deepening
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustaining deep state with continuous prosody
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: Core Absorptive Work
- Target depth: Deep
- Material state: Carrier (F-A-C at F2), harmonic bed (F minor with suspended fourth)
- Development action: Thicken
- Hypnotic function: Deepen
- Energy / density posture: Medium-high
- Harmonic / spectral posture: F minor with suspended fourth (return color)
- Spatial posture: Medium-wide width, carrier near center
- Transition condition: After 7 minutes of core work
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 5 - Fractionation Pocket
- Name / role: Brief lightening and return to deeper state
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: Fractionation Pocket
- Target depth: Brief lightening (medium) then deeper return
- Material state: Carrier (brief upward scoop to F#-A-C), harmonic bed (Bb major 7th)
- Development action: Fractionate
- Hypnotic function: Fractionate
- Energy / density posture: Medium (lightening) → medium-high (return)
- Harmonic / spectral posture: Bb major 7th (lightening) → F minor with suspended fourth (return)
- Spatial posture: Narrow width during lightening, medium-wide for return
- Transition condition: After 2 minutes of fractionation pocket
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 6 - Integration
- Name / role: Stabilizing the state with return to orientation
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: Integration / Stabilization
- Target depth: Light (stabilizing)
- Material state: Carrier (F-A-C at F3), harmonic bed (F minor with suspended fourth)
- Development action: Recur
- Hypnotic function: Stabilize
- Energy / density posture: Medium-low
- Harmonic / spectral posture: F minor with suspended fourth (return color)
- Spatial posture: Medium width, carrier near center
- Transition condition: After 1.5 minutes of integration
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 7 - Return
- Name / role: Gentle reorientation to baseline
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: Return / Reorientation
- Target depth: Baseline
- Material state: Carrier (F-A-C at F3, brighter phrase endings), harmonic bed (F minor)
- Development action: Dissolve
- Hypnotic function: Reorient
- Energy / density posture: Low
- Harmonic / spectral posture: F minor (home color)
- Spatial posture: Narrow width, carrier near center
- Transition condition: After 30 seconds of return
- Motion source commitment: foreground: voice_adjacent

## Orchestration Map

- Root / ground function: Low drone (F2) grounds the listener
- Harmonic color function: Harmonic bed provides pitch identity
- Motion function: Carrier's phrase contour and modulation rates create time perception
- Foreground / event function: Carrier is foreground; events are rare and subtle
- Voice-adjacent induction function: Sarangi carrier carries hypnotist's cadence
- Anchor / trigger function: Soft filter sweep as anchor, paired with carrier phrases
- Air / bloom function: High-frequency air layer creates openness and shimmer
- Spatial / depth function: Width, motion, and placement create 3D field
- Silence / restraint function: Withheld anchor (2 minutes) and sparse events

## Temporal Behavior

- Micro-event rate: One soft glint every 20-35 seconds
- Scene-change rate: Stage changes at unit boundaries (every 3-7 minutes)
- Long-wave change: Harmonic color shifts from home to contrast and back
- Maximum stasis tolerance: 45 seconds without micro-event or harmonic shift
- Repetition policy: Motif returns transformed, not identically
- Fractionation timing: Brief upward scoop (0.5s) at 20:00, return to deep state at 20:05
- Anchor recurrence spacing: At 1:45, 7:15, 13:45, and 20:15

## Spatial Composition

- Stereo requirement: Stereo
- Spatial arc: Narrow → Medium → Wide → Medium-wide → Narrow → Medium → Narrow
- Layer placement principles: Ground drone centered; harmonic bed wide stable; carrier near center; air layer at edges
- Motion limits: No fast autopan; motion slow (20-120s)
- Verification obligation: Verify not mono, width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No harmonic color change for >2 minutes
  - Revision target: Add subtle harmonic shift at 5 minutes

- Failure mode: too generic
  - Evidence: Motif not recognizable across stages
  - Revision target: Strengthen motif transformations in each stage

- Failure mode: too busy
  - Evidence: More than one event per 15 seconds
  - Revision target: Reduce micro-events to one every 20-35 seconds

- Failure mode: weak recurrence
  - Evidence: Motif absent in core or integration stages
  - Revision target: Ensure motif returns transformed in every stage

- Failure mode: weak ending
  - Evidence: Fade-out without orientation motif return
  - Revision target: Add orientation motif at 23:45 with brighter phrase ending

- Failure mode: weak spatial composition
  - Evidence: Static width throughout piece
  - Revision target: Implement full spatial arc as described

- Failure mode: weak hypnotic architecture
  - Evidence: Fractionation pocket resets to new state
  - Revision target: Ensure lightening brief (0.5s) and return to deep state

- Failure mode: weak fractionation
  - Evidence: Lightening >10 seconds or abrupt return
  - Revision target: Limit lightening to 0.5s with slow filter automation

- Failure mode: weak anchors / triggers
  - Evidence: Anchor too obvious or random
  - Revision target: Soften anchor to 10dB below carrier

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier <6dB above bed or phrases <5s
  - Revision target: Adjust gain to ensure carrier >6dB above bed

## Sketch Handoff

- Required sketch style family: Hypnotic Stage Architecture Journey
- Required formal units: 7 stages (orientation, induction, deepening, core, fractionation, integration, return)
- Required material to preserve: Primary motif (F-A-C), harmonic signature (home/contrast/return colors)
- Required hypnotic architecture to preserve: Stage map and depth trajectory
- Required voice-adjacent induction behavior to preserve: Sarangi carrier, 6dB above bed
- Required development grammar to preserve: Establishment, Variation, Contrast, Recurrence, Withholding
- Required revision criteria to preserve: 10 failure modes and targets
- Explicit exclusions: No percussion, no abrupt changes, no foreground melodies