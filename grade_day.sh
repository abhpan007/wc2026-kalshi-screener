#!/usr/bin/env bash
# Shadow-mode grading helper.
#
#   bash grade_day.sh YYYY-MM-DD
#
# First run for a date: downloads that day's report from S3 and writes a
# results.csv pre-filled with the match_ids (you just type the scores).
# Second run: grades the CSV (predicted vs actual calibration + any edge PnL).
#
# Override the bucket/region with SCREENER_S3_BUCKET / AWS_REGION if needed.
set -euo pipefail

DAY="${1:-}"
if [[ -z "$DAY" ]]; then
  echo "usage: bash grade_day.sh YYYY-MM-DD   (or: bash grade_day.sh all)"
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Aggregate across every already-graded day (no download needed).
if [[ "$DAY" == "all" ]]; then
  uv run python -m screener.grade --all
  exit $?
fi
BUCKET="${SCREENER_S3_BUCKET:-wc2026screenerstack-outputbucket7114eb27-iwgxnhcb6nap}"
REGION="${AWS_REGION:-us-east-1}"
DIR="output/date=$DAY"
REPORT="$DIR/report.json"
RESULTS="$DIR/results.csv"
mkdir -p "$DIR"

echo "↓ Downloading report for $DAY from s3://$BUCKET ..."
if ! aws s3 cp "s3://$BUCKET/date=$DAY/report.json" "$REPORT" --region "$REGION" >/dev/null 2>&1; then
  echo "✗ No report found in S3 for $DAY (did the screener run that day?)."
  exit 1
fi

if [[ -f "$RESULTS" ]]; then
  echo "✓ Results file found — grading $DAY"
  echo
  uv run python -m screener.grade --date "$DAY" --results "$RESULTS"
else
  echo "✎ Creating results template from the report..."
  uv run python - "$REPORT" "$RESULTS" <<'PY'
import json, sys, csv
report, out = sys.argv[1], sys.argv[2]
r = json.load(open(report))
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["match_id", "matchup", "home_score", "away_score", "ht_home", "ht_away", "advanced"])
    for m in r["matches"]:
        mt = m["match"]
        w.writerow([mt["match_id"], f"{mt['home']['name']} vs {mt['away']['name']}", "", "", "", "", ""])
PY
  echo
  echo "➜ Fill in home_score / away_score (90-min score) in:  $RESULTS"
  echo "  (ht_home/ht_away optional — for first-half markets; advanced=home/away"
  echo "   optional — for knockout 'to advance' markets, who went through)"
  echo "  Then re-run:  bash grade_day.sh $DAY"
fi
