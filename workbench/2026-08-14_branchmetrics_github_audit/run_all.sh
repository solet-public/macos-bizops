#!/usr/bin/env bash
# Regenerate the BranchMetrics GitHub repo compliance report end-to-end.
# See README.md for prerequisites (gh auth, SSO authorization, scopes).
set -euo pipefail
cd "$(dirname "$0")"

OUTDIR="run_$(date +%F)"
mkdir -p "$OUTDIR"

echo "== 1/5 discovering candidate repos ==" >&2
python3 discover_repos.py > "$OUTDIR/repos.json"
cat "$OUTDIR/repos.json" >&2

echo "== 2/5 auditing GitHub ==" >&2
python3 audit_github.py "$OUTDIR/repos.json" "$OUTDIR"

echo "== 3/5 analyzing ==" >&2
python3 analyze.py "$OUTDIR"

echo "== 4/5 generating CSVs ==" >&2
python3 generate_csvs.py "$OUTDIR"

echo "== 5/5 creating the sheet ==" >&2
python3 create_sheet.py "$OUTDIR" "$(date +%F)"
