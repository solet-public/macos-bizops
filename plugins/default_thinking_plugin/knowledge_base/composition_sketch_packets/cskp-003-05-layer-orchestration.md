# Composition Sketch Packet 05: Layer Orchestration
PACKET ID: cskp-003-05-layer-orchestration
SKETCH ID: 003
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 05

## Layer Roster
| Layer name | Layer type | Voice role | Register lane | Foreground stages | Background stages |
|------------|------------|------------|---------------|-------------------|-------------------|
| substrate | substrate | pedal | sub-low to low-mid |  | orientation, induction, deepening, core_absorptive_work, fractionation, integration, return |
| harmonic_bed | harmonic_bed | chord_bed | low-mid to mid | induction, deepening, core_absorptive_work, fractionation, integration | orientation |
| entrainment | entrainment | carrier | low-mid to upper-mid | orientation, core_absorptive_work | induction, deepening, fractionation, integration, return |
| air | air | texture | upper-mid to high |  | orientation, induction, deepening, core_absorptive_work, fractionation, integration, return |
| motif | motif | companion | mid to upper-mid | core_absorptive_work | orientation, induction, deepening, fractionation, integration, return |
Audit: All layers present in every stage (foreground or background); motif absent from all stages except core_absorptive_work.

## Carrier Cell Assignment
orientation:
  carrier_layer: entrainment
  carrier_voice: "lead voice of entrainment layer"
  carrier_dynamic_role: foreground-statement
induction:
  carrier_layer: entrainment
  carrier_voice: "background voice of entrainment layer"
  carrier_dynamic_role: silhouette
deepening:
  carrier_layer: entrainment
  carrier_voice: "background voice of entrainment layer"
  carrier_dynamic_role: silhouette
core_absorptive_work:
  carrier_layer: entrainment
  carrier_voice: "fragmented voice of entrainment layer"
  carrier_dynamic_role: embedded
fractionation:
  carrier_layer: entrainment
  carrier_voice: "background voice of entrainment layer"
  carrier_dynamic_role: silhouette
integration:
  carrier_layer: entrainment
  carrier_voice: "background voice of entrainment layer"
  carrier_dynamic_role: silhouette

## Entrance and Exit Choreography
substrate:
  entrance_stage: orientation
  entrance_offset_seconds: 0
  fade_in_duration_seconds: 15
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: null
  staggered_against: []
  stagger_minimum_gap_seconds: 0
harmonic_bed:
  entrance_stage: orientation
  entrance_offset_seconds: 30
  fade_in_duration_seconds: 20
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: null
  staggered_against: [substrate]
  stagger_minimum_gap_seconds: 30
entrainment:
  entrance_stage: orientation
  entrance_offset_seconds: 45
  fade_in_duration_seconds: 15
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: null
  staggered_against: [substrate]
  stagger_minimum_gap_seconds: 45
air:
  entrance_stage: orientation
  entrance_offset_seconds: 20
  fade_in_duration_seconds: 15
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: null
  staggered_against: [substrate]
  stagger_minimum_gap_seconds: 20
motif:
  entrance_stage: core_absorptive_work
  entrance_offset_seconds: 130
  fade_in_duration_seconds: 5
  exit_stage: core_absorptive_work
  exit_offset_seconds: 415
  fade_out_duration_seconds: 15
  staggered_against: [harmonic_bed, entrainment]
  stagger_minimum_gap_seconds: 5
Rationale: Substrate enters first (0s). Air, harmonic_bed, and entrainment stagger against substrate with minimum gaps of 20s, 30s, and 45s. Motif enters in core_absorptive_work (130s into stage) staggered against harmonic_bed and entrainment with 5s gap to ensure perceptible emergence.

## Foreground Ownership Per Stage
orientation:
  foreground_layer: entrainment
  foreground_evidence: "carrier statement on entrainment layer signals first anchor pairing"
  background_layers: [substrate, harmonic_bed, air]
  silence_layers: []
induction:
  foreground_layer: harmonic_bed
  foreground_evidence: "harmonic bed's subtle shift in voicing signals attention narrowing"
  background_layers: [substrate, entrainment, air]
  silence_layers: []
deepening:
  foreground_layer: harmonic_bed
  foreground_evidence: "harmonic bed's contrast region shift signals deepening"
  background_layers: [substrate, entrainment, air]
  silence_layers: []
core_absorptive_work:
  foreground_layer: motif
  foreground_evidence: "rare motif fragment on companion layer signals core reassurance"
  background_layers: [substrate, harmonic_bed, entrainment, air]
  silence_layers: []
fractionation:
  foreground_layer: harmonic_bed
  foreground_evidence: "harmonic bed's contrast region with brief brightness signals fractionation"
  background_layers: [substrate, entrainment, air]
  silence_layers: []
integration:
  foreground_layer: harmonic_bed
  foreground_evidence: "harmonic bed's return region shift signals integration"
  background_layers: [substrate, entrainment, air]
  silence_layers: []
return:
  foreground_layer: substrate
  foreground_evidence: "substrate's sustained warmth signals return to baseline"
  background_layers: [harmonic_bed, entrainment, air]
  silence_layers: []