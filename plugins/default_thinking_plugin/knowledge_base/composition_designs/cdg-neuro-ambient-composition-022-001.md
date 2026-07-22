# Composition Design Document
Article Layer: 3

DESIGN ID: cdg-neuro-ambient-composition-022-001
WORK_MANIFEST: wmf-neuro-ambient-composition-022-001
Status: ready

## Artistic Reading

- User image / premise: A deep, evolving journey through layered resonance and breath-like motion
- Musical problem: Creating non-verbal guidance for self-hypnosis without leading or demanding attention
- Listener stance: Inward_deep_listening with focus on internal absorption
- What this is not: Dramatic, pop-hook-based, or cinematic tension; no foreground melody
- Style-family fit: Sequenced_ambient in Carbon Based Lifeforms/Solar Fields tradition with hypnotic architecture

## Chosen Musical Premise

- Premise sentence: A 24-minute harmonic journey guided by breath-length carrier phrases and spectral shifts, with anchor cues that become conditionally meaningful through withholding
- Primary dramatic motion: Deepen (through harmonic progression, spatial expansion, and carrier contour)
- Sound-world metaphor: "Resonant cave breath" – slow harmonic expansion with spectral settling
- Musical consequence: 
  * Harmonic: F2 → A2 progression over 4 minutes with arpeggio variations
  * Rhythm: Non-metric carrier phrases (12-18s) with 3-6s rests
  * Texture: Continuous harmonic bed that thickens during deepening, thins during fractionation
  * Register: Carrier descends from C3 to A2 during deepening, returns to C3 for integration
  * Space: Narrow → medium → wide → medium-wide → narrow width arc
- Duration rationale: 24 minutes allows for slow harmonic progression (F2→A2), multiple anchor returns, and fractionation without rushing

## Hypnotic Function Architecture

- Target use: Music-only self-hypnosis
- Stage map:
  * Orientation (0:00-3:00): Baseline to light – sparse carrier, narrow width
  * Induction (3:00-7:00): Light to medium – carrier phrases lengthen, bass root moves
  * Deepening (7:00-13:00): Medium to deep – carrier descends, width widens
  * Core absorptive work (13:00-20:00): Deep – 4-note arpeggio, maximum width
  * Fractionation (20:00-22:00): Deep to light-medium to deep – brief brightness increase then re-deepening
  * Integration (22:00-23:30): Medium to light – carrier returns to medium register
  * Return (23:30-24:00): Light to baseline – carrier fades with settling phrase
- Depth trajectory: Baseline (sparse) → light (carrier present) → medium (bass root F#2, width 30%) → deep (A2, width 70%, carrier descent) → light-medium (width 90%) → deep (width 70% re-deepening) → light (width 40%) → baseline
- Pacing-and-leading plan: Start with sparse carrier phrases (12s), lengthen to 18s during induction, add bass root movement
- Ratification plan: Resonant_Whisper Anchor returns at 06:00,12:00,18:00,23:00 to ratify depth states
- Suggestion-compatible windows: Carrier's 3-6s rests and anchor returns (non-masked by events)
- Reset avoidance rule: No abrupt cuts, hard transients, or brightness spikes; all transitions 10-25s

## Material Inventory

### Material Inventory - Primary Motif / Gesture
- Identity: 3-note interval cell (root, minor third, fifth) in slow arpeggio
- First form: F2-A2-C3 pattern (12s phrase, 3s rest)
- Later forms: Shift to F#2-A#2-C#3 during induction; evolve to 4-note pattern (F2-A2-C3-E3) in core

### Material Inventory - Harmonic Signature
- Home color: F2 root with minor third and fifth (F,A,C)
- Contrast color: F#2 root with minor third and fifth (F#,A#,C#)
- Return color: A2 root with added seventh (A,C#,E,G)

### Material Inventory - Texture Family
- Continuous layer behavior: Low-mid harmonic pad with 0.1Hz amplitude breath and irregular spectral eddies
- Event layer behavior: Soft glints (0.5s, 1dB above noise) at 1-2 per minute
- Noise / air behavior: High-frequency layer that widens with stereo arc, volume -25dB

### Material Inventory - Spatial Material
- Width behavior: Narrow (0-3:00) → medium (3-7:00) → wide (7-20:00) → medium-wide (20-21:00) → narrow (21-24:00)
- Motion behavior: Air layer drifts left→right during core, returns to center
- Depth behavior: Reverb tail lengthens during deepening (7-13:00), shortens during integration

### Material Inventory - Modulation Identity
- Rate family: Slow breath (0.1Hz) with secondary shimmer (0.35Hz)
- Change rule: Breath rate slows from 0.15Hz to 0.07Hz during deepening

### Material Inventory - Hypnotist-Voice Analog
- Reference cadence profile: reference_voice_cadence_profile_v1
- Reference pitch-contour profile: falling_settling
- Carrier instrument / layer: Filtered sine + noise blend (200-400Hz resonance)
- Cadence behavior: 12-18s phrases with 3-6s rests, downward pitch glide (50-100 cents)
- Contour behavior: Each phrase ends with settling descent
- Weighting behavior: Soft attack (0.5s), long release (4-6s), volume -12dB
- Return behavior: Transformed returns at anchor points (lower register, wider stereo)
- Salience limit: Always below lead melody attention (-15dB)

### Material Inventory - Nonverbal Anchors And Subtle Triggers
- Anchor identity: 0.5s spectral unsealing (8kHz boost, -20dB)
- First pairing: At 00:00 (end of first carrier phrase)
- Withholding plan: Absent for 90s after first pairing
- Return / trigger plan: Returns at 06:00,12:00,18:00,23:00 (end of carrier phrase)
- Salience policy: Audible enough to learn, not salient enough to monitor

## Development Grammar

- Establishment rule: Introduce harmonic signature (F2,A,C) and carrier with 12s phrases
- Variation rule: Arpeggio pattern shifts register (F→F#→A) without becoming unrelated
- Contrast rule: Introduce contrast color (F#,A#,C#) during induction, return to home
- Recurrence rule: Carrier returns transformed at anchor points (lower register)
- Foreground rule: Carrier remains below lead melody attention
- Withholding rule: Anchor withheld for 90s to preserve meaning in return
- Fractionation rule: Lightening (width 90%, carrier brighter) → re-deepening (width 70%)
- Trigger conditioning rule: Anchor paired with carrier's downward settling phrase
- Dissolution / cadence rule: Final carrier phrase (18s) followed by 30s fade
- Anti-padding rule: No material looped to fill time; all changes developmentally motivated

## Formal Architecture

### Formal Unit 1 - Orientation
- Name / role: Settling the field and establishing safety
- Approximate duration range: 0:00-3:00
- Hypnotic stage role: orientation
- Target depth: baseline to light
- Material state: Home harmonic color (F2,A,C), sparse carrier
- Development action: Establish
- Hypnotic function: settle and establish safety
- Energy / density posture: Low (sparse carrier, minimal events)
- Harmonic / spectral posture: F2 root
- Spatial posture: Narrow (10% width)
- Transition condition: Carrier phrase ends with downward settling
- Motion source commitment: parameter_group_sequence

### Formal Unit 2 - Induction
- Name / role: Pacing and leading deeper
- Approximate duration range: 3:00-7:00
- Hypnotic stage role: induction
- Target depth: light to medium
- Material state: Home → contrast color (F2→F#2), carrier phrases lengthen
- Development action: Vary
- Hypnotic function: pace and lead
- Energy / density posture: Medium (carrier phrases 15s, width 30%)
- Harmonic / spectral posture: F2 → F#2
- Spatial posture: Medium (30% width)
- Transition condition: Bass root reaches F#2
- Motion source commitment: parameter_group_sequence

### Formal Unit 3 - Deepening
- Name / role: Reducing orienting load
- Approximate duration range: 7:00-13:00
- Hypnotic stage role: deepening
- Target depth: medium to deep
- Material state: Contrast → return color (F#2→A2), carrier descends
- Development action: Vary
- Hypnotic function: reduce orienting load
- Energy / density posture: Deep (carrier phrases 16s, width 70%)
- Harmonic / spectral posture: F#2 → A2
- Spatial posture: Wide (70% width)
- Transition condition: Bass root reaches A2
- Motion source commitment: parameter_group_sequence

### Formal Unit 4 - Core Absorptive Work
- Name / role: Sustaining deep absorption
- Approximate duration range: 13:00-20:00
- Hypnotic stage role: core absorptive work
- Target depth: deep
- Material state: Return color (A2,C#,E,G), 4-note arpeggio
- Development action: Thicken
- Hypnotic function: sustain absorption
- Energy / density posture: Deep (dense harmonic bed)
- Harmonic / spectral posture: A2 root with added seventh
- Spatial posture: Wide to medium-wide (60-80% width)
- Transition condition: Carrier phrase ends with settling
- Motion source commitment: parameter_group_sequence

### Formal Unit 5 - Fractionation
- Name / role: Briefly lightening then returning deeper
- Approximate duration range: 20:00-22:00
- Hypnotic stage role: fractionation
- Target depth: deep to light-medium to deep
- Material state: Lightening (width 90%, carrier brighter) → re-deepening
- Development action: Fractionate
- Hypnotic function: briefly lighten then return deeper
- Energy / density posture: Light-medium → deep
- Harmonic / spectral posture: A2 root, brighter high frequencies
- Spatial posture: Medium-wide (90% width) → medium-wide (70%)
- Transition condition: End of lightening period
- Motion source commitment: none — short unit, no internal motion source required

### Formal Unit 6 - Integration
- Name / role: Stabilizing calm after deep absorption
- Approximate duration range: 22:00-23:30
- Hypnotic stage role: integration
- Target depth: medium to light
- Material state: Return color (A2,C#,E,G), carrier returns to medium register
- Development action: Dissolve
- Hypnotic function: stabilize
- Energy / density posture: Light (carrier phrases 12s, width 40%)
- Harmonic / spectral posture: A2 root
- Spatial posture: Medium (40% width)
- Transition condition: Carrier phrase ends with settling
- Motion source commitment: none — short unit, no internal motion source required

### Formal Unit 7 - Return
- Name / role: Reorienting attention gently
- Approximate duration range: 23:30-24:00
- Hypnotic stage role: return
- Target depth: light to baseline
- Material state: Home harmonic color (F2,A,C), carrier fades out
- Development action: Dissolve
- Hypnotic function: reorient
- Energy / density posture: Baseline (sparse, quiet)
- Harmonic / spectral posture: F2 root
- Spatial posture: Narrow (10% width)
- Transition condition: Final carrier phrase ends
- Motion source commitment: none

## Orchestration Map

- Root / ground function: Moving bass root (F2→A2) provides harmonic foundation
- Harmonic color function: Arpeggio bed supplies pitch identity (3-note → 4-note)
- Motion function: Filter automation (2800→1900Hz) creates spectral motion
- Foreground / event function: Voice-adjacent carrier (only foreground element)
- Voice-adjacent induction function: Carrier layer with breath-length phrases
- Anchor / trigger function: Resonant_Whisper Anchor (spectral unsealing)
- Air / bloom function: High-frequency noise layer creates openness
- Spatial / depth function: Stereo width arc and reverb tail control space
- Silence / restraint function: 3-6s rests in carrier and anchor withholding

## Temporal Behavior

- Micro-event rate: 1-2 events per minute (soft glints)
- Scene-change rate: Every 3-7 minutes at stage boundaries
- Long-wave change: Harmonic progression (F2→A2) and stereo width arc
- Maximum stasis tolerance: 45 seconds without deliberate change
- Repetition policy: Arpeggio pattern varies (register, note count) to avoid wallpaper
- Fractionation timing: Lightening 20:00-21:00, re-deepening 21:00-22:00
- Anchor recurrence spacing: Returns at 06:00,12:00,18:00,23:00 (every 6 minutes)

## Spatial Composition

- Stereo requirement: stereo
- Spatial arc: Narrow → medium → wide → medium-wide → narrow
- Layer placement principles: Ground (centered), harmonic bed (wide stable), carrier (near center low motion)
- Motion limits: No fast autopan; spatial movement over 20-120 seconds
- Verification obligation: Verify not mono, width arc audible and motion not alerting

## Revision Criteria

- Failure mode: too static
  - Evidence: No perceptible change for >45 seconds
  - Revision target: Add micro-event (soft glint) or adjust harmonic bed modulation

- Failure mode: weak fractionation
  - Evidence: Lightening feels like reset (harmonic bed breaks)
  - Revision target: Keep harmonic bed continuous under fractionation, use only brightness change

- Failure mode: weak voice-adjacent induction
  - Evidence: Carrier has mechanical rhythm or bright attack
  - Revision target: Apply falling_settling contour and keep volume at -12dB

- Failure mode: weak hypnotic architecture
  - Evidence: Stages not distinct (no harmonic/spatial progression)
  - Revision target: Ensure each stage has clear depth trajectory change

## Sketch Handoff

- Required sketch style family: sequenced_ambient
- Required formal units: 7 (as defined in Formal Architecture)
- Required material to preserve: Primary motif, harmonic signature, texture behavior
- Required hypnotic architecture to preserve: Stage map and depth trajectory
- Required voice-adjacent induction behavior to preserve: Carrier layer, phrase timing, contour
- Required development grammar to preserve: Establishment, Variation, Withholding rules
- Required revision criteria to preserve: All listed above
- Explicit exclusions: No percussion, no hard transients, no static pedal tone >120s