# Composition Sketch Packet 07: Motion Modulation Architecture
PACKET ID: cskp-nd3-07-motion-modulation-architecture
SKETCH ID: nd3
MANIFEST ID: wmf-neuro-ambient-nd3-001
PASS NUMBER: 07

## Rate Families
family_label: breath_family
rate_seed_hz: 0.11
rate_seed_rationale: "matches reference voice mean phrase rate (9s cycle)"
member_rates_hz: [0.11, 0.17, 0.28]
member_relationships: ["seed", "seed * 1.54 (incommensurable)", "seed * 2.54 (incommensurable)"]

family_label: drift_family
rate_seed_hz: 0.07
rate_seed_rationale: "matches geological drift for background evolution"
member_rates_hz: [0.07, 0.11, 0.18]
member_relationships: ["seed", "seed * 1.57 (incommensurable)", "seed * 2.57 (incommensurable)"]

## Incommensurable Rate Pairings
pairings:
  - left_rate_hz: 0.11
    right_rate_hz: 0.07
    incommensurable_evidence: "irrational-ratio (11/7)"
    used_by: ["substrate", "harmonic_bed"]

## Modulation Assignments — Stage 1: orientation
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.11
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.07
  depth: 0.04
  motion_role: breath

## Modulation Assignments — Stage 2: induction
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.11
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.07
  depth: 0.04
  motion_role: breath

## Modulation Assignments — Stage 3: deepening
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.11
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.07
  depth: 0.04
  motion_role: breath
air:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.18
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 4: core_absorptive_work
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.11
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.07
  depth: 0.04
  motion_role: breath
air:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.18
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 5: fractionation_pocket
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.11
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.07
  depth: 0.04
  motion_role: breath
air:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.18
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 6: integration
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.11
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.07
  depth: 0.04
  motion_role: breath
air:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.18
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 7: return
substrate:
  process: none
  rate_family: null
  rate_hz: null
  depth: null
  motion_role: none
harmonic_bed:
  process: none
  rate_family: null
  rate_hz: null
  depth: null
  motion_role: none
air:
  process: none
  rate_family: null
  rate_hz: null
  depth: null
  motion_role: none

## Macroform Modulation Arc
depth_arc:
  - orientation: subtle
  - induction: subtle
  - deepening: mid
  - core_absorptive_work: mid
  - fractionation_pocket: subtle
  - integration: subtle
  - return: still
rate_density_arc:
  - orientation: subtle
  - induction: subtle
  - deepening: mid
  - core_absorptive_work: mid
  - fractionation_pocket: subtle
  - integration: subtle
  - return: still
fractionation_alignment: "modulation depth reduced during fractionation events (core_absorptive_work at 360s and fractionation_pocket at 60s) to support lightening"