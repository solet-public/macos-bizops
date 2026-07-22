# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-029-001
WORK_MANIFEST: wmf-neuro-ambient-composition-029-001
Status: ready

## Artistic Reading

- User image / premise: Continuous vocal-styled instrumental lead guiding through seven hypnotic stages for self-hypnosis
- Musical problem: Creating a non-melodic, continuous carrier that guides without demanding attention while supporting hypnotic depth progression
- Listener stance: Inward deep listening with no active tracking of the lead as a melody
- What this is not: Dramatic, pop-hook-based, static drone over 120s, or with abrupt changes
- Style-family fit: Sequenced Ambient (Carbon Based Lifeforms/Solar Fields) with continuous texture and slow harmonic evolution

## Chosen Musical Premise

- Premise sentence: A soft cello-like carrier with breath-length phrasing guides the listener through seven hypnotic stages via harmonic region shifts and subtle spatial expansion, maintaining a warm spectral posture that deepens without becoming oppressive
- Primary dramatic motion: Deepen (throughout, with brief lightening in fractionation)
- Sound-world metaphor: A slow descent into a warm resonant cave where the earth's voice guides inward
- Musical consequence: Harmonic shifts from F2 to A2 (deepening) then C3 (core), texture evolves from sparse eddies to high-density micro-events, register lowers gradually, space widens then contracts
- Duration rationale: 24 minutes earned through seven-stage architecture with sufficient time for depth progression (e.g., 7-minute core allows micro-developments without rushing)

## Hypnotic Function Architecture

- Target use: Self-hypnosis bed for deep relaxation
- Stage map:
  - Orientation (0:00-3:00): baseline to light — establish safety with sparse carrier and first anchor pairing
  - Induction (3:00-7:00): light to medium — pace attention inward with increasing carrier density
  - Deepening (7:00-13:00): medium to deep — widen space, lower register, darken brightness
  - Core absorptive work (13:00-20:00): deep — sustain depth with rare carrier events
  - Fractionation pocket (20:00-22:00): deep to light-medium to deep — brief brightness/lightening then deeper return
  - Integration (22:00-23:30): medium to light — stabilize with harmonic return
  - Return (23:30-24:00): light to baseline — reorient with gentle fade
- Depth trajectory: Baseline (low density, narrow width) → Light (medium density, medium width) → Medium (high density, wide space) → Deep (very high density, widest space) → Light-medium (reduced density, brighter spectrum) → Deep return (increased density, darker spectrum) → Light (medium density, contracted space) → Baseline (low density, narrow width)
- Pacing-and-leading plan: Start with slow ground and sparse carrier (0.25 events/min), then lengthen rests, lower register, darken brightness, and widen space to lead attention inward
- Ratification plan: Carrier returns with transformed contour (lower register, longer decay) at depth thresholds to ratify state
- Suggestion-compatible windows: Quietest moments (10-20s of low density) in core absorptive work and integration
- Reset avoidance rule: No abrupt cuts, hard transients, or sudden brightness spikes; all transitions use soft widening crescendos

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Falling minor third (A3-C4) followed by rising second (C4-D4), with falling interval 20% longer
- First form: Orientation stage, single sparse event (0.25 events/min), soft attack/long release
- Later forms: Deepening stage (transposed down minor third, 2.5 events/min); core absorptive work (fragment B3-C4 at 4.0 events/min)

### Material Inventory - Harmonic Signature
- Home color: F2 (87.31 Hz) with fifth and minor seventh (F2, C3, Eb3)
- Contrast color: A2 (110.00 Hz) with fifth and major seventh (A2, E3, F#4)
- Return color: Original home with minor seventh softened (F2, C3, E4), fifth omitted

### Material Inventory - Texture Family
- Continuous layer behavior: Low-mid substrate (87.31 Hz) with irregular spectral eddies every 20-45 seconds
- Event layer behavior: Sparse carrier events (orientation) → moderate density (induction) → high density (core)
- Noise / air behavior: Air bloom layer that widens and brightens during deepening, contracts in integration

### Material Inventory - Spatial Material
- Width behavior: Narrow (0-3:00) → medium (3:00-7:00) → wide (7:00-13:00) → medium-wide (13:00-20:00) → narrow (20:00-24:00)
- Motion behavior: Slow air layer drift (left to right) during deepening; anchor cues at center
- Depth behavior: Near (orientation) → medium (induction) → deep (deepening/core) → near (integration/return)

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) for ground layer, asymmetric shimmer in air bloom
- Change rule: Modulation rate decreases by 0.01 Hz every 2 minutes; depth increases slightly in deepening

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1`
- Reference pitch-contour profile: `reference_voice_pitch_contours_v1` (84.5% falling_settling)
- Carrier instrument / layer: Soft cello with warm low-mid resonance (filtered to 102-212 Hz)
- Cadence behavior: Breath-length phrases (6-14s) with 1-3s inter-phrase rests
- Contour behavior: Stepwise motion with permissive leaps (minor third max), prosodic peaks rise then settle
- Weighting behavior: Slight sustain increase at phrase peak (not gain spike)
- Return behavior: Transformed contour returns at depth thresholds
- Salience limit: 6 dB above harmonic bed, 10 dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Soft low-mid warmth bloom (0.5s, 120 Hz) with slow decay
- First pairing: Orientation (1:45), carrier phrase settles downward → anchor bloom appears at release point
- Withholding plan: Anchor absent during induction (3:00-7:00) and deepening (7:00-13:00)
- Return / trigger plan: Core absorption (15:00), fractionation lightening (20:15), integration (22:30)
- Salience policy: Audible enough to learn (-18 dB relative to carrier), not salient enough for monitoring

## Development Grammar

- Establishment rule: Introduce carrier and substrate with sparse events (0.25/min) in orientation
- Variation rule: Transform motif by register and density across stages (e.g., minor third → major second)
- Contrast rule: Shift harmonic regions with soft widening crescendos (F2 → G#2 → A2)
- Recurrence rule: Motif returns transformed at each stage transition (deepening stability, core absorption)
- Foreground rule: Carrier always foreground (≥6 dB above bed) in all stages
- Withholding rule: Anchor absent during induction and deepening to preserve meaning for returns
- Fractionation rule: Lighten brightness (10% spectral energy reduction) and reduce density for 60s, then return deeper with wider space
- Trigger conditioning rule: Anchor paired with carrier settling in orientation; returns at depth thresholds to ratify
- Dissolution / cadence rule: Final fade includes orientation motif return at 10% density
- Anti-padding rule: No event loops; all events generated with permissive timing (15-45s apart)

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish safety and first carrier pairing
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: ground (F2), harmonic body, carrier seed, first anchor pairing
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: sparse (0.25 events/min)
- Harmonic / spectral posture: F2 home color, warm but not dark
- Spatial posture: narrow (width 10%)
- Transition condition: carrier established and anchor first paired
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace attention inward
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: ground, harmonic body (G#2), carrier variants, anchor return
- Development action: vary
- Hypnotic function: pace and lead attention inward
- Energy / density posture: moderate (1.0 events/min)
- Harmonic / spectral posture: G#2 contrast color, slightly brighter
- Spatial posture: medium (width 30%)
- Transition condition: attention narrowed and space prepared to widen
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize depth
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: ground, harmonic body (A2), air bloom, low carrier returns, anchor softening
- Development action: thicken
- Hypnotic function: widen space and stabilize root memory
- Energy / density posture: medium (2.5 events/min)
- Harmonic / spectral posture: A2 contrast color, darker
- Spatial posture: wide (width 70%)
- Transition condition: depth stable and core can withhold carrier
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain deep absorption
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: ground, harmonic body (C3), rare carrier, motif fragment, anchor reassurance
- Development action: sustain
- Hypnotic function: sustain absorption with microdevelopment
- Energy / density posture: high (4.0 events/min)
- Harmonic / spectral posture: C3 home color, minor seventh softened
- Spatial posture: medium-wide (width 60%)
- Transition condition: field thinned and fractionation pre-echo seeded
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten and return deeper
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation pocket
- Target depth: deep to light-medium to deep
- Material state: ground, harmonic body (E3), carrier lightened, anchor brief return
- Development action: answer
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: low (0.5 events/min) during lightening, high for return
- Harmonic / spectral posture: E3 contrast color (brighter), returns to C3
- Spatial posture: medium (width 40%) during lightening, wide for return
- Transition condition: fractionation pre-echo seeded
- Motion source commitment: none — short unit (exactly 120 seconds)

### Formal Unit 6 - Integration
- Name / role: Stabilize and reduce depth pressure
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: ground, harmonic body (G#2), carrier returns to orientation contour
- Development action: dissolve
- Hypnotic function: stabilize and reduce depth pressure
- Energy / density posture: medium (2.0 events/min)
- Harmonic / spectral posture: G#2 contrast color, returning to home
- Spatial posture: medium (width 30%)
- Transition condition: integration complete
- Motion source commitment: none — short unit (90 seconds)

### Formal Unit 7 - Return
- Name / role: Reorient attention gently
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: ground, harmonic body (F2), carrier fades
- Development action: dissolve
- Hypnotic function: reorient without alerting
- Energy / density posture: sparse (0.3 events/min)
- Harmonic / spectral posture: F2 home color, softened
- Spatial posture: narrow (width 10%)
- Transition condition: reorientation complete
- Motion source commitment: none — short unit (30 seconds)

## Orchestration Map

- Root / ground function: F2 substrate providing harmonic bed
- Harmonic color function: A2 and C3 for harmonic shifts
- Motion function: Carrier layer with breath-length phrases (6-14s)
- Foreground / event function: Carrier layer (always ≥6 dB above bed)
- Voice-adjacent induction function: Soft cello carrier with prosodic phrasing
- Anchor / trigger function: Low-mid warmth bloom (120 Hz) at thresholds
- Air / bloom function: Air layer widening during deepening, contracting in integration
- Spatial / depth function: Stereo width arc (narrow → medium → wide → medium-wide → narrow)
- Silence / restraint function: Anchor withheld during induction and deepening

## Temporal Behavior

- Micro-event rate: Every 15-45 seconds (substrate eddies, carrier events)
- Scene-change rate: Every 75-150 seconds (harmonic region shifts)
- Long-wave change: Harmonic root progression across stages
- Maximum stasis tolerance: 45 seconds without meaningful change (e.g., no event)
- Repetition policy: Recurrence of motif in transformed form; no identical loops
- Fractionation timing: Lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: First pairing (1:45), core absorption return (15:00), fractionation lightening return (20:15), integration return (22:30)

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow (0-3:00) → medium (3:00-7:00) → wide (7:00-13:00) → medium-wide (13:00-20:00) → narrow (20:00-24:00)
- Layer placement principles:
  * Ground: centered
  * Harmonic body: wide stable
  * Carrier: near center, low motion
  * Air bloom: edges and rear (slow drift)
  * Anchor: local but soft (centered, short duration)
- Motion limits: Air layer drift at 0.5-1 degree per second; no fast autopan
- Verification obligation: Verify not mono, width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No perceptible change for >45 seconds in any stage
  - Revision target: Add micro-event (substrate eddy) or density shift

- Failure mode: weak hypnotic architecture
  - Evidence: Depth trajectory not audible (e.g., no widening in deepening)
  - Revision target: Adjust spatial and harmonic posture to match stage map

- Failure mode: weak anchors
  - Evidence: Anchor cues too frequent (e.g., every 5 minutes) or not paired with carrier settling
  - Revision target: Adjust withholding and return timing to match design

- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset (e.g., sudden brightness)
  - Revision target: Reduce fractionation salience and ensure re-deepening darker

- Failure mode: weak ending
  - Evidence: Piece fades without returning orientation motif
  - Revision target: Add return of first carrier contour at 10% density

## Sketch Handoff

- Required sketch style family: Sequenced Ambient
- Required formal units: 7 (as defined in Formal Architecture)
- Required material to preserve: Primary motif, harmonic signature, texture behavior, spatial arc
- Required hypnotic architecture to preserve: Stage map and depth trajectory
- Required voice-adjacent induction behavior: Carrier (soft cello), phrase timing 6-14s, contour falling_settling
- Required development grammar: Recurrence rule (transformed motif), withholding rule (anchor absent in induction/deepening)
- Required revision criteria: As defined above
- Explicit exclusions: No percussion, no abrupt changes