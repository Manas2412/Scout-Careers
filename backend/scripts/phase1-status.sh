#!/usr/bin/env bash
#
# What the database says about the Phase 1 exit gates.
#
# The run summary reports what one run did. This reports the standing state, and
# the two are not the same question: "0 new this run" is not "dedup is correct",
# and a gate is closed by what is in the table, not by what scrolled past.
#
# Read-only. Every statement is a SELECT.
#
# Usage:  bash scripts/phase1-status.sh

set -euo pipefail

DB="${SCOUT_DB_CONTAINER:-scout-postgres}"

if ! docker exec "$DB" pg_isready -U scout -d scout >/dev/null 2>&1; then
  echo "error: $DB is not answering. Try: docker compose up -d postgres" >&2
  exit 1
fi

q() { docker exec -i "$DB" psql -U scout -d scout -qAX -c "$1"; }

echo
echo "═══ Postings held ═════════════════════════════════════════════════════"
q "SELECT count(*) FILTER (WHERE closed_at IS NULL AND NOT filtered_out) AS visible,
          count(*) FILTER (WHERE closed_at IS NOT NULL)                  AS closed,
          count(*) FILTER (WHERE filtered_out)                           AS filtered,
          count(*)                                                       AS total
   FROM job_posting;"

echo
echo "═══ Gate 1.4 · a re-run creates no duplicates ═════════════════════════"
echo "Same (source, external_id) stored twice — the unique constraint makes this"
echo "impossible, so a non-zero count means the constraint is gone:"
q "SELECT count(*) AS duplicate_identities FROM (
     SELECT source_id, external_id FROM job_posting
     GROUP BY 1, 2 HAVING count(*) > 1
   ) d;"

echo "Last three runs (new should be 0 on a re-run with nothing changed upstream):"
q "SELECT id, status,
          stats->>'fetched' AS fetched, stats->>'new' AS new,
          stats->>'updated' AS upd,     stats->>'unchanged' AS same,
          stats->>'superseded' AS sup,  stats->>'closed' AS closed
   FROM run_log ORDER BY started_at DESC LIMIT 3;"

echo
echo "═══ Dedup · is a superseded row really a duplicate? ═══════════════════"
echo "same_text=f means two postings with different descriptions were collapsed."
echo "Before the same_role fix this read 685 f / 37 t. It should now be 0 f:"
q "SELECT (l.content_hash = w.content_hash) AS same_text,
          (l.source_id    = w.source_id)    AS same_source,
          count(*)
   FROM job_posting l
   JOIN job_posting w ON w.id = replace(l.filter_reason, 'superseded_by:', '')
   WHERE l.filter_reason LIKE 'superseded_by:%'
   GROUP BY 1, 2 ORDER BY 3 DESC;"

echo
echo "═══ Gate 1.5 · the two-run close rule ═════════════════════════════════"
q "SELECT missed_runs, count(*) FILTER (WHERE closed_at IS NULL) AS still_open,
          count(*) FILTER (WHERE closed_at IS NOT NULL) AS closed
   FROM job_posting GROUP BY 1 ORDER BY 1;"

echo
echo "═══ Gate 1.7 · the alert path ═════════════════════════════════════════"
echo "Postings from a mail_alert source, and zero requests to any LinkedIn host."
echo "(The never-scrape half is asserted by the invariant tests; this is the"
echo "'produces postings' half, which needs an alert email to have arrived.)"
q "SELECT s.id, s.last_status, s.consecutive_failures,
          count(p.id) FILTER (WHERE p.closed_at IS NULL) AS open_leads,
          count(DISTINCT p.company_id)                   AS across_companies
   FROM source s LEFT JOIN job_posting p ON p.source_id = s.id
   WHERE s.adapter = 'mail_alert'
   GROUP BY 1, 2, 3;"

echo "Leads that could not be attributed to a tracked company:"
q "SELECT coalesce(c.slug, '(none)') AS company, count(*)
   FROM job_posting p JOIN company c ON c.id = p.company_id
   WHERE c.slug = 'unmatched' GROUP BY 1;"

echo
echo "═══ Gate 1.1 · scheduled runs ═════════════════════════════════════════"
echo "run_type=discovery rows on three consecutive days closes it:"
# `max(status)` was wrong here and misreported: run_status is an ordered enum,
# so max() returned the worst status of the day, not the latest run's. Seven
# runs ending in `completed` read as `completed_with_errors`. DISTINCT ON gives
# the actual last one.
q "SELECT d.day, d.run_type, d.runs, l.status AS last_status, l.id AS last_run
   FROM (
     SELECT date_trunc('day', started_at)::date AS day, run_type, count(*) AS runs,
            max(started_at) AS last_at
     FROM run_log GROUP BY 1, 2
   ) d
   JOIN run_log l ON l.started_at = d.last_at
   ORDER BY d.day DESC LIMIT 7;"

echo
echo "═══ Sources needing attention ═════════════════════════════════════════"
q "SELECT s.id, c.slug, s.adapter, s.last_status, s.consecutive_failures, s.enabled
   FROM source s JOIN company c ON c.id = s.company_id
   WHERE s.last_status NOT IN ('ok') OR NOT s.enabled
   ORDER BY s.consecutive_failures DESC, s.id;"
echo
