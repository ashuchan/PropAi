"""STAGING demo tenant (3360791e): unit-tagging demo data in schema demo_seed (Ankur 2026-11-17: "we will be having unit
tagging for each tour ... ensure we have dummy data in place"). Synthetic, staging only; never real data.

Tables (created if missing), keyed by gold's own unit_key (U####, the key the tour map's unit taps already use):
  demo_seed.unit_attributes      one row per gold unit of the six demo communities: plan, bedrooms, sqft, building,
                                 floor_number / floor_band, finish_level (~40/45/15 classic/renovated/premium), view
                                 (weighted per building), is_model, building_has_elevator
  demo_seed.tour_unit_shown      1-3 homes per Full tour, in order, shown_at inside the tour; the first home is the tour's
                                 own map tap when there is one (source app_tap); ~20% of tours include the model unit
  demo_seed.tour_lease_unit      one row per Full tour with a 30-day lease in demo_seed.tour_outcomes: ~45% the first home
                                 shown, ~35% a later shown home, ~1 in 8 a home not shown on that tour
  demo_seed.tour_unit_reactions  each tour's own tour_analytics.feature_sentiments (feature, sentiment, VERBATIM quote);
                                 said_at from the quote's transcript turn when found; unit_key = the shown home whose
                                 window holds said_at (else null). Nothing is invented: the "story" (renovated -> finishes
                                 praise, street view -> sound concerns, ground floor -> noise/privacy, top floor without an
                                 elevator -> access concerns) comes only from WHICH homes a tour is shown.
Deterministic (sha256 of ids). Writes only the difference: a second run changes 0 rows. Undo (0600) records every
inserted key and every replaced row. Dry run unless --commit.
"""
import argparse, datetime, hashlib, json, os, re, sys
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from psycopg2.extras import Json
from jobs.demo_sync.config import load_db, DB_NAMES

COMPANY = '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec'
SIX = ['cad6d9d4-5881-40ed-9727-73b60ad1a375', 'f585b747-ff97-4d43-987b-1e0882f029eb', '24589e94-c8a1-419d-95fe-314a0aa14332',
       '85ec7ed8-5d5f-49cb-af16-e8c34aced869', '076a07bd-beec-4e8b-85a4-afee64fd6ab4', '9254d356-a920-5e47-8793-e5f1808c4d41']
VIEWS = ('pool', 'courtyard', 'street', 'parking', 'other')
DDL = """
CREATE TABLE IF NOT EXISTS demo_seed.unit_attributes (
  company_id uuid NOT NULL, community_id uuid NOT NULL, unit_key text NOT NULL, floor_plan_code text, bedrooms int, sqft int,
  building text, floor_number int, floor_band text, finish_level text, view text, is_model boolean NOT NULL,
  building_has_elevator boolean, seed_version text NOT NULL, PRIMARY KEY (company_id, community_id, unit_key));
CREATE TABLE IF NOT EXISTS demo_seed.tour_unit_shown (
  tour_id uuid NOT NULL, community_id uuid NOT NULL, unit_key text NOT NULL, shown_order int NOT NULL, shown_at timestamptz,
  source text NOT NULL, seed_version text NOT NULL, PRIMARY KEY (tour_id, shown_order));
CREATE TABLE IF NOT EXISTS demo_seed.tour_lease_unit (
  tour_id uuid PRIMARY KEY, unit_key text NOT NULL, lease_signed_on date, source text NOT NULL, seed_version text NOT NULL);
CREATE TABLE IF NOT EXISTS demo_seed.tour_unit_reactions (
  tour_id uuid NOT NULL, ordinal int NOT NULL, feature text NOT NULL, sentiment text, quote text, said_at timestamptz,
  unit_key text, quote_source text NOT NULL, seed_version text NOT NULL, PRIMARY KEY (tour_id, ordinal));
COMMENT ON TABLE demo_seed.tour_unit_shown IS 'Staging demo tenant only: synthetic homes shown per tour (PropAi demo_backfills/unit_tagging_seed.py). Never real data.';
"""
SEED = 'demo-units-v1'
KEYS = {'unit_attributes': ('company_id', 'community_id', 'unit_key'), 'tour_unit_shown': ('tour_id', 'shown_order'),
        'tour_lease_unit': ('tour_id',), 'tour_unit_reactions': ('tour_id', 'ordinal')}


def u(*parts):
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:12], 16) / float(16 ** 12)


def beds_of(plan_type):
    m = re.match(r'\s*(\d+)\s*x', (plan_type or '').lower())
    return 0 if (plan_type or '').lower().startswith('studio') else (int(m.group(1)) if m else None)


def slug(feature):
    return re.sub(r'[^a-z0-9]+', '_', (feature or '').strip().lower()).strip('_')[:60]


def bias_of(feature, sentiment):
    """Which unit trait a reaction should lean the shown home toward (or None)."""
    f, s = slug(feature), (sentiment or '').lower()
    if s == 'positive' and f in ('finishes', 'flooring', 'kitchen', 'appliances', 'gas_stove', 'countertops'):
        return 'renovated'
    if s == 'negative' and f in ('sound_insulation', 'noise', 'street_noise', 'traffic_noise'):
        return 'street'
    if s == 'negative' and f in ('privacy', 'ground_floor_access', 'security', 'neighborhood_safety', 'floor_level'):
        return 'ground'
    if s == 'negative' and f in ('elevator_access', 'stairs', 'accessibility', 'floor_level_access'):
        return 'walkup_top'
    return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo'); a = ap.parse_args()
    if a.commit and (not a.undo or os.path.exists(a.undo)):
        sys.exit('--commit needs --undo <new file>')
    conn = psycopg2.connect(**load_db('staging'), options='-c statement_timeout=600000 -c lock_timeout=15000')
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT current_database() d'); assert cur.fetchone()['d'] == DB_NAMES['staging']
    cur.execute(DDL)

    # 1. unit attributes from gold
    cur.execute("""SELECT community_id::text cid, unit_key, floor_plan_code, plan_type, sqft, availability_class FROM gold.availability_unit_current
                   WHERE company_id=%s AND community_id::text = ANY(%s) ORDER BY community_id, unit_key""", (COMPANY, SIX))
    gunits = cur.fetchall()
    by_comm = {}
    for g in gunits: by_comm.setdefault(g['cid'], []).append(g)
    attrs = {}
    for cid, us in by_comm.items():
        nb = max(2, min(5, (len(us) + 119) // 120)); floors = 4 if len(us) > 250 else 3
        models = sorted(us, key=lambda g: u('model', cid, g['unit_key']))[:1 if len(us) < 400 else 2]
        for g in us:
            k = g['unit_key']; b = 1 + int(u('bldg', cid, k) * nb); fl = 1 + int(u('floor', cid, k) * floors)
            fv = u('finish', cid, k)
            wv = u('view', cid, k); shift = (b - 1) % len(VIEWS)
            weights = [0.30, 0.30, 0.20, 0.12, 0.08]; weights = weights[-shift:] + weights[:-shift] if shift else weights
            acc, view = 0.0, VIEWS[-1]
            for name, w in zip(VIEWS, weights):
                acc += w
                if wv < acc: view = name; break
            attrs[(cid, k)] = dict(company_id=COMPANY, community_id=cid, unit_key=k, floor_plan_code=g['floor_plan_code'],
                                   bedrooms=beds_of(g['plan_type']), sqft=g['sqft'], building=str(b), floor_number=fl,
                                   floor_band='ground' if fl == 1 else ('top' if fl == floors else 'mid'),
                                   finish_level='classic' if fv < 0.40 else ('renovated' if fv < 0.85 else 'premium'),
                                   view=view, is_model=g in models, building_has_elevator=(b % 3 != 0), seed_version=SEED)

    # 2. tours, taps, reactions, outcomes
    cur.execute("""SELECT t.tour_id::text tid, t.community_id::text cid, t.start_time, t.end_time, pp.bedroom_preference bp,
                          ta.feature_sentiments fs, ta.transcript_turns tt, o.leased, o.leased_date
                   FROM tours t LEFT JOIN prospect_profiles pp ON pp.tour_id=t.tour_id LEFT JOIN tour_analytics ta ON ta.tour_id=t.tour_id
                   LEFT JOIN demo_seed.tour_outcomes o ON o.tour_id=t.tour_id
                   WHERE t.company_id=%s AND t.tour_type='full_tour' AND t.community_id::text = ANY(%s) ORDER BY t.tour_id""", (COMPANY, SIX))
    tours = cur.fetchall()
    cur.execute("""SELECT tour_id::text tid, building_unit, clicked_at FROM tour_availability_interactions
                   WHERE tour_id IN (SELECT tour_id FROM tours WHERE company_id=%s) ORDER BY clicked_at""", (COMPANY,))
    taps = {}
    for r in cur.fetchall(): taps.setdefault(r['tid'], []).append(r)
    shown, leases, reacts = {}, {}, {}
    for t in tours:
        cid, tid = t['cid'], t['tid']
        pool = [v for (c, _), v in attrs.items() if c == cid]
        if not pool: continue
        start = t['start_time']; end = t['end_time'] if t['end_time'] and t['end_time'] > start else start + datetime.timedelta(minutes=30)
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.timezone.utc); end = end.replace(tzinfo=datetime.timezone.utc)
        m = re.search(r'(\d)', t['bp'] or ''); want_beds = int(m.group(1)) if m else (0 if 'studio' in (t['bp'] or '').lower() else None)
        fs = t['fs'] if isinstance(t['fs'], list) else []
        biases = [b for b in (bias_of(f.get('feature'), f.get('sentiment')) for f in fs if isinstance(f, dict)) if b]
        def fits(v, bias):
            return {'renovated': v['finish_level'] in ('renovated', 'premium'), 'street': v['view'] == 'street',
                    'ground': v['floor_band'] == 'ground', 'walkup_top': v['floor_band'] == 'top' and not v['building_has_elevator']}.get(bias, True)
        n = 1 + int(u('n', tid) * 3)
        picks, used = [], set()
        tap = next((x for x in taps.get(tid, []) if (cid, x['building_unit']) in attrs), None)
        if tap:
            picks.append((tap['building_unit'], tap['clicked_at'], 'app_tap')); used.add(tap['building_unit'])
        i = 0
        while len(picks) < n and i < 6:
            bias = biases[i % len(biases)] if biases else None
            cand = [v for v in pool if v['unit_key'] not in used and not v['is_model'] and (want_beds is None or v['bedrooms'] == want_beds)]
            cand = [v for v in cand if fits(v, bias)] or cand or [v for v in pool if v['unit_key'] not in used]
            if not cand: break
            v = min(cand, key=lambda v: u('pick', tid, i, v['unit_key']))
            picks.append((v['unit_key'], None, 'crm_units_shown' if u('src', tid, i) < 0.6 else 'manual_tag')); used.add(v['unit_key']); i += 1
        model = next((v for v in pool if v['is_model']), None)
        if model and u('model', tid) < 0.20 and model['unit_key'] not in used:
            if len(picks) >= 3: picks[-1] = (model['unit_key'], None, 'manual_tag')
            else: picks.append((model['unit_key'], None, 'manual_tag'))
        span = (end - start).total_seconds()
        rows = []
        for o, (k, at, src) in enumerate(picks, 1):
            if at is None:
                at = start + datetime.timedelta(seconds=span * o / (len(picks) + 1))
            elif at.tzinfo is None:
                at = at.replace(tzinfo=datetime.timezone.utc)
            rows.append(dict(tour_id=tid, community_id=cid, unit_key=k, shown_order=o, shown_at=at, source=src, seed_version=SEED))
        rows.sort(key=lambda r: r['shown_at'])
        for o, r in enumerate(rows, 1): r['shown_order'] = o
        shown[tid] = rows
        if t['leased'] and rows:
            x = u('lease', tid)
            if x < 0.45 or len(rows) == 1 and x < 0.875:
                k = rows[0]['unit_key']
            elif x < 0.80:
                k = rows[1 + int(u('later', tid) * (len(rows) - 1))]['unit_key'] if len(rows) > 1 else rows[0]['unit_key']
            elif x < 0.925:
                other = [v for v in pool if v['unit_key'] not in used and not v['is_model'] and (want_beds is None or v['bedrooms'] == want_beds)] \
                        or [v for v in pool if v['unit_key'] not in used]
                k = min(other, key=lambda v: u('unshown', tid, v['unit_key']))['unit_key'] if other else rows[0]['unit_key']
            else:
                k = rows[0]['unit_key']
            leases[tid] = dict(tour_id=tid, unit_key=k, lease_signed_on=t['leased_date'], source='demo_seed', seed_version=SEED)
        turns = t['tt'] if isinstance(t['tt'], list) else []
        rr = []
        for o, f in enumerate([f for f in fs if isinstance(f, dict) and f.get('feature')], 1):
            q = (f.get('quote') or '').strip()
            said = None
            if q:
                probe = q[:40].lower()
                turn = next((x for x in turns if isinstance(x, dict) and probe and probe in (x.get('text') or '').lower()), None)
                if turn and isinstance(turn.get('start_time'), (int, float)):
                    said = start + datetime.timedelta(seconds=float(turn['start_time']))
            unit = None
            if said is not None:
                for j, r in enumerate(rows):
                    nxt = rows[j + 1]['shown_at'] if j + 1 < len(rows) else end + datetime.timedelta(minutes=5)
                    if r['shown_at'] <= said < nxt: unit = r['unit_key']
            rr.append(dict(tour_id=tid, ordinal=o, feature=slug(f.get('feature')), sentiment=(f.get('sentiment') or None),
                           quote=q or None, said_at=said, unit_key=unit, quote_source='tour_analytics.feature_sentiments', seed_version=SEED))
        reacts[tid] = rr

    want = {'unit_attributes': list(attrs.values()), 'tour_unit_shown': [r for v in shown.values() for r in v],
            'tour_lease_unit': list(leases.values()), 'tour_unit_reactions': [r for v in reacts.values() for r in v]}
    undo = {'inserted': {}, 'replaced': {}, 'deleted': {}}; out = {}
    for table, rows in want.items():
        keys = KEYS[table]
        cur.execute(f'SELECT * FROM demo_seed.{table}')
        have = {tuple(str(r[k]) for k in keys): r for r in cur.fetchall()}
        wantmap = {tuple(str(r[k]) for k in keys): r for r in rows}
        ins = upd = dele = 0
        def same(a_, b_):
            return all(str(a_.get(c)) == str(b_.get(c)) for c in b_)
        for k, r in wantmap.items():
            cols = list(r)
            if k not in have:
                cur.execute(f"INSERT INTO demo_seed.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", [r[c] for c in cols])
                undo['inserted'].setdefault(table, []).append(list(k)); ins += 1
            elif not same(have[k], r):
                undo['replaced'].setdefault(table, []).append(have[k])
                cur.execute(f"UPDATE demo_seed.{table} SET {', '.join(c + '=%s' for c in cols)} WHERE {' AND '.join(c + '::text=%s' for c in keys)}",
                            [r[c] for c in cols] + list(k)); upd += 1
        for k, r in have.items():
            if k not in wantmap and str(r.get('seed_version')) == SEED:
                undo['deleted'].setdefault(table, []).append(r)
                cur.execute(f"DELETE FROM demo_seed.{table} WHERE {' AND '.join(c + '::text=%s' for c in keys)}", list(k)); dele += 1
        out[table] = {'rows': len(rows), 'inserted': ins, 'updated': upd, 'deleted': dele}
    # summary for the report
    st = want['tour_unit_shown']; lu = want['tour_lease_unit']
    first = {r['tour_id']: r['unit_key'] for r in st if r['shown_order'] == 1}
    shown_sets = {}
    for r in st: shown_sets.setdefault(r['tour_id'], set()).add(r['unit_key'])
    out['summary'] = dict(
        tours_with_shown=len(shown), shown_per_tour={n: sum(1 for v in shown.values() if len(v) == n) for n in (1, 2, 3, 4)},
        app_tap_first=sum(1 for v in shown.values() if v and v[0]['source'] == 'app_tap'),
        tours_with_model=sum(1 for v in shown.values() if any(attrs[(r['community_id'], r['unit_key'])]['is_model'] for r in v)),
        lease_first=sum(1 for r in lu if first.get(r['tour_id']) == r['unit_key']),
        lease_not_shown=sum(1 for r in lu if r['unit_key'] not in shown_sets.get(r['tour_id'], set())),
        reactions_with_quote=sum(1 for r in want['tour_unit_reactions'] if r['quote']),
        reactions_with_unit=sum(1 for r in want['tour_unit_reactions'] if r['unit_key']),
        finish_mix={f: sum(1 for v in attrs.values() if v['finish_level'] == f) for f in ('classic', 'renovated', 'premium')})
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
