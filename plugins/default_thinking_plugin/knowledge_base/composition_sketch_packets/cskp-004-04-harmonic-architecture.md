# Composition Sketch Packet 04: Harmonic Architecture

PACKET ID: cskp-004-04-harmonic-architecture
SKETCH ID: 004
MANIFEST ID: wmf-neuro-ambient-composition-004
PASS NUMBER: 04

## Tonal Center
tonal_center_note: F2
tonal_center_hz: 87.307
tuning_reference: A4=440
tonal_center_rationale: "Carrier cell descends from F2; A=440 standard tuning for low-mid warmth and resonance in neuro-ambient context."

## Harmonic Regions
region_label: home
root_hz: 87.307
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 3, 7, 10]
voicing_register: low-mid
mode_or_color: "minor 7th open shell with minor third and seventh"
function: home

region_label: contrast
root_hz: 87.307
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 2, 3, 7]
voicing_register: low-mid
mode_or_color: "minor triad with added major second for open contrast"
function: contrast

region_label: fractionation
root_hz: 87.307
root_note: F2
relationship_to_tonal_center: identity
intervals_semitones: [0, 3, 7, 10]
voicing_register: low-mid
mode_or_color: "minor 7th with microtonal beating for spectral thickening"
function: fractionation-color

## Region Schedule Per Stage
orientation_settling:
  primary_region: home
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

induction_narrowing:
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
  primary_region: home
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

fractionation_pocket:
  primary_region: fractionation
  secondary_region: null
  region_change_within_stage: gradual (with microtonal detuning)
  cell_compatibility_check: verified

integration:
  primary_region: home
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

return_reorientation:
  primary_region: home
  secondary_region: null
  region_change_within_stage: none
  cell_compatibility_check: verified

## Root Motion Rules
allowed_root_motions: [identity]
voice_leading_constraint: "common-tone retention required across region changes; e.g., when shifting from home to contrast, retain F and C (root and fifth) as common tones"
register_transition_constraint: "all harmonic region changes occur within the low-mid register (F2 to F3) without shifting octaves"
forbidden_motions: [fifth-down, fifth-up, semitone-step, third-step, free]