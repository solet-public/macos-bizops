# Composition Sketch Packet 02: Formal State Dramaturgy
PACKET ID: cskp-nd3-02-formal-state-dramaturgy
SKETCH ID: nd3
MANIFEST ID: wmf-neuro-ambient-nd3-001
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

Stage count equals Pass 01's stage_count (7). Stage duration sum equals target_duration_seconds (1440) with 0 deviation.

## Felt-State Arc
orientation:
  target_depth: Level 1
  arousal: settled
  affective_color: calm, safe
  transition_in: cold-start
  transition_out: soft-fade
induction:
  target_depth: Level 2
  arousal: low
  affective_color: focused, gentle
  transition_in: soft-fade
  transition_out: soft-fade
deepening:
  target_depth: Level 3
  arousal: very-low
  affective_color: deep, immersive
  transition_in: soft-fade
  transition_out: soft-fade
core_absorptive_work:
  target_depth: Level 3
  arousal: very-low
  affective_color: sustained, deep absorption
  transition_in: soft-fade
  transition_out: soft-fade
fractionation_pocket:
  target_depth: Level 2
  arousal: low
  affective_color: lightened, re-deepened
  transition_in: soft-fade
  transition_out: soft-fade
integration:
  target_depth: Level 1
  arousal: settled
  affective_color: stable, gentle return
  transition_in: soft-fade
  transition_out: soft-fade
return:
  target_depth: Level 0
  arousal: alert
  affective_color: reoriented, calm
  transition_in: soft-fade
  transition_out: soft-fade

## Anchor Beats
first_carrier_pairing:
  occurs_within_stage: orientation
  approximate_time_seconds: 120
  evidence_modality: harmonic-shift
  function: "marks the first pairing of carrier with anchor"
deepening_start:
  occurs_within_stage: induction
  approximate_time_seconds: 200
  evidence_modality: spatial-shift
  function: "signals the beginning of deepening"
core_absorption_start:
  occurs_within_stage: deepening
  approximate_time_seconds: 300
  evidence_modality: density-shift
  function: "signals the start of core absorption"
fractionation_seed:
  occurs_within_stage: core_absorptive_work
  approximate_time_seconds: 360
  evidence_modality: harmonic-shift
  function: "seeds fractionation cycle before core work"
fractionation_redeepening:
  occurs_within_stage: fractionation_pocket
  approximate_time_seconds: 60
  evidence_modality: harmonic-shift
  function: "signals re-deepening phase of fractionation"
integration_end:
  occurs_within_stage: integration
  approximate_time_seconds: 60
  evidence_modality: spatial-shift
  function: "signals transition from integration to return"

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