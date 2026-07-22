# Composition Sketch Packet 07: Motion Modulation Architecture
PACKET ID: cskp-03n8zqkx1w9-07-motion-modulation-architecture
SKETCH ID: 03n8zqkx1w9
MANIFEST ID: wmf-neuro-ambient-composition-03-001
PASS NUMBER: 07

## Rate Families
family_label: breath_family
rate_seed_hz: 0.1
rate_seed_rationale: "matches reference voice mean phrase rate from Packet 03"
member_rates_hz: [0.1, 0.13, 0.21]
member_relationships: ["seed", "seed * golden_ratio (approx)", "seed * 2.1"]

family_label: drift_family
rate_seed_hz: 0.05
rate_seed_rationale: "matches geological drift rate for background evolution"
member_rates_hz: [0.05, 0.08, 0.13]
member_relationships: ["seed", "seed * golden_ratio (approx)", "seed * 2.6"]

family_label: anchor_family
rate_seed_hz: 0.1
rate_seed_rationale: "matches anchor beat spacing from Packet 02"
member_rates_hz: [0.1, 0.15]
member_relationships: ["seed", "seed * 1.5"]

## Incommensurable Rate Pairings
pairings:
  - left_rate_hz: 0.13
    right_rate_hz: 0.08
    incommensurable_evidence: "near-golden"
    used_by: [("substrate", "deepening"), ("harmonic_bed", "deepening")]

## Modulation Assignments — Stage 1: orientation
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 2: induction
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.03
  motion_role: drift

## Modulation Assignments — Stage 3: deepening
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.03
  motion_role: drift
air:
  process: tremolo
  rate_family: anchor_family
  rate_hz: 0.15
  depth: 0.03
  motion_role: anchor-pulse

## Modulation Assignments — Stage 4: core_absorptive_work
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.04
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.03
  motion_role: drift
air:
  process: tremolo
  rate_family: anchor_family
  rate_hz: 0.15
  depth: 0.03
  motion_role: anchor-pulse

## Modulation Assignments — Stage 5: fractionation
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.03
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.02
  motion_role: drift
air:
  process: tremolo
  rate_family: anchor_family
  rate_hz: 0.15
  depth: 0.02
  motion_role: anchor-pulse

## Modulation Assignments — Stage 6: integration
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.03
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.02
  motion_role: drift
air:
  process: tremolo
  rate_family: anchor_family
  rate_hz: 0.15
  depth: 0.02
  motion_role: anchor-pulse

## Modulation Assignments — Stage 7: return
substrate:
  process: tremolo
  rate_family: breath_family
  rate_hz: 0.1
  depth: 0.03
  motion_role: breath
harmonic_bed:
  process: tremolo
  rate_family: drift_family
  rate_hz: 0.05
  depth: 0.02
  motion_role: drift
air:
  process: tremolo
  rate_family: anchor_family
  rate_hz: 0.15
  depth: 0.02
  motion_role: anchor-pulse

## Macroform Modulation Arc
macroform:
  depth_arc: [("orientation", "subtle"), ("induction", "subtle"), ("deepening", "mid"), ("core_absorptive_work", "mid"), ("fractionation", "subtle"), ("integration", "subtle"), ("return", "subtle")]
  rate_density_arc: [("orientation", "subtle"), ("induction", "subtle"), ("deepening", "mid"), ("core_absorptive_work", "mid"), ("fractionation", "subtle"), ("integration", "subtle"), ("return", "subtle")]
  fractionation_alignment: "reduction in modulation depth and rate density during lightening phase of fractionation, aligned with anchor_fractionation_redeepening at 1200s"