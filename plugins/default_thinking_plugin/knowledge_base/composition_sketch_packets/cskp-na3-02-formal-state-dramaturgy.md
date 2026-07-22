# Composition Sketch Packet 02: Formal State Dramaturgy
PACKET ID: cskp-na3-02-formal-state-dramaturgy
SKETCH ID: na3
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 02

## Stage Sequence
| Index | Stage name (snake_case) | Hypnotic function | Duration in seconds |
| --- | --- | --- | --- |
| 1 | orientation | arrival | 180 |
| 2 | induction | induction | 240 |
| 3 | deepening | deepening | 360 |
| 4 | core_absorptive_work | core_work | 420 |
| 5 | fractionation_pocket | fractionation | 120 |
| 6 | integration | integration | 90 |
| 7 | return | return | 30 |
Stage count: 7 (matches Pass 01's stage_count)
Sum of durations: 1440 seconds (exactly matches Pass 01's target_duration_seconds)

## Felt-State Arc
orientation:
  target_depth: Level 1
  arousal: settled
  affective_color: calm_trust
  transition_in: cold-start
  transition_out: soft-fade
induction:
  target_depth: Level 2
  arousal: low
  affective_color: inward_curiosity
  transition_in: soft-fade
  transition_out: soft-fade
deepening:
  target_depth: Level 3
  arousal: very-low
  affective_color: protected_absorption
  transition_in: soft-fade
  transition_out: soft-fade
core_absorptive_work:
  target_depth: Level 3
  arousal: very-low
  affective_color: protected_absorption
  transition_in: soft-fade
  transition_out: soft-fade
fractionation_pocket:
  target_depth: Level 3
  arousal: low
  affective_color: gentle_release
  transition_in: soft-fade
  transition_out: soft-fade
integration:
  target_depth: Level 1
  arousal: low
  affective_color: gentle_release
  transition_in: soft-fade
  transition_out: soft-fade
return:
  target_depth: Level 0
  arousal: alert
  affective_color: calm
  transition_in: soft-fade
  transition_out: none

## Anchor Beats
orientation_carrier_pairing:
  occurs_within_stage: orientation
  approximate_time_seconds: 180
  evidence_modality: harmonic-shift
  function: marks end of settling and start of pacing
induction_attention_narrowed:
  occurs_within_stage: induction
  approximate_time_seconds: 240
  evidence_modality: spatial-shift
  function: marks attention narrowing complete and deepening begins
deepening_depth_stable:
  occurs_within_stage: deepening
  approximate_time_seconds: 360
  evidence_modality: density-shift
  function: marks deepening complete and core work begins
core_field_thinned:
  occurs_within_stage: core_absorptive_work
  approximate_time_seconds: 420
  evidence_modality: harmonic-shift
  function: marks core field thinned for fractionation
fractionation_brightness_closed:
  occurs_within_stage: fractionation_pocket
  approximate_time_seconds: 120
  evidence_modality: brightness-shift
  function: marks fractionation re-deepening complete and integration begins
integration_final_carrier_return:
  occurs_within_stage: integration
  approximate_time_seconds: 90
  evidence_modality: spatial-shift
  function: marks integration complete and return begins

## Stage Boundary Contracts
orientation -> induction:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0
induction -> deepening:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0
deepening -> core_absorptive_work:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0
core_absorptive_work -> fractionation_pocket:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0
fractionation_pocket -> integration:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0
integration -> return:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0