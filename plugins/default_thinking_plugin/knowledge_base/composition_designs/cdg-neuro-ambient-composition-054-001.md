# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-054-001
WORK_MANIFEST: wmf-neuro-ambient-composition-054-001
Status: ready

## Artistic Reading

- User image / premise: A continuous, breath-length prosodic lead voice guides the listener through a self-hypnotic journey from orientation to return, using instrumental phrasing to evoke a hypnotist's cadence
- Musical problem: Creating continuous foreground vocal-led prosody that guides without becoming a lead melody, while maintaining hypnotic stage progression through harmonic and spatial evolution
- Listener stance: Inward deep listening with attention absorbed by the carrier's breath-length phrasing, not actively monitoring
- What this is not: A lead vocal performance, a rhythmic groove, or abrupt section changes; avoids "trick" mechanisms like alerting cues
- Style-family fit: Neuro-ambient with foreground vocal-led prosody, adhering to house style's absorption over spectacle and continuity over contrast

## Chosen Musical Premise

- Premise sentence: A continuous breath-length instrumental carrier in the low-mid register guides listeners through hypnotic stages via weighted phrase contours, harmonic evolution, and spatial unfolding
- Primary dramatic motion: Establish → Reveal → Deepen → Sustain → Lighten and Return Deeper → Stabilize → Dissolve
- Sound-world metaphor: A slow river current (carrier) flowing through a forest canopy (harmonic bed), with ripples (anchors) marking depth transitions
- Musical consequence: Carrier's phrase contours create breath-like pacing; harmonic bed widens from narrow to wide with interval shifts; spatial arc mirrors river flow
- Duration rationale: 24 minutes allows natural progression through hypnotic stages with durations matching depth requirements (e.g., 6-minute deepening for gradual absorption)

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for focused immersive listening
- Stage map:
  - Orientation / Settling: 0:00-3:00, target depth: baseline to light, musical function: establish river current (carrier) and forest canopy (harmonic bed)
  - Induction / Narrowing: 3:00-7:00, target depth: light to medium, musical function: guide attention inward via carrier phrase spacing
  - Deepening: 7:00-13:00, target depth: medium to deep, musical function: deepen current (carrier register lowers) and widen canopy (harmonic bed widens)
  - Core Absorptive Work: 13:00-20:00, target depth: deep, musical function: sustain steady flow (longer carrier phrases)
  - Fractionation: 20:00-22:00, target depth: deep to light-medium to deep, musical function: brief ripple (upward pitch scoop) then deeper return
  - Integration / Stabilization: 22:00-23:30, target depth: medium to light, musical function: slow current (carrier phrases clarify)
  - Return / Reorientation: 23:30-24:00, target depth: light to baseline, musical function: dissolve current into forest (carrier fades)
- Depth trajectory: Carrier pitch lowers from A3 to G4; harmonic bed widens from 10° to 90° then narrows to 25°; density increases gradually
- Pacing-and-leading plan: Carrier's phrase spacing (6-14s) and weighted stress points pace attention inward, with longer phrases in deepening/core work
- Ratification plan: Downward settling contours (84.5% of phrases) and harmonic bed widening at phrase endings ratify depth progression
- Suggestion-compatible windows: Inter-phrase rests (1-3s) and carrier softening at phrase endings for spoken guidance
- Reset avoidance rule: No abrupt pitch/density/spatial changes; all transitions occur over 8-30 seconds via harmonic shifts

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: 3-note interval cell (minor third + major second) rising and settling, e.g., [C4-Eb4-D4]
- First form: Orientation (A3-C#4-B3) with soft attack, long release
- Later forms: Deepening (B3-D#4-C#4) transposed up; Core work (C#4-E4-D#4 fragment)

### Material Inventory - Harmonic Signature
- Home color: F2 root with interval stack [0,3,10] (root, minor third, major seventh)
- Contrast color: A2 root with interval stack [0,4,10] (root, major third, major seventh)
- Return color: Home color with softened major seventh (gain -50%)

### Material Inventory - Texture Family
- Continuous layer behavior: Substrate drone at 87.31 Hz, harmonic recipe [1.0,2.0,3.0] with decreasing gain
- Event layer behavior: Motif fragments (1-2 per 5 minutes) only in deepening/core work
- Noise / air behavior: Air layer with slow shimmer (0.15-0.35 Hz) that brightens in fractionation

### Material Inventory - Spatial Material
- Width behavior: Narrow (10°) → wide (90°) → narrow (25°)
- Motion behavior: Carrier near center; air layer drifts slowly left to right
- Depth behavior: Harmonic bed depth 10m → 25m → 10m

### Material Inventory - Modulation Identity
- Rate family: Long breath (0.07-0.13 Hz) for substrate; gentle body pulse (0.15-0.35 Hz) for harmonic bed
- Change rule: Breath rate slows 0.01 Hz every 2 minutes; body pulse depth increases 5% in deepening

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1` (6-14s phrases, 1-3s rests)
- Reference pitch-contour profile: `reference_voice_pitch_contours_v1` (84.5% falling_settling)
- Carrier instrument / layer: Warm cello patch (GM94) low-mid register
- Cadence behavior: Phrases 6-14s with weighted stress at midpoint, soft release
- Contour behavior: 84.5% falling_settling (e.g., rise to peak then settle), 8.2% rising_opening
- Weighting behavior: Slight amplitude increase at phrase midpoint without hard attack
- Return behavior: Integration uses orientation contour with longer phrases and brighter register
- Salience limit: 6 dB above harmonic bed, 10 dB above substrate; never becomes lead melody

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Soft low-mid shimmer (0.5s, 120-180 Hz)
- First pairing: Orientation (1:45) with downward settling phrase
- Withholding plan: Absent in induction and deepening (to avoid overuse)
- Return / trigger plan: Core work (15:00), fractionation lightening (20:30), integration (22:15)
- Salience policy: Audible enough to learn (-30 dB relative to carrier), not salient for monitoring

## Development Grammar

- Establishment rule: Introduce river current (carrier) and forest canopy (harmonic bed) in orientation
- Variation rule: Carrier pitch shifts up by minor third in deepening; harmonic bed widens
- Contrast rule: Contrast color (A2) appears for 3 minutes in deepening, returns to home
- Recurrence rule: Primary motif cell recurs transformed (deepening transposed, core fragmented)
- Foreground rule: Carrier always foreground (6 dB above bed), never background
- Withholding rule: Anchor withheld in induction/deepening to preserve meaning on return
- Fractionation rule: Brief upward pitch scoop (10%) and brighter air layer for 30s, then deeper return via downward settling
- Trigger conditioning rule: Anchor paired with carrier's downward settling in orientation to associate with comfort
- Dissolution / cadence rule: Carrier fades over 30s into harmonic bed; home color simplifies
- Anti-padding rule: No event repeated within 10 minutes; all events transformed

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish river current and forest canopy
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: Carrier (A3-C#4-B3), harmonic bed (F2, [0,3,10]), substrate
- Development action: Establish
- Hypnotic function: settle and establish safety
- Energy / density posture: low, sparse
- Harmonic / spectral posture: narrow (10°), home color
- Spatial posture: narrow, centered carrier
- Transition condition: Carrier established and anchor first paired (1:45)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Guide attention inward with river current
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: Carrier (A3-C#4-B3, 6-10s phrases), harmonic bed (F2, [0,3,10]), substrate
- Development action: Vary (phrases more regular)
- Hypnotic function: pace and lead attention
- Energy / density posture: medium, slightly denser
- Harmonic / spectral posture: home color, narrow (15°)
- Spatial posture: slightly wider (20°), carrier near center
- Transition condition: Attention narrowed and space prepared to widen (6:45)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen river and deepen current
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: Carrier (B3-D#4-C#4, 8-12s phrases), harmonic bed (F2→A2 contrast color [0,4,10]), substrate
- Development action: Thicken (harmonic bed widens), transform (carrier pitch shifts)
- Hypnotic function: reduce orienting load
- Energy / density posture: medium-high, wider field
- Harmonic / spectral posture: contrast color (A2), wide (90°)
- Spatial posture: wide, carrier near center
- Transition condition: Depth stable and core can withhold carrier (12:45)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain deep river flow
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: Carrier (C4-Eb4-D4, 10-14s phrases), harmonic bed (F2 home color [0,3,10] softened 7th), substrate
- Development action: Sustain (longer phrases, anchor returns at 15:00/18:30)
- Hypnotic function: sustain absorption
- Energy / density posture: high, stable
- Harmonic / spectral posture: home color (softened), wide (90°)
- Spatial posture: wide, carrier near center
- Transition condition: Field thinned and fractionation pre-echo seeded (19:45)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 5 - Fractionation
- Name / role: Brief ripple and deeper return
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep to light-medium to deep
- Material state: Carrier (C#4-E4-D#4, 8-12s with upward scoop), harmonic bed (F2 home color brighter), air layer brightened
- Development action: Lighten then return deeper (upward pitch scoop, downward settling)
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: medium, brighter
- Harmonic / spectral posture: home color (brighter), wide (90°)
- Spatial posture: wide, carrier near center
- Transition condition: Fractionation lightening complete (21:30)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 6 - Integration
- Name / role: Stabilize river flow
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: Carrier (B3-D#4-C#4, 8-12s), harmonic bed (F2 home color [0,3,10] normal 7th), substrate
- Development action: Dissolve (phrases more resonant, anchor at 22:15)
- Hypnotic function: stabilize
- Energy / density posture: medium, calming
- Harmonic / spectral posture: home color (normal), narrowing to 25°
- Spatial posture: narrowing, carrier near center
- Transition condition: Depth stabilized (23:25)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 7 - Return
- Name / role: Dissolve river into forest
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: Carrier (A3-C#4-B3, fading), harmonic bed (F2 home color simplified)
- Development action: Dissolve (carrier fades, bed simplifies)
- Hypnotic function: reorient
- Energy / density posture: low, fading
- Harmonic / spectral posture: home color (simplified), narrow (10°)
- Spatial posture: narrow, carrier center
- Transition condition: None
- Motion source commitment: foreground: voice_adjacent

## Orchestration Map

- Root / ground function: Substrate drone (87.31 Hz) grounds listener
- Harmonic color function: Harmonic bed supplies pitch identity (F2 root)
- Motion function: Carrier makes time perceptible via phrase arcs
- Foreground / event function: Primary motif fragments (rare events)
- Voice-adjacent induction function: Carrier carries hypnotist cadence
- Anchor / trigger function: Shimmer anchor triggered by carrier settling
- Air / bloom function: Creates openness in deepening/core work
- Spatial / depth function: Stereo width and harmonic bed depth carry space
- Silence / restraint function: Anchor withheld in induction/deepening

## Temporal Behavior

- Micro-event rate: One motif fragment every 5-10 minutes (e.g., 8:30, 15:00)
- Scene-change rate: Every 6-7 minutes (deepening, core work, fractionation)
- Long-wave change: Harmonic bed shifts from home to contrast color and back
- Maximum stasis tolerance: 45 seconds without harmonic shift or motif fragment
- Repetition policy: Motif cell recurs transformed each time (no identical repeats)
- Fractionation timing: Upward scoop 20:30-21:00, deeper return 21:00-21:30
- Anchor recurrence spacing: Core work (15:00), fractionation lightening (20:30), integration (22:15)

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: Narrow (10°) → wide (90°) → narrow (25°)
- Layer placement principles: Carrier near center; air layer edges/rear; anchor local (center-left/right)
- Motion limits: Air drift at 0.5°/second (slow, non-alerting)
- Verification obligation: Verify width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No harmonic shift or motif recurrence >45 seconds
  - Revision target: Add micro-event (motif fragment) or harmonic shift

- Failure mode: too generic
  - Evidence: Carrier contour identical throughout (no falling_settling variation)
  - Revision target: Apply stage-specific pitch-contour profiles

- Failure mode: weak fractionation
  - Evidence: Fractionation lightening feels like reset (bright, abrupt)
  - Revision target: Make lightening gradual (upward scoop) and return deeper via downward settling

- Failure mode: weak anchors / triggers
  - Evidence: Anchor too obvious (bright, sharp) or absent in core work
  - Revision target: Soften to shimmer (120-180 Hz, -30 dB) and ensure return at 15:00

- Failure mode: weak ending
  - Evidence: Carrier fades without harmonic simplification
  - Revision target: Add bed simplification and carrier fade over 30 seconds

## Sketch Handoff

- Required sketch style family: neuro-ambient with voice-adjacent induction
- Required formal units: 7 (orientation, induction, deepening, core absorptive work, fractionation, integration, return)
- Required material to preserve: Primary motif cell (3-note interval), harmonic signature (home/contrast/return colors)
- Required hypnotic architecture to preserve: Stage map with depth trajectory and fractionation rule
- Required voice-adjacent induction behavior to preserve: Carrier at 6 dB above bed, phrases 6-14s with falling_settling contour
- Required development grammar to preserve: Variation rule (pitch shift), recurrence rule (motif transformed)
- Required revision criteria to preserve: All listed above
- Explicit exclusions: No percussion, no abrupt transitions, no foreground melody