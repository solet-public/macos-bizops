# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-19-001
WORK_MANIFEST: wmf-neuro-ambient-composition-19-001
Status: ready

## Artistic Reading

- User image / premise: A 24-minute self-hypnosis journey through harmonic evolution, breath-length carrier phrasing, and subsalient anchoring
- Musical problem: Creating continuous hypnotic progression without abrupt transitions while maintaining harmonic identity through interval shifts
- Listener stance: Inward deep listening with attention shifting from environmental to internal absorption
- What this is not: Dramatic, pop-hook-based, cinematic tension, static drone over 120s
- Style-family fit: Sequenced Ambient (Carbon Based Lifeforms, Solar Fields) with integrated harmonic motion

## Chosen Musical Premise

- Premise sentence: A continuous harmonic evolution through interval stack shifts, carried by breath-length sarangi-like carrier phrasing and anchored by subsalient warmth blooms
- Primary dramatic motion: Deepen (through harmonic shift) then return to softened home state
- Sound-world metaphor: A slow, deepening breath in a warm cave — the air thickens and shifts as you sink deeper
- Musical consequence: Harmonic color changes via interval stack evolution (F2 major triad + minor seventh → Fm9 omit 5th), texture thickens then thins, spatial width expands and contracts
- Duration rationale: 24 minutes allows full hypnotic arc with sufficient time for harmonic evolution and anchor recurrence without rushing

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for deep absorption and reorientation
- Stage map:
  - Orientation (0:00-3:00): baseline to light — establish world and first carrier pairing
  - Induction (3:00-7:00): light to medium — pace and lead attention inward
  - Deepening (7:00-13:00): medium to deep — widen space, lower salience
  - Core absorptive work (13:00-20:00): deep — sustain absorption with micro-developments
  - Fractionation (20:00-22:00): deep to light-medium to deep — briefly lighten then return deeper
  - Integration (22:00-23:30): medium to light — stabilize and reduce depth pressure
  - Return (23:30-24:00): light to baseline — reorient attention gently
- Depth trajectory: Baseline → Light (induction start) → Medium (induction end, deepening start) → Deep (deepening end, core start) → Light-medium (fractionation lightening) → Deep (fractionation return) → Medium (integration start) → Light (integration end, return start) → Baseline
- Pacing-and-leading plan: Start with sparse carrier phrases and narrow field; lengthen rests, lower register, darken brightness, widen space to guide attention inward
- Ratification plan: Anchor trigger returns at depth thresholds to ratify state without words
- Suggestion-compatible windows: Quiet moments between carrier phrases (induction/deepening) for spoken guidance
- Reset avoidance rule: No abrupt cuts, hard transients, alerting cues, or sudden brightness spikes

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Two-note interval cell (minor third F-A) followed by falling second (A-G), forming downward settling contour
- First form: Single soft-edged glint in air layer at 120s (end of induction)
- Later forms: Returns transformed as wider interval (minor sixth F-C) in core work; filtered version with longer decay during fractionation

### Material Inventory - Harmonic Signature
- Home color: F2 root with open C (F-C), Eb warmth (F-Eb), G add9 — interval stack [0, 3, 10]
- Contrast color: Db root with common-tone F (Db-F), brighter Ab (Db-Ab) — interval stack [0, 4, 9]
- Return color: F2 root with only C and Eb (F-C-Eb), no G/Ab, upper partials softened — interval stack [0, 3]

### Material Inventory - Texture Family
- Continuous layer behavior: Ground drone (F2) with slow beating (0.1 Hz), long attack (8-45s), release (20-60s)
- Event layer behavior: Rare motif fragments and soft partial blooms, always below carrier salience
- Noise / air behavior: Pink noise high-passed at 2800 Hz with slow opening/closing (attack/release: [10,60]s)

### Material Inventory - Spatial Material
- Width behavior: Narrow (0-3:00) → Medium (3:00-7:00) → Wide (7:00-13:00) → Medium-wide (13:00-20:00) → Narrow (20:00-24:00)
- Motion behavior: Slow non-alerting drift (air layer edges move left to right over 60-120s)
- Depth behavior: Near (orientation) → Medium (induction) → Deep (deepening/core) → Slightly less deep during fractionation lightening

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) for ground drone; gentle body pulse (0.15-0.35 Hz) for harmonic pad
- Change rule: Modulation rates slow down as depth increases, speed up slightly during fractionation lightening

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1 (phrase arcs 0.9-3.3s, pauses 0.5-1.8s)
- Reference pitch-contour profile: reference_voice_pitch_contours_v1 (downward-settling dominant)
- Carrier instrument / layer: Sarangi-like bowed string, low-mid register (F2 to C4)
- Cadence behavior: Breath-length phrase arcs with soft attack and long release, downward settling
- Contour behavior: Predominantly falling/settling; occasional scoop_then_settle for permissive gestures
- Weighting behavior: Emphasis via slight register warmth and harmonic reinforcement (F-C-Eb chord at phrase end)
- Return behavior: Returns transformed by register (lower) and filtering (softer), longer decay
- Salience limit: Carrier remains below lead melody attention; never exceeds -10 dB

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Tiny low-mid warmth bloom (F2-Eb3, 0.5s duration) paired with carrier release
- First pairing: At 3:00 (end of orientation), after first carrier phrase resolves
- Withholding plan: Absent for 90 seconds (until 4:30) to preserve meaning
- Return / trigger plan: Returns at induction threshold (4:30), deepening stability point (10:00), core reassurance (16:00), fractionation re-deepening (21:00), integration (23:00)
- Salience policy: Audible enough to learn (-15 dB) but not salient enough to monitor

## Development Grammar

- Establishment rule: Introduce ground drone, harmonic bed (home color), and carrier seed in orientation
- Variation rule: Shift interval stack to [0,4,9] (contrast color) in deepening; return via [0,3] in integration
- Contrast rule: Use Db contrast (with common F) to avoid key change
- Recurrence rule: Primary motif returns transformed every 75-150s (2:30, 9:00, 16:00)
- Foreground rule: Event layer (motif fragments) perceptible only during fractionation lightening
- Withholding rule: Anchor trigger withheld for 90s after first pairing; no clear motif in orientation
- Fractionation rule: Lightening via increased brightness (air layer opening) and density; re-deepening via harmonic shift to [0,5,12] and spatial contraction
- Trigger conditioning rule: Anchor paired with carrier release to create comfort association
- Dissolution / cadence rule: End via harmonic simplification (return color) and spatial contraction to narrow
- Anti-padding rule: No material looped merely to fill time; every event has purpose (e.g., motif at 120s)

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish world and first carrier pairing
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: ground, harmonic body (home color), carrier seed
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: sparse, low density
- Harmonic / spectral posture: home color (F2 major triad + minor seventh)
- Spatial posture: narrow, centered
- Transition condition: carrier established and anchor first paired (at 3:00)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace and lead attention inward
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: ground, harmonic body (home color), carrier variants
- Development action: vary
- Hypnotic function: pace and lead attention
- Energy / density posture: moderate, increasing density
- Harmonic / spectral posture: home color (F2 major triad + minor seventh)
- Spatial posture: medium, widening
- Transition condition: attention narrowed and space prepared to widen (at 7:00)
- Motion source commitment: foreground: voice_adjacent

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: ground, harmonic body (contrast color), air layer
- Development action: vary
- Hypnotic function: reduce orienting load
- Energy / density posture: high, stable
- Harmonic / spectral posture: contrast color (Db major triad + minor seventh)
- Spatial posture: wide, stable
- Transition condition: depth stable and core can withhold carrier (at 13:00)
- Motion source commitment: parameter_group_sequence

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with micro-developments
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: ground, harmonic body (home color), rare carrier
- Development action: withhold and recur
- Hypnotic function: sustain absorption
- Energy / density posture: high, with micro-thinning
- Harmonic / spectral posture: home color (F2 major triad + minor seventh)
- Spatial posture: medium-wide, stable
- Transition condition: field thinned and fractionation pre-echo seeded (at 20:00)
- Motion source commitment: event_schedule

### Formal Unit 5 - Fractionation
- Name / role: Briefly lighten then return deeper
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep to light-medium to deep
- Material state: ground, harmonic body (home color), air layer
- Development action: answer and transform
- Hypnotic function: fractionate (brief lightening then deeper return)
- Energy / density posture: medium, with brief peak
- Harmonic / spectral posture: home color (brighter upper partials during lightening)
- Spatial posture: medium-wide to narrow
- Transition condition: fractionation complete (at 22:00)
- Motion source commitment: none — short unit, no internal motion source required

### Formal Unit 6 - Integration
- Name / role: Stabilize and reduce depth pressure
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: ground, harmonic body (return color), anchor return
- Development action: dissolve
- Hypnotic function: stabilize
- Energy / density posture: moderate, decreasing
- Harmonic / spectral posture: return color (F2 major triad without seventh)
- Spatial posture: narrow, contracting
- Transition condition: integration complete (at 23:30)
- Motion source commitment: none — short unit

### Formal Unit 7 - Return
- Name / role: Reorient attention gently
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: ground, harmonic body (return color), anchor return
- Development action: dissolve
- Hypnotic function: reorient
- Energy / density posture: low, fading
- Harmonic / spectral posture: return color (simplified)
- Spatial posture: narrow, contracting
- Transition condition: none (end of piece)
- Motion source commitment: none — short unit

## Orchestration Map

- Root / ground function: F2 drone (ground layer)
- Harmonic color function: harmonic body layer
- Motion function: air layer (slow opening/closing)
- Foreground / event function: primary motif fragments
- Voice-adjacent induction function: sarangi-like carrier layer
- Anchor / trigger function: anchor layer (low-mid warmth bloom)
- Air / bloom function: air layer
- Spatial / depth function: stereo width and placement of layers (air at edges, carrier near center)
- Silence / restraint function: withholding motif in orientation and core work

## Temporal Behavior

- Micro-event rate: every 15-45 seconds (motif fragments, air blooms)
- Scene-change rate: every 75-150 seconds (harmonic shifts, density changes)
- Long-wave change: harmonic signature evolution from home to contrast and back
- Maximum stasis tolerance: 45 seconds without deliberate change or withholding
- Repetition policy: recurrence of motif and anchor, transformed each time
- Fractionation timing: lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: first at 3:00, then withheld until 4:30 (90s), then at 10:00, 16:00, 21:00, 23:00

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow (0-3:00) → medium (3:00-7:00) → wide (7:00-13:00) → medium-wide (13:00-20:00) → narrow (20:00-24:00)
- Layer placement principles:
  - ground: centered
  - harmonic body: wide stable (stereo spread)
  - carrier: near center, low motion
  - air: edges and rear
  - anchor: local but soft (centered)
- Motion limits: no fast autopan; motion over 20-120 seconds (e.g., air layer drifts left to right in 60s)
- Verification obligation: verify not mono, not dual-mono; width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: no perceptible change for >45s, harmonic color unchanged
  - Revision target: add micro-event (motif fragment) or density shift

- Failure mode: too generic
  - Evidence: could be any ambient piece; no specific motif or harmonic signature
  - Revision target: add primary motif (minor third + falling second) and specific interval stacks

- Failure mode: too busy
  - Evidence: carrier phrases too frequent (every 1s) or motif fragments overwhelming
  - Revision target: reduce event rate to every 20-45s and lower carrier salience

- Failure mode: weak recurrence
  - Evidence: motif appears once, no return
  - Revision target: add transformed returns at 9:00 and 16:00

- Failure mode: weak ending
  - Evidence: piece fades out without harmonic simplification
  - Revision target: add return color (F2 major triad without seventh) and spatial contraction

- Failure mode: weak hypnotic architecture
  - Evidence: no clear stage progression (e.g., deepening doesn't widen space)
  - Revision target: enforce spatial arc and harmonic shift per stage

- Failure mode: weak fractionation
  - Evidence: lightening feels like a reset (e.g., bright, abrupt)
  - Revision target: use only brightness and density for lightening; keep continuity bed

- Failure mode: weak anchors / triggers
  - Evidence: anchor cue too obvious (bright, loud) or absent
  - Revision target: soften anchor to -15 dB and pair with carrier release

- Failure mode: weak voice-adjacent induction
  - Evidence: carrier is lead melody (clear contour, high register)
  - Revision target: move carrier to low-mid and use downward settling

## Sketch Handoff

- Required sketch style family: Sequenced Ambient (Carbon Based Lifeforms, Solar Fields)
- Required formal units: 7 stages as defined
- Required material to preserve: primary motif (minor third + falling second), harmonic signature, texture behavior
- Required hypnotic architecture to preserve: stage map, depth trajectory, fractionation timing, anchor return plan
- Required voice-adjacent induction behavior to preserve: carrier (sarangi-like), phrase timing [0.9,3.3s], downward settling
- Required development grammar to preserve: establishment, variation, contrast, recurrence rules
- Required revision criteria to preserve: the list above
- Explicit exclusions: no percussion, no abrupt changes, no foreground melody