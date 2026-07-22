# Composition Sketch Packet 04: Harmonic Architecture
PACKET ID: cskp-03n8zqkx1w9-04-harmonic-architecture
SKETCH ID: 03n8zqkx1w9
MANIFEST ID: wmf-neuro-ambient-composition-03-001
PASS NUMBER: 04

## Tonal Center
tonal_center_note: F2
tonal_center_hz: 87.307
tuning_reference: A4=440
tonal_center_rationale: "F2 is the tonal center for all harmonic regions, providing a warm low-mid anchor that supports hypnotic continuity and matches the carrier cell's descending fifth from F2 to C2"

## Harmonic Regions
region_label: home
root_hz: 87.307
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 4, 7, 9]
voicing_register: low-mid
mode_or_color: "F major with added 9th (open voicing)"
function: home

region_label: contrast
root_hz: 69.300
root_note: Db2
relationship_to_tonal_center: fifth-down
intervals_semitones: [0, 4, 7]
voicing_register: low-mid
mode_or_color: "Db major with common-tone F retained"
function: contrast

region_label: return
root_hz: 87.307
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 4, 7]
voicing_register: low-mid
mode_or_color: "F major with simplified voicing"
function: return

## Region Schedule Per Stage
orientation:
  primary_region: home
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: "verified"

induction:
  primary_region: home
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: "verified"

deepening:
  primary_region: home
  secondary_region: contrast
  region_change_within_stage: gradual
  cell_compatibility_check: "verified"

core_absorptive_work:
  primary_region: contrast
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: "verified"

fractionation:
  primary_region: home
  secondary_region: contrast
  region_change_within_stage: gradual
  cell_compatibility_check: "verified"

integration:
  primary_region: return
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: "verified"

return:
  primary_region: return
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: "verified"

## Root Motion Rules
allowed_root_motions: [fifth-down, fifth-up, semitone-step]
voice_leading_constraint: "common-tone retention required across region changes"
register_transition_constraint: "register transitions must occur within the low-mid to mid range, avoiding abrupt shifts above C4"
forbidden_motions: [tritone-leap, parallel-octaves]