#!/usr/bin/env bash
#
# A stand-in for your real non-prod deployment, used by FOREMAN_DEPLOY_MODE=simulated.
#
# It exists so the whole loop can be rehearsed — including the wait, the
# verdict and the failure path — before anyone points Foreman at a pipeline
# that touches a real environment. Replace it, or switch the mode to
# github-actions or webhook, when you are ready.
#
# Foreman passes:
#   FOREMAN_TICKET  FOREMAN_BRANCH  FOREMAN_SHA  FOREMAN_ENV  FOREMAN_WORKDIR
#
# Print `DEPLOY_URL=<url>` on stdout and Foreman links it on the ticket.
# Exit non-zero and the ticket stops with the deployment marked failed.

set -euo pipefail

TICKET="${FOREMAN_TICKET:-unknown}"
BRANCH="${FOREMAN_BRANCH:-unknown}"
SHA="${FOREMAN_SHA:-unknown}"
ENVIRONMENT="${FOREMAN_ENV:-nonprod}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo "[deploy] ticket      : $TICKET"
echo "[deploy] branch      : $BRANCH"
echo "[deploy] commit      : ${SHA:0:12}"
echo "[deploy] environment : $ENVIRONMENT"
echo "[deploy] building image…"
sleep 1
echo "[deploy] pushing to registry…"
sleep 1
echo "[deploy] rolling out…"
sleep 1
echo "[deploy] health check: 200 OK"
echo "[deploy] complete at $STAMP"

# A record on disk, so a rehearsal leaves the same kind of evidence trail a
# real deployment would.
RECORD_DIR="${FOREMAN_WORKDIR:-/tmp}/deployments"
mkdir -p "$RECORD_DIR"
printf '{"ticket":"%s","branch":"%s","sha":"%s","env":"%s","at":"%s","result":"success"}\n' \
  "$TICKET" "$BRANCH" "$SHA" "$ENVIRONMENT" "$STAMP" \
  > "$RECORD_DIR/$TICKET-$STAMP.json"

echo "DEPLOY_URL=file://$RECORD_DIR/$TICKET-$STAMP.json"
