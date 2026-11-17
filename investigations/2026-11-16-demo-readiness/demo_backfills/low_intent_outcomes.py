"""STAGING demo tenant (3360791e): give Low-intent tours a small, non-zero apply rate in demo_seed.tour_outcomes.

Why: the A9 intent rule moves ~24% of never-applying Medium tours to Low, so Low reads 0-1% applied per community
(2026-11-17 matrix), and the demo rule is Low < Medium < High with Low never 0% (Low ~7-9%).
What (v2, evenly spaced so every window reads ~11%): per community, Low-intent tours (tour_analytics.lease_intent 'low')
on Full tours (what the dashboards count) are ranked by tour day; every 10th one (offset hashed from the community id) is applied (applied_date = tour day + 2-12
days; ~70% of those leased 2-8 days later); every other Low tour is not applied. A random ~9% pick read 0-33% per
community in small windows (Low above Medium); spacing keeps any window with 10+ Full Low tours at ~10%, never 0. Non-Full Low tours keep the model's outcome.
Medium/High outcomes stay the outcome model's own (jobs.demo_outcome_seed).
Run order: after jobs.demo_outcome_seed (which would rewrite these rows to its model draw).
Staging only (refuses any DB not named ontour_db_staging), demo company only, dry run unless --commit,
undo file (original rows) written first, 0600, never overwritten; idempotent (a second run changes 0 rows).
"""
import argparse, datetime, hashlib, json, os, sys
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from jobs.demo_sync.config import load_db

COMPANY = '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec'
EVERY, LEASE = 10, 0.70   # every 10th Full Low tour per community (~10%)


def u(tag, tid):
    return int(hashlib.sha256(f'{tag}:{tid}'.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo'); a = ap.parse_args()
    c = load_db('staging')
    conn = psycopg2.connect(host=c['host'], port=c['port'], dbname=c['dbname'], user=c['user'], password=c['password'], application_name='low_intent_outcomes')
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('select current_database() db'); assert cur.fetchone()['db'] == 'ontour_db_staging'
    cur.execute("""select o.*, t.community_id::text community_id, (t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date tour_day
                     from demo_seed.tour_outcomes o join tours t on t.tour_id=o.tour_id join tour_analytics ta on ta.tour_id=t.tour_id
                    where t.company_id=%s and lower(ta.lease_intent)='low' and coalesce(t.tour_type, 'full_tour')='full_tour'
                    order by t.community_id, tour_day, o.tour_id""", (COMPANY,))
    rows = cur.fetchall(); changes = []; rank = {}
    for r in rows:
        tid, cid = str(r['tour_id']), r['community_id']
        k = rank.get(cid, 0); rank[cid] = k + 1
        if k % EVERY == int(hashlib.sha256(cid.encode()).hexdigest()[:4], 16) % EVERY:
            ad = r['tour_day'] + datetime.timedelta(days=2 + int(u('low-apply-day', tid) * 11))
            leased = u('low-lease', tid) < LEASE
            ld = ad + datetime.timedelta(days=2 + int(u('low-lease-day', tid) * 7)) if leased else None
            want = dict(applied=True, applied_date=ad, leased=leased, leased_date=ld)
        else:
            want = dict(applied=False, applied_date=None, leased=False, leased_date=None)
        if any(r[k2] != v for k2, v in want.items()):
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
