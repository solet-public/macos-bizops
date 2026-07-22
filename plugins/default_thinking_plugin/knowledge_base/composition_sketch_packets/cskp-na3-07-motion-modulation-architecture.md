# Composition Sketch Packet 07: Motion Modulation Architecture
PACKET ID: cskp-na3-07-motion-modulation-architecture
SKETCH ID: na3
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 07

## Rate Families
family_label: breath_family
rate_seed_hz: 0.12
rate_seed_rationale: "matches reference voice mean phrase rate (1.55s) and anchor beat spacing"
member_rates_hz: [0.12, 0.19, 0.31]
member_relationships: ["seed", "seed * golden_ratio (≈1.618)", "seed * 2.59"]

family_label: drift_family
rate_seed_hz: 0.05
rate_seed_rationale: "matches geological drift range (0.02-0.1 Hz) for background evolution"
member_rates_hz: [0.05, 0.08, 0.13]
member_relationships: ["seed", "seed * 1.6", "seed * 2.6"]

family_label: anchor_family
rate_seed_hz: 0.02
rate_seed_rationale: "matches anchor beat spacing (180s for orientation, 240s for induction)"
member_rates_hz: [0.02, 0.03, 0.05]
member_relationships: ["seed", "seed * 1.5", "seed * 2.5"]

## Incommensurable Rate Pairings
pairings:
  - left_rate_hz: 0.12
    right_rate_hz: 0.05
    incommensurable_evidence: "irrational-ratio (golden_ratio)"
    used_by: [("substrate", "core_absorptive_work"), ("harmonic_bed", "core_absorptive_work")]

## Modulation Assignments — Stage 1: orientation
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.12
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: phaser
  rate_family: breath_family
  rate_hz: 0.19
  depth: 0.03
  motion_role: drift
air:
  process: phaser
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.10
  motion_role: drift

## Modulation Assignments — Stage 2: induction
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.12
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: phaser
  rate_family: breath_family
  rate_hz: 0.19
  depth: 0.03
  motion_role: drift
air:
  process: phaser
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.10
  motion_role: drift

## Modulation Assignments — Stage 3: deepening
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.12
  depth: 0.05
  motion_role: breath
harmonic_bed:
  process: phaser
  rate_family: breath_family
  rate_hz: 0.19
  depth: 0.04
  motion_role: drift
air:
  process: phaser
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.12
  motion_role: drift

## Modulation Assignments — Stage 4: core_absorptive_work
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.12
  depth: 0.05
  motion_role: breath
harmonic_bed:
  process: phaser
  rate_family: breath_family
  rate_hz: 0.19
  depth: 0.04
  motion_role: drift
air:
  process: phaser
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.12
  motion_role: drift

## Modulation Assignments — Stage 5: fractionation_pocket
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.12
  depth: 0.03
  motion_role: breath
harmonic_bed:
  process: phaser
  rate_family: breath_family
  rate_hz: 0.19
  depth: 0.02
  motion_role: drift
air:
  process: phaser
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.08
  motion_role: drift

## Modulation Assignments — Stage 6: integration
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.12
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: phaser
  rate_family: breath_family
  rate_hz: 0.19
  depth: 0.03
  motion_role: drift
air:
  process: phaser
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.10
  motion_role: drift

## Modulation Assignments — Stage 7: return
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.12
  depth: 0.03
  motion_role: breath
harmonic_bed:
  process: phaser
  rate_family: breath_family
  rate_hz: 0.19
  depth: 0.02
  motion_role: drift
air:
  process: phaser
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.05
  motion_role: drift

## Macroform Modulation Arc
macroform:
  depth_arc: [("orientation", "subtle"), ("induction", "subtle"), ("deepening", "mid"), ("core_absorptive_work", "mid"), ("fractionation_pocket", "subtle"), ("integration", "subtle"), ("return", "still")]
  rate_density_arc: [("orientation", "medium"), ("induction", "medium"), ("deepening", "high"), ("core_absorptive_work", "high"), ("fractionation_pocket", "medium"), ("integration", "medium"), ("return", "low")]
  fractionation_alignment: "air depth reduced at fractionation_brightness_closed anchor beat (120s) to create lightening effect"