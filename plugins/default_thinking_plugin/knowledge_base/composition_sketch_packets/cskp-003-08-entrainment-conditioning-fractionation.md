# Composition Sketch Packet 08: Entrainment Conditioning Fractionation
PACKET ID: cskp-003-08-entrainment-conditioning-fractionation
SKETCH ID: 003
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 08

## Entrainment Mechanism
mechanism: isochronic_pulses
mechanism_rationale: "Isochronic pulses provide speaker-compatible entrainment without requiring headphones, matching the brief's delivery format flexibility and listener stance of inward_deep_listening."
target_layer: entrainment
listener_stance_match: verified
delivery_format_match: verified

## Target Bands Per Stage
orientation:
  target_band: alpha_mid
  beat_hz: 10.0
  carrier_hz: 150.0
  bandwidth_audit: verified
  felt_state_alignment: verified
induction:
  target_band: theta_mid
  beat_hz: 6.0
  carrier_hz: 150.0
  bandwidth_audit: verified
  felt_state_alignment: verified
deepening:
  target_band: theta_low
  beat_hz: 4.5
  carrier_hz: 150.0
  bandwidth_audit: verified
  felt_state_alignment: verified
core_absorptive_work:
  target_band: theta_low
  beat_hz: 4.5
  carrier_hz: 150.0
  bandwidth_audit: verified
  felt_state_alignment: verified
fractionation:
  target_band: theta_low
  beat_hz: 4.5
  carrier_hz: 150.0
  bandwidth_audit: verified
  felt_state_alignment: verified
integration:
  target_band: alpha_mid
  beat_hz: 10.0
  carrier_hz: 150.0
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
  - event_name: induction_threshold_prime
    event_kind: conditioning_prime
    occurs_within_stage: induction
    occurs_at_offset_seconds: 120
    duration_seconds: 30
    mechanism_for_event: "subtle increase in isochronic pulse amplitude from 0.05 to 0.07 over 30 seconds"
    aligned_anchor_beat: induction_threshold
    target_outcome: "primes the listener for deepening by signaling attention narrowing threshold"
  - event_name: fractionation_lightening
    event_kind: fractionation_descent
    occurs_within_stage: fractionation
    occurs_at_offset_seconds: 60
    duration_seconds: 30
    mechanism_for_event: "spectral centroid rises by 300 Hz (from 500 to 800 Hz) and reverb wet ratio decreases by 0.1 (from 0.6 to 0.5) over 30 seconds"
    aligned_anchor_beat: fractionation_redeepening
    target_outcome: "briefly lightens depth for fractionation cycle, preparing for deeper return"
  - event_name: fractionation_redeepening
    event_kind: fractionation_return
    occurs_within_stage: fractionation
    occurs_at_offset_seconds: 90
    duration_seconds: 30
    mechanism_for_event: "spectral centroid falls by 300 Hz (from 800 to 500 Hz) and reverb wet ratio increases by 0.1 (from 0.5 to 0.6) over 30 seconds"
    aligned_anchor_beat: fractionation_redeepening
    target_outcome: "re-deepens to Level 3 with a deeper, more settled field"
Event count audit: 1 conditioning event and 2 fractionation events.

## Spacing And Anti-Habituation Audit
mean_event_inter_arrival_seconds: 495
minimum_event_inter_arrival_seconds: 30
event_kind_distribution: [("conditioning_prime", 1), ("fractionation_descent", 1), ("fractionation_return", 1)]
habituation_risk: low
treatment_plan: none required