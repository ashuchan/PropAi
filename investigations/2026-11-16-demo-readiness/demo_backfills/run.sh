#!/usr/bin/env bash
# Staging demo backfills. Credentials are read at run time (ontour-ops staging config + gcloud token), never stored.
#   run.sh evidence <sample|all> [--apply --undo <new file>]   coaching-note evidence (coaching_evidence_backfill.py)
# Candidate lists live in the private folder ($DEMO_PRIVATE/evidence_candidates.json: {"sample": [...], "all": [...]}).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEMO_PRIVATE="${DEMO_PRIVATE:-$HOME/ontour-private-evidence/2026-11-16-demo-readiness}"
OPS="${DEMO_SYNC_REPO:-$HOME/ontour-ops-wt/staging-merge-rescrub}"
export AISERVICE_REPO="${AISERVICE_REPO:-$DEMO_PRIVATE/aisvc-staging}"     # aiservice checkout at origin/staging
PY="${AISERVICE_PY:-$HOME/aiservice-tier0/.venv/bin/python}"                 # has google-genai + psycopg2
WHAT="$1"; WHICH="$2"; shift 2
eval "$("$HOME/ontour-ops-wt/agent-security/.venv/bin/python" - "$OPS" <<'PY'
import sys, shlex; sys.path.insert(0, sys.argv[1])
from jobs.demo_sync.config import load_db
c = load_db('staging')
print(' '.join(f'export {k}={shlex.quote(str(v))};' for k, v in dict(DB_HOST=c['host'], DB_PORT=c['port'], DB_NAME=c['dbname'], DB_USER=c['user'], DB_PASSWORD=c['password']).items()))
PY
)"
export CLOUDSDK_ACTIVE_CONFIG_NAME="${CLOUDSDK_ACTIVE_CONFIG_NAME:-claude-ontour}" GOOGLE_CLOUD_PROJECT=ontour-485600
export GCLOUD_TOKEN="$(gcloud auth print-access-token 2>/dev/null)"
case "$WHAT" in
  evidence) IDS=$(python3 -c "import json,sys; print(' '.join(json.load(open(sys.argv[1]))[sys.argv[2]]))" "$DEMO_PRIVATE/evidence_candidates.json" "$WHICH")
            "$PY" "$HERE/coaching_evidence_backfill.py" --tour-ids $IDS "$@" ;;
  *) echo "unknown backfill $WHAT"; exit 2 ;;
esac
