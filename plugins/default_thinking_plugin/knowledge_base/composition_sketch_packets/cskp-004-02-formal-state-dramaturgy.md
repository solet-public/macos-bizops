# Composition Sketch Packet 02: Formal State Dramaturgy

PACKET ID: cskp-004-02-formal-state-dramaturgy
SKETCH ID: 004
MANIFEST ID: wmf-neuro-ambient-composition-004
PASS NUMBER: 02

## Stage Sequence
| Index | Stage name (snake_case)        | Hypnotic function | Duration in seconds |
|-------|--------------------------------|-------------------|---------------------|
| 1     | orientation_settling           | arrival           | 180                 |
| 2     | induction_narrowing            | induction         | 240                 |
| 3     | deepening                      | deepening         | 360                 |
| 4     | core_absorptive_work           | core_work         | 420                 |
| 5     | fractionation_pocket           | fractionation     | 120                 |
| 6     | integration                    | integration       | 90                  |
| 7     | return_reorientation           | return            | 30                  |

Stage count equals invariant gate's stage_count (7). Stage duration sum: 1440 seconds (exactly matches target_duration_seconds within tolerance).

## Felt-State Arc
orientation_settling:
  target_depth: Level 1
  arousal: settled
  affective_color: warm, settling
  transition_in: cold-start
  transition_out: soft-fade

induction_narrowing:
  target_depth: Level 2
  arousal: low
  affective_color: focused, deepening
  transition_in: soft-fade
  transition_out: soft-fade

deepening:
  target_depth: Level 2
  arousal: low
  affective_color: immersive, slow
  transition_in: soft-fade
  transition_out: soft-fade

core_absorptive_work:
  target_depth: Level 2
  arousal: low
  affective_color: stable, absorbing
  transition_in: soft-fade
  transition_out: soft-fade

fractionation_pocket:
  target_depth: Level 2 (with temporary Level 1 lightening)
  arousal: settled
  affective_color: gentle, lightening
  transition_in: soft-fade
  transition_out: soft-fade

integration:
  target_depth: Level 1
  arousal: settled
  affective_color: calm, stabilizing
  transition_in: soft-fade
  transition_out: soft-fade

return_reorientation:
  target_depth: Level 0
  arousal: alert
  affective_color: gentle, returning
  transition_in: soft-fade
  transition_out: soft-fade

## Anchor Beats
first_induction_anchor:
  occurs_within_stage: induction_narrowing
  approximate_time_seconds: 120
  evidence_modality: harmonic-shift
  function: marks transition from settling to induction phase

deepening_anchor:
  occurs_within_stage: deepening
  approximate_time_seconds: 180
  evidence_modality: spatial-shift
  function: signals deepening phase arrival

core_absorption_anchor:
  occurs_within_stage: core_absorptive_work
  approximate_time_seconds: 210
  evidence_modality: density-shift
  function: establishes core absorptive work plateau

fractionation_lightening_anchor:
  occurs_within_stage: fractionation_pocket
  approximate_time_seconds: 60
  evidence_modality: spectral-shift
  function: initiates gentle lightening for fractionation

fractionation_recovery_anchor:
  occurs_within_stage: fractionation_pocket
  approximate_time_seconds: 90
  evidence_modality: density-shift
  function: signals return to core depth

integration_return_anchor:
  occurs_within_stage: integration
  approximate_time_seconds: 45
  evidence_modality: spatial-shift
  function: marks beginning of integration phase

## Stage Boundary Contracts
orientation_settling → induction_narrowing:
  crossover_seconds: 30
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0

induction_narrowing → deepening:
  crossover_seconds: 30
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0

deepening → core_absorptive_work:
  crossover_seconds: 30
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0

core_absorptive_work → fractionation_pocket:
  crossover_seconds: 30
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0

fractionation_pocket → integration:
  crossover_seconds: 30
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0

integration → return_reorientation:
  crossover_seconds: 30
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0