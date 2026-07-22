# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-045-001
WORK_MANIFEST: wmf-neuro-ambient-composition-045-001
Status: ready

## Artistic Reading

- User image / premise: Continuous breath-phrased instrumental lead guiding self-hypnotic journey through seven stages
- Musical problem: Sustain hypnotic depth progression without attention-demanding elements or abrupt transitions
- Listener stance: Relaxed absorption with continuous carrier as focal point
- What this is not: Pop hook, dramatic tension, or vocal imitation
- Style-family fit: Neuro-ambient with voice-adjacent induction and layered trance depth

## Chosen Musical Premise

- Premise sentence: A continuous breath-phrased carrier in the low-mid register guides listeners through seven hypnotic stages via evolving harmonic bed and subtle nonverbal anchors
- Primary dramatic motion: Establish → deepen → fractionate → stabilize → return
- Sound-world metaphor: A slow river with consistent current (carrier) through changing landscapes of water color and depth
- Musical consequence: Harmonic bed shifts via F-centered open voicings; spatial width evolves with depth; carrier maintains 6dB above bed
- Duration rationale: 24 minutes allows proportional stage durations (deepening requires more time than orientation) without rushing depth progression

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for focused immersion
- Stage map:
  - Orientation / Settling: 0:00-3:00, baseline-to-light depth, function: establish safety
  - Induction / Narrowing: 3:00-7:00, light-to-medium depth, function: narrow attention
  - Deepening: 7:00-13:00, medium-to-deep depth, function: reduce orienting load
  - Core Absorptive Work: 13:00-20:00, deep depth, function: sustain absorption
  - Fractionation Pocket: 20:00-22:00, deep-to-light-medium-to-deep depth, function: brief lightening then deeper return
  - Integration / Stabilization: 22:00-23:30, medium-to-light depth, function: stabilize
  - Return / Reorientation: 23:30-24:00, light-to-baseline depth, function: dissolve
- Depth trajectory: Baseline (0:00) → light (3:00) → medium (7:00) → deep (13:00) → light-medium (21:00) → medium (23:30) → baseline (24:00)
- Pacing-and-leading plan: Carrier establishes breath-length cadence in orientation; induction uses regular phrasing to narrow attention
- Ratification plan: Carrier's falling-settling contour (84.5%) and low-mid warmth bloom anchor at 3:00, 7:00, 13:00
- Suggestion-compatible windows: Carrier phrase endings at 3:00, 7:00, 13:00
- Reset avoidance rule: No abrupt harmonic shifts or brightness spikes; all transitions use 12-second crossfades

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Three-note falling-settling cell (A4-G#4-F#4) with 0.5s gaps
- First form: Introduced at 0:45 in orientation with soft attack and breath release
- Later forms: Transposed down minor third at 7:00; widened stereo at 13:00; filtered lower register at 22:00

### Material Inventory - Harmonic Signature
- Home color: F2 root with interval stack [0, 3, 10] (F2-A#2-D3)
- Contrast color: G#2 root [0, 3, 10] (G#2-B#2-E3) for induction; A2 root [0, 3, 10] (A2-C#3-F3) for deepening
- Return color: F2 root [0, 10] (F2-D3) for return

### Material Inventory - Texture Family
- Continuous layer behavior: Warm ground drone (attack 8s, release 60s) with harmonic bed as wide pad
- Event layer behavior: Submerged motif fragments (every 45s) with soft filtering
- Noise / air behavior: Air bloom layer (attack 60s, release 90s) opening at deepening start

### Material Inventory - Spatial Material
- Width behavior: Narrow (0:00) → medium (3:00) → wide (13:00) → medium-wide (20:00) → narrow (24:00)
- Motion behavior: Slow non-alerting drift; no fast autopan
- Depth behavior: Near (0:00) → medium (7:00) → deep (13:00) → near (24:00)

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) for ground/harmonic bed; asymmetric shimmer in air layer
- Change rule: Modulation depth decreases 10% every 5 minutes

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1` (6-14s phrases)
- Reference pitch-contour profile: `reference_voice_pitch_contours_v1` (84.5% falling-settling)
- Carrier instrument / layer: Warm low-mid reed-like MIDI (GM 13) at 240-300Hz
- Cadence behavior: Breath-length phrases with permissive 1-3s rests
- Contour behavior: 84.5% falling-settling; occasional arches (3.6%)
- Weighting behavior: Slight register warmth at phrase endings
- Return behavior: Motif cell returns transformed at depth thresholds
- Salience limit: Carrier ≥6dB above harmonic bed, ≥10dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Low-mid warmth bloom (0.5s, 120Hz center)
- First pairing: At 3:00 with carrier's settling phrase
- Withholding plan: Absent during induction (3:00-7:00)
- Return / trigger plan: At 7:00 (deepening), 13:00 (core stability), 22:00 (integration)
- Salience policy: -15dB relative to carrier; audible but not attention-demanding

## Development Grammar

- Establishment rule: Introduce carrier and home color in orientation
- Variation rule: Transform motif cell (transposition, widening) at stage thresholds
- Contrast rule: Shift harmonic root and density; avoid bright contrasts
- Recurrence rule: Motif cell returns every 10 minutes transformed; anchor at depth thresholds
- Foreground rule: Carrier always foreground (≥6dB above bed)
- Withholding rule: Anchor withheld during induction; fractionation lightening brief
- Fractionation rule: Lighten via carrier's brighter register (20:00-21:00), return deeper via darker register (21:00-22:00)
- Trigger conditioning rule: Anchor paired with carrier settling at 3:00
- Dissolution / cadence rule: Final fade uses longest carrier phrase (14s) with air layer closing
- Anti-padding rule: No looped events; all changes tied to development

## Formal Architecture

### Formal Unit 1 - Orientation / Settling (0:00-3:00)
- Name / role: Establish world and carrier pairing
- Approximate duration range: 0:00-3:00 (180s)
- Hypnotic stage role: Orientation
- Target depth: Baseline-to-light
- Material state: Ground, harmonic bed (F2), carrier seed
- Development action: Establish
- Hypnotic function: Settle and establish safety
- Energy / density posture: Low, sparse
- Harmonic / spectral posture: Home color (F2), dark low-mid
- Spatial posture: Narrow, centered
- Transition condition: Carrier established and anchor paired (0:45)
- Motion source commitment: `foreground: voice_adjacent`

### Formal Unit 2 - Induction / Narrowing (3:00-7:00)
- Name / role: Pace attention inward
- Approximate duration range: 3:00-7:00 (240s)
- Hypnotic stage role: Induction
- Target depth: Light-to-medium
- Material state: Ground, harmonic bed (G#2), carrier variants
- Development action: Vary and narrow
- Hypnotic function: Pace attention inward
- Energy / density posture: Medium, increasing
- Harmonic / spectral posture: Contrast color (G#2), slightly brighter
- Spatial posture: Medium, stable width
- Transition condition: Attention narrowed (7:00)
- Motion source commitment: `foreground: voice_adjacent`, `parameter_group_sequence`

### Formal Unit 3 - Deepening (7:00-13:00)
- Name / role: Widen space and stabilize root memory
- Approximate duration range: 7:00-13:00 (360s)
- Hypnotic stage role: Deepening
- Target depth: Medium-to-deep
- Material state: Ground, harmonic bed (A2), air, anchor softening
- Development action: Thicken and deepen
- Hypnotic function: Reduce orienting load
- Energy / density posture: High, stable
- Harmonic / spectral posture: Contrast color (A2), darkened
- Spatial posture: Wide, slow drift
- Transition condition: Depth stable (13:00)
- Motion source commitment: `foreground: voice_adjacent`, `event_schedule` (anchor at 7:00,13:00)

### Formal Unit 4 - Core Absorptive Work (13:00-20:00)
- Name / role: Sustain absorption with micro-developments
- Approximate duration range: 13:00-20:00 (420s)
- Hypnotic stage role: Core
- Target depth: Deep
- Material state: Ground, harmonic bed (B2), rare carrier, motif fragment
- Development action: Sustain and thin
- Hypnotic function: Maintain absorption
- Energy / density posture: Medium, sparse events
- Harmonic / spectral posture: Home color (B2), darkened
- Spatial posture: Medium-wide, stable
- Transition condition: Field thinned (20:00)
- Motion source commitment: `foreground: voice_adjacent`, `event_schedule` (motif at 16:00,19:00)

### Formal Unit 5 - Fractionation Pocket (20:00-22:00)
- Name / role: Briefly lighten then return deeper
- Approximate duration range: 20:00-22:00 (120s)
- Hypnotic stage role: Fractionation
- Target depth: Deep-to-light-medium-to-deep
- Material state: Ground, harmonic bed (C3), clearer carrier
- Development action: Lighten and return deeper
- Hypnotic function: Brief lightening then deeper return
- Energy / density posture: Medium (lightening), high (return)
- Harmonic / spectral posture: Brighter (C3) → darkened
- Spatial posture: Medium-wide → narrow on return
- Transition condition: Brightness closed (22:00)
- Motion source commitment: `foreground: voice_adjacent`, `event_schedule` (anchor at 21:00)

### Formal Unit 6 - Integration / Stabilization (22:00-23:30)
- Name / role: Return familiar material in calmer form
- Approximate duration range: 22:00-23:30 (90s)
- Hypnotic stage role: Integration
- Target depth: Medium-to-light
- Material state: Ground, harmonic bed (D#2), clearer carrier
- Development action: Recur and clarify
- Hypnotic function: Stabilize
- Energy / density posture: Low, sparse
- Harmonic / spectral posture: Home color (D#2), warmer
- Spatial posture: Narrow, centered
- Transition condition: Fractionation redeepened (22:00)
- Motion source commitment: `foreground: voice_adjacent`

### Formal Unit 7 - Return / Reorientation (23:30-24:00)
- Name / role: Dissolve and cadence
- Approximate duration range: 23:30-24:00 (30s)
- Hypnotic stage role: Return
- Target depth: Light-to-baseline
- Material state: Ground residue, air fade
- Development action: Dissolve
- Hypnotic function: Reorient
- Energy / density posture: Very low, fading
- Harmonic / spectral posture: Home color (F2) simplified
- Spatial posture: Narrow, fading to center
- Transition condition: Integration cadence prepared (23:30)
- Motion source commitment: `foreground: voice_adjacent`

## Orchestration Map

- Root / ground function: Warm slow-beating drone
- Harmonic color function: F-centered open voicings with slow filter motion
- Motion function: Carrier (breath phrases) and air bloom
- Foreground / event function: Submerged motif fragments
- Voice-adjacent induction function: Carrier layer (240-300Hz reed)
- Anchor / trigger function: Low-mid warmth bloom
- Air / bloom function: Filtered high air at edges (slow opening/closing)
- Spatial / depth function: Stereo width evolution
- Silence / restraint function: Anchor withheld during induction

## Temporal Behavior

- Micro-event rate: One motif fragment every 45 seconds
- Scene-change rate: Every stage transition (max 7 minutes)
- Long-wave change: Harmonic root shifts every 3-6 minutes
- Maximum stasis tolerance: 45 seconds without change (core)
- Repetition policy: Motif cell returns transformed; no identical loops
- Fractionation timing: Lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: At 3:00 (first pairing), 7:00, 13:00, 22:00

## Spatial Composition

- Stereo requirement: Stereo
- Spatial arc: Narrow → medium → wide → medium-wide → narrow
- Layer placement principles: Ground centered, harmonic bed wide stable, carrier near-center low motion, air at edges
- Motion limits: No fast autopan; slow drift only (max 20s sweep)
- Verification obligation: Verify not mono, width arc audible, motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No material change >45s in core (13:00-20:00)
  - Revision target: Add motif fragment at 16:00 and 19:00
- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset (bright, abrupt)
  - Revision target: Reduce brightness to 10% above bed; ensure return uses darker register
- Failure mode: weak anchors
  - Evidence: Anchor at 7:00 too obvious (bright, sharp)
  - Revision target: Soften to -15dB relative to carrier; pair with settling phrase
- Failure mode: weak ending
  - Evidence: Fades out without carrier's final phrase
  - Revision target: Extend carrier's last phrase to 14s with air layer fade

## Sketch Handoff

- Required sketch style family: Neuro-ambient voice-adjacent induction
- Required formal units: 7 stages as defined in Formal Architecture
- Required material to preserve: Primary motif, harmonic signature (home/contrast/return), texture behavior
- Required hypnotic architecture to preserve: Stage map, depth trajectory, fractionation timing
- Required voice-adjacent induction behavior to preserve: Carrier pitch [240,300]Hz, 6-14s phrases, falling-settling contour
- Required development grammar to preserve: Establishment, variation, contrast rules
- Required revision criteria to preserve: Static, fractionation, anchors, ending
- Explicit exclusions: No percussion, no bright bells, no abrupt transitions