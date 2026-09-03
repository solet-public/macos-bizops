# Branch Salesforce Org — Business Conventions (learned in operation)

Embedding Description: Business conventions learned operating Branch's
Salesforce org: closed-opp stage values, the describe_sobject picklist
limitation, the region rollup field for revenue analyses
(Account.Top_Billing_Region__c), and new-platform migration tracking. For
churn/bookings metric definitions (which Opportunity Types count, which ACV
field to sum), see `bizops_knowledge_base` — this article no longer states
its own churn convention.

## Churned opportunities — canonical definition lives in bizops_knowledge_base

Do not use this article for the churn/bookings-metric formula. A prior
(2026-08-18) version of this section stated `Type IN ('Churn', 'Clawback -
Churn')` summed on `ACV_at_Time_of_Churn__c` as the churn convention — that
conflicts with the company's actual GTM metric doctrine (`Clawback - Churn`
is explicitly excluded from Finance's GTM aggregations, and the standard
bookings metric sums `Software_ACV__c`, not `ACV_at_Time_of_Churn__c`). That
version is retired as of 2026-09-03, after `bizops_knowledge_base` (synced
from `BranchMetrics/bizops-knowledge-base`) was ingested and found to
disagree with it.

Query `bizops_knowledge_base` directly instead of relying on a second,
hand-maintained copy of the same doctrine here — duplication is exactly how
this article drifted out of sync in the first place:

- Base bookings-metric doctrine: `prod_share_branch/psb-015-gtm-metric-definitions.md`
  (PSB-015) and `prod_share_branch/psb-014-opportunity-type-reference.md` (PSB-014).
- The 2026-08-24 operator refinement (combined full-churn+downscope view vs.
  full-logo-churn-only view): `salesforce/sfdc-156-renewal-churn-revenue-field-rulings.md`.
- Per-opportunity-type field rules: `salesforce/sfdc-107..114-opportunity-type-*.md`.

What's still true here and unrelated to the retired convention:

- `Churn` and `Clawback - Churn` are real Opportunity `Type` values in this
  org; churn closes as `StageName = '7.1: Closed Won'` — "won" means the
  churn was processed, not that revenue was won.
- Supporting picklists on churn opps: `Churn_Type__c` (Voluntary/Involuntary),
  `Churn_Reason__c` (label: "Primary Churn/Downscope Reason"),
  `Primary_Churn_Downscope_Reason_Details__c`, free-text `Churn_Details__c`.
- Closed-opp stage values this org actually uses: `7.1: Closed Won`,
  `7.2: Closed Lost` (lost pursuit, NOT churn), `7.3: Reject`.
- `describe_sobject` in this org returns picklist fields **without**
  `picklistValues`; discover real values with a GROUP BY query over live
  data instead.
- Region rollups for revenue analyses use `Account.Top_Billing_Region__c`
  (per operator, 2026-08-18); `Greater_Billing_Region__c` no longer rolls
  NA/LATAM into AMS.

## New-platform migration tracking (verified 2026-08-20)

"Live on the new platform" is tracked on **Account** in the picklist
`New_Platform_Migration_Status__c` (label "New Platform Migration Status").
Observed values: `Live on New Platform - Migrated`, `Live on New Platform -
Net New`, `Ready for migration`, `Eligible for migration`, `Pending
eligibility review`, `CX ready`, `Not eligible for migration`. Count live
customers with:

```sql
SELECT COUNT(Id) FROM Account
WHERE New_Platform_Migration_Status__c LIKE 'Live on New Platform%'
  AND Customer_Lifecycle_Status__c = 'Customer'
```

Filter on `Customer_Lifecycle_Status__c = 'Customer'` — the migration field
is also set on a handful of Prospects/Former Customers. Most customer
accounts (~8.7k of ~9.6k) have the field blank; it is maintained only for
the actively-managed base. Companion schedule fields:
`Assigned_New_Platform_Upgrade_Month__c`,
`Preferred_New_Platform_Upgrade_Month__c`,
`Preferred_New_Platform_Upgrade_Notes__c`.

## CLI quoting trap

`larry call plugin::salesforce_plugin::soql_query '<json>'` breaks when the
SOQL contains single-quoted literals (`Type IN ('Churn')`) — the shell's
outer single quotes terminate. Write the JSON payload to a file and pass
`"$(cat payload.json)"` instead.
