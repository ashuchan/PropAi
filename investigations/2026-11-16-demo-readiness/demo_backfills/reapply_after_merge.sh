#!/bin/zsh
# Re-apply the 2026-11-17 data-sufficiency fixes after the 06:30 CT staging merge (until the ontour-ops post-merge
# step does it). Staging + demo company only; every step is idempotent and writes its own undo file first.
# Never run between 06:30 and 07:10 CT.
set -e
H=$(TZ=America/Chicago date +%H%M); if [[ $H -ge 0630 && $H -lt 0710 ]]; then echo "merge window: not now"; exit 2; fi
cd "$(dirname "$0")"
U=~/ontour-private-evidence/2026-11-17-data-sufficiency/undo; T=$(date +%H%M%S)
PY=~/aiservice-tier0/.venv/bin/python
S=/private/tmp/claude-501/-Users-ankur-PropAi-main--claude-worktrees-confident-solomon-298d80/328c9e97-cf0f-4660-a147-639fda1d7afc/scratchpad
echo "== 1 history shift";      $PY shift_july_tours.py --commit --undo $U/undo-shift-$T.json | tail -2
echo "== 2 outcome seed";       (umask 077; $S/q.sh -c "\copy (select * from demo_seed.tour_outcomes) to '$U/undo-tour-outcomes-$T.csv' csv header")
(cd ~/ontour-ops-wt/staging-merge-rescrub && ~/ontour-ops-wt/agent-security/.venv/bin/python -m jobs.demo_outcome_seed --commit | python3 -c "import json,sys;d=json.load(sys.stdin);print({k:d[k] for k in ('outcome','updated','inserted')})")
echo "== 3 low intent";         $PY low_intent_outcomes.py --commit --undo $U/undo-low-intent-$T.json
echo "== 4 remy + lessons";     $S/qw.sh -f a6_remy_willow_run.sql; $S/qw.sh -t -f a7_coaching_assignments.sql | tail -2
echo "== 5 learning briefs (5 communities in parallel)"
for c in "Cedar Point" "Willow Run" Stonebridge "Maple Grove" Ivywood; do k=${c// /}
  ./run_briefs.sh --only "$c" --commit --undo $U/undo-briefs-$k-$T.json > ~/ontour-private-evidence/2026-11-17-data-sufficiency/raw/briefs-$k-$T.log 2>&1 &
done; wait; tail -qn1 ~/ontour-private-evidence/2026-11-17-data-sufficiency/raw/briefs-*-$T.log
echo "== second runs (expect 0)"; $PY shift_july_tours.py | tail -1; $PY low_intent_outcomes.py | head -1
