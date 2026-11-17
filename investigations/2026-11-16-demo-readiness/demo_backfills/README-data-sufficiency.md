# 2026-11-17 data sufficiency (staging demo tenant 3360791e)

Fixes from the 11-17 coverage matrix (artifact https://claude.ai/artifact/S8pjvBFw72cdVHs1NhPfnk). Staging only, demo company only.
Every tool writes its undo file first (0600, under ~/ontour-private-evidence/2026-11-17-data-sufficiency/undo/), and a second run changes 0 rows.

| Tool | What | Survives the 06:30 merge? |
|---|---|---|
| `shift_july_tours.py` | Moves the untyped Feb–Aug 2026 demo tours (1,491) +77 days (11 weeks) and relabels them by the classifier's hard rules (consent_declined / short_tour <10 min / full_tour). Fills the Sep–Oct gap for 5 communities. | No: the merge rewrites tours, tour_analytics, tour_sentiment_signals, prospect_profiles, followup_edits, tour_outcome_records. Staging-only children are shifted once, recorded in `demo_seed.history_shift_ledger`. |
| `jobs.demo_outcome_seed` (ontour-ops) | Re-seeds `demo_seed.tour_outcomes` from the new tour days | yes (demo_seed) |
| `low_intent_outcomes.py` | ~9% of Low-intent tours applied (Low never 0%) | yes, but re-run after the outcome seed |
| `demo_gold_rescale.py` + `units_seed.py --replace` | Prestige Park rents into the $1.75–1.95k band; its Units seed rebuilt | yes (gold/Units are not merged) |
| `a6_remy_willow_run.sql` | Remy Reed → Willow Run mapping (fixes a tour-review 404) | No (associate_community_map is merged) |
| `a7_coaching_assignments.sql` | 12 library lesson assignments, the most active associate per community | yes (tm_lesson_assignments is staging-only) |
| `tm_briefs_backfill.py` / `run_briefs.sh` | Learning hero briefs + tour-summary prose for 7/90 days, via aiservice's own generator (gemini-3.5-flash, ~$0.3/run). Prestige Park is skipped: aiservice hard-excludes that community name as a test community. | No (tm_narrative_briefs is merged) |
| `reapply_after_merge.sh` | Runs everything above that the merge reverts, in order. Refuses 06:30–07:10 CT. | until ontour-ops runs it post-merge |

## demo_seed.history_shift_ledger
`tour_id uuid PRIMARY KEY, days integer NOT NULL, shifted_at timestamptz NOT NULL DEFAULT now()`, with a table comment. SELECT is granted to appuser.
One row per tour whose staging-only child rows (tour_events, tour_status_updates, notifications, …) were moved. The merge never touches demo_seed, so the shift re-applies only the merge-rewritten tables after each merge.
