# Composition Sketch Packet 04: Harmonic Architecture
PACKET ID: cskp-na3-04-harmonic-architecture
SKETCH ID: na3
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 04

## Tonal Center
tonal_center_note: F2
tonal_center_hz: 87.307
tuning_reference: A4=440
tonal_center_rationale: "F2 anchors the warm, dark sonic identity per style family; matches carrier cell's descending range (F→D#) and supports hypnotic depth trajectory through low-mid resonance."

## Harmonic Regions
region_label: home
root_hz: 73.416
root_note: D#2
relationship_to_tonal_center: third-minor-down
intervals_semitones: [0, 3, 7, 10]
voicing_register: low-mid
mode_or_color: "Minor 7th open shell with root and minor third in low register, fifth omitted for warmth"
function: home

region_label: contrast
root_hz: 87.307
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 3, 7, 10]
voicing_register: low-mid
mode_or_color: "Minor 7th open shell with root and minor third in low register, fifth omitted for warmth"
function: contrast

region_label: return
root_hz: 87.307
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 4, 7, 11]
voicing_register: low-mid
mode_or_color: "Major 7th open shell with root and major third in low register, fifth omitted for warmth"
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
  region_change_within_stage: gradual
  cell_compatibility_check: "D# is passing tone above C (fifth of F minor 7th); upper neighbor to fifth"

core_absorptive_work:
  primary_region: contrast
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: "D# is passing tone above C (fifth of F minor 7th); upper neighbor to fifth"

fractionation_pocket:
  primary_region: return
  secondary_region: null
  region_change_within_stage: abrupt
  cell_compatibility_check: "D# is upper neighbor to C (minor seventh of F major 7th); passing tone"

integration:
  primary_region: return
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: "D# is upper neighbor to C (minor seventh of F major 7th); passing tone"

return:
  primary_region: return
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: "D# is upper neighbor to C (minor seventh of F major 7th); passing tone"

## Root Motion Rules
allowed_root_motions: [third-minor]
voice_leading_constraint: "Common tone C retained between home→contrast; moving voices stepwise (A→Ab)"
register_transition_constraint: "All roots within low-mid register band (73-87 Hz) to maintain harmonic continuity"
forbidden_motions: [tritone-leap, parallel-octaves]