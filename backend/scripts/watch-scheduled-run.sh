#!/usr/bin/env bash
#
# Watch the scheduler fire a discovery run by itself.
#
# Gate 1.1 asks for three consecutive *scheduled* runs. On a daily cron that is
# three days, and there is no way to compress it without changing what is being
# proved — the gate is partly about the process surviving overnight. What this
# script proves is the half you can check in two minutes: that the scheduler
# registers the job, fires it on its own trigger with nobody typing a command,
# takes the Redis run lock, and records a `run_log` row.
#
# It changes no file. `DISCOVERY_CRON_HOUR` / `DISCOVERY_CRON_MINUTE` are passed
# as environment variables, which pydantic-settings ranks above .env, so the
# override lives exactly as long as this process does. Ctrl-C, and 08:00 is back
# without anything to undo — which is the point: a test that edits your .env can
# leave your schedule wrong hours later.
#
# Usage:
#   bash scripts/watch-scheduled-run.sh          # fire ~2 minutes from now
#   bash scripts/watch-scheduled-run.sh 5        # fire ~5 minutes from now

set -euo pipefail

MINUTES="${1:-2}"
TZ_NAME="${TZ:-Asia/Kolkata}"

if ! [[ "$MINUTES" =~ ^[0-9]+$ ]] || [ "$MINUTES" -lt 1 ] || [ "$MINUTES" -gt 59 ]; then
  echo "error: minutes must be 1-59 (got '$MINUTES')" >&2
  exit 2
fi

# The cron is hour+minute, so a fire time is only reachable inside this hour.
# Rolling over the top of the hour would mean waiting nearly sixty minutes for a
# two-minute test, so refuse and say why rather than appear to hang.
FIRE_EPOCH=$(( $(date +%s) + MINUTES * 60 ))
FIRE_HOUR=$(TZ="$TZ_NAME" date -r "$FIRE_EPOCH" +%H 2>/dev/null || TZ="$TZ_NAME" date -d "@$FIRE_EPOCH" +%H)
FIRE_MIN=$(TZ="$TZ_NAME" date -r "$FIRE_EPOCH" +%M 2>/dev/null || TZ="$TZ_NAME" date -d "@$FIRE_EPOCH" +%M)
NOW_HOUR=$(TZ="$TZ_NAME" date +%H)

if [ "$FIRE_HOUR" != "$NOW_HOUR" ]; then
  echo "error: +${MINUTES}m crosses the top of the hour, and the cron is hour+minute."
  echo "       Wait a few minutes and re-run, or pass a smaller number."
  exit 2
fi

cat <<EOF

  Scheduler test
  --------------
  timezone      $TZ_NAME
  now           $(TZ="$TZ_NAME" date +%H:%M:%S)
  will fire at  ${FIRE_HOUR}:${FIRE_MIN}:00   (in ~${MINUTES}m)

  Watch for, in order:
    scheduler_started      jobs=['discovery_daily']
    run_started            — a run_log row, unprompted
    scheduled_run_finished job_id=discovery_daily

  There is no "lock acquired" line, and its absence is not a failure: the lock
  is taken before the run begins, and a failure to take it raises
  RunLockUnavailable and refuses the run. A run that started at all held the
  lock. (An earlier version of this script said to watch for one, which was
  wrong — it promised a log line the code does not emit.)

  A run reporting sources=0 is also correct if you ran discovery in the last
  24h: nothing is due, and a scheduled run is supposed to respect that. This
  test proves the trigger fires, not that the pipeline works — only
  'run discovery --force' proves the pipeline.

  Nobody types anything to make it happen. That is the whole test.
  Ctrl-C when it finishes; the 08:00 schedule is untouched.

EOF

# Redis holds the run lock, and without it the run is *refused*, not degraded:
# `_default_lock` raises RunLockUnavailable when there is no client. Checking
# here means a stopped container reads as a stopped container rather than as a
# scheduler that fired and then mysteriously refused itself.
if ! docker exec scout-redis redis-cli ping >/dev/null 2>&1; then
  echo "  !! scout-redis is not answering. Start it first:"
  echo "       docker compose up -d redis"
  echo
  exit 1
fi

exec env \
  SCHEDULER_ENABLED=true \
  DISCOVERY_CRON_HOUR="$FIRE_HOUR" \
  DISCOVERY_CRON_MINUTE="$FIRE_MIN" \
  scout-careers scheduler start
