#!/usr/bin/env python3
"""Compute derived compliance fields from raw audit data.

Usage: python3 analyze.py <outdir>
Reads <outdir>/audit_results.json and <outdir>/team_rosters.json,
writes <outdir>/final_rows.json.
"""
import json, sys

ORG_ADMIN_TEAMS = {"saas-gh-admins", "disco-gh-admins", "infra-gh-admins"}
BIZOPS_TEAM = "business-solutions"

# Operator ruling 2026-08-14 (David Westgate, business-solutions owner): the
# doc's model (owning team holds Maintain, an org-admin team holds sole
# Admin) is declined for business-solutions' own repos. bizops is not
# relinquishing Admin/ownership on its repos to an org-admin team -- Admin
# held by business-solutions itself is a deliberate stance, not a compliance
# gap, and should not keep resurfacing across audit re-runs. Only flag if
# business-solutions holds neither Maintain nor Admin (i.e. is missing
# owning-team-level access entirely). This is scoped to business-solutions'
# own repos specifically, not a blanket read of the doc for every team.

# Doc's PR-settings auto-delete-branch exception is scoped to whichever team
# owns the dashboard repo, not to a repo literally named "dashboard". There is
# no clean API signal for "which team is 'the dashboard team'" today, so this
# exception is NOT evaluated here -- if a future audited repo needs it,
# check the repo's owning team by hand rather than pattern-matching the name.


def main():
    if len(sys.argv) != 2:
        print("usage: analyze.py <outdir>", file=sys.stderr)
        sys.exit(1)
    outdir = sys.argv[1]
    audit = json.load(open(f"{outdir}/audit_results.json"))
    rosters = json.load(open(f"{outdir}/team_rosters.json"))

    def yn(b):
        return "Yes" if b else "No"

    rows = []
    for repo, e in audit.items():
        teams = e.get("teams") or []
        team_perm = {t["slug"]: t["permission"] for t in teams}

        bs_role = team_perm.get(BIZOPS_TEAM)
        owning_team_has_maintain = bs_role == "maintain"
        owning_team_ok = bs_role in ("maintain", "admin")

        org_admin_with_admin = [s for s, p in team_perm.items() if s in ORG_ADMIN_TEAMS and p == "admin"]
        infra_admin = "infra-gh-admins" in org_admin_with_admin

        branch_write = team_perm.get("branch") == "push"

        push_teams = [s for s, p in team_perm.items() if p == "push"]
        codeowners_required = len(push_teams) > 1 or branch_write
        codeowners_present = bool(e.get("codeowners_path"))
        codeowners_ok = (not codeowners_required) or codeowners_present

        direct = set(e.get("direct_collaborators") or [])
        covered = set()
        for slug in team_perm:
            covered |= set(rosters.get(slug, []))
        individual_only = sorted(direct - covered)
        collaborators_unassessable = e.get("direct_collaborators") is None

        prot = e.get("protection")
        protection_unassessable = prot is None and "protection_error" in e
        if prot == "none":
            pr_required = approvals_ok = codeowner_review = status_checks = branch_current = no_bypass = False
            min_approvals = 0
            protection_configured = False
        elif isinstance(prot, dict):
            pr_required = bool(prot.get("pr_required"))
            min_approvals = prot.get("min_approvals") or 0
            approvals_ok = min_approvals >= 1
            codeowner_review = bool(prot.get("codeowner_review"))
            status_checks = bool(prot.get("status_checks_required"))
            branch_current = bool(prot.get("branch_must_be_current"))
            no_bypass = bool(prot.get("enforce_admins"))
            protection_configured = True
        else:
            pr_required = approvals_ok = codeowner_review = status_checks = branch_current = no_bypass = None
            min_approvals = None
            protection_configured = None

        squash_only = bool(e.get("allow_squash_merge")) and not e.get("allow_merge_commit") and not e.get("allow_rebase_merge")
        pr_title_default = (e.get("squash_merge_commit_title") == "PR_TITLE") and (e.get("squash_merge_commit_message") == "PR_BODY")
        auto_delete = bool(e.get("delete_branch_on_merge"))

        visibility = e.get("visibility")
        internal_ok = visibility == "internal"
        default_branch = e.get("default_branch")
        branch_main_ok = default_branch == "main"

        confidence = "Full"
        if protection_unassessable or collaborators_unassessable:
            confidence = "Partial (insufficient GitHub access to check everything)"

        gaps = []
        if not internal_ok: gaps.append(f"visibility={visibility}")
        if not branch_main_ok: gaps.append(f"default branch={default_branch}")
        if not owning_team_ok: gaps.append(f"owning team role={bs_role or 'none'} (not Maintain/Admin)")
        if not org_admin_with_admin: gaps.append("no org-admin team has Admin")
        if individual_only: gaps.append(f"individual grants: {','.join(individual_only)}")
        if not codeowners_ok: gaps.append("CODEOWNERS required but missing")
        if not e.get("codecov_path"): gaps.append("no codecov")
        if protection_unassessable:
            gaps.append("branch protection: not assessable (insufficient access)")
        elif not protection_configured:
            gaps.append("no branch protection")
        else:
            if not approvals_ok: gaps.append("no required approvals")
            if not codeowner_review: gaps.append("no CODEOWNERS review requirement")
            if not status_checks: gaps.append("no required status checks")
            if not no_bypass: gaps.append("admins can bypass")
        if not squash_only: gaps.append("non-squash merges allowed")
        if not pr_title_default: gaps.append("squash message not PR title/body")
        if not auto_delete: gaps.append("no auto-delete branch")

        rows.append({
            "repo": repo,
            "url": e.get("html_url"),
            "owning_team_has_maintain": owning_team_has_maintain,
            "owning_team_ok": owning_team_ok,
            "owning_team_role": bs_role,
            "org_admin_with_admin": org_admin_with_admin,
            "infra_admin": infra_admin,
            "individual_only": individual_only,
            "collaborators_unassessable": collaborators_unassessable,
            "branch_write": branch_write,
            "codeowners_present": codeowners_present,
            "codeowners_required": codeowners_required,
            "codeowners_ok": codeowners_ok,
            "codecov_present": bool(e.get("codecov_path")),
            "visibility": visibility,
            "internal_ok": internal_ok,
            "default_branch": default_branch,
            "branch_main_ok": branch_main_ok,
            "protection_configured": protection_configured,
            "protection_unassessable": protection_unassessable,
            "pr_required": pr_required,
            "min_approvals": min_approvals,
            "approvals_ok": approvals_ok,
            "codeowner_review": codeowner_review,
            "status_checks": status_checks,
            "branch_current": branch_current,
            "no_bypass": no_bypass,
            "squash_only": squash_only,
            "pr_title_default": pr_title_default,
            "auto_delete": auto_delete,
            "confidence": confidence,
            "gaps": gaps,
        })

    json.dump(rows, open(f"{outdir}/final_rows.json", "w"), indent=2)
    for r in rows:
        print(r["repo"], "->", len(r["gaps"]), "gaps [", r["confidence"], "]")


if __name__ == "__main__":
    main()
