# Composition Sketch Packet 05: Layer Orchestration

PACKET ID: cskp-004-05-layer-orchestration
SKETCH ID: 004
MANIFEST ID: wmf-neuro-ambient-composition-004
PASS NUMBER: 05

## Layer Roster
| Layer name (snake_case) | Layer type   | Voice role    | Register lane | Foreground stages                     | Background stages                                                                 |
|-------------------------|--------------|---------------|---------------|---------------------------------------|----------------------------------------------------------------------------------|
| substrate               | substrate    | pedal         | sub           | orientation_settling, deepening, integration, return_reorientation | induction_narrowing, core_absorptive_work, fractionation_pocket                   |
| harmonic_bed            | harmonic_bed | chord_bed     | low-mid       | core_absorptive_work                  | orientation_settling, induction_narrowing, deepening, fractionation_pocket, integration, return_reorientation |
| motif                   | motif        | carrier       | mid           | induction_narrowing, fractionation_pocket | orientation_settling, deepening, core_absorptive_work, integration, return_reorientation |
| air                     | air          | texture       | high          |                                       | orientation_settling, induction_narrowing, deepening, core_absorptive_work, fractionation_pocket, integration, return_reorientation |

Audit: All layers present in all stages. Motif foreground only during induction_narrowing and fractionation_pocket.

## Carrier Cell Assignment
induction_narrowing:
  carrier_layer: motif
  carrier_voice: "lead voice of motif layer"
  carrier_dynamic_role: foreground-statement

fractionation_pocket:
  carrier_layer: motif
  carrier_voice: "lead voice of motif layer"
  carrier_dynamic_role: foreground-statement

## Entrance and Exit Choreography
substrate:
  entrance_stage: orientation_settling
  entrance_offset_seconds: 0
  fade_in_duration_seconds: 15
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: null
  staggered_against: []
  stagger_minimum_gap_seconds: 0
  Rationale: Anchor establishes tonal center from composition start.

harmonic_bed:
  entrance_stage: orientation_settling
  entrance_offset_seconds: 30
  fade_in_duration_seconds: 25
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: null
  staggered_against: [substrate]
  stagger_minimum_gap_seconds: 30
  Rationale: Enters after substrate establishes foundation to avoid simultaneous onset.

motif:
  entrance_stage: induction_narrowing
  entrance_offset_seconds: 15
  fade_in_duration_seconds: 20
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: null
  staggered_against: [harmonic_bed]
  stagger_minimum_gap_seconds: 15
  Rationale: Enters after harmonic_bed establishes context to create foreground emergence.

air:
  entrance_stage: orientation_settling
  entrance_offset_seconds: 45
  fade_in_duration_seconds: 30
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: null
  staggered_against: [substrate, harmonic_bed]
  stagger_minimum_gap_seconds: 15
  Rationale: Enters after substrate and harmonic_bed to create layered spatial depth.

## Foreground Ownership Per Stage
orientation_settling:
  foreground_layer: substrate
  foreground_evidence: "substrate pedal establishes tonal center and warmth"
  background_layers: [harmonic_bed, motif, air]
  silence_layers: []

induction_narrowing:
  foreground_layer: motif
  foreground_evidence: "carrier cell statement on motif layer signals induction phase arrival"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []

deepening:
  foreground_layer: substrate
  foreground_evidence: "substrate pedal maintains stability during deepening phase"
  background_layers: [harmonic_bed, motif, air]
  silence_layers: []

core_absorptive_work:
  foreground_layer: harmonic_bed
  foreground_evidence: "harmonic_bed establishes core absorptive work plateau"
  background_layers: [substrate, motif, air]
  silence_layers: []

fractionation_pocket:
  foreground_layer: motif
  foreground_evidence: "carrier cell statement on motif layer initiates fractionation"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []

integration:
  foreground_layer: substrate
  foreground_evidence: "substrate pedal returns as integration begins"
  background_layers: [harmonic_bed, motif, air]
  silence_layers: []

return_reorientation:
  foreground_layer: substrate
  foreground_evidence: "substrate pedal provides gentle anchor for reorientation"
  background_layers: [harmonic_bed, motif, air]
  silence_layers: []