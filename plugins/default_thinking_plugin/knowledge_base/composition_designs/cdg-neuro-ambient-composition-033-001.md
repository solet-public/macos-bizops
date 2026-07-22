# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-033-001
WORK_MANIFEST: wmf-neuro-ambient-composition-033-001
Status: ready

## Artistic Reading

- User image / premise: A continuous breath-length prosodic lead voice (via soft cello) guiding self-hypnosis journey from orientation to return
- Musical problem: Create 24-minute hypnotic architecture with continuous carrier that guides without demanding attention
- Listener stance: Following the continuous lead through a field of calm absorption
- What this is not: Pop song with vocal, EDM track, static drone, or abrupt transitions
- Style-family fit: Hypnotic Stage Architecture with Prosodic Lead Carrier (foreground_vocal_led_prosody)

## Chosen Musical Premise

- Premise sentence: A continuous soft cello carrier guides the listener through a 24-minute hypnotic journey using breath-length phrases and harmonic evolution to deepen absorption without alerting the listener
- Primary dramatic motion: Establish → Pace and lead → Deepen → Sustain → Lighten and return deeper → Stabilize → Dissolve
- Sound-world metaphor: A slow, warm river flowing through a forest at dusk; the cello is the current that carries the listener along
- Musical consequence: River metaphor changes harmony (root shifts to support depth), texture (subtle eddies as micro-events), and space (width widens to suggest forest openness)
- Duration rationale: 24 minutes earned through seven-stage architecture requiring time for depth establishment (deepening needs 6 minutes to stabilize field)

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for focused absorption
- Stage map:
  - Orientation (0:00-3:00): baseline to light, settle and establish safety
  - Induction (3:00-7:00): light to medium, pace and lead attention inward
  - Deepening (7:00-13:00): medium to deep, reduce orienting load
  - Core work (13:00-20:00): deep, sustain absorption
  - Fractionation (20:00-22:00): deep to light-medium to deep, briefly lighten then return deeper
  - Integration (22:00-23:30): medium to light, stabilize
  - Return (23:30-24:00): light to baseline, reorient
- Depth trajectory: Baseline → light (orientation) → medium (induction) → deep (deepening/core work) → brief light-medium (fractionation) → medium-light (integration) → baseline (return). Depth measured by harmonic density, carrier register lowering, and spatial widening.
- Pacing-and-leading plan: Cello's phrase cadence (6-14s) and contour (84.5% falling_settling) pace attention inward; phrases become more settled and longer per stage.
- Ratification plan: Anchor cue returns at 22:00 to ratify integration state, confirming safety.
- Suggestion-compatible windows: Cello's pauses (1-3s) and anchor cue at 22:00 for spoken guidance without masking.
- Reset avoidance rule: No abrupt changes, no hard transients, no sudden brightness spikes.

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: 3-note interval cell (minor third + minor second) descending and settling
- First form: Orientation - single phrase F2-A2-G2 (1.5s duration)
- Later forms: Deepening - transposed down minor third (D2-F2-E2); Core work - fragment A2-G2 at 10s intervals

### Material Inventory - Harmonic Signature
- Home color: F2 root with interval stack [0, 4, 9] (F-A-C#) - warm minor seventh
- Contrast color: G2 root with [0, 3, 10] (G-B-D#) - brighter minor ninth for induction
- Return color: F2 root with [0, 4, 9] missing third (F-C#) - simplified for return

### Material Inventory - Texture Family
- Continuous layer: Ground drone (F2) with 8-45s attack, 20-60s release
- Event layer: Micro-events (high-frequency shimmer) every 15-45s, <2s duration
- Noise / air: Air bloom layer (10-60s attack, 20-90s release) opening at cello phrase ends

### Material Inventory - Spatial Material
- Width behavior: Narrow (10%) → medium (3-7min) → wide (7-20min) → medium-wide (20-22min) → narrow (22-24min)
- Motion behavior: Slow drift (0.5-2Hz) in air layer, carrier near center
- Depth behavior: Ground centered/close; harmonic bed widens to 10-20m depth in core work

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13Hz) for ground drone, asymmetric shimmer in air layer
- Change rule: Modulation depth decreases 10% every 3 minutes for gradual calming

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1` (6-14s phrases, 1-3s rests)
- Reference pitch-contour profile: `reference_voice_pitch_contours_v1` (84.5% falling_settling)
- Carrier instrument: Soft cello with warm low-mid resonance
- Cadence behavior: Breath-length phrases (6-14s) with permissive pauses after settling
- Contour behavior: 84.5% falling_settling, occasional arches at fractionation
- Weighting behavior: Slight register warmth and harmonic reinforcement on settling points
- Return behavior: Same contour returns transformed (lower register, longer decay) at deepening/integration
- Salience limit: 6dB above harmonic bed, 10dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Low-frequency pulse (30Hz) filtered to inaudible until paired
- First pairing: At 3:00, coincides with cello settling to F2
- Withholding plan: Absent 3:00-13:00 (core work)
- Return / trigger plan: At 22:00, returns with slight amplitude increase to ratify stability
- Salience policy: Audible enough to learn (-25dB), not salient enough to monitor

## Development Grammar

- Establishment rule: Introduce cello carrier and ground drone in orientation
- Variation rule: Harmonic bed shifts root every stage, interval stack consistent [0,4,9]
- Contrast rule: Brightness and width increase during induction/deepening, decrease in integration
- Recurrence rule: Motif cell returns at 10s intervals in core work, transposed and thinned
- Foreground rule: Cello carrier always foreground (6dB above bed)
- Withholding rule: Anchor cue withheld 3:00-13:00
- Fractionation rule: Lightening (20:00-21:00) uses brighter filter and clearer cello; re-deepening (21:00-22:00) uses darker filter and wider field
- Trigger conditioning rule: Anchor paired with cello settling at 3:00, associated with safety
- Dissolution / cadence rule: Reduce event density and spatial width to baseline for ending
- Anti-padding rule: No loops or stretched events; every micro-event serves purpose

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish river current and forest
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: ground, harmonic bed (F2 [0,4,9]), carrier seed
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: low (sparse carrier, steady ground)
- Harmonic / spectral posture: F2 home color
- Spatial posture: narrow (10% width), centered carrier
- Transition condition: carrier established and anchor first paired at 3:00
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Guide attention inward through river flow
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: ground, harmonic bed (G2 [0,3,10]), carrier variants
- Development action: vary
- Hypnotic function: pace and lead attention inward
- Energy / density posture: medium (increased carrier presence)
- Harmonic / spectral posture: G2 contrast color
- Spatial posture: medium width, carrier near center
- Transition condition: attention narrowed and space prepared to widen
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen river field and stabilize depth
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: ground, harmonic bed (A2 [0,5,9]), air layer
- Development action: thicken
- Hypnotic function: reduce orienting load
- Energy / density posture: deep (wider field, lower register)
- Harmonic / spectral posture: A2 harmonic color
- Spatial posture: wide (40% width), carrier near center
- Transition condition: depth stable and core can withhold carrier
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain deep absorption through river flow
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: ground, harmonic bed (B2 [0,6,11]), rare carrier
- Development action: sustain
- Hypnotic function: sustain absorption
- Energy / density posture: deep (sparse events, stable field)
- Harmonic / spectral posture: B2 harmonic color
- Spatial posture: wide (40% width), carrier near center
- Transition condition: field thinned and fractionation pre-echo seeded
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten river surface then return deeper
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep to light-medium to deep
- Material state: ground, harmonic bed (C3 [0,4,7]), brief brightness
- Development action: answer
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: light-medium (brighter filter, clearer carrier)
- Harmonic / spectral posture: C3 contrast color
- Spatial posture: medium-wide (30% width), carrier near center
- Transition condition: brightness closed and deeper return established
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 6 - Integration / Stabilization
- Name / role: Return to forest calm with stabilized river
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: ground, harmonic bed (D3 [0,5,9]), clearer carrier
- Development action: dissolve
- Hypnotic function: stabilize
- Energy / density posture: light (reduced events, calmer field)
- Harmonic / spectral posture: D3 harmonic color
- Spatial posture: medium (20% width), carrier near center
- Transition condition: final carrier return prepares cadence
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve river current into forest stillness
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: ground residue, air fade
- Development action: dissolve
- Hypnotic function: reorient
- Energy / density posture: baseline (minimal events)
- Harmonic / spectral posture: F2 return color
- Spatial posture: narrow (10% width), air in edges
- Transition condition: final public delivery tail complete
- Motion source commitment: foreground: voice_adjacent

## Orchestration Map

- Root / ground function: Ground drone (F2) provides stability
- Harmonic color function: Harmonic bed defines pitch world through interval stacks
- Motion function: Carrier's phrase cadence and air layer modulation create time perception
- Foreground / event function: Carrier is foreground; micro-events are rare events
- Voice-adjacent induction function: Soft cello carrier with prosodic delivery
- Anchor / trigger function: Low-frequency pulse (30Hz) for anchoring safety
- Air / bloom function: Creates openness and shimmer in the field
- Spatial / depth function: Width arc (narrow to wide) creates spatial dimension
- Silence / restraint function: Anchor withheld 3:00-13:00

## Temporal Behavior

- Micro-event rate: One shimmer event every 30 seconds
- Scene-change rate: Stage transitions at boundaries (every 3-7 minutes)
- Long-wave change: Harmonic root shifts every 3 minutes
- Maximum stasis tolerance: 45 seconds without micro-event or harmonic shift
- Repetition policy: Motif cell repeats but transforms (transposed, thinned)
- Fractionation timing: Lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: First at 3:00, return at 22:00

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow (0-3min) → medium (3-7min) → wide (7-20min) → medium-wide (20-22min) → narrow (22-24min)
- Layer placement principles: Ground centered, harmonic bed wide stable, carrier near center low motion, air in edges and rear
- Motion limits: No fast autopan; slow drift (0.5-2Hz) in air layer
- Verification obligation: Verify not mono, width arc audible

## Revision Criteria

- Failure mode: too static
  - Evidence: no micro-events for >45s, harmonic color unchanged
  - Revision target: add shimmer event every 30s and shift root at 15min

- Failure mode: weak fractionation
  - Evidence: lightening feels like reset, re-deepening not deeper
  - Revision target: reduce fractionation salience, strengthen darker re-deepening

- Failure mode: weak anchors
  - Evidence: anchor cue absent at 22:00, or too obvious
  - Revision target: soften anchor onset and move return to integration threshold

- Failure mode: weak voice-adjacent induction
  - Evidence: carrier not continuous, or phrases mechanical
  - Revision target: ensure cello present in all sections with empirical contour

## Sketch Handoff

- Required sketch style family: Hypnotic Stage Architecture with Prosodic Lead Carrier
- Required formal units: 7 (as defined in Formal Architecture)
- Required material to preserve: motif cell, harmonic signature colors, texture behavior
- Required hypnotic architecture to preserve: stage map, depth trajectory, fractionation timing
- Required voice-adjacent induction behavior to preserve: carrier instrument, cadence profile, salience ceiling
- Required development grammar to preserve: establishment, variation, recurrence rules
- Required revision criteria to preserve: all failure modes and targets
- Explicit exclusions: no percussion, no abrupt changes, no foreground melody