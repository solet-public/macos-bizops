#!/usr/bin/env python3
"""Create the Google Sheet from the three tab CSVs, via dax's g_suite_plugin.

Usage: python3 create_sheet.py <outdir> [YYYY-MM-DD]
(date defaults to today if omitted -- but see README: this environment
disallows Date.now()-equivalents in some contexts, so pass it explicitly
from the calling shell to be safe: `date +%F`)

IMPORTANT -- read before changing this script: as of 2026-08, every
g_suite_plugin verb (including sheets_create_from_files and
sheets_batch_update) dispatches as an ASYNC JOB. The dispatch call returns
{job_id, status: "queued"} immediately; the actual result is NOT in that
response. There is no plugin-specific job-status verb exposed for g_suite.
The only way to retrieve the result from a `dax call` CLI session (as
opposed to a live listening flow) is the generic job_payload table via the
platform's own StateManagementInterface primitive:

    dax call service_interface::state_service::read_state \\
      '{"namespace": "core", "query": {"table": "job_payload",
         "filters": {"job_id": "<job_id>"}}}'

...and look for a row with payload_type "result" (success) or "error"
(failure). This script does that polling for you. If a future dax release
adds a real job-status verb for g_suite, prefer that instead -- check
`plugins/g_suite_plugin/knowledge_base/processes/` for one before assuming
this workaround is still necessary.
"""
import json, subprocess, sys, time

DEFAULT_W = 100
CAP = 2 * DEFAULT_W  # operator's stated default: autofit capped at 2x default width


def dax_call(process_key, args):
    r = subprocess.run(
        ["dax", "call", process_key, json.dumps(args)],
        capture_output=True, text=True,
    )
    out = r.stdout.strip()
    try:
        return json.loads(out)
    except Exception:
        print(f"dax call {process_key} did not return JSON:\n{out}\n{r.stderr}", file=sys.stderr)
        raise


def poll_job(job_id, timeout_s=60, interval_s=2):
    """Poll core.job_payload for a result/error row. Returns (payload_type, data)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = dax_call("service_interface::state_service::read_state", {
            "namespace": "core",
            "query": {"table": "job_payload", "filters": {"job_id": job_id}},
        })
        records = resp["result"]["data"]["records"]
        for rec in records:
            if rec["payload_type"] in ("result", "error"):
                return rec["payload_type"], json.loads(rec["payload_data"])
        time.sleep(interval_s)
    raise TimeoutError(f"job {job_id} did not complete within {timeout_s}s")


def width_for(header):
    est = len(header) * 7 + 24
    return max(DEFAULT_W, min(CAP, est))


def main():
    if len(sys.argv) < 2:
        print("usage: create_sheet.py <outdir> [YYYY-MM-DD]", file=sys.stderr)
        sys.exit(1)
    outdir = sys.argv[1]
    date_prefix = sys.argv[2] if len(sys.argv) > 2 else None
    if not date_prefix:
        print("WARNING: no date passed -- title will have no date prefix. "
              "Pass `date +%F` from the caller.", file=sys.stderr)
        title = "BranchMetrics GitHub Repo Compliance Audit"
    else:
        title = f"{date_prefix} BranchMetrics GitHub Repo Compliance Audit"

    tabs = [
        {"name": "Summary", "file_path": f"{outdir}/tab1_summary.csv"},
        {"name": "Detail", "file_path": f"{outdir}/tab2_detail.csv"},
        {"name": "Legend", "file_path": f"{outdir}/tab3_legend.csv"},
    ]

    print(f"Creating spreadsheet '{title}'...", file=sys.stderr)
    resp = dax_call("plugin::g_suite_plugin::sheets_create_from_files", {"title": title, "tabs": tabs})
    job_id = resp["result"]["data"]["job_id"]
    kind, payload = poll_job(job_id)
    if kind == "error":
        print(f"Sheet creation FAILED: {payload}", file=sys.stderr)
        sys.exit(1)

    spreadsheet_id = payload["id"]
    sheet_ids = {t["name"]: t["sheet_id"] for t in payload["tabs"]}
    print(f"Created: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit", file=sys.stderr)

    headers = {
        "Summary": ["Repo", "Owning Team", "Compliance Gaps", "Confidence"],
        "Detail": ["Repo", "Internal?", "Branch=main?", "Owning Team", "Admin Team", "Infra Admin?",
                   "Individual Collabs", "Branch Wr?", "CODEOWNERS?", "CO Required OK?", "Codecov?",
                   "PR Required?", ">=1 Approval?", "CO Review?", "Status Checks?", "Branch Current?",
                   "No Bypass?", "Squash Only?", "PR Title Default?", "Auto-Del Branch?", "Notes"],
        "Legend": ["Column Label", "Full Requirement"],
    }
    requests = []
    for tab_name, sheet_id in sheet_ids.items():
        for i, h in enumerate(headers[tab_name]):
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": width_for(h)}, "fields": "pixelSize",
            }})
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }})
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }})

    print("Applying formatting...", file=sys.stderr)
    resp = dax_call("plugin::g_suite_plugin::sheets_batch_update", {"id": spreadsheet_id, "requests": requests})
    job_id = resp["result"]["data"]["job_id"]
    kind, payload = poll_job(job_id)
    if kind == "error":
        print(f"Formatting FAILED (sheet still created, just unformatted): {payload}", file=sys.stderr)

    print(f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit")


if __name__ == "__main__":
    main()
