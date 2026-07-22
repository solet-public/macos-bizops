# Composition Sketch Packet 09: Spatial Density IR Arc
PACKET ID: cskp-03n8zqkx1w9-09-spatial-density-ir-arc
SKETCH ID: 03n8zqkx1w9
MANIFEST ID: wmf-neuro-ambient-composition-03-001
PASS NUMBER: 09

## Width Arc Per Stage
orientation:
  width_value: 0.4
  width_band: narrow
  width_rationale: "intimate grounding for arrival"
  width_transition_in_seconds: 0
induction:
  width_value: 0.5
  width_band: mid
  width_rationale: "gradual widening for attention narrowing"
  width_transition_in_seconds: 30
deepening:
  width_value: 0.4
  width_band: narrow
  width_rationale: "deepening absorption requires focused intimacy"
  width_transition_in_seconds: 30
core_absorptive_work:
  width_value: 0.4
  width_band: narrow
  width_rationale: "sustained deep absorption maintains focused intimacy"
  width_transition_in_seconds: 30
fractionation:
  width_value: 0.6
  width_band: mid
  width_rationale: "brief lightening opens space for gentle reorientation"
  width_transition_in_seconds: 30
integration:
  width_value: 0.8
  width_band: wide
  width_rationale: "release requires expansive, open space for gentle return"
  width_transition_in_seconds: 30
return:
  width_value: 0.8
  width_band: wide
  width_rationale: "calm trust maintained through open, unbroken space"
  width_transition_in_seconds: 30
Width arc audit: narrows for absorption (deepening/core), widens for release (fractionation/integration/return) per felt-state arc.

## Layer Placement — Stage 1: orientation
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "anchor stability requires centered position"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed anchors tonal foundation"
motif:
  pan: 0.0
  placement_strategy: center
  rationale: "carrier cell requires centered attention focus"

## Layer Placement — Stage 2: induction
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "anchor stability maintained for attention narrowing"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed continues tonal foundation"
motif:
  pan: 0.0
  placement_strategy: center
  rationale: "carrier cell maintains centered attention"

## Layer Placement — Stage 3: deepening
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "anchor stability for deep absorption"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed maintains tonal foundation"
air:
  pan: 0.0
  placement_strategy: center
  rationale: "air texture spreads within narrow width"
motif:
  pan: 0.0
  placement_strategy: center
  rationale: "carrier cell remains centered for stability"

## Layer Placement — Stage 4: core_absorptive_work
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "anchor stability for sustained absorption"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed maintains tonal foundation"
air:
  pan: 0.0
  placement_strategy: center
  rationale: "air texture spreads within narrow width"
motif:
  pan: 0.0
  placement_strategy: center
  rationale: "carrier cell foreground statement centered"

## Layer Placement — Stage 5: fractionation
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "anchor stability maintained through lightening"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed continues tonal foundation"
air:
  pan: 0.0
  placement_strategy: center
  rationale: "air texture spreads within mid width"
motif:
  pan: 0.0
  placement_strategy: center
  rationale: "carrier cell remains centered for re-deepening"

## Layer Placement — Stage 6: integration
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "anchor stability for gentle release"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed maintains tonal foundation"
air:
  pan: 0.0
  placement_strategy: center
  rationale: "air texture spreads within wide width"
motif:
  pan: 0.0
  placement_strategy: center
  rationale: "carrier cell remains centered for stabilization"

## Layer Placement — Stage 7: return
substrate:
  pan: 0.0
  placement_strategy: center
  rationale: "anchor stability for calm return"
harmonic_bed:
  pan: 0.0
  placement_strategy: center
  rationale: "harmonic bed maintains tonal foundation"
air:
  pan: 0.0
  placement_strategy: center
  rationale: "air texture spreads within wide width"

## Density Arc Per Stage
orientation:
  active_layer_count: 3
  density_band: moderate
  event_density_per_minute: 1
  density_rationale: "sparse event density for initial grounding"
  silence_treatment: breath-between-statements
induction:
  active_layer_count: 3
  density_band: moderate
  event_density_per_minute: 1
  density_rationale: "moderate event density to guide attention narrowing"
  silence_treatment: breath-between-statements
deepening:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 1
  density_rationale: "low event density for stability and absorption"
  silence_treatment: breath-between-statements
core_absorptive_work:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 0
  density_rationale: "minimal events to sustain deep absorption without distraction"
  silence_treatment: breath-between-statements
fractionation:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 3
  density_rationale: "increased event density for gentle lightening and re-deepening signals"
  silence_treatment: breath-between-statements
integration:
  active_layer_count: 4
  density_band: moderate
  event_density_per_minute: 1
  density_rationale: "moderate event density to stabilize depth during release"
  silence_treatment: breath-between-statements
return:
  active_layer_count: 3
  density_band: moderate
  event_density_per_minute: 0
  density_rationale: "no events for calm, unbroken return"
  silence_treatment: continuous

## Impulse Response Selection Per Stage
orientation:
  ir_name: maes_howe
  ir_wet: 0.25
  ir_decay_perception: short
  ir_color: neutral
  ir_rationale: "intimate, short reverb for grounding"
  ir_transition_from_previous_stage: none
induction:
  ir_name: maes_howe
  ir_wet: 0.25
  ir_decay_perception: short
  ir_color: neutral
  ir_rationale: "consistent intimate reverb to maintain focus"
  ir_transition_from_previous_stage: retain
deepening:
  ir_name: lady_chapel_st_albans
  ir_wet: 0.25
  ir_decay_perception: short
  ir_color: neutral
  ir_rationale: "intimate, short reverb for absorption"
  ir_transition_from_previous_stage: crossfade
core_absorptive_work:
  ir_name: lady_chapel_st_albans
  ir_wet: 0.25
  ir_decay_perception: short
  ir_color: neutral
  ir_rationale: "consistent intimate reverb to sustain depth"
  ir_transition_from_previous_stage: retain
fractionation:
  ir_name: york_minster
  ir_wet: 0.35
  ir_decay_perception: long
  ir_color: sacred
  ir_rationale: "longer, sacred reverb for lightening and openness"
  ir_transition_from_previous_stage: crossfade
integration:
  ir_name: york_minster
  ir_wet: 0.40
  ir_decay_perception: long
  ir_color: sacred
  ir_rationale: "sacred reverb to support gentle release"
  ir_transition_from_previous_stage: retain
return:
  ir_name: york_minster
  ir_wet: 0.40
  ir_decay_perception: long
  ir_color: sacred
  ir_rationale: "sacred reverb for calm, open return"
  ir_transition_from_previous_stage: retain