#!/usr/bin/env python3
"""Render final_rows.json into the three report CSVs (Summary/Detail/Legend).

Usage: python3 generate_csvs.py <outdir>
"""
import json, csv, sys


def yn(v):
    if v is None:
        return "Unassessable"
    return "Yes" if v else "No"


def main():
    if len(sys.argv) != 2:
        print("usage: generate_csvs.py <outdir>", file=sys.stderr)
        sys.exit(1)
    outdir = sys.argv[1]
    rows = json.load(open(f"{outdir}/final_rows.json"))

    with open(f"{outdir}/tab1_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Repo", "Owning Team", "Compliance Gaps", "Confidence"])
        for r in rows:
            owning = "business-solutions" if r["owning_team_role"] else "None (individual grants only)"
            w.writerow([r["repo"], owning, "; ".join(r["gaps"]) if r["gaps"] else "None found", r["confidence"]])

    with open(f"{outdir}/tab2_detail.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "Repo", "Internal?", "Branch=main?", "Owning Team", "Admin Team", "Infra Admin?",
            "Individual Collabs", "Branch Wr?", "CODEOWNERS?", "CO Required OK?", "Codecov?",
            "PR Required?", ">=1 Approval?", "CO Review?", "Status Checks?", "Branch Current?",
            "No Bypass?", "Squash Only?", "PR Title Default?", "Auto-Del Branch?", "Notes"
        ])
        for r in rows:
            owning = "business-solutions" if r["owning_team_role"] else "None"
            co_required_ok = "N/A (not required)" if not r["codeowners_required"] else yn(r["codeowners_ok"])
            notes = []
            if r["collaborators_unassessable"]:
                notes.append("collaborator list not readable (insufficient access)")
            if r["protection_unassessable"]:
                notes.append("branch protection not readable (insufficient access)")
            elif not r["protection_configured"]:
                notes.append("no branch protection configured at all")
            w.writerow([
                r["repo"], r["visibility"], r["default_branch"], owning,
                ", ".join(r["org_admin_with_admin"]) if r["org_admin_with_admin"] else "None",
                yn(r["infra_admin"]),
                "Unassessable" if r["collaborators_unassessable"] else (", ".join(r["individual_only"]) if r["individual_only"] else "None"),
                yn(r["branch_write"]), yn(r["codeowners_present"]), co_required_ok, yn(r["codecov_present"]),
                yn(r["pr_required"]), yn(r["approvals_ok"]), yn(r["codeowner_review"]), yn(r["status_checks"]),
                yn(r["branch_current"]), yn(r["no_bypass"]), yn(r["squash_only"]), yn(r["pr_title_default"]),
                yn(r["auto_delete"]), "; ".join(notes),
            ])

    legend = [
        ("Repo", "The repository name, as it appears in the BranchMetrics GitHub org."),
        ("Compliance Gaps", "Summary tab only: every requirement below found to be unmet for this repo, in one cell."),
        ("Confidence", "Whether this evaluation had sufficient GitHub API access to check every requirement (branch protection and the collaborator list both require admin-level access to read)."),
        ("Internal?", "Doc: 'Following the principle of Innersource, repositories should have visibility internal rather than private.' Shows the repo's actual visibility."),
        ("Branch=main?", "Doc: 'The default (or base) branch should be named main... Legacy projects still using master may transition at the owner's discretion.' Shows the repo's actual default branch name."),
        ("Owning Team", "Doc Repository Permissions #1: 'Repository must have a responsible team with the Maintain role.' Whether business-solutions holds Maintain. Operator ruling 2026-08-14: business-solutions is not relinquishing Admin/ownership on its own repos to an org-admin team, so an Admin-level grant here is a deliberate stance, not a gap -- Compliance Gaps only flags this team if it holds neither Maintain nor Admin."),
        ("Admin Team", "Doc Repository Permissions #2: 'Repository must grant one of the Org Admin teams (saas-gh-admins, disco-gh-admins, infra-gh-admins) the Admin role.' Lists which of those teams currently holds Admin."),
        ("Infra Admin?", "Doc Repository Permissions #3: 'For occasional support from the Infra team on CI issues the infra-gh-admins team should be granted the Admin role.' (A should, not a must.)"),
        ("Individual Collabs", "Doc Repository Permissions #7: 'Individuals should not be explicitly granted permissions.' Lists any GitHub users with access to this repo not explained by any team grant on the repo -- i.e. a direct/individual add."),
        ("Branch Wr?", "Doc Repository Permissions #4: 'Repository should grant the Branch team the Write role. Do not do this without CODEOWNERS and branch protection rules.' Whether the company-wide 'Branch' team has Write on this repo."),
        ("CODEOWNERS?", "Whether a CODEOWNERS file exists in the repo (checked at CODEOWNERS, .github/CODEOWNERS, and docs/CODEOWNERS)."),
        ("CO Required OK?", "Doc CODEOWNERS section: required when a repo either (a) has more than one team granted Write, or (b) grants Write to the Branch team. Shows whether that requirement, when triggered, is satisfied. 'N/A (not required)' means neither trigger applies to this repo."),
        ("Codecov?", "Doc Code Coverage: 'Repositories should be setup with code coverage integration using Codecov... a codecov.yaml needs to be included in the root of the repository.'"),
        ("PR Required?", "Doc Branch Protection Rules: 'Require a pull request before merging.'"),
        (">=1 Approval?", "Doc Branch Protection Rules: 'Require >= 1 approvals from the owning team before merging.'"),
        ("CO Review?", "Doc Branch Protection Rules: 'Require review from Code Owners.'"),
        ("Status Checks?", "Doc Branch Protection Rules: 'Require status checks to pass before merging' (separate from 'Branch Current?', which is 'up to date before merging')."),
        ("Branch Current?", "Doc Branch Protection Rules: 'Require branches to be up to date before merging.'"),
        ("No Bypass?", "Doc Branch Protection Rules: 'Do not allow bypassing the above settings' -- mapped from GitHub's 'enforce_admins' flag, the closest available proxy."),
        ("Squash Only?", "Doc Pull Requests: 'Only allow squash merging.' Checked as squash-merge allowed AND merge-commit disabled AND rebase-merge disabled."),
        ("PR Title Default?", "Doc Pull Requests: 'Use Default to pull request title and description.' Checked as squash-merge-commit-title=PR_TITLE and squash-merge-commit-message=PR_BODY."),
        ("Auto-Del Branch?", "Doc Pull Requests: 'Automatically delete head branches, with exception for dashboard team which utilizes rc branches.' The dashboard-team exception is not evaluated automatically (no reliable API signal for team-name-to-repo mapping) -- check by hand if a dashboard-owned repo is ever in scope."),
        ("Notes", "Detail tab only: free-text caveats -- most commonly flagging that a repo's collaborator list or branch protection could not be read due to insufficient GitHub access, or that no branch protection is configured at all."),
    ]
    with open(f"{outdir}/tab3_legend.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Column Label", "Full Requirement"])
        for label, desc in legend:
            w.writerow([label, desc])

    print("CSVs written to", outdir)


if __name__ == "__main__":
    main()
