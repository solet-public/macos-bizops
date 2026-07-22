# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-026-001
WORK_MANIFEST: wmf-neuro-ambient-composition-026-001
Status: ready

## Artistic Reading

- User image / premise: A 24-minute music-only self-hypnosis journey through layered arpeggios, moving bass roots, and a breath-like voice-adjacent carrier.
- Musical problem: To create continuous hypnotic architecture without percussion, transient attacks, or pop-style hooks while maintaining spectral depth.
- Listener stance: Inward deep listening with attention shifting from broad environmental to internal absorptive, then gently reoriented.
- What this is not: Dramatic, sad narrative, triumphant, pop-hook-based, cinematic tension.
- Style-family fit: Sequenced ambient (Carbon Based Lifeforms, Solar Fields) with spectral color and process-and-phasing.

## Chosen Musical Premise

- Premise sentence: A continuous field of delay-processed arpeggios and a sarangi-like voice-adjacent carrier guides the listener through seven hypnotic stages, with harmonic depth and spatial expansion evolving from contained to expansive then gently contracting.
- Primary dramatic motion: Establish → deepen → fractionate (brief lightening) → return
- Sound-world metaphor: A slow, deep breath that expands and contracts with the listener's own rhythm.
- Musical consequence: The metaphor changes harmony (through interval stacks), texture (sparse to dense then sparse again), register (low-mid anchor with upward air bloom), and space (narrow to wide then narrow).
- Duration rationale: 24 minutes allows for the seven-stage hypnotic architecture to unfold with sufficient time for each stage's depth progression and fractionation pocket.

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis bed for deep absorption and gentle reorientation.
- Stage map:
  - Orientation / Settling: 0:00-3:00, baseline to light depth, establishes voice-adjacent carrier and first anchor pairing.
  - Induction / Narrowing: 3:00-7:00, light to medium depth, paces attention through carrier variations.
  - Deepening: 7:00-13:00, medium to deep depth, widens space and lowers carrier salience.
  - Core Absorptive Work: 13:00-20:00, deep depth, sustains absorption with rare carrier returns.
  - Fractionation: 20:00-22:00, deep to light then back to deep (lightening 20:00-21:00, re-deepening 21:00-22:00), no reset.
  - Integration / Stabilization: 22:00-23:30, light to deep return (not as deep as core), preserves calm while reducing depth pressure.
  - Return / Reorientation: 23:30-24:00, deep to baseline, gently opens attention without abrupt arousal.
- Depth trajectory: Baseline (low harmonic density, narrow width) → Light (density increases, width widens slightly) → Medium (density peaks, width expands) → Deep (stabilized density, maximum width) → Lightening (width contracts, brightness increases) → Re-deepening (width expands again, brightness recedes) → Integration (width contracts, brightness softens) → Baseline (narrow width, receded brightness).
- Pacing-and-leading plan: First pace broad listening with slow ground and sparse carrier (Orientation), then lengthen rests, lower register, darken brightness, widen space to lead attention inward.
- Ratification plan: Voice-adjacent carrier returns with downward-settling contour at end of each stage to ratify depth achieved.
- Suggestion-compatible windows: 7:30-9:00 (Deepening), 13:00-18:30 (Core Absorptive Work), 22:00-23:00 (Integration).
- Reset avoidance rule: No abrupt cuts, hard transients, alerting cues, or sudden brightness spikes.

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Two-note minor third interval cell (F-D) with soft decay and slight downward pitch bend.
- First form: Sparse low-mid glint (0.5s duration) every 30 seconds in Orientation.
- Later forms: Transforms to three-note sequence (F-D-C) with wider spacing in Deepening; becomes single note (F) at carrier phrase ends in Core Absorptive Work.

### Material Inventory - Harmonic Signature
- Home color: Root F2 (87.31 Hz) with interval stack [0, 3, 10] (F-A-D).
- Contrast color: Root Bb2 (93.70 Hz) with interval stack [0, 4, 10] (Bb-Eb-A).
- Return color: Home color with third interval (A) removed and seventh (D) softened to minor sixth.

### Material Inventory - Texture Family
- Continuous layer behavior: Low-mid drone (F2) with slow amplitude modulation (0.1 Hz), creating breathing field.
- Event layer behavior: Primary motif cell appears as sparse events every 15-45 seconds, increasing frequency in Deepening.
- Noise / air behavior: High-frequency noise band (10-20 kHz) that subtly brightens during fractionation and recedes in Core Absorptive Work.

### Material Inventory - Spatial Material
- Width behavior: Narrow (Orientation) → Medium (Induction) → Wide (Deepening/Core) → Medium-wide (Fractionation) → Narrow (Integration/Return).
- Motion behavior: Event layer drifts left to right irregularly; air layer moves from rear to edges.
- Depth behavior: Continuous layer at medium depth; event layer shifts near-to-far with reverb tail.

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.1 Hz) for continuous layer; asymmetric shimmer (0.15 Hz and 0.25 Hz rates) for event layer.
- Change rule: Modulation rates slow in Deepening/Core, speed up slightly during fractionation lightening.

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1.
- Reference pitch-contour profile: falling_settling (from reference_voice_pitch_contours_v1).
- Carrier instrument / layer: Sarangi-like bowed string patch (GM program 89, custom filter).
- Cadence behavior: Breath-length phrase arcs (1.5s-3.15s) with permissive pauses (0.75s).
- Contour behavior: Downward-settling in all stages except Fractionation lightening (brief rising arch).
- Weighting behavior: Emphasis on downward fall with slight amplitude increase at start.
- Return behavior: Returns as ratification cue (e.g., end of Orientation/Induction) with deeper settling.
- Salience limit: Carrier at -15 dB relative to continuous layer, never exceeding -10 dB.

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Soft low-mid glint (F2, 0.3s duration) at carrier phrase end.
- First pairing: At 3:00, coincides with carrier settling downward; field becomes warmer.
- Withholding plan: Absent from 3:00 to 13:00 (except single return at 7:00).
- Return / trigger plan: Returns at 20:00 (fractionation start) and 21:30 (re-deepening), coinciding with carrier settling.
- Salience policy: Anchor at -20 dB relative to continuous layer, audible only when carrier phrase ends.

## Development Grammar

- Establishment rule: Introduce continuous layer and primary motif cell in Orientation.
- Variation rule: Motif transforms by adding notes (two to three) and increasing spacing in Deepening.
- Contrast rule: Harmonic signature shifts from home color (F) to contrast color (Bb) in Deepening.
- Recurrence rule: Primary motif cell returns transformed every 15-45 seconds (not identically).
- Foreground rule: Carrier becomes slightly more audible in Induction/Deepening but never exceeds salience ceiling.
- Withholding rule: Anchor withheld for 10 minutes (until 13:00) to preserve meaning.
- Fractionation rule: Lightening (20:00-21:00) via air layer brightness increase and spatial contraction; re-deepening (21:00-22:00) via wider expansion and darker harmonic color.
- Trigger conditioning rule: Anchor paired with carrier downward settling at 3:00 to associate it with deepening.
- Dissolution / cadence rule: End by reducing event density, narrowing width to contained, fading air layer.
- Anti-padding rule: No material repeated merely to fill time; every event serves development.

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish the field and first carrier pairing.
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: Orientation / Settling
- Target depth: baseline to light
- Material state: Continuous layer, primary motif cell, voice-adjacent carrier.
- Development action: Establish
- Hypnotic function: Orientation (settle)
- Energy / density posture: Sparse, low
- Harmonic / spectral posture: Home color (F2 with [0,3,10])
- Spatial posture: Narrow width, centered
- Transition condition: Carrier established and anchor first paired (at 3:00)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace attention inward through carrier variations.
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: Induction / Narrowing
- Target depth: light to medium
- Material state: Continuous layer, primary motif cell (increasing frequency), voice-adjacent carrier with variations.
- Development action: Vary
- Hypnotic function: Induction (pace and lead)
- Energy / density posture: Moderate, increasing
- Harmonic / spectral posture: Home color with subtle harmonic expansion (adding contrast partials)
- Spatial posture: Medium width, slight motion
- Transition condition: Attention narrowed and space prepared to widen (at 7:00)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory.
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: Deepening
- Target depth: medium to deep
- Material state: Continuous layer, harmonic body (contrast color), air layer, carrier returns.
- Development action: Thicken and widen
- Hypnotic function: Deepening (widen space, lower salience)
- Energy / density posture: Dense
- Harmonic / spectral posture: Contrast color (Bb2 with [0,4,10])
- Spatial posture: Wide
- Transition condition: Depth stable and core can withhold carrier (at 13:00)
- Motion source commitment: parameter_group_sequence

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with non-inert microdevelopment.
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: Core Absorptive Work
- Target depth: deep
- Material state: Continuous layer, harmonic body (softened home color), rare carrier returns.
- Development action: Withhold and recur
- Hypnotic function: Core (sustain absorption)
- Energy / density posture: Sparse
- Harmonic / spectral posture: Softened home color (F2 with [0,10] and missing third)
- Spatial posture: Medium-wide
- Transition condition: Field thinned and fractionation pre-echo seeded (at 20:00)
- Motion source commitment: event_schedule

### Formal Unit 5 - Fractionation Pocket
- Name / role: Brief lightening and deeper return.
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: Fractionation
- Target depth: deep to light then back to deep (lightening 20:00-21:00, re-deepening 21:00-22:00)
- Material state: Continuous layer, air layer (brighter), carrier becomes clearer.
- Development action: Lighten and re-deepen
- Hypnotic function: Fractionation (brief lightening without reset)
- Energy / density posture: Moderate, then sparse
- Harmonic / spectral posture: Brighter (air layer), then returns to softened home color
- Spatial posture: Medium-wide, contracts slightly during lightening, expands for re-deepening
- Transition condition: Fractionation pocket complete (at 22:00)
- Motion source commitment: none — short unit, no internal motion source required

### Formal Unit 6 - Integration / Stabilization
- Name / role: Preserve calm while reducing depth pressure.
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: Integration / Stabilization
- Target depth: light to deep return (not as deep as core)
- Material state: Continuous layer, harmonic body (softened home color), carrier returns.
- Development action: Recur and thin
- Hypnotic function: Integration (stabilize)
- Energy / density posture: Sparse
- Harmonic / spectral posture: Softened home color, slight brightness increase for integration.
- Spatial posture: Medium width
- Transition condition: Integration complete (at 23:30)
- Motion source commitment: none — short unit, no internal motion source required

### Formal Unit 7 - Return / Reorientation
- Name / role: Gently open attention without abrupt arousal.
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: Return / Reorientation
- Target depth: deep to baseline
- Material state: Continuous layer, carrier (familiar contour), air layer fading.
- Development action: Dissolve
- Hypnotic function: Return (reorient)
- Energy / density posture: Very sparse
- Harmonic / spectral posture: Home color, air layer fading.
- Spatial posture: Narrow
- Transition condition: None (end of piece)
- Motion source commitment: none — short unit, no internal motion source required

## Orchestration Map

- Root / ground function: Continuous layer (drone at F2)
- Harmonic color function: Harmonic body layer (interval stacks for home/contrast colors)
- Motion function: Event layer (primary motif cell) and air layer (noise band for micro-events)
- Foreground / event function: Voice-adjacent carrier and primary motif cell
- Voice-adjacent induction function: Sarangi-like bowed string (carrier layer)
- Anchor / trigger function: The anchor glint (subtle cue at carrier phrase end)
- Air / bloom function: High-frequency noise band (10-20 kHz) for air layer
- Spatial / depth function: Layer placement (continuous centered, event moving, air at rear)
- Silence / restraint function: Withholding anchor for 10 minutes (until 13:00)

## Temporal Behavior

- Micro-event rate: Primary motif cell every 15-45 seconds (increasing to every 30s in Deepening).
- Scene-change rate: Every 7-10 minutes (at stage transitions).
- Long-wave change: Harmonic signature shifts from home to contrast and back.
- Maximum stasis tolerance: 45 seconds without meaningful change (e.g., no motif cell, but continuous layer modulates).
- Repetition policy: Primary motif returns transformed (not identically).
- Fractionation timing: Lightening at 20:00-21:00, re-deepening at 21:00-22:00.
- Anchor recurrence spacing: First at 3:00, then withheld until 20:00 (17 minutes later), and again at 21:30.

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow → medium → wide → medium-wide → narrow (as per depth trajectory)
- Layer placement principles: continuous layer centered; harmonic body wide stable; carrier near center low motion; air at edges and rear.
- Motion limits: No fast autopan; spatial motion over 20-120 seconds allowed only for tiny anchor gestures.
- Verification obligation: Verify not mono, not dual-mono; width arc audible and motion not alerting.

## Revision Criteria

- Failure mode: too static
  - Evidence: no perceptible change for >120 seconds (e.g., Core Absorptive Work without motif cell or harmonic shift).
  - Revision target: Add transformed recurrence of primary motif cell every 30-45 seconds.

- Failure mode: weak hypnotic architecture
  - Evidence: no clear depth progression (e.g., Deepening does not widen space or darken brightness).
  - Revision target: Adjust spatial width and harmonic color to match depth trajectory.

- Failure mode: weak fractionation
  - Evidence: lightening at 20:00-21:00 feels like reset (e.g., sudden brightness spike).
  - Revision target: Ensure lightening is gentle increase in air layer brightness and spatial contraction, then re-deepening via wider expansion.

- Failure mode: weak voice-adjacent induction
  - Evidence: carrier behaves like lead melody (e.g., too bright, high register).
  - Revision target: Reduce carrier salience to -15 dB and use downward-settling phrases with breath-length timing.

## Sketch Handoff

- Required sketch style family: sequenced_ambient
- Required formal units: Orientation, Induction, Deepening, Core Absorptive Work, Fractionation, Integration, Return
- Required material to preserve: primary motif cell (with transformation), harmonic signature (home/contrast/return), texture behavior, spatial material.
- Required hypnotic architecture to preserve: stage map (with durations), depth trajectory, fractionation behavior.
- Required voice-adjacent induction behavior to preserve: carrier (sarangi-like), cadence profile, salience ceiling.
- Required development grammar to preserve: recurrence rule (transformed returns), contrast rule, withholding rule.
- Required revision criteria to preserve: the four listed above (too static, weak hypnotic architecture, weak fractionation, weak voice-adjacent induction).
- Explicit exclusions: no percussion, no pop hooks, no abrupt changes.