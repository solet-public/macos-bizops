# Composition Sketch Packet 03: Material Carrier Anchor

PACKET ID: cskp-004-03-material-carrier-anchor
SKETCH ID: 004
MANIFEST ID: wmf-neuro-ambient-composition-004
PASS NUMBER: 03

## Carrier Cell
carrier_cell_name: downward_settling_two_note
shape_description: "Two-note cell with descending minor third interval, each note held for 1.0 seconds in low-mid register, followed by 1.0s breath"
note_count: 2
intervals_semitones: [-3]
duration_per_note_s: 1.0
total_cell_duration_s: 2.0
breath_after_cell_s: 1.0

## Carrier Behavior Across Stages
orientation_settling:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: []

induction_narrowing:
  presence: foreground
  statement_count: 3
  inter_statement_gap_s: 60
  transformation: none
  mapped_anchor_beats: [first_induction_anchor]

deepening:
  presence: background
  statement_count: 2
  inter_statement_gap_s: 180
  transformation: none
  mapped_anchor_beats: [deepening_anchor]

core_absorptive_work:
  presence: silhouette
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: [core_absorption_anchor]

fractionation_pocket:
  presence: foreground
  statement_count: 2
  inter_statement_gap_s: 30
  transformation: none
  mapped_anchor_beats: [fractionation_lightening_anchor, fractionation_recovery_anchor]

integration:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: [integration_return_anchor]

return_reorientation:
  presence: returned
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: []

## Anchor Identity
primary_anchor: "the two-note downward-settling cell with warmth bloom at release point"
anchor_register: low-mid
anchor_evidence_pair:
  - timbre_signature: "warm, slightly dark reed-like texture with soft attack"
  - rhythm_signature: "two-note cell (1.0s per note) with 1.0s breath"
secondary_anchor: "warmth bloom as conditioned marker for return and deepening"
silence_signature: "1.0s breath after cell with subtle warmth bloom at release"

## Reference Voice Cadence Profile
reference_voice_cadence_profile: reference_voice_cadence_profile_v1.md
mean_phrase_length_s: 1.55
mean_pause_length_s: 0.75
phrase_rate_phrases_per_minute: 38