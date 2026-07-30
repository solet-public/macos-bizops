# Marketo Merge Canary — One Duplicate Group

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:marketo, domain:data-remediation


JOSEKI_KEY: marketo_merge_canary_one_group_v2
DESCRIPTION: Execute exactly ONE Marketo duplicate-person merge as a gated canary and capture the three field snapshots that decide whether the merge rule is safe to scale. Names the OLDEST record as winner so its read-only originalSourceType/originalSourceInfo survive, sets mergeInCRM=false so Marketo does not ask the CRM to merge its own records, then reads the group immediately after the merge and again after a sync cycle. mergeInCRM=false does NOT mean Salesforce is untouched: merge-resulting field values sync to the linked record within about 90 seconds, which is intended — filling a gap in Salesforce is desirable, and duplicate Salesforce records cannot be created because that org forbids duplicate email addresses by design. The immediate read answers whether the survivor inherited the live SFDC Contact or kept the winner's purged Contact id; the post-sync read answers whether the sync recomputes the read-only source fields back to salesforce.com/Contact. Merges are IRREVERSIBLE and must never be retried blindly. Use before any bulk merge run.
EMBEDDING_DESCRIPTION: Safely merge a single pair or group of duplicate people in Marketo as a test canary before running thousands, keeping the oldest original record as the survivor to preserve its read-only original source type and source info fields, allowing the merged field values to flow on to the linked Salesforce record as they normally would while preventing Marketo from merging the CRM records themselves, and recording person field values before the merge, right after the merge, and once more after the CRM sync has run so you can tell whether the surviving person kept the live Salesforce contact link and whether the sync overwrote the source fields. One irreversible destructive merge with full before-and-after evidence.

Supersedes `marketo_merge_canary_one_group`, which asserted that `mergeInCRM=false`
means "Salesforce is never written". Measured against live Marketo on 2026-07-29 that
is false: the managed-package sync pushed a merge-filled field to the linked record in
about 90 seconds. The operator's position is that this push is *wanted* — Salesforce
usually already holds the record, and filling a gap in it is desirable — and that the
real safety property is different: duplicate Salesforce records cannot be created,
because that org forbids duplicate email addresses by design. The v1 wording would have
led an operator to believe Salesforce was isolated while driving the first irreversible
merge of a customer record.

## Input Contract

- One duplicate group already chosen, all of whose Marketo person ids are known
- The group must have exactly ONE live SFDC Contact among its members, and that Contact must sit on a record OTHER than the winner (this is the case being tested)
- The winner is the OLDEST member by createdAt; every other member is a loser
- Marketo reachable through marketo_plugin with a role permitting merge_leads (unproven until step 2 runs)

## Output Contract

- Pre-merge snapshot of every group member recorded on the run flow
- Merge outcome recorded: success plus request id, or a permission/validation error
- Immediate post-merge snapshot recorded — decides the CRM-link question
- Post-sync snapshot recorded — decides the source-field-recompute question
- Together these four records are the evidence for whether the rule scales; the consumer applies the verdict per Coherence Obligations

## Sequence

[ ] 1. Snapshot every group member before touching anything
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Read all group members with the decision fields (plugin::marketo_plugin::get_leads)
        Arguments:
        {"filter_type": "id", "filter_values": "<<BIND:group_person_ids>>", "fields": ["id", "email", "createdAt", "updatedAt", "leadStatus", "leadScore", "originalSourceType", "originalSourceInfo", "sfdcType", "sfdcContactId", "sfdcLeadId"]}

[ ] 2. Merge the losers into the oldest record with mergeInCRM=false
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Merge losing people into the winning person (plugin::marketo_plugin::merge_leads)
        Arguments:
        {"winning_lead_id": "<<BIND:winning_lead_id>>", "losing_lead_ids": "<<BIND:losing_lead_ids>>", "merge_in_crm": false}

[ ] 3. Read the group immediately after the merge
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Re-read the same ids with the same fields (plugin::marketo_plugin::get_leads)
        Arguments:
        {"filter_type": "id", "filter_values": "<<BIND:group_person_ids>>", "fields": ["id", "email", "createdAt", "updatedAt", "leadStatus", "leadScore", "originalSourceType", "originalSourceInfo", "sfdcType", "sfdcContactId", "sfdcLeadId"]}

[ ] 4. Read the group again after a sync cycle has elapsed
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Re-read the same ids a third time (plugin::marketo_plugin::get_leads)
        Arguments:
        {"filter_type": "id", "filter_values": "<<BIND:group_person_ids>>", "fields": ["id", "email", "createdAt", "updatedAt", "leadStatus", "leadScore", "originalSourceType", "originalSourceInfo", "sfdcType", "sfdcContactId", "sfdcLeadId"]}

## Expected Step Count

4 steps.

## Binding Guidance

- Bind `group_person_ids` to EVERY Marketo person id in the group — winner and losers together — and use the identical list in steps 1, 3 and 4. Reading the losers is not redundant: their disappearance in step 3 is how the merge is confirmed, and a loser still present means the merge did not do what was asked.
- Bind `winning_lead_id` to the OLDEST member by `createdAt`, tie-breaking on the lowest Marketo id. This is the whole point of the card: `originalSourceType` and `originalSourceInfo` are read-only in the REST API, so only the named winner's values can survive. Naming any other record silently and permanently discards them.
- Bind `losing_lead_ids` to every remaining member. Cap is 25 per call, and exactly 1 if `merge_in_crm` were ever true — it must not be.
- Do NOT bind `merge_in_crm`. It is a hard-coded literal `false`. The plugin only sends the `mergeInCRM` parameter when the argument is present, so omitting it would leave Marketo to apply its own default against a Salesforce org that was itself restored from a mass deletion. Understand what `false` does and does not buy: it stops Marketo merging the *CRM* records, and it caps `losing_lead_ids` at 1 if it were ever true. It does NOT stop the managed-package sync from pushing the merge's field values to the linked Salesforce record — measured at roughly 90 seconds. That push is wanted, not a leak.
- Do NOT bind the field lists. They are literals and identical across all three reads so the snapshots are directly comparable.
- Run step 4 only after a sync cycle has actually elapsed. With the Marketo managed package that is seconds, not hours — but a step 4 that runs too early reports a false pass on the source-field question.

## Coherence Obligations

- Step 2 is IRREVERSIBLE and must never be re-fired. If it returns ambiguously — timeout, transport error, unclear envelope — do NOT retry: the winner may already have absorbed the losers, whose ids are now gone, so a retry can merge the wrong records. Go to step 3, read the live state, and decide from evidence.
- A `marketo.permission_denied` on step 2 is a clean, harmless failure: the role lacks merge rights and nothing was changed. Abandon the run and fix the role.
- Merge confirmed requires BOTH, in step 3: every loser id absent, AND the winner id still present.
- **The CRM-link verdict is the gate on scaling.** In step 3 the survivor's `sfdcContactId` must equal the group's ONE live Contact id — not the winner's own pre-merge value, which is a purged Contact id. If the survivor holds the purged id instead, the rule is falsified: that person's Salesforce sync is broken, and the same outcome would apply to every group. STOP; do not proceed to a larger batch.
- **The source-field verdict needs step 4, not step 3.** `originalSourceType` and `originalSourceInfo` must match the winner's pre-merge values in step 3 AND still match in step 4. Correct in step 3 but reverted to `salesforce.com` / `Contact` in step 4 means the sync recomputes them from the inherited CRM link — the rule is defeated even though the merge behaved. STOP and re-plan.
- `leadStatus` and `leadScore` are the recoverable case. If either is wrong on the survivor, that alone does not fail the canary: both are writable and can be restored with create_or_update_leads using action updateOnly. Record the discrepancy rather than aborting.
- A merge fires Marketo trigger campaigns — lead-created, data-value-changed and program-status-changed are all in play. Measured: the merge's own activities carry Reason `Merge (Web service API)`, NOT `Merge (leaddb)`, so any campaign-side exclusion keyed on the latter will not match and gives false assurance. Smart campaigns were also observed writing to the survivor about 45 seconds after a merge. This card captures field evidence only; it does not observe campaign side effects. Inspect campaign activity out of band before scaling.
- Expect the survivor's blank fields to be filled from the losers and its non-blank fields to be left alone. Measured on a synthetic pair: a blank field took the loser's value, a differing non-blank field was kept, and every Data Value Change carried the merge's Reason with no non-blank overwrite. A non-blank overwrite would falsify the rule the whole remediation rests on — treat one as a STOP.

## Next Joseki

Explicitly absent as a registered card. A passing canary routes to the batched merge runner, which is ordinary tooling rather than a joseki: it needs a checkpoint, a daily call budget and bounded concurrency, none of which a card models. Re-run this card at a wider group count only to re-confirm before scale.

## Repair Joseki

Explicitly absent, and deliberately so — a completed Marketo merge cannot be undone. The only remediation is forward: create_or_update_leads with action updateOnly to restore the writable fields (`leadStatus`, `leadScore`). The read-only source fields and a lost CRM link have no API remedy; a failed CRM-link verdict is an operator decision, not a repair procedure.
