# Composition Design Document
DESIGN ID: cdg-neuro-ambient-composition-046-001
WORK_MANIFEST: wmf-neuro-ambient-composition-046-001
Status: ready

## Artistic Reading

- User image / premise: A continuous breath-length instrumental voice guides the listener through a self-hypnotic journey from orientation to return, evoking calm internal absorption.
- Musical problem: Creating a continuous voice that guides without becoming foreground while structuring seven hypnotic stages with soft transitions.
- Listener stance: Inward deep listening, absorbing the field without tracking individual events.
- What this is not: Dramatic, pop-hook-based, cinematic tension, or sad narrative.
- Style-family fit: Neuro-ambient with voice-adjacent induction (subsalient carrier) and long-form hypnotic stage journey.

## Chosen Musical Premise

- Premise sentence: A sarangi-like bowed string carrier delivers breath-length phrases that settle downward, guiding the listener through seven hypnotic stages with a stable low-mid anchor and evolving harmonic color.
- Primary dramatic motion: Establish → Reveal → Deepen → Sustain → Lighten and return deeper → Stabilize → Dissolve.
- Sound-world metaphor: A slow, deepening breath in a warm cave — the voice is the breath itself.
- Musical consequence: Carrier's downward settling contour creates natural absorption rhythm; harmonic color shifts (home to contrast to return) provide subtle progression without disruption.
- Duration rationale: 24 minutes allows seven stages to unfold organically (e.g., 6-minute deepening, 7-minute core) with micro-events feeling intentional.

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for deep absorption and return.
- Stage map:
  - Orientation (0:00-3:00): baseline to light, function=settle and establish safety
  - Induction (3:00-7:00): light to medium, function=pace and lead attention inward
  - Deepening (7:00-13:00): medium to deep, function=reduce orienting load
  - Core absorptive work (13:00-20:00): deep, function=sustain absorption
  - Fractionation (20:00-22:00): deep to light-medium to deep, function=briefly lighten then return deeper
  - Integration (22:00-23:30): medium to light, function=stabilize
  - Return (23:30-24:00): light to baseline, function=reorient
- Depth trajectory: Baseline (orientation) → Light (induction) → Medium (deepening start) → Deep (core) → Brief light-medium (fractionation) → Light (integration) → Baseline (return). Achieved via carrier register descent, harmonic color darkening, spatial narrowing.
- Pacing-and-leading plan: Carrier phrases establish slow breath rhythm (6-14s) in orientation; induction tightens phrase spacing and adds downward settling; deepening extends phrases and lowers register.
- Ratification plan: Carrier returns to orientation contour at integration, confirming journey completion without new material.
- Suggestion-compatible windows: Quiet moments between carrier phrases (0.5-1.8s) and harmonic bed shifts.
- Reset avoidance rule: No abrupt cuts, hard transients, or sudden brightness spikes; transitions use 12s crossfades and harmonic-region shifts.

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Two-note interval cell (minor third, falling) with soft attack and long release.
- First form: F4 to D4 (minor third) at 0.9s duration, soft attack (15ms), long release (2.4s).
- Later forms: Transposed down by minor third in deepening; widened to major sixth in integration.

### Material Inventory - Harmonic Signature
- Home color: Root (F2, 87.31 Hz) + fifth (C3, 174.62 Hz) + minor seventh (E♭3, 207.65 Hz).
- Contrast color: Root a fourth above (B♭2, 147.65 Hz) + second (C3, 174.62 Hz) + fifth (F3, 349.23 Hz).
- Return color: Original home color with minor seventh softened (reduced by 6dB).

### Material Inventory - Texture Family
- Continuous layer behavior: Substrate drone at 87.31 Hz with slow amplitude modulation (0.05-0.12Hz) and 8s release.
- Event layer behavior: One motif cell per stage (max), emerging at carrier's downward settling point.
- Noise / air behavior: Air layer with slow spectral drift (0.15Hz) and 60s release, opening only after event layers.

### Material Inventory - Spatial Material
- Width behavior: Narrow (0.5) → Medium (1.2) → Wide (2.0) → Medium-wide (1.5) → Narrow (0.6).
- Motion behavior: Slow drift from left to right in deepening, then back in integration.
- Depth behavior: Near (0.2) → Medium (0.5) → Far (0.8) in core, then back to near.

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13Hz) for substrate, asymmetric shimmer (multiple slow rates) for air.
- Change rule: Modulation depth decreases by 20% in deepening, then increases slightly in fractionation.

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1.
- Reference pitch-contour profile: reference_voice_pitch_contours_v1 (falling_settling dominant).
- Carrier instrument / layer: Sarangi-like bowed string patch with warm low-mid resonance.
- Cadence behavior: 0.9-3.3s phrase arcs with 0.5-1.8s rests, permissive pauses.
- Contour behavior: Falling_settling (84.5% of phrases), with occasional arch for fractionation.
- Weighting behavior: Slight register warmth at phrase end, no gain spikes.
- Return behavior: Carrier returns to orientation contour in integration with 30% lower density.
- Salience limit: -15dB relative to harmonic bed, conditionable but not foreground.

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: 0.2s spectral unsealing (150Hz to 300Hz) at carrier's phrase end.
- First pairing: Orientation (0:45), paired with first downward settling phrase.
- Withholding plan: Absent in induction and deepening to preserve meaning.
- Return / trigger plan: 12:00 (deepening stability), 19:30 (fractionation re-deepening), 22:45 (integration).
- Salience policy: Audible enough to learn (0.5dB above substrate), not salient enough to monitor.

## Development Grammar

- Establishment rule: Introduce carrier and home color in orientation.
- Variation rule: Transpose motif down by minor third each stage; harmonic contrast appears at deepening.
- Contrast rule: Home color (warm, stable) vs. contrast color (slightly brighter, less stable).
- Recurrence rule: Motif cell returns transformed in integration; anchor returns at key thresholds.
- Foreground rule: No foreground events except carrier (subsalient).
- Withholding rule: Anchor withheld in induction and deepening to preserve meaning.
- Fractionation rule: Brief lightening via carrier contour (arch instead of settling) and harmonic contrast, then return to home color with deeper density.
- Trigger conditioning rule: Anchor paired with carrier's downward settling; return at deepening stability and fractionation re-deepening.
- Dissolution / cadence rule: Final fade with carrier returning to orientation contour, harmonic bed simplifying.
- Anti-padding rule: No event repetition without transformation; each micro-event unique.

## Formal Architecture

### Formal Unit 1 - Orientation
- Name / role: Settling the field, establishing safety.
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: Carrier (sarangi) enters, home color harmonic bed.
- Development action: Establish carrier and motif cell.
- Hypnotic function: settle
- Energy / density posture: low (0.2)
- Harmonic / spectral posture: home color, warm
- Spatial posture: narrow (0.5), centered.
- Transition condition: Carrier phrase ends with downward settling at 2:45.
- Motion source commitment: `event_schedule` (motif cell at 0:45)

### Formal Unit 2 - Induction
- Name / role: Narrowing attention inward through pacing.
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: Carrier phrases tighten, motif cell emerges.
- Development action: Vary carrier phrasing to lead attention inward.
- Hypnotic function: pace and lead
- Energy / density posture: medium-low (0.4)
- Harmonic / spectral posture: home color, slight brightness
- Spatial posture: medium (1.2), slow left drift.
- Transition condition: Carrier phrase ends with deeper settling at 6:30.
- Motion source commitment: `event_schedule` (motif cell at 3:45)

### Formal Unit 3 - Deepening
- Name / role: Reducing orienting load through harmonic shift.
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: Harmonic bed shifts to contrast color, carrier lowers register.
- Development action: Contrast harmonic region; deepen carrier contour.
- Hypnotic function: reduce orienting load
- Energy / density posture: medium (0.5)
- Harmonic / spectral posture: contrast color, darker
- Spatial posture: medium-wide (1.5), slow right drift.
- Transition condition: Harmonic shift completes at 10:30.
- Motion source commitment: `parameter_group_sequence` (harmonic shift at 4:30)

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustaining deep absorption through continuous field.
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: Contrast color harmonic bed, carrier in deepest register.
- Development action: Sustain with subtle modulation shifts.
- Hypnotic function: sustain absorption
- Energy / density posture: deep (0.6)
- Harmonic / spectral posture: contrast color, stable
- Spatial posture: wide (2.0), minimal motion.
- Transition condition: Air layer opens at 19:30 for fractionation lightening.
- Motion source commitment: `parameter_group_sequence` (harmonic bed modulation)

### Formal Unit 5 - Fractionation
- Name / role: Brief lightening then deeper return without reset.
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep to light-medium to deep
- Material state: Carrier contour shifts to arch, harmonic contrast brightens.
- Development action: Lighten via carrier contour and brightness; return deeper.
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: light-medium (0.4)
- Harmonic / spectral posture: contrast color, brighter
- Spatial posture: medium-wide (1.5), slight widening.
- Transition condition: Fractionation lightening ends at 20:30, return begins.
- Motion source commitment: none — short unit, no internal motion source required

### Formal Unit 6 - Integration
- Name / role: Stabilizing calm through carrier return.
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: Carrier returns to orientation contour, harmonic bed simplifies.
- Development action: Recur orientation motif; stabilize depth pressure.
- Hypnotic function: stabilize
- Energy / density posture: light (0.3)
- Harmonic / spectral posture: home color, softened
- Spatial posture: medium (1.2), slow contraction.
- Transition condition: Carrier phrase ends with settling at 23:15.
- Motion source commitment: none — short unit, no internal motion source required

### Formal Unit 7 - Return
- Name / role: Reorienting attention through dissolution.
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: Carrier returns to orientation contour, harmonic bed fades.
- Development action: Dissolve with carrier and harmonic simplification.
- Hypnotic function: reorient
- Energy / density posture: baseline (0.1)
- Harmonic / spectral posture: home color, minimal
- Spatial posture: narrow (0.5), centered.
- Transition condition: Final carrier phrase ends at 23:58.
- Motion source commitment: none — short unit, no internal motion source required

## Orchestration Map

- Root / ground function: Substrate drone at 87.31 Hz.
- Harmonic color function: Harmonic bed (home/contrast/return colors).
- Motion function: Carrier's breath-length phrases.
- Foreground / event function: Motif cell (one per stage).
- Voice-adjacent induction function: Carrier layer.
- Anchor / trigger function: Spectral unsealing (anchor cue).
- Air / bloom function: Air layer with slow spectral drift.
- Spatial / depth function: Stereo width and depth changes.
- Silence / restraint function: Withholding anchor in induction/deepening.

## Temporal Behavior

- Micro-event rate: One motif cell per stage (every 60-120s).
- Scene-change rate: Every 3-7 minutes (harmonic shifts at stage transitions).
- Long-wave change: Harmonic color shift from home to contrast to return.
- Maximum stasis tolerance: 45 seconds without meaningful change (e.g., no motif cell, but air layer drift).
- Repetition policy: Motif cell transforms on return; harmonic bed shifts without repetition.
- Fractionation timing: 20:00-22:00 (lightening at 20:30, return to deep at 21:00).
- Anchor recurrence spacing: At 0:45 (orientation), 12:00 (deepening stability), 19:30 (fractionation re-deepening), 22:45 (integration).

## Spatial Composition

- Stereo requirement: stereo.
- Spatial arc: Narrow → Medium → Wide → Medium-wide → Narrow (as defined in Material Inventory).
- Layer placement principles: Substrate centered, harmonic bed wide stable, carrier near center low motion, air at edges/rear.
- Motion limits: No fast autopan; slow drift (10s to cross).
- Verification obligation: Verify not mono, width arc audible.

## Revision Criteria

- Failure mode: too static
  - Evidence: no perceptible change for >45s, harmonic color unchanged.
  - Revision target: add micro-event (e.g., air layer drift) or subtle harmonic shift.

- Failure mode: weak recurrence
  - Evidence: motif cell appears once and vanishes; anchor returns identically.
  - Revision target: transform motif by register (e.g., minor third → major sixth) and anchor by brightness.

- Failure mode: weak fractionation
  - Evidence: lightening feels like reset; return not deeper.
  - Revision target: reduce fractionation salience (brightness <10% increase), strengthen re-deepening via harmonic bed darkening.

- Failure mode: weak anchors / triggers
  - Evidence: anchor cues random, too obvious (0.5dB above substrate), or absent in key thresholds.
  - Revision target: move anchor to carrier's phrase end, soften onset (0.2s unsealing), add withholding in induction.

- Failure mode: weak hypnotic architecture
  - Evidence: no clear depth progression; stage transitions feel abrupt.
  - Revision target: bind harmonic color to depth (home=light, contrast=deep), add 12s crossfades.

- Failure mode: weak ending
  - Evidence: piece fades out without carrier return or harmonic simplification.
  - Revision target: add carrier returning to orientation contour at 23:45, harmonic bed simplifying.

## Sketch Handoff

- Required sketch style family: neuro-ambient with voice-adjacent induction.
- Required formal units: 7 stages as defined in Formal Architecture.
- Required material to preserve: Primary motif, harmonic signature (home/contrast/return), texture behavior.
- Required hypnotic architecture to preserve: Stage map, depth trajectory, fractionation timing, anchor returns.
- Required voice-adjacent induction behavior to preserve: Carrier (sarangi), cadence profile, salience limit.
- Required development grammar to preserve: Recurrence rule, contrast rule, fractionation rule.
- Required revision criteria to preserve: The full list above.
- Explicit exclusions: No percussion, no bright transient events, no foreground melody.