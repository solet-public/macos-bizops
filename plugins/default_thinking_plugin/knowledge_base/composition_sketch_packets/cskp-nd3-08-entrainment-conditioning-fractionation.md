# Composition Sketch Packet 08: Entrainment Conditioning Fractionation
PACKET ID: cskp-nd3-08-entrainment-conditioning-fractionation
SKETCH ID: nd3
MANIFEST ID: wmf-neuro-ambient-nd3-001
PASS NUMBER: 08

## Entrainment Mechanism
mechanism: modulated_drone
mechanism_rationale: "Integrated amplitude modulation on substrate drone layer provides non-intrusive entrainment that works across all delivery formats without requiring headphones or creating alerting cues."
target_layer: substrate
listener_stance_match: verified
delivery_format_match: verified

## Target Bands Per Stage
orientation:
  target_band: alpha_mid
  beat_hz: 8.0
  carrier_hz: null
  bandwidth_audit: verified
  felt_state_alignment: verified

induction:
  target_band: alpha_mid
  beat_hz: 8.0
  carrier_hz: null
  bandwidth_audit: verified
  felt_state_alignment: verified

deepening:
  target_band: low_theta
  beat_hz: 4.5
  carrier_hz: null
  bandwidth_audit: verified
  felt_state_alignment: verified

core_absorptive_work:
  target_band: low_theta
  beat_hz: 4.5
  carrier_hz: null
  bandwidth_audit: verified
  felt_state_alignment: verified

fractionation_pocket:
  target_band: low_theta
  beat_hz: 4.5
  carrier_hz: null
  bandwidth_audit: verified
  felt_state_alignment: verified

integration:
  target_band: alpha_mid
  beat_hz: 8.0
  carrier_hz: null
  bandwidth_audit: verified
  felt_state_alignment: verified

return:
  target_band: none
  beat_hz: null
  carrier_hz: null
  bandwidth_audit: verified
  felt_state_alignment: verified

## Conditioning And Fractionation Events
events:
  - event_name: conditioning_deepening
    event_kind: conditioning_anchor
    occurs_within_stage: induction
    occurs_at_offset_seconds: 200
    duration_seconds: 120
    mechanism_for_event: "tremolo rate_hz transitions from 8.0 Hz to 4.5 Hz over 120 seconds"
    aligned_anchor_beat: "deepening_start"
    target_outcome: "listener begins to experience deeper trance as entrainment shifts from alpha to theta"

  - event_name: fractionation_descent
    event_kind: fractionation_descent
    occurs_within_stage: core_absorptive_work
    occurs_at_offset_seconds: 360
    duration_seconds: 45
    mechanism_for_event: "tremolo rate_hz temporarily increases to 6.0 Hz for 45 seconds"
    aligned_anchor_beat: "fractionation_seed"
    target_outcome: "listener experiences gentle lightening of depth through micro-softening of carrier and spatial bloom"

  - event_name: fractionation_redeepening
    event_kind: fractionation_return
    occurs_within_stage: fractionation_pocket
    occurs_at_offset_seconds: 60
    duration_seconds: 30
    mechanism_for_event: "tremolo rate_hz decreases from 6.0 Hz back to 4.5 Hz over 30 seconds"
    aligned_anchor_beat: "fractionation_redeepening"
    target_outcome: "listener returns to deeper absorption with settled re-deepening"

  - event_name: conditioning_integration
    event_kind: conditioning_anchor
    occurs_within_stage: integration
    occurs_at_offset_seconds: 60
    duration_seconds: 30
    mechanism_for_event: "tremolo rate_hz transitions from 4.5 Hz to 8.0 Hz over 30 seconds"
    aligned_anchor_beat: "integration_end"
    target_outcome: "listener begins emergence as entrainment shifts from theta to alpha"

Event count audit: 4 events (2 conditioning, 1 fractionation_descent, 1 fractionation_return)

## Spacing And Anti-Habituation Audit
mean_event_inter_arrival_seconds: 313.3
minimum_event_inter_arrival_seconds: 120
event_kind_distribution: [("conditioning_anchor", 2), ("fractionation_descent", 1), ("fractionation_return", 1)]
habituation_risk: low
treatment_plan: none required