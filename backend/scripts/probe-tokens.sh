#!/usr/bin/env bash
# Probe candidate board identifiers against the three Phase 1 ATS APIs.
#
# Board tokens move. An employer switches ATS, renames a board, or publishes
# embed-only, and the seeded identifier starts returning 404 — which the runner
# correctly reports as `adapter.board_not_found` rather than retrying. This
# script is how you find the replacement without editing the seed file blind.
#
# It is deliberately outside the application: no database, no config, no
# adapters. Just the same public endpoints, so the answer is unambiguous.
#
#   ./scripts/probe-tokens.sh greenhouse confluent confluentinc confluent-cloud
#   ./scripts/probe-tokens.sh lever      swiggy swiggy-in bundl
#   ./scripts/probe-tokens.sh ashby      perplexity perplexityai anysphere cursor
#   ./scripts/probe-tokens.sh all        stripe          # try one name everywhere
#
# A 200 with a non-zero job count is a live board. A 200 with zero jobs is
# either a genuinely empty board or a token that resolves but is not the one you
# want — check the URL in a browser before trusting it.
set -uo pipefail

UA='ScoutCareers/1.0 (personal job-search agent; token probe)'

usage() {
  echo "usage: $0 {greenhouse|lever|ashby|all} <candidate> [candidate...]" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
adapter="$1"
shift

probe() {
  local kind="$1" token="$2" url count body status
  case "$kind" in
    greenhouse) url="https://boards-api.greenhouse.io/v1/boards/${token}/jobs?content=true" ;;
    lever)      url="https://api.lever.co/v0/postings/${token}?mode=json" ;;
    ashby)      url="https://api.ashbyhq.com/posting-api/job-board/${token}" ;;
    *)          usage ;;
  esac

  body=$(curl -sS --max-time 20 -A "$UA" -w $'\n%{http_code}' "$url" 2>/dev/null) || {
    printf '  %-11s %-24s %s\n' "$kind" "$token" "network error"
    return
  }
  status="${body##*$'\n'}"
  body="${body%$'\n'*}"

  if [[ "$status" != "200" ]]; then
    printf '  %-11s %-24s HTTP %s\n' "$kind" "$token" "$status"
    return
  fi

  # Count postings without needing jq: greenhouse/ashby wrap in {"jobs": [...]},
  # lever returns a bare array.
  count=$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("unparseable"); raise SystemExit
jobs = data.get("jobs", data) if isinstance(data, dict) else data
print(len(jobs) if isinstance(jobs, list) else "?")
' 2>/dev/null || echo "?")

  if [[ "$count" == "0" ]]; then
    printf '  %-11s %-24s HTTP 200 — but 0 postings (empty or wrong board)\n' "$kind" "$token"
  else
    printf '  %-11s %-24s HTTP 200 — %s postings  ✓\n' "$kind" "$token" "$count"
  fi
}

echo "Probing ${#} candidate(s):"
for token in "$@"; do
  if [[ "$adapter" == "all" ]]; then
    for kind in greenhouse lever ashby; do probe "$kind" "$token"; done
  else
    probe "$adapter" "$token"
  fi
done
