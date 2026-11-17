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
    rows = cur.fetchall(); changes = []; rank = {}; final = {}

    def applied_want(r):
        tid = str(r['tour_id'])
        ad = r['tour_day'] + datetime.timedelta(days=2 + int(u('low-apply-day', tid) * 11))
        leased = u('low-lease', tid) < LEASE
        ld = ad + datetime.timedelta(days=2 + int(u('low-lease-day', tid) * 7)) if leased else None
        return dict(applied=True, applied_date=ad, leased=leased, leased_date=ld)
    NOT = dict(applied=False, applied_date=None, leased=False, leased_date=None)
    for r in rows:
        tid, cid = str(r['tour_id']), r['community_id']
        k = rank.get(cid, 0); rank[cid] = k + 1
        final[tid] = applied_want(r) if k % EVERY == int(hashlib.sha256(cid.encode()).hexdigest()[:4], 16) % EVERY else NOT
    # Window rules (coordinator 11-17, Ankur "never make Low 0%"), on the dashboards' 30-day outcome windows as of today
    # (tours from today-59..today-30 for 30D, today-36..today-30 for 7D):
    #  (a) every community's 30D window with Full Low tours has at least one applied (the lowest-hash one);
    #  (b) the portfolio 7D window keeps Low's applied rate below Medium's (un-apply highest-hash Low tours, never one
    #      that rule (a) needs).
    today = datetime.date.today()
    w30 = (today - datetime.timedelta(days=59), today - datetime.timedelta(days=30))
    w7 = (today - datetime.timedelta(days=36), today - datetime.timedelta(days=30))
    inw = lambda r, w: w[0] <= r['tour_day'] <= w[1]
    keep = set()
    for cid in {r['community_id'] for r in rows}:
        win = [r for r in rows if r['community_id'] == cid and inw(r, w30)]
        if win and not any(final[str(r['tour_id'])]['applied'] for r in win):
            pick = min(win, key=lambda r: u('low-floor', str(r['tour_id'])))
            final[str(pick['tour_id'])] = applied_want(pick)
        keep |= {str(r['tour_id']) for r in win if final[str(r['tour_id'])]['applied']}
    cur.execute("""select count(*) n, count(*) filter (where o.applied and o.applied_date <= (t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date + 30) a
                     from demo_seed.tour_outcomes o join tours t on t.tour_id=o.tour_id join tour_analytics ta on ta.tour_id=t.tour_id
                    where t.company_id=%s and lower(ta.lease_intent)='medium' and coalesce(t.tour_type, 'full_tour')='full_tour'
                      and (t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date between %s and %s""", (COMPANY, *w7))
    m = cur.fetchone(); med_rate = (m['a'] / m['n']) if m['n'] else 1.0
    low7 = [r for r in rows if inw(r, w7)]
    rate = lambda: sum(final[str(r['tour_id'])]['applied'] for r in low7) / len(low7) if low7 else 0
    for r in sorted(low7, key=lambda r: -u('low-floor', str(r['tour_id']))):
        if rate() <= med_rate - 0.03: break   # at least ~3 pts below Medium
        tid = str(r['tour_id'])
        if final[tid]['applied'] and tid not in keep: final[tid] = NOT
    for r in rows:
        want = final[str(r['tour_id'])]
        if any(r[k2] != v for k2, v in want.items()):
            changes.append((r, want))
    # (b, part 2) if Low still isn't ~3 pts under Medium in the 7D window (its applied Low tours are all needed by rule
    # (a)), mark the lowest-hash not-applied Medium Full tours there applied until it is.
    if low7 and m['n'] and rate() > med_rate - 0.03:
        cur.execute("""select o.*, (t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date tour_day
                         from demo_seed.tour_outcomes o join tours t on t.tour_id=o.tour_id join tour_analytics ta on ta.tour_id=t.tour_id
                        where t.company_id=%s and lower(ta.lease_intent)='medium' and coalesce(t.tour_type, 'full_tour')='full_tour'
                          and (t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date between %s and %s
                          and not (o.applied and o.applied_date <= (t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date + 30)""",
                    (COMPANY, *w7))
        meds = sorted(cur.fetchall(), key=lambda r: u('med-floor', str(r['tour_id'])))
        a_med = m['a']
        for r in meds:
            if a_med / m['n'] >= rate() + 0.03: break
            changes.append((r, applied_want(r))); a_med += 1
        med_rate = a_med / m['n']
    # (c) per community, 30D window: Low's applied rate stays ~3 pts under Medium's (Low < Medium < High). When it is
    #     not (small communities: Stonebridge 1/7 Low vs 2/18 Medium on 11-17), mark the lowest-hash not-applied Medium
    #     Full tours there applied until it is. Never un-applies Low (rule (a) keeps Low non-zero).
    queued = {str(r['tour_id']) for r, w in changes if w['applied']}
    cur.execute("""select o.*, t.community_id::text community_id, (t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date tour_day
                     from demo_seed.tour_outcomes o join tours t on t.tour_id=o.tour_id join tour_analytics ta on ta.tour_id=t.tour_id
                    where t.company_id=%s and lower(ta.lease_intent)='medium' and coalesce(t.tour_type, 'full_tour')='full_tour'
                      and (t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date between %s and %s""", (COMPANY, *w30))
    meds30 = cur.fetchall()
    ok = lambda r: str(r['tour_id']) in queued or (r['applied'] and r['applied_date'] is not None and r['applied_date'] <= r['tour_day'] + datetime.timedelta(days=30))
    for cid in sorted({r['community_id'] for r in rows}):
        lows = [r for r in rows if r['community_id'] == cid and inw(r, w30)]
        meds = [r for r in meds30 if r['community_id'] == cid]
        if not lows or not meds: continue
        low_rate = sum(final[str(r['tour_id'])]['applied'] for r in lows) / len(lows)
        a_med = sum(ok(r) for r in meds)
        for r in sorted((r for r in meds if not ok(r)), key=lambda r: u('med-floor', str(r['tour_id']))):
            if a_med / len(meds) >= low_rate + 0.03: break
            changes.append((r, applied_want(r))); queued.add(str(r['tour_id'])); a_med += 1
    print(json.dumps({'low_tours': len(rows), 'to_change': len(changes), 'commit': a.commit, 'w7': [str(w7[0]), str(w7[1])], 'low7': len(low7), 'low7_rate': round(rate(), 3), 'med7_rate': round(med_rate, 3), 'med7_n': m['n']}))
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
