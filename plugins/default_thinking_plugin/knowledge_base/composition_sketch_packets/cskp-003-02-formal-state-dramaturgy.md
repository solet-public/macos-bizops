# Composition Sketch Packet 02: Formal State Dramaturgy
PACKET ID: cskp-003-02-formal-state-dramaturgy
SKETCH ID: 003
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 02

## Stage Sequence
| Index | Stage name (snake_case)         | Hypnotic function | Duration in seconds |
|-------|---------------------------------|-------------------|---------------------|
| 1     | orientation                     | arrival           | 180                 |
| 2     | induction                       | induction         | 240                 |
| 3     | deepening                       | deepening         | 360                 |
| 4     | core_absorptive_work            | core_work         | 420                 |
| 5     | fractionation                   | fractionation     | 120                 |
| 6     | integration                     | integration       | 90                  |
| 7     | return                          | return            | 30                  |

Stage count equals Pass 01's stage_count (7). Stage duration sum: 1440 seconds (exactly matches target_duration_seconds).

## Felt-State Arc
orientation:
  target_depth: Level 1
  arousal: settled
  affective_color: calm_trust
  transition_in: cold-start
  transition_out: anchor-trigger
induction:
  target_depth: Level 2
  arousal: low
  affective_color: inward_curiosity
  transition_in: anchor-trigger
  transition_out: anchor-trigger
deepening:
  target_depth: Level 3
  arousal: very-low
  affective_color: protected_absorption
  transition_in: anchor-trigger
  transition_out: anchor-trigger
core_absorptive_work:
  target_depth: Level 3
  arousal: very-low
  affective_color: protected_absorption
  transition_in: anchor-trigger
  transition_out: anchor-trigger
fractionation:
  target_depth: Level 3
  arousal: very-low
  affective_color: protected_absorption
  transition_in: anchor-trigger
  transition_out: anchor-trigger
integration:
  target_depth: Level 1
  arousal: settled
  affective_color: gentle_release
  transition_in: anchor-trigger
  transition_out: anchor-trigger
return:
  target_depth: Level 0
  arousal: alert
  affective_color: calm_trust
  transition_in: anchor-trigger
  transition_out: soft-fade

## Anchor Beats
first_anchor_pairing:
  occurs_within_stage: orientation
  approximate_time_seconds: 90
  evidence_modality: anchor-trigger
  function: "marks first carrier-anchor pairing, establishing safety"
induction_threshold:
  occurs_within_stage: induction
  approximate_time_seconds: 120
  evidence_modality: anchor-trigger
  function: "signals attention narrowing threshold, triggering deepening"
deepening_stability:
  occurs_within_stage: deepening
  approximate_time_seconds: 180
  evidence_modality: anchor-trigger
  function: "marks deep trance stability, triggering core work"
core_reassurance:
  occurs_within_stage: core_absorptive_work
  approximate_time_seconds: 210
  evidence_modality: anchor-trigger
  function: "reassures at core depth, preparing for fractionation"
fractionation_redeepening:
  occurs_within_stage: fractionation
  approximate_time_seconds: 60
  evidence_modality: anchor-trigger
  function: "signals re-deepening phase, returning to deep trance"
integration_stabilization:
  occurs_within_stage: integration
  approximate_time_seconds: 45
  evidence_modality: anchor-trigger
  function: "stabilizes integration phase, triggering return"

## Stage Boundary Contracts
orientation → induction:
  crossover_seconds: 12
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0
induction → deepening:
  crossover_seconds: 12
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0
deepening → core_absorptive_work:
  crossover_seconds: 12
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0
core_absorptive_work → fractionation:
  crossover_seconds: 12
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0
fractionation → integration:
  crossover_seconds: 12
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0
integration → return:
  crossover_seconds: 12
  boundary_audibility: imperceptible
  carrier_continuity: continuous
  silence_bridge_seconds: 0