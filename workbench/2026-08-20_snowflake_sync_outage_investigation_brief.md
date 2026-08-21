# Brief: Snowflake sync-outage investigation (lane-sync-outage-snowflake)

**Dispatched by:** Operator session, 2026-08-20
**Worker role:** Snowflake-Analyst (sonnet, medium effort)
**Read-only investigation. Never run DDL/DML/ALTER/EXECUTE TASK — only SHOW / SELECT / DESCRIBE.**

## Access

Local `snow` CLI (`/opt/homebrew/bin/snow`), default connection: account gea00983,
user DAVID@BRANCH.IO, role ANALYTICS_FULL_READER_ROLE. Example:

```sh
snow sql -q "SHOW TASKS IN ACCOUNT" --format json
```

Known layout note: `ANALYTICS.PROD_SHARE_BRANCH` is a relevant schema (possibly a share).
The tasks/procedure may live in a different database the role can see.

## Background (field report — verify, don't assume)

A stored procedure that syncs **133 Salesforce objects** into Snowflake had its signature
changed (new `object_group` argument) without updating the calling task(s). All runs broke
starting **2026-06-11 06:00**. Snowflake auto-suspended BOTH tasks after 3 consecutive
failures. They sat suspended ~6.5 days until someone fixed the call and resumed them on
**2026-06-18**. The next successful run was 2026-06-18 06:00 and only looked back 6 hours,
so records modified during the gap were never synced. No backfill was ever run.

## Goals — answer ALL

1. **Find the two tasks and the stored procedure.** `SHOW TASKS IN ACCOUNT`,
   `SHOW PROCEDURES IN ACCOUNT` (fall back to per-database if denied). Get each task's
   schedule (CRON + timezone or interval), its SQL definition (the procedure call), and
   current state.
2. **Extract the exact list of ~133 Salesforce object names the procedure syncs.**
   Procedure body via `SELECT GET_DDL('PROCEDURE', '...')`, and/or a config/driver table
   the procedure reads (the new `object_group` argument suggests a driver table with a
   group column). If the list lives in a table, SELECT it.
3. **Determine which Salesforce timestamp field the sync filters on** — LastModifiedDate
   vs SystemModstamp — and the exact lookback logic. Quote the relevant procedure code
   verbatim.
4. **Pin the exact missed window in UTC.** Try `SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY`
   and the `INFORMATION_SCHEMA.TASK_HISTORY()` table function (either may be denied —
   try both, report which worked). Identify: last successful run before the break, first
   failed run, resume time, first successful run after the fix → the un-synced window
   [start, end] in UTC. If task history is invisible, derive the window from the task
   schedule + the field report, adjusted for the schedule's actual timezone.
5. **Sanity-check the gap in the data.** Pick 2–3 large synced tables and count rows
   grouped by day of the sync timestamp field across 2026-06-08..2026-06-21. Confirm the
   hole is visible and which timestamp column shows it. If someone already backfilled,
   there will be no hole — report that explicitly.

## Deliverables

1. Object list as JSON →
   `/private/tmp/claude-503/-Users-david-westgate-Workspace-dax/e72d44ad-1f87-4a7d-a668-1d31fab20233/scratchpad/sync_outage_objects.json`
   Shape: `{"objects": [...], "count": N, "source": "<where the list came from>", "object_groups": {...if groups exist}}`
2. Full report (markdown) →
   `/private/tmp/claude-503/-Users-david-westgate-Workspace-dax/e72d44ad-1f87-4a7d-a668-1d31fab20233/scratchpad/sync_outage_report.md`
   covering: task names/schedules/timezone, procedure name + verbatim lookback/filter
   logic, timestamp field used, exact UTC missed window with evidence, object count +
   list source, gap sanity-check results, anything surprising.
3. When both files are written, print `INVESTIGATION COMPLETE` as your final line.

Be token-efficient: targeted queries, LIMIT everything, don't dump big result sets into
context.
