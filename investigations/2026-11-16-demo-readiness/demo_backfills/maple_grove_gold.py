"""STAGING demo tenant: make Maple Grove's seeded gold look like a stabilised community (Ankur, 2026-11-16).

Maple Grove's gold rows were seeded (ontour-etl-pipeline tools/demo_waymaker_availability.py) from a real source
community whose rents ($3.5-9.3k) and 61% occupancy read as broken next to the other demo communities. This
rescales it in place, demo rows only (job_id = the seed tool's DEMO_JOB), company 3360791e, community Maple Grove:
  * rents x FACTOR on every row (units, unit history, daily, by-plan): keeps every plan's order; the 1-bed median
    lands near the other Paige communities' (~$1,470);
  * units (stable hash order): 74 vacant AVAILABLE_SOON -> OCCUPIED, 6 -> NOTICE (available soon), 4 -> AVAILABLE_NOW
    ready; the 16 LEASED_VACANT not-ready -> OCCUPIED; the open unit_status_history row follows;
  * today's availability_daily and availability_by_plan_daily rows recomputed from the units with the gold formulas
    (occupancy = (occupied + notice) / revenue units; leased = (occupied + preleased signed) / revenue units;
    exposure = 100 - leased); earlier days scaled by the same per-field ratios.
Undo (written before commit, 0600): every original row of the four tables for Maple Grove.
Persistence: the 10:00 UTC waymaker-demo-refresh-staging job only shifts dates, so the values stay. Re-running the
seed tool, or a staging medallion run that rebuilds WayMaker gold, replaces them: run this again afterwards.
Dry run unless --commit.
"""
import argparse, datetime, hashlib, json, os, sys
from decimal import Decimal
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from jobs.demo_sync.config import load_db, DB_NAMES

W = '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec'
MG = '076a07bd-beec-4e8b-85a4-afee64fd6ab4'
DEMO_JOB = '00000000-0000-4000-8000-00000000d3e0'
FACTOR = Decimal('0.3532')
DONE_MARK = 'maple-grove-rescaled'


def h(x):
    return hashlib.sha256(('mg:' + str(x)).encode()).hexdigest()


def rnd(v):
    return None if v is None else (Decimal(v) * FACTOR).quantize(Decimal('1'))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo'); a = ap.parse_args()
    if a.commit and (not a.undo or os.path.exists(a.undo)):
        sys.exit('--commit needs --undo <new file>')
    conn = psycopg2.connect(**load_db('staging'), options='-c statement_timeout=300000 -c lock_timeout=15000')
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT current_database() d'); assert cur.fetchone()['d'] == DB_NAMES['staging']
    key = {'company_id': W, 'community_id': MG}
    where = 'company_id=%(company_id)s::uuid AND community_id=%(community_id)s::uuid'
    cur.execute(f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY market_rent) m FROM gold.availability_unit_current WHERE {where}", key)
    if (cur.fetchone()['m'] or 0) < 3000:
        print(json.dumps({'skipped': 'already rescaled (median rent < $3,000)'})); return
    undo = {}
    for t in ('availability_unit_current', 'unit_status_history', 'availability_daily', 'availability_by_plan_daily'):
        cur.execute(f'SELECT * FROM gold.{t} WHERE {where}', key); undo[t] = cur.fetchall()
    assert all(str(r.get('job_id')) == DEMO_JOB for r in undo['availability_unit_current']), 'not the demo seed rows'
    today_old = max(undo['availability_daily'], key=lambda r: r['day'])

    # Units: state changes (stable order) + rents.
    units = sorted(undo['availability_unit_current'], key=lambda u: h(u['unit_key']))
    soon = [u for u in units if u['availability_class'] == 'AVAILABLE_SOON' and u['occupancy_status'] == 'VACANT']
    lv_nr = [u for u in units if u['availability_class'] == 'LEASED_VACANT' and u['ready_state'] == 'NOT_READY']
    change = {}
    for u in soon[:74] + lv_nr:
        change[u['unit_key']] = dict(occupancy_status='OCCUPIED', availability_class='OCCUPIED', next_lease_state='NOT_APPLICABLE',
                                     ready_state='NOT_APPLICABLE', available_on=None, days_vacant=None)
    for u in soon[74:80]:
        change[u['unit_key']] = dict(occupancy_status='NOTICE', availability_class='AVAILABLE_SOON', next_lease_state='NOT_LEASED',
                                     ready_state='NOT_APPLICABLE', days_vacant=None)
    today = today_old['day']
    for u in soon[80:84]:
        change[u['unit_key']] = dict(availability_class='AVAILABLE_NOW', ready_state='READY', available_on=today)
    for u in units:
        c = dict(change.get(u['unit_key'], {}), market_rent=rnd(u['market_rent']))
        sets = ', '.join(f'{k}=%({k})s' for k in c)
        cur.execute(f'UPDATE gold.availability_unit_current SET {sets} WHERE {where} AND unit_key=%(unit_key)s',
                    dict(c, unit_key=u['unit_key'], **key))
    for r in undo['unit_status_history']:
        c = {'market_rent': rnd(r['market_rent'])}
        if r['valid_to'] is None and r['unit_key'] in change:
            c.update({k: v for k, v in change[r['unit_key']].items() if k in ('availability_class', 'occupancy_status', 'next_lease_state', 'ready_state')})
        sets = ', '.join(f'{k}=%({k})s' for k in c)
        cur.execute(f'UPDATE gold.unit_status_history SET {sets} WHERE {where} AND unit_key=%(unit_key)s AND valid_from=%(valid_from)s',
                    dict(c, unit_key=r['unit_key'], valid_from=r['valid_from'], **key))

    # Today's rows from the new units.
    cur.execute(f'SELECT * FROM gold.availability_unit_current WHERE {where}', key); now_units = cur.fetchall()
    def tally(us):
        rev = [u for u in us if u['availability_class'] != 'OFFLINE']
        t = dict(units_known=len(us), revenue_units=len(rev),
                 occupied=sum(u['occupancy_status'] == 'OCCUPIED' for u in rev), notice=sum(u['occupancy_status'] == 'NOTICE' for u in rev),
                 vacant=sum(u['occupancy_status'] == 'VACANT' for u in rev),
                 available_now=sum(u['availability_class'] == 'AVAILABLE_NOW' for u in us), available_soon=sum(u['availability_class'] == 'AVAILABLE_SOON' for u in us),
                 leased_vacant=sum(u['availability_class'] == 'LEASED_VACANT' for u in us), notice_leased=sum(u['availability_class'] == 'NOTICE_LEASED' for u in us),
                 offline=sum(u['availability_class'] == 'OFFLINE' for u in us), not_on_market=sum(u['availability_class'] == 'NOT_ON_MARKET' for u in us))
        rents = [u['market_rent'] for u in us if u['market_rent'] is not None]
        t['avg_market_rent'] = (sum(rents) / len(rents)).quantize(Decimal('0.01')) if rents else None
        dv = [u['days_vacant'] for u in us if u['days_vacant'] is not None and u['occupancy_status'] == 'VACANT']
        t['avg_days_vacant'] = round(sum(dv) / len(dv), 1) if dv else None
        return t
    new = tally(now_units)
    signed = min(today_old['preleased_signed'] or 0, new['leased_vacant'])
    applied = min(today_old['preleased_applied'] or 0, new['available_now'] + new['available_soon'])
    def pcts(t, signed_):
        ru = max(t.get('revenue_units') or t.get('units_known') or 1, 1)
        occ = round(100 * (t['occupied'] + t['notice']) / ru, 2); leased = round(100 * (t['occupied'] + signed_) / ru, 2)
        return dict(occupancy_pct=occ, leased_pct=leased, exposure_pct=round(100 - leased, 2))
    ratio = {f: (Decimal(new[f]) / Decimal(today_old[f])) if today_old[f] else Decimal(1)
             for f in ('notice', 'vacant', 'available_now', 'available_soon', 'leased_vacant')}
    dv_ratio = (Decimal(str(new['avg_days_vacant'])) / Decimal(str(today_old['avg_days_vacant']))) if new['avg_days_vacant'] and today_old['avg_days_vacant'] else Decimal(1)
    daily_new = {}
    out = {'today_before': {k: str(today_old[k]) for k in ('occupied', 'notice', 'vacant', 'available_now', 'available_soon', 'occupancy_pct', 'avg_market_rent')}}
    for r in undo['availability_daily']:
        if r['day'] == today:
            n = dict(new, preleased_signed=signed, preleased_applied=applied, **pcts(new, signed))
        else:
            n = {f: int(round(Decimal(r[f] or 0) * ratio[f])) for f in ratio}
            n['occupied'] = max(0, (r['revenue_units'] or r['units_known'] or 0) - n['vacant'] - n['notice'])
            n['preleased_signed'] = min(r['preleased_signed'] or 0, n['leased_vacant'])
            n['preleased_applied'] = min(r['preleased_applied'] or 0, n['available_now'] + n['available_soon'])
            n['avg_market_rent'] = rnd(r['avg_market_rent']) if r['avg_market_rent'] is None else (Decimal(r['avg_market_rent']) * FACTOR).quantize(Decimal('0.01'))
            n['avg_days_vacant'] = None if r['avg_days_vacant'] is None else round(float(Decimal(str(r['avg_days_vacant'])) * dv_ratio), 1)
            n.update(pcts(dict(n, revenue_units=r['revenue_units'] or r['units_known']), n['preleased_signed']))
        daily_new[r['day']] = n
        sets = ', '.join(f'{k}=%({k})s' for k in n)
        cur.execute(f'UPDATE gold.availability_daily SET {sets} WHERE {where} AND day=%(day)s', dict(n, day=r['day'], **key))
        if r['day'] == today:
            out['today_after'] = {k: str(n[k]) for k in ('occupied', 'notice', 'vacant', 'available_now', 'available_soon', 'occupancy_pct', 'avg_market_rent')}
    # By plan: today from the units, earlier days scaled by the same ratios (open counts) and the rent factor.
    # The by-plan rows group units under the community's setup plans (not the units' raw codes), so each day's plan
    # counts are the day's new totals, shared across plans in proportion to their old counts (largest remainder);
    # occupied takes up the difference so a plan's unit count is unchanged; rents take the same factor.
    def allocate(total, weights, fallback):
        w = [Decimal(x or 0) for x in weights]
        if not sum(w):
            w = [Decimal(x or 0) for x in fallback]
        if not sum(w) or not total:
            return [0] * len(weights)
        raw = [total * x / sum(w) for x in w]
        base = [int(x) for x in raw]
        for i in sorted(range(len(raw)), key=lambda i: -(raw[i] - base[i]))[:total - sum(base)]:
            base[i] += 1
        return base
    days = {}
    for r in undo['availability_by_plan_daily']:
        days.setdefault(r['day'], []).append(r)
    plans_out = {}
    for day, rows in days.items():
        tot = daily_new.get(day)
        if tot is None:
            continue
        alloc = {f: allocate(int(tot[f]), [r[f] for r in rows], [r['units_known'] for r in rows])
                 for f in ('available_now', 'available_soon', 'leased_vacant')}
        for i, r in enumerate(rows):
            n = {f: alloc[f][i] for f in alloc}
            n['occupied'] = max(0, (r['occupied'] or 0) + (r['available_now'] or 0) + (r['available_soon'] or 0) + (r['leased_vacant'] or 0)
                                - n['available_now'] - n['available_soon'] - n['leased_vacant'])
            n['avg_market_rent'] = None if r['avg_market_rent'] is None else (Decimal(r['avg_market_rent']) * FACTOR).quantize(Decimal('0.01'))
            if day == today:
                plans_out[r['floor_plan_code']] = {'before': [str(r['avg_market_rent']), r['available_now'], r['available_soon']],
                                                   'after': [str(n['avg_market_rent']), n['available_now'], n['available_soon']]}
            sets = ', '.join(f'{k}=%({k})s' for k in n)
            cur.execute(f'UPDATE gold.availability_by_plan_daily SET {sets} WHERE {where} AND day=%(day)s AND floor_plan_code IS NOT DISTINCT FROM %(fp)s',
                        dict(n, day=r['day'], fp=r['floor_plan_code'], **key))
    out['plans_today'] = plans_out
    out['units_changed'] = len(change)
    if a.commit:
        fd = os.open(a.undo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(undo, f, default=str)
        conn.commit()
    else:
        conn.rollback()
    print(json.dumps(dict(out, committed=a.commit), default=str))


if __name__ == '__main__':
    main()
