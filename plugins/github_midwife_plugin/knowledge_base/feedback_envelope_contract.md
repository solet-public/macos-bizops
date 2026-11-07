# Feedback envelope v1: local data contract

The source JSON Schema is `.github/feedback-envelope.v1.json`. The callable is
`github_midwife_plugin.feedback_envelope.validate_feedback`. Both describe a
version 1 feedback envelope; the callable additionally checks a draft's title,
Markdown headings and externally resolved release facts. This component performs
local validation only. It performs no network request, tag resolution, Git
operation, command execution, issue intake, quota check or public write.

## Callable input and output

```python
from github_midwife_plugin.feedback_envelope import ReleaseFacts, validate_feedback

facts = ReleaseFacts(
    repository="solet-public/example-seed",
    release_tag="release-example",
    release_commit="a" * 40,
    plugins=("example_plugin",),
)
result = validate_feedback(body=draft_body, title="§60.1 — Example", facts=facts)
# result.valid; result.diagnostics; result.envelope; result.title; result.body
```

The caller must resolve the repository, real release tag, its full lowercase
40-character commit and the plugin roster **at that release** through a trusted
source. Passing values copied from the draft itself does not establish these
facts. `ReleaseFacts` rejects malformed facts with `ValueError`; it cannot prove
their provenance. No tag or repository is inferred from attacker-controlled
text. The roster is an explicit tuple, including an empty tuple for a release
without plugins. Duplicate or malformed roster names are caller errors.

`ValidationResult` contains a tuple of `Diagnostic(code, field, message)`, an
accepted envelope dictionary (or `None` on any failure), and the original title
and body. Its `valid` property means no diagnostics. Literal body and title
text, including line endings and apparent commands, is retained as data. The
record is frozen; the returned envelope dictionary is caller-owned data, not
an authorization token. Diagnostic messages can quote invalid input and must
also be handled as data. The lower-level `validate_envelope(envelope, facts)`
returns diagnostics for scalar/schema and release-fact checks only; it does
not certify a title or headings.

## Wire format

A body starts at its first character with exactly one opening line
`` ```solet-feedback `` and a closing line `` ``` ``. LF and CRLF are accepted.
The enclosed YAML is one flat mapping. All ten keys below are required, and
unknown keys, duplicate keys, aliases, anchors, explicit tags, collections and
multiple YAML documents are rejected. PyYAML's safe parser is already declared
by this package; no new runtime dependency is needed. Quoting numeric-looking
strings avoids YAML implicit scalar typing. JSON Schema integer semantics apply:
1 and 1.0 denote the same integer; booleans do not denote integers.

| Field | Version 1 rule |
| --- | --- |
| `version` | Integer 1 |
| `class` | `defect`, `question`, `feature-request`, `closure-confirmation`, `feedback-round` |
| `item` | `§N.M` with positive N and M; a feedback round uses `Part N`; at most 64 characters |
| `round` | Positive integer or explicit `null`; a parent feedback round requires `null` |
| `seed` | Exact authoritative `owner/repository`; ASCII letters, digits, underscores, dots and hyphens; at most 256 characters |
| `release_tag` | Exact authoritative tag; nonempty, no whitespace/control characters; at most 256 characters |
| `release_commit` | Exact authoritative full lowercase hexadecimal commit, 40 characters |
| `manager` | Nonblank string without CR/LF, at most 256 characters; reported version text, not independently verified |
| `surface` | `core`, `manager`, `seed`, `seed:<repository-name>`, `plugin:<name>`, or `unknown`; at most 256 characters |
| `content_gate` | Literal `passed`; a filer attestation, not an independent content/leak audit |

A `plugin:` name must be in the authoritative release roster. `seed:<key>` must
name the current repository's final path component. `unknown` is an explicit
supported proposal for later triage; it never establishes plugin membership.
A title must start with the exact `item`, followed by end-of-title or a space,
tab, dash, colon or em dash. Thus `§60.10` does not match `§60.1`.

The complete body's UTF-8 limit is 65,536 bytes; the title limit is 1,024 bytes.
The callable checks size before parsing YAML and reports invalid UTF-8 strings.
Schema lengths are Unicode character counts for scalar fields. Schema validation
alone cannot establish raw YAML uniqueness, byte size, headings, title or facts.
The smoke carries shared structural parity fixtures plus separate contextual
fixtures; it exercises every class using headings read from the current forms.

## Required Markdown headings

Use level 2 through 6 ATX headings after the envelope. Each required heading
must occur exactly once and contain non-whitespace text other than the form's
`_No response_` sentinel. Heading-looking lines inside code fences do not create
sections. Optional headings remain allowed. Required labels follow the current
forms; these are the complete class lists:

| Class | Required headings |
| --- | --- |
| defect | Item number; The exact command or action; Observed output; Expected behavior; Release measured against; Content gate |
| question | Item number; The question; What you are trying to do; Where you already looked; Release you are on; Content gate |
| feature-request | Item number; The outcome you need; The workflow that hit the gap; What it costs you today; Your current workaround; Content gate |
| closure-confirmation | Item number; The item being confirmed; Verified outcome; How you verified it; Release you verified on; Content gate |
| feedback-round | Part number; Release this round was measured against; What this round is about; Items in this round; Content gate |

## Local CLI

Run `python -m github_midwife_plugin.feedback_envelope` in an environment with
this plugin installed. For a source checkout the equivalent is
`python plugins/github_midwife_plugin/src/github_midwife_plugin/feedback_envelope.py`.
Supply one UTF-8 JSON object on stdin containing exactly `title` and `body`
(strings), and `facts` with exactly `repository`, `release_tag`, `release_commit`
(strings) and `plugins` (an array of strings). The entire request is limited to
131,072 bytes before JSON parsing. Duplicate JSON keys are rejected at every
level. The CLI reads no file path or command from the request.

Output is one JSON object with `valid`, `diagnostics`, `envelope`, `title` and
`body`; request errors contain `valid` and `diagnostics` only. Exit 0 means a
valid draft, 1 means invalid draft data, and 2 means an invalid caller request.
Stable codes are `invalid_request`, `invalid_utf8`, `input_too_large`,
`invalid_envelope_yaml`, `missing_field`, `unknown_field`, `unsupported_version`,
`invalid_round`, `invalid_type`, `invalid_value`, `release_fact_mismatch`,
`plugin_not_in_release`, `seed_surface_mismatch`, `title_item_mismatch` and
`required_heading`. Diagnostics are deterministic in field order; wording may
change. No output grants permission to publish.

## Lifecycle and integration responsibilities

This is a stateless contract. Callers create a draft and a `ReleaseFacts`
snapshot, read the validation result, correct the draft or fact snapshot, and
validate again. A required field can be corrected but cannot be cleared from a
valid draft; `round: null` explicitly clears an optional parent association.
A caller clears the draft/result by discarding its own values. Schema versions
are corrected through reviewed source changes; an incompatible contract gets a
new version, and unknown versions remain invalid. The diagnostics, constants
`MAX_BODY_BYTES`, `MAX_REQUEST_BYTES`, `MAX_TITLE_BYTES`, `_FIELDS`, `_CLASSES`,
`_REQUIRED_HEADINGS`, `_PATTERNS` and `_MAX_LENGTHS`, and the `EnvelopeValue`
type have source-code rather than database lifecycles. The same applies to the
`ReleaseFacts` fields, `Diagnostic` fields and `ValidationResult` fields and
`valid` property. No persistent record, close/reopen transition, optimistic
write, revision history, clearing verb or authorization override is introduced.
Consumers own append-only evidence and concurrency control for later edits;
a fresh validation of edited bytes is required, and a prior result certifies
only its original draft. Correction, null reversal, missing-field refusal and
revalidation are exercised by the existing drift-guard smoke extension.

Invalid-data refusals protect an explicit data-integrity boundary. The positive
producer in this component is a caller holding independently resolved release
facts and a correctly formed draft; the registered smoke constructs that path
for every class and the CLI. No scheduled production intake consumer is installed
by this change. Future workflow, runbook, skill and form integrations must
supply real facts and use the complete draft callable, then implement their own
quota, authorization and content-audit requirements.

Workflow wiring, quotas, updated form/runbook/skill generation, actual schema
shipping in a seed release and live rollout remain unmeasured residual work.
Presence of this source schema is not proof of release export or adopter use.
The broader feedback pipeline and its intake and publication lifecycles remain
outside this component.
