# Composition Sketch Packet 03: Material Carrier Anchor
PACKET ID: cskp-003-03-material-carrier-anchor
SKETCH ID: 003
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 03

## Carrier Cell
carrier_cell_name: falling_three_note
shape_description: A three-note descending contour with a minor third followed by a minor second, creating gentle settling motion. Each note held for 0.5 seconds with soft attack and long release, followed by breath.
note_count: 3
intervals_semitones: [-4, -1]
duration_per_note_s: 0.5
total_cell_duration_s: 1.5
breath_after_cell_s: 0.75

## Carrier Behavior Across Stages
orientation:
  presence: foreground
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: [first_anchor_pairing]
induction:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: [induction_threshold]
deepening:
  presence: silhouette
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: [deepening_stability]
core_absorptive_work:
  presence: silhouette
  statement_count: 1
  inter_statement_gap_s: null
  transformation: fragmented
  mapped_anchor_beats: [core_reassurance]
fractionation:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: [fractionation_redeepening]
integration:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: [integration_stabilization]
return:
  presence: returned
  statement_count: 1
  inter_statement_gap_s: null
  transformation: diminished
  mapped_anchor_beats: []

## Anchor Identity
primary_anchor: "tiny low-mid warmth bloom at the end of a carrier phrase"
anchor_register: low-mid
anchor_evidence_pair:
  - timbre_signature: "warm, soft-edged bloom with slow attack and release"
  - rhythm_signature: "brief pulse at the end of a carrier phrase, aligned with breath after"
silence_signature: "0.5-second silence after bloom to allow settling"

## Reference Voice Cadence Profile
reference_voice_cadence_profile: reference_voice_cadence_profile_v1.md
mean_phrase_length_s: 1.55
mean_pause_length_s: 0.75
phrase_rate_phrases_per_minute: 26.09