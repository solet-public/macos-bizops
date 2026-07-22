# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-038-001
WORK_MANIFEST: wmf-neuro-ambient-composition-038-001
Status: ready

## Artistic Reading

- User image / premise: "a continuous, breath-led instrumental journey through hypnotic stages"
- Musical problem: Creating 24 minutes of continuous absorption without abrupt changes or attention-demanding elements
- Listener stance: "following the continuous lead" (absorptive, not analytical)
- What this is not: dramatic, sad narrative, triumphant, pop-hook-based, cinematic tension
- Style-family fit: Hypnotic Stage Architecture with Voice-Adjacent Induction Carrier

## Chosen Musical Premise

- Premise sentence: "A continuous breath-led journey through seven hypnotic stages, using a sarangi-like carrier to guide attention inward while harmonic and spatial shifts deepen absorption without alerting the listener."
- Primary dramatic motion: deepen
- Sound-world metaphor: "a slow, warm river flowing through a forest at twilight"
- Musical consequence: "The river's flow (substrate) becomes deeper and wider as the forest darkens (harmonic shift to lower roots, widening stereo), while occasional ripples (motif fragments) emerge from the current without breaking its continuity."
- Duration rationale: "24 minutes allows for seven distinct hypnotic stages with sufficient time to establish, deepen, and return from absorption without rushing the process. Each stage's duration is justified by depth of attention required: orientation (3 min) to settle, induction (4 min) to narrow focus, deepening (6 min) for stability, core (7 min) for sustained absorption, fractionation (2 min) as brief lightening, integration (1.5 min) to stabilize, and return (0.5 min) for reorientation."

## Hypnotic Function Architecture

- Target use: music-only self-hypnosis
- Stage map:
  - Orientation / Settling (0:00-3:00): baseline_to_light, establish_world_and_first_carrier_pairing
  - Induction / Narrowing (3:00-7:00): light_to_medium, pace_and_lead_attention
  - Deepening (7:00-13:00): medium_to_deep, widen_space_lower_salience_and_stabilize_root_memory
  - Core Absorptive Work (13:00-20:00): deep, sustain_absorption_with_non_inert_microdevelopment
  - Fractionation Pocket (20:00-22:00): deep_to_light_medium_to_deep, briefly_lighten_then_return_deeper
  - Integration / Stabilization (22:00-23:30): medium_to_light, stabilize
  - Return / Reorientation (23:30-24:00): light_to_baseline, reorient
- Depth trajectory: "Baseline (orientation) → Light (induction) → Medium (deepening start) → Deep (core) → Brief lightening (fractionation) → Return to Medium (integration) → Baseline (return). Depth measured by harmonic density, spatial width, and carrier register: low for deep, wider for lightening."
- Pacing-and-leading plan: "Establish slow ground (F2 drone) and sparse carrier phrases. Lengthen rests, lower carrier register, darken brightness (lowpass filter), widen space to narrow attention inward."
- Ratification plan: "Carrier's downward settling contour and anchor bloom at depth thresholds confirm acceptance without words."
- Suggestion-compatible windows: "Quiet moments after carrier phrases (10-20s into each stage) for spoken guidance without masking."
- Reset avoidance rule: "No abrupt cuts, hard transients, alerting cues, or sudden brightness spikes. All transitions crossfaded over 12s with harmonic/spectral shifts."

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: Three-note falling-settling interval cell (C3-Ab2-G2) functioning as carrier memory
- First form: Soft breath-length phrase arc (1.2-2.5s) in carrier layer after ground established, slight downward contour
- Later forms: Lowered register (B2-G2-Eb2) in deepening, thinned to two notes (Ab2-G2) in core, clearer return (C3-Ab2) in integration

### Material Inventory - Harmonic Signature
- Home color: F root (87.31 Hz) with open C, Eb warmth (interval stack [0, 3, 10] in low register), G add9 for gentle lift
- Contrast color: Db root (138.59 Hz) with common-tone F retained, interval stack [0, 4, 12] for luminous shadow
- Return color: F root with fewer color tones (only C and Eb), softened upper partials, narrower spatial field

### Material Inventory - Texture Family
- Continuous layer behavior: Warm F2 drone (87.31 Hz) with slow beating (0.5-1.5 Hz), long attack (8s), release (20s)
- Event layer behavior: Rare submerged motif fragments (every 15-45s) and soft partial blooms, always below carrier salience
- Noise / air behavior: Filtered high air (10-20 kHz) at edges with slow opening/closing (attack 30s, release 60s), receding during deepening

### Material Inventory - Spatial Material
- Width behavior: Narrow (120°) in orientation, widening to medium (240°) in deepening, contracting to narrow (90°) in return
- Motion behavior: Carrier remains near-center with minimal motion; air layer drifts slowly from rear to edges
- Depth behavior: Deepening uses longer reverb tails (2.5s) and wider stereo; return contracts to intimate depth

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.07-0.13 Hz) for ground/harmonic bed, asymmetric shimmer in air layer
- Change rule: Modulation rates slow as depth increases; brightness decreases (lowpass cutoff 4700Hz→2600Hz) across stages

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1
- Reference pitch-contour profile: reference_voice_pitch_contours_v1 (conversational_induction for orientation/induction, relaxation_return for deepening/core/integration/return)
- Carrier instrument / layer: sarangi-like bowed string (warm low-mid reed patch)
- Cadence behavior: Breath-length phrase arcs (0.9-3.3s) with rise-and-settle contours, permissive pauses
- Contour behavior: Falling_settling (84.5% of phrases) with occasional rising_opening for fractionation lightening
- Weighting behavior: Slight emphasis on second note of phrase (e.g., Ab2 in C3-Ab2-G2) via velocity/filter
- Return behavior: Carrier returns transformed (register, density) at depth thresholds to ratify state
- Salience limit: foreground_lead_at_least_6dB_above_harmonic_bed_and_10dB_above_substrate

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: Tiny low-mid warmth bloom (0.1s, 87-95 Hz) paired with carrier release
- First pairing: Orientation after first carrier phrase resolves (at 1:45)
- Withholding plan: Absent for at least 90 seconds after first pairing (until induction threshold)
- Return / trigger plan: Returns at induction threshold (3:00), deepening stability point (9:00), core reassurance (16:30), fractionation re-deepening (21:00), integration (22:45)
- Salience policy: Audible enough to learn (0.5 dB above bed) but not salient enough to monitor

## Development Grammar

- Establishment rule: Introduce ground, harmonic bed, and carrier in orientation with sparse events
- Variation rule: Transform motif by register (lower) and density (thinner) in deepening; add air layer for width
- Contrast rule: Shift harmonic root (F→Db) in fractionation, then return to F for core
- Recurrence rule: Motif returns transformed (e.g., thinned in core, clearer in integration) every 15-45s
- Foreground rule: Carrier is foreground (≥6dB above bed) but never becomes lead melody
- Withholding rule: Withhold event layer and fractionation brightness until core stable
- Fractionation rule: Brief lightening (20:00-21:00) via carrier pitch rise and brightness increase, then return deeper (21:00-22:00) with darker harmonic color and wider field
- Trigger conditioning rule: Anchor bloom pairs with carrier release to create conditioned comfort cue at depth thresholds
- Dissolution / cadence rule: End with carrier returning to orientation contour, harmonic bed simplifying, spatial contraction
- Anti-padding rule: No loops or stretches; every event serves development (e.g., motif fragment at 15s intervals)

## Formal Architecture

### Formal Unit 1 - Orientation / Settling
- Name / role: Establish the river and first carrier phrase
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline_to_light
- Material state: ground, harmonic_body, carrier_seed, first_anchor_pairing
- Development action: establish
- Hypnotic function: settle_and_establish_safety
- Energy / density posture: low, sparse
- Harmonic / spectral posture: F home color (open C, Eb warmth)
- Spatial posture: narrow (120°), centered
- Transition condition: carrier_established_and_anchor_first_paired (at 1:45)
- Motion source commitment: foreground: voice_adjacent, parameter_group_sequence (F home color)

### Formal Unit 2 - Induction / Narrowing
- Name / role: Narrow attention through pacing and carrier evolution
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light_to_medium
- Material state: ground, harmonic_body, carrier_variants, anchor_return (first return at 3:00)
- Development action: vary
- Hypnotic function: pace_and_lead_attention
- Energy / density posture: medium, sparse but more active
- Harmonic / spectral posture: F home color with slight register lowering (C3→B2)
- Spatial posture: medium (180°), widening
- Transition condition: attention_narrowed_and_space_prepared_to_widen (at 7:00)
- Motion source commitment: foreground: voice_adjacent, parameter_group_sequence (F home color → F lower register)

### Formal Unit 3 - Deepening
- Name / role: Deepen absorption through widening and harmonic shift
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium_to_deep
- Material state: ground, harmonic_body, air, low_carrier_returns, anchor_softening
- Development action: thicken and deepen
- Hypnotic function: reduce_orienting_load
- Energy / density posture: medium, with air layer added
- Harmonic / spectral posture: F→G (98Hz) harmonic shift, darker color
- Spatial posture: wide (240°), carrier near-center
- Transition condition: depth_stable_and_core_can_withhold_carrier (at 13:00)
- Motion source commitment: foreground: voice_adjacent, parameter_group_sequence (F→G harmonic region)

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustain deep absorption with micro-developments
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core_absorptive_work
- Target depth: deep
- Material state: ground, harmonic_body, rare_carrier, rare_motif_fragment, anchor_reassurance
- Development action: withhold and recur
- Hypnotic function: sustain_absorption
- Energy / density posture: low, sparse events (motif fragments every 15-45s)
- Harmonic / spectral posture: F home color with fewer tones (C, Eb), softened upper partials
- Spatial posture: wide (240°), air at edges
- Transition condition: field_thinned_and_fractionation_pre_echo_seeded (at 20:00)
- Motion source commitment: foreground: voice_adjacent, event_schedule (motif fragments at 15-45s intervals)

### Formal Unit 5 - Fractionation Pocket
- Name / role: Brief lightening and deeper return
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep_to_light_medium_to_deep
- Material state: ground, harmonic_body, brief_brightness, clearer_carrier, anchor_redeepening
- Development action: lighten and return deeper
- Hypnotic function: briefly_lighten_then_return_deeper
- Energy / density posture: medium, with brief brightness increase
- Harmonic / spectral posture: Db contrast color (briefly), then F return
- Spatial posture: medium-wide (210°), carrier clearer
- Transition condition: brightness_closed_and_deeper_return_established (at 22:00)
- Motion source commitment: none — fractionation pocket is a single event window (lightening and re-deepening) within the unit, no continuous motion source required

### Formal Unit 6 - Integration / Stabilization
- Name / role: Stabilize the absorption state
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium_to_light
- Material state: ground, harmonic_body, carrier_return (clearer), anchor_stabilization
- Development action: dissolve and stabilize
- Hypnotic function: stabilize
- Energy / density posture: low, with carrier returning to orientation contour
- Harmonic / spectral posture: F home color (simplified, fewer tones)
- Spatial posture: medium (180°), contracting
- Transition condition: stability confirmed for return (at 23:30)
- Motion source commitment: none — short unit, no internal motion source required

### Formal Unit 7 - Return / Reorientation
- Name / role: Gentle reorientation to baseline
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light_to_baseline
- Material state: ground, harmonic_body (simplified), carrier_final_return
- Development action: dissolve and fade
- Hypnotic function: reorient
- Energy / density posture: very low, sparse
- Harmonic / spectral posture: F home color (minimal)
- Spatial posture: narrow (90°), carrier near-center
- Transition condition: reorientation complete (at 24:00)
- Motion source commitment: none — short unit, no internal motion source required

## Orchestration Map

- Root / ground function: F2 drone (87.31 Hz) with slow beating
- Harmonic color function: F-centered open voicings (harmonic_body layer)
- Motion function: Carrier's breath-length phrases and motif fragments
- Foreground / event function: Rare motif fragments (event_layer) at 15-45s intervals
- Voice-adjacent induction function: Sarangi-like carrier with prosodic phrasing
- Anchor / trigger function: Low-mid warmth bloom (anchor_layer) at depth thresholds
- Air / bloom function: Filtered high air layer at edges with slow opening/closing
- Spatial / depth function: Stereo width and reverb tail control spatial arc
- Silence / restraint function: Withholding event layer until core stable

## Temporal Behavior

- Micro-event rate: Every 15-45 seconds (motif fragments, anchor returns)
- Scene-change rate: Every 7-10 minutes (harmonic region shifts, spatial changes)
- Long-wave change: Harmonic root shift from F to Db and back, spectral arc (4700→2600Hz)
- Maximum stasis tolerance: 45 seconds without meaningful change
- Repetition policy: Motif fragments recur transformed; harmonic regions shift without repetition
- Fractionation timing: Lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: First pairing at 1:45, returns at 3:00 (induction), 9:00 (deepening stability), 16:30 (core reassurance), 21:00 (fractionation re-deepening), 22:45 (integration)

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: narrow (120°) → medium-wide (240°) → narrow (90°)
- Layer placement principles: ground centered, harmonic_body wide_stable, carrier near_center_low_motion, air edges_and_rear, anchor local_but_soft
- Motion limits: No fast autopan; spatial motion over 20-120 seconds only for tiny anchor gestures
- Verification obligation: Verify not mono, not dual-mono; width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No micro-events for >45 seconds, harmonic color unchanged
  - Revision target: Add motif fragments at 15-45s intervals; introduce slow harmonic shift
- Failure mode: too generic
  - Evidence: Same layer recipe for every stage; no distinct harmonic signature
  - Revision target: Define home/contrast/return colors per stage; transform motif by register and density
- Failure mode: too busy
  - Evidence: Too many foreground events; carrier becomes lead melody
  - Revision target: Reduce event density to 1 per 45s; lower carrier salience by 2dB
- Failure mode: weak recurrence
  - Evidence: Motif appears once and vanishes; no transformed return
  - Revision target: Transform motif in deepening (lower register) and integration (clearer)
- Failure mode: weak ending
  - Evidence: Simple fade-out without harmonic simplification or spatial contraction
  - Revision target: Compose dissolution with carrier returning to orientation contour, harmonic bed simplifying, spatial contraction
- Failure mode: weak spatial composition
  - Evidence: Output is mono or width doesn't change
  - Revision target: Add stereo construction for air layer and anchor; verify width arc in final master
- Failure mode: weak hypnotic architecture
  - Evidence: No clear stage progression; depth changes not tied to concrete parameters
  - Revision target: Map each stage's duration and material state to depth trajectory (baseline→light→medium→deep→lightening→deeper→baseline)
- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset; return not deeper
  - Revision target: Bind fractionation lightening to carrier pitch rise and brightness increase, then return with darker harmonic color
- Failure mode: weak anchors / triggers
  - Evidence: Cues random, too obvious (bright bloom), or absent
  - Revision target: Pair anchor with carrier release; withhold for 90s after first pairing
- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier mechanical, not breath-length; no downward settling
  - Revision target: Use empirical cadence profile (v1) for phrase timing and contour; ensure 84.5% falling_settling

## Sketch Handoff

- Required sketch style family: Hypnotic Stage Architecture (7-stage)
- Required formal units: Orientation, Induction, Deepening, Core Absorptive Work, Fractionation Pocket, Integration, Return
- Required material to preserve: Primary motif (C3-Ab2-G2), harmonic signature (home F, contrast Db, return F), texture behavior (continuous substrate with eddies), spatial material (width arc 120°→240°→90°)
- Required hypnotic architecture to preserve: Stage map, depth trajectory (baseline→light→medium→deep→lightening→deeper→baseline), fractionation timing, anchor recurrence
- Required voice-adjacent induction behavior to preserve: Sarangi-like carrier with breath-length phrases (0.9-3.3s), falling_settling contour, salience ≥6dB above bed
- Required development grammar to preserve: Establishment rule, variation rule (register/density), contrast rule (F→Db), recurrence rule (transformed motif), withholding rule
- Required revision criteria to preserve: All 10 failure modes and their targets as listed above
- Explicit exclusions: No percussion, no transient rhythmic material, no pop hooks, no abrupt changes