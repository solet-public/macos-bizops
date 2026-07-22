# Composition Sketch Packet 03: Material Carrier Anchor
PACKET ID: cskp-nd3-03-material-carrier-anchor
SKETCH ID: nd3
MANIFEST ID: wmf-neuro-ambient-nd3-001
PASS NUMBER: 03

## Carrier Cell
carrier_cell_name: descending_minor_third_step
shape_description: "Three-note cell with minor third descent followed by step down, creating gentle settling contour. Each note held for 3 seconds with soft decay."
note_count: 3
intervals_semitones: [-3, -2]
duration_per_note_s: [3.0, 3.0, 3.0]
total_cell_duration_s: 9
breath_after_cell_s: 3

## Carrier Behavior Across Stages
orientation:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: ["first_carrier_pairing"]
induction:
  presence: background
  statement_count: 2
  inter_statement_gap_s: 12
  transformation: none
  mapped_anchor_beats: ["deepening_start"]
deepening:
  presence: background
  statement_count: 2
  inter_statement_gap_s: 15
  transformation: augmented
  mapped_anchor_beats: []
core_absorptive_work:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: transformed
  mapped_anchor_beats: []
fractionation_pocket:
  presence: background
  statement_count: 2
  inter_statement_gap_s: 10
  transformation: diminished
  mapped_anchor_beats: ["fractionation_seed"]
integration:
  presence: returned
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: ["integration_end"]
return:
  presence: absent
  statement_count: 0
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: []

## Anchor Identity
primary_anchor: "three-note descending minor third followed by step down with 3-second breath"
anchor_register: low-mid
anchor_evidence_pair:
  - timbre_signature: "warm breathy synth pad with soft attack and long release"
  - rhythm_signature: "9-second active phrase + 3-second breath = 12-second cycle"
secondary_anchor: null
silence_signature: "permissive 3-second breath after each cell creates natural breathing rhythm"

## Reference Voice Cadence Profile
reference_voice_cadence_profile: 04_reference_profiles/reference_voice_cadence_profile_v1.md
mean_phrase_length_s: 9.0
mean_pause_length_s: 3.0
phrase_rate_phrases_per_minute: 5.0