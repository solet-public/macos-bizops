# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-003
WORK_MANIFEST: wmf-neuro-ambient-development-001
Status: ready

## Artistic Reading

- User image / premise: A 24-minute music-only self-hypnosis journey through evolving harmonic depth, guided by a voice-adjacent carrier and sequenced arpeggio with structural motion.
- Musical problem: Creating continuous hypnotic progression without spoken words, avoiding alerting cues while maintaining depth trajectory.
- Listener stance: Inward deep listening focused on internal absorption, not external monitoring.
- What this is not: Pop song structure, cinematic tension bed, EDM groove, new age piano feature, or hard percussion.
- Style-family fit: Directly fits Sequenced Ambient (Carbon Based Lifeforms, Solar Fields) with spectral color and process-and-phasing elements.

## Chosen Musical Premise

- Premise sentence: A continuous harmonic field deepens through moving bass roots and a voice-adjacent carrier, with subtle fractionation pockets that briefly lighten before returning to deeper absorption.
- Primary dramatic motion: Deepen and return with fractionation as a lightening-return cycle.
- Sound-world metaphor: A slow, deep ocean current that gently widens and contracts with the listener's breath.
- Musical consequence: Harmonic expansion (bass roots moving through regions), texture density shifts, carrier descent in deepening stages, and spatial widening/contraction.
- Duration rationale: 24 minutes allows full depth trajectory from baseline to deep (7-minute core) with sufficient time for each stage's development without rushing.

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for focused immersive listening.
- Stage map:
  - Orientation / Settling: 3:00, baseline to light, establish world and first carrier pairing.
  - Induction / Narrowing: 4:00, light to medium, pace and lead attention.
  - Deepening: 6:00, medium to deep, widen space and stabilize root memory.
  - Core Absorptive Work: 7:00, deep, sustain absorption with micro-developments.
  - Fractionation Pocket: 2:00, deep to light-medium to deep, briefly lighten then return deeper.
  - Integration / Stabilization: 1:30, medium to light, return familiar material in calmer form.
  - Return / Reorientation: 0:30, light to baseline, dissolve and reorient.
- Depth trajectory: Baseline (lightest) → Light → Medium → Deep → Light-Medium (fractionation) → Deep → Medium-Light → Baseline. Concrete parameters: Depth increases via harmonic expansion (more partials, wider intervals), spatial widening, and carrier descent; fractionation uses 10-25% spectral opening then darkening.
- Pacing-and-leading plan: Establish stable harmonic bed (Orientation), use carrier to pace attention inward via lengthening phrases and anchor pairing (Induction), then widen field to reduce orienting load (Deepening).
- Ratification plan: Carrier returns transformed (e.g., lower register, wider) at key depth points to ratify state without words.
- Suggestion-compatible windows: Only Fractionation Pocket (20:00-22:00) as natural pause for spoken guidance.
- Reset avoidance rule: No abrupt cuts, hard transients, alerting brightness spikes, or sudden density changes; all transitions use 12-second crossfades with slow filter automation.

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Two-note interval cell (minor third) followed by falling second, irregular spacing.
- First form: Sparse low-register (F2-G2) in Orientation with 1.5s phrase and 0.75s rest.
- Later forms: Transposed up minor third (G2-A#2) in Deepening; shortened to one note with brighter filter in Fractionation.

### Material Inventory - Harmonic Signature
- Home color: Root (F2) + fifth (C3) + minor seventh (E♭3) in 1:2:3 partial ratio, low gain on seventh.
- Contrast color: Root (G2) + second (A2) + fifth (D3), brighter open sound.
- Return color: Original home with seventh removed and partials softened (1.0, 2.0, 2.5).

### Material Inventory - Texture Family
- Continuous layer behavior: Low-mid drone with irregular spectral eddies (micro-events every 20-35s).
- Event layer behavior: Soft glints (1 per 45s), brief and non-metric.
- Noise / air behavior: Slow, wide air layer (10s attack) that opens during deepening and closes in return.

### Material Inventory - Spatial Material
- Width behavior: Narrow (0:00) → Medium (3:00) → Wide (7:00) → Medium-wide (13:00) → Wider during Fractionation lightening (20:00) → Medium (22:00) → Narrow (24:00).
- Motion behavior: Air layer drifts slowly left to right; carrier stays near center with minimal motion.
- Depth behavior: Reverb tail lengthens in Deepening (to 3s), shortens in Return.

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.1 Hz) with asymmetric shimmer (two incommensurable rates: 0.07 and 0.13 Hz).
- Change rule: Breath rate slows by 25% in Deepening, speeds up slightly during Fractionation.

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1.
- Reference pitch-contour profile: falling_settling for induction/deepening, scoop_then_settle for fractionation lightening.
- Carrier instrument / layer: Filtered cello patch (low-mid, warm) at -12 dB.
- Cadence behavior: Breath-length phrases (4-8s), downward settling in deepening stages.
- Contour behavior: Predominantly falling_settling; Fractionation lightening uses brief scoop_then_settle.
- Weighting behavior: Slight sustain increase and lower register emphasis at phrase ends.
- Return behavior: Transformed returns (wider, slower) to ratify depth or fractionation return.
- Salience limit: Carrier never exceeds -10 dB; always below foreground events.

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Soft low-mid glint (10ms burst at 2.5kHz).
- First pairing: Coincides with first carrier phrase ending in Orientation (settling moment).
- Withholding plan: Absent during Induction; returns in Deepening to ratify stability.
- Return / trigger plan: Returns during Fractionation lightening (as brief cue) and in Deepening return to signal deeper absorption.
- Salience policy: Anchor at -18 dB; audible but not monitored.

## Development Grammar

- Establishment rule: Introduce harmonic bed and carrier in Orientation with sparse material.
- Variation rule: Changes via register shift, partial softening, and spatial widening (not harmonic key change).
- Contrast rule: Only between home color (F2) and contrast color (G2), with clear return to home.
- Recurrence rule: Primary motif returns transformed; anchor recurs at depth thresholds.
- Foreground rule: Only carrier is foreground (events sub-salient below -15 dB).
- Withholding rule: Anchor withheld during Induction; motif absent in Integration.
- Fractionation rule: Brief lightening (brightness + density reduction for 30s), then return via darkening and widening.
- Trigger conditioning rule: Anchor paired with carrier phrase endings in Orientation, returns at deeper state thresholds.
- Dissolution / cadence rule: Final 30s must include spatial contraction, carrier register rise, and harmonic simplification (return color).
- Anti-padding rule: No looped events; all micro-events unique and spaced.

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish harmonic bed and first carrier pairing.
- Approximate duration range: 0:00 - 3:00
- Hypnotic stage role: Orientation / Settling
- Target depth: baseline to light
- Material state: Ground drone, harmonic bed (home color), carrier seed (primary motif first form).
- Development action: Establish.
- Hypnotic function: Settle and establish safety.
- Energy / density posture: Sparse (one carrier phrase per 2.5s).
- Harmonic / spectral posture: Home color, low brightness.
- Spatial posture: Narrow (centered).
- Transition condition: Carrier established and anchor first paired.

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace attention inward with carrier variations.
- Approximate duration range: 3:00 - 7:00
- Hypnotic stage role: Induction / Narrowing
- Target depth: light to medium
- Material state: Ground drone, harmonic bed (home color), carrier variants (longer phrases).
- Development action: Vary.
- Hypnotic function: Pace and lead attention.
- Energy / density posture: Moderate (carrier phrases every 3s).
- Harmonic / spectral posture: Home color, slight brightness increase.
- Spatial posture: Slightly wider (medium).
- Transition condition: Attention narrowed and space prepared to widen.

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory.
- Approximate duration range: 7:00 - 13:00
- Hypnotic stage role: Deepening
- Target depth: medium to deep
- Material state: Ground drone, harmonic bed (home color), air layer, carrier returns (lower register).
- Development action: Thicken and widen.
- Hypnotic function: Reduce orienting load.
- Energy / density posture: Moderate (air layer added).
- Harmonic / spectral posture: Home color, wider intervals.
- Spatial posture: Wide (medium-wide).
- Transition condition: Depth stable and core can withhold carrier.

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain deep absorption with micro-developments.
- Approximate duration range: 13:00 - 20:00
- Hypnotic stage role: Core Absorptive Work
- Target depth: deep
- Material state: Ground drone, harmonic bed (home color), rare carrier (transformed motif).
- Development action: Withhold and recur.
- Hypnotic function: Sustain absorption.
- Energy / density posture: Sparse (carrier every 5s).
- Harmonic / spectral posture: Home color, softened.
- Spatial posture: Wide (medium-wide).
- Transition condition: Field thinned and fractionation pre-echo seeded.

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper.
- Approximate duration range: 20:00 - 22:00
- Hypnotic stage role: Fractionation Pocket
- Target depth: deep to light-medium to deep
- Material state: Ground drone, harmonic bed (contrast color), brief brightness.
- Development action: Lighten and return deeper.
- Hypnotic function: Briefly lighten then return deeper.
- Energy / density posture: Light (density reduced).
- Harmonic / spectral posture: Contrast color, brighter.
- Spatial posture: Wider during lightening (medium-wide), contracts slightly for return.
- Transition condition: Brightness closed and deeper return established.

### Formal Unit 6 - Integration / Stabilization
- Name / role: Return familiar material in calmer form.
- Approximate duration range: 22:00 - 23:30
- Hypnotic stage role: Integration / Stabilization
- Target depth: medium to light
- Material state: Ground drone, harmonic bed (home color), clearer carrier.
- Development action: Recur and clarify.
- Hypnotic function: Stabilize.
- Energy / density posture: Moderate (carrier more present).
- Harmonic / spectral posture: Home color, slightly brighter.
- Spatial posture: Medium (contracting from wide).
- Transition condition: Fractionation redeepened.

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve and reorient.
- Approximate duration range: 23:30 - 24:00
- Hypnotic stage role: Return / Reorientation
- Target depth: light to baseline
- Material state: Ground residue, air fade.
- Development action: Dissolve.
- Hypnotic function: Reorient.
- Energy / density posture: Sparse (only air fade).
- Harmonic / spectral posture: Return color, darkened.
- Spatial posture: Narrow (contracting to center).
- Transition condition: Integration cadence prepared.

## Orchestration Map

- Root / ground function: Ground drone (F2, 87.31 Hz).
- Harmonic color function: Harmonic bed (partial stack, moving bass roots).
- Motion function: Arpeggio sequence (50 BPM) and modulation identity.
- Foreground / event function: Carrier layer (voice-adjacent).
- Voice-adjacent induction function: Carrier with weighted cadence.
- Anchor / trigger function: Subtle glint (nonverbal anchor).
- Air / bloom function: Noise layer with slow attack.
- Spatial / depth function: Stereo width and reverb tail.
- Silence / restraint function: Withholding of motif in Integration.

## Temporal Behavior

- Micro-event rate: One event every 20-35 seconds (e.g., soft glint).
- Scene-change rate: Every 6-7 minutes (at stage transitions).
- Long-wave change: Harmonic region shift from home to contrast and back.
- Maximum stasis tolerance: 45 seconds without change (e.g., in Core Absorptive Work).
- Repetition policy: Motif returns transformed; no identical loops.
- Fractionation timing: 30 seconds lightening (20:00-20:30), then 30s return to deep (20:30-21:00).
- Anchor recurrence spacing: Every 4 minutes (Deepening and Fractionation return).

## Spatial Composition

- Stereo requirement: Stereo.
- Spatial arc: Narrow → Medium → Wide → Medium-wide → Wider (Fractionation) → Medium → Narrow.
- Layer placement principles: Ground centered, harmonic bed wide stable, carrier near center low motion, air edges and rear.
- Motion limits: Air layer drifts slowly (left to right over 120s), no fast autopan.
- Verification obligation: Verify not mono, width arc audible, motion not alerting.

## Revision Criteria

- Failure mode: too static
  - Evidence: No perceptible change for >45s in Core Absorptive Work.
  - Revision target: Add micro-event (e.g., air layer opening) or slight harmonic shift.

- Failure mode: too generic
  - Evidence: Sounds like any ambient drone.
  - Revision target: Add specific motif (minor third interval cell) and anchor glint.

- Failure mode: weak recurrence
  - Evidence: Primary motif appears once and vanishes.
  - Revision target: Return motif in Deepening (transposed) and Fractionation (shortened).

- Failure mode: weak fractionation
  - Evidence: Lightening feels like a reset.
  - Revision target: Keep continuity bed under fractionation, return via darkening.

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier phrases are metrical and bright.
  - Revision target: Use falling_settling contour, breath-length phrases (4-8s), lower salience.

## Sketch Handoff

- Required sketch style family: Sequenced Ambient (Carbon Based Lifeforms, Solar Fields).
- Required formal units: 7 stages as defined.
- Required material to preserve: Primary motif (minor third interval cell), harmonic signature, texture behavior, spatial arc.
- Required hypnotic architecture to preserve: Stage map with depth trajectory and fractionation rule.
- Required voice-adjacent induction behavior to preserve: Carrier with reference_voice_cadence_profile_v1, falling_settling contour.
- Required development grammar to preserve: Recurrence rule (transformed returns), contrast rule, anti-padding.
- Required revision criteria to preserve: All listed in Revision Criteria section.
- Explicit exclusions: No percussion, no bright bells, no hard transients.