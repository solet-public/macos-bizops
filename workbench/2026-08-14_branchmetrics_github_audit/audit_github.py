#!/usr/bin/env python3
"""Pull raw GitHub compliance data for a list of repos.

Usage: python3 audit_github.py <repos.json> <outdir>

Fetches, per repo: core repo fields, team grants, direct collaborators,
CODEOWNERS presence, codecov config presence, branch protection on the
default branch, and merge/PR settings. Also fetches the live membership
roster for every team that shows up on any audited repo (no hardcoded
rosters -- this is what makes "individual collaborator" detection accurate
on a re-run).

Requires: `gh` authenticated with `repo` + `read:org` scopes, SSO-authorized
for the org (see README.md).
"""
import json, subprocess, base64, sys, os

ORG = "BranchMetrics"


def gh(path):
    """Call gh api -i, return (status_code, json_or_none)."""
    r = subprocess.run(["gh", "api", "-i", path], capture_output=True, text=True)
    out = r.stdout
    if not out:
        return None, None
    parts = out.split("\r\n\r\n", 1)
    if len(parts) < 2:
        parts = out.split("\n\n", 1)
    header_block, body = parts[0], (parts[1] if len(parts) > 1 else "")
    try:
        status = int(header_block.splitlines()[0].split(" ")[1])
    except Exception:
        status = None
    try:
        data = json.loads(body) if body.strip() else None
    except Exception:
        data = None
    return status, data


def audit_repo(repo):
    entry = {}

    status, data = gh(f"repos/{ORG}/{repo}")
    if status == 200 and data:
        entry.update({
            "visibility": data.get("visibility"),
            "archived": data.get("archived"),
            "default_branch": data.get("default_branch"),
            "allow_squash_merge": data.get("allow_squash_merge"),
            "allow_merge_commit": data.get("allow_merge_commit"),
            "allow_rebase_merge": data.get("allow_rebase_merge"),
            "delete_branch_on_merge": data.get("delete_branch_on_merge"),
            "squash_merge_commit_title": data.get("squash_merge_commit_title"),
            "squash_merge_commit_message": data.get("squash_merge_commit_message"),
            "html_url": data.get("html_url"),
        })
    else:
        entry["core_error"] = status

    status, data = gh(f"repos/{ORG}/{repo}/teams")
    entry["teams"] = (
        [{"slug": t.get("slug"), "permission": t.get("permission")} for t in data]
        if status == 200 and data is not None else None
    )
    if entry["teams"] is None:
        entry["teams_error"] = status

    status, data = gh(f"repos/{ORG}/{repo}/collaborators?affiliation=direct&per_page=100")
    entry["direct_collaborators"] = (
        [c.get("login") for c in data] if status == 200 and data is not None else None
    )
    if entry["direct_collaborators"] is None:
        entry["collaborators_error"] = status

    codeowners_content = codeowners_path = None
    for path in ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]:
        status, data = gh(f"repos/{ORG}/{repo}/contents/{path}")
        if status == 200 and data and data.get("content"):
            try:
                codeowners_content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                codeowners_path = path
            except Exception:
                pass
            break
    entry["codeowners_path"] = codeowners_path
    entry["codeowners_content"] = codeowners_content

    codecov_found = None
    for path in ["codecov.yaml", "codecov.yml"]:
        status, _ = gh(f"repos/{ORG}/{repo}/contents/{path}")
        if status == 200:
            codecov_found = path
            break
    entry["codecov_path"] = codecov_found

    default_branch = entry.get("default_branch", "main")
    status, data = gh(f"repos/{ORG}/{repo}/branches/{default_branch}/protection")
    if status == 200 and data:
        prr = data.get("required_pull_request_reviews") or {}
        rsc = data.get("required_status_checks") or {}
        entry["protection"] = {
            "pr_required": data.get("required_pull_request_reviews") is not None,
            "min_approvals": prr.get("required_approving_review_count"),
            "codeowner_review": prr.get("require_code_owner_reviews"),
            "status_checks_required": bool(rsc.get("contexts") or rsc.get("checks")),
            "branch_must_be_current": rsc.get("strict"),
            "enforce_admins": (data.get("enforce_admins") or {}).get("enabled"),
        }
    elif status == 404:
        entry["protection"] = "none"
    else:
        # NOT the same as "none" -- a 403 here means we lack admin on this
        # repo and genuinely cannot tell. Surface it as unassessable, don't
        # silently coerce to "no protection" (that reads as a false gap).
        entry["protection_error"] = status

    return entry


def main():
    if len(sys.argv) != 3:
        print("usage: audit_github.py <repos.json> <outdir>", file=sys.stderr)
        sys.exit(1)
    repos_path, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    repos = json.load(open(repos_path))

    results = {}
    all_team_slugs = set()
    for repo in repos:
        print(f"=== {repo} ===", file=sys.stderr)
        entry = audit_repo(repo)
        results[repo] = entry
        for t in (entry.get("teams") or []):
            all_team_slugs.add(t["slug"])

    rosters = {}
    for slug in sorted(all_team_slugs):
        status, data = gh(f"orgs/{ORG}/teams/{slug}/members?per_page=100")
        rosters[slug] = [m["login"] for m in data] if status == 200 and data else []
        if status != 200:
            print(f"WARNING: could not read roster for team '{slug}' (status {status}) -- "
                  f"individual-collaborator detection for repos using this team will "
                  f"under-report.", file=sys.stderr)

    json.dump(results, open(f"{outdir}/audit_results.json", "w"), indent=2)
    json.dump(rosters, open(f"{outdir}/team_rosters.json", "w"), indent=2)
    print(f"Wrote {outdir}/audit_results.json and {outdir}/team_rosters.json", file=sys.stderr)


if __name__ == "__main__":
    main()
