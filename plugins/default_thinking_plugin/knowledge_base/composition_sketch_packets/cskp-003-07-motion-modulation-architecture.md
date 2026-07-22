# Composition Sketch Packet 07: Motion Modulation Architecture
PACKET ID: cskp-003-07-motion-modulation-architecture
SKETCH ID: 003
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 07

## Rate Families
family_label: breath_family
rate_seed_hz: 0.1
rate_seed_rationale: "adjusted from reference voice mean phrase rate (0.645 Hz) to slow breathing range"
member_rates_hz: [0.1, 0.16, 0.26]
member_relationships: ["seed", "seed × golden-ratio (1.618)", "seed × golden-ratio^2 (2.618)"]

family_label: drift_family
rate_seed_hz: 0.05
rate_seed_rationale: "geological drift rate for background texture evolution"
member_rates_hz: [0.05, 0.08, 0.13]
member_relationships: ["seed", "seed × golden-ratio (1.6)", "seed × golden-ratio^2 (2.6)"]

## Incommensurable Rate Pairings
pairings:
  - left_rate_hz: 0.1
    right_rate_hz: 0.16
    incommensurable_evidence: "golden-ratio"
    used_by: [("substrate", "core_absorptive_work"), ("harmonic_bed", "core_absorptive_work")]

## Modulation Assignments — Stage 1: orientation
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.16
  depth: 0.04
  motion_role: breath
air:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 2: induction
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.16
  depth: 0.05
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.26
  depth: 0.05
  motion_role: breath
air:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.08
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 3: deepening
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.06
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.16
  depth: 0.06
  motion_role: breath
air:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.13
  depth: 0.04
  motion_role: drift

## Modulation Assignments — Stage 4: core_absorptive_work
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.16
  depth: 0.07
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.26
  depth: 0.07
  motion_role: breath
air:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.13
  depth: 0.04
  motion_role: drift

## Modulation Assignments — Stage 5: fractionation
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.03
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.16
  depth: 0.03
  motion_role: breath
air:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.02
  motion_role: drift

## Modulation Assignments — Stage 6: integration
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
macroform:
  depth_arc: [("orientation", "subtle"), ("induction", "subtle"), ("deepening", "mid"), ("core_absorptive_work", "pronounced"), ("fractionation", "subtle"), ("integration", "still"), ("return", "still")]
  rate_density_arc: [("orientation", "low"), ("induction", "medium"), ("deepening", "medium"), ("core_absorptive_work", "high"), ("fractionation", "low"), ("integration", "none"), ("return", "none")]
  fractionation_alignment: "modulation depth reduced during lightening phase (20:00-21:00) and increased during re-deepening phase (21:00-22:00)"