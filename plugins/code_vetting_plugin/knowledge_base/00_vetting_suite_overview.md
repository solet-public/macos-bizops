# AI Code-Vetting Suite — Overview: Layers, Run-Profiles, and Honest Coverage

Article Layer: 2
Article Role: capability_reference
Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:code-vetting, consumer_profile:both

Embedding Description: The orientation article for the AI code-vetting suite — what the whole thing is and how its pieces fit. Covers the three-layer architecture (L1 deterministic scanners emitting structured findings, L2 AI critic lenses reviewing through single perspectives, L3 adversarial verification re-stamping candidates confirmed or refuted), the run-profile model (one engine parameterized by target, rulebook stack, stakes profile, substrate, and scope, so self-vet versus foreign-vet is a target property and not a code fork), the honest-coverage posture (a scanner that cannot run records a gap rather than silently passing, and coverage is evidence never a percentage gate), the target-class derivation (self versus foreign derived from the target, driving which lenses apply and whether findings calibrate or verdict), the deterministic L1 scanner roster and which dimensions each feeds, and the evidence-payload channel by which L1 hands the AI layer targeting input such as test-reach, candidate dead symbols, literal frequency, and structural metrics.

**When you need this**: orienting on the vetting suite before touching any one part; deciding whether a new check belongs at L1, L2, or L3; understanding why self-vet and foreign-vet share one engine; learning how a scanner that could not run is reported; finding which L1 evidence payload an AI lens consumes as targeting input.

The suite answers one question for a codebase, self or foreign: what should a careful reviewer flag, and how confident are we. It is the install-a-quality-tool-not-a-homunculus adoption wedge — local, zero-marginal-cost because it orchestrates the operator's already-paid coding-agent subscriptions and free open-source scanners rather than a metered API key, and honest about its own coverage.

## The three layers

L1 is deterministic. External tools and platform gates are wrapped into one structured finding schema — leaked-secret detection, static application security analysis, vulnerable-dependency and license checks, the platform quality gates, an identity, PII, and network-bind regex battery, hidden-unicode, duplication, dead-code, orphan detection, and structural metrics. L1 runs synchronously and fast, and a tool that is absent records a coverage gap rather than passing silently. L2 is inference: single-perspective AI critic lenses (architecture, security, correctness, ai_slop, test_adequacy, kb_doc_fidelity) review the target and emit candidate findings, each naming the rule it breaks. L3 is adversarial verification: perspective-diverse skeptics try to REFUTE each L2 candidate and re-stamp it confirmed or refuted, so a plausible-but-wrong finding does not survive.

## One engine, many run-profiles

There is a single engine parameterized by a run-profile — target, rulebook stack, stakes profile, substrate, and scope. Self-vet and foreign-vet are the same engine with a different target and rulebook tier, never a forked codebase. The scope (whole-tree, diff-scoped, subsystem) and the coverage measurement (opt-in, on-demand) are further run-profile axes. Vetting is advisory to the commit path and never a second gate — the platform's own Git-Controller gate run remains the sole commit authority.

## Target-class derivation and the calibrate posture

Self versus foreign is derived from the target, not passed as a flag. The target class drives which lenses apply — a foreign run skips the two platform-local lenses, recording a coverage row for each skip rather than reframing them, which protects the per-lens survival metric — and whether findings read as a verdict or a calibrated observation. On a foreign tree the suite calibrates: it observes with confidence, it does not grade someone else's engineering.

## L1 evidence payloads feed the AI layer

Some L1 output is not a finding but targeting evidence for L2. The deterministic dead-code scanner emits only its provable classes as findings — the live report renders `Verdict: confirmed (L1_deterministic)` on unreachable code and unused imports — while its Candidate Dead Symbols section carries the explicit framing that it is evidence for the AI-critic layer, NOT findings. The repeated-literals table renders inside Structural Quality Metrics as the same kind of evidence, alongside the structural-complexity distributions and the test-reach data. Each payload is persisted with the findings and handed to the relevant lens as input, so the lens names the specific untested behavior, the specific magic string, or the specific dead symbol — using the L1 evidence to aim, never re-deriving it.
