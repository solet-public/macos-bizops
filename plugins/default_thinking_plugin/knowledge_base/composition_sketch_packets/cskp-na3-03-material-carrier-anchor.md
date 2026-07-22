# Composition Sketch Packet 03: Material Carrier Anchor
PACKET ID: cskp-na3-03-material-carrier-anchor
SKETCH ID: na3
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 03

## Carrier Cell
carrier_cell_name: descending_third_second
shape_description: "Three-note cell with minor third descent followed by major second descent, each note lasting 0.67 seconds for total duration of 2.0 seconds."
note_count: 3
intervals_semitones: [-4, -2]
duration_per_note_s: 0.67
total_cell_duration_s: 2.0
breath_after_cell_s: 0.75

## Carrier Behavior Across Stages
orientation:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: [orientation_carrier_pairing]
induction:
  presence: background
  statement_count: 3
  inter_statement_gap_s: 1.0
  transformation: none
  mapped_anchor_beats: [induction_attention_narrowed]
deepening:
  presence: background
  statement_count: 2
  inter_statement_gap_s: 3.0
  transformation: augment
  mapped_anchor_beats: [deepening_depth_stable]
core_absorptive_work:
  presence: absent
  statement_count: 0
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: []
fractionation_pocket:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: [fractionation_brightness_closed]
integration:
  presence: background
  statement_count: 2
  inter_statement_gap_s: 1.5
  transformation: none
  mapped_anchor_beats: [integration_final_carrier_return]
return:
  presence: background
  statement_count: 1
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: []

## Anchor Identity
primary_anchor: "three-note descending cell at orientation carrier pairing and key depth thresholds"
anchor_register: low-mid
anchor_evidence_pair:
  - timbre_signature: "warm breathy reed-like pad with soft attack and long release"
  - rhythm_signature: "uniform 0.67s note durations with 0.75s permissive breath"
secondary_anchor: null
silence_signature: "permissive 0.75s breath after each cell statement"

## Reference Voice Cadence Profile
reference_voice_cadence_profile: reference_voice_cadence_profile_v1.md
mean_phrase_length_s: 1.55
mean_pause_length_s: 0.75
phrase_rate_phrases_per_minute: 26