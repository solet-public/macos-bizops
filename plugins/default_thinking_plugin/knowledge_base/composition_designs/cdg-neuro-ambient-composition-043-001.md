# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-043-001
WORK_MANIFEST: wmf-neuro-ambient-composition-043-001
Status: ready

## Artistic Reading

- User image / premise: A continuous prosodic lead voice guides the listener through a self-hypnotic journey using breath-length phrases and settling contours, embedded in a dynamic ambient bed.
- Musical problem: Creating continuous conditionable carrier that deepens absorption without becoming foreground melody, while supporting seven-stage hypnotic architecture with soft transitions.
- Listener stance: Following the continuous lead as a guide, shifting from broad environmental to internal absorption and gentle reorientation.
- What this is not: Pop hook, dramatic tension, cinematic narrative, or percussion-driven groove; no transient attacks or alerting cues.
- Style-family fit: Neuro-ambient with instrumental voice-adjacent carrier (sarangi-like), evolving harmonic bed, and nonverbal anchoring; fits continuous form with soft boundaries.

## Chosen Musical Premise

- Premise sentence: A sarangi-like carrier guides the listener through seven-stage absorption using breath-length phrases and settling contours, while harmonic bed widens and deepens in spectral color to support the journey.
- Primary dramatic motion: Deepen (gradual absorption through stage progression).
- Sound-world metaphor: A slow, warm river flowing over smooth stones with gentle ripples that deepen the current without breaking flow.
- Musical consequence: River surface (carrier) remains continuous; water depth and clarity change with stages. Stones (harmonic bed) shift in color/resonance to support journey.
- Duration rationale: 24 minutes allows gradual, non-rushed progression through stages (each stage long enough to feel settled in; transitions as natural as river flow).

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis for absorption and reorientation.
- Stage map:
  - Orientation (0:00-3:00): baseline to light, settle and establish safety.
  - Induction (3:00-7:00): light to medium, pace and lead attention inward.
  - Deepening (7:00-13:00): medium to deep, reduce orienting load and widen space.
  - Core absorptive work (13:00-20:00): deep, sustain absorption with micro-developments.
  - Fractionation (20:00-22:00): deep to light-medium to deep, briefly lighten then return deeper.
  - Integration (22:00-23:30): medium to light, stabilize and prepare for reorientation.
  - Return (23:30-24:00): light to baseline, dissolve and cadence.
- Depth trajectory: Baseline → Light (induction) → Medium (deepening) → Deep (core work) → Brief lightening (fractionation) → Light-medium (integration) → Baseline (return). Achieved through harmonic density, spectral width, and carrier register.
- Pacing-and-leading plan: Carrier's breath-length phrases (6–14s) and downward settling pace attention inward; pauses allow absorption. Bed widens as attention narrows.
- Ratification plan: Subtle anchor cue (spectral bloom) returns at end of each stage's deepening phase to ratify state.
- Suggestion-compatible windows: Quiet moments between carrier phrases (1–3s) for spoken guidance without masking.
- Reset avoidance rule: No abrupt changes, hard transients, or sudden brightness spikes.

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Rising minor third (C to E) followed by falling second (E down to D), forming 3-note cell.
- First form: Single instance at 1.5s intervals in orientation (within carrier phrase).
- Later forms: Transposed down minor third during deepening; appears as fragment at end of carrier phrases in core work.

### Material Inventory - Harmonic Signature
- Home color: F2 (87.31 Hz) with interval stack [0, 3, 7] (F-A-C).
- Contrast color: A2 (110.00 Hz) with interval stack [0, 4, 7] (A-C#-E), used in induction for gentle shift.
- Return color: F2 with interval stack [0, 3] (F-A), used in integration to return without full closure.

### Material Inventory - Texture Family
- Continuous layer behavior: Low, warm drone (ground) with slow amplitude modulation (0.1 Hz), creating breathing field.
- Event layer behavior: Spectral blooms (0.5–1s) at 15–45 second intervals, never overlapping carrier phrases.
- Noise / air behavior: High-frequency air layer (10–20 kHz) that widens during deepening/fractionation, contracts in return.

### Material Inventory - Spatial Material
- Width behavior: Narrow (15°) → Medium (30°) → Wide (60°) → Medium-wide (45°) → Narrow (20°) → Wide (50°) → Contracting to narrow (15°).
- Motion behavior: Carrier near-center with minimal motion; air layer moves slowly left-to-right during deepening, contracts in return.
- Depth behavior: Carrier and harmonic bed close front (orientation) → recede to deeper field during deepening → return.

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.1–0.2 Hz) for ground drone; secondary shimmer rate 0.3–0.5 Hz in harmonic bed.
- Change rule: Shimmer rate increases during deepening (0.3→0.5 Hz), decreases in integration.

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: `reference_voice_cadence_profile_v1` (6–14s phrases, 84.5% falling-settling).
- Reference pitch-contour profile: `reference_voice_pitch_contours_v1` (84.5% falling-settling).
- Carrier instrument / layer: Sarangi-like bowed string (soft, low-mid resonance, no high-end brightness).
- Cadence behavior: Breath-length phrases (6–14s) with 1–3s pauses, downward settling at phrase ends.
- Contour behavior: Primarily falling-settling (84.5%), with rising-openings for fractionation lightening.
- Weighting behavior: Slight emphasis on second note (rising minor third), longer decay on falling note.
- Return behavior: Returns to orientation contour in integration with wider phrase.
- Salience limit: Carrier 6 dB above harmonic bed, 10 dB above substrate; never foreground melody.

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Soft spectral bloom (10–20 kHz, 0.5s) with slight stereo widening.
- First pairing: Orientation (1:45), carrier phrase settles downward, bloom appears at release.
- Withholding plan: Absent during induction (to avoid over-familiarity), returns only at deepening stability, core threshold, fractionation re-deepening.
- Return / trigger plan: 
  - Deepening stability (10:30): Bloom returns, confirming deep state.
  - Core work threshold (18:45): Bloom returns with brighter spectral edge, signaling core depth.
  - Fractionation re-deepening (21:30): Bloom returns with wider stereo spread, signaling return to deep.
- Salience policy: Audible enough to learn (10–20 kHz), not salient enough for monitoring.

## Development Grammar

- Establishment rule: Introduce carrier and home harmonic color in orientation.
- Variation rule: Motif transposes down minor third during deepening; harmonic bed shifts to contrast color in induction.
- Contrast rule: Home (F2) → contrast (A2) shift is gentle and harmonic, not abrupt.
- Recurrence rule: Motif returns transformed (transposed) in deepening/core work; anchor cue returns at key thresholds.
- Foreground rule: Carrier always present and audible (6 dB above bed), never foreground melody.
- Withholding rule: Anchor cue withheld during induction/fractionation lightening to preserve meaning.
- Fractionation rule: During fractionation, carrier contour becomes brighter (rising-openings) for 60s, then returns to falling-settling with wider stereo (re-deepening).
- Trigger conditioning rule: Anchor paired with downward settling carrier phrase in orientation, associating it with deepening.
- Dissolution / cadence rule: End by contracting stereo field, reducing harmonic density, fading carrier with final settling phrase.
- Anti-padding rule: No events added merely to fill time; every micro-event serves purpose.

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish world and first carrier pairing.
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: ground, harmonic body (home color), carrier seed (first motif form)
- Development action: establish
- Hypnotic function: settle and establish safety
- Energy / density posture: low, sparse (1 event in 3 minutes)
- Harmonic / spectral posture: home color [F2,0,3,7] at low density
- Spatial posture: narrow (15°), centered carrier, ground centered.
- Transition condition: carrier established and anchor first paired (1:45)
- Motion source commitment: `foreground: voice_adjacent` (180s > 120s)

### Formal Unit 2 - Induction / Narrowing
- Name / role: Pace and lead attention inward.
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: ground, harmonic body (contrast color), carrier variants (motif transposed up minor third)
- Development action: vary
- Hypnotic function: pace and lead attention
- Energy / density posture: medium (2 events in 4 minutes)
- Harmonic / spectral posture: contrast color [A2,0,4,7] at medium density
- Spatial posture: medium (30°), carrier near-center, harmonic body wide.
- Transition condition: attention narrowed and space prepared to widen (6:50)
- Motion source commitment: `foreground: voice_adjacent` (240s > 120s)

### Formal Unit 3 - Deepening
- Name / role: Widen space and stabilize root memory.
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: ground, harmonic body (home color), air layer, low carrier returns (motif transposed down minor third)
- Development action: thicken, widen
- Hypnotic function: reduce orienting load and widen space
- Energy / density posture: medium-high (3 events in 6 minutes)
- Harmonic / spectral posture: home color [F2,0,3,7] with air layer (10–20 kHz)
- Spatial posture: wide (60°), carrier near-center, air layer on edges.
- Transition condition: depth stable and core can withhold carrier (12:50)
- Motion source commitment: `foreground: voice_adjacent` (360s > 120s)

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain absorption with micro-developments.
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: ground, harmonic body (home color), rare carrier (motif fragment at phrase end), anchor reassurance
- Development action: thin, recur
- Hypnotic function: sustain absorption
- Energy / density posture: low-medium (1 event in 7 minutes)
- Harmonic / spectral posture: home color [F2,0,3] at low density
- Spatial posture: medium-wide (45°), carrier near-center, anchor softening.
- Transition condition: field thinned and fractionation pre-echo seeded (19:50)
- Motion source commitment: `foreground: voice_adjacent` (420s > 120s)

### Formal Unit 5 - Fractionation Pocket
- Name / role: Briefly lighten then return deeper.
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep to light-medium to deep
- Material state: ground, harmonic body (home color), brief brightness, clearer carrier, anchor redeepening
- Development action: brighten, then darken
- Hypnotic function: briefly lighten and return deeper
- Energy / density posture: medium (2 events in 2 minutes)
- Harmonic / spectral posture: home color [F2,0,3] with brief brightness (15–20 kHz)
- Spatial posture: narrow (20°) during lightening, wide (50°) for re-deepening.
- Transition condition: brightness closed and deeper return established (21:50)
- Motion source commitment: `foreground: voice_adjacent` (120s, but foreground mode requires it)

### Formal Unit 6 - Integration
- Name / role: Return familiar material in clearer calm form.
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: ground, harmonic body (return color), clearer carrier, anchor reassurance
- Development action: clarify, thin
- Hypnotic function: stabilize and prepare for return
- Energy / density posture: low (1 event in 1.5 minutes)
- Harmonic / spectral posture: return color [F2,0,3] at low density
- Spatial posture: medium (40°), carrier near-center, harmonic body wide.
- Transition condition: fractionation redeepened (22:50)
- Motion source commitment: `foreground: voice_adjacent` (90s, but foreground mode requires it)

### Formal Unit 7 - Return / Reorientation
- Name / role: Dissolve and cadence.
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: ground residue, air fade
- Development action: dissolve
- Hypnotic function: reorient without abrupt arousal
- Energy / density posture: very low (0 events)
- Harmonic / spectral posture: home color [F2,0] fading
- Spatial posture: contracting from medium to narrow (15°)
- Transition condition: integration cadence prepared (23:20)
- Motion source commitment: `foreground: voice_adjacent` (30s, but foreground mode requires it)

## Orchestration Map

- Root / ground function: Low drone (F2) grounds listener.
- Harmonic color function: Harmonic bed provides pitch identity and shifts (home → contrast → return).
- Motion function: Carrier's phrase arcs create sense of time.
- Foreground / event function: Spectral blooms (rare, subtle events).
- Voice-adjacent induction function: Sarangi-like carrier carries prosody.
- Anchor / trigger function: Spectral bloom is anchor cue.
- Air / bloom function: Air layer creates openness and widens during deepening.
- Spatial / depth function: Stereo width and carrier placement create spatial arc.
- Silence / restraint function: Anchor withheld during induction/fractionation lightening.

## Temporal Behavior

- Micro-event rate: One spectral bloom every 15–45 seconds (core work/deepening).
- Scene-change rate: Every 7–10 minutes (end of hypnotic stages).
- Long-wave change: Harmonic bed shifts from home to contrast and back; spatial arc widens then contracts.
- Maximum stasis tolerance: 45 seconds without micro-event or harmonic shift (ground drone modulation counts).
- Repetition policy: Motif returns transformed, not identically.
- Fractionation timing: Lightening 20:00–21:00 (60s), re-deepening 21:00–22:00 (60s).
- Anchor recurrence spacing: 1:45, 10:30, 18:45, 21:30.

## Spatial Composition

- Stereo requirement: stereo.
- Spatial arc: narrow (15°) → medium (30°) → wide (60°) → medium-wide (45°) → narrow (20°) → wide (50°) → contracting to narrow (15°).
- Layer placement principles: Ground centered; harmonic body wide stable; carrier near-center low motion; air layer on edges/rear; anchor local but soft.
- Motion limits: No fast autopan (>20–120s); spatial movement slow and natural.
- Verification obligation: Verify not mono, stereo width arc audible, motion not alerting.

## Revision Criteria

- Failure mode: too static
  - Evidence: no micro-events >45s, harmonic color unchanged.
  - Revision target: Add spectral bloom or slight harmonic shift.

- Failure mode: too generic
  - Evidence: motif not identifiable, anchor random.
  - Revision target: Define specific motif (rising minor third) and anchor identity.

- Failure mode: too busy
  - Evidence: >1 event/15s, carrier too loud.
  - Revision target: Reduce events to 1/30–45s, lower carrier by 2 dB.

- Failure mode: weak recurrence
  - Evidence: motif appears once, anchor returns identically.
  - Revision target: Transform motif (transpose down), vary anchor's spectral edge.

- Failure mode: weak ending
  - Evidence: fades out without cadence.
  - Revision target: Add final settling phrase, contract stereo field.

- Failure mode: weak spatial composition
  - Evidence: output mono or width doesn't change.
  - Revision target: Implement spatial arc in stereo.

- Failure mode: weak hypnotic architecture
  - Evidence: no clear stage progression, depth unchanged.
  - Revision target: Map harmonic bed/carrier register to each stage's depth.

- Failure mode: weak fractionation
  - Evidence: lightening feels like reset, re-deepening abrupt.
  - Revision target: Lighten via carrier contour (brighter, rising), re-deepen with wider stereo.

- Failure mode: weak anchors / triggers
  - Evidence: anchor too obvious (bell-like) or absent at thresholds.
  - Revision target: Soften cue to 10–20 kHz, place at key thresholds.

- Failure mode: weak voice-adjacent induction
  - Evidence: carrier too bright, phrases metrical.
  - Revision target: Use sarangi timbre, ensure 84.5% falling-settling.

## Sketch Handoff

- Required sketch style family: neuro-ambient with voice-adjacent carrier.
- Required formal units: Seven stages as defined in Formal Architecture.
- Required material to preserve: Primary motif (rising minor third), harmonic signature (home/contrast/return colors), texture behavior, spatial material.
- Required hypnotic architecture to preserve: Stage map and depth trajectory as defined in Hypnotic Function Architecture.
- Required voice-adjacent induction behavior to preserve: Carrier (sarangi-like), phrase duration 6–14s, falling-settling contour.
- Required development grammar to preserve: Rules for recurrence, contrast, fractionation as defined in Development Grammar.
- Required revision criteria to preserve: Failure modes and revisions listed above.
- Explicit exclusions: No percussion, no transient attacks, no bright bells.