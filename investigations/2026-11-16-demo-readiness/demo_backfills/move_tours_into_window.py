"""STAGING demo tenant: move a few completed demo tours into the last 7 days (demo prep 2026-11-17, Ankur:
"not all associate need to have, but atleast few per community").

Only tours the staging merge never re-copies (not in <handoff>/state/staging.json) and not recorded on staging; each
stays with its own associate and community; content unchanged. Every timestamp / date column of the tour's rows
(tours, tour_analytics, tour_status_updates, tour_events, tour_sentiment_signals, prospect_profiles,
tour_notifications) moves by the same whole number of days; tours.updated_at and tour_analytics.updated_at become
now() so the portal's day rollups (dashboard_backend cm_rollups, keyed by the Chicago day of tours.created_at) rebuild
the NEW day; for each OLD community-day, one remaining tour's tour_analytics.updated_at is touched so that day
rebuilds too, or, when no tour remains there, that community-day's cm_rollup rows are deleted.
Undo (written before commit, 0600): original values of every changed row and every deleted rollup row.
Idempotent: a tour already on its target day is skipped. Dry run unless --commit.
Usage: move_tours_into_window.py --moves tour_id=YYYY-MM-DD,... [--commit --undo <new file>]
"""
import argparse, datetime, json, os, sys
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from psycopg2 import sql
from jobs.demo_sync.config import load_db, DB_NAMES

COMPANY = '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec'
TABLES = ('tours', 'tour_analytics', 'tour_status_updates', 'tour_events', 'tour_sentiment_signals', 'prospect_profiles', 'tour_notifications')
ROLLUPS = ('cm_rollup_associate_day', 'cm_rollup_category_day', 'cm_rollup_days')
DAY = "((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/Chicago')::date"


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--moves', required=True); ap.add_argument('--state', required=True)
    ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo'); a = ap.parse_args()
    if a.commit and (not a.undo or os.path.exists(a.undo)):
        sys.exit('--commit needs --undo <new file>')
    moves = dict(m.split('=') for m in a.moves.split(','))
    prod = set(json.load(open(a.state)).get('tours') or [])
    assert prod, 'merge state required'
    conn = psycopg2.connect(**load_db('staging'), options='-c statement_timeout=300000 -c lock_timeout=15000')
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT current_database() d'); assert cur.fetchone()['d'] == DB_NAMES['staging']
    cur.execute("SELECT (now() AT TIME ZONE 'America/Chicago')::date d"); today = cur.fetchone()['d']
    undo = {'rows': [], 'rollups_deleted': []}; out = []; old_days = set()
    cols = {}
    for t in TABLES:
        cur.execute("""SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=%s
                       AND data_type IN ('timestamp without time zone', 'timestamp with time zone', 'date')""", (t,))
        cols[t] = [r['column_name'] for r in cur.fetchall()]
    for tid, target in moves.items():
        target = datetime.date.fromisoformat(target)
        assert target < today, 'no future (or today) dates'
        assert tid not in prod, f'{tid[:8]}: the merge re-copies this tour'
        cur.execute(f"""SELECT tour_id::text, community_id::text, associate_id::text, audio_file_url, {DAY} d FROM tours
                        WHERE tour_id=%s AND company_id=%s FOR UPDATE""", (tid, COMPANY))
        t0 = cur.fetchone(); assert t0, f'{tid[:8]}: not a demo tour'
        assert 'ontour-files-ontour-485600' not in (t0['audio_file_url'] or ''), 'recorded on staging'
        delta = (target - t0['d']).days
        if delta == 0:
            out.append({'tour': tid[:8], 'skipped': 'already on target day'}); continue
        old_days.add((t0['community_id'], t0['d']))
        n = 0
        for t in TABLES:
            if not cols[t]: continue
            cur.execute(sql.SQL('SELECT * FROM public.{} WHERE tour_id=%s').format(sql.Identifier(t)), (tid,))
            for row in cur.fetchall():
                pk = 'tour_id' if t in ('tours', 'tour_analytics', 'prospect_profiles') else 'id'
                undo['rows'].append({'table': t, 'pk': pk, 'key': str(row[pk]), 'columns': {c: row[c] for c in cols[t]}})
            sets = sql.SQL(', ').join(sql.SQL('{c} = {c} + make_interval(days => %s)').format(c=sql.Identifier(c)) if c != 'x' else sql.SQL('') for c in cols[t])
            cur.execute(sql.SQL('UPDATE public.{} SET {} WHERE tour_id=%s').format(sql.Identifier(t), sets), [delta] * len(cols[t]) + [tid])
            n += cur.rowcount
        cur.execute('UPDATE tours SET updated_at=now() WHERE tour_id=%s', (tid,))
        cur.execute('UPDATE tour_analytics SET updated_at=now() WHERE tour_id=%s', (tid,))
        out.append({'tour': tid[:8], 'from': str(t0['d']), 'to': str(target), 'rows_shifted': n})
    # Old community-days: make the rollup rebuild them (or drop them when empty).
    for cid, d in sorted(old_days):
        cur.execute(f"""SELECT t.tour_id FROM tours t JOIN tour_analytics ta ON ta.tour_id=t.tour_id
                        WHERE t.company_id=%s AND t.community_id=%s AND ((t.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/Chicago')::date=%s LIMIT 1""",
                    (COMPANY, cid, d))
        r = cur.fetchone()
        if r:
            cur.execute('SELECT updated_at FROM tour_analytics WHERE tour_id=%s', (r['tour_id'],))
            undo['rows'].append({'table': 'tour_analytics', 'pk': 'tour_id', 'key': str(r['tour_id']), 'columns': {'updated_at': cur.fetchone()['updated_at']}})
            cur.execute('UPDATE tour_analytics SET updated_at=now() WHERE tour_id=%s', (r['tour_id'],))
            out.append({'old_day': str(d), 'community': cid[:8], 'touched': str(r['tour_id'])[:8]})
        else:
            for t in ROLLUPS:
                cur.execute(sql.SQL('SELECT * FROM public.{} WHERE community_id=%s AND day=%s').format(sql.Identifier(t)), (cid, d))
                for row in cur.fetchall(): undo['rollups_deleted'].append({'table': t, 'row': row})
                cur.execute(sql.SQL('DELETE FROM public.{} WHERE community_id=%s AND day=%s').format(sql.Identifier(t)), (cid, d))
            out.append({'old_day': str(d), 'community': cid[:8], 'rollup_rows_deleted': True})
    if a.commit:
        fd = os.open(a.undo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(undo, f, default=str)
        conn.commit()
    else:
        conn.rollback()
    print(json.dumps({'moves': out, 'committed': a.commit}, default=str))


if __name__ == '__main__':
    main()
