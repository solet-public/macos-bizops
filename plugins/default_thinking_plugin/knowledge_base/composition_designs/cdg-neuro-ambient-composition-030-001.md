# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-030-001
WORK_MANIFEST: wmf-neuro-ambient-composition-030-001
Status: ready

## Artistic Reading

- User image / premise: A continuous, breath-shaped instrumental lead guiding a 24-minute self-hypnosis journey without vocal narration.
- Musical problem: Creating hypnotic continuity through instrumental prosody while avoiding melodic or rhythmic distraction.
- Listener stance: Following the lead's breath-length phrases without active concentration, absorbing into a field of sound.
- What this is not: A vocal track, pop melody, rhythmic groove, or abrupt transition piece.
- Style-family fit: Fits "sequenced ambient" (Carbon Based Lifeforms/Solar Fields) with continuous texture, harmonic motion without pulse, and voice-adjacent carrier.

## Chosen Musical Premise

- Premise sentence: A breath-shaped instrumental lead guides the listener through harmonic and spatial depth via falling-settling contour, with fractionation as a gentle lightening that returns deeper.
- Primary dramatic motion: Establish → Deepen → Fractionate (lighten + return) → Stabilize → Dissolve.
- Sound-world metaphor: A slow-moving river deepening into a calm pool, with ripples (fractionation) that return to stillness.
- Musical consequence: The river metaphor changes harmony through slow harmonic regions (home/contrast/return colors), rhythm via breath-length phrases, texture through continuous substrate with sparse events, register by lowering the lead's pitch over time, and space via widening/narrowing stereo arc.
- Duration rationale: 24 minutes allows full hypnotic progression (3+4+6+7+2+1.5+0.5=24) with time for absorption and gentle return without rushing.

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for deep absorption and gentle return.
- Stage map:
  - Orientation (0:00-3:00): baseline to light, settle and establish safety.
  - Induction (3:00-7:00): light to medium, pace and lead attention inward.
  - Deepening (7:00-13:00): medium to deep, reduce orienting load and increase continuity pressure.
  - Core Absorptive Work (13:00-20:00): deep, sustain absorption without dead stasis.
  - Fractionation Pocket (20:00-22:00): deep to light-medium to deep, briefly lighten then return deeper.
  - Integration (22:00-23:30): medium to light, stabilize and reduce depth pressure.
  - Return (23:30-24:00): light to baseline, reorient without abrupt arousal.
- Depth trajectory: Baseline (orientation) → Light (induction) → Medium (deepening) → Deep (core work) → Brief lightening (fractionation) → Deeper return (fractionation) → Light-medium (integration) → Baseline (return). Measured by lead register, harmonic density, and spatial width.
- Pacing-and-leading plan: Lead's phrase cadence (6–14s) and contour (84.5% falling-settling) pace attention inward during induction; longer phrases and deeper register in deepening.
- Ratification plan: Lead returns with transformed contour (e.g., wider interval, slower decay) at stage transitions to ratify the accepted state.
- Suggestion-compatible windows: Quiet moments between lead phrases (1–3s rests) where spoken guidance could sit without masking.
- Reset avoidance rule: No abrupt harmonic shifts, no hard transients, no sudden brightness spikes; transitions use slow filter sweeps (15–30s).

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: 3-note interval cell (C-E-G) with falling contour, where third note is always lower than second.
- First form: Orientation (0:00–3:00) – single soft swell (C-E-G), 8s phrase with 2s rest.
- Later forms: Deepening (7:00–13:00) – 4-note cell (C-E-G-D), wider interval; Core Work (13:00–20:00) – 5-note cell (C-E-G-Bb), lower register.

### Material Inventory - Harmonic Signature
- Home color: C2 root + [0, 3, 7] interval stack (C-E-G), pad with slow filter sweep.
- Contrast color: F2 root + [0, 3, 7] interval stack (F-A-C), introduced in deepening.
- Return color: C2 root + [0, 3] (C-E), used in integration and return.

### Material Inventory - Texture Family
- Continuous layer behavior: Low breathy pad (C2) with slow attack (10s), release (30s).
- Event layer behavior: Sparse motif fragments (1 per 20–45s), appearing only in core work and fractionation.
- Noise / air behavior: High-frequency noise band (10kHz+) with slow amplitude modulation (15s period).

### Material Inventory - Spatial Material
- Width behavior: Narrow (10%) → Wide (50%) → Narrow (25%).
- Motion behavior: Lead moves slowly left-to-right over first 10 minutes, then centered.
- Depth behavior: Reverb tail lengthens (1.5s → 3s) in core work, shortens for return.

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.1 Hz) for pad, asymmetric shimmer (0.3 Hz).
- Change rule: Breath rate slows from 0.15 to 0.08 Hz; shimmer rate increases from 0.2 to 0.4 Hz in deepening.

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1` (empirical).
- Reference pitch-contour profile: `reference_voice_pitch_contours_v1` (84.5% falling-settling).
- Carrier instrument / layer: Formant synth (GM program 92), low-mid resonance.
- Cadence behavior: 6–14s phrases with 1–3s rests, per empirical profile.
- Contour behavior: 84.5% falling-settling; small rise (scoop) in 10% of phrases.
- Weighting behavior: Slight sustain increase at phrase midpoint (10% longer on second note).
- Return behavior: Transformed contour (wider interval, slower decay) at stage transitions.
- Salience limit: Lead 6dB above harmonic bed, 10dB above substrate.

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Soft high-frequency glint (12kHz) at phrase end.
- First pairing: Orientation – anchor appears after first settling contour, paired with slight widening.
- Withholding plan: Absent during induction (first 2 minutes), then appears once per phrase.
- Return / trigger plan: Returns at 7:30 (deepening), 15:00 (core work), and 20:30 (fractionation return).
- Salience policy: Anchor at -15dB relative to lead, audible but not attention-grabbing.

## Development Grammar

- Establishment rule: Home color and motif cell established in orientation.
- Variation rule: Motif cell transforms by adding notes (3→4→5) and lowering register.
- Contrast rule: Contrast color introduced in deepening for widening without jarring shift.
- Recurrence rule: Motif cell returns transformed in every stage (e.g., core work uses 4-note version).
- Foreground rule: Lead is foreground in every section; bed remains subdued.
- Withholding rule: Anchor withheld for 2 minutes in induction to make return meaningful.
- Fractionation rule: Lightening (wider stereo, brighter high end) for 45s at 20:00; return deeper via slower motif cell.
- Trigger conditioning rule: Anchor paired with settling (downward contour) so return evokes comfort.
- Dissolution / cadence rule: Slow reduction in motif density and stereo width over last 30 seconds.
- Anti-padding rule: No material repeated merely to fill time; every event has purpose.

## Formal Architecture

### Formal Unit 1 - Orientation
- Name / role: Settling and safety establishment.
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: Orientation.
- Target depth: Baseline to light.
- Material state: Home color (C2), motif cell introduced, continuous pad present.
- Development action: Establish home color and lead with first motif form.
- Hypnotic function: Settle and establish safety.
- Energy / density posture: Low, sparse events (1 per 30s).
- Harmonic / spectral posture: Home color (C2, [0,3,7]).
- Spatial posture: Narrow width (10%), centered.
- Transition condition: Lead contour becomes regular; motif cell appears twice in a row.
- Motion source commitment: `foreground: voice_adjacent` (continuous lead).

### Formal Unit 2 - Induction
- Name / role: Attention narrowing.
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: Induction.
- Target depth: Light to medium.
- Material state: Home color continues, motif cell appears more frequently (1 per 20s), anchor withheld.
- Development action: Vary motif cell; introduce slight harmonic shift (C2 to C#2).
- Hypnotic function: Pace and lead attention inward.
- Energy / density posture: Medium, events (1 per 15s).
- Harmonic / spectral posture: Home color with C#2 shift mid-unit.
- Spatial posture: Width widens slightly (15%).
- Transition condition: Lead phrase length consistent; motif cell appears with downward contour.
- Motion source commitment: `foreground: voice_adjacent`.

### Formal Unit 3 - Deepening
- Name / role: Reducing orienting load.
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: Deepening.
- Target depth: Medium to deep.
- Material state: Contrast color (F2) introduced, motif cell transforms to 4-note, anchor appears.
- Development action: Introduce contrast color; use anchor for ratification.
- Hypnotic function: Reduce orienting load, increase continuity pressure.
- Energy / density posture: Medium-high, anchor events (1 per 30s).
- Harmonic / spectral posture: Contrast color (F2, [0,3,7]) with home in background.
- Spatial posture: Width widens to 50%, lead moves left-to-right.
- Transition condition: Anchor appears twice; motif cell slows (10s phrases).
- Motion source commitment: `foreground: voice_adjacent` and `parameter_group_sequence`.

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustaining deep absorption.
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: Core Absorptive Work.
- Target depth: Deep.
- Material state: Contrast color continues, motif cell 4-note lower register, anchor regular.
- Development action: Sustain deep state with transformed motif cell and anchors.
- Hypnotic function: Sustain absorption without dead stasis.
- Energy / density posture: High, events (1 per 20s).
- Harmonic / spectral posture: Contrast color with home in background.
- Spatial posture: Width 50%, lead centered, reverb tail long (3s).
- Transition condition: Motif cell slows; anchor appears with deeper register.
- Motion source commitment: `foreground: voice_adjacent` and `event_schedule`.

### Formal Unit 5 - Fractionation Pocket
- Name / role: Brief lightening and deeper return.
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: Fractionation.
- Target depth: Deep to light-medium to deep.
- Material state: Lightening (wider stereo, brighter high end), then return deeper.
- Development action: Lighten texture for 45s; return deeper with transformed motif cell.
- Hypnotic function: Briefly lighten then return deeper without reset.
- Energy / density posture: Light (lightening), deep (return).
- Harmonic / spectral posture: Home color with brighter filter (2400Hz) for lightening.
- Spatial posture: Width widens to 70% (lightening), narrows to 50%.
- Transition condition: Lead contour brighter; motif cell wider interval.
- Motion source commitment: `foreground: voice_adjacent` and `parameter_group_sequence`.

### Formal Unit 6 - Integration
- Name / role: Stabilizing and reducing depth pressure.
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: Integration.
- Target depth: Medium to light.
- Material state: Return color (C2, [0,3]) introduced, motif cell returns to 3-note softer.
- Development action: Return home color; soften motif cell; reduce density.
- Hypnotic function: Stabilize without increasing depth pressure.
- Energy / density posture: Medium-low, events (1 per 45s).
- Harmonic / spectral posture: Return color (C2, [0,3]).
- Spatial posture: Width narrows to 25%, reverb tail shortens (1.5s).
- Transition condition: Motif cell shorter; lead contour more level.
- Motion source commitment: `foreground: voice_adjacent`.

### Formal Unit 7 - Return
- Name / role: Gentle reorientation.
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: Return.
- Target depth: Light to baseline.
- Material state: Home color (C2, [0,3]), minimal events; lead fades.
- Development action: Dissolve motif cell; narrow stereo field to baseline.
- Hypnotic function: Reorient without abrupt arousal.
- Energy / density posture: Low, no events last 15s.
- Harmonic / spectral posture: Home color with filter sweep to 1700Hz.
- Spatial posture: Width narrows to 5%, reverb tail disappears.
- Transition condition: Phrase length longer (12s); contour level.
- Motion source commitment: `foreground: voice_adjacent`.

## Orchestration Map

- Root / ground function: Continuous pad (C2) grounds the listener.
- Harmonic color function: Harmonic bed supplies pitch identity via interval stack [0,3,7].
- Motion function: Moving bass root (C2 to F2) makes time perceptible without groove.
- Foreground / event function: Lead carries foreground events (motif cell).
- Voice-adjacent induction function: Formant synth (GM 92) carries hypnotist-like cadence.
- Anchor / trigger function: High-frequency glint (12kHz) is the anchor, appearing at phrase ends.
- Air / bloom function: High-frequency noise band (10kHz+) creates openness and shimmer.
- Spatial / depth function: Stereo width and reverb tail carry spatial dimension.
- Silence / restraint function: 1–3s rests between lead phrases are the silence.

## Temporal Behavior

- Micro-event rate: One event (motif fragment or anchor) every 15–45 seconds.
- Scene-change rate: Every 3–7 minutes (stage changes).
- Long-wave change: Harmonic region shifts from home to contrast and back.
- Maximum stasis tolerance: 45 seconds without meaningful change (e.g., no event, harmonic shift).
- Repetition policy: Motif cell returns transformed; identical repetition avoided.
- Fractionation timing: Lightening at 20:00 for 45s (until 20:45), return deeper.
- Anchor recurrence spacing: Anchors at 7:30, 15:00, and 20:30 (all within stage).

## Spatial Composition

- Stereo requirement: Stereo.
- Spatial arc: Narrow (10%) → Wide (50%) → Narrow (25%) → Very narrow (5%).
- Layer placement principles: Ground centered; harmonic bed wide stable; carrier near center with slow motion; air at edges/rear; anchor local but soft.
- Motion limits: No fast autopan; motion slow and smooth (over 10+ minutes).
- Verification obligation: Must be verified as not mono, with clear width arc and non-alerting motion.

## Revision Criteria

- Failure mode: too static
  - Evidence: No harmonic shift, no motif transformation.
  - Revision target: Add harmonic region change (home to contrast) and transform motif cell.

- Failure mode: too generic
  - Evidence: Sounds like any ambient track, no specific motif.
  - Revision target: Define unique interval cell (C-E-G) and its transformations.

- Failure mode: too busy
  - Evidence: More than one event per 15 seconds.
  - Revision target: Reduce events to one every 20–30 seconds.

- Failure mode: weak recurrence
  - Evidence: Motif cell appears once and vanishes.
  - Revision target: Transform motif cell in every stage.

- Failure mode: weak ending
  - Evidence: Piece fades out without cadence.
  - Revision target: Add slow reduction in motif density and stereo width over last 30 seconds.

- Failure mode: weak spatial composition
  - Evidence: Output mono or width doesn't change.
  - Revision target: Implement spatial arc (narrow to wide to narrow).

- Failure mode: weak hypnotic architecture
  - Evidence: Stages don't progress (e.g., no deepening).
  - Revision target: Ensure each stage has clear depth progression.

- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset.
  - Revision target: Make lightening brief (45s) and ensure return deeper.

- Failure mode: weak anchors / triggers
  - Evidence: Anchor appears randomly or too often.
  - Revision target: Restrict anchor to specific stages (deepening, core work) at 15-minute intervals.

- Failure mode: weak voice-adjacent induction
  - Evidence: Lead too bright or mechanical.
  - Revision target: Use empirical profile (84.5% falling-settling) and ensure lead 6dB above bed.

## Sketch Handoff

- Required sketch style family: sequenced_ambient.
- Required formal units: 7 (as defined in Formal Architecture).
- Required material to preserve: Primary motif cell, harmonic signature colors, texture behavior (continuous layer with sparse events), spatial material (width arc).
- Required hypnotic architecture to preserve: Stage map, depth trajectory, fractionation timing.
- Required voice-adjacent induction behavior to preserve: Carrier (formant synth, GM 92), phrase length (6–14s), contour (84.5% falling-settling).
- Required development grammar to preserve: Recurrence rule, contrast rule, fractionation rule.
- Required revision criteria to preserve: All listed in Revision Criteria.
- Explicit exclusions: No percussion, no hard transients, no abrupt transitions.