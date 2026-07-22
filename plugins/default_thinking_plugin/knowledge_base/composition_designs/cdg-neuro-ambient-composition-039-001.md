# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-039-001
WORK_MANIFEST: wmf-neuro-ambient-composition-039-001
Status: ready

## Artistic Reading

- User image / premise: A continuous, breath-phrased instrumental lead guides the listener through a self-hypnotic journey from orientation to return, with evolving ambient bed and subtle nonverbal anchors
- Musical problem: Creating a continuous conditionable lead that guides without demanding attention while evolving the ambient bed to support stage transitions without abrupt changes
- Listener stance: Following the continuous lead with broad environmental attention, then internal absorption, and gentle reorientation
- What this is not: Not a melody-driven piece; no percussion or groove; no abrupt changes; no attention-grabbing cues
- Style-family fit: Stage-Based Self-Hypnosis Journey (foreground_vocal_led_prosody mode) with warm, slightly dark timbre

## Chosen Musical Premise

- Premise sentence: A continuous sarangi-like carrier guides the listener through seven hypnotic stages, with harmonic and spatial evolution supporting depth progression without abrupt change
- Primary dramatic motion: Establish → deepen → fractionate → stabilize → dissolve
- Sound-world metaphor: A slow, deepening river that widens and darkens as it flows toward a calm pool
- Musical consequence: The river's widening (spatial) and darkening (spectral) correspond to deepening stages; the carrier's phrase arcs mirror the river's flow
- Duration rationale: 24 minutes allows for gradual depth progression (3+4+6+7=20, plus fractionation and return) with micro-variation preventing stasis

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for deep relaxation and absorption
- Stage map:
  - Orientation / settling: 0:00-3:00, baseline to light, settle and establish safety
  - Induction / narrowing: 3:00-7:00, light to medium, pace and lead attention inward
  - Deepening: 7:00-13:00, medium to deep, reduce orienting load
  - Core absorptive work: 13:00-20:00, deep, sustain absorption
  - Fractionation pockets: 20:00-22:00, deep to light-medium to deep, briefly lighten then return deeper
  - Integration / stabilization: 22:00-23:30, medium to light, stabilize
  - Return / reorientation: 23:30-24:00, light to baseline, reorient
- Depth trajectory: Gradual spectral darkening (filter automation 1700→5100 Hz) and spatial widening (width arc narrow → wide) from orientation to deepening, brief lightening during fractionation (20:00-21:00), then deeper return and gradual narrowing
- Pacing-and-leading plan: Start with slow ground (F2 drone) and sparse carrier phrases; lengthen rests, lower register, darken brightness, widen space as depth increases
- Ratification plan: Carrier's downward settling contour at phrase end ratifies depth without words
- Suggestion-compatible windows: Permissive pauses (0.5-1.8s) and harmonic bed changes create natural windows for spoken guidance
- Reset avoidance rule: No abrupt cuts, hard transients, alerting cues, or sudden brightness spikes

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Two-note minor third interval cell (F4 to D5) with falling contour, emerging from harmonic bed
- First form: Single glint at 0:12 and 0:38 in orientation
- Later forms: Three-note sequence (F4→D5→C#5) at 7:15 in deepening; four-note sequence (F4→D5→C#5→B4) at 13:00 in core work

### Material Inventory - Harmonic Signature
- Home color: F2 (87.31 Hz) with interval stack [0, 3] (F and A)
- Contrast color: G2 (98.00 Hz) with interval stack [0, 4] (G and C)
- Return color: F2 with interval stack [0, 3, 8] (F, A, D) — richer home color

### Material Inventory - Texture Family
- Continuous layer behavior: Slow-breathing F2 drone (8-45s attack, 20-60s release) creating warm anchor
- Event layer behavior: Motif fragments appearing every 15-45 seconds as subtle glints
- Noise / air behavior: High-air layer (filtered >10kHz) opening slowly at edges with 10-60s attack and 20-90s release

### Material Inventory - Spatial Material
- Width behavior: Narrow (orientation) → medium (induction) → wide (deepening) → medium-wide (core work)
- Motion behavior: Carrier near center with minimal motion; air layer drifts slowly from edges to rear
- Depth behavior: Harmonic bed reverb tail 1.5s in deepening, narrowing to 0.8s in integration

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) for ground drone, asymmetric shimmer in harmonic bed
- Change rule: Ground modulation slows from 0.13 Hz to 0.07 Hz over deepening, speeds slightly during fractionation

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1 (foreground mode)
- Reference pitch-contour profile: reference_voice_pitch_contours_v1, "conversational_induction" for orientation/induction, "relaxation_return" for deepening/core/fractionation/integration
- Carrier instrument / layer: Sarangi-like bowed string (low-mid register, warm timbre)
- Cadence behavior: Breath-length phrases 6-14s with permissive pauses (0.5-1.8s)
- Contour behavior: 84.5% falling_settling, occasional arches (2.8%) for variation
- Weighting behavior: Slight emphasis on second note of each phrase (weighted stress)
- Return behavior: Carrier returns with transformed contour at deepening stability (7:15) and fractionation re-deepening (21:05)
- Salience limit: 6 dB above harmonic bed, 10 dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Low-mid warmth bloom (F2 + minor third) at carrier release point
- First pairing: At 0:45 (orientation), carrier phrase settles downward and anchor bloom appears
- Withholding plan: Anchor absent during induction (3:00-7:00) to preserve meaning
- Return / trigger plan: Returns at deepening stability (7:15), core work threshold (13:00), fractionation re-deepening (21:05)
- Salience policy: Audible enough to learn, not salient enough to monitor (0.5 dB above bed)

## Development Grammar

- Establishment rule: Introduce carrier and ground drone in orientation; establish home color
- Variation rule: Transform motif cell by adding notes and widening spacing in deepening/core work
- Contrast rule: Shift to contrast color (G2) during induction, return to home for deepening
- Recurrence rule: Motif cell returns transformed in each stage; anchor cue returns at depth thresholds
- Foreground rule: Carrier always foreground (≥6 dB above bed)
- Withholding rule: Anchor cue withheld during induction to preserve meaning
- Fractionation rule: Brief lightening (20:00-21:00) via carrier contour brightening and air layer opening, then re-deepening (21:00-22:00) via carrier contour darkening and air layer closing
- Trigger conditioning rule: Anchor paired with carrier release in orientation; returns at deeper thresholds to reinforce stability
- Dissolution / cadence rule: Final fade uses carrier's slowest contour (relaxation_return) and air layer closing
- Anti-padding rule: No event repetition without transformation; all events sparse and meaningful

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish the world and first carrier pairing
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: ground, harmonic_body (home color), carrier_seed, first_anchor_pairing
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: sparse, low density
- Harmonic / spectral posture: home color (F2 + A), warm and centered
- Spatial posture: narrow, centered ground; carrier near center
- Transition condition: carrier established and anchor first paired (0:45)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace and lead attention inward
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: ground, harmonic_body (contrast color), carrier_variants
- Development action: vary
- Hypnotic function: pace and lead attention
- Energy / density posture: slightly denser than orientation
- Harmonic / spectral posture: contrast color (G2 + C), brighter than home
- Spatial posture: medium width, carrier near center with longer phrases
- Transition condition: attention narrowed and space prepared to widen (7:00)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen space and lower salience to stabilize root memory
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: ground, harmonic_body (home color), air, low_carrier_returns
- Development action: thicken
- Hypnotic function: reduce orienting load
- Energy / density posture: medium density, air layer opening
- Harmonic / spectral posture: home color (F2 + A), darkened by lowpass filter
- Spatial posture: wide, carrier near center with downward settling contours
- Transition condition: depth stable and core can withhold carrier (13:00)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with non-inert microdevelopment
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: ground, harmonic_body (home color), rare_carrier, motif_fragment
- Development action: sustain
- Hypnotic function: sustain absorption
- Energy / density posture: low density, rare motif fragments
- Harmonic / spectral posture: home color (F2 + A), slightly darker than deepening
- Spatial posture: medium-wide, carrier near center with transformed motif cell
- Transition condition: field thinned and fractionation pre-echo seeded (20:00)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten attention then return deeper
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep to light-medium to deep
- Material state: ground, harmonic_body (home color), brief_brightness, clearer_carrier
- Development action: fractionate
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: light-medium (20:00-21:00), then deep
- Harmonic / spectral posture: home color with brighter upper partials (20:00-21:00), darker return
- Spatial posture: medium-wide to wide (lightening) then back to medium-wide (re-deepening)
- Transition condition: brightness closed and deeper return established (22:00)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 6 - Integration
- Name / role: Return familiar material in clearer calm form
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: ground, harmonic_body (home color), clearer_carrier
- Development action: dissolve
- Hypnotic function: stabilize
- Energy / density posture: sparse, calm
- Harmonic / spectral posture: home color (F2 + A), return color (F2+A+D) at end
- Spatial posture: medium, carrier near center with clearer contour
- Transition condition: final carrier return prepares cadence (23:30)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve and cadence to baseline
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: ground_residue, air_fade
- Development action: dissolve
- Hypnotic function: reorient
- Energy / density posture: very sparse
- Harmonic / spectral posture: home color fading to F2 drone only
- Spatial posture: narrow, air layer fading from edges
- Transition condition: final public delivery tail complete (24:00)
- Motion source commitment: foreground: voice_adjacent

## Orchestration Map

- Root / ground function: F2 drone (ground layer) provides stability
- Harmonic color function: harmonic_body_layer carries interval stacks (home, contrast, return)
- Motion function: carrier layer provides breath-length phrase arcs
- Foreground / event function: motif fragments (rare) and anchor cues (subtle)
- Voice-adjacent induction function: sarangi-like carrier with breath-length phrases
- Anchor / trigger function: low-mid warmth bloom (anchor layer) paired with carrier release
- Air / bloom function: high-air layer creates openness at edges
- Spatial / depth function: stereo width and reverb tail control spatial posture
- Silence / restraint function: withholding of anchor during induction

## Temporal Behavior

- Micro-event rate: One motif fragment every 15-45 seconds (e.g., 0:38, 7:15, 13:00)
- Scene-change rate: Every 6-7 minutes (at stage transitions)
- Long-wave change: Harmonic region shifts at each stage
- Maximum stasis tolerance: 45 seconds without meaningful change (e.g., no motif fragment)
- Repetition policy: Motif cell returns transformed, not identical
- Fractionation timing: Lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: At 0:45 (orientation), 7:15 (deepening stability), 13:00 (core threshold), 21:05 (fractionation re-deepening)

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow (0:00-3:00) → medium (3:00-7:00) → wide (7:00-13:00) → medium-wide (13:00-20:00) → narrow (22:00-24:00)
- Layer placement principles: ground centered, harmonic_body wide stable, carrier near center low motion, air at edges and rear
- Motion limits: No fast autopan; spatial movement over 20-120 seconds allowed for air layer
- Verification obligation: Verify not mono, width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No motif recurrence or harmonic shift for >45 seconds
  - Revision target: Add transformed motif fragment at 10:30 and 16:45

- Failure mode: too generic
  - Evidence: Harmonic bed sounds like standard ambient pad
  - Revision target: Use F-centered open voicings with slow filter motion

- Failure mode: too busy
  - Evidence: More than one motif fragment per 15 seconds
  - Revision target: Reduce event rate to every 20-45 seconds

- Failure mode: weak recurrence
  - Evidence: Motif cell appears once and vanishes
  - Revision target: Transform motif by adding note at 7:15 and another at 13:00

- Failure mode: weak ending
  - Evidence: Piece fades out without cadence
  - Revision target: Add return color (F2+A+D) at 23:45 and fade with carrier

- Failure mode: weak spatial composition
  - Evidence: Output mono or width doesn't change
  - Revision target: Ensure stereo construction for all layers

- Failure mode: weak hypnotic architecture
  - Evidence: No clear depth progression (deepening stage doesn't feel deeper)
  - Revision target: Adjust filter automation to darken harmonic bed in deepening (3400→5100 Hz)

- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset (bright and abrupt)
  - Revision target: Make lightening gradual (20:00-21:00) and re-deepening through carrier contour

- Failure mode: weak anchors / triggers
  - Evidence: Anchor cue too obvious (e.g., click)
  - Revision target: Soften anchor to 0.5 dB above bed as warmth bloom

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier too bright or mechanical
  - Revision target: Use empirical profile (84.5% falling_settling) and ensure phrase length 6-14s

## Sketch Handoff

- Required sketch style family: Stage-Based Self-Hypnosis Journey (foreground_vocal_led_prosody)
- Required formal units: 7 units as defined above
- Required material to preserve: Primary motif (two-note cell), harmonic signature, texture behavior, spatial material
- Required hypnotic architecture to preserve: Stage map and depth trajectory (baseline→light→medium→deep→brief lightening→deep return→baseline)
- Required voice-adjacent induction behavior to preserve: Sarangi-like carrier, breath-length phrases (6-14s), falling_settling contour
- Required development grammar to preserve: Establishment, variation, contrast, recurrence rules
- Required revision criteria to preserve: The 10 failure modes and targets above
- Explicit exclusions: No percussion, no abrupt changes, no foreground melody