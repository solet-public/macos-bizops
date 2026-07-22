# Composition Sketch Packet 05: Layer Orchestration
PACKET ID: cskp-03n8zqkx1w9-05-layer-orchestration
SKETCH ID: 03n8zqkx1w9
MANIFEST ID: wmf-neuro-ambient-composition-03-001
PASS NUMBER: 05

## Layer Roster
| Layer name (snake_case) | Layer type | Voice role | Register lane | Foreground stages | Background stages |
| --- | --- | --- | --- | --- | --- |
| substrate | substrate | pedal | low |  | orientation, induction, deepening, core_absorptive_work, fractionation, integration, return |
| harmonic_bed | harmonic_bed | chord_bed | low-mid |  | orientation, induction, deepening, core_absorptive_work, fractionation, integration, return |
| air | air | texture | high |  | orientation, induction, deepening, core_absorptive_work, fractionation, integration, return |
| motif | motif | carrier | mid | core_absorptive_work | orientation, induction, deepening, fractionation, integration |

Layer coverage audit: All layers present in all stages except motif absent during return.

## Carrier Cell Assignment
orientation:
  carrier_layer: motif
  carrier_voice: "first pairing of falling-settling cell at anchor-trigger"
  carrier_dynamic_role: background-statement
induction:
  carrier_layer: motif
  carrier_voice: "threshold signal for deepening at anchor-trigger"
  carrier_dynamic_role: background-statement
deepening:
  carrier_layer: motif
  carrier_voice: "stability point for deepening at anchor-trigger"
  carrier_dynamic_role: background-statement
core_absorptive_work:
  carrier_layer: motif
  carrier_voice: "reassurance signal during core work at anchor-trigger"
  carrier_dynamic_role: foreground-statement
fractionation:
  carrier_layer: motif
  carrier_voice: "re-deepening signal after fractionation at anchor-trigger"
  carrier_dynamic_role: background-statement
integration:
  carrier_layer: motif
  carrier_voice: "start of integration signal at anchor-trigger"
  carrier_dynamic_role: background-statement

## Entrance and Exit Choreography
substrate:
  entrance_stage: orientation
  entrance_offset_seconds: 0
  fade_in_duration_seconds: 30
  exit_stage: return
  exit_offset_seconds: 1400
  fade_out_duration_seconds: 30
  staggered_against: []
  stagger_minimum_gap_seconds: 0
  Stagger rationale: Anchor establishes foundation before harmonic bed enters.

harmonic_bed:
  entrance_stage: orientation
  entrance_offset_seconds: 15
  fade_in_duration_seconds: 30
  exit_stage: return
  exit_offset_seconds: 1400
  fade_out_duration_seconds: 30
  staggered_against: [substrate]
  stagger_minimum_gap_seconds: 15
  Stagger rationale: Bed enters after substrate establishes tonal anchor.

air:
  entrance_stage: deepening
  entrance_offset_seconds: 420
  fade_in_duration_seconds: 30
  exit_stage: return
  exit_offset_seconds: 1400
  fade_out_duration_seconds: 30
  staggered_against: [substrate, harmonic_bed]
  stagger_minimum_gap_seconds: 15
  Stagger rationale: Air enters after harmonic bed establishes texture.

motif:
  entrance_stage: orientation
  entrance_offset_seconds: 90
  fade_in_duration_seconds: 15
  exit_stage: integration
  exit_offset_seconds: 1405
  fade_out_duration_seconds: 5
  staggered_against: [substrate, harmonic_bed]
  stagger_minimum_gap_seconds: 30
  Stagger rationale: Carrier cell enters after substrate establishes foundation.

## Foreground Ownership Per Stage
orientation:
  foreground_layer: null
  foreground_evidence: "carrier cell background-statement establishes safety"
  background_layers: [substrate, harmonic_bed, motif]
  silence_layers: [air]
induction:
  foreground_layer: null
  foreground_evidence: "carrier cell background-statement guides attention narrowing"
  background_layers: [substrate, harmonic_bed, motif]
  silence_layers: [air]
deepening:
  foreground_layer: null
  foreground_evidence: "carrier cell background-statement maintains stability"
  background_layers: [substrate, harmonic_bed, air, motif]
  silence_layers: []
core_absorptive_work:
  foreground_layer: motif
  foreground_evidence: "carrier statement on motif layer signals core reassurance"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []
fractionation:
  foreground_layer: null
  foreground_evidence: "carrier cell background-statement enables re-deepening"
  background_layers: [substrate, harmonic_bed, air, motif]
  silence_layers: []
integration:
  foreground_layer: null
  foreground_evidence: "carrier cell background-statement stabilizes depth"
  background_layers: [substrate, harmonic_bed, air, motif]
  silence_layers: []
return:
  foreground_layer: null
  foreground_evidence: "carrier cell absent during return to baseline"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: [motif]