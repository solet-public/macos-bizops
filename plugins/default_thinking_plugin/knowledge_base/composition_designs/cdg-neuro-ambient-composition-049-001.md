# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-049-001
WORK_MANIFEST: wmf-neuro-ambient-composition-049-001
Status: ready

## Artistic Reading

- User image / premise: A continuous, breath-led instrumental voice guides the listener through a self-hypnotic journey of deepening absorption and gentle return
- Musical problem: Creating non-intrusive vocal-styled carrier that guides without demanding attention while maintaining harmonic and textural interest over 24 minutes
- Listener stance: Following the continuous lead without active concentration, allowing absorption to deepen naturally
- What this is not: Dramatic, pop-hook-based, cinematic tension, or a lead vocal that becomes the focus
- Style-family fit: Neuro-ambient with voice-adjacent induction (Carbon Based Lifeforms/Solar Fields influence), using instrumental carrier for hypnotic pacing

## Chosen Musical Premise

- Premise sentence: A sarangi-like carrier layer guides the listener through a 24-minute absorption journey, using breath-length phrases and subtle harmonic shifts to mark hypnotic stage transitions without interruption
- Primary dramatic motion: Establish → deepen → fractionate → stabilize → dissolve (with return)
- Sound-world metaphor: A slow, deepening river that occasionally widens to a calm lake before flowing back into the current
- Musical consequence: The river's flow (carrier) remains constant, but its depth and width (harmonic density, spatial posture) change to mark stages. The riverbed (harmonic bed) shifts color subtly as the journey progresses
- Duration rationale: 24 minutes allows natural progression through hypnotic stages with each stage's duration matching time needed for depth target (e.g., 6 minutes deepening allows gradual absorption)

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for focused immersive listening
- Stage map:
  - Orientation / Settling: 3:00, baseline-to-light depth, function=settle and establish safety
  - Induction / Narrowing: 4:00, light-to-medium depth, function=pace and lead attention inward
  - Deepening: 6:00, medium-to-deep depth, function=reduce orienting load
  - Core Absorptive Work: 7:00, deep depth, function=sustain absorption
  - Fractionation Pocket: 2:00, deep-to-light-medium-to-deep depth, function=briefly lighten then return deeper
  - Integration / Stabilization: 1:30, medium-to-light depth, function=stabilize
  - Return / Reorientation: 0:30, light-to-baseline depth, function=reorient
- Depth trajectory: Baseline → light (orientation) → medium (induction) → deep (deepening/core) → brief light-medium (fractionation) → medium-light (integration) → baseline (return). Achieved through carrier register descent, harmonic density increase, and spatial widening
- Pacing-and-leading plan: Carrier begins with 10-second phrase arcs (orientation), shortens to 7 seconds (induction) for pacing, then lengthens to 12 seconds in deepening. Downward settling (84.5% falling-settling contour) guides depth
- Ratification plan: Carrier's return to orientation motif at deepening threshold (7:00) and core absorption (13:00) marks acceptance of depth
- Suggestion-compatible windows: 1.5-second pauses between carrier phrases in core (13:00-20:00) and integration (22:00-23:30)
- Reset avoidance rule: No abrupt changes; all transitions use slow harmonic shifts (15-30s) and avoid bright transients

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: 3-note interval cell (C4-Eb4-G4) with falling contour
- First form: Introduced at C4-C5 in orientation (10-second phrase arc, soft attack)
- Later forms: Deepening transposes to Bb3-D4-F4; fractionation brightens to C#5-E5-G#5

### Material Inventory - Harmonic Signature
- Home color: F2 root with interval stack [0,3,10] (F-A-C#) — warm pad in GM 89
- Contrast color: Bb2 root with interval stack [0,4,7] (Bb-D-F) — used in deepening
- Return color: F2 root with interval stack [0,3,10] but C# lowered to C (F-A-C) — used in integration

### Material Inventory - Texture Family
- Continuous layer behavior: Pink noise high-passed at 2800 Hz, filtered to recessed upper detail (river current)
- Event layer behavior: Micro-events every 20-35 seconds (single high-frequency glint, non-intrusive)
- Noise / air behavior: Air layer widens and softens during deepening, contracts in return

### Material Inventory - Spatial Material
- Width behavior: Narrow (0:00) → medium (3:00) → wide (7:00) → medium-wide (13:00) → narrow (24:00)
- Motion behavior: Carrier near center; air moves slowly from edges to rear in deepening
- Depth behavior: Carrier at medium depth (slightly forward); air recedes to rear in deepening

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.1 Hz) for harmonic bed, secondary shimmer at 0.3 Hz in air layer
- Change rule: Breath rate slows by 15% during deepening; shimmer becomes more asymmetric

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1` (6-14s phrases)
- Reference pitch-contour profile: `reference_voice_pitch_contours_v1` (84.5% falling-settling)
- Carrier instrument / layer: Sarangi-like bowed string (soft, low-mid resonance) at C4-C5
- Cadence behavior: Breath-length phrases (6-14s) with 1.5s inter-phrase pauses; downward settling in deepening
- Contour behavior: 84.5% falling-settling, with arches (3.6%) in fractionation
- Weighting behavior: Slight emphasis on second note of each phrase (e.g., E in C-E-G)
- Return behavior: Orientation motif returns at 7:00 (deepening threshold) and 13:00 (core absorption)
- Salience limit: 6dB above harmonic bed, 10dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Micro-filter shift (lowpass cutoff rising 50Hz over 2s) at carrier phrase end
- First pairing: Orientation (at 2:45), paired with carrier settling and bed widening
- Withholding plan: Anchor absent in induction (builds expectation)
- Return / trigger plan: Returns at 7:00, 13:00 (core absorption), and 22:00 (fractionation re-deepening)
- Salience policy: Audible but not noticeable until learned (0.5dB above bed)

## Development Grammar

- Establishment rule: Introduce carrier and harmonic bed with minimal movement (orientation)
- Variation rule: Transform primary motif by register, density, and spatial placement
- Contrast rule: Use harmonic contrast (F2 vs Bb2) to mark deepening without abruptness
- Recurrence rule: Primary motif returns at 7:00, 13:00, and 22:00 with transformation
- Foreground rule: Carrier foreground in all sections (no exceptions)
- Withholding rule: Anchor withheld during induction to build meaning
- Fractionation rule: Brief upward scoop (carrier brightens, spatial widens) for 15s at 20:15, then return to deeper state
- Trigger conditioning rule: Anchor paired with carrier phrase settling; returns at deepening threshold and core absorption
- Dissolution / cadence rule: Final fade uses carrier's return to orientation motif, air layer fading out
- Anti-padding rule: No looped events; all micro-events unique and sparse

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish the river's flow and bed
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline-to-light
- Material state: carrier, harmonic bed (F2), air (subtle)
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: low, sparse
- Harmonic / spectral posture: F2 home color (warm pad)
- Spatial posture: narrow, centered
- Transition condition: carrier established and anchor first paired (at 2:45)
- Motion source commitment: `foreground: voice_adjacent` (required by mode), `parameter_group_sequence`: F2 home color → F2 home color

### Formal Unit 2 - Induction / Narrowing
- Name / role: Guide attention inward
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light-to-medium
- Material state: carrier, harmonic bed (F2), air (wider)
- Development action: vary
- Hypnotic function: pace and lead attention inward
- Energy / density posture: medium, sparse events
- Harmonic / spectral posture: F2 home color (slight density increase)
- Spatial posture: medium, carrier near center
- Transition condition: attention narrowed and space prepared to widen (at 6:45)
- Motion source commitment: `foreground: voice_adjacent`, `event_schedule`: micro-event at 4:15, 5:30

### Formal Unit 3 - Deepening
- Name / role: Widen the river and deepen absorption
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium-to-deep
- Material state: carrier, harmonic bed (F2 → Bb2 contrast), air (wide)
- Development action: thicken, vary
- Hypnotic function: reduce orienting load and stabilize root memory
- Energy / density posture: medium-high, with micro-events
- Harmonic / spectral posture: F2 home color → Bb2 contrast (over 30s)
- Spatial posture: wide, carrier near center
- Transition condition: depth stable and core can withhold carrier (at 12:45)
- Motion source commitment: `foreground: voice_adjacent`, `parameter_group_sequence`: F2 home color → Bb2 contrast

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain deep absorption
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: carrier, harmonic bed (Bb2), air (wide)
- Development action: sustain
- Hypnotic function: sustain absorption with non-inert microdevelopment
- Energy / density posture: high, sparse events
- Harmonic / spectral posture: Bb2 contrast color (stable)
- Spatial posture: wide, carrier near center
- Transition condition: field thinned and fractionation pre-echo seeded (at 19:45)
- Motion source commitment: `foreground: voice_adjacent`

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten and return deeper
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep-to-light-medium-to-deep
- Material state: carrier, harmonic bed (Bb2 → F2 return), air (wider)
- Development action: fractionate
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: medium, with brief brightness
- Harmonic / spectral posture: Bb2 contrast → F2 return color (over 15s)
- Spatial posture: wide, carrier near center
- Transition condition: brightness closed and deeper return established (at 21:45)
- Motion source commitment: `foreground: voice_adjacent` (carrier handles lightening via contour change)

### Formal Unit 6 - Integration / Stabilization
- Name / role: Return to core with calmer form
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium-to-light
- Material state: carrier, harmonic bed (F2 return), air (contracting)
- Development action: recur
- Hypnotic function: stabilize and return familiar material
- Energy / density posture: medium, sparse events
- Harmonic / spectral posture: F2 return color (stable)
- Spatial posture: medium-wide, carrier near center
- Transition condition: final carrier return prepares cadence (at 23:15)
- Motion source commitment: `foreground: voice_adjacent`

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve and reorient
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light-to-baseline
- Material state: harmonic bed (F2), air (fading)
- Development action: dissolve
- Hypnotic function: reorient without abrupt arousal
- Energy / density posture: low, fading events
- Harmonic / spectral posture: F2 home color (fading)
- Spatial posture: narrow, air fading
- Transition condition: final public delivery tail complete (at 23:57)
- Motion source commitment: `foreground: voice_adjacent` (carrier fades with bed)

## Orchestration Map

- Root / ground function: Harmonic bed (F2/Bb2) provides the root
- Harmonic color function: Harmonic bed supplies pitch identity (F2 home, Bb2 contrast)
- Motion function: Carrier provides continuous motion
- Foreground / event function: Micro-events (air glints) are occasional foreground events
- Voice-adjacent induction function: Carrier layer carries hypnotist-like cadence
- Anchor / trigger function: Micro-filter shift (anchor) triggered by carrier phrase settling
- Air / bloom function: Air layer creates openness and shimmer
- Spatial / depth function: Stereo placement (carrier center, air edges) carries space as dimension
- Silence / restraint function: Anchor withheld in induction to build meaning

## Temporal Behavior

- Micro-event rate: One micro-event every 20-35 seconds (e.g., at 4:15, 5:30)
- Scene-change rate: Every 6 minutes (deepening, core, fractionation) for major harmonic shifts
- Long-wave change: Harmonic bed shifts from F2 to Bb2 in deepening, then back to F2 in integration
- Maximum stasis tolerance: 45 seconds without micro-event or harmonic shift (e.g., core max 40s between events)
- Repetition policy: Micro-events unique (different pitch, duration) to avoid wallpaper
- Fractionation timing: Brief upward scoop at 20:15 (lightening) for 15s, return to deeper at 20:30
- Anchor recurrence spacing: At 2:45 (orientation), 7:00 (deepening threshold), 13:00 (core absorption), 22:00 (fractionation re-deepening)

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow (0:00) → medium (3:00) → wide (7:00) → medium-wide (13:00) → narrow (24:00)
- Layer placement principles: Carrier near center, air in edges and rear
- Motion limits: No fast autopan; spatial movement slow (over 30s)
- Verification obligation: Verify not mono, width arc audible, motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No micro-events or harmonic shifts for >45s
  - Revision target: Add micro-event and slow harmonic shift

- Failure mode: too generic
  - Evidence: Same harmonic bed for entire piece, no motif variation
  - Revision target: Introduce contrast color (Bb2) and transform primary motif

- Failure mode: too busy
  - Evidence: More than one micro-event per 15s
  - Revision target: Reduce to one every 20-35s

- Failure mode: weak recurrence
  - Evidence: Primary motif does not return at 7:00 or 13:00
  - Revision target: Ensure motif returns at those points

- Failure mode: weak ending
  - Evidence: Ends with fade-out without carrier return
  - Revision target: Carrier returns to orientation motif in last 15s

- Failure mode: weak spatial composition
  - Evidence: Stereo width does not change; air layer centered
  - Revision target: Move air to edges/rear, slow widening

- Failure mode: weak hypnotic architecture
  - Evidence: Fractionation does not lighten and return deeper (e.g., resets)
  - Revision target: Ensure fractionation has slow harmonic shift over 15s

- Failure mode: weak fractionation
  - Evidence: Fractionation is hard cut to bright then back
  - Revision target: Use slow harmonic shift (Bb2 → F2) over 15s

- Failure mode: weak anchors / triggers
  - Evidence: Anchor absent at 7:00 or 13:00
  - Revision target: Place anchor at those points

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier not at least 6dB above bed, or phrases mechanical
  - Revision target: Adjust carrier level and use empirical cadence profile

## Sketch Handoff

- Required sketch style family: neuro-ambient with voice-adjacent induction
- Required formal units: 7 (as defined in Formal Architecture)
- Required material to preserve: Primary motif, harmonic signature (home/contrast/return), texture behavior, spatial material
- Required hypnotic architecture to preserve: Stage map and depth trajectory
- Required voice-adjacent induction behavior to preserve: Carrier at 6dB above bed, empirical cadence profile
- Required development grammar to preserve: Establishment, variation, contrast, recurrence rules
- Required revision criteria to preserve: All listed above
- Explicit exclusions: No percussion, no bright transients, no abrupt cuts