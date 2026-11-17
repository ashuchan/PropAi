"""STAGING demo tenant (Paige Apartments, 3360791e): move the untyped Feb-Aug 2026 demo tours forward 11 weeks
(Jul -> Sep 16 - Oct 25, Feb-Jun -> May 10 - Sep 15), so the five demo communities that start on Oct 12 have a prior window and a 90-day window.

Why (2026-11-17 data-sufficiency matrix): Cedar Point, Willow Run, Stonebridge, Maple Grove and Ivywood have no Full
tours before Oct 12, so "vs prior" reads +100-338%, lease-intent outcomes have no 30-day-old tours, and 90D ~ 30D.
The 275 July tours (all analysed, tour_type NULL because they predate the classifier) are real demo tours of the same
communities and associates; moving them is the honest fill ("copy or shift existing demo tours").

What it does, in ONE transaction (dry run unless --commit):
  * selects company 3360791e tours at the six demo communities with start_time in [2026-07-01, 2026-08-10) and
    tour_type IS NULL and a tour_analytics row (the selection is empty after a run: idempotent);
  * adds exactly 77 days (11 weeks: weekday kept) to every timestamp of those tours and their tour-keyed rows
    (TABLES below), each tour keeps its associate, community, guest, transcript and analysis;
  * sets tour_type with the classifier's hard rules (aiservice agents/tour_classifier.py): consent not given ->
    consent_declined; under 10 minutes -> short_tour; otherwise full_tour;
  * deletes the six communities' CM rollup rows (cm_rollup_*), which the dashboard API recomputes (the 06:30 merge
    does the same).
Undo (written first, 0600, never overwritten): the tour ids with their original tour_type, plus the deleted rollup rows.
Staging-only child tables (LOCAL) are shifted once per tour, recorded in demo_seed.history_shift_ledger; the merge
never rewrites them, so a re-run after the merge shifts only the merge-rewritten tables again.
Undo run: --undo-from <file> subtracts the 77 days from the same rows and restores tour_type (rollups recompute).
Persistence: the 06:30 staging merge rewrites these tours from PROD (original dates), so this must run after every
merge (A9 post-merge step) - until then, re-run it after 07:10 CT. Then re-run jobs.demo_outcome_seed so the synthetic
outcomes follow the new tour days.
Staging only: refuses any database not named ontour_db_staging; company 3360791e only.
"""
import argparse, datetime, json, os, sys
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from jobs.demo_sync.config import load_db

COMPANY = '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec'
COMMUNITIES = ('cad6d9d4-5881-40ed-9727-73b60ad1a375', 'f585b747-ff97-4d43-987b-1e0882f029eb', '24589e94-c8a1-419d-95fe-314a0aa14332',
               '85ec7ed8-5d5f-49cb-af16-e8c34aced869', '076a07bd-beec-4e8b-85a4-afee64fd6ab4', '9254d356-a920-5e47-8793-e5f1808c4d41')
DAYS = 77
WINDOW = ('2026-02-01', '2026-08-10')   # the whole untyped pre-classifier history (Feb-Aug 2026)
# (schema.table, timestamp/date columns) keyed by tour_id.
# MERGED: the 06:30 merge rewrites these from PROD (policy.json), so they are shifted on every run.
MERGED = [('public.tour_analytics', ['created_at', 'updated_at']), ('public.tour_sentiment_signals', ['created_at']),
          ('public.prospect_profiles', ['created_at']), ('public.followup_edits', ['updated_at']),
          ('public.tour_outcome_records', ['recorded_at', 'updated_at'])]
# LOCAL: staging-only rows the merge never rewrites; shifted ONCE per tour, tracked in demo_seed.history_shift_ledger.
# (demo_seed.tour_outcomes is not shifted: jobs.demo_outcome_seed recomputes it from the tour day.)
LOCAL = [('public.tour_events', ['created_at']), ('public.tour_status_updates', ['created_at']),
         ('public.tours_event_log', ['created_at', 'event_timestamp']), ('public.tour_notifications', ['sent_at']),
         ('public.tour_availability_interactions', ['created_at', 'clicked_at']), ('public.pipeline_metrics', ['created_at']),
         ('public.tours_failure_handler', ['last_attempt_timestamp']),
         ('canon.app_tour', ['started_at', 'ended_at', 'source_updated_at', 'local_date']),
         ('demo_seed.tour_unit_shown', ['shown_at']), ('demo_seed.tour_unit_reactions', ['said_at']),
         ('demo_seed.tour_lease_unit', ['lease_signed_on'])]
LEDGER_DDL = """CREATE TABLE IF NOT EXISTS demo_seed.history_shift_ledger (
    tour_id    uuid PRIMARY KEY,
    days       integer NOT NULL,
    shifted_at timestamptz NOT NULL DEFAULT now());
COMMENT ON TABLE demo_seed.history_shift_ledger IS
    'Staging demo tenant only: tours whose staging-only child rows were moved by the 11-week history shift (demo_backfills/shift_july_tours.py). Never real data.';"""
ROLLUPS = ('cm_rollup_associate_day', 'cm_rollup_category_day', 'cm_rollup_days', 'cm_rollup_watermarks')


def connect():
    c = load_db('staging')
    conn = psycopg2.connect(host=c['host'], port=c['port'], dbname=c['dbname'], user=c['user'], password=c['password'],
                            options='-c statement_timeout=120000 -c lock_timeout=15000', application_name='shift_july_tours')
    with conn.cursor() as cur:
        cur.execute('select current_database()'); db = cur.fetchone()[0]
    if db != 'ontour_db_staging':
        raise SystemExit(f'refusing database {db}')
    return conn


def existing(cur, table, cols):
    s, t = table.split('.')
    cur.execute("select column_name from information_schema.columns where table_schema=%s and table_name=%s", (s, t))
    have = {r['column_name'] for r in cur.fetchall()}
    return [c for c in cols if c in have] if 'tour_id' in have else []


def _move(cur, tables, ids, days, counts):
    if not ids: return
    for table, cols in tables:
        cols = existing(cur, table, cols)
        if not cols: continue
        sets = ', '.join(f"{c}={c}+interval '{int(days)} days'" if c not in ('local_date', 'lease_signed_on') else f'{c}={c}+{int(days)}' for c in cols)
        cur.execute(f'update {table} set {sets} where tour_id::text = any(%s::text[])', (ids,))
        counts[table] = counts.get(table, 0) + cur.rowcount


def shift(cur, ids, days, counts, local_ids):
    """Merge-rewritten tables for every id; staging-only tables for local_ids only (see LOCAL)."""
    iv = f"interval '{int(days)} days'"
    cur.execute(f"""update public.tours set start_time=start_time+{iv}, end_time=end_time+{iv}, created_at=created_at+{iv},
                    updated_at=updated_at+{iv} where tour_id = any(%s::uuid[]) and company_id=%s""", (ids, COMPANY))
    counts['public.tours'] = cur.rowcount
    _move(cur, MERGED, ids, days, counts)
    _move(cur, LOCAL, local_ids, days, counts)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo', help='new undo file (required with --commit)')
    ap.add_argument('--undo-from', help='apply this undo file instead'); a = ap.parse_args()
    conn = connect(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select name from companies where id=%s", (COMPANY,)); assert cur.fetchone()['name'] == 'Paige Apartments'
    counts = {}
    if a.undo_from:
        u = json.load(open(a.undo_from)); ids = [t['tour_id'] for t in u['tours']]
        # only rows still shifted (start_time >= Sep) are moved back: re-running the undo changes nothing
        # only rows still shifted (they carry a tour_type; the originals have none) move back: re-running changes nothing
        cur.execute("select tour_id::text from tours where tour_id=any(%s::uuid[]) and tour_type is not null and company_id=%s", (ids, COMPANY))
        live = [r['tour_id'] for r in cur.fetchall()]
        cur.execute('select tour_id::text from demo_seed.history_shift_ledger where tour_id=any(%s::uuid[])', (ids,))
        led = [r['tour_id'] for r in cur.fetchall()]
        shift(cur, live, -DAYS, counts, led)
        cur.execute('delete from demo_seed.history_shift_ledger where tour_id=any(%s::uuid[])', (led,))
        for t in u['tours']:
            if t['tour_id'] in live:
                cur.execute('update tours set tour_type=%s where tour_id=%s', (t['tour_type'], t['tour_id']))
        cur.execute('delete from cm_rollup_watermarks where community_id=any(%s::uuid[])', (list(COMMUNITIES),))
        print(json.dumps({'undo_tours': len(live), **counts}))
    else:
        cur.execute("""select t.tour_id::text tour_id, t.tour_type, t.consent_given, t.duration_minutes, t.start_time
                         from tours t join tour_analytics ta on ta.tour_id=t.tour_id
                        where t.company_id=%s and t.community_id=any(%s::uuid[]) and t.tour_type is null
                          and t.start_time >= %s and t.start_time < %s order by t.tour_id""", (COMPANY, list(COMMUNITIES), *WINDOW))
        tours = cur.fetchall(); ids = [t['tour_id'] for t in tours]
        label = lambda t: 'consent_declined' if not t['consent_given'] else ('short_tour' if (t['duration_minutes'] or 0) < 10 else 'full_tour')
        plan = {}
        for t in tours: plan[label(t)] = plan.get(label(t), 0) + 1
        print(json.dumps({'tours': len(ids), 'labels': plan, 'commit': a.commit}))
        if not ids:
            conn.rollback(); print('nothing to do (already shifted)'); return
        rollups = {}
        for r in ROLLUPS:
            cur.execute(f'select * from public.{r} where community_id=any(%s::uuid[])', (list(COMMUNITIES),)); rollups[r] = cur.fetchall()
        if a.commit:
            if not a.undo or os.path.exists(a.undo): raise SystemExit('--undo <new file> required')
            fd = os.open(a.undo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'days': DAYS, 'tours': [{'tour_id': t['tour_id'], 'tour_type': t['tour_type'], 'start_time': str(t['start_time'])} for t in tours],
                           'rollups': rollups, 'written_at': datetime.datetime.utcnow().isoformat()}, f, default=str)
        cur.execute(LEDGER_DDL)
        cur.execute('select tour_id::text from demo_seed.history_shift_ledger where tour_id=any(%s::uuid[])', (ids,))
        done = {r['tour_id'] for r in cur.fetchall()}
        local_ids = [i for i in ids if i not in done]
        counts['local_shift_tours'] = len(local_ids)
        shift(cur, ids, DAYS, counts, local_ids)
        cur.execute('insert into demo_seed.history_shift_ledger (tour_id, days) select unnest(%s::uuid[]), %s on conflict do nothing', (local_ids, DAYS))
        for t in tours:
            cur.execute('update tours set tour_type=%s where tour_id=%s', (label(t), t['tour_id']))
        for r in ROLLUPS:
            cur.execute(f'delete from public.{r} where community_id=any(%s::uuid[])', (list(COMMUNITIES),)); counts['deleted:' + r] = cur.rowcount
        print(json.dumps(counts))
    if a.commit or a.undo_from:
        conn.commit(); print('committed')
    else:
        conn.rollback(); print('dry run: rolled back')


if __name__ == '__main__':
    main()
