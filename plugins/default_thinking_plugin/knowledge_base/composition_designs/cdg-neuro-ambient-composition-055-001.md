# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-055-001
WORK_MANIFEST: wmf-neuro-ambient-composition-055-001
Status: ready

## Artistic Reading

- User image / premise: A continuous, breath-led hypnotic journey through seven stages of self-hypnosis using a voice-adjacent instrumental carrier.
- Musical problem: Create 24-minute continuous form that guides listeners through hypnotic stages without abrupt changes, using only prosodic lead as foreground element.
- Listener stance: Following the continuous lead without active concentration, in calm absorption.
- What this is not: Dramatic, sad narrative, triumphant, pop-hook-based, cinematic tension.
- Style-family fit: Structured Hypnotic Stage Architecture (7-stage) with voice-adjacent induction, fitting neuro-ambient style family.

## Chosen Musical Premise

- Premise sentence: A continuous, breath-length vocal-styled carrier guides the listener through seven hypnotic stages of self-hypnosis using harmonic shifts and spatial evolution to mark depth progression without interruption.
- Primary dramatic motion: Deepening (with brief fractionation) and return, through a continuous arc of widening + deepening + recognition.
- Sound-world metaphor: A slow-moving river that deepens and widens as it flows, with occasional gentle eddies (fractionation) before returning to its source.
- Musical consequence: The river metaphor changes harmony through slow harmonic region shifts (174.62 Hz to 220.00 Hz and back), texture through density modulation (sparse to full, then fractional lightening), register via carrier pitch descent in deepening stages, and space through stereo arc (narrow to wide to narrow).
- Duration rationale: 24 minutes earned by seven-stage architecture with durations reflecting hypnotic function (deepening requires 6 minutes for stability, core work 7 minutes for sustained absorption).

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for focused immersive listening.
- Stage map:
  - Orientation / Settling: 3:00, baseline-to-light depth, function: settle and establish safety.
  - Induction / Narrowing: 4:00, light-to-medium depth, function: pace and lead attention inward.
  - Deepening: 6:00, medium-to-deep depth, function: reduce orienting load.
  - Core Absorptive Work: 7:00, deep depth, function: sustain absorption.
  - Fractionation Pocket: 2:00, deep-to-light-medium-to-deep depth, function: briefly lighten then return deeper.
  - Integration / Stabilization: 1:30, medium-to-light depth, function: stabilize.
  - Return / Reorientation: 0:30, light-to-baseline depth, function: reorient.
- Depth trajectory: Baseline (orientation) → light (induction) → medium (deepening start) → deep (core work) → light-medium (fractionation lightening) → medium (integration start) → deep (fractionation return) → baseline (return). Measured by carrier pitch descent, harmonic density increase, and spatial widening.
- Pacing-and-leading plan: Carrier's breath-length phrases (6–14 seconds) pace attention inward during induction, with weighted stress on "settle" moments to lead deeper.
- Ratification plan: Carrier's downward settling contour (84.5% falling_settling) at phrase endings ratifies accepted state without words.
- Suggestion-compatible windows: 1–3 second inter-phrase breaths for spoken guidance insertion without masking.
- Reset avoidance rule: No abrupt changes; all transitions use slow harmonic shifts (3–5 seconds) and density modulation.

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Two-note interval cell (minor third, 3 semitones) followed by falling second (2 semitones), low register minor third → mid-register falling second.
- First form: Sparse, low-mid glint (0.5s) with 3-second rest in orientation.
- Later forms: Transposed up minor third in deepening; widened to stereo cross in core work; fragmented into single notes during fractionation.

### Material Inventory - Harmonic Signature
- Home color: Root (F2, 87.31 Hz) with interval stack [0, 3, 10] (F2, A2, D#3) at harmonic bed level.
- Contrast color: Root shifted up minor third to A2 (110.00 Hz) with interval stack [0, 3, 7] (A2, C#3, E4) for core work.
- Return color: Home color (F2) with interval stack [0, 3] (F2, A2), missing D#3 to soften.

### Material Inventory - Texture Family
- Continuous layer behavior: Substrate drone at 87.31 Hz with [1.0, 2.0, 3.0] partials and progressive gain reduction (amplitude target: 0.22).
- Event layer behavior: Primary motif cell appears as sparse events (every 15–45 seconds) in orientation/induction, increasing frequency in deepening.
- Noise / air behavior: Low-pass filtered noise (cutoff 3200 Hz) that widens slightly during deepening and contracts in return.

### Material Inventory - Spatial Material
- Width behavior: Starts narrow (mono), widens to medium in deepening, peaks wide in core work, contracts to medium in integration, narrows to mono in return.
- Motion behavior: Slow stereo drift (e.g., motif cell crosses left to right over 10 seconds), no fast autopan.
- Depth behavior: Reverb tail length increases in deepening (1.5s → 3.0s), decreases in return.

### Material Inventory - Modulation Identity
- Rate family: Long breath (0.07–0.13 Hz) for substrate/harmonic bed, asymmetric shimmer (multiple slow rates).
- Change rule: Modulation depth decreases in deepening and increases slightly during fractionation lightening.

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1` (6–14s phrases, 1–3s breaths).
- Reference pitch-contour profile: `reference_voice_pitch_contours_v1` (84.5% falling_settling, 3.6% scoop_then_settle).
- Carrier instrument / layer: Sarangi-like bowed string (warm, low-mid resonance) with formant filter to avoid vocal imitation.
- Cadence behavior: Breath-length phrases (6–14s) with permissive pauses; phrase ends have slight downward settling.
- Contour behavior: Primarily falling_settling (84.5%), with scoop_then_settle for fractionation lightening.
- Weighting behavior: Slight increase in sustain and low-mid warmth at phrase endings for emphasis.
- Return behavior: Carrier returns to orientation contour in integration with calmer register and wider spatial placement.
- Salience limit: Carrier 6 dB above harmonic bed, 10 dB above substrate (foreground_lead_at_least_6dB_above_harmonic_bed_and_10dB_above_substrate).

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Tiny spectral unsealing (0.2s) at phrase endings with 10Hz lowpass filter sweep.
- First pairing: In orientation, anchor appears at phrase end (after 2 minutes), paired with slight widening.
- Withholding plan: Anchor absent during induction (to build expectation).
- Return / trigger plan: Returns at 7:05 (deepening stability), 13:20 (core work threshold), 22:15 (integration ratification).
- Salience policy: Audible enough to learn (0.5 dB above bed), not salient enough to cause monitoring.

## Development Grammar

- Establishment rule: Introduce carrier and substrate in orientation; motif cell appears once.
- Variation rule: Motif transposes up minor third during deepening; harmonic bed shifts to contrast color in core work.
- Contrast rule: Home vs. contrast colors (F2 vs. A2) for core work; fractionation uses brighter harmonic region.
- Recurrence rule: Motif cell returns transformed (transposed, widened) in deepening/core work; anchor returns at key thresholds.
- Foreground rule: Carrier foreground in all stages (no exceptions); motif events subforeground.
- Withholding rule: Anchor withheld during induction; carrier's full pitch range withheld until deepening.
- Fractionation rule: During fractionation, harmonic bed brightens (cutoff 3200 Hz → 4500 Hz) and density drops for 60s, then returns deeper with wider spatial placement.
- Trigger conditioning rule: Anchor paired with widening/warmth in orientation; returns at deeper states to reinforce stability.
- Dissolution / cadence rule: In return, carrier pitch rises slightly (G3 → A3), spatial width narrows, reverb tail shortens.
- Anti-padding rule: No looped events; all material changes tied to development grammar.

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish safety and carrier timbre.
- Approximate duration range: 0:00–3:00
- Hypnotic stage role: orientation
- Target depth: baseline-to-light
- Material state: Substrate drone, sparse motif cell (1x), carrier enters.
- Development action: Establish
- Hypnotic function: settle
- Energy / density posture: sparse, low density
- Harmonic / spectral posture: home color (F2), 1900 Hz spectral arc
- Spatial posture: narrow (mono)
- Transition condition: Carrier phrase ends with downward settling.
- Motion source commitment: `foreground: voice_adjacent` (180s > 120s, required by mode).

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace attention inward.
- Approximate duration range: 3:00–7:00
- Hypnotic stage role: induction
- Target depth: light-to-medium
- Material state: Carrier pacing, motif cell every 30s.
- Development action: Vary (motif transposes up minor third)
- Hypnotic function: lead
- Energy / density posture: moderate, increasing density
- Harmonic / spectral posture: home color (F2), 2400 Hz spectral arc
- Spatial posture: narrow to medium width (slow drift)
- Transition condition: Carrier phrase ends with weighted stress on "settle".
- Motion source commitment: `foreground: voice_adjacent` (240s > 120s).

### Formal Unit 3 - Deepening
- Name / role: Reduce orienting load.
- Approximate duration range: 7:00–13:00
- Hypnotic stage role: deepening
- Target depth: medium-to-deep
- Material state: Full harmonic bed, carrier deepens (pitch descent), anchor appears.
- Development action: Thicken (harmonic bed full, carrier pitch lowers)
- Hypnotic function: ratify
- Energy / density posture: high, stable
- Harmonic / spectral posture: home color (F2), 3200 Hz spectral arc
- Spatial posture: wide, stable
- Transition condition: Anchor cue appears at 7:05 (deepening stability).
- Motion source commitment: `foreground: voice_adjacent` and `event_schedule: anchor_cue` (360s > 120s).

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain deep absorption.
- Approximate duration range: 13:00–20:00
- Hypnotic stage role: core work
- Target depth: deep
- Material state: Contrast color harmonic bed (A2), carrier continues as lead.
- Development action: Transform (harmonic region shift, motif widened)
- Hypnotic function: sustain
- Energy / density posture: high, stable
- Harmonic / spectral posture: contrast color (A2), 2600 Hz spectral arc
- Spatial posture: wide, stable
- Transition condition: Carrier phrase ends with downward settling at 20:00.
- Motion source commitment: `foreground: voice_adjacent` (420s > 120s).

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper.
- Approximate duration range: 20:00–22:00
- Hypnotic stage role: fractionation
- Target depth: deep-to-light-medium-to-deep
- Material state: Fractionation lightening (brighter bed, less density), then return.
- Development action: Answer (lighten and deepen)
- Hypnotic function: fractionate
- Energy / density posture: low (lightening), then high (return)
- Harmonic / spectral posture: contrast color brightened (A2, 4500 Hz cutoff), then return to 2600 Hz
- Spatial posture: wide (lightening) → wider (return)
- Transition condition: Fractionation lightening ends at 21:00.
- Motion source commitment: `foreground: voice_adjacent` (required by mode, 120s threshold).

### Formal Unit 6 - Integration / Stabilization
- Name / role: Stabilize after fractionation.
- Approximate duration range: 22:00–23:30
- Hypnotic stage role: integration
- Target depth: medium-to-light
- Material state: Home color harmonic bed returns, carrier reuses orientation contour.
- Development action: Recur (motif cell to first form)
- Hypnotic function: stabilize
- Energy / density posture: moderate, decreasing
- Harmonic / spectral posture: home color (F2), 1800 Hz spectral arc
- Spatial posture: medium width, contracting
- Transition condition: Carrier phrase ends with calm register at 23:30.
- Motion source commitment: `foreground: voice_adjacent` (required by mode).

### Formal Unit 7 - Return / Reorientation
- Name / role: Reorient attention gently.
- Approximate duration range: 23:30–24:00
- Hypnotic stage role: return
- Target depth: light-to-baseline
- Material state: Home color, carrier fades with upward settling.
- Development action: Dissolve (carrier pitch rises slightly)
- Hypnotic function: reorient
- Energy / density posture: low, fading
- Harmonic / spectral posture: home color (F2), 1800 Hz → 1900 Hz
- Spatial posture: narrow (mono)
- Transition condition: Final carrier phrase ends with upward settling.
- Motion source commitment: `foreground: voice_adjacent` (required by mode).

## Orchestration Map

- Root / ground function: Substrate drone (87.31 Hz, [1.0,2.0,3.0] partials).
- Harmonic color function: GM89 pad (harmonic bed) with interval stack.
- Motion function: Carrier's breath-length phrases and motif events.
- Foreground / event function: Carrier (foreground) and motif cell as subforeground events.
- Voice-adjacent induction function: Sarangi-like carrier with weighted cadence and downward settling.
- Anchor / trigger function: Spectral unsealing (0.2s) at phrase endings.
- Air / bloom function: Low-pass filtered noise layer (cutoff 3200 Hz).
- Spatial / depth function: Stereo width and reverb tail control.
- Silence / restraint function: 1–3 second inter-phrase breaths.

## Temporal Behavior

- Micro-event rate: Motif cell every 15–45 seconds (orientation: 30s, deepening: 20s).
- Scene-change rate: Every stage (7 stages over 24 minutes, ~3.5 min intervals).
- Long-wave change: Harmonic region shifts (home → contrast → home) and spectral arc (1900→2400→3200→2600→1800 Hz).
- Maximum stasis tolerance: 45 seconds without micro-event or harmonic shift.
- Repetition policy: Motif returns transformed; no identical repetition without change.
- Fractionation timing: Lightening 20:00–21:00 (60s), re-deepening 21:00–22:00 (60s).
- Anchor recurrence spacing: 7:05, 13:20, 22:15 (all within stage durations).

## Spatial Composition

- Stereo requirement: Stereo.
- Spatial arc: Narrow (mono) → narrow-medium (drift) → wide (stable) → wider (fractionation return) → medium (integration) → narrow (mono).
- Layer placement principles: Substrate centered, harmonic bed wide stable, carrier near center low motion, air on edges.
- Motion limits: Stereo movement over 10 seconds or more; no fast autopan.
- Verification obligation: Verify not mono, width arc audible and motion not alerting.

## Revision Criteria

- Failure mode: too static
  - Evidence: No micro-events or harmonic shifts for >45 seconds.
  - Revision target: Add motif cell events and harmonic region shift in deepening.

- Failure mode: too generic
  - Evidence: Same motif cell and harmonic bed throughout.
  - Revision target: Transform motif (transposition, widening) and use home/contrast colors.

- Failure mode: too busy
  - Evidence: Motif cell every 10 seconds, bright harmonic bed.
  - Revision target: Reduce motif rate to 25–40 seconds, lower spectral arc.

- Failure mode: weak recurrence
  - Evidence: Motif cell appears once and vanishes.
  - Revision target: Return motif transformed in deepening and core work.

- Failure mode: weak ending
  - Evidence: Simple fade-out without dissolution.
  - Revision target: Add carrier pitch rise and spatial contraction.

- Failure mode: weak spatial composition
  - Evidence: Output mono or width doesn't change.
  - Revision target: Implement stereo arc as designed.

- Failure mode: weak hypnotic architecture
  - Evidence: No clear stage progression, depth trajectory flat.
  - Revision target: Map harmonic bed shifts and spectral arc to stage depths.

- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset.
  - Revision target: Keep continuity bed under fractionation, use brighter harmonic region for lightening.

- Failure mode: weak anchors / triggers
  - Evidence: Anchor cue random or too obvious.
  - Revision target: Pair anchor with widening/warmth, withhold until deepening.

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier too bright or mechanical.
  - Revision target: Use sarangi-like carrier with falling_settling contour and permissive pauses.

## Sketch Handoff

- Required sketch style family: Structured Hypnotic Stage Architecture (7-stage).
- Required formal units: Orientation, Induction, Deepening, Core Work, Fractionation, Integration, Return.
- Required material to preserve: Primary motif (interval cell), harmonic signature (home/contrast/return colors), texture behavior, spatial material.
- Required hypnotic architecture to preserve: Stage map with durations and depth trajectory; fractionation lightening/deeper-return.
- Required voice-adjacent induction behavior to preserve: Carrier (sarangi-like), cadence (6–14s phrases, falling_settling), salience limit.
- Required development grammar to preserve: Establishment, Variation, Contrast, Recurrence rules.
- Required revision criteria to preserve: All listed above.
- Explicit exclusions: No percussion, no transient attacks, no abrupt changes.