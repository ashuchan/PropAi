"""STAGING demo tenant (Paige Apartments): a demo unit-availability report for the app's Units tab, from demo gold.

Approved 2026-11-16 (demo prep). Staging only; company 3360791e only. One transaction; dry run unless --commit.
Writes, per demo community (the six with demo tours; --community limits it to one or more of them):
  * availability_reports: one COMPLETED, current report dated today, file "demo-availability-seed.xlsx", no file URL;
  * buildings / floor_plans / units: invented codes and numbers; plan names generic from the plan type
    ("A1 · 1 Bed / 1 Bath"), never gold's plan codes (some look like real names);
  * unit_availability: one row per unit; available-now units VACANT_NOT_LEASED_READY (ready 1-20 days ago),
    available-soon units NTV_NOT_LEASED (move-out in 5-45 days, ready 3-7 days later); market rent = the plan's
    gold average +/- 3%, stable per unit; applicant_name always null;
  * floor_plan_availability_summary: per-plan counts.
Counts per plan = gold.availability_by_plan_daily (latest day): available_now + available_soon.
Also: Prestige Park's June client report: its 14 applicant names -> null (privacy) and the report, its unit rows and
plan summaries -> not current. communities.is_unit_availability_enabled -> true for the other five.
Undo (written first, 0600): original values of every updated row and the ids of every inserted row.
Idempotent: a community that already has a current seed report is skipped (a second run changes nothing).

--replace (2026-11-17, after a gold rescale such as demo_gold_rescale.py / maple_grove_gold.py): for an already
seeded community, the seed it would write now (plan code, plan name, unit number, rent) is compared with the current
seed. Identical -> skipped ("up to date"; a second run changes 0 rows). Different -> the current seed's
availability_reports / unit_availability / floor_plan_availability_summary / units / floor_plans rows (full rows) go
to --undo-replaced (new file, 0600, written BEFORE any delete), are deleted, and the community is seeded afresh.
Buildings 11-13 are kept and reused. Nothing outside the seed report's own rows is touched.
"""
import argparse, datetime, hashlib, json, os, sys, uuid
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from psycopg2.extras import Json
from jobs.demo_sync.config import load_db, DB_NAMES

COMPANY = '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec'
COMMUNITIES = {'cad6d9d4-5881-40ed-9727-73b60ad1a375': 'Prestige Park', 'f585b747-ff97-4d43-987b-1e0882f029eb': 'Cedar Point',
               '24589e94-c8a1-419d-95fe-314a0aa14332': 'Willow Run', '85ec7ed8-5d5f-49cb-af16-e8c34aced869': 'Stonebridge',
               '076a07bd-beec-4e8b-85a4-afee64fd6ab4': 'Maple Grove', '9254d356-a920-5e47-8793-e5f1808c4d41': 'Ivywood'}
PRESTIGE = 'cad6d9d4-5881-40ed-9727-73b60ad1a375'
SEED_FILE = 'demo-availability-seed.xlsx'
BUILDINGS = ('11', '12', '13')
AMENITIES = ('Balcony', 'Walk-in Closet', 'Quartz Countertops', 'Stainless Appliances', 'In-unit Washer/Dryer',
             'Courtyard View', 'Wood-style Flooring', 'Smart Thermostat', 'Kitchen Island', 'Garden Tub')
SQFT = {0: (520, 600), 1: (690, 840), 2: (1040, 1190), 3: (1320, 1460)}
SEED_TABLES = ('availability_reports', 'unit_availability', 'floor_plan_availability_summary', 'units', 'floor_plans')


def h(*parts):
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest(), 16)


def plan_rooms(plan_type):
    try:
        b, ba = (plan_type or '1x1').lower().split('x')[:2]
        return (0 if b.startswith('s') else int(b)), int(float(ba))
    except Exception:
        return 1, 1


def build_seed(cur, cid, gday, ignore_fp=(), ignore_units=()):
    """The seed this community gets from gold now (no writes). ignore_*: ids of the seed being replaced."""
    cur.execute("""SELECT floor_plan_code, plan_type, coalesce(available_now,0) now, coalesce(available_soon,0) soon, avg_market_rent rent
                   FROM gold.availability_by_plan_daily WHERE community_id=%s AND day=%s ORDER BY floor_plan_code""", (cid, gday))
    plans = [p for p in cur.fetchall() if p['now'] + p['soon'] > 0]
    # A1 < A2 < ... by rent within a bedroom count (gold's own codes sort as text: A10 < A3)
    plans.sort(key=lambda p: (plan_rooms(p['plan_type'])[0], float(p['rent']) if p['rent'] is not None else 1e9, p['floor_plan_code']))
    cur.execute('SELECT avg_market_rent r FROM gold.availability_daily WHERE community_id=%s ORDER BY day DESC LIMIT 1', (cid,))
    fallback = float((cur.fetchone() or {}).get('r') or 1500)
    seq = {}; used_units = set(); built = []
    for p in plans:
        beds, baths = plan_rooms(p['plan_type'])
        letter = 'S' if beds == 0 else 'ABCD'[min(beds, 4) - 1]
        seq[letter] = seq.get(letter, 0) + 1
        code = f'{letter}{seq[letter]}'
        name = f"{code} · {'Studio' if beds == 0 else f'{beds} Bed'} / {baths} Bath"
        lo, hi = SQFT.get(min(beds, 3), (700, 850))
        sqft = lo + h(cid, code, 'sqft') % (hi - lo + 1)
        cur.execute('SELECT id FROM floor_plans WHERE community_id=%s AND code=%s AND NOT (id::text = ANY(%s))', (cid, code, list(ignore_fp)))
        if cur.fetchone():
            code = f'D-{code}'; name = name.replace(name.split(' · ')[0], code, 1)
        base = float(p['rent']) if p['rent'] is not None else fallback
        units = []
        for i in range(p['now'] + p['soon']):
            kind = 'now' if i < p['now'] else 'soon'
            k = h(cid, code, i)
            while True:
                b = BUILDINGS[k % len(BUILDINGS)]; floor = 1 + (k // 7) % 4; num = 1 + (k // 31) % 40
                bu = f'{b}-{floor}{num:02d}'
                if bu not in used_units: break
                k = h(k, 'next')
            used_units.add(bu)
            cur.execute('SELECT 1 FROM units WHERE community_id=%s AND building_unit=%s AND NOT (id::text = ANY(%s))', (cid, bu, list(ignore_units)))
            if cur.fetchone():
                continue
            ams = [AMENITIES[(k // (j + 3)) % len(AMENITIES)] for j in range(3)]
            units.append(dict(kind=kind, k=k, b=b, floor=floor, num=num, bu=bu, ams=sorted(set(ams)),
                              rent=round(base * (1 + ((k // 13) % 7 - 3) / 100))))
        built.append(dict(code=code, name=name, beds=beds, baths=baths, sqft=sqft, units=units))
    return built


def signature(built):
    return sorted((p['code'], p['name'], u['bu'], int(u['rent'])) for p in built for u in p['units'])


def current_seed(cur, cid):
    """The community's current seed rows (full rows), or None."""
    cur.execute("""SELECT * FROM availability_reports WHERE community_id=%s AND company_id=%s AND file_name=%s AND is_current""",
                (cid, COMPANY, SEED_FILE))
    reports = cur.fetchall()
    if not reports:
        return None
    assert len(reports) == 1, f'{cid}: {len(reports)} current seed reports'
    rid = reports[0]['id']
    rows = {'availability_reports': reports}
    cur.execute('SELECT * FROM unit_availability WHERE report_id=%s AND community_id=%s AND company_id=%s', (rid, cid, COMPANY))
    rows['unit_availability'] = cur.fetchall()
    cur.execute('SELECT * FROM floor_plan_availability_summary WHERE report_id=%s AND community_id=%s AND company_id=%s', (rid, cid, COMPANY))
    rows['floor_plan_availability_summary'] = cur.fetchall()
    cur.execute('SELECT * FROM units WHERE id::text = ANY(%s) AND community_id=%s', ([str(r['unit_id']) for r in rows['unit_availability']], cid))
    rows['units'] = cur.fetchall()
    cur.execute('SELECT * FROM floor_plans WHERE id::text = ANY(%s) AND community_id=%s',
                ([str(r['floor_plan_id']) for r in rows['floor_plan_availability_summary']], cid))
    rows['floor_plans'] = cur.fetchall()
    return rows


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo')
    ap.add_argument('--community', action='append', help='limit to these community ids (default: all six)')
    ap.add_argument('--replace', action='store_true', help='re-seed an already seeded community whose seed no longer matches gold')
    ap.add_argument('--undo-replaced', help='new file for the replaced seed rows (needed with --replace --commit)')
    a = ap.parse_args()
    if a.commit and (not a.undo or os.path.exists(a.undo)):
        sys.exit('--commit needs --undo <new file>')
    if a.commit and a.replace and (not a.undo_replaced or os.path.exists(a.undo_replaced) or a.undo_replaced == a.undo):
        sys.exit('--replace --commit needs --undo-replaced <another new file>')
    selected = {c: COMMUNITIES[c] for c in (a.community or COMMUNITIES)}   # KeyError = not a demo community
    conn = psycopg2.connect(**load_db('staging'), options='-c statement_timeout=300000 -c lock_timeout=15000')
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT current_database() d'); db = cur.fetchone()['d']
    assert db.endswith('_staging') and db == DB_NAMES['staging'], 'not the staging database'
    cur.execute('SELECT community_id::text c FROM community_company_map WHERE company_id=%s', (COMPANY,))
    assert set(COMMUNITIES) <= {r['c'] for r in cur.fetchall()}, 'every community must belong to the demo company'
    from zoneinfo import ZoneInfo
    today = datetime.datetime.now(ZoneInfo('America/Chicago')).date()
    undo = {'updated': [], 'inserted': {t: [] for t in ('availability_reports', 'buildings', 'floor_plans', 'units',
                                                        'unit_availability', 'floor_plan_availability_summary')}}
    out = {'communities': {}}

    def update(table, pk, key, cols):
        undo['updated'].append({'table': table, 'pk': pk, 'key': str(key), 'columns': cols})

    # 1. Privacy first: the June client report's applicant names on Prestige Park -> null; that report -> not current.
    if PRESTIGE in selected:
        cur.execute("""SELECT id, applicant_name FROM unit_availability WHERE community_id=%s AND company_id=%s AND applicant_name IS NOT NULL""",
                    (PRESTIGE, COMPANY))
        names = cur.fetchall()
        for r in names:
            update('unit_availability', 'id', r['id'], {'applicant_name': r['applicant_name']})
        cur.execute("UPDATE unit_availability SET applicant_name=NULL WHERE community_id=%s AND company_id=%s AND applicant_name IS NOT NULL", (PRESTIGE, COMPANY))
        out['applicant_names_nulled'] = len(names)
        for table, pk in (('availability_reports', 'id'), ('unit_availability', 'id'), ('floor_plan_availability_summary', 'id')):
            cur.execute(f"""SELECT {pk} k FROM {table} WHERE community_id=%s AND company_id=%s AND is_current""" +
                        (" AND coalesce(file_name,'') <> %s" if table == 'availability_reports' else
                         " AND report_id NOT IN (SELECT id FROM availability_reports WHERE file_name = %s)"), (PRESTIGE, COMPANY, SEED_FILE))
            keys = [r['k'] for r in cur.fetchall()]
            for k in keys:
                update(table, pk, k, {'is_current': True})
            if keys:
                cur.execute(f'UPDATE {table} SET is_current=false WHERE {pk}::text = ANY(%s)', ([str(k) for k in keys],))
            out[f'prestige_old_{table}_not_current'] = len(keys)

    # 2. Flags.
    cur.execute("""SELECT id::text, is_unit_availability_enabled en FROM communities WHERE id::text = ANY(%s)""", (list(selected),))
    for r in cur.fetchall():
        if not r['en']:
            update('communities', 'id', r['id'], {'is_unit_availability_enabled': r['en']})
            cur.execute('UPDATE communities SET is_unit_availability_enabled=true WHERE id=%s', (r['id'],))
            out.setdefault('flags_enabled', []).append(COMMUNITIES[r['id']])

    # 3. The seed, per community.
    cur.execute('SELECT max(day) d FROM gold.availability_by_plan_daily WHERE community_id::text = ANY(%s)', (list(COMMUNITIES),))
    gday = cur.fetchone()['d']
    replaced = {}
    for cid, cname in selected.items():
        old = current_seed(cur, cid)
        if old and not a.replace:
            out['communities'][cname] = 'already seeded'; continue
        if old:
            fp_ids = [str(r['id']) for r in old['floor_plans']]; unit_ids = [str(r['id']) for r in old['units']]
            built = build_seed(cur, cid, gday, ignore_fp=fp_ids, ignore_units=unit_ids)
            cur.execute("""SELECT fp.code, fp.name, u.building_unit bu, ua.market_rent FROM unit_availability ua JOIN units u ON u.id = ua.unit_id
                           JOIN floor_plans fp ON fp.id = u.floor_plan_id WHERE ua.report_id=%s""", (old['availability_reports'][0]['id'],))
            if sorted((r['code'], r['name'], r['bu'], int(r['market_rent'])) for r in cur.fetchall()) == signature(built):
                out['communities'][cname] = 'up to date'; continue
            # Seed plans must not be used by anything outside the seed being replaced.
            cur.execute('SELECT count(*) n FROM units WHERE floor_plan_id::text = ANY(%s) AND NOT (id::text = ANY(%s))', (fp_ids, unit_ids))
            assert cur.fetchone()['n'] == 0, f'{cname}: seed floor plans are used by non-seed units'
            replaced[cid] = old
        else:
            built = build_seed(cur, cid, gday)
        out['communities'][cname] = {'replaced_units': len(old['units']) if old else 0, 'built': built}

    # Undo for the replaced rows: written before any delete.
    if replaced and a.commit:
        fd = os.open(a.undo_replaced, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({t: [r for old in replaced.values() for r in old[t]] for t in SEED_TABLES}, f, default=str, indent=1)
    for cid, old in replaced.items():
        rid = old['availability_reports'][0]['id']
        cur.execute('DELETE FROM unit_availability WHERE report_id=%s AND community_id=%s', (rid, cid))
        cur.execute('DELETE FROM floor_plan_availability_summary WHERE report_id=%s AND community_id=%s', (rid, cid))
        cur.execute('DELETE FROM units WHERE id::text = ANY(%s) AND community_id=%s', ([str(r['id']) for r in old['units']], cid))
        cur.execute('DELETE FROM floor_plans WHERE id::text = ANY(%s) AND community_id=%s', ([str(r['id']) for r in old['floor_plans']], cid))
        cur.execute('DELETE FROM availability_reports WHERE id=%s AND community_id=%s', (rid, cid))
        out.setdefault('replaced_rows', {})[COMMUNITIES[cid]] = {t: len(old[t]) for t in SEED_TABLES}

    for cid, cname in selected.items():
        res = out['communities'][cname]
        if not isinstance(res, dict):
            continue
        built = res.pop('built')
        nowts = datetime.datetime.utcnow()
        cur.execute("""INSERT INTO availability_reports (community_id, company_id, created_at, updated_at, is_current, file_name, status,
                         report_date, data_from, total_units_processed, floor_plans_processed, processing_time_ms)
                       VALUES (%s,%s,%s,%s,true,%s,'COMPLETED',%s,'CLIENT_PROVIDED',%s,%s,0) RETURNING id""",
                    (cid, COMPANY, nowts, nowts, SEED_FILE, today, sum(len(p['units']) for p in built), len(built)))
        rid = cur.fetchone()['id']; undo['inserted']['availability_reports'].append(rid)
        bids = {}
        for code in BUILDINGS:
            cur.execute('SELECT id FROM buildings WHERE community_id=%s AND code=%s', (cid, code))
            row = cur.fetchone()
            if row:
                bids[code] = row['id']; continue
            bid = str(uuid.uuid4())
            cur.execute('INSERT INTO buildings (id, code, community_id, company_id, created_at, updated_at, name) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        (bid, code, cid, COMPANY, nowts, nowts, f'Building {code}'))
            bids[code] = bid; undo['inserted']['buildings'].append(bid)
        written = 0; rents = []; plan_rents = {}
        for p in built:
            fpid = str(uuid.uuid4())
            cur.execute("""INSERT INTO floor_plans (id, bathrooms, bedrooms, code, community_id, company_id, created_at, updated_at, name, sqft)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (fpid, p['baths'], p['beds'], p['code'], cid, COMPANY, nowts, nowts, p['name'], p['sqft']))
            undo['inserted']['floor_plans'].append(fpid)
            counts = {'now': 0, 'soon': 0}
            for un in p['units']:
                k, rent = un['k'], un['rent']
                uid = str(uuid.uuid4())
                cur.execute("""INSERT INTO units (id, amenities, building_unit, community_id, company_id, created_at, updated_at, floor_level, sqft,
                                 unit_number, building_id, floor_plan_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (uid, Json(un['ams']), un['bu'], cid, COMPANY, nowts, nowts, un['floor'], p['sqft'],
                             f"{un['floor']}{un['num']:02d}", bids[un['b']], fpid))
                undo['inserted']['units'].append(uid)
                rents.append(rent); plan_rents.setdefault(p['code'], []).append(rent)
                if un['kind'] == 'now':
                    move_out = today - datetime.timedelta(days=10 + (k // 17) % 31)
                    ready = today - datetime.timedelta(days=1 + (k // 19) % 20)
                    status, days_vacant = 'VACANT_NOT_LEASED_READY', (today - move_out).days
                else:
                    move_out = today + datetime.timedelta(days=5 + (k // 17) % 41)
                    ready = move_out + datetime.timedelta(days=3 + (k // 19) % 5)
                    status, days_vacant = 'NTV_NOT_LEASED', -(move_out - today).days
                uaid = str(uuid.uuid4())
                cur.execute("""INSERT INTO unit_availability (id, applicant_name, availability_status, community_id, company_id, created_at, updated_at,
                                 is_current, days_vacant, last_lease_rent, make_ready_date, market_rent, move_out_date, report_date, report_id,
                                 unit_id, data_from) VALUES (%s,NULL,%s,%s,%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s,'CLIENT_PROVIDED')""",
                            (uaid, status, cid, COMPANY, nowts, nowts, days_vacant, rent - (k // 23) % 60, ready, rent, move_out, today, rid, uid))
                undo['inserted']['unit_availability'].append(uaid)
                counts[un['kind']] += 1; written += 1
            fsid = str(uuid.uuid4())
            cur.execute("""INSERT INTO floor_plan_availability_summary (id, admin_units, community_id, company_id, corporate_units, created_at, is_current,
                             down_units, employee_units, model_units, ntv_leased, ntv_not_leased, report_date, total_leased, total_not_leased,
                             total_units, units_on_hold, vacant_leased_not_ready, vacant_leased_ready, vacant_not_leased_not_ready,
                             vacant_not_leased_ready, floor_plan_id, report_id)
                           VALUES (%s,0,%s,%s,0,%s,true,0,0,0,0,%s,%s,0,%s,%s,0,0,0,0,%s,%s,%s)""",
                        (fsid, cid, COMPANY, nowts, counts['soon'], today, counts['now'] + counts['soon'], counts['now'] + counts['soon'],
                         counts['now'], fpid, rid))
            undo['inserted']['floor_plan_availability_summary'].append(fsid)
        res.update({'plans': len(built), 'units': written, 'rent_range': [min(rents), max(rents)] if rents else None,
                    'plan_rent_ranges': {k: [min(v), max(v)] for k, v in plan_rents.items()},
                    'plan_names': [p['name'] for p in built]})
    out['gold_day'] = str(gday)
    if a.commit:
        fd = os.open(a.undo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(undo, f, default=str, indent=1)
        conn.commit()
    else:
        conn.rollback()
    conn.close()
    print(json.dumps(dict(out, committed=a.commit), default=str))


if __name__ == '__main__':
    main()
