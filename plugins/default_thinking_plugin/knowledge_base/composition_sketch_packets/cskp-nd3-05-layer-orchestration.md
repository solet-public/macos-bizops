# Composition Sketch Packet 05: Layer Orchestration
PACKET ID: cskp-nd3-05-layer-orchestration
SKETCH ID: nd3
MANIFEST ID: wmf-neuro-ambient-nd3-001
PASS NUMBER: 05

## Layer Roster
| Layer name | Layer type | Voice role | Register lane | Foreground stages | Background stages |
| --- | --- | --- | --- | --- | --- |
| substrate | substrate | pedal | low-mid |  | orientation, induction, deepening, core_absorptive_work, fractionation_pocket, integration, return |
| harmonic_bed | harmonic_bed | chord_bed | low-mid |  | orientation, induction, deepening, core_absorptive_work, fractionation_pocket, integration, return |
| entrainment | entrainment | motion | mid |  | induction, deepening, core_absorptive_work, fractionation_pocket, integration, return |
| air | air | texture | high |  | deepening, core_absorptive_work, fractionation_pocket, integration, return |
| motif | motif | carrier | low-mid |  | orientation, induction, deepening, core_absorptive_work, fractionation_pocket, integration |

Layer coverage audit: All layers present in at least one stage. No layer appears in both foreground and background for any stage.

## Carrier Cell Assignment
orientation:
  carrier_layer: motif
  carrier_voice: "three-note descending minor third cell (F2-Eb2-C2) with 3s per note"
  carrier_dynamic_role: embedded
induction:
  carrier_layer: motif
  carrier_voice: "transformed to minor third step with 3s per note (F2-Eb2-C2)"
  carrier_dynamic_role: embedded
deepening:
  carrier_layer: motif
  carrier_voice: "augmented transformation (F2-Eb2-C2) with 4s per note"
  carrier_dynamic_role: embedded
core_absorptive_work:
  carrier_layer: motif
  carrier_voice: "transformed to minor third step with 3s per note (F2-Eb2-C2)"
  carrier_dynamic_role: embedded
fractionation_pocket:
  carrier_layer: motif
  carrier_voice: "diminished transformation (F2-Eb2-C2) with 3s per note"
  carrier_dynamic_role: embedded
integration:
  carrier_layer: motif
  carrier_voice: "transformed to minor third step with 3s per note (F2-Eb2-C2)"
  carrier_dynamic_role: embedded

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
  entrance_offset_seconds: 30
  fade_in_duration_seconds: 20
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: 0
  staggered_against: [substrate]
  stagger_minimum_gap_seconds: 15

motif:
  entrance_stage: orientation
  entrance_offset_seconds: 120
  fade_in_duration_seconds: 15
  exit_stage: return
  exit_offset_seconds: 0
  fade_out_duration_seconds: 30
  staggered_against: [harmonic_bed]
  stagger_minimum_gap_seconds: 10

entrainment:
  entrance_stage: induction
  entrance_offset_seconds: 30
  fade_in_duration_seconds: 25
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: 0
  staggered_against: [substrate, harmonic_bed]
  stagger_minimum_gap_seconds: 20

air:
  entrance_stage: deepening
  entrance_offset_seconds: 30
  fade_in_duration_seconds: 30
  exit_stage: null
  exit_offset_seconds: null
  fade_out_duration_seconds: 0
  staggered_against: [substrate, harmonic_bed]
  stagger_minimum_gap_seconds: 25

Stagger rationale: All entrances avoid simultaneous onset through minimum gap requirements. Staggering follows orchestration principle of spectral gap conditioning (air enters after harmonic bed establishes mid-register space).

## Foreground Ownership Per Stage
orientation:
  foreground_layer: motif
  foreground_evidence: "first carrier cell statement at 120s signals arrival and first pairing"
  background_layers: [substrate, harmonic_bed]
  silence_layers: [entrainment, air]
induction:
  foreground_layer: motif
  foreground_evidence: "carrier cell returns at deepening threshold (200s) signals induction completion"
  background_layers: [substrate, harmonic_bed, entrainment]
  silence_layers: [air]
deepening:
  foreground_layer: motif
  foreground_evidence: "carrier cell transformation at 300s signals deepening onset"
  background_layers: [substrate, harmonic_bed, entrainment, air]
  silence_layers: []
core_absorptive_work:
  foreground_layer: motif
  foreground_evidence: "carrier cell statement at 360s seeds fractionation cycle"
  background_layers: [substrate, harmonic_bed, entrainment, air]
  silence_layers: []
fractionation_pocket:
  foreground_layer: motif
  foreground_evidence: "carrier cell transformation at 60s signals re-deepening"
  background_layers: [substrate, harmonic_bed, entrainment, air]
  silence_layers: []
integration:
  foreground_layer: motif
  foreground_evidence: "carrier cell statement at 60s signals integration completion"
  background_layers: [substrate, harmonic_bed, entrainment, air]
  silence_layers: []
return:
  foreground_layer: substrate
  foreground_evidence: "anchor remains as sole voice during dissolution"
  background_layers: [harmonic_bed, entrainment, air]
  silence_layers: [motif]