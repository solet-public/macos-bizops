# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-051-001
WORK_MANIFEST: wmf-neuro-ambient-composition-051-001
Status: ready

## Artistic Reading

- User image / premise: A continuous breath-length instrumental lead voice guiding through seven hypnotic stages without demanding attention
- Musical problem: Creating a non-intrusive lead that guides depth progression through prosody rather than melody
- Listener stance: Inward deep listening, absorbing without monitoring or tracking
- What this is not: Pop hook-based composition, dramatic narrative, cinematic tension bed, or foreground vocal performance
- Style-family fit: Fits neuro-ambient style family (drone_sustained_tone, body_resonance) with subsalient carrier dynamics

## Chosen Musical Premise

- Premise sentence: A continuous breath-length instrumental lead voice guides the listener through seven hypnotic stages using prosodic phrasing and subtle harmonic evolution, with a stable low-mid anchor supporting continuity
- Primary dramatic motion: Establish → Reveal → Deepen → Sustain → Lighten and return deeper → Stabilize → Dissolve
- Sound-world metaphor: A slow, warm breath moving through a forest of low-mid resonance and air
- Musical consequence: Lead voice contour (rise-and-settle) guides depth; harmonic shifts via interval stack evolution without rhythmic pulse; spatial width mirrors absorption state
- Duration rationale: 24 minutes allows sufficient time for each stage to develop (deepening at 6 minutes, core at 7 minutes) and micro-variation to prevent stasis

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for deep listening
- Stage map:
  - Orientation (0:00-3:00): baseline to light, settle and establish safety
  - Induction (3:00-7:00): light to medium, pace and lead attention inward
  - Deepening (7:00-13:00): medium to deep, reduce orienting load
  - Core (13:00-20:00): deep, sustain absorption
  - Fractionation (20:00-22:00): deep to light-medium to deep, briefly lighten then return deeper
  - Integration (22:00-23:30): medium to light, stabilize
  - Return (23:30-24:00): light to baseline, reorient
- Depth trajectory: Baseline (F2 anchor) → Light (F3 harmonic bed, 10% brightness) → Medium (G#2/A2, 25% brightness) → Deep (C3/D#3, 40% brightness) → Light-medium (D#3/E3, 50% brightness) → Deep (C3/Bb2, 40% brightness) → Baseline (F2/F3, 10% brightness)
- Pacing-and-leading plan: Lead voice uses breath-length phrases (6-14s) with permissive pauses to guide attention inward during induction, then deepens through downward settling
- Ratification plan: Lead voice returns to orientation contour at deepening and integration, confirming stability without words
- Suggestion-compatible windows: Quiet moments after lead phrase (0.5-1.8s) for spoken guidance
- Reset avoidance rule: No abrupt changes; all transitions use 12-second crossfades and harmonic shifts (not rhythmic pulses)

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Rising minor third (A3-C#4) followed by falling second (C#4-B3), with 0.5-1.8s rest before next occurrence
- First form: Introduced in orientation as soft low-mid contour (A3-C#4-B3) with 0.8s rest
- Later forms: Transposed up minor third in induction (C#4-E4-D#4), transposed down to A2-C#3-B2 in deepening, returned at core as filtered version (C#4-E4-D#4 with 3200Hz cutoff)

### Material Inventory - Harmonic Signature
- Home color: F2 (87.31 Hz) + F3 (174.62 Hz) + A#3 (233.08 Hz) [interval stack: 0,12,19 semitones]
- Contrast color: G#2 (98.00 Hz) + A3 (220.00 Hz) + C#4 (277.18 Hz) [interval stack: 0,13,20 semitones]
- Return color: F2 + F3 with A#3 replaced by D#4 (155.56 Hz) [interval stack: 0,12,19]

### Material Inventory - Texture Family
- Continuous layer behavior: Sequenced ambient arpeggio (ground) with irregular gaps and 8-45s attack/release, filtered ≤3200Hz
- Event layer behavior: Micro-events (motif fragments) every 15-45s with soft attack and long release
- Noise / air behavior: Air bloom (diffuse noise) that widens stereo field and opens spectral content at 10-60s intervals

### Material Inventory - Spatial Material
- Width behavior: Narrow (15°) → Medium (45°) → Wide (90°) → Medium-wide (60°) → Narrow-medium (30°) → Medium (45°) → Narrow (15°)
- Motion behavior: Slow drift of air bloom from rear to edges, no fast autopan (max 20°/sec)
- Depth behavior: Reverb tail length increases from 1.5s (orientation) to 3.0s (deepening), then decreases

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) for ground, gentle body pulse (0.15-0.35 Hz) for harmonic bed
- Change rule: Modulation rate decreases by 10% every 3 minutes to deepen the field

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1 (6-14s phrases, 0.5-3.0s pauses)
- Reference pitch-contour profile: reference_voice_pitch_contours_v1 (84.5% falling_settling, 8.2% rising_opening)
- Carrier instrument / layer: Sarangi-like bowed string patch (low-mid register, warm timbre)
- Cadence behavior: Breath-length phrases with 1.5s average duration, permissive pauses
- Contour behavior: Primarily falling_settling (84.5%), occasional rising_opening for fractionation
- Weighting behavior: Slight emphasis on second note of phrase (e.g., C#4 in A3-C#4-B3)
- Return behavior: Orientation phrase returns at deepening and integration, transformed by register
- Salience limit: Carrier level 6dB above harmonic bed (≥10dB above substrate), not foreground

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: 0.3s filter dip at 1200Hz (soft, non-alerting)
- First pairing: In orientation, anchor appears at end of lead phrase (after settling)
- Withholding plan: Anchor absent during induction and deepening
- Return / trigger plan:
  - Core (15:00): Anchor at end of lead phrase, confirming absorption
  - Fractionation (20:30): Anchor during lightening phase; returns at 21:45 for re-deepening
  - Integration (22:30): Anchor at end of lead phrase, confirming stability
- Salience policy: Filter dip amplitude -15dB (audible but not attention-grabbing)

## Development Grammar

- Establishment rule: Introduce lead voice and ground arpeggio in orientation
- Variation rule: Transpose motif cell by minor third every stage, harmonic bed shifting
- Contrast rule: Shift to brighter contrast color (induction) then return to home with transformation
- Recurrence rule: Motif cell returns at core and integration, transformed by register/filtering
- Foreground rule: Only lead voice is foreground; events ≤ -15dB
- Withholding rule: Anchor absent during induction and deepening to preserve meaning
- Fractionation rule: Brief upward pitch shift (D#3→E3) and brightness increase (to 50%), then return to core's deeper state
- Trigger conditioning rule: Anchor paired with lead phrase settling; returns at deepening/integration to reinforce state
- Dissolution / cadence rule: Final fade uses harmonic simplification (F2/F3) and spatial contraction
- Anti-padding rule: No event repetition without transformation; all events serve hypnotic arc

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish the world and first carrier pairing
- Approximate duration range: 0:00-3:00 (180s)
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: ground, harmonic bed (F2/F3), carrier seed (A3-C#4-B3 motif)
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: low (1 event per 45s)
- Harmonic / spectral posture: home color, 10% brightness
- Spatial posture: narrow (15°), centered carrier
- Transition condition: carrier established and anchor first paired
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace and lead attention inward
- Approximate duration range: 3:00-7:00 (240s)
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: ground, harmonic bed (G#2/A2), carrier variants (C#4-E4-D#4 motif)
- Development action: vary
- Hypnotic function: pace and lead attention inward
- Energy / density posture: medium (1 event per 30s)
- Harmonic / spectral posture: contrast color, 25% brightness
- Spatial posture: medium (45°), carrier near center
- Transition condition: attention narrowed and space prepared to widen
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory
- Approximate duration range: 7:00-13:00 (360s)
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: ground, harmonic bed (A2/Bb2), air bloom, low carrier returns
- Development action: thicken and soften
- Hypnotic function: reduce orienting load
- Energy / density posture: medium (1 event per 30s)
- Harmonic / spectral posture: home color with A#3, 40% brightness
- Spatial posture: wide (90°), air in edges
- Transition condition: depth stable and core can withhold carrier
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with microdevelopment
- Approximate duration range: 13:00-20:00 (420s)
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: ground, harmonic bed (C3/D#3), rare carrier (motif fragment at 15:00, 18:00), anchor reassurance
- Development action: recur and thin
- Hypnotic function: sustain absorption
- Energy / density posture: low (1 event per 45s)
- Harmonic / spectral posture: home color with D#4, 40% brightness
- Spatial posture: medium-wide (60°), carrier near center
- Transition condition: field thinned and fractionation pre-echo seeded
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper
- Approximate duration range: 20:00-22:00 (120s)
- Hypnotic stage role: fractionation
- Target depth: deep to light-medium to deep
- Material state: ground, harmonic bed (D#3/E3), brief brightness, clearer carrier
- Development action: lighten and deepen
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: medium (1 event per 30s)
- Harmonic / spectral posture: contrast color, 50% brightness
- Spatial posture: narrow-medium (30°), carrier near center
- Transition condition: brightness closed and deeper return established
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 6 - Integration
- Name / role: Return familiar material in clearer calm form
- Approximate duration range: 22:00-23:30 (90s)
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: ground, harmonic bed (C3/Bb2), clearer carrier, anchor reassurance
- Development action: recur and clarify
- Hypnotic function: stabilize
- Energy / density posture: low (1 event per 45s)
- Harmonic / spectral posture: return color, 25% brightness
- Spatial posture: medium (45°), carrier near center
- Transition condition: fractionation redeepened
- Motion source commitment: none — short unit, no internal motion source required

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve and cadence
- Approximate duration range: 23:30-24:00 (30s)
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: ground residue, air fade
- Development action: dissolve
- Hypnotic function: reorient
- Energy / density posture: very low (no events)
- Harmonic / spectral posture: home color, 10% brightness
- Spatial posture: narrow (15°), air in rear
- Transition condition: integration cadence prepared
- Motion source commitment: none — short unit, no internal motion source required

## Orchestration Map

- Root / ground function: Ground arpeggio (F2 anchor, 87.31 Hz)
- Harmonic color function: Harmonic bed (F2/F3, A#3 for home)
- Motion function: Carrier voice and air bloom
- Foreground / event function: Motif fragments (rare, ≤ -15dB)
- Voice-adjacent induction function: Sarangi-like carrier (A3-G5, 6dB above bed)
- Anchor / trigger function: Filter dip at 1200Hz (non-alerting, -15dB)
- Air / bloom function: Diffuse noise layer (stereo edges, 0.5s attack)
- Spatial / depth function: Stereo width and reverb tail
- Silence / restraint function: Withholding anchor during induction

## Temporal Behavior

- Micro-event rate: Every 15-45 seconds (motif fragments)
- Scene-change rate: Every 75-150 seconds (harmonic shifts)
- Long-wave change: Harmonic color evolution across stages
- Maximum stasis tolerance: 45 seconds without event or harmonic shift
- Repetition policy: Events transform (e.g., motif transposed) to avoid wallpaper
- Fractionation timing: Brief upward scoop at 20:15 (D#3→E3, +10% brightness), return at 21:45
- Anchor recurrence spacing: Core (15:00), Fractionation (20:30, 21:45), Integration (22:30)

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: Narrow (15°) → Medium (45°) → Wide (90°) → Medium-wide (60°) → Narrow-medium (30°) → Medium (45°) → Narrow (15°)
- Layer placement principles: Ground centered, harmonic bed wide stable, carrier near center low motion, air in edges and rear
- Motion limits: No fast autopan (max 20°/sec)
- Verification obligation: Verify not mono, width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No perceptible change for >45s in core
  - Revision target: Add motif fragment at 16:30 and 18:45
- Failure mode: too generic
  - Evidence: Harmonic bed identical to prior composition
  - Revision target: Change home color interval stack (0,12,19 → 0,13,20)
- Failure mode: too busy
  - Evidence: More than one event per 30s in deepening
  - Revision target: Reduce events to one every 45s and lower level by 3dB
- Failure mode: weak recurrence
  - Evidence: Motif cell appears once and vanishes in core
  - Revision target: Return motif at 15:00 (core) and 22:30 (integration)
- Failure mode: weak ending
  - Evidence: Piece fades out without harmonic simplification
  - Revision target: Add F2/F3 return at 23:45 and fade from there
- Failure mode: weak spatial composition
  - Evidence: Stereo width constant at medium throughout
  - Revision target: Implement full spatial arc (narrow→wide→narrow)
- Failure mode: weak hypnotic architecture
  - Evidence: Fractionation feels like reset
  - Revision target: Ensure fractionation brightness (50%) returns to core's 40% without abrupt change
- Failure mode: weak fractionation
  - Evidence: No lightening in fractionation pocket
  - Revision target: Add brief pitch shift (D#3→E3) and brightness increase to 50%
- Failure mode: weak anchors / triggers
  - Evidence: Anchor at 15:00 too obvious (bright)
  - Revision target: Reduce anchor amplitude to -15dB and make filter dip
- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier level too low (3dB above bed)
  - Revision target: Increase carrier to 6dB above harmonic bed

## Sketch Handoff

- Required sketch style family: neuro-ambient with voice-adjacent induction
- Required formal units: 7 stages as defined in Formal Architecture
- Required material to preserve: Primary motif, harmonic signature (home/contrast/return), texture behavior
- Required hypnotic architecture to preserve: Stage map, depth trajectory, fractionation rule
- Required voice-adjacent induction behavior to preserve: Carrier (sarangi-like), cadence profile, salience ceiling
- Required development grammar to preserve: Variation rule (transposition), recurrence rule (motif returns)
- Required revision criteria to preserve: All listed above
- Explicit exclusions: No percussion, no bright bells, no abrupt changes