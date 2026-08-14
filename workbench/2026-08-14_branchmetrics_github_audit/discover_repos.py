#!/usr/bin/env python3
"""Discover the candidate repo set for the BranchMetrics GitHub audit.

Live discovery only -- no hardcoded repo names or team rosters, so a re-run
months later reflects the team's actual current grants and membership.

Discovery criteria (as agreed with the operator, 2026-08):
  1. Non-archived repos where the 'business-solutions' team (the org's
     internal name for "bizops") holds an explicit team grant.
  2. Non-archived repos with 'bizops' in the name, regardless of grant.
  3. Any additional individually-named repos the operator confirms belong
     to the team (see EXTRA_INCLUDES below) -- there is no reliable API
     signal for "this repo is ours" beyond (1) and (2), so this list is a
     manually-confirmed addendum, not something to silently regenerate.
  4. Any additional individually-named repos the operator confirms should
     be EXCLUDED even though they'd otherwise match (see EXTRA_EXCLUDES).

Requires: `gh` authenticated with `repo` + `read:org` scopes, SSO-authorized
for the BranchMetrics org (see README.md in this directory).
"""
import json, subprocess, sys

ORG = "BranchMetrics"
TEAM_SLUG = "business-solutions"  # confirmed 2026-08-14: includes dwestgate,
                                   # SushantYadav-branch, jitender-yadav-branch,
                                   # shcaliskan (Hasan Caliskan) -- re-verify
                                   # membership if the team's purpose ever changes.

# Manually confirmed additions/exclusions from the 2026-08-14 run.
# Update this list by hand when the operator confirms a change --
# do not infer it from access patterns alone (see module docstring, criterion 3/4).
EXTRA_INCLUDES = ["salesforce-unit-test-data"]
EXTRA_EXCLUDES = ["branch-salesforce-ai-kit", "woods-service"]


def gh_json(args):
    r = subprocess.run(["gh", "api"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"gh api {args} failed: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(r.stdout) if r.stdout.strip() else None


def main():
    team_repos = gh_json([f"orgs/{ORG}/teams/{TEAM_SLUG}/repos", "--paginate"]) or []
    from_team = {
        r["name"] for r in team_repos if not r.get("archived")
    }

    search = gh_json(["-X", "GET", f"search/repositories?q=bizops+in:name+org:{ORG}&per_page=100"])
    from_name = {
        item["name"] for item in (search or {}).get("items", []) if not item.get("archived")
    }

    combined = (from_team | from_name | set(EXTRA_INCLUDES)) - set(EXTRA_EXCLUDES)
    result = sorted(combined, key=str.lower)

    print(json.dumps(result, indent=2))
    print(f"# {len(result)} repos", file=sys.stderr)


if __name__ == "__main__":
    main()
