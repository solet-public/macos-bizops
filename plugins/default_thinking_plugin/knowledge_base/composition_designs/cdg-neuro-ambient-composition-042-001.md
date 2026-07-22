# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-042-001
WORK_MANIFEST: wmf-neuro-ambient-composition-042-001
Status: ready

## Artistic Reading

- User image / premise: Continuous instrumental lead voice guiding self-hypnosis through prosodic phrasing and layered ambient textures
- Musical problem: Creating hypnotic depth progression without abrupt transitions or attention-demanding elements in a 24-minute duration
- Listener stance: Following the continuous lead voice with relaxed absorption, not active monitoring
- What this is not: Pop hook-based composition, dramatic narrative, or cinematic tension piece
- Style-family fit: Hypnotic Stage Architecture with Foreground Vocal-Led Prosody mode (continuous lead voice)

## Chosen Musical Premise

- Premise sentence: A continuous sarangi-like carrier guides the listener through seven hypnotic stages using breath-length phrases and harmonic color shifts, with material returning transformed across the 24-minute duration
- Primary dramatic motion: Establish → deepen → fractionate → stabilize → dissolve
- Sound-world metaphor: A slow-moving river with seasonal changes in water clarity and depth, carrying a single consistent current
- Musical consequence: Harmonic color shifts (root intervals) and carrier pitch contour create depth progression; texture behavior evolves from sparse to dense then back to sparse
- Duration rationale: 24 minutes allows for seven distinct hypnotic stages with sufficient time for material transformation (3-7 minute sections) and micro-developments to earn the duration

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis with continuous vocal-styled lead
- Stage map:
  - Orientation: 0:00-3:00, baseline-to-light depth, establish safety and carrier
  - Induction: 3:00-7:00, light-to-medium depth, pace attention inward
  - Deepening: 7:00-13:00, medium-to-deep depth, reduce orienting load
  - Core absorptive work: 13:00-20:00, deep depth, sustain absorption
  - Fractionation: 20:00-22:00, deep-to-light-medium-to-deep depth, brief lightening then return
  - Integration: 22:00-23:30, medium-to-light depth, return to orientation motif
  - Return: 23:30-24:00, light-to-baseline depth, dissolve and reorient
- Depth trajectory: Spectral brightness (1800→3200→2600 Hz), harmonic density (low→medium→high→medium), carrier pitch range (F3-G#4 → E3-F#4)
- Pacing-and-leading plan: First establish sparse carrier with 12s phrases, then lengthen rests (0.5→1.8s) and lower register to narrow attention
- Ratification plan: Carrier's downward settling contour (84.5% falling_settling) at stage transitions confirms depth progression
- Suggestion-compatible windows: 12.5s after carrier phrase endings during deepening and core work
- Reset avoidance rule: No harmonic root changes >12 semitones, no brightness spikes above 3200 Hz

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: 3-note interval cell (minor third + major second) with falling contour
- First form: F3-A3-G#4 (orientation)
- Later forms: Transposed down a minor third for deepening, widened to F3-A3-C#4 in core work

### Material Inventory - Harmonic Signature
- Home color: Root F2 (87.31 Hz) with interval stack [0, 3, 10] (F2-A2-C#3)
- Contrast color: Root Bb2 (105.6 Hz) with interval stack [0, 5, 9] (Bb2-Eb3-A3)
- Return color: Root F2 with interval stack [0, 2, 7] (F2-A2-E3)

### Material Inventory - Texture Family
- Continuous layer behavior: Ground drone with 8-45s attack, 20-60s release
- Event layer behavior: Micro-events every 15-45s (soft glints, spectral shifts)
- Noise / air behavior: Air bloom with 10-60s attack, 20-90s release

### Material Inventory - Spatial Material
- Width behavior: Narrow (15°) → medium (45°) → wide (90°) → medium-wide (75°) → narrow (15°)
- Motion behavior: Slow drift left→right during deepening, static in core work
- Depth behavior: Near (0.5s reverb) → medium (1.2s) → wide (3.0s) → near (0.8s)

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) with asymmetric shimmer
- Change rule: Rate decreases by 25% at each stage transition

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: v1 empirical profile (6-14s phrases, 84.5% falling_settling)
- Reference pitch-contour profile: relaxation_return for deepening/core, fractionation for pocket
- Carrier instrument / layer: Sarangi-like bowed string (GM program 102)
- Cadence behavior: Breath-length phrases with permissive pauses
- Contour behavior: 84.5% falling_settling, 10-30s deepening arcs
- Weighting behavior: Slight register warmth at phrase endings
- Return behavior: Transformed motif returns in lower register during integration
- Salience limit: 6dB above harmonic bed, 10dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Low-mid warmth bloom (120-350 Hz)
- First pairing: Orientation phrase ending at 2:47 (F3-A3-G#4 settling)
- Withholding plan: Absent during induction, returns at deepening stability
- Return / trigger plan: Deepening stability (10:23), fractionation re-deepening (21:45)
- Salience policy: 3dB above substrate, no transient onset

## Development Grammar

- Establishment rule: Introduce motif cell with sparse carrier in orientation
- Variation rule: Transpose motif down a minor third at deepening stage
- Contrast rule: Shift harmonic root by 5 semitones between induction and deepening
- Recurrence rule: Motif returns transformed in integration (lower register, wider spacing)
- Foreground rule: Carrier remains foreground throughout all sections
- Withholding rule: Anchor absent during induction and fractionation lightening phase
- Fractionation rule: Lighten brightness (3200→1900 Hz) 20:00-21:00, re-deepen (1900→2600 Hz) 21:00-22:00
- Trigger conditioning rule: Anchor paired with carrier's downward settling at 10:23 deepening stability
- Dissolution / cadence rule: Final carrier phrase ends at 23:57 with spectral narrowing to 1700 Hz
- Anti-padding rule: No event repetition within 30s of previous occurrence

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish world and carrier pairing
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: Orientation
- Target depth: Baseline-to-light
- Material state: Ground, harmonic bed (home color), carrier seed
- Development action: Establish motif cell and carrier contour
- Hypnotic function: Settle and establish safety
- Energy / density posture: Sparse (1 event/30s)
- Harmonic / spectral posture: Home color, 1800 Hz
- Spatial posture: Narrow width (15°), near depth
- Transition condition: Carrier established and anchor first paired at 2:47
- Motion source commitment: foreground: voice_adjacent (continuous carrier)

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace attention inward
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: Induction
- Target depth: Light-to-medium
- Material state: Ground, harmonic bed (contrast color), carrier variants
- Development action: Vary motif contour and lengthen rests
- Hypnotic function: Pace attention inward
- Energy / density posture: Medium-sparse (1 event/20s)
- Harmonic / spectral posture: Contrast color, 3200 Hz
- Spatial posture: Medium width (45°), medium depth
- Transition condition: Attention narrowed and space prepared to widen at 6:58
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: Deepening
- Target depth: Medium-to-deep
- Material state: Ground, harmonic bed (home color), air, low carrier returns
- Development action: Thicken texture and lower register
- Hypnotic function: Reduce orienting load
- Energy / density posture: Medium (1 event/15s)
- Harmonic / spectral posture: Home color, 2600 Hz
- Spatial posture: Wide (90°), deep (3.0s reverb)
- Transition condition: Depth stable and core can withhold carrier at 12:59
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with microdevelopment
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: Core absorptive work
- Target depth: Deep
- Material state: Ground, harmonic bed (home color), rare carrier, anchor reassurance
- Development action: Withhold anchor and vary motif fragment
- Hypnotic function: Sustain absorption
- Energy / density posture: Dense (1 event/10s)
- Harmonic / spectral posture: Home color, 2400 Hz
- Spatial posture: Medium-wide (75°), deep (2.5s reverb)
- Transition condition: Field thinned and fractionation pre-echo seeded at 19:58
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: Fractionation
- Target depth: Deep-to-light-medium-to-deep
- Material state: Ground, harmonic bed (contrast color), brief brightness, carrier clarity
- Development action: Lighten and re-deepen without reset
- Hypnotic function: Brief lightening then deeper return
- Energy / density posture: Medium (1 event/20s)
- Harmonic / spectral posture: Contrast color, 1900 Hz (lightening), 2600 Hz (re-deepening)
- Spatial posture: Medium-wide (75°), medium depth
- Transition condition: Brightness closed and deeper return established at 21:59
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 6 - Integration / Stabilization
- Name / role: Return to orientation motif in calmer form
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: Integration
- Target depth: Medium-to-light
- Material state: Ground, harmonic bed (return color), clearer carrier
- Development action: Transform motif to orientation form
- Hypnotic function: Return to familiar material
- Energy / density posture: Sparse (1 event/30s)
- Harmonic / spectral posture: Return color, 2200 Hz
- Spatial posture: Medium (45°), medium depth
- Transition condition: Final carrier return prepares cadence at 23:28
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve and cadence
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: Return
- Target depth: Light-to-baseline
- Material state: Ground residue, air fade
- Development action: Dissolve carrier and spatial field
- Hypnotic function: Reorient attention gently
- Energy / density posture: Very sparse (1 event/60s)
- Harmonic / spectral posture: Return color, 1700 Hz
- Spatial posture: Narrow (15°), near depth
- Transition condition: Final public delivery tail complete at 24:00
- Motion source commitment: foreground: voice_adjacent

## Orchestration Map

- Root / ground function: F2 drone (87.31 Hz) with 45s release
- Harmonic color function: Interval stack shifts (home→contrast→return)
- Motion function: Carrier's breath-length phrases
- Foreground / event function: Micro-events (glints, spectral shifts)
- Voice-adjacent induction function: Sarangi carrier with 84.5% falling_settling contour
- Anchor / trigger function: Low-mid warmth bloom (120-350 Hz)
- Air / bloom function: 90s release air layer
- Spatial / depth function: Stereo width arc (narrow→wide→narrow)
- Silence / restraint function: Anchor withheld during induction

## Temporal Behavior

- Micro-event rate: Every 15-45 seconds (glints, spectral shifts)
- Scene-change rate: Every 75-150 seconds (harmonic region changes)
- Long-wave change: Spectral brightness arc (1800→3200→2600 Hz)
- Maximum stasis tolerance: 45 seconds without micro-event
- Repetition policy: Motif returns transformed, not identical
- Fractionation timing: Lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: First pairing at 2:47, deepening stability at 10:23, fractionation re-deepening at 21:45

## Spatial Composition

- Stereo requirement: Stereo
- Spatial arc: Narrow (15°) → medium (45°) → wide (90°) → medium-wide (75°) → narrow (15°)
- Layer placement principles: Ground centered, harmonic bed wide-stable, carrier near-center low-motion
- Motion limits: No autopan over 20s duration; slow drift only during deepening
- Verification obligation: Confirm not mono, width arc audible, motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No micro-events for >45s in core work section
  - Revision target: Add one spectral shift event every 30s

- Failure mode: weak fractionation
  - Evidence: Lightening phase (20:00-21:00) feels like new section
  - Revision target: Reduce brightness to 1900 Hz, maintain harmonic bed continuity

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier phrases too short (3s) without breath-length pacing
  - Revision target: Extend phrase arcs to 6-14s with permissive pauses

- Failure mode: weak hypnotic architecture
  - Evidence: No depth trajectory visible in harmonic color shifts
  - Revision target: Enforce interval stack changes per stage (home→contrast→return)

- Failure mode: weak ending
  - Evidence: Final fade without spectral narrowing to 1700 Hz
  - Revision target: Add dissolution phase (23:50-24:00) narrowing to 1700 Hz

## Sketch Handoff

- Required sketch style family: Hypnotic Stage Architecture with Foreground Vocal-Led Prosody
- Required formal units: Seven stages (orientation to return) with exact duration ranges
- Required material to preserve: Primary motif cell, harmonic signature (home/contrast/return), carrier contour profile
- Required hypnotic architecture to preserve: Stage map, depth trajectory, fractionation timing (20:00-21:00 lightening)
- Required voice-adjacent induction behavior to preserve: 84.5% falling_settling contour, continuous carrier presence
- Required development grammar to preserve: Variation rule (motif transposition), Fractionation rule (lighten→re-deepen)
- Required revision criteria to preserve: All five failure modes and targets
- Explicit exclusions: No percussion, no abrupt transitions, no foreground pop melody