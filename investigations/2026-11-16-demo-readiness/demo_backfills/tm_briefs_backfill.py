"""STAGING demo tenant: generate the Learning hero briefs (tm_narrative_briefs) and tour-summary prose
(tm_tour_summary_snapshots) for 7 and 90 days (+30 where missing) per demo community, through the product's own
generator (aiservice snapshot_job/tm_snapshot.py build_tm_narrative_brief / build_tm_tour_summary). The nightly job
only builds 30-day rows (TM_PERIOD_DAYS), and none exist for Prestige Park.

Run with AISERVICE_REPO=<aiservice checkout at origin/staging>, DB_* from the ops staging config (run.sh exports
them), GEMINI_NARRATIVE_MODEL=gemini-3.5-flash. Dry run (model calls, no writes) unless --commit. Refuses any DB not
named ontour_db_staging. Writes are upserts keyed (community, period_days, period_end_date, tab|kind); the undo file
lists the keys written (delete them to undo). A re-run for the same date overwrites the same keys (no new rows).
Persistence: the 06:30 merge rewrites the demo communities' rows from PROD, so run after 07:10 CT.
"""
import argparse, datetime, json, os, sys
if os.environ.get('GCLOUD_TOKEN'):   # stale ADC: authenticate Vertex with a fresh `gcloud auth print-access-token`
    import google.auth, google.auth._default
    from google.oauth2.credentials import Credentials
    _d = lambda *a, **k: (Credentials(token=os.environ['GCLOUD_TOKEN']), os.environ.get('GOOGLE_CLOUD_PROJECT', 'ontour-485600'))
    google.auth.default = _d; google.auth._default.default = _d
sys.path.insert(0, os.path.join(os.environ['AISERVICE_REPO'], 'snapshot_job'))
sys.path.insert(0, os.environ['AISERVICE_REPO'])
from snapshot_helpers import get_conn  # noqa: E402
from tm_snapshot import build_tm_narrative_brief, build_tm_tour_summary  # noqa: E402

COMMUNITIES = {'cad6d9d4-5881-40ed-9727-73b60ad1a375': 'Prestige Park', 'f585b747-ff97-4d43-987b-1e0882f029eb': 'Cedar Point',
               '24589e94-c8a1-419d-95fe-314a0aa14332': 'Willow Run', '85ec7ed8-5d5f-49cb-af16-e8c34aced869': 'Stonebridge',
               '076a07bd-beec-4e8b-85a4-afee64fd6ab4': 'Maple Grove', '9254d356-a920-5e47-8793-e5f1808c4d41': 'Ivywood'}

ap = argparse.ArgumentParser(); ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo')
ap.add_argument('--periods', default='7,90'); ap.add_argument('--only'); ap.add_argument('--date')
a = ap.parse_args()
ref = datetime.date.fromisoformat(a.date) if a.date else datetime.date.today() - datetime.timedelta(days=1)
conn = get_conn()
with conn.cursor() as cur:
    cur.execute('select current_database()'); db = cur.fetchone()[0]
if db != 'ontour_db_staging':
    raise SystemExit(f'refusing {db}')
plan = []
for cid, name in COMMUNITIES.items():
    if a.only and a.only not in (cid, name): continue
    periods = [int(p) for p in a.periods.split(',')]
    with conn.cursor() as cur:
        cur.execute('select 1 from tm_narrative_briefs where community_id=%s and period_days=30 and period_end_date>=%s', (cid, ref - datetime.timedelta(days=3)))
        if not cur.fetchone() and 30 not in periods: periods.append(30)
    plan += [(cid, name, p) for p in periods]
if a.commit:
    if not a.undo or os.path.exists(a.undo): raise SystemExit('--undo <new file> required')
    fd = os.open(a.undo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'period_end_date': str(ref), 'keys': [{'community_id': c, 'period_days': p} for c, _, p in plan],
                   'undo_sql': "delete from tm_narrative_briefs where community_id=%s and period_days=%s and period_end_date=%s; "
                               "delete from tm_tour_summary_snapshots where community_id=%s and period_days=%s and period_end_date=%s"}, f)
out = []
for cid, name, p in plan:
    n1 = build_tm_narrative_brief(conn, cid, name, ref, p, not a.commit)
    n2 = build_tm_tour_summary(conn, cid, name, ref, p, not a.commit)
    if a.commit: conn.commit()
    out.append((name, p, n1, n2)); print(name, p, 'briefs', n1, 'summaries', n2, flush=True)
print(json.dumps({'calls': len(plan) * 3, 'rows': out, 'commit': a.commit}))
