# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-15-001
WORK_MANIFEST: wmf-neuro-ambient-composition-15-001
Status: ready

## Artistic Reading

- User image / premise: A slow, breath-like descent into deep internal absorption without verbal guidance
- Musical problem: Creating a nonverbal guide that feels weighted and permissive while avoiding generic ambient tropes
- Listener stance: Inward deep listening, allowing the piece to guide without demanding attention
- What this is not: Dramatic, pop-hook-based, cinematic tension, or vocal suggestion
- Style-family fit: Sequenced Ambient (Carbon Based Lifeforms/Solar Fields) with hypnotic architecture as core mechanism

## Chosen Musical Premise

- Premise sentence: A breath-like descent into absorption through harmonic shifts, spatial widening, and a voice-adjacent carrier that settles without becoming foreground
- Primary dramatic motion: Establish -> deepen -> dissolve (with fractionation as return to deeper absorption)
- Sound-world metaphor: A slow descent through layers of water, where each layer is a stage of absorption
- Musical consequence: Harmony shifts downward (F2 → G2), texture thickens then thins, register lowers, space widens then contracts
- Duration rationale: 24 minutes allows for the slow, multi-stage progression of absorption (each stage's duration proportional to depth work)

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for deep internal absorption
- Stage map:
  - Orientation / Settling: 3:00, baseline_to_light, settle_and_establish_safety
  - Induction / Narrowing: 4:00, light_to_medium, pace_and_lead
  - Deepening: 6:00, medium_to_deep, reduce_orienting_load
  - Core Absorptive Work: 7:00, deep, sustain_absorption
  - Fractionation Pocket: 2:00, deep_to_light_medium_to_deep, briefly_lighten_then_return_deeper
  - Integration: 1:30, medium_to_light, stabilize
  - Return / Reorientation: 0:30, light_to_baseline, reorient
- Depth trajectory: Baseline (orientation) → Light (induction) → Medium (deepening) → Deep (core work) → Brief lightening (fractionation) → Deeper return (fractionation) → Medium (integration) → Light (return)
- Pacing-and-leading plan: First pace broad listening with slow ground and sparse carrier, then lengthen rests (induction), lower register (deepening), darken brightness (core work), widen space (integration)
- Ratification plan: Voice-adjacent carrier returns transformed at end of each stage to ratify depth achieved
- Suggestion-compatible windows: None (music-only), but carrier pauses create natural nonverbal guidance windows
- Reset avoidance rule: No abrupt cuts, hard transients, alerting cues, or sudden brightness spikes

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Two-note minor third interval cell (F2-A2) with falling contour, followed by brief silence
- First form: Single instance at 0:45 (after carrier establishes), soft attack, long release
- Later forms: Transposed down minor third in deepening (G2-B2); simplified to single note at core work bottom register

### Material Inventory - Harmonic Signature
- Home color: F2 (87.31 Hz) with interval stack [0, 4, 7] (root, major third, perfect fifth)
- Contrast color: G2 (103.83 Hz) with interval stack [0, 3, 7] (root, minor third, perfect fifth)
- Return color: F2 with interval stack [0, 4] (root and major third)

### Material Inventory - Texture Family
- Continuous layer behavior: Low-mid drone with slow filter automation (0.1 Hz LFO) creating breathing effect
- Event layer behavior: Sparse soft glints (1-2 per 60 seconds), brief and non-intrusive
- Noise / air behavior: High-frequency air layer that subtly brightens during fractionation then recedes

### Material Inventory - Spatial Material
- Width behavior: Narrow (orientation) → Medium (induction) → Wide (deepening/core work) → Medium-wide (fractionation) → Narrow (integration/return)
- Motion behavior: Slow drifting motion in air layer; no fast autopan
- Depth behavior: Ground near, harmonic body medium, carrier and air layers recede to rear

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.1 Hz) with secondary shimmer at 0.25 Hz
- Change rule: Breath rate slows by 10% every 3 minutes; shimmer depth increases during deepening

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1
- Carrier instrument / layer: Filtered sine wave at 87.31 Hz with formant-like synth layer
- Cadence behavior: Breath-length phrases (1.5-3 seconds) with permissive pauses (0.75s)
- Contour behavior: Downward settling in deepening/core work; brief rising during fractionation lightening
- Weighting behavior: Slight amplitude increase at phrase end (without hard attack)
- Return behavior: Carrier returns transformed in each stage with more subdued contour
- Salience limit: Always below harmonic body layer volume; never becomes lead

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Tiny spectral unsealing (10Hz shift in air layer) at carrier phrase end
- First pairing: Appears after first downward settling carrier phrase (at 1:30)
- Withholding plan: Absent during orientation and core work
- Return / trigger plan: Returns at deepening (5:00) and fractionation return (21:30)
- Salience policy: Audible enough to learn (-15dB), not salient enough for monitoring

## Development Grammar

- Establishment rule: Introduce ground drone and carrier in orientation
- Variation rule: Harmonic signature shifts from home to contrast color during deepening
- Contrast rule: Core work uses darker harmonic color (contrast) and lower register than induction
- Recurrence rule: Primary motif returns transformed at end of each stage (register shift)
- Foreground rule: Event layer glints become slightly more legible during fractionation
- Withholding rule: Anchor withheld for 3 minutes after first pairing to build meaning
- Fractionation rule: Air layer brightens (100Hz shift) for 30 seconds, then returns to darker color
- Trigger conditioning rule: Anchor paired only with downward settling phrases for deepening association
- Dissolution / cadence rule: Ends by returning opening motif in simplified form (F2, [0,4]) with spatial contraction
- Anti-padding rule: No event repeated without transformation; all events spaced ≥15 seconds apart

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establishing the field and carrier
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline_to_light
- Material state: Ground drone, sparse carrier phrases, air layer
- Development action: Establish
- Hypnotic function: settle_and_establish_safety
- Energy / density posture: Low (sparse carrier, no events)
- Harmonic / spectral posture: Home color (F2), 1700 Hz
- Spatial posture: Narrow, centered ground; air at edges
- Transition condition: Carrier established and anchor first paired (0:45)

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pacing and leading attention inward
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light_to_medium
- Material state: Carrier more frequent, primary motif introduced
- Development action: Vary (motif appears)
- Hypnotic function: pace_and_lead
- Energy / density posture: Moderate (carrier phrases every 2 minutes)
- Harmonic / spectral posture: Home color, slightly darker (1600 Hz)
- Spatial posture: Medium width; carrier near center
- Transition condition: Attention narrowed and space prepared to widen (6:45)

### Formal Unit 3 - Deepening
- Name / role: Reducing orienting load, deepening absorption
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium_to_deep
- Material state: Contrast color harmonic, anchor introduced (7:30), motif returns transposed
- Development action: Answer, thicken (harmonic body)
- Hypnotic function: reduce_orienting_load
- Energy / density posture: Medium (anchor every 3 minutes)
- Harmonic / spectral posture: Contrast color, 1500 Hz
- Spatial posture: Wide; carrier and air spread out
- Transition condition: Depth stable and core can withhold carrier (12:45)

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustaining deep absorption
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core_absorptive_work
- Target depth: deep
- Material state: Carrier mostly absent, anchor withheld (until 19:00), harmonic body dominant
- Development action: Withhold, dissolve (carrier)
- Hypnotic function: sustain_absorption
- Energy / density posture: Low (only air layer events)
- Harmonic / spectral posture: Contrast color, 1400 Hz
- Spatial posture: Wide but stable; air at edges
- Transition condition: Field thinned and fractionation pre-echo seeded (19:45)

### Formal Unit 5 - Fractionation Pocket
- Name / role: Brief lightening then deeper return
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep_to_light_medium_to_deep
- Material state: Air layer brightens (100Hz shift), carrier returns with rising contour
- Development action: Lighten, return deeper
- Hypnotic function: briefly_lighten_then_return_deeper
- Energy / density posture: Moderate (carrier and air active)
- Harmonic / spectral posture: Home color, 2000 Hz (lightening), contrast at 21:30
- Spatial posture: Medium-wide → Wide (re-deepening)
- Transition condition: Brightness closed and deeper return established (21:50)

### Formal Unit 6 - Integration
- Name / role: Stabilizing the absorption state
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium_to_light
- Material state: Carrier returns with familiar contour, anchor returns (22:45)
- Development action: Recur (motif in return color)
- Hypnotic function: stabilize
- Energy / density posture: Low (carrier and anchor)
- Harmonic / spectral posture: Home color, 1800 Hz
- Spatial posture: Contracting from wide to medium (carrier near center)
- Transition condition: Fractionation redeepened and carrier returns (23:15)

### Formal Unit 7 - Return / Reorientation
- Name / role: Gentle reorientation to baseline
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light_to_baseline
- Material state: Opening motif returns simplified, spatial width narrows
- Development action: Dissolve (to opening motif)
- Hypnotic function: reorient
- Energy / density posture: Very low (carrier and air)
- Harmonic / spectral posture: Home color, 1700 Hz
- Spatial posture: Narrow (centered)
- Transition condition: Integration cadence prepared

## Orchestration Map

- Root / ground function: Ground drone (low-mid anchor)
- Harmonic color function: Harmonic body layer
- Motion function: Arpeggio/bass motion (16th-note sequence at 50 BPM, root F2)
- Foreground / event function: Event layer (glints) and air layer
- Voice-adjacent induction function: Carrier layer (filtered sine with formant)
- Anchor / trigger function: Air layer spectral unsealing
- Air / bloom function: High-frequency air layer (brightening during fractionation)
- Spatial / depth function: Layer placement and width automation
- Silence / restraint function: Withholding carrier during core work

## Temporal Behavior

- Micro-event rate: 1-2 events per minute (glints, anchor)
- Scene-change rate: Every 3 minutes (at stage transitions)
- Long-wave change: Harmonic color shifts at stages, spatial arc
- Maximum stasis tolerance: 45 seconds (no event or change)
- Repetition policy: Events transformed on recurrence
- Fractionation timing: 20:00-20:30 (lightening), 20:30-21:30 (return deeper)
- Anchor recurrence spacing: Every 3 minutes during deepening and integration

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow → medium → wide → medium-wide → narrow (as per stages)
- Layer placement principles: ground centered, harmonic body wide stable, carrier near center low motion, air at edges and rear
- Motion limits: no fast autopan; slow drift (over 20-120 seconds)
- Verification obligation: verify not mono, width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No perceptible change for >45 seconds in harmonic color or density
  - Revision target: Add micro-event (e.g., air layer brightening) or harmonic shift

- Failure mode: too generic
  - Evidence: Could be any ambient piece; no specific motif or anchor identity
  - Revision target: Add primary motif and anchor as defined

- Failure mode: too busy
  - Evidence: >2 events per minute, carrier becomes lead
  - Revision target: Reduce event layer to one glint every 60 seconds; lower carrier volume

- Failure mode: weak recurrence
  - Evidence: Primary motif appears once and vanishes
  - Revision target: Return motif transformed at each stage

- Failure mode: weak ending
  - Evidence: Piece fades out without returning opening motif
  - Revision target: Compose dissolution with return of primary motif in simplified form

- Failure mode: weak spatial composition
  - Evidence: Output mono or width doesn't change
  - Revision target: Add stereo construction for each layer as per spatial arc

- Failure mode: weak hypnotic architecture
  - Evidence: Stages don't progress in depth; no fractionation
  - Revision target: Add stage-specific harmonic, density, and spatial controls

- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset
  - Revision target: Ensure lightening brief (30s) and return uses darker color

- Failure mode: weak anchors / triggers
  - Evidence: Anchor too obvious or random
  - Revision target: Make anchor tiny spectral shift at carrier phrase end

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier too bright or mechanical
  - Revision target: Use reference cadence profile and lower salience

## Sketch Handoff

- Required sketch style family: Sequenced Ambient
- Required formal units: 7 stages (Orientation, Induction, Deepening, Core, Fractionation, Integration, Return)
- Required material to preserve: Primary motif (two-note minor third falling), harmonic signature (home/contrast/return colors), texture behavior, spatial material
- Required hypnotic architecture to preserve: Stage map and depth trajectory
- Required voice-adjacent induction behavior: Carrier layer with reference cadence profile, downward settling
- Required development grammar to preserve: Recurrence rule (transformed returns), fractionation rule, anti-padding
- Required revision criteria to preserve: The full list above for failure modes
- Explicit exclusions: No percussion, no transient attacks; no vocal samples