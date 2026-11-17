"""STAGING demo tenant (3360791e): give Low-intent tours a small, non-zero apply rate in demo_seed.tour_outcomes.

Why: the A9 intent rule moves ~24% of never-applying Medium tours to Low, so Low reads 0-1% applied per community
(2026-11-17 matrix), and the demo rule is Low < Medium < High with Low never 0% (Low ~7-9%).
What: for demo-company tours whose tour_analytics.lease_intent is 'low', a stable ~9% (sha256 of 'low-apply:' + tour id)
become applied (applied_date = tour day + 2-12 days) and ~70% of those leased (leased_date = applied + 2-8 days).
Nothing else changes; every other outcome stays the outcome model's own (jobs.demo_outcome_seed).
Run order: after jobs.demo_outcome_seed (which would rewrite these rows to its model draw).
Staging only (refuses any DB not named ontour_db_staging), demo company only, dry run unless --commit,
undo file (original rows) written first, 0600, never overwritten; idempotent (a second run changes 0 rows).
"""
import argparse, datetime, hashlib, json, os, sys
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from jobs.demo_sync.config import load_db

COMPANY = '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec'
RATE, LEASE = 0.09, 0.70


def u(tag, tid):
    return int(hashlib.sha256(f'{tag}:{tid}'.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo'); a = ap.parse_args()
    c = load_db('staging')
    conn = psycopg2.connect(host=c['host'], port=c['port'], dbname=c['dbname'], user=c['user'], password=c['password'], application_name='low_intent_outcomes')
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('select current_database() db'); assert cur.fetchone()['db'] == 'ontour_db_staging'
    cur.execute("""select o.*, (t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date tour_day
                     from demo_seed.tour_outcomes o join tours t on t.tour_id=o.tour_id join tour_analytics ta on ta.tour_id=t.tour_id
                    where t.company_id=%s and lower(ta.lease_intent)='low' order by o.tour_id""", (COMPANY,))
    rows = cur.fetchall(); changes = []
    for r in rows:
        tid = str(r['tour_id'])
        if u('low-apply', tid) >= RATE:
            continue
        ad = r['tour_day'] + datetime.timedelta(days=2 + int(u('low-apply-day', tid) * 11))
        leased = u('low-lease', tid) < LEASE
        ld = ad + datetime.timedelta(days=2 + int(u('low-lease-day', tid) * 7)) if leased else None
        want = dict(applied=True, applied_date=ad, leased=leased, leased_date=ld)
        if any(r[k] != v for k, v in want.items()):
            changes.append((r, want))
    print(json.dumps({'low_tours': len(rows), 'to_change': len(changes), 'commit': a.commit}))
    if a.commit and changes:
        if not a.undo or os.path.exists(a.undo): raise SystemExit('--undo <new file> required')
        fd = os.open(a.undo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump([{k: r[k] for k in ('tour_id', 'applied', 'applied_date', 'leased', 'leased_date')} for r, _ in changes], f, default=str)
    for r, w in changes:
        cur.execute('update demo_seed.tour_outcomes set applied=%s, applied_date=%s, leased=%s, leased_date=%s where tour_id=%s',
                    (w['applied'], w['applied_date'], w['leased'], w['leased_date'], r['tour_id']))
    (conn.commit if a.commit else conn.rollback)(); print('committed' if a.commit else 'dry run: rolled back')


if __name__ == '__main__':
    main()
