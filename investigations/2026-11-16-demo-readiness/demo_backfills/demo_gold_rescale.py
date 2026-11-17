"""STAGING demo tenant: rescale one demo community's seeded gold RENTS into a target band (Ankur, 2026-11-17).

Generalises maple_grove_gold.py's rent step. It is rents only: occupancy, availability classes and every count stay
exactly as they are (use maple_grove_gold.py's state step if a community's occupancy also needs fixing).

  python demo_gold_rescale.py --community <community_id> [--band 1750 1950]                       # dry run
  python demo_gold_rescale.py --community <community_id> --commit --undo <new file>               # write

What it does, demo rows only (job_id = the seed tool's DEMO_JOB), company 3360791e only, one community:
  * the target is the latest day's gold.availability_daily.avg_market_rent (the number the dashboards show);
  * FACTOR = band midpoint / that value (4 dp). Every rent column of the four gold availability tables is multiplied
    by the same FACTOR (unit rents to whole dollars, averages to cents): availability_unit_current.market_rent,
    unit_status_history.market_rent, availability_daily.avg_market_rent, availability_by_plan_daily.avg_market_rent.
    One factor keeps each plan's order, the plans' relative spread and each unit's place within its plan (the
    seeded units already vary within a plan; the app's Units seed adds its own +/-3% per unit on top);
  * the latest day's average is checked against the band after the update; outside it, nothing is committed.
Safety:
  * refuses any database that is not the staging one (name must end in _staging);
  * the community must belong to company 3360791e, and every row touched must carry the demo seed's job_id;
  * dry run unless --commit; with --commit the undo file (every original row of the four tables for the
    community, 0600, never overwritten) is written BEFORE the first UPDATE;
  * idempotent: if the latest day's average is already inside the band it does nothing (a second run changes 0 rows).
Persistence: as maple_grove_gold.py (the 10:00 UTC demo refresh only shifts dates; a re-seed or staging medallion
rebuild of WayMaker gold restores the source rents: run this again afterwards, then units_seed.py --replace).
"""
import argparse, json, os, sys
from decimal import Decimal
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from jobs.demo_sync.config import load_db, DB_NAMES

COMPANY = '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec'
DEMO_JOB = '00000000-0000-4000-8000-00000000d3e0'
# (table, rent column, quantum)
RENTS = (('availability_unit_current', 'market_rent', Decimal('1')), ('unit_status_history', 'market_rent', Decimal('1')),
         ('availability_daily', 'avg_market_rent', Decimal('0.01')), ('availability_by_plan_daily', 'avg_market_rent', Decimal('0.01')))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--community', required=True)
    ap.add_argument('--band', nargs=2, type=Decimal, default=[Decimal('1750'), Decimal('1950')], metavar=('LO', 'HI'))
    ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo')
    a = ap.parse_args()
    lo, hi = a.band
    if not lo < hi:
        sys.exit('--band LO HI with LO < HI')
    if a.commit and (not a.undo or os.path.exists(a.undo)):
        sys.exit('--commit needs --undo <new file>')
    conn = psycopg2.connect(**load_db('staging'), options='-c statement_timeout=300000 -c lock_timeout=15000')
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT current_database() d'); db = cur.fetchone()['d']
        if not (db.endswith('_staging') and db == DB_NAMES['staging']):
            sys.exit(f'refusing: database {db!r} is not staging')
        cur.execute('SELECT 1 FROM community_company_map WHERE company_id=%s AND community_id=%s', (COMPANY, a.community))
        if not cur.fetchone():
            sys.exit('refusing: community is not in the demo company')
        key = {'company_id': COMPANY, 'community_id': a.community}
        where = 'company_id=%(company_id)s::uuid AND community_id=%(community_id)s::uuid'

        def latest():
            cur.execute(f'SELECT day, avg_market_rent FROM gold.availability_daily WHERE {where} ORDER BY day DESC LIMIT 1', key)
            return cur.fetchone()

        def snapshot(day):
            cur.execute(f'''SELECT floor_plan_code, plan_type, avg_market_rent FROM gold.availability_by_plan_daily
                            WHERE {where} AND day=%(day)s ORDER BY avg_market_rent NULLS LAST, floor_plan_code''', dict(key, day=day))
            plans = {r['floor_plan_code']: str(r['avg_market_rent']) for r in cur.fetchall()}
            cur.execute(f'''SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY market_rent) med, min(market_rent) mn, max(market_rent) mx
                            FROM gold.availability_unit_current WHERE {where}''', key)
            u = cur.fetchone()
            return {'plans': plans, 'unit_median': str(u['med']), 'unit_min': str(u['mn']), 'unit_max': str(u['mx'])}

        top = latest()
        if top is None or top['avg_market_rent'] is None:
            sys.exit('no gold.availability_daily rent for this community')
        out = {'community': a.community, 'band': [str(lo), str(hi)], 'latest_day': str(top['day']), 'avg_before': str(top['avg_market_rent'])}
        if lo <= top['avg_market_rent'] <= hi:
            print(json.dumps(dict(out, skipped='already in band', rows_changed=0, committed=False))); return
        factor = ((lo + hi) / 2 / Decimal(top['avg_market_rent'])).quantize(Decimal('0.0001'))
        out['factor'] = str(factor)
        out['before'] = snapshot(top['day'])

        undo = {}
        for t, _, _ in RENTS:
            cur.execute(f'SELECT * FROM gold.{t} WHERE {where}', key); undo[t] = cur.fetchall()
            bad = [r for r in undo[t] if str(r.get('job_id')) != DEMO_JOB]
            assert not bad, f'{t}: {len(bad)} rows are not demo seed rows'
        if a.commit:   # undo first, before any write
            fd = os.open(a.undo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'tool': 'demo_gold_rescale.py', 'company_id': COMPANY, 'community_id': a.community, 'factor': str(factor),
                           'rows': undo}, f, default=str)

        changed = {}
        for t, col, q in RENTS:
            cur.execute(f'UPDATE gold.{t} SET {col} = round({col} * %(f)s, %(dp)s) WHERE {where} AND {col} IS NOT NULL AND job_id=%(job)s::uuid',
                        dict(key, f=factor, dp=-q.as_tuple().exponent, job=DEMO_JOB))
            changed[t] = cur.rowcount
        out['rows_changed'] = changed
        now = latest()
        out['avg_after'] = str(now['avg_market_rent'])
        out['after'] = snapshot(top['day'])
        if not lo <= now['avg_market_rent'] <= hi:
            conn.rollback(); sys.exit(f'after-check failed ({now["avg_market_rent"]} outside band); rolled back')
        if a.commit:
            conn.commit()
        else:
            conn.rollback()
        print(json.dumps(dict(out, committed=a.commit), default=str))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
