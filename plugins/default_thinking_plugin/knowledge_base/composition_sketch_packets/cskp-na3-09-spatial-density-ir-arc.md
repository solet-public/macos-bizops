# Composition Sketch Packet 09: Spatial Density IR Arc
PACKET ID: cskp-na3-09-spatial-density-ir-arc
SKETCH ID: na3
MANIFEST ID: wmf-neuro-ambient-composition-003
PASS NUMBER: 09

## Width Arc Per Stage
orientation:
  width_value: 0.4
  width_band: narrow
  width_rationale: "intimate containment for safety and settling"
  width_transition_in_seconds: 20
induction:
  width_value: 0.5
  width_band: mid-narrow
  width_rationale: "gradual widening as attention narrows inward"
  width_transition_in_seconds: 20
deepening:
  width_value: 0.2
  width_band: very-narrow
  width_rationale: "deepening into absorption requires minimal spatial distraction"
  width_transition_in_seconds: 20
core_absorptive_work:
  width_value: 0.15
  width_band: extremely-narrow
  width_rationale: "deepest absorption requires near-monophonic focus"
  width_transition_in_seconds: 20
fractionation_pocket:
  width_value: 0.6
  width_band: mid-wide
  width_rationale: "brief lightening requires gentle widening for openness"
  width_transition_in_seconds: 20
integration:
  width_value: 0.4
  width_band: narrow
  width_rationale: "integration requires return to calm containment"
  width_transition_in_seconds: 20
return:
  width_value: 0.3
  width_band: narrow
  width_rationale: "return to baseline requires gentle narrowing"
  width_transition_in_seconds: 20
Width arc audit: Respects felt-state trajectory (narrow → wide → narrow) with transitions matching stage duration ratios.

## Layer Placement — Stage 1: orientation
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "foundation layer requires stable central presence"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed anchors tonal identity at center"
entrainment:
  pan: 0.1
  placement_strategy: left-of-center
  rationale: "carrier establishes subtle directional cue for attention"
air:
  pan: 0.0
  placement_strategy: mid-side-spread
  rationale: "air texture requires diffuse edge spread for spatial openness"

## Layer Placement — Stage 2: induction
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "foundation layer maintains central stability"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed continues tonal anchoring"
entrainment:
  pan: -0.1
  placement_strategy: right-of-center
  rationale: "carrier shifts slightly to create subtle directional movement"
air:
  pan: 0.0
  placement_strategy: mid-side-spread
  rationale: "air texture maintains diffuse spatial presence"

## Layer Placement — Stage 3: deepening
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "foundation layer remains central for absorption"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed maintains tonal stability"
entrainment:
  pan: 0.1
  placement_strategy: left-of-center
  rationale: "carrier establishes subtle directional anchor for deepening"
air:
  pan: 0.0
  placement_strategy: mid-side-spread
  rationale: "air texture provides spatial depth without distraction"

## Layer Placement — Stage 4: core_absorptive_work
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "foundation layer remains central for deep absorption"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed provides stable absorption field"
air:
  pan: 0.0
  placement_strategy: mid-side-spread
  rationale: "air texture maintains subtle spatial presence without distraction"

## Layer Placement — Stage 5: fractionation_pocket
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "foundation layer maintains central stability"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed provides harmonic continuity"
entrainment:
  pan: -0.1
  placement_strategy: right-of-center
  rationale: "carrier establishes directional cue for lightening"
air:
  pan: 0.0
  placement_strategy: mid-side-spread
  rationale: "air texture widens to support lightening effect"

## Layer Placement — Stage 6: integration
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "foundation layer returns to central stability"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed provides harmonic continuity"
entrainment:
  pan: 0.1
  placement_strategy: left-of-center
  rationale: "carrier establishes subtle directional cue for integration"
air:
  pan: 0.0
  placement_strategy: mid-side-spread
  rationale: "air texture maintains spatial openness for calm"

## Layer Placement — Stage 7: return
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "foundation layer maintains central presence for return"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed provides harmonic continuity"
entrainment:
  pan: -0.1
  placement_strategy: right-of-center
  rationale: "carrier establishes subtle directional cue for dissolution"
air:
  pan: 0.0
  placement_strategy: mid-side-spread
  rationale: "air texture maintains spatial openness for return"

## Density Arc Per Stage
orientation:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 0
  density_rationale: "sparse layer count establishes foundational texture"
  silence_treatment: none
induction:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 1
  density_rationale: "moderate layer count with breath between carrier statements"
  silence_treatment: breath-between-statements
deepening:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 0
  density_rationale: "stable layer count for deepening absorption"
  silence_treatment: breath-between-statements
core_absorptive_work:
  active_layer_count: 3
  density_band: sparse
  event_density_per_minute: 0
  density_rationale: "minimal layer count for deepest absorption"
  silence_treatment: continuous
fractionation_pocket:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 1
  density_rationale: "moderate layer count with lightening effect"
  silence_treatment: none
integration:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 1
  density_rationale: "moderate layer count for integration stability"
  silence_treatment: breath-between-statements
return:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 2
  density_rationale: "increased event density for dissolution cadence"
  silence_treatment: none
Density audit: Matches Pass 05 layer counts exactly. Density band aligns with felt-state arc (sparse at core, moderate elsewhere).

## Impulse Response Selection Per Stage
orientation:
  ir_name: maes_howe
  ir_wet: 0.25
  ir_decay_perception: medium
  ir_color: sacred
  ir_rationale: "intimate sacred space for settling and safety"
  ir_transition_from_previous_stage: retain
induction:
  ir_name: maes_howe
  ir_wet: 0.25
  ir_decay_perception: medium
  ir_color: sacred
  ir_rationale: "consistent intimate space for attention narrowing"
  ir_transition_from_previous_stage: retain
deepening:
  ir_name: maes_howe
  ir_wet: 0.25
  ir_decay_perception: medium
  ir_color: sacred
  ir_rationale: "stable intimate space for deepening absorption"
  ir_transition_from_previous_stage: retain
core_absorptive_work:
  ir_name: maes_howe
  ir_wet: 0.25
  ir_decay_perception: medium
  ir_color: sacred
  ir_rationale: "intimate space for deepest absorption"
  ir_transition_from_previous_stage: retain
fractionation_pocket:
  ir_name: lady_chapel_st_albans
  ir_wet: 0.35
  ir_decay_perception: long
  ir_color: sacred
  ir_rationale: "slightly more open sacred space for gentle lightening"
  ir_transition_from_previous_stage: crossfade
integration:
  ir_name: maes_howe
  ir_wet: 0.25
  ir_decay_perception: medium
  ir_color: sacred
  ir_rationale: "return to intimate space for integration stability"
  ir_transition_from_previous_stage: crossfade
return:
  ir_name: maes_howe
  ir_wet: 0.25
  ir_decay_perception: medium
  ir_color: sacred
  ir_rationale: "intimate space for return to baseline"
  ir_transition_from_previous_stage: retain
IR audit: Uses only library IRs from impulse_response_library.md. Transitions respect stage function (crossfade at fractionation boundary). Wet levels maintain mono compatibility per stereo pipeline reference.