# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-060-001
WORK_MANIFEST: wmf-neuro-ambient-composition-060-001
Status: ready

## Artistic Reading

- User image / premise: A continuous, voice-adjacent lead guides the listener through seven hypnotic stages, evoking a deep, self-sustaining state of absorptive calm.
- Musical problem: To create 24-minute continuous form that guides listeners through distinct hypnotic stages without abrupt changes, using harmonic evolution and prosodic lead to maintain depth progression.
- Listener stance: Inward deep listening, absorbing the piece as a continuous environment without active monitoring of structure.
- What this is not: Not a pop song, not a dramatic piece, not a rhythmic groove, not static drone (stasis ≠ continuity), and not alerting with abrupt transitions.
- Style-family fit: Neuro-ambient with subsalient voice-adjacent induction (carrier layer), generative ambient bed, and subtle anchoring. Fits the style family's absorption over spectacle bias.

## Chosen Musical Premise

- Premise sentence: A continuous voice-adjacent carrier with breath-length phrase arcs guides listeners through seven hypnotic stages via harmonic evolution and subtle spatial shifts, maintaining a warm low-mid anchor throughout.
- Primary dramatic motion: Establish → Reveal → Deepen → Sustain → Lighten and Return Deeper → Stabilize → Dissolve.
- Sound-world metaphor: A slow, deepening river that widens and narrows with the seasons, carrying gentle current of voice-like ripples.
- Musical consequence: River metaphor changes harmony (interval shifts in harmonic bed), texture (water droplets/air bubbles), register (lead descends as river deepens), and space (stereo field widens to represent breadth).
- Duration rationale: 24 minutes earned through seven-stage journey with each stage's duration allowing natural development of depth (core absorptive work at 7 minutes sustains deep absorption).

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for deep absorption and reorientation.
- Stage map:
  - Orientation / Settling: 3:00, baseline to light depth, function: establish safety and first carrier pairing.
  - Induction / Narrowing: 4:00, light to medium depth, function: pace attention inward via carrier phrasing.
  - Deepening: 6:00, medium to deep depth, function: reduce orienting load and widen field.
  - Core Absorptive Work: 7:00, deep depth, function: sustain absorption with sparse motif fragments.
  - Fractionation Pocket: 2:00, deep to light-medium to deep depth, function: briefly lighten attention then return deeper.
  - Integration / Stabilization: 1:30, medium to light depth, function: stabilize absorbed state.
  - Return / Reorientation: 0:30, light to baseline depth, function: gently reorient attention outward.
- Depth trajectory: Baseline (0) → Light (1) → Medium (2) → Deep (3) → Light-Medium (2.5) → Medium (2) → Baseline (0). Achieved via harmonic root shifts, density changes, and lead register descent.
- Pacing-and-leading plan: Carrier's breath-length phrases (6–14s) and downward settling contour pace attention inward during induction, then slower decay tails deepen state.
- Ratification plan: Carrier returns with transformed contour (longer phrase, darker register) at deepening stability points to ratify accepted state.
- Suggestion-compatible windows: Quietest moments (after carrier phrase resolves) for suggestion compatibility.
- Reset avoidance rule: No abrupt changes, no hard transients, no sudden brightness spikes.

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Three-note interval cell `[0, -1, 2]` (F3-E3-G#3) in lead.
- First form: Soft breath-length phrase arc (1.5s) at orientation's first carrier entry.
- Later forms: Transposed down minor third (D3-C3-E#3) at deepening stability; transformed to `[0, 3, 7]` (F4-A4-E5) in core absorptive work.

### Material Inventory - Harmonic Signature
- Home color: F2 root with interval stack `[0, 4, 7]` (F2-B2-C#3) in harmonic bed.
- Contrast color: B2 root with `[0, 4, 7]` (B2-E3-F#3) during induction.
- Return color: F2 root with `[0, 4, 7]` replaced by minor seventh (F2-A2-C#3) in integration.

### Material Inventory - Texture Family
- Continuous layer behavior: Warm F2 drone with 0.1 Hz slow beating (ground).
- Event layer behavior: Rare motif fragments appearing every 15–45 seconds.
- Noise / air behavior: High-frequency air bloom opening slowly at stereo edges.

### Material Inventory - Spatial Material
- Width behavior: Narrow (10°) → Medium (45°) → Wide (90°) → Medium-Wide (75°) → Narrow (20°) → Medium (45°) → Narrow (10°).
- Motion behavior: Slow drifting air bloom (10s cycle) and subtle stereo widening during deepening.
- Depth behavior: Reverb tail length increases from 1.5s (orientation) to 3.0s (deepening), then decreases.

### Material Inventory - Modulation Identity
- Rate family: Slow asymmetric shimmer (0.05–0.15 Hz) with multiple incommensurable rates.
- Change rule: Modulation depth decreases by 10% every 3 minutes, creating settling effect.

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1`.
- Reference pitch-contour profile: Dominant falling_settling (84.5%).
- Carrier instrument / layer: Soft cello patch with formant resonance (GM program 94).
- Cadence behavior: Breath-length phrases (0.6–3.5s) with permissive pauses (0.5–1.8s).
- Contour behavior: Downward settling at deepening; arches for fractionation lightening.
- Weighting behavior: Slight sustain increase at phrase peak (weighted emphasis).
- Return behavior: Carrier returns transformed (longer phrase, darker register) at deepening stability and core reassurance.
- Salience limit: Carrier 6 dB above harmonic bed, 10 dB above substrate (subsalient carrier).

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Low-mid warmth bloom (0.5s) at carrier phrase resolution.
- First pairing: At 0:45, warmth bloom appears after first carrier phrase resolves.
- Withholding plan: Anchor absent for 90 seconds (until induction threshold).
- Return / trigger plan: Returns at deepening stability (5:30), core reassurance (14:00), fractionation re-deepening (20:30), integration (21:45).
- Salience policy: Audible enough to learn, not salient enough to cause monitoring (0.5 dB above bed).

## Development Grammar

- Establishment rule: Introduce carrier and harmonic bed with minimal event density (0 events in first 3 minutes).
- Variation rule: Shift harmonic root by fourth during induction; expand interval stack in deepening.
- Contrast rule: Use contrast color (B2 root) for induction, return to home with minor seventh in core.
- Recurrence rule: Primary motif cell returns transformed at deepening stability and core (transposed, filtered).
- Foreground rule: Only motif cell becomes foreground in core absorptive work (sparse events).
- Withholding rule: Anchor absent for 90 seconds after first pairing; carrier withheld in fractionation pocket.
- Fractionation rule: Brief lightening via carrier contour (slightly brighter, clearer) for 30 seconds at 19:00–19:30, then re-deepening via darker register and wider field.
- Trigger conditioning rule: Anchor pairs with carrier phrase resolution to create learned association for deepening and return.
- Dissolution / cadence rule: Final fade with carrier dissolving into harmonic bed, spectral narrowing to 1700 Hz.
- Anti-padding rule: No event loops; all events spaced ≥15 seconds apart.

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish safety and first carrier pairing.
- Approximate duration range: 0:00–3:00
- Hypnotic stage role: Orientation / Settling
- Target depth: Baseline to light
- Material state: Ground, harmonic bed (home color), carrier seed.
- Development action: Establish world and first pairing.
- Hypnotic function: Settle and establish safety.
- Energy / density posture: Low (0.25).
- Harmonic / spectral posture: Home color, F2 root.
- Spatial posture: Narrow (10°), reverb 1.5s.
- Transition condition: Carrier established and anchor first paired (0:45).
- Motion source commitment: `event_schedule` (first anchor at 0:45).

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace attention inward via carrier phrasing.
- Approximate duration range: 3:00–7:00
- Hypnotic stage role: Induction / Narrowing
- Target depth: Light to medium
- Material state: Ground, harmonic bed (contrast color), carrier variants.
- Development action: Vary contour and pace attention inward.
- Hypnotic function: Pace and lead attention.
- Energy / density posture: Medium (0.5).
- Harmonic / spectral posture: Contrast color, B2 root.
- Spatial posture: Medium (45°), reverb 1.8s.
- Transition condition: Attention narrowed and space prepared to widen (5:00).
- Motion source commitment: `parameter_group_sequence` (harmonic shift from home to contrast at 5:00).

### Formal Unit 3 - Deepening
- Name / role: Widen field and stabilize root memory.
- Approximate duration range: 7:00–13:00
- Hypnotic stage role: Deepening
- Target depth: Medium to deep
- Material state: Ground, harmonic bed (expanded stack), air, carrier returns.
- Development action: Thicken field and widen space.
- Hypnotic function: Reduce orienting load.
- Energy / density posture: Medium-high (0.7).
- Harmonic / spectral posture: Expanded stack `[0,4,7,12]`, F2 root.
- Spatial posture: Wide (90°), reverb 3.0s.
- Transition condition: Depth stable and core can withhold carrier (12:45).
- Motion source commitment: `parameter_group_sequence` (harmonic shift to expanded stack at 10:00).

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with non-inert microdevelopment.
- Approximate duration range: 13:00–20:00
- Hypnotic stage role: Core Absorptive Work
- Target depth: Deep
- Material state: Ground, harmonic bed (home with minor seventh), rare carrier.
- Development action: Sustain depth with sparse events.
- Hypnotic function: Sustain absorption.
- Energy / density posture: Medium (0.6).
- Harmonic / spectral posture: Home color with minor seventh.
- Spatial posture: Medium-Wide (75°), reverb 2.5s.
- Transition condition: Field thinned and fractionation pre-echo seeded (19:45).
- Motion source commitment: `event_schedule` (motif fragments at 14:00, 16:30, 18:00).

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten attention then return deeper.
- Approximate duration range: 20:00–22:00
- Hypnotic stage role: Fractionation Pocket
- Target depth: Deep to light-medium to deep
- Material state: Ground, harmonic bed (lightened brightness), air bloom.
- Development action: Lighten attention then return deeper.
- Hypnotic function: Briefly lighten and re-deepen.
- Energy / density posture: Low-medium (0.4).
- Harmonic / spectral posture: Brightness increased, density reduced.
- Spatial posture: Narrow (20°), reverb 1.8s.
- Transition condition: Lightening complete and deeper return begins (20:30).
- Motion source commitment: none — short unit, no internal motion source required.

### Formal Unit 6 - Integration / Stabilization
- Name / role: Stabilize absorbed state.
- Approximate duration range: 22:00–23:30
- Hypnotic stage role: Integration / Stabilization
- Target depth: Medium to light
- Material state: Ground, harmonic bed (return color), carrier returns.
- Development action: Stabilize and reduce depth pressure.
- Hypnotic function: Stabilize absorbed state.
- Energy / density posture: Low (0.3).
- Harmonic / spectral posture: Return color, F2 root minor seventh.
- Spatial posture: Medium (45°), reverb 1.8s.
- Transition condition: Depth pressure reduced and return threshold reached (23:00).
- Motion source commitment: none — short unit, no internal motion source required.

### Formal Unit 7 - Return / Reorientation
- Name / role: Gently reorient attention outward.
- Approximate duration range: 23:30–24:00
- Hypnotic stage role: Return / Reorientation
- Target depth: Light to baseline
- Material state: Ground, harmonic bed (home color), carrier dissolves.
- Development action: Dissolve lead into bed with spectral narrowing.
- Hypnotic function: Reorient attention outward.
- Energy / density posture: Very low (0.1).
- Harmonic / spectral posture: Home color, spectral narrowing to 1700 Hz.
- Spatial posture: Narrow (10°), reverb 1.2s.
- Transition condition: Final fade begins (23:45).
- Motion source commitment: none — short unit, no internal motion source required.

## Orchestration Map

- Root / ground function: Ground layer (warm F2 drone with slow beating).
- Harmonic color function: Harmonic bed (carries interval stacks and root shifts).
- Motion function: Carrier layer (voice-adjacent, breath-length phrases) for pacing.
- Foreground / event function: Event layer (motif fragments, anchor cues).
- Voice-adjacent induction function: Carrier layer.
- Anchor / trigger function: Air bloom (low-mid warmth) for subtle triggers.
- Air / bloom function: High-frequency air layer (slow opening/closing).
- Spatial / depth function: Stereo field width and reverb tail.
- Silence / restraint function: Carrier withheld in fractionation pocket.

## Temporal Behavior

- Micro-event rate: Every 15–45 seconds (motif fragments, anchor returns).
- Scene-change rate: Every 3 minutes (harmonic shifts at induction and deepening thresholds).
- Long-wave change: Harmonic root shift from F2 to B2 (induction) then to expanded stack (deepening).
- Maximum stasis tolerance: 45 seconds without meaningful change.
- Repetition policy: Primary motif cell returns transformed; no looped events.
- Fractionation timing: Lightening 20:00–20:30, re-deepening 20:30–22:00.
- Anchor recurrence spacing: Deepening stability (5:30), core reassurance (14:00), fractionation re-deepening (20:30), integration (21:45).

## Spatial Composition

- Stereo requirement: stereo.
- Spatial arc: Narrow (10°) → Medium (45°) → Wide (90°) → Medium-Wide (75°) → Narrow (20°) → Medium (45°) → Narrow (10°).
- Layer placement principles: Ground centered, harmonic bed wide stable, carrier near center low motion, air at edges and rear.
- Motion limits: No fast autopan; spatial motion over 20 seconds for air layer.
- Verification obligation: Verify not mono, width arc audible and motion not alerting.

## Revision Criteria

- Failure mode: too static
  - Evidence: no harmonic shift or event for >45 seconds in deepening stage.
  - Revision target: Add harmonic shift at 9:00 and event at 10:30.

- Failure mode: too generic
  - Evidence: harmonic bed uses common intervals (major triads) without defined signature.
  - Revision target: Enforce home color `[0,4,7]` (F2-B2-C#3) and contrast as `[0,4,7]` on B2.

- Failure mode: too busy
  - Evidence: >1 motif fragment per minute in core absorptive work.
  - Revision target: Reduce to one fragment every 30–45 seconds.

- Failure mode: weak recurrence
  - Evidence: primary motif cell does not return in core absorptive work.
  - Revision target: Add transformed motif at 14:00 and 16:30.

- Failure mode: weak ending
  - Evidence: piece fades out without spectral narrowing.
  - Revision target: Add final carrier phrase dissolving into bed with spectral narrowing to 1700 Hz.

- Failure mode: weak spatial composition
  - Evidence: stereo width unchanged from beginning.
  - Revision target: Implement defined spatial arc.

- Failure mode: weak hypnotic architecture
  - Evidence: depth trajectory plateaus (no deepening in deepening stage).
  - Revision target: Adjust harmonic root to F2 with expanded stack and increase reverb.

- Failure mode: weak fractionation
  - Evidence: lightening abrupt or re-deepening not deeper.
  - Revision target: Make lightening gradual (30s) and re-deepening with wider field.

- Failure mode: weak anchors / triggers
  - Evidence: anchor returns at random times, not thresholds.
  - Revision target: Anchor must return only at specified thresholds.

- Failure mode: weak voice-adjacent induction
  - Evidence: carrier phrases <0.6s or >3.5s.
  - Revision target: Adjust phrase length to 0.6–3.5s with permissive pauses.

## Sketch Handoff

- Required sketch style family: neuro-ambient, voice-adjacent induction, generative ambient bed.
- Required formal units: 7 stages as defined in stage map.
- Required material to preserve: primary motif cell, harmonic signature (home/contrast/return), texture behavior.
- Required hypnotic architecture to preserve: stage map, depth trajectory, fractionation timing, anchor returns.
- Required voice-adjacent induction behavior to preserve: carrier layer (cello patch, GM94), phrase length 0.6–3.5s, contour falling_settling.
- Required development grammar to preserve: establishment, variation, contrast, recurrence rules.
- Required revision criteria to preserve: the list above.
- Explicit exclusions: no percussion, no bright bells, no abrupt transitions.