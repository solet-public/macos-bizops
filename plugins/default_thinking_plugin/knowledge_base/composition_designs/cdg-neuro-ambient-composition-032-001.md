# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-032-001
WORK_MANIFEST: wmf-neuro-ambient-composition-032-001
Status: ready

## Artistic Reading

- User image / premise: A continuous breath-length vocal carrier guiding self-hypnosis through natural prosody, not spoken words
- Musical problem: Creating hypnotic depth without rhythmic or melodic hooks that disrupt absorption
- Listener stance: Following the carrier's breath-length phrasing as a natural anchor for attention
- What this is not: Pop melody, rhythmic groove, or vocal performance; no transient percussion
- Style-family fit: Long-form self-hypnosis journey with voice-adjacent induction, not ambient wallpaper

## Chosen Musical Premise

- Premise sentence: A sarangi-like carrier delivers breath-length phrases with 84.5% falling-settling contours, guiding the listener through harmonic bed evolution without melodic repetition
- Primary dramatic motion: Deepening → fractionation (brief lightening) → deeper return, all through continuous carrier
- Sound-world metaphor: A slow-moving river with occasional gentle eddies that deepen the current without breaking flow
- Musical consequence: Harmonic bed shifts from F2-F3-A3 (home) to Bb2-D3-F#3 (contrast), then returns to F2-A3-C4; carrier phrases lengthen from 6s to 14s during deepening
- Duration rationale: 24 minutes allows for three harmonic shifts (home→contrast→return), two fractionation cycles, and natural absorption time without artificial pacing

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis with continuous carrier for deep absorption
- Stage map:
  - Orientation / Settling (0:00-3:00): Establish carrier timbre and first anchor pairing
  - Induction / Narrowing (3:00-7:00): Pace attention inward through carrier phrasing
  - Deepening (7:00-13:00): Reduce orienting load via harmonic darkening and carrier register lowering
  - Core Absorptive Work (13:00-20:00): Sustain depth with rare motif fragments and anchor returns
  - Fractionation Pocket (20:00-22:00): Brief lightening via carrier brightness increase, then deeper return
  - Integration (22:00-23:30): Return to orientation motifs in calmer register
  - Return / Reorientation (23:30-24:00): Dissolve carrier into air residue
- Depth trajectory: Baseline (orientation) → light (induction) → medium (deepening) → deep (core) → light-medium (fractionation) → medium (integration) → baseline (return)
- Pacing-and-leading plan: Carrier phrases establish 6–14s breath arcs; pauses after settling phrases create natural attention narrowing
- Ratification plan: Anchor cue (high-frequency glint) returns at 15:20 and 22:45, paired with carrier settling
- Suggestion-compatible windows: All anchor returns occur during deepening (15:20) and integration (22:45), with bed warming 0.5s before cue
- Reset avoidance rule: No harmonic shifts >12 seconds, no brightness spikes above 8kHz, no carrier phrase <6s

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: 3-note interval cell (F-A-C) with falling minor third + ascending major second
- First form: Single phrase at 1.5s duration (F-A-C) in orientation, soft attack/release
- Later forms: Transposed down minor third (D-F-A) in deepening; widened to F-C-E with longer decay in integration

### Material Inventory - Harmonic Signature
- Home color: F2 (root), F3 (fifth), A3 (major third) — warm, stable triad at 87.31/174.62 Hz
- Contrast color: Bb2 (root), D3 (major third), F#3 (fifth) — darker, ambiguous color for fractionation
- Return color: F2 (root), A3 (major third), C4 (fifth) — home color with brighter upper partial

### Material Inventory - Texture Family
- Continuous layer: F2 drone (87.31 Hz) with 0.15Hz amplitude modulation creating breath-like field
- Event layer: Motif fragments (3-note cell) every 20–40s, at -15dB relative to carrier
- Noise / air: High-frequency noise (filtered >8kHz) that widens during deepening, contracts in return

### Material Inventory - Spatial Material
- Width behavior: Narrow (0:00) → medium (3:00) → wide (7:00) → medium-wide (13:00) → narrow (24:00)
- Motion behavior: Air layer drifts left→right during deepening (7:00–13:00), returns in integration
- Depth behavior: Carrier near center (shallow depth); harmonic bed moves from close to distant during deepening

### Material Inventory - Modulation Identity
- Rate family: Incommensurable slow rates (drone 0.15Hz → 0.07Hz; harmonic bed 0.25Hz)
- Change rule: Modulation rates gradually slow over piece, with drone rate halving by core work

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1` (6–14s phrases, 84.5% falling_settling)
- Reference pitch-contour profile: `reference_voice_pitch_contours_v1` (84.5% falling_settling)
- Carrier instrument: Sarangi-like bowed string with soft attack, breathy release
- Cadence behavior: 6–14s phrase arcs with permissive pauses (0.5–1.8s), downward settling at end
- Contour behavior: 84.5% falling_settling, occasional rising_opening (fractionation)
- Weighting behavior: Slight sustain increase at phrase peak, warmth in low-mid register
- Return behavior: Primary motif returns transformed (transposed/wider) at 15:20 and 22:45
- Salience limit: Carrier always ≥6dB above harmonic bed, ≥10dB above substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: 0.5s high-frequency glint (12kHz, filtered to avoid sharpness)
- First pairing: At 3:15 in induction (paired with carrier settling phrase)
- Withholding plan: Absent during orientation and deepening, returns only in core/integration
- Return / trigger plan: 15:20 (core absorptive work), 22:45 (integration)
- Salience policy: Audible at -15dB relative to carrier, not salient enough for monitoring

## Development Grammar

- Establishment rule: Introduce primary motif and home color in orientation
- Variation rule: Transpose motif down minor third during deepening; widen interval in integration
- Contrast rule: Shift harmonic bed to contrast color (Bb2-D3-F#3) only during fractionation
- Recurrence rule: Anchor cue returns at 15:20 and 22:45, transformed each time
- Foreground rule: Carrier always present; no other layer becomes foreground
- Withholding rule: Anchor absent during orientation and deepening to preserve meaning
- Fractionation rule: 60s lightening (carrier brightness increase, wider stereo) → 60s deeper return (darker carrier, narrower stereo)
- Trigger conditioning rule: Anchor paired with carrier settling; returns evoke "deepening" state
- Dissolution / cadence rule: Final fade begins at 23:45 with carrier decaying into air residue
- Anti-padding rule: No event repetition under 10s; all micro-events serve harmonic or spatial purpose

## Formal Architecture

### Formal Unit A - Orientation / Settling
- Name / role: Establish carrier timbre and first anchor pairing
- Approximate duration range: 0:00-3:00 (180s)
- Hypnotic stage role: Orientation
- Target depth: Baseline to light
- Material state: Home color harmonic bed, carrier present at full salience
- Development action: Establish primary motif and anchor identity
- Hypnotic function: Settle and establish safety
- Energy / density posture: Low, sparse
- Harmonic / spectral posture: Home color (F2-F3-A3), bright upper partials muted
- Spatial posture: Narrow width, carrier centered
- Transition condition: Carrier established and anchor first paired (3:15)
- Motion source commitment: `foreground: voice_adjacent`, `parameter_group_sequence` (home color harmonic bed)

### Formal Unit B - Induction / Narrowing
- Name / role: Pace attention inward through carrier phrasing
- Approximate duration range: 3:00-7:00 (240s)
- Hypnotic stage role: Induction
- Target depth: Light to medium
- Material state: Home color harmonic bed, carrier phrasing regularized
- Development action: Introduce anchor pairing at 3:15, withhold until core
- Hypnotic function: Pace and lead attention inward
- Energy / density posture: Moderate, increasing focus
- Harmonic / spectral posture: Home color with subtle harmonic bed widening
- Spatial posture: Medium width, carrier near center
- Transition condition: Attention narrowed and space prepared to widen (7:00)
- Motion source commitment: `foreground: voice_adjacent`, `event_schedule` (anchor at 3:15)

### Formal Unit C - Deepening
- Name / role: Reduce orienting load via harmonic darkening and carrier register lowering
- Approximate duration range: 7:00-13:00 (360s)
- Hypnotic stage role: Deepening
- Target depth: Medium to deep
- Material state: Harmonic bed shifts toward contrast color, carrier register lowers
- Development action: Darken harmonic bed; lower carrier pitch by minor third
- Hypnotic function: Reduce orienting load and stabilize root memory
- Energy / density posture: Low, deepening
- Harmonic / spectral posture: Contrast color (Bb2-D3-F#3) emerging
- Spatial posture: Wide width, air layer drifting left→right
- Transition condition: Depth stable and core can withhold carrier (13:00)
- Motion source commitment: `foreground: voice_adjacent`, `parameter_group_sequence` (harmonic bed shift)

### Formal Unit D - Core Absorptive Work
- Name / role: Sustain depth with rare motif fragments and anchor returns
- Approximate duration range: 13:00-20:00 (420s)
- Hypnotic stage role: Core absorptive work
- Target depth: Deep
- Material state: Home color harmonic bed, rare motif fragments at 15:20 and 17:40
- Development action: Withhold anchor until core; return at 15:20 with deeper settling
- Hypnotic function: Sustain absorption without inert stasis
- Energy / density posture: Very low, sparse events
- Harmonic / spectral posture: Home color with subtle contrast color hints
- Spatial posture: Medium-wide, carrier near center
- Transition condition: Field thinned and fractionation pre-echo seeded (20:00)
- Motion source commitment: `foreground: voice_adjacent`, `event_schedule` (anchor at 15:20, motif fragment at 17:40)

### Formal Unit E - Fractionation Pocket
- Name / role: Briefly lighten attention then return deeper without reset
- Approximate duration range: 20:00-22:00 (120s)
- Hypnotic stage role: Fractionation
- Target depth: Deep to light-medium to deep
- Material state: Contrast color harmonic bed, carrier brightness increase
- Development action: Lighten via carrier brightness (20:00-21:00), return deeper (21:00-22:00)
- Hypnotic function: Briefly lighten then return deeper
- Energy / density posture: Moderate, temporary lift
- Harmonic / spectral posture: Contrast color (Bb2-D3-F#3) with brighter upper partials
- Spatial posture: Wide to medium-wide, air layer contracts
- Transition condition: Brightness closed and deeper return established (22:00)
- Motion source commitment: `foreground: voice_adjacent`, "none — short unit, no additional motion source required"

### Formal Unit F - Integration
- Name / role: Return familiar material in calmer register
- Approximate duration range: 22:00-23:30 (90s)
- Hypnotic stage role: Integration
- Target depth: Medium to light
- Material state: Return color harmonic bed, carrier returns to orientation motifs
- Development action: Transform primary motif (F-C-E) with longer decay
- Hypnotic function: Stabilize and confirm journey
- Energy / density posture: Low, calm
- Harmonic / spectral posture: Return color (F2-A3-C4) with warmer low-mid
- Spatial posture: Medium-wide to narrow, air layer softens
- Transition condition: Final carrier return prepares cadence (23:30)
- Motion source commitment: `foreground: voice_adjacent`, "none — short unit, no additional motion source required"

### Formal Unit G - Return / Reorientation
- Name / role: Dissolve carrier into air residue for reorientation
- Approximate duration range: 23:30-24:00 (30s)
- Hypnotic stage role: Return
- Target depth: Light to baseline
- Material state: Carrier decays into air residue, harmonic bed fades
- Development action: Dissolve carrier via slow release and spatial contraction
- Hypnotic function: Reorient without abrupt arousal
- Energy / density posture: Very low, fading
- Harmonic / spectral posture: Home color residue with no upper partials
- Spatial posture: Narrow width, air layer fades to rear
- Transition condition: Final public delivery tail complete (24:00)
- Motion source commitment: `foreground: voice_adjacent`, "none — short unit, no additional motion source required"

## Orchestration Map

- Root / ground function: F2 drone (87.31 Hz) with 0.15Hz amplitude modulation
- Harmonic color function: F3 (174.62 Hz) as harmonic bed, shifting to contrast color
- Motion function: Carrier's breath-length phrasing and anchor cues
- Foreground / event function: Primary motif fragments (3-note cell) at -15dB
- Voice-adjacent induction function: Sarangi-like carrier with 6–14s phrases
- Anchor / trigger function: High-frequency glint (12kHz) at -15dB
- Air / bloom function: High-frequency noise (>8kHz) with spatial drift
- Spatial / depth function: Carrier near center, air at edges; width arc from narrow to wide
- Silence / restraint function: Anchor withheld during orientation and deepening

## Temporal Behavior

- Micro-event rate: Motif fragments every 20–40 seconds (15:20, 17:40)
- Scene-change rate: Harmonic bed shifts at 7:00 (deepening), 20:00 (fractionation)
- Long-wave change: Two harmonic shifts (home→contrast at 20:00, contrast→return at 22:00)
- Maximum stasis tolerance: 45 seconds without micro-event or harmonic shift
- Repetition policy: Motif fragments transform (transposed, widened) on return; no identical repeats
- Fractionation timing: 20:00–21:00 (lightening), 21:00–22:00 (deeper return)
- Anchor recurrence spacing: 15:20 (core), 22:45 (integration) — both within stage duration

## Spatial Composition

- Stereo requirement: Stereo with distinct layer roles
- Spatial arc: Narrow (0:00) → medium (3:00) → wide (7:00) → medium-wide (13:00) → narrow (24:00)
- Layer placement principles: Carrier near center, air at edges/rear; harmonic bed wide-stable
- Motion limits: Air layer drifts 0.5–1.2 seconds per second (no fast autopan)
- Verification obligation: Confirm not mono, width arc audible, motion non-alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No harmonic shift or motif transformation for >2 minutes (e.g., core work)
  - Revision target: Add harmonic bed shift at 16:00 and motif fragment transformation

- Failure mode: weak fractionation
  - Evidence: Lightening feels like new section (e.g., abrupt brightness spike)
  - Revision target: Reduce fractionation brightness to <10% of carrier level; ensure deeper return uses contrast color

- Failure mode: weak anchors
  - Evidence: Anchor cue at 15:20 is too sharp or frequent (e.g., repeated every 3 minutes)
  - Revision target: Filter anchor to 12kHz with 0.5s duration; pair only with carrier settling

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier phrases <6s or >14s; salience below 6dB above bed
  - Revision target: Enforce 6–14s phrase arcs; increase carrier level by 3dB

- Failure mode: weak hypnotic architecture
  - Evidence: No depth trajectory (e.g., deepening stage has no harmonic darkening)
  - Revision target: Add harmonic bed shift to contrast color at 20:00; lower carrier pitch by minor third

## Sketch Handoff

- Required sketch style family: Long-Form Self-Hypnosis Journey with Prosodic Lead
- Required formal units: A-G (orientation to return) as defined in Formal Architecture
- Required material to preserve: Primary motif, harmonic signature (home/contrast/return), anchor identity
- Required hypnotic architecture to preserve: Stage map, depth trajectory (baseline→light→medium→deep→light-medium→medium→baseline), fractionation behavior
- Required voice-adjacent induction behavior to preserve: Carrier instrument (sarangi-like), phrase duration 6–14s, salience ≥6dB above bed
- Required development grammar to preserve: Variation rule (motif transposition), Contrast rule (harmonic shift only in fractionation)
- Required revision criteria to preserve: All five failure modes with evidence and targets
- Explicit exclusions: No percussion, no melodic repetition, no abrupt harmonic shifts >12s