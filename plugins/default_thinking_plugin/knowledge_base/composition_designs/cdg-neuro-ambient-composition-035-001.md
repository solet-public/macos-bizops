# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-035-001
WORK_MANIFEST: wmf-neuro-ambient-composition-035-001
Status: ready

## Artistic Reading

- User image / premise: A hypnotist's voice delivered through a reed or bowed string, guiding listeners through seven self-hypnosis stages without words
- Musical problem: Creating continuous prosodic guidance that feels like a living voice while maintaining non-melodic, ambient texture
- Listener stance: Following the carrier's breath-length phrases without active concentration
- What this is not: Pop melody, vocal samples, rhythmic pulse, or dramatic transitions
- Style-family fit: Self-Hypnosis Stage Architecture with foreground_vocal_led_prosody

## Chosen Musical Premise

- Premise sentence: A sarangi-like carrier voice guides listeners through seven hypnotic stages using breath-length phrases, with harmonic bed as a warm anchor and subtle spatial widening to deepen absorption
- Primary dramatic motion: Establish → deepen → fractionate → stabilize → dissolve
- Sound-world metaphor: A slow-moving river with reed-like currents carrying the listener through deepening channels
- Musical consequence: Harmonic bed remains stable F2 root while carrier's pitch range narrows (A3→G5) to create depth pressure; spatial width expands with each stage
- Duration rationale: 24 minutes allows for seven distinct hypnotic stages (3+4+6+7+2+1.5+0.5) with 15-45s micro-events preventing stasis

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis with continuous carrier voice
- Stage map:
  - Orientation (0:00-3:00): Baseline-to-light, establish safety
  - Induction (3:00-7:00): Light-to-medium, pace attention inward
  - Deepening (7:00-13:00): Medium-to-deep, reduce orienting load
  - Core absorptive work (13:00-20:00): Deep, sustain absorption
  - Fractionation (20:00-22:00): Deep-to-light-medium-to-deep, brief lightening
  - Integration (22:00-23:30): Medium-to-light, stabilize
  - Return (23:30-24:00): Light-to-baseline, reorient
- Depth trajectory: Pitch range narrows (A3→G5), harmonic density increases, spatial width expands to 360° then contracts
- Pacing-and-leading plan: Carrier phrases (6-14s) with 0.5-1.8s rests, using rise-and-settle contours to guide attention
- Ratification plan: Carrier's downward settling at 120s intervals confirms depth acceptance
- Suggestion-compatible windows: Core absorptive work (13:00-20:00) and integration (22:00-23:30)
- Reset avoidance rule: No brightness spikes, hard cuts, or abrupt spatial changes

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Rising minor third (A3-C#4) followed by falling second (C#4-B3)
- First form: Introduced in orientation at 0:15 with soft attack
- Later forms: Transposed down by minor third in deepening; widened to 360° in core work

### Material Inventory - Harmonic Signature
- Home color: F2 root + minor seventh (F2-A3) with fifth (C3)
- Contrast color: F2 root + major third (F2-A#3) with fifth
- Return color: Original home color minus seventh, softened by lowpass

### Material Inventory - Texture Family
- Continuous layer behavior: Ground drone with 8-45s attack, 20-60s release
- Event layer behavior: Micro-events every 15-45s (e.g., spectral glints)
- Noise / air behavior: Air bloom with 10-60s attack, 20-90s release

### Material Inventory - Spatial Material
- Width behavior: Narrow (120°) → medium (240°) → wide (360°) → medium-wide (180°) → narrow (270°)
- Motion behavior: Carrier drifts 15° left to right; air moves from edges inward
- Depth behavior: Reverb tail lengthens during deepening, shortens in return

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) with asymmetric shimmer
- Change rule: Modulation depth decreases 25% during core work, increases 10% in fractionation

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1 (6-14s phrases)
- Reference pitch-contour profile: empirical_profile_proportions_v1
- Carrier instrument / layer: Sarangi-like bowed string (GM program 89)
- Cadence behavior: Breath-length phrases with permissive pauses
- Contour behavior: 84.5% falling_settling, 3.6% scoop_then_settle
- Weighting behavior: Slight register warmth at phrase endings
- Return behavior: Downward settling with 0.5s pause before return
- Salience limit: ≥6dB above harmonic bed, ≥10dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Low-mid warmth bloom (F2-A3) at 1.5s duration
- First pairing: Orientation phrase ending (0:45) with bed widening to 180°
- Withholding plan: Absent during deepening (7:00-13:00)
- Return / trigger plan: Integration at 22:45 (bed widens to 270°)
- Salience policy: Audible but not attention-demanding (≤ -15dB)

## Development Grammar

- Establishment rule: Introduce carrier and home color in orientation
- Variation rule: Transpose motif down by minor third per stage
- Contrast rule: Shift harmonic color to major third during deepening
- Recurrence rule: Anchor returns transformed (wider, softer) in integration
- Foreground rule: Carrier remains continuous; no other foreground events
- Withholding rule: Anchor absent during deepening to preserve meaning
- Fractionation rule: Brief upward pitch scoop (0.5s) at 20:15, return to deep state
- Trigger conditioning rule: Anchor paired with bed widening at 0:45, returns at 22:45
- Dissolution / cadence rule: Final carrier phrase ends with spatial contraction to 120°
- Anti-padding rule: No event repetition within 45s; micro-events spaced at 15-45s

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish carrier and home color
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: Orientation
- Target depth: Baseline-to-light
- Material state: Carrier, home color harmonic bed, ground drone
- Development action: Establish carrier and motif cell
- Hypnotic function: Settle safety
- Energy / density posture: Sparse (1 event)
- Harmonic / spectral posture: Home color
- Spatial posture: Narrow (120°)
- Transition condition: Carrier established and anchor first paired
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace attention inward
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: Induction
- Target depth: Light-to-medium
- Material state: Carrier variants, anchor return
- Development action: Vary carrier contour
- Hypnotic function: Pace attention inward
- Energy / density posture: Moderate (2 events)
- Harmonic / spectral posture: Home color
- Spatial posture: Medium (180°)
- Transition condition: Attention narrowed and space prepared to widen
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Reduce orienting load
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: Deepening
- Target depth: Medium-to-deep
- Material state: Air, low carrier returns, anchor softening
- Development action: Thicken harmonic bed
- Hypnotic function: Reduce orienting load
- Energy / density posture: Dense (3 events)
- Harmonic / spectral posture: Contrast color
- Spatial posture: Wide (240°)
- Transition condition: Depth stable and core can withhold carrier
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: Core absorptive work
- Target depth: Deep
- Material state: Rare carrier, motif fragment, anchor reassurance
- Development action: Transform motif cell
- Hypnotic function: Sustain absorption
- Energy / density posture: Sparse (1 event)
- Harmonic / spectral posture: Home color minus seventh
- Spatial posture: Widest (360°)
- Transition condition: Field thinned and fractionation pre-echo seeded
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 5 - Fractionation Pocket
- Name / role: Brief lightening then deeper return
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: Fractionation
- Target depth: Deep-to-light-medium-to-deep
- Material state: Brief brightness, clearer carrier, anchor redeepening
- Development action: Lighten then deepen
- Hypnotic function: Brief lightening without reset
- Energy / density posture: Moderate (2 events)
- Harmonic / spectral posture: Contrast color
- Spatial posture: Medium-wide (180°)
- Transition condition: Brightness closed and deeper return established
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 6 - Integration / Stabilization
- Name / role: Return familiar material in calmer form
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: Integration
- Target depth: Medium-to-light
- Material state: Clearer carrier, anchor reassurance
- Development action: Recur motif transformed
- Hypnotic function: Stabilize depth pressure
- Energy / density posture: Sparse (1 event)
- Harmonic / spectral posture: Home color
- Spatial posture: Medium-wide (270°)
- Transition condition: Final carrier return prepares cadence
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve and cadence
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: Return
- Target depth: Light-to-baseline
- Material state: Ground residue, air fade
- Development action: Dissolve carrier
- Hypnotic function: Reorient attention
- Energy / density posture: Sparse (0 events)
- Harmonic / spectral posture: Home color
- Spatial posture: Narrow (120°)
- Transition condition: Final public delivery tail complete
- Motion source commitment: foreground: voice_adjacent

## Orchestration Map

- Root / ground function: F2 drone (87.31 Hz)
- Harmonic color function: Home/contrast colors in harmonic bed
- Motion function: Carrier's breath-length phrases
- Foreground / event function: Micro-events (15-45s)
- Voice-adjacent induction function: Sarangi carrier
- Anchor / trigger function: Low-mid warmth bloom
- Air / bloom function: Spectral air layer at edges
- Spatial / depth function: Width arc and reverb tail
- Silence / restraint function: Anchor withheld during deepening

## Temporal Behavior

- Micro-event rate: Every 15-45 seconds (e.g., spectral glints)
- Scene-change rate: Every 75-150 seconds (harmonic region shifts)
- Long-wave change: Width arc from 120°→360°→120°
- Maximum stasis tolerance: 45 seconds without change
- Repetition policy: Motif cell transforms per stage; no identical returns
- Fractionation timing: Brief upward pitch scoop at 20:15 (0.5s)
- Anchor recurrence spacing: First pairing at 0:45, return at 22:45

## Spatial Composition

- Stereo requirement: Stereo
- Spatial arc: Narrow (120°) → medium (180°) → wide (360°) → medium-wide (180°) → narrow (270°)
- Layer placement principles: Carrier near center, air at edges, anchors local
- Motion limits: No fast autopan; carrier drifts 15° over 60s
- Verification obligation: Verify width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No micro-events for >45s in deepening stage
  - Revision target: Add spectral glint at 10:23

- Failure mode: weak anchors
  - Evidence: Anchor absent during deepening, no return in integration
  - Revision target: Add anchor at 22:45 with bed widening

- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset (brightness spike)
  - Revision target: Reduce fractionation brightness to ≤-12dB

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier absent in deepening stage
  - Revision target: Ensure carrier continuous through all stages

## Sketch Handoff

- Required sketch style family: Self-Hypnosis Stage Architecture
- Required formal units: 7 stages with exact duration ranges
- Required material to preserve: Primary motif, harmonic signature, anchor identity
- Required hypnotic architecture to preserve: Stage map and depth trajectory
- Required voice-adjacent induction behavior to preserve: Carrier continuity, 6-14s phrases
- Required development grammar to preserve: Variation rule and withholding plan
- Required revision criteria to preserve: All four failure modes above
- Explicit exclusions: No percussion, no pop hooks, no abrupt transitions