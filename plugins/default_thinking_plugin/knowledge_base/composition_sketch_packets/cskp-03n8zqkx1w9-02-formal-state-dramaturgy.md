# Composition Sketch Packet 02: Formal State Dramaturgy
PACKET ID: cskp-03n8zqkx1w9-02-formal-state-dramaturgy
SKETCH ID: 03n8zqkx1w9
MANIFEST ID: wmf-neuro-ambient-composition-03-001
PASS NUMBER: 02

## Stage Sequence
| Index | Stage name (snake_case) | Hypnotic function | Duration in seconds |
| ---   | ---                     | ---               | ---                 |
| 1     | orientation             | arrival           | 180                 |
| 2     | induction               | induction         | 240                 |
| 3     | deepening               | deepening         | 360                 |
| 4     | core_absorptive_work    | core_work         | 420                 |
| 5     | fractionation           | fractionation     | 120                 |
| 6     | integration             | integration       | 90                  |
| 7     | return                  | return            | 30                  |

Stage count: 7 (matches Pass 01's stage_count)
Total duration sum: 1440 seconds (within ±15 tolerance of Pass 01's target_duration_seconds)

## Felt-State Arc
orientation:
  target_depth: Level 0 → Level 1
  arousal: settled
  affective_color: calm_trust
  transition_in: cold-start
  transition_out: anchor-trigger

induction:
  target_depth: Level 1 → Level 2
  arousal: low
  affective_color: inward_curiosity
  transition_in: anchor-trigger
  transition_out: soft-fade

deepening:
  target_depth: Level 2
  arousal: low
  affective_color: protected_absorption
  transition_in: soft-fade
  transition_out: soft-fade

core_absorptive_work:
  target_depth: Level 2
  arousal: low
  affective_color: protected_absorption
  transition_in: soft-fade
  transition_out: soft-fade

fractionation:
  target_depth: Level 2 (with temporary Level 1)
  arousal: low
  affective_color: protected_absorption
  transition_in: soft-fade
  transition_out: soft-fade

integration:
  target_depth: Level 2 → Level 1
  arousal: settled
  affective_color: gentle_release
  transition_in: soft-fade
  transition_out: soft-fade

return:
  target_depth: Level 1 → Level 0
  arousal: alert
  affective_color: calm_trust
  transition_in: soft-fade
  transition_out: soft-fade

## Anchor Beats
anchor_first_pairing:
  occurs_within_stage: orientation
  approximate_time_seconds: 90
  evidence_modality: anchor-trigger
  function: "marks first pairing of carrier and anchor, enabling induction"

anchor_induction_threshold:
  occurs_within_stage: induction
  approximate_time_seconds: 410
  evidence_modality: anchor-trigger
  function: "signals threshold for deepening, triggering attention narrowing"

anchor_deepening_stability:
  occurs_within_stage: deepening
  approximate_time_seconds: 600
  evidence_modality: anchor-trigger
  function: "marks stability point for deepening, enabling core work"

anchor_core_reassurance:
  occurs_within_stage: core_absorptive_work
  approximate_time_seconds: 990
  evidence_modality: anchor-trigger
  function: "reassures listener during core work, maintaining absorption"

anchor_fractionation_redeepening:
  occurs_within_stage: fractionation
  approximate_time_seconds: 1200
  evidence_modality: anchor-trigger
  function: "signals re-deepening after fractionation lightening"

anchor_integration:
  occurs_within_stage: integration
  approximate_time_seconds: 1320
  evidence_modality: anchor-trigger
  function: "signals start of integration, stabilizing depth"

## Stage Boundary Contracts
orientation → induction:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0

induction → deepening:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0

deepening → core_absorptive_work:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0

core_absorptive_work → fractionation:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0

fractionation → integration:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0

integration → return:
  crossover_seconds: 30
  boundary_audibility: subtle
  carrier_continuity: continuous
  silence_bridge_seconds: 0