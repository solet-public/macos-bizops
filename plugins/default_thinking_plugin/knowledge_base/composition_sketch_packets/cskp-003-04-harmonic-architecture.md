# Composition Sketch Packet 04: Harmonic Architecture
PACKET ID: cskp-003-04-harmonic-architecture
SKETCH ID: 003
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 04

## Tonal Center
tonal_center_note: F2
tonal_center_hz: 87.306
tuning_reference: A4=440
tonal_center_rationale: "F2 is the primary tonal center for warm, dark low-mid anchor throughout; matches carrier cell's descending range and Complete Brief Form specification."

## Harmonic Regions
region_label: home
root_hz: 87.306
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 3, 7, 10]
voicing_register: low-mid
mode_or_color: "F Aeolian minor 7th with open fifth and flat seventh"
function: home

region_label: contrast
root_hz: 69.306
root_note: Db2
relationship_to_tonal_center: minor_third-down
intervals_semitones: [0, 4, 7]
voicing_register: low-mid
mode_or_color: "Db major triad with open fifth"
function: contrast

region_label: return
root_hz: 87.306
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 3, 7]
voicing_register: low-mid
mode_or_color: "F Aeolian minor triad, simplified for return"
function: return

## Region Schedule Per Stage
orientation:
  primary_region: home
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

induction:
  primary_region: home
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

deepening:
  primary_region: contrast
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

core_absorptive_work:
  primary_region: home
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

fractionation:
  primary_region: contrast
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

integration:
  primary_region: return
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

return:
  primary_region: return
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

## Root Motion Rules
allowed_root_motions: [minor_third-down, fifth-down]
voice_leading_constraint: "common-tone retention required across region changes"
register_transition_constraint: "root motion within low-mid register (F2 to Db2 is minor third, both in low-mid)"
forbidden_motions: [tritone-leap, parallel-octaves]