# Composition Sketch Packet 04: Harmonic Architecture
PACKET ID: cskp-nd3-04-harmonic-architecture
SKETCH ID: nd3
MANIFEST ID: wmf-neuro-ambient-nd3-001
PASS NUMBER: 04

## Tonal Center
tonal_center_note: F2
tonal_center_hz: 87.310
tuning_reference: A4=440
tonal_center_rationale: "F2 anchors the warm, slightly dark sonic identity per brief and aligns with carrier cell's starting pitch from Pass 03."

## Harmonic Regions
region_label: home
root_hz: 87.310
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 3, 7, 10]
voicing_register: low-mid
mode_or_color: "F Aeolian minor 7th with open shell"
function: home

region_label: contrast
root_hz: 87.310
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 3, 7, 10, 14]
voicing_register: low-mid
mode_or_color: "F Aeolian minor 9th for added warmth and depth"
function: contrast

region_label: return
root_hz: 87.310
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 3, 7, 10]
voicing_register: low-mid
mode_or_color: "F Aeolian minor 7th with open shell (return version)"
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
  cell_compatibility_check: verified

core_absorptive_work:
  primary_region: contrast
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

fractionation_pocket:
  primary_region: return
  secondary_region: null
  region_change_within_stage: gradual
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
  cell_compatibility_check: not_applicable

## Root Motion Rules
allowed_root_motions: [identity]
voice_leading_constraint: "Common tones retained across region changes; moving voices step by 1-2 semitones"
register_transition_constraint: "Register shifts limited to within low-mid register (F2 to F3) for continuity"
forbidden_motions: [tritone-leap, parallel-octaves]