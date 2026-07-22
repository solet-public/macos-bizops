# Composition Design Document
DESIGN ID: cdg-neuro-ambient-composition-001
WORK_MANIFEST: wmf-neuro-ambient-composition-001
Status: ready

## Artistic Reading
- User image / premise: Quiet unfolding of inner awareness as layered sonic journey through breath-led space
- Musical problem: Creating immersive non-intrusive environment guiding attention inward without demanding monitoring
- Listener stance: Inward deep listening, allowing awareness to expand and settle
- What this is not: Narrative journey, pop song, or attention-grabbing ambient piece
- Style-family fit: Sequenced ambient (Carbon Based Lifeforms, Solar Fields) with breath-length phrasing and harmonic continuity

## Chosen Musical Premise
- Premise sentence: A continuous field of F2-centered harmonic warmth evolves through breath-length carrier phrases and subtle spatial shifts, guiding the listener from broad environmental awareness to deep internal absorption.
- Primary dramatic motion: Establish → Reveal → Deepen → Sustain → Lighten and return deeper → Stabilize → Reorient
- Sound-world metaphor: Slow breath moving through forest of layered sound, each exhalation revealing new textures
- Musical consequence: Forest texture thickens as breath deepens (deepening), thins during lightening (fractionation) before returning to intimate forest (integration)
- Duration rationale: 24 minutes allows full arc of deepening and fractionation without rushing, with each stage having natural progression time

## Hypnotic Function Architecture
- Target use: Music-only self-hypnosis for inward deep listening
- Stage map:
  - Orientation (3:00): baseline-to-light, settle and establish safety
  - Induction (4:00): light-to-medium, pace and lead attention inward
  - Deepening (6:00): medium-to-deep, reduce orienting load
  - Core absorptive work (7:00): deep, sustain absorption with microdevelopment
  - Fractionation (2:00): deep-to-light-medium-to-deep, briefly lighten then return deeper
  - Integration (1:30): medium-to-light, stabilize and reduce depth pressure
  - Return (0:30): light-to-baseline, reorient attention outward
- Depth trajectory: Baseline (broad) → Light (focused carrier) → Medium (narrowed attention) → Deep (internal absorption) → Light (fractionation) → Medium (stabilized deep field) → Baseline (reoriented)
- Pacing-and-leading plan: Start sparse carrier phrases, lengthen rests, lower register, darken brightness, widen space
- Ratification plan: Carrier's downward-settling phrase arc returns at depth thresholds to confirm state
- Suggestion-compatible windows: Quiet moments after carrier phrases (e.g., 1:30, 3:00, 7:00)
- Reset avoidance rule: No abrupt cuts, hard transients, alerting cues, or sudden brightness spikes

## Material Inventory
### Material Inventory - Primary Motif / Gesture
- Identity: Three-note falling-settling cell (C3-Ab2-G2) functioning as carrier memory
- First form: Soft, sparse carrier phrase (0.9s duration) during orientation after ground established
- Later forms: Lower-slower-deepening form (slower, lower register), thinned-core-fragment (reduced density), clearer-integration-return (slightly brighter, wider)

### Material Inventory - Harmonic Signature
- Home color: F root with open C (F-A-C), Eb warmth (E♭-G-B♭), G add9 (G-B-D-F#) — F-centered open voicings
- Contrast color: Db luminous shadow (Db-F-Ab) with common-tone F retained, creating subtle harmonic shift
- Return color: F root with fewer tones (only open C and Eb), softened upper partials, narrower spatial field

### Material Inventory - Texture Family
- Continuous layer behavior: Warm F2 drone with slow beating (0.1 Hz) and long attack (8-45s)
- Event layer behavior: Rare submerged motif fragments (every 15-45s) and soft partial blooms
- Noise / air behavior: Filtered high air at edges with slow opening (10-60s) and closing (20-90s)

### Material Inventory - Spatial Material
- Width behavior: Narrow → Medium → Wide → Medium-wide → Narrow across stages
- Motion behavior: Slow drift for air layer, minimal autopan (max 20-120s)
- Depth behavior: Near field → deeper reverb tails in core → shallow for return

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) for ground, gentle body pulse (0.15-0.35 Hz) for harmonic pad
- Change rule: Modulation rates slow and depths recede as piece deepens, return slightly faster for fractionation

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1
- Reference pitch-contour profile: falling_settling (reference_voice_pitch_contours_v1)
- Carrier instrument / layer: Warm low-mid reed-like MIDI carrier (GM program 89)
- Cadence behavior: Breath-length phrase arcs (0.9s-3.3s) with downward settling
- Contour behavior: Falling-settling (C3 to G2 in motif)
- Weighting behavior: Soft attack, long release; emphasis on phrase endings
- Return behavior: Returns transformed (lower register, slower) at depth thresholds
- Salience limit: Below lead melody attention; audible but not foreground

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Tiny low-mid warmth bloom (100-250 Hz) paired with carrier release
- First pairing: Orientation after first carrier phrase resolves (at 1:30)
- Withholding plan: Absent for at least 90 seconds after first pairing (until induction threshold)
- Return / trigger plan: Returns at induction threshold (3:00), deepening stability point (7:00), core reassurance (13:00), fractionation re-deepening (21:00), integration (19:30)
- Salience policy: Audible enough to learn; not salient enough to monitor (level -15 dB)

## Development Grammar
- Establishment rule: Introduce ground, harmonic body, and first carrier phrase in orientation
- Variation rule: Gradually lengthen rests and lower register during induction/deepening
- Contrast rule: Introduce Db contrast color in deepening, return to home color in core
- Recurrence rule: Primary motif returns transformed at each depth threshold (induction, deepening, core)
- Foreground rule: Event layer remains sparse; no foreground melody
- Withholding rule: Anchor withheld for 90s after first pairing, returns at key thresholds
- Fractionation rule: Lightening (20:00-21:00) with brighter air and wider field, re-deepening (21:00-22:00) with darker carrier
- Trigger conditioning rule: Anchor paired with carrier release during orientation; returns at deeper states to reinforce comfort
- Dissolution / cadence rule: Core ends with motif fragment and anchor return; integration uses wider field to stabilize
- Anti-padding rule: No looped events; all changes deliberate and tied to development

## Formal Architecture
### Formal Unit 1 - Orientation / Settling
- Name / role: Establish world and first carrier pairing
- Approximate duration range: 0:00-3:00 (180s)
- Hypnotic stage role: orientation
- Target depth: baseline-to-light
- Material state: ground, harmonic body, carrier seed, first anchor pairing
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: sparse, low
- Harmonic / spectral posture: home color (F2 open voicings)
- Spatial posture: narrow, centered
- Transition condition: carrier established and anchor first paired (at 1:30)
- Motion source commitment: foreground: cadence_carrier

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace and lead attention inward
- Approximate duration range: 3:00-7:00 (240s)
- Hypnotic stage role: induction
- Target depth: light-to-medium
- Material state: ground, harmonic body, carrier variants, anchor return
- Development action: vary (lengthen rests, lower register)
- Hypnotic function: pace and lead
- Energy / density posture: moderate, increasing focus
- Harmonic / spectral posture: home color with subtle Db hint
- Spatial posture: medium, widening slightly
- Transition condition: attention narrowed and space prepared to widen (at 7:00)
- Motion source commitment: foreground: cadence_carrier

### Formal Unit 3 - Deepening
- Name / role: Widen space, lower salience, stabilize root memory
- Approximate duration range: 7:00-13:00 (360s)
- Hypnotic stage role: deepening
- Target depth: medium-to-deep
- Material state: ground, harmonic body, air, low carrier returns, anchor softening
- Development action: deepen (widen space, lower register)
- Hypnotic function: reduce orienting load
- Energy / density posture: low, deepening
- Harmonic / spectral posture: contrast color (Db) with F retained
- Spatial posture: wide, slow drift
- Transition condition: depth stable and core can withhold carrier (at 13:00)
- Motion source commitment: foreground: cadence_carrier

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with non-inert microdevelopment
- Approximate duration range: 13:00-20:00 (420s)
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: ground, harmonic body, rare carrier, rare motif fragment, anchor reassurance
- Development action: sustain (withhold carrier, sparse events)
- Hypnotic function: sustain absorption
- Energy / density posture: very low, stable
- Harmonic / spectral posture: home color with fewer tones
- Spatial posture: wide, stable
- Transition condition: field thinned and fractionation pre-echo seeded (at 20:00)
- Motion source commitment: event_schedule

## Orchestration Map
- Root / ground function: F2 drone (centered, slow beating)
- Harmonic color function: harmonic body layer (wide stable)
- Motion function: carrier layer (near center, low motion)
- Foreground / event function: event layer (rare motif fragments)
- Voice-adjacent induction function: carrier layer
- Anchor / trigger function: anchor layer (low-mid warmth bloom)
- Air / bloom function: air_bloom_layer (edges and rear)
- Spatial / depth function: stereo width arc
- Silence / restraint function: withheld carrier in core

## Temporal Behavior
- Micro-event rate: every 15-45 seconds (motif fragments, air blooms)
- Scene-change rate: every 75-150 seconds (harmonic shifts, spatial changes)
- Long-wave change: harmonic signature shift from home to contrast and back
- Maximum stasis tolerance: 45 seconds without deliberate change or withholding
- Repetition policy: recurrence of motif transformed, not identical repetition
- Fractionation timing: lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: first pairing (1:30), then returns at 3:00, 7:00, 13:00, 21:00, 19:30

## Spatial Composition
- Stereo requirement: stereo
- Spatial arc: narrow (0:00) → medium (3:00) → wide (13:00) → medium-wide (20:00) → narrow (24:00)
- Layer placement principles: ground centered, harmonic body wide stable, carrier near center low motion, air at edges and rear
- Motion limits: no fast autopan; max 20-120s for spatial movement
- Verification obligation: verify not mono, width arc audible and motion not alerting

## Revision Criteria
- Failure mode: too static
  - Evidence: no perceptible change for >45s, harmonic color unchanged
  - Revision target: add micro-event or density change at 45s intervals

- Failure mode: weak recurrence
  - Evidence: motif appears once and vanishes, no transformed return
  - Revision target: add two more returns of motif in different forms

- Failure mode: weak fractionation
  - Evidence: lightening feels like reset, return not deeper
  - Revision target: reduce fractionation brightness and strengthen re-deepening with darker carrier

- Failure mode: weak voice-adjacent induction
  - Evidence: carrier is bright, mechanical, or too frequent
  - Revision target: soften attack/release and use falling-settling contour

## Sketch Handoff
- Required sketch style family: sequenced_ambient
- Required formal units: 4 (Orientation, Induction, Deepening, Core)
- Required material to preserve: primary motif (C3-Ab2-G2), harmonic signature, anchor trigger
- Required hypnotic architecture to preserve: stage map and depth trajectory
- Required voice-adjacent induction behavior: breath-length, downward-settling carrier with salience ceiling
- Required development grammar to preserve: recurrence rule and withholding rule
- Required revision criteria to preserve: the four listed above
- Explicit exclusions: no pop melody, hard transients, or abrupt changes