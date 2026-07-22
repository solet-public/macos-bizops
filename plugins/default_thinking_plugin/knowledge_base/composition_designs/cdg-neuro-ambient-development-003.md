# Composition Design Document
DESIGN ID: cdg-neuro-ambient-development-003
WORK_MANIFEST: wmf-neuro-ambient-development-001
Status: ready

## Artistic Reading

- User image / premise: A 24-minute music-only self-hypnosis journey through layered harmonic motion, voice-adjacent carrier, and stage-based depth progression
- Musical problem: Creating continuous hypnotic form without abrupt transitions while maintaining harmonic development as the primary engine
- Listener stance: Inward deep listening with attention shifting from environmental awareness to internal absorption and back
- What this is not: Dramatic, sad narrative, triumphant, pop-hook-based, cinematic tension
- Style-family fit: Sequenced ambient (Carbon Based Lifeforms/Solar Fields) with spectral color and process-and-phasing as core mechanisms

## Chosen Musical Premise

- Premise sentence: A continuous harmonic river flowing through seven hypnotic stages, using interval-based arpeggios and moving bass roots to guide the listener from external orientation into deep absorption, with a sarangi-like carrier providing voice-adjacent cadence
- Primary dramatic motion: Deepen and dissolve through harmonic evolution, spatial widening, and carrier transformation
- Sound-world metaphor: A slow-moving river of harmonic color that deepens as it flows, with occasional ripples (motif) and gentle eddies (fractionation)
- Musical consequence: The river metaphor changes harmony through slow root movement, texture by adding/removing harmonic partials (water clarity), register by shifting river depth, and space by widening the riverbed
- Duration rationale: 24 minutes earned through seven distinct hypnotic stages requiring time for depth establishment; core work (7m) sustains absorption, fractionation (2m) provides brief lightening without reset

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for guided meditation and trance states
- Stage map:
  - Orientation / Settling: 3m, baseline to light depth, function: establish safety and first carrier pairing
  - Induction / Narrowing: 4m, light to medium depth, function: pace attention with carrier phrasing
  - Deepening: 6m, medium to deep depth, function: reduce orienting load and widen space
  - Core Absorptive Work: 7m, deep depth, function: sustain absorption with rare carrier returns
  - Fractionation Pocket: 2m, deep to light-medium to deep, function: briefly lighten then return deeper
  - Integration / Stabilization: 1.5m, medium to light depth, function: stabilize with clearer carrier
  - Return / Reorientation: 0.5m, light to baseline depth, function: dissolve and reorient
- Depth trajectory: Baseline (0m) → Light (1.5m) → Medium (4m) → Deep (10m) → Light-Medium (20.5m) → Medium (23m) → Baseline (24m)
- Pacing-and-leading plan: Establish slow ground with sparse carrier phrases → lengthen rests and lower register to narrow attention → widen space and darken brightness to deepen
- Ratification plan: Sarangi carrier's downward-settling phrase returns at key transitions (3m, 7m, 13m) to ratify depth states
- Suggestion-compatible windows: Quiet moments after carrier phrases (0.5-1.2s) and during fractionation lightening
- Reset avoidance rule: No abrupt changes in harmonic root, brightness, or stereo width; all transitions over 3-5 seconds

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Three-note interval cell (minor third + major second) in descending order, 0.3-1.5s gaps between notes
- First form: Introduced at 0:45 as soft arpeggio on home root (F2)
- Later forms: Transposed up by minor third at 7:00, transposed down by major second at 13:00, filtered/wider version at 22:00

### Material Inventory - Harmonic Signature
- Home color: Root F2 (87.31 Hz) with interval stack [0, 3, 10] (F2, A2, F3)
- Contrast color: Root G#2 (98.00 Hz) with interval stack [0, 4, 11] (G#2, C3, G#3)
- Return color: Root F2 with interval stack [0, 3] (F2, A2) and softened seventh

### Material Inventory - Texture Family
- Continuous layer behavior: Low-complexity substrate drone at 87.31 Hz with harmonic recipe [1.0, 2.0, 3.0] and progressively lower gain
- Event layer behavior: Sparse harmonic motif events (every 15-45s) transformed across stages
- Noise / air behavior: Soft wide air layer with slow 0.1 Hz LFO opening/closing high end (2500-4700 Hz) over 60s

### Material Inventory - Spatial Material
- Width behavior: Narrow (15°) → Medium (40°) → Wide (80°) → Medium-wide (65°) → Narrow (20°)
- Motion behavior: Air layer drifts left to right at 0.5°/second during deepening, stationary for core work
- Depth behavior: Reverb tail increases from 1.2s (orientation) to 2.5s (deepening), reduces to 0.8s (return)

### Material Inventory - Modulation Identity
- Rate family: Slow incommensurable rate (0.07-0.13 Hz) for harmonic bed filter, 0.2-0.4 Hz LFO for air layer brightness
- Change rule: Modulation rates slow during deepening (0.13 Hz → 0.07 Hz), speed slightly during fractionation (0.15 Hz → 0.2 Hz)

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1
- Reference pitch-contour profile: falling_settling from reference_voice_pitch_contours_v1
- Carrier instrument / layer: Sarangi-like bowed string (SoundFont GM 89) at low salience
- Cadence behavior: Breath-length phrases (1.5-3s) with permissive pauses (0.75s), downward settling at deepening
- Contour behavior: Falling/settling (e.g., F3 → D3 over 2s) with soft attack/release
- Weighting behavior: Slight amplitude increase at phrase midpoint (not peak)
- Return behavior: Transposed, filtered returns at stage transitions
- Salience limit: Carrier amplitude never exceeds -15 dB relative to harmonic bed

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: 0.2s spectral glint at 3500 Hz (upper partial of harmonic bed)
- First pairing: At 3:00, coincides with carrier's downward settling phrase
- Withholding plan: Absent during orientation and core work (17 minutes withheld)
- Return / trigger plan: Returns at 22:00 and 23:45, coinciding with deeper settling events
- Salience policy: Anchor amplitude -25 dB (audible but not attention-demanding)

## Development Grammar

- Establishment rule: Introduce home harmonic color (F2, A2, F3) and carrier in sparse orientation
- Variation rule: Transform motif by transposition, filtering, widening across stages (not adding new elements)
- Contrast rule: Change harmonic root and interval stack for contrast without disrupting continuity
- Recurrence rule: Motif returns transformed (7:00, 13:00, 22:00) with increasing transformation
- Foreground rule: Carrier becomes slightly more legible only during induction and fractionation
- Withholding rule: Anchor withheld for 17 minutes to preserve meaning at return
- Fractionation rule: Brief brightness increase (3500 Hz) for 60s, then return to deep field via darkening (2900 Hz)
- Trigger conditioning rule: Anchor paired with carrier settling to associate with comfort
- Dissolution / cadence rule: Ends when harmonic bed simplifies to root only (F2) and width narrows
- Anti-padding rule: No material repeated >45s without transformation

## Formal Architecture

### Formal Unit 1 - Orientation / Settling (0:00-3:00)
- Name / role: Establish world and first carrier pairing
- Approximate duration range: 2.5-3.5 minutes
- Hypnotic stage role: Orientation
- Target depth: Baseline to light
- Material state: Ground, harmonic bed (home color), carrier seed
- Development action: Establish
- Hypnotic function: Settle and establish safety
- Energy / density posture: Low (0.3)
- Harmonic / spectral posture: Home color, 1800 Hz
- Spatial posture: Narrow (15°), centered ground
- Transition condition: Carrier established and anchor first paired

### Formal Unit 2 - Induction / Narrowing (3:00-7:00)
- Name / role: Pace and lead attention
- Approximate duration range: 3.5-4.5 minutes
- Hypnotic stage role: Induction
- Target depth: Light to medium
- Material state: Ground, harmonic bed (home color), carrier variants
- Development action: Vary
- Hypnotic function: Pace and lead attention
- Energy / density posture: Medium (0.5)
- Harmonic / spectral posture: Home color, 2400 Hz
- Spatial posture: Medium (40°), carrier near center
- Transition condition: Attention narrowed and space prepared to widen

### Formal Unit 3 - Deepening (7:00-13:00)
- Name / role: Widen space and stabilize root memory
- Approximate duration range: 5.5-6.5 minutes
- Hypnotic stage role: Deepening
- Target depth: Medium to deep
- Material state: Ground, harmonic bed (contrast color), air layer, low carrier returns
- Development action: Thicken and widen
- Hypnotic function: Reduce orienting load
- Energy / density posture: High (0.8)
- Harmonic / spectral posture: Contrast color, 3500 Hz
- Spatial posture: Wide (80°), air layer drifting left to right
- Transition condition: Depth stable and core can withhold carrier

### Formal Unit 4 - Core Absorptive Work (13:00-20:00)
- Name / role: Sustain absorption with microdevelopment
- Approximate duration range: 6.5-7.5 minutes
- Hypnotic stage role: Core absorptive work
- Target depth: Deep
- Material state: Ground, harmonic bed (contrast color), rare carrier, anchor softening
- Development action: Withhold and thin
- Hypnotic function: Sustain absorption
- Energy / density posture: Medium (0.6)
- Harmonic / spectral posture: Contrast color, 3100 Hz
- Spatial posture: Wide (80°), carrier embedded at -25 dB
- Transition condition: Field thinned and fractionation pre-echo seeded

### Formal Unit 5 - Fractionation Pocket (20:00-22:00)
- Name / role: Briefly lighten then return deeper
- Approximate duration range: 1.5-2.5 minutes
- Hypnotic stage role: Fractionation
- Target depth: Deep to light-medium to deep
- Material state: Ground, harmonic bed (contrast color), brief brightness, clearer carrier
- Development action: Brighten and answer
- Hypnotic function: Briefly lighten then return deeper
- Energy / density posture: Medium (0.7)
- Harmonic / spectral posture: Contrast color, 3500 Hz (lightening), then 2900 Hz (return)
- Spatial posture: Medium-wide (65°), carrier more centered
- Transition condition: Brightness closed and deeper return established

### Formal Unit 6 - Integration (22:00-23:30)
- Name / role: Return familiar material in clearer calm form
- Approximate duration range: 1.0-2.0 minutes
- Hypnotic stage role: Integration
- Target depth: Medium to light
- Material state: Ground, harmonic bed (return color), clearer carrier, anchor reassurance
- Development action: Recur and clarify
- Hypnotic function: Stabilize
- Energy / density posture: Medium (0.5)
- Harmonic / spectral posture: Return color, 3100 Hz
- Spatial posture: Medium-wide (65°), carrier at -18 dB
- Transition condition: Final carrier return prepares cadence

### Formal Unit 7 - Return / Reorientation (23:30-24:00)
- Name / role: Dissolve and cadence
- Approximate duration range: 0.3-0.7 minutes
- Hypnotic stage role: Return
- Target depth: Light to baseline
- Material state: Ground residue, air fade
- Development action: Dissolve and narrow
- Hypnotic function: Reorient
- Energy / density posture: Low (0.2)
- Harmonic / spectral posture: Root only, 2200 Hz
- Spatial posture: Narrow (20°), air layer fading to silence
- Transition condition: Final public delivery tail complete

## Orchestration Map

- Root / ground function: Substrate drone (87.31 Hz) with harmonic recipe [1.0, 2.0, 3.0]
- Harmonic color function: Harmonic bed (GM program 89) with interval stack [0,3,10] for home color
- Motion function: Air layer (slow drift) and harmonic bed filter automation
- Foreground / event function: Primary motif (sparse events, every 15-45s)
- Voice-adjacent induction function: Sarangi carrier (GM 89, low salience)
- Anchor / trigger function: Spectral glint at 3500 Hz (paired with carrier phrase end)
- Air / bloom function: Noise layer with slow LFO for brightness
- Spatial / depth function: Stereo width and reverb tail control
- Silence / restraint function: Withholding of anchor during core work

## Temporal Behavior

- Micro-event rate: One motif event every 15-45 seconds (e.g., 0:45, 2:30, 7:15)
- Scene-change rate: Every 6-8 minutes (at stage transitions)
- Long-wave change: Harmonic root changes at 7:00 (deepening) and 13:00 (core work)
- Maximum stasis tolerance: 45 seconds without micro-event or harmonic shift
- Repetition policy: Motif returns transformed, not identical; no looped events without change
- Fractionation timing: 20:00-22:00 (lightening at 20:00, return to deep field by 21:30)
- Anchor recurrence spacing: First at 3:00, then at 22:00 and 23:45 (spaced by 19m, then 1.5m)

## Spatial Composition

- Stereo requirement: Stereo
- Spatial arc: Narrow (15°) → Medium (40°) → Wide (80°) → Medium-wide (65°) → Narrow (20°)
- Layer placement principles: Ground centered, harmonic bed wide stable, carrier near center low motion, air edges and rear
- Motion limits: Air layer drift at 0.5°/second (slow, not alerting)
- Verification obligation: Verify stereo width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No harmonic or spatial change for >45 seconds
  - Revision target: Add micro-event (motif) or slow harmonic shift

- Failure mode: too generic
  - Evidence: Sounds like any ambient drone without specific motif/anchor
  - Revision target: Add the specific interval cell and anchor pairing

- Failure mode: too busy
  - Evidence: Carrier or motif dominates >10% of time
  - Revision target: Reduce carrier to -25 dB, limit motif events

- Failure mode: weak recurrence
  - Evidence: Motif appears once without return
  - Revision target: Add transformed returns at 7:00 and 22:00

- Failure mode: weak ending
  - Evidence: Fades out without harmonic simplification
  - Revision target: Simplify to root only and narrow width

- Failure mode: weak spatial composition
  - Evidence: Output mono or static width
  - Revision target: Implement full width arc

- Failure mode: weak hypnotic architecture
  - Evidence: No clear stage progression (e.g., no deepening)
  - Revision target: Add harmonic root change at 7:00 and widen space

- Failure mode: weak fractionation
  - Evidence: Fractionation lightening feels like reset
  - Revision target: Keep deep field bed under fractionation, return to deeper state

- Failure mode: weak anchors / triggers
  - Evidence: Anchor too obvious or absent
  - Revision target: Set anchor to -25 dB and pair with carrier phrase end

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier too bright or mechanical
  - Revision target: Use sarangi patch with soft attack and downward settling

## Sketch Handoff

- Required sketch style family: sequenced_ambient
- Required formal units: 7 stages as defined in Formal Architecture
- Required material to preserve: Primary motif, harmonic signature (home/contrast/return), texture behavior
- Required hypnotic architecture to preserve: Stage map, depth trajectory, fractionation lightening and return
- Required voice-adjacent induction behavior to preserve: Sarangi carrier, breath-length phrasing, downward settling
- Required development grammar to preserve: Establishment, variation, contrast, recurrence rules
- Required revision criteria to preserve: All listed above
- Explicit exclusions: No percussion, no abrupt transitions, no static harmonic bed >120s