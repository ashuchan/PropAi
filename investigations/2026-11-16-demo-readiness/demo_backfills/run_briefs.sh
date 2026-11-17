#!/bin/zsh
# usage: run_briefs.sh [tm_briefs_backfill.py args]  (staging only; credentials read at run time, never stored)
cd "$(dirname "$0")"
eval "$(~/ontour-ops-wt/agent-security/.venv/bin/python - ~/ontour-ops-wt/staging-merge-rescrub <<'PY'
import sys, shlex; sys.path.insert(0, sys.argv[1])
from jobs.demo_sync.config import load_db
c = load_db('staging')
print(' '.join(f'export {k}={shlex.quote(str(v))};' for k, v in dict(DB_HOST=c['host'], DB_PORT=c['port'], DB_NAME=c['dbname'], DB_USER=c['user'], DB_PASSWORD=c['password']).items()))
PY
)"
export AISERVICE_REPO=~/ontour-private-evidence/2026-11-16-demo-readiness/aisvc-staging GEMINI_NARRATIVE_MODEL=gemini-3.5-flash VERTEX_AI_LOCATION=global CLOUDSDK_ACTIVE_CONFIG_NAME=claude-ontour GOOGLE_CLOUD_PROJECT=ontour-485600
export GCLOUD_TOKEN="$(gcloud auth print-access-token --account=ankur@ontour.ai 2>/dev/null)"
exec ~/aiservice-tier0/.venv/bin/python tm_briefs_backfill.py "$@"
