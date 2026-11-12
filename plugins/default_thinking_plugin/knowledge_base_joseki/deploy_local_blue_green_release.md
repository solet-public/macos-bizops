# Deploy Local Blue-Green Release

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:self-deployment, domain:platform-operations


JOSEKI_KEY: deploy_local_blue_green_release
DESCRIPTION: Deploy committed code to the local solet via the materialized-release blue-green swap. Checks router swap state, captures the manifest etag with a dry run, commits the cutover under a compare-and-swap etag, confirms the old color drains, and verifies plugin health on the new release. Use after a merge to master when the running platform must pick up new code with zero downtime; not for manifest/plugin-set changes requiring binding review.
EMBEDDING_DESCRIPTION: Deploy new committed code on the local solet with zero downtime using the blue-green release swap: check the router swap status for the active color, read the current manifest etag via an apply-manifest dry run, commit apply_manifest with the CAS etag so a new release color builds and cuts over, confirm cutover completion and old-color drain, verify plugin health on the new release. Routine local code deploy after a merge to master.

## Input Contract

- A merged, pushed commit on master whose code the live platform must adopt
- The live manifest plugin list (pass-through unchanged for pure code deploys)
- No swap already in progress; exactly one active color on the router

## Output Contract

- A new materialized release (current symlink -> rel-<timestamp>-<sha>) serving as the active color
- The prior release preserved as previous (durable rollback path)
- The old color drained and reaped (no drain entries remaining)

## Sequence

[ ] 1. Verify router state and that no swap is in progress
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Verify router state and that no swap is in progress (service_interface::local_self_deployment_service::swap_status)

[ ] 2. Read the current manifest etag with a dry run
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Read the current manifest etag with a dry run (service_interface::lifecycle_management_service::apply_manifest)

[ ] 3. Commit the cutover under the captured etag
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Commit the cutover under the captured etag (service_interface::lifecycle_management_service::apply_manifest)

[ ] 4. Confirm cutover completion and old-color drain
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Confirm cutover completion and old-color drain (service_interface::local_self_deployment_service::swap_status)

[ ] 5. Verify plugin health on the new release
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Verify plugin health on the new release (service_interface::lifecycle_management_service::list_plugins)

[ ] 6. Record the deploy outcome and step state
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Record the deploy outcome and step state (service_interface::thinking_service::record_work_breakdown_structure_step_state)

## Expected Step Count

6 steps.

## Binding Guidance

After the authorizing seat enables its external driver, prefer the standalone
command at `ananta/src/ananta/services/lifecycle_management_service/_deploy_by_rule.py`
with the lane/runtime interpreter and `SOLET_NAME` set to this deployment's own
solet name (`<<BIND:solet_name>>`, never hardcoded). Supply `--trigger landing`
or `--trigger tick`, absolute `--repo`, `--app-home`, `--releases-root`, `--psolet`
and `--bridge` paths, and the seat-supplied `--agent-instance-id`, `--actor-model`
and `--actor-effort`. `--deadline` defaults to 900 seconds. The command does not
enable a driver or create an identity. The seat owns driver enable, cadence
correction, disable and readback, including identity refresh after rotation.

Under the default cadence rule, a landing deploys only when the cumulative diff
from the last recorded deployment to pinned master includes `ananta/src/`, a
plugin's `src/`, or `profile/config/`, including old and new rename paths.
Other landing deltas emit `skipped_nonproduction`; a seat-owned 7200-second tick
may deploy moved master regardless of paths. Unchanged master does neither.
The command composes steps 1–5 and records a verified `psolet deployed`
observation. Step 6's authored WBS step-state recording remains applicable when
running this card as a WBS. Manual steps below remain diagnostic/repair guidance.

Inspect the command's terminal JSON decision, advisories, pinned target and
measurement id; reconcile with `psolet db version current` and `db version history`.
Live full-battery receipts cause advisory deferral, conservatively across the host
because receipts lack repository identity. Missing observations advise and continue.
This observes neither every landing phase nor a gate starting after the check;
an overlapping gate can fail and need rerun. Identity-holder failures also advise
and continue recording with the supplied identity. Completion requires drained
old color, ready lifecycle plugins and the builder attestation/hash bound to the
pinned full SHA. It consumes the builder's proof rather than independently
rehashing release files; live profile config is checked separately. An ambiguous
apply is never resubmitted. An already-serving verified target with a missing
version observation is recorded on retry without another swap. Unresolved or
invalid proof yields failure without a success observation; use the repair verbs
below when seat investigation establishes that rollback is needed.

After `deployed`, `already_recorded`, `already_current` or
`skipped_nonproduction`, the verified outcome is complete and the external
command sends no Main notice. Success is read from the register's version
current/history rows; exception storage is not model acceptance. A `failed` or
`deferred_*` terminal outcome attempts one bounded advisory
`peer_send_by_name` through its existing solet-scoped bridge to the solet's
Main role (title-cased `SOLET_NAME` plus `-Main`). The exception content carries
the solet, outcome, trigger, and known target/release/measurement identities,
leaving unavailable identities explicitly null, plus bounded native error and
advisories. Its stable `notice_key` identifies the same terminal outcome.

Read terminal JSON's `notice` alongside a failure or hold decision. `status=queued`
and its `message_id` mean queued delivery only, not model receipt or acceptance.
A timeout, unavailable role, malformed/error reply or missing message id leaves
`status=uncertain` and an explicit advisory while preserving the original
failure/hold decision and exit behavior. There is no automatic notice retry,
second APPLY or duplicate deployed record; uncertain effects require read
reconciliation before a new explicit attempt. Correction or reversal uses a
separate verified deployment and measurement, with old records and receipts
immutable. Stdout remains the terminal outcome readback even when notice
delivery is uncertain.

- Bind step 2 and step 3 `new_manifest` to the LIVE manifest content unchanged for a pure code deploy; any plugin-set change makes this a different (manifest-change) procedure with binding-validation review.
- Bind step 3 `expected_etag` ONLY from step 2's `current_etag` in the same run — never from a stale read.
- Bind step 3 `reason` to carry the merge SHA being deployed, for the release ledger audit trail.
- Bind step 4 as repeat-until: `active_color` flipped AND `drain_entries` empty AND `swap_in_progress` false; the release build plus new-color boot takes minutes — poll patiently, do not resubmit.
- Step 5 passes no filter: review the full roster for `status=error` or `is_running=false` rows.

## Coherence Obligations

- Never commit a cutover without a same-run dry-run etag — the CAS is the guard against racing a concurrent deploy, and it only works if the read is fresh.
- A swap is complete when the OLD color leaves the drain entries (finisher reaped), not merely when `active_color` flips; declaring success at the flip leaves an undrained process holding resources.
- New verbs introduced by the deployed commit are live only after the new color's registry build: verify them by process discovery before starting dependent work, and expect a brief burst of namespace errors from flows caught mid-swap (self-terminating).
- Wait for startup quiescence before submitting model-dependent work to the new color: knowledge-base re-ingest competes for the embedder immediately after boot.

## Next Joseki

Explicitly absent — post-deploy live verification is performed inline today; it should graduate to its own card once the pattern stabilizes.

## Repair Joseki

Explicitly absent as a card. The platform repair verbs are `service_interface::local_self_deployment_service::swap_rollback` (inside the drain window: re-point the router to the prior color) and `service_interface::local_self_deployment_service::rollback_release` (any time after: durable code rollback to the previous materialized release). A dedicated rollback joseki should be authored when these have a proven multi-step procedure around them.
