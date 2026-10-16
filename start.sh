#!/usr/bin/env bash
#
# AI-SDLC Foreman — entrypoint. Safe to run from cron.
#
#   ./start.sh                        # advance every matching ticket (DRY RUN)
#   ./start.sh --live                 # actually write to Jira and git
#   ./start.sh --ticket FORE-1        # just one ticket
#   ./start.sh --jql "project = X"    # override the intake query
#   ./start.sh --show FORE-1          # what Foreman knows about a ticket
#   ./start.sh --doctor               # check every dependency and interlock
#   ./start.sh --states               # print the state machine
#
# A ticket advances at most ONE state per run. That keeps every run bounded,
# keeps the board legible while it moves, and means you can stop the pipeline
# between any two stages by stopping the cron.
#
# Cron — every ten minutes during working hours, live:
#   */10 8-19 * * 1-5 /opt/ai-sdlc-foreman/start.sh --live >> /opt/ai-sdlc-foreman/var/cron.log 2>&1
#
# Writing requires BOTH --live AND FOREMAN_WRITE=true in .env. Two independent
# interlocks: neither a stray flag nor a stale variable alone can act on a real
# board.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

ENV_FILE="$HERE/.env"
CONFIG="$HERE/harness/foreman.yaml"
PYTHON="${PYTHON:-python3}"

LIVE=0; TICKETS=(); JQL=""; MAX=""; ACTION="run"; SHOW=(); FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --live)    LIVE=1; shift ;;
    --ticket)  TICKETS+=("${2:-}"); shift 2 ;;
    --jql)     JQL="${2:-}"; shift 2 ;;
    --max)     MAX="${2:-}"; shift 2 ;;
    --env)     ENV_FILE="${2:-}"; shift 2 ;;
    --show)    ACTION="show"; SHOW+=("${2:-}"); shift 2 ;;
    --doctor)  ACTION="doctor"; shift ;;
    --states)  ACTION="states"; shift ;;
    --report)  ACTION="report"; shift ;;
    --force)   FORCE=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# ──────────────────────────────────────────────────────────────────── env ───
if [[ ! -f "$ENV_FILE" ]]; then
  echo "FATAL: $ENV_FILE not found. Copy .env.example to .env and fill it in." >&2
  exit 2
fi

# A world-readable file holding a production API token is a finding in its own
# right, and this one holds three.
PERM="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE" 2>/dev/null)"
if [[ -n "$PERM" && "$PERM" != "600" && "$PERM" != "400" ]]; then
  echo "WARNING: $ENV_FILE is mode $PERM. Run: chmod 600 $ENV_FILE" >&2
fi

set -a; source "$ENV_FILE"; set +a

abspath() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s' "$HERE/${1#./}" ;; esac; }
LOGDIR="$(abspath "${FOREMAN_LOGDIR:-./var/logs}")"
OUTDIR="$(abspath "${FOREMAN_OUTDIR:-./var/out}")"
mkdir -p "$LOGDIR" "$OUTDIR" "$HERE/var"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_LOG="$LOGDIR/run-$RUN_ID.log"

# The package lives under harness/; every invocation runs from there, with
# --root pointing back at the project so policy/, extension/ and var/ resolve.
cd "$HERE/harness" || exit 2

# ────────────────────────────────────────────── one-shot, non-run actions ───
case "$ACTION" in
  doctor)
    exec "$PYTHON" -m foreman --config "$CONFIG" --env "$ENV_FILE" --root "$HERE" doctor
    ;;
  states)
    exec "$PYTHON" -m foreman --config "$CONFIG" --env "$ENV_FILE" --root "$HERE" states
    ;;
  report)
    exec "$PYTHON" -m foreman --config "$CONFIG" --env "$ENV_FILE" --root "$HERE" report
    ;;
  show)
    exec "$PYTHON" -m foreman --config "$CONFIG" --env "$ENV_FILE" --root "$HERE" \
      show "${SHOW[@]}"
    ;;
esac

# ──────────────────────────────────────────────────────────────── locking ───
# Two overlapping runs would both read a ticket at the same state, both advance
# it, and both post a comment. The gate would be evaluated twice against the
# same approval and the build stage would run twice on one ticket.
LOCK="$HERE/var/.foreman.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -n 9 || { echo "another Foreman run is in progress; exiting"; exit 0; }
else
  if [[ -d "$LOCK.d" ]]; then
    AGE=$(( $(date +%s) - $(stat -f %m "$LOCK.d" 2>/dev/null || stat -c %Y "$LOCK.d") ))
    if [[ $AGE -lt 10800 ]]; then
      echo "another Foreman run is in progress (lock ${AGE}s old); exiting"; exit 0
    fi
    rmdir "$LOCK.d" 2>/dev/null
  fi
  mkdir "$LOCK.d" 2>/dev/null || { echo "cannot acquire lock"; exit 0; }
  trap 'rmdir "$LOCK.d" 2>/dev/null' EXIT
fi

# ───────────────────────────────────────────────────────────── interlocks ───
ARGS=(--config "$CONFIG" --env "$ENV_FILE" --root "$HERE" --run-id "$RUN_ID" run)
if [[ $LIVE -eq 1 ]]; then
  if [[ "${FOREMAN_WRITE:-false}" != "true" ]]; then
    echo "NOTE: --live given but FOREMAN_WRITE is '${FOREMAN_WRITE:-false}'."
    echo "      Both interlocks must be set. Continuing as a DRY RUN."
  else
    ARGS+=(--live)
  fi
fi
[[ -n "$JQL" ]] && ARGS+=(--jql "$JQL")
[[ -n "$MAX" ]] && ARGS+=(--max-tickets "$MAX")
[[ $FORCE -eq 1 ]] && ARGS+=(--force)
if [[ ${#TICKETS[@]} -gt 0 ]]; then
  ARGS+=(--ticket "${TICKETS[@]}")
fi
ARGS+=(--out "$OUTDIR/run-$RUN_ID.json")

"$PYTHON" -m foreman "${ARGS[@]}" 2>&1 | tee -a "$RUN_LOG"
RC=${PIPESTATUS[0]}

echo
echo "log: $RUN_LOG"

# A run that completed is a success even when every ticket is parked at a gate
# waiting for a human. Exit non-zero only when the harness itself could not run.
exit $RC
