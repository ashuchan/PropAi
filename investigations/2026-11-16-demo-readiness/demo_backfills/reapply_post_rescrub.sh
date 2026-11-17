#!/bin/zsh
# Second half of reapply_after_merge.sh, for a day when the merge's own rescrub failed and was re-run by hand
# (2026-11-17): the shift, outcome seed and Remy/lessons already ran; this regenerates the 7/90-day briefs from the
# rescrubbed text, runs A9 over them (household wording + intent rule; no-op on rows the job's A9 already did),
# then the Low-intent overlay and the second runs. Run only after the rescrub owner says "rescrub done".
set -e
H=$(TZ=America/Chicago date +%H%M); if [[ $H -ge 0630 && $H -lt 0710 ]]; then echo "merge window: not now"; exit 2; fi
cd "$(dirname "$0")"
U=~/ontour-private-evidence/2026-11-17-data-sufficiency/undo; R=~/ontour-private-evidence/2026-11-17-data-sufficiency/raw; T=$(date +%H%M%S)
PY=~/aiservice-tier0/.venv/bin/python; OPSPY=~/ontour-ops-wt/agent-security/.venv/bin/python
S=/private/tmp/claude-501/-Users-ankur-PropAi-main--claude-worktrees-confident-solomon-298d80/328c9e97-cf0f-4660-a147-639fda1d7afc/scratchpad
ts() { TZ=America/Chicago date +%H:%M:%S; }
echo "== 0 shift still in place (expect nothing to do) $(ts)"; $PY shift_july_tours.py | tail -1
echo "== 4 learning briefs, 5 communities in parallel $(ts)"
for c in "Cedar Point" "Willow Run" Stonebridge "Maple Grove" Ivywood; do k=${c// /}
  ./run_briefs.sh --only "$c" --commit --undo $U/undo-briefs-$k-$T.json > $R/briefs-$k-$T.log 2>&1 &
done; wait; tail -qn1 $R/briefs-*-$T.log
echo "== 5 A9 $(ts)"
(cd ~/PropAi-main/investigations/2026-11-16-demo-readiness/demo_scrub && $OPSPY a9_cli.py --commit --undo $U/undo-a9-$T.jsonl | tail -1 | cut -c1-600)
echo "== 6 low-intent overlay $(ts)"; $PY low_intent_outcomes.py --commit --undo $U/undo-low-intent-$T.json
echo "== 6b matured-tours fill (ledger re-apply + top-up) $(ts)"; $PY matured_tours_fill.py --commit --undo $U/undo-matured-fill-$T.json | /usr/bin/grep -v '"associate"'
echo "== 7 second runs, expect 0 $(ts)"; $PY shift_july_tours.py | tail -1; $PY low_intent_outcomes.py | head -1; $PY matured_tours_fill.py | head -1
$S/qw.sh -f a6_remy_willow_run.sql; $S/qw.sh -t -f a7_coaching_assignments.sql | /usr/bin/grep INSERT
(cd ~/PropAi-main/investigations/2026-11-16-demo-readiness/demo_scrub && $OPSPY a9_cli.py | tail -1 | cut -c1-600)
echo "== done $(ts)"
