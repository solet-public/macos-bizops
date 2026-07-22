# Composition Sketch Packet 03: Material Carrier Anchor
PACKET ID: cskp-03n8zqkx1w9-03-material-carrier-anchor
SKETCH ID: 03n8zqkx1w9
MANIFEST ID: wmf-neuro-ambient-composition-03-001
PASS NUMBER: 03

## Carrier Cell
carrier_cell_name: falling_settling_triple
shape_description: A three-note falling-settling cell with descending fifth followed by ascending second, played at 1.0s per note for total duration of 3.0 seconds with 0.75s breath after.
note_count: 3
intervals_semitones: [-7, +2]
duration_per_note_s: 1.0
total_cell_duration_s: 3.0
breath_after_cell_s: 0.75

## Carrier Behavior Across Stages
orientation:
  presence: background
  statement_count: 2
  inter_statement_gap_s: 60
  transformation: none
  mapped_anchor_beats: [anchor_first_pairing]
induction:
  presence: background
  statement_count: 3
  inter_statement_gap_s: 100
  transformation: none
  mapped_anchor_beats: [anchor_induction_threshold]
deepening:
  presence: background
  statement_count: 2
  inter_statement_gap_s: 150
  transformation: augmented
  mapped_anchor_beats: [anchor_deepening_stability]
core_absorptive_work:
  presence: silhouette
  statement_count: 1
  inter_statement_gap_s: null
  transformation: fragmented
  mapped_anchor_beats: [anchor_core_reassurance]
fractionation:
  presence: background
  statement_count: 2
  inter_statement_gap_s: 30
  transformation: diminished
  mapped_anchor_beats: [anchor_fractionation_redeepening]
integration:
  presence: returned
  statement_count: 1
  inter_statement_gap_s: null
  transformation: diminished
  mapped_anchor_beats: [anchor_integration]
return:
  presence: absent
  statement_count: 0
  inter_statement_gap_s: null
  transformation: none
  mapped_anchor_beats: []

## Anchor Identity
primary_anchor: "the three-note falling-settling cell in the low-mid register"
anchor_register: low-mid
anchor_evidence_pair:
  - timbre_signature: "warm reed-like MIDI pad with soft attack and long release"
  - rhythm_signature: "breath-length phrase arcs of 1.0s per note with 0.75s silence after"
silence_signature: "0.75s breath after each cell statement"

## Reference Voice Cadence Profile
reference_voice_cadence_profile: "04_reference_profiles/reference_voice_cadence_profile_v1.md"
mean_phrase_length_s: 1.55
mean_pause_length_s: 0.75