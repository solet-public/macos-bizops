# Composition Sketch Packet 08: Entrainment Conditioning Fractionation
PACKET ID: cskp-na3-08-entrainment-conditioning-fractionation
SKETCH ID: na3
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 08

## Entrainment Mechanism
mechanism: tremolo
mechanism_rationale: "Integrated into harmonic_bed layer to avoid separate sublayer and work on all playback formats; aligns with non-percussive identity per Pass 01 hard exclusions."
target_layer: harmonic_bed
listener_stance_match: verified
delivery_format_match: verified

## Target Bands Per Stage
orientation:
  target_band: alpha_mid
  beat_hz: 10.0
  carrier_hz: 73.416
  bandwidth_audit: verified
  felt_state_alignment: verified
induction:
  target_band: theta_mid
  beat_hz: 6.0
  carrier_hz: 73.416
  bandwidth_audit: verified
  felt_state_alignment: verified
deepening:
  target_band: theta_low
  beat_hz: 4.5
  carrier_hz: 87.307
  bandwidth_audit: verified
  felt_state_alignment: verified
core_absorptive_work:
  target_band: theta_low
  beat_hz: 4.5
  carrier_hz: 87.307
  bandwidth_audit: verified
  felt_state_alignment: verified
fractionation_pocket:
  target_band: theta_low
  beat_hz: 4.5
  carrier_hz: 87.307
  bandwidth_audit: verified
  felt_state_alignment: verified
integration:
  target_band: alpha_mid
  beat_hz: 10.0
  carrier_hz: 87.307
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
  - event_name: induction_attention_narrowed_anchor
    event_kind: conditioning_anchor
    occurs_within_stage: induction
    occurs_at_offset_seconds: 60
    duration_seconds: 1
    mechanism_for_event: "tremolo depth increased from 0.05 to 0.08 over 1 second, carrier_hz at D#2 (73.416 Hz) to mark attention narrowing"
    aligned_anchor_beat: induction_attention_narrowed
    target_outcome: "listener's attention narrows as intended, marking the start of deepening"
  - event_name: fractionation_descent
    event_kind: fractionation_descent
    occurs_within_stage: fractionation_pocket
    occurs_at_offset_seconds: 30
    duration_seconds: 60
    mechanism_for_event: "tremolo depth reduced from 0.08 to 0.03 over 60 seconds, carrier_hz remains at F2 (87.307 Hz)"
    aligned_anchor_beat: fractionation_brightness_closed
    target_outcome: "listener experiences gentle lightening without breaking absorption"
  - event_name: fractionation_return
    event_kind: fractionation_return
    occurs_within_stage: fractionation_pocket
    occurs_at_offset_seconds: 90
    duration_seconds: 60
    mechanism_for_event: "tremolo depth increased from 0.03 to 0.08 over 60 seconds, carrier_hz remains at F2 (87.307 Hz)"
    aligned_anchor_beat: fractionation_brightness_closed
    target_outcome: "listener returns to deeper absorption with a sense of inevitability"
count_audit: 3 events (1 conditioning, 2 fractionation)

## Spacing And Anti-Habituation Audit
mean_event_inter_arrival_seconds: 525
minimum_event_inter_arrival_seconds: 60
event_kind_distribution: [("conditioning_anchor", 1), ("fractionation_descent", 1), ("fractionation_return", 1)]
habituation_risk: low
treatment_plan: none required