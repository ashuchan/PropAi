"""STAGING: apply ontour-ops jobs.test_sync.parker_share to the demo tours already in staging (the staging merge
applies the same rule to every later run). Dry run unless --apply; --apply needs --undo <new file>, written before
any write: per tour the original associate_id and the original value of every text column changed."""
import argparse, json, os, sys
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from psycopg2 import sql
from psycopg2.extras import Json
from jobs.demo_sync.config import load_db, DB_NAMES
from jobs.demo_sync.crypto import GuestCipher, fetch_key
from jobs.test_sync import parker_share as ps

ap = argparse.ArgumentParser(); ap.add_argument('--apply', action='store_true'); ap.add_argument('--undo'); a = ap.parse_args()
if a.apply and (not a.undo or os.path.exists(a.undo)):
    sys.exit('--apply needs --undo <new file>')
conn = psycopg2.connect(**load_db('staging')); c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
c.execute('SELECT current_database() d'); assert c.fetchone()['d'] == DB_NAMES['staging']
cipher = GuestCipher(fetch_key('staging', 'gcloud'))
c.execute("""SELECT t.tour_id::text tour_id, t.company_id::text company_id, t.community_id::text community_id, t.tour_type, t.created_at,
             t.associate_id::text associate_id, t.guest_uuid, a.first_name, a.last_name FROM tours t LEFT JOIN associates a ON a.id = t.associate_id
             WHERE t.company_id=%s AND t.community_id=%s""", (ps.DEMO_COMPANY, ps.COMMUNITY))
tours = [r for r in c.fetchall() if ps.picked(r)]
types = {}
for t in ps.TEXT_TABLES:
    c.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND data_type IN ('text','character varying','json','jsonb','ARRAY')", (t,))
    types[t] = {r['column_name']: r['data_type'] for r in c.fetchall() if r['column_name'] not in ('audio_file_url', 'transcription_job_id')}
from jobs.demo_sync.database import PK as pk
plan, undo = [], []
for t in tours:
    guest_first = None
    c.execute('SELECT first_name FROM guest_identity WHERE guest_uuid=%s', (t['guest_uuid'],)); g = c.fetchone()
    try: guest_first = cipher.decrypt(g['first_name']) if g else None
    except Exception: pass
    swap = ps.NameSwap(t['first_name'], t['last_name'], keep_bare_first=(guest_first == t['first_name']))
    changes = []
    for table, cols in types.items():
        key = pk[table]
        c.execute(sql.SQL('SELECT {} FROM public.{} WHERE tour_id=%s').format(sql.SQL(', ').join(map(sql.Identifier, [key] + sorted(cols))), sql.Identifier(table)), (t['tour_id'],))
        for row in c.fetchall():
            vals = {col: swap.walk(row[col]) for col in cols if row[col] is not None}
            vals = {k: v for k, v in vals.items() if v != row[k]}
            if vals: changes.append((table, key, str(row[key]), vals, {k: row[k] for k in vals}))
    plan.append((t, changes, swap.count, guest_first == t['first_name']))
    undo.append({'tour_id': t['tour_id'], 'associate_id': t['associate_id'],
                 'columns': [{'table': tb, 'pk': k, 'key': kv, 'original': orig} for tb, k, kv, _, orig in changes]})
print(json.dumps({'tours': len(plan), 'from': {f"{t['first_name']} {t['last_name']}": sum(1 for p in plan if p[0]['associate_id'] == t['associate_id']) for t, *_ in plan},
                  'days': sorted({str(t['created_at'])[:10] for t, *_ in plan}), 'name_swaps': sum(p[2] for p in plan),
                  'rows_changed': sum(len(p[1]) for p in plan), 'guest_shares_first_name': sum(1 for p in plan if p[3])}))
if not a.apply:
    print('DRY RUN: nothing written.'); sys.exit()
fd = os.open(a.undo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as fh: json.dump(undo, fh, default=str)
for t, changes, _, _ in plan:
    for table, key, kv, vals, _ in changes:
        if table == 'tours': continue
        assign = sql.SQL(', ').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in vals)
        c.execute(sql.SQL('UPDATE public.{} SET {} WHERE {}::text=%s').format(sql.Identifier(table), assign, sql.Identifier(key)),
                  [Json(v) if types[table][k] in ('json', 'jsonb') else v for k, v in vals.items()] + [kv]); assert c.rowcount == 1
    tv = next((v for tb, _, _, v, _ in changes if tb == 'tours'), {})
    assign = sql.SQL(', ').join([sql.SQL('associate_id=%s'), sql.SQL('updated_at=now()')] + [sql.SQL('{}=%s').format(sql.Identifier(k)) for k in tv])
    c.execute(sql.SQL('UPDATE public.tours SET {} WHERE tour_id::text=%s AND company_id=%s').format(assign),
              [ps.PARKER] + [Json(v) if types['tours'][k] in ('json', 'jsonb') else v for k, v in tv.items()] + [t['tour_id'], ps.DEMO_COMPANY]); assert c.rowcount == 1
conn.commit(); print('applied; undo', os.path.abspath(a.undo))
