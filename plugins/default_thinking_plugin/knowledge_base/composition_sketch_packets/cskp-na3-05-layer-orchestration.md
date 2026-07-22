# Composition Sketch Packet 05: Layer Orchestration
PACKET ID: cskp-na3-05-layer-orchestration
SKETCH ID: na3
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 05

## Layer Roster
| Layer name      | Layer type     | Voice role   | Register lane | Foreground stages                     | Background stages                                     |
|-----------------|----------------|--------------|---------------|---------------------------------------|-------------------------------------------------------|
| substrate       | substrate      | pedal        | low-mid       | none                                  | orientation, induction, deepening, core_absorptive_work, fractionation_pocket, integration, return |
| harmonic_bed    | harmonic_bed   | chord_bed    | low-mid       | core_absorptive_work                  | orientation, induction, deepening, fractionation_pocket, integration, return |
| entrainment     | entrainment    | carrier      | low-mid       | orientation, induction, deepening, fractionation_pocket, integration, return | none |
| air             | air            | texture      | air           | none                                  | orientation, induction, deepening, core_absorptive_work, fractionation_pocket, integration, return |

Layer coverage audit: All layers present in all stages (entrainment absent only in core_absorptive_work, covered by harmonic_bed foreground).

## Carrier Cell Assignment
orientation:
  carrier_layer: entrainment
  carrier_voice: "descending_third_second cell"
  carrier_dynamic_role: foreground-statement
induction:
  carrier_layer: entrainment
  carrier_voice: "descending_third_second cell"
  carrier_dynamic_role: foreground-statement
deepening:
  carrier_layer: entrainment
  carrier_voice: "descending_third_second cell"
  carrier_dynamic_role: foreground-statement
fractionation_pocket:
  carrier_layer: entrainment
  carrier_voice: "descending_third_second cell"
  carrier_dynamic_role: foreground-statement
integration:
  carrier_layer: entrainment
  carrier_voice: "descending_third_second cell"
  carrier_dynamic_role: foreground-statement
return:
  carrier_layer: entrainment
  carrier_voice: "descending_third_second cell"
  carrier_dynamic_role: foreground-statement

## Entrance and Exit Choreography
substrate:
  entrance_stage: orientation
  entrance_offset_seconds: 0
  fade_in_duration_seconds: 30
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: 0
  staggered_against: []
  stagger_minimum_gap_seconds: 0

harmonic_bed:
  entrance_stage: orientation
  entrance_offset_seconds: 15
  fade_in_duration_seconds: 30
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: 0
  staggered_against: [substrate]
  stagger_minimum_gap_seconds: 15

air:
  entrance_stage: orientation
  entrance_offset_seconds: 60
  fade_in_duration_seconds: 45
  exit_stage: return
  exit_offset_seconds: 15
  fade_out_duration_seconds: 10
  staggered_against: [substrate, harmonic_bed]
  stagger_minimum_gap_seconds: 15

entrainment:
  entrance_stage: orientation
  entrance_offset_seconds: 150
  fade_in_duration_seconds: 30
  exit_stage: return
  exit_offset_seconds: 15
  fade_out_duration_seconds: 15
  staggered_against: [substrate, harmonic_bed, air]
  stagger_minimum_gap_seconds: 30

Stagger rationale: Entrances spaced to avoid simultaneous onset (substrate first, then harmonic_bed 15s later, air 45s after harmonic_bed fade-in complete, entrainment at carrier pairing point with 30s fade-in).

## Foreground Ownership Per Stage
orientation:
  foreground_layer: entrainment
  foreground_evidence: "carrier cell establishes first anchor pairing at stage boundary"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []
induction:
  foreground_layer: entrainment
  foreground_evidence: "carrier cell signals attention narrowing at induction threshold"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []
deepening:
  foreground_layer: entrainment
  foreground_evidence: "carrier cell marks depth stability at deepening threshold"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []
core_absorptive_work:
  foreground_layer: harmonic_bed
  foreground_evidence: "carrier absent; bed provides stable absorption field"
  background_layers: [substrate, air]
  silence_layers: [entrainment]
fractionation_pocket:
  foreground_layer: entrainment
  foreground_evidence: "carrier cell signals re-deepening at fractionation threshold"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []
integration:
  foreground_layer: entrainment
  foreground_evidence: "carrier cell signals final return at integration threshold"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []
return:
  foreground_layer: entrainment
  foreground_evidence: "carrier cell provides subtle cadence during dissolution"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []