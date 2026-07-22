# Composition Sketch Packet 07: Motion Modulation Architecture

PACKET ID: cskp-004-07-motion-modulation-architecture
SKETCH ID: 004
MANIFEST ID: wmf-neuro-ambient-composition-004
PASS NUMBER: 07

## Rate Families

family_label: breath_family
rate_seed_hz: 0.15
rate_seed_rationale: "matches reference voice mean phrase rate (38 phrases/min) and slow breathing rhythm"
member_rates_hz: [0.15, 0.23, 0.37]
member_relationships: ["seed", "seed × 1.5 (incommensurable)", "seed × 2.46 (incommensurable)"]

family_label: drift_family
rate_seed_hz: 0.08
rate_seed_rationale: "geological drift for background layer evolution without conscious tracking"
member_rates_hz: [0.08, 0.13, 0.21]
member_relationships: ["seed", "seed × 1.6 (incommensurable)", "seed × 2.6 (incommensurable)"]

## Incommensurable Rate Pairings

pairings:
  - left_rate_hz: 0.15
    right_rate_hz: 0.23
    incommensurable_evidence: "15:23 ratio (coprime)"
    used_by: [ ("substrate", "core_absorptive_work"), ("harmonic_bed", "core_absorptive_work") ]

  - left_rate_hz: 0.08
    right_rate_hz: 0.13
    incommensurable_evidence: "8:13 ratio (coprime)"
    used_by: [ ("substrate", "deepening"), ("air", "deepening") ]

## Modulation Assignments — Stage 1: orientation_settling

substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.15
  depth: 0.04
  motion_role: breath

harmonic_bed:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.08
  depth: 0.02
  motion_role: drift

air:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.13
  depth: 0.02
  motion_role: drift

## Modulation Assignments — Stage 2: induction_narrowing

substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.15
  depth: 0.04
  motion_role: breath

harmonic_bed:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.23
  depth: 0.04
  motion_role: breath

air:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.13
  depth: 0.02
  motion_role: drift

## Modulation Assignments — Stage 3: deepening

substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.15
  depth: 0.06
  motion_role: breath

harmonic_bed:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.08
  depth: 0.03
  motion_role: drift

air:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.13
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 4: core_absorptive_work

substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.15
  depth: 0.06
  motion_role: breath

harmonic_bed:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.23
  depth: 0.06
  motion_role: breath

air:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.21
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 5: fractionation_pocket

substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.15
  depth: 0.02
  motion_role: breath

harmonic_bed:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.23
  depth: 0.02
  motion_role: breath

air:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.21
  depth: 0.02
  motion_role: drift

## Modulation Assignments — Stage 6: integration

substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.15
  depth: 0.03
  motion_role: breath

harmonic_bed:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.13
  depth: 0.02
  motion_role: drift

air:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.08
  depth: 0.02
  motion_role: drift

## Modulation Assignments — Stage 7: return_reorientation

substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.15
  depth: 0.02
  motion_role: breath

harmonic_bed:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.08
  depth: 0.01
  motion_role: drift

air:
  process: slow_filter
  rate_family: drift_family
  rate_hz: 0.13
  depth: 0.01
  motion_role: drift

## Macroform Modulation Arc

macroform:
  depth_arc: [ ("orientation_settling", "subtle"), ("induction_narrowing", "subtle"), ("deepening", "mid"), ("core_absorptive_work", "mid"), ("fractionation_pocket", "subtle"), ("integration", "subtle"), ("return_reorientation", "still") ]
  rate_density_arc: [ ("all stages", "mid") ]
  fractionation_alignment: "depth reduced at fractionation_lightening_anchor (60s) and fractionation_recovery_anchor (90s)"