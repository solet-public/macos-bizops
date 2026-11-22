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
or `--trigger tick`, absolute `--repo`, `--app-home`, `--releases-root` and
`--bridge` paths, and the seat-supplied `--agent-instance-id`, `--actor-model`
and `--actor-effort`. `--deadline` defaults to 900 seconds. The command does not
enable a driver or create an identity. The seat owns driver enable, cadence
correction, disable and readback, including identity refresh after rotation.

Under the default cadence rule, a landing deploys only when the cumulative diff
from the last recorded deployment to pinned master includes `ananta/src/`, a
plugin's `src/`, or `profile/config/`, including old and new rename paths.
Other landing deltas emit `skipped_nonproduction`; a seat-owned 7200-second tick
may deploy moved master regardless of paths. Unchanged master does neither.
The command composes steps 1–5 and records the verified deployment through the
solet's own `record_deployment` verb, reading the last one back through
`read_deployments`. It calls only git and this solet's own bridge. Step 6's
authored WBS step-state recording remains applicable when running this card as a
WBS. Manual steps below remain diagnostic/repair guidance.

A newly recorded deployment (decision `deployed`) or a landing skipped as
non-production asks the solet for one full battery run of master's newest commit, through its own
`trigger_full_smoke_battery` verb with trigger `deploy`; the reply is in the
terminal JSON under `full_battery`. A failed ask is an advisory
(`full_battery_request_unresolved`), is not retried, and never changes the
decision; the next deployment asks again, and the verb runs a commit only once.
No ask follows `already_recorded`, `already_current`, `deferred_*` or `failed`.
A skipped landing sends no Main notice. A seat-owned 7200-second tick may deploy
moved master regardless of paths, and then asks the same way.

Inspect the command's terminal JSON decision, advisories, pinned target and
deployment id; reconcile against the solet's own records with
`solet-bridge call service_interface::lifecycle_management_service::read_deployments '{"solet_name":"<<BIND:solet_name>>","mode":"history"}'`
(mode `current` returns the newest non-withdrawn deployment).
A live battery receipt whose campaign does not start `scheduled:` causes advisory
deferral (`deferred_gate`), conservatively across the host because receipts lack
repository identity; a running full campaign's receipt does not: the command notes
it as `full_campaign_running_not_a_hold` and deploys, the swap interrupts that run,
and the deploy's own request asks for the new commit's run. Missing observations
advise and continue.
This observes neither every landing phase nor a gate starting after the check;
an overlapping gate can fail and need rerun. Identity-holder failures also advise
and continue recording with the supplied identity. Completion requires drained
old color, ready lifecycle plugins and the builder attestation/hash bound to the
pinned full SHA. It consumes the builder's proof rather than independently
rehashing release files; live profile config is checked separately. An ambiguous
apply is never resubmitted. An apply the verb answers with an error fails the run at once as apply_rejected; only an apply whose outcome is unknown waits for the drain. An already-serving verified target with a missing
version observation is recorded on retry without another swap. Unresolved or
invalid proof yields failure without a success observation; use the repair verbs
below when seat investigation establishes that rollback is needed.

A cutover that refuses the release's schema change ends the run as
`deferred_schema_route`, not as `apply_rejected`. The committing apply's answer
carries the cutover's code `schema_preflight_refused` as `restart_reason_code`.
The terminal JSON names the hand route in `route` (the section "Installing a
release the cutover refuses" below), the code in `refused`, the cutover's message
in `detail`, and `built: true`, because the cutover builds the candidate before it
refuses it. That first run sends one Main notice whose `error` starts
`schema_preflight_refused:`, and writes the marker file
`deploy-by-rule.schema-route.json` in the `data/runtime` folder under the app
home, holding `target`, `current_release` and `refused_at`. Every later run whose
`releases_root/current` is still the marker's `current_release` returns
`deferred_schema_route` with `built: false`. It does so before it looks for a
gate, reads the swap state or sends any apply, the dry run included, and it sends
no notice. The marker clears when the hand route moves current: a run that finds a
different current ignores the marker and goes on as usual, and the next run
records its deployment. The command never deletes the marker; to try again before
current moves, for example when master no longer drops the column, remove the
file. A marker that cannot be read or written is an advisory
(`schema_route_marker_unreadable`, `schema_route_marker_unwritten`) and the run
goes on.

After `deployed`, `already_recorded`, `already_current` or
`skipped_nonproduction`, the verified outcome is complete and the external
command sends no Main notice. Success is read from the solet's own deployment
records (`read_deployments`, modes `current` and `history`); exception storage is
not model acceptance. A `failed` or
`deferred_*` terminal outcome attempts one bounded advisory
`peer_send_by_name` through its existing solet-scoped bridge to the solet's
Main role (title-cased `SOLET_NAME` plus `-Main`). The exception content carries
the solet, outcome, trigger, and known target/release/measurement identities,
leaving unavailable identities explicitly null, plus bounded native error and
advisories. Its stable `notice_key` identifies the same terminal outcome. The one
exception is a `deferred_schema_route` with `built: false`, which repeats a notice
already sent and sends none.

Read terminal JSON's `notice` alongside a failure or hold decision. `status=queued`
and its `message_id` mean queued delivery only, not model receipt or acceptance.
A timeout, unavailable role, malformed/error reply or missing message id leaves
`status=uncertain` and an explicit advisory while preserving the original
failure/hold decision and exit behavior. There is no automatic notice retry,
second APPLY or duplicate deployed record; uncertain effects require read
reconciliation before a new explicit attempt. Correction or reversal uses a
separate verified deployment record, with old records and receipts
immutable. Stdout remains the terminal outcome readback even when notice
delivery is uncertain.

- Bind step 2 and step 3 `new_manifest` to the LIVE manifest content unchanged for a pure code deploy; any plugin-set change makes this a different (manifest-change) procedure with binding-validation review.
- Bind step 3 `expected_etag` ONLY from step 2's `current_etag` in the same run — never from a stale read.
- Bind step 3 `reason` to carry the merge SHA being deployed, for the release ledger audit trail.
- Bind step 4 as repeat-until: `active_color` flipped AND `drain_entries` empty AND `swap_in_progress` false; the release build plus new-color boot takes minutes — poll patiently, do not resubmit.
- Step 5 passes no filter: review the full roster for `status=error` or `is_running=false` rows.

## Installing a release the cutover refuses

The blue-green cutover refuses any release that drops a column or a table, and it has no override. A release like that is installed by hand, in the order below.

WHEN: a deploy by rule ended `deferred_schema_route`, a deploy answered `schema_preflight_refused`, or the Manager refused an update with `supervisor_launch_needs_hand_route`.

WHO: a solet whose LaunchAgent runs `macos_self_deployment_plugin.supervisor`. Read the agent's plist: when its ProgramArguments run that module, this route applies. A solet made by `solet create` launches `ananta.cli` directly and updates through the Manager, so it does not need this route.

DECISION: the decision to take this route belongs to whoever operates the solet. The operator chooses the window. The controller seat for that solet carries the route out and records the output of each step. A session that only reads this section does not take the route.

COST: the restart applies the release's schema change to the live database, and code cannot undo that. Dropped columns and tables, and their data, do not come back by code. Code alone goes back with `ReleaseManager.rollback()` followed by the same restart. Every session's bridge stops while the solet restarts.

PRECONDITIONS: all of the following hold before step (c), the restart.
- A backup of every table and column the release drops exists. A plain pg_dump of those tables is enough, and no restore test is required. Record where the backup is, as <backup_file>.
- No seed verification and no release run is in flight inside the solet, because a restart loses them. The in-flight check below must pass.
- No deploy-by-rule run is in flight.
- The checkout holds the commit to install. The build's dirty-tree gate decides whether the tree is clean: it refuses dirty paths unless the caller passes allow_dirty, and this route does not pass it.

WHERE THE DROP HAPPENS: the release's schema change is applied when the new release boots, through the Postgres backend's update path in plugins/postgres_state_management_plugin/src/postgres_state_management_plugin/postgres_backend/lifecycle.py. That path drops the tables the release retires first, and then drops the columns the release removes (ALTER DROP). The new release boots during step (c), so the backup must exist before step (c) starts.

IN-FLIGHT CHECK: list the release runs with `solet-bridge call plugin::seed_factory_plugin::list_release_runs '{}'`, and read each run that is not finished with `solet-bridge call plugin::seed_factory_plugin::release_run_status '{"run_id": "<run_id>"}'`. Read any seed verification the operator knows of with `solet-bridge call plugin::seed_factory_plugin::verify_seed_bundle_status '{"job_id": "<job_id>"}'`. Every one must be finished before the restart.

(a) Build the release from the commit, passing the schema snapshot and the manifest plugin set that the orchestrator passes. Run this from the solet's own environment, with the checkout as the working directory:

from pathlib import Path
from ananta.core.plugins.profile_manifest import load_manifest_plugin_set
from macos_self_deployment_plugin.release_manager import ReleaseManager
from macos_self_deployment_plugin.schema_snapshot_producer import build_schema_snapshot_fn

app_home = Path("<repo>/profile")
source_root = Path("<repo>")
manager = ReleaseManager(solet_name="<name>", source_root=source_root)
live_plugins = load_manifest_plugin_set(app_home)
manifest_plugins = None if live_plugins is None else tuple(sorted(live_plugins))
candidate = manager.build_candidate(
    manifest_plugins=manifest_plugins,
    schema_snapshot_fn=build_schema_snapshot_fn(
        solet_name="<name>", app_home=app_home, source_root=source_root,
    ),
)
print(candidate.release_id)

Print the release id, or reuse a finalized release already built from that commit (its VERSION records the git sha).

(b) Before this step, record the release that serves now, from `ReleaseManager.current_release`. Then point current at the built release, and read state.json back in the releases root. Check that current names the new release and previous names the release recorded before this step:

manager.cutover(manager.candidate_for(candidate.release_id))
print(manager.current_release)

(c) Stop the serving colour, then kickstart the supervisor's LaunchAgent. A colour runs as a process whose command line is `-m ananta.cli --app-home <repo>/profile`. Stop each such process with `kill <pid>`. The supervisor starts the active colour from current once the agent restarts:

launchctl kickstart -k gui/<uid>/local.solet.<name>

Here <uid> is the numeric user id of the account that runs the agent.

(d) Verify and read back after the restart, and record the output of each read:
- the attested live release, read with `solet-bridge call service_interface::local_self_deployment_service::attest_runtime_code '{"verification_modules": []}'`; it must name the release id from step (a);
- bridge health, read with `solet-bridge health`, which must report healthy;
- exactly one colour process running, found by its command line;
- the dropped column gone from the live table, read with `psql --dbname=<database> --command="\d <schema>.<table>"`;
- the fleet reconnects: sessions that were connected before the restart register again, read with `solet-bridge call plugin::agent_messaging_plugin::fleet_status '{}'`;
- the next deploy-by-rule run records its deployment, read back with `solet-bridge call service_interface::lifecycle_management_service::read_deployments '{"solet_name":"<name>","mode":"current"}'`.

THE WAY BACK, if the new colour does not serve within the readiness budget the operator sets. First, what the supervisor does with a colour that fails to start, read in plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/supervisor.py: it spawns a colour from current whenever the router reports no active colour. If the colour process exits, it waits an exponential backoff, capped, and spawns from current again, so it retries the same release. It does not roll back by itself. If the colour process stays alive without becoming active, it waits for that process with no timeout. The supervisor has no readiness deadline of its own, so the readiness budget is the operator's to set.

Then point current back with `ReleaseManager.rollback()`, which swaps current and previous, so current is the release that served before step (b). Stop the new colour as in step (c), and kickstart the LaunchAgent as in step (c). The supervisor then starts the release that rollback made current.

What the old release's boot does: it declares the tables and columns that the new release dropped, so its schema diff re-creates the retired tables empty and re-adds the dropped columns with no rows. The rows come back only by restoring the backup with pg_restore. Putting them back into the live database is a separate decision, and this route does not make it.


## Coherence Obligations

- Never commit a cutover without a same-run dry-run etag — the CAS is the guard against racing a concurrent deploy, and it only works if the read is fresh.
- A swap is complete when the OLD color leaves the drain entries (finisher reaped), not merely when `active_color` flips; declaring success at the flip leaves an undrained process holding resources.
- New verbs introduced by the deployed commit are live only after the new color's registry build: verify them by process discovery before starting dependent work, and expect a brief burst of namespace errors from flows caught mid-swap (self-terminating).
- Wait for startup quiescence before submitting model-dependent work to the new color: knowledge-base re-ingest competes for the embedder immediately after boot.

## Next Joseki

Explicitly absent — post-deploy live verification is performed inline today; it should graduate to its own card once the pattern stabilizes.

## Repair Joseki

Explicitly absent as a card. The platform repair verbs are `service_interface::local_self_deployment_service::swap_rollback` (inside the drain window: re-point the router to the prior color) and `service_interface::local_self_deployment_service::rollback_release` (any time after: durable code rollback to the previous materialized release). A dedicated rollback joseki should be authored when these have a proven multi-step procedure around them.
