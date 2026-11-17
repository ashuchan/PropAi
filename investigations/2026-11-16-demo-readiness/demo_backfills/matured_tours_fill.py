"""STAGING demo tenant (Paige Apartments, 3360791e): give every associate who toured in the last 30 days at least
MIN matured Full tours with a known outcome, so Ops -> Associates "Conversion vs Star Level" can plot them.

Why (coordinator 2026-11-17, Ankur's feedback): the plot uses the matured cohort (Full tours whose 30-day outcome
window has closed: tours from today-59..today-30 at 30D). Associates who started touring after Oct 12 have 0-2
such tours, so they are left off the plot.

What (dry run unless --commit), in ONE transaction:
  * re-applies the moves already in demo_seed.associate_move_ledger (the 06:30 merge rewrites tours and
    prospect_profiles from PROD, so moved tours revert to their original associate);
  * for each associate who toured (Full, completed) in the last 30 days with fewer than MIN matured Full tours, moves
    whole existing demo tours to them from a colleague at the SAME community: transcript, analysis, stars and outcome
    travel with the tour, and no outcome row is written (demo_seed.tour_outcomes does not depend on the associate,
    so the outcome model, the intent rule and the Low-intent overlay are unaffected).
    Donor tours: Full, completed, outcome window closed, tour day in today-59..today-30 (the most recent first, so
    they also fill the 7D matured window today-36..today-30 and stay in the 30D window longest); no coaching
    assignment, alert or fair-housing dismissal on the tour; the donor's first name is not in the transcript or the
    analysis. Donors are associates not touring in the last 30 days first, then active associates who keep at least
    DONOR_KEEP matured tours.
    Leased spread (Star Level): the associate's leased share among their matured tours targets
    p = 0.10 + 0.30 * (Star Level - 3.5), clamped to [0, 0.55], rounded on the final count; the moved tours are
    picked leased / not leased to meet it (a 4.3-star associate ends near 1 of 3, a 3.6-star one 0 of 3).
  * moves associate_id on the tour's rows (MOVE below), records (tour, from, to) in the ledger, and deletes the
    affected communities' cm_rollup_* rows (the API recomputes them, as after the merge).
Undo (written first, 0600, never overwritten): the moved tour ids with their original associate, plus the deleted
rollup rows; --undo-from <file> moves them back and removes them from the ledger.
Idempotent: a second run moves 0 tours. Staging only (refuses any DB not named ontour_db_staging), company 3360791e only.
Run order (reapply_after_merge.sh): after the shift, outcome seed, briefs, A9 and the Low-intent overlay.
"""
import argparse, datetime, hashlib, json, os, re, sys
sys.path.insert(0, os.environ.get('DEMO_SYNC_REPO', os.path.expanduser('~/ontour-ops-wt/staging-merge-rescrub')))
import psycopg2, psycopg2.extras
from jobs.demo_sync.config import load_db

COMPANY = '3360791e-8c3a-4f3f-87c8-d5abf4ab78ec'
MIN, DONOR_KEEP = 3, 8
# Staged / test recordings in the demo tenant that the product scored as real tours: marked ANALYTICS_FAILED (the
# dashboards' invalid state, as for any recording that could not be scored). Re-applied after every merge.
# 71157236: Morgan W., Prestige Park, Oct 19: a 509-character staged clip ("crazy apartment", "$200K" for a 2-bed),
# rated 1.2 / AI score 9; it was Morgan's only tour in the 30D period and stretched Conversion vs Star Level to 1.0
# (coordinator 11-17). Its rating is not changed.
EXCLUDE = {'71157236-0284-42b1-a83f-327cac221744': 'staged 509-char clip, not a tour'}
VALID = ('ANALYTICS_FINISHED', 'FINISHED', 'INSIGHTS_OPENED', 'COACHING_OPENED', 'OBJECTION_OPENED',
         'SCORE_TRANSCRIPT_OPENED', 'FOLLOWUP_COPIED', 'TOUR_FOLLOWED_UP', 'REWARD_AWARDED')
# (table, associate column) rows keyed by tour_id that move with the tour
MOVE = [('public.tours', 'associate_id'), ('public.tours', 'acted_by_associate_id'), ('public.prospect_profiles', 'associate_id'),
        ('canon.app_tour', 'associate_id'), ('public.tour_events', 'associate_id'), ('public.tour_notifications', 'associate_id'),
        ('public.tour_availability_interactions', 'associate_id')]
BLOCK = ('public.tm_lesson_assignments', 'public.tm_alert_augmentations', 'public.fh_alert_dismissals')
ROLLUPS = ('cm_rollup_associate_day', 'cm_rollup_category_day', 'cm_rollup_days', 'cm_rollup_watermarks')
LEDGER_DDL = """CREATE TABLE IF NOT EXISTS demo_seed.associate_move_ledger (
    tour_id        uuid PRIMARY KEY,
    from_associate uuid NOT NULL,
    to_associate   uuid NOT NULL,
    moved_at       timestamptz NOT NULL DEFAULT now());
COMMENT ON TABLE demo_seed.associate_move_ledger IS
    'Staging demo tenant only: demo tours moved to a colleague at the same community so every touring associate has 3 matured tours (demo_backfills/matured_tours_fill.py). Never real data.';"""
FULL = """coalesce(t.tour_type, 'full_tour')='full_tour' and t.status in (""" + ','.join(f"'{v}'" for v in VALID) + ')'
DAY = "(t.created_at at time zone 'UTC' at time zone 'America/Chicago')::date"


def u(tag, tid):
    return int(hashlib.sha256(f'{tag}:{tid}'.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def connect():
    c = load_db('staging')
    conn = psycopg2.connect(host=c['host'], port=c['port'], dbname=c['dbname'], user=c['user'], password=c['password'],
                            options='-c statement_timeout=120000 -c lock_timeout=15000', application_name='matured_tours_fill')
    with conn.cursor() as cur:
        cur.execute('select current_database()'); db = cur.fetchone()[0]
    if db != 'ontour_db_staging':
        raise SystemExit(f'refusing database {db}')
    return conn


def move(cur, tid, frm, to, counts):
    for table, col in MOVE:
        cur.execute(f'update {table} set {col}=%s where tour_id=%s and {col}=%s', (to, tid, frm))
        counts[f'{table}.{col}'] = counts.get(f'{table}.{col}', 0) + cur.rowcount


def tours(cur):
    cur.execute(f"""select t.tour_id::text tour_id, t.associate_id::text aid, t.community_id::text cid, {DAY} d, t.star_rating,
                           o.window_closed, o.leased, a.first_name,
                           exists (select 1 from public.tm_lesson_assignments x where x.tour_id=t.tour_id)
                        or exists (select 1 from public.tm_alert_augmentations x where x.tour_id=t.tour_id)
                        or exists (select 1 from public.fh_alert_dismissals x where x.tour_id=t.tour_id) blocked,
                           coalesce(t.transcript_text, '') || ' ' || coalesce(t.client_transcript_text, '') || ' ' || coalesce(row_to_json(ta)::text, '') txt
                      from tours t left join demo_seed.tour_outcomes o on o.tour_id=t.tour_id
                      left join tour_analytics ta on ta.tour_id=t.tour_id left join associates a on a.id=t.associate_id
                     where t.company_id=%s and {FULL} and t.created_at > now() - interval '130 days'
                       and coalesce(t.audio_file_url, '') not like '%%/ontour-files-ontour-485600/%%'""", (COMPANY,))
    return cur.fetchall()


def named(r):
    """True when the tour's current associate's first name appears in its transcript or analysis."""
    fn = (r['first_name'] or '').strip()
    return len(fn) >= 2 and bool(re.search(r'\b' + re.escape(fn) + r'\b', r['txt'], re.I))


def plan(rows, today, ledger):
    w30 = (today - datetime.timedelta(days=59), today - datetime.timedelta(days=30))
    cur30 = today - datetime.timedelta(days=29)
    matured = lambda r: w30[0] <= r['d'] <= w30[1] and r['window_closed'] and r['leased'] is not None
    active, mat, stars, home = set(), {}, {}, {}
    for r in rows:
        a = r['aid']
        if r['d'] >= cur30:
            active.add(a); home.setdefault(a, {}); home[a][r['cid']] = home[a].get(r['cid'], 0) + 1
            if r['star_rating'] and 1 <= r['star_rating'] <= 5: stars.setdefault(a, []).append(r['star_rating'])
        if matured(r): mat.setdefault(a, []).append(r)
    short = sorted((a for a in active if len(mat.get(a, [])) < MIN), key=lambda a: (len(mat.get(a, [])), a))
    picks, report = [], []
    for a in short:
        cid = max(home[a].items(), key=lambda kv: (kv[1], kv[0]))[0]
        star = sum(stars.get(a, [0])) / max(len(stars.get(a, [])), 1)
        p = min(0.55, max(0.0, 0.10 + 0.30 * (star - 3.5)))
        have = list(mat.get(a, [])); need = MIN - len(have)
        want_leased = max(0, min(need, round(MIN * p) - sum(1 for r in have if r['leased'])))
        def ok(r):
            if r['cid'] != cid or r['aid'] == a or r['aid'] in short or r['tour_id'] in ledger or r['blocked'] or not matured(r):
                return False
            if named(r):
                return False
            return r['aid'] not in active or len(mat.get(r['aid'], [])) > DONOR_KEEP
        order = lambda r: (r['aid'] in active, -r['d'].toordinal(), u('matured-fill', r['tour_id']))
        got = []
        for leased, k in ((True, want_leased), (False, need - want_leased)):
            for r in sorted((r for r in rows if ok(r) and bool(r['leased']) == leased), key=order)[:k]:
                got.append(r); ledger.add(r['tour_id'])
                mat[r['aid']] = [x for x in mat[r['aid']] if x['tour_id'] != r['tour_id']]
                mat.setdefault(a, []).append(r)
        picks += [(r['tour_id'], r['aid'], a, cid) for r in got]
        report.append(dict(associate=a[:8], community=cid[:4], star=round(star, 2), matured_before=len(have),
                           leased_before=sum(1 for r in have if r['leased']), moved=len(got),
                           moved_leased=sum(1 for r in got if r['leased']), matured_after=len(have) + len(got)))
    return picks, report


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--commit', action='store_true'); ap.add_argument('--undo', help='new undo file (required with --commit)')
    ap.add_argument('--undo-from', help='apply this undo file instead'); a = ap.parse_args()
    conn = connect(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('select name from companies where id=%s', (COMPANY,)); assert cur.fetchone()['name'] == 'Paige Apartments'
    counts = {}
    if a.undo_from:
        u_ = json.load(open(a.undo_from))
        for m in u_['moves']:
            move(cur, m['tour_id'], m['to'], m['from'], counts)
            cur.execute('delete from demo_seed.associate_move_ledger where tour_id=%s', (m['tour_id'],))
        for e in u_.get('excluded', []):
            cur.execute("update tours set status=%s where tour_id=%s and status='ANALYTICS_FAILED'", (e['status'], e['tour_id']))
            counts['unexcluded'] = counts.get('unexcluded', 0) + cur.rowcount
        cur.execute('delete from cm_rollup_watermarks where community_id=any(%s::uuid[])', (sorted({m['community_id'] for m in u_['moves']}),))
        print(json.dumps({'undo_moves': len(u_['moves']), **counts}))
        conn.commit(); print('committed'); return
    cur.execute(LEDGER_DDL)
    # demo-company tours to demo associates only; tours recorded on staging never move (ontour-ops #119 review). The
    # original associate is not required to be demo-mapped: some demo tours belong to an associate mapped to another
    # company (ad2d1498), and moving a demo tour away from them touches no other tenant's rows.
    cur.execute("""select l.tour_id::text, l.from_associate::text, l.to_associate::text from demo_seed.associate_move_ledger l
                     join tours t on t.tour_id=l.tour_id and t.company_id=%s
                    where coalesce(t.audio_file_url, '') not like '%%/ontour-files-ontour-485600/%%'
                      and l.to_associate in (select associate_id from associate_company_map where company_id=%s)""", (COMPANY, COMPANY))
    led = cur.fetchall()
    # 1. re-apply the ledger (only rows the merge put back on the original associate move)
    reapplied = 0
    for m in led:
        before = sum(counts.values()); move(cur, m['tour_id'], m['from_associate'], m['to_associate'], counts)
        reapplied += sum(counts.values()) > before
    # 2. invalidate staged recordings (EXCLUDE): the original status goes to the undo file
    cur.execute("select tour_id::text, status from tours where tour_id=any(%s::uuid[]) and company_id=%s and status<>'ANALYTICS_FAILED' and coalesce(audio_file_url, '') not like '%%/ontour-files-ontour-485600/%%'",
                (list(EXCLUDE), COMPANY))
    excl = cur.fetchall()
    for e in excl:
        cur.execute("update tours set status='ANALYTICS_FAILED' where tour_id=%s and status=%s", (e['tour_id'], e['status']))
        counts['excluded'] = counts.get('excluded', 0) + cur.rowcount
    # 3. top up associates still short
    rows = tours(cur)
    picks, report = plan(rows, datetime.date.today(), {m['tour_id'] for m in led})
    print(json.dumps({'ledger': len(led), 'ledger_reapplied_tours': reapplied, 'excluded': len(excl), 'new_moves': len(picks), 'commit': a.commit}))
    for r in report: print(json.dumps(r))
    comms = sorted({p[3] for p in picks} | ({r['cid'] for r in rows if r['tour_id'] in {m['tour_id'] for m in led}} if reapplied else set()))
    rollups = {}
    for r in ROLLUPS:
        cur.execute(f'select * from public.{r} where community_id=any(%s::uuid[])', (comms,)); rollups[r] = cur.fetchall()
    if a.commit and (picks or reapplied or excl):
        if not a.undo or os.path.exists(a.undo): raise SystemExit('--undo <new file> required')
        fd = os.open(a.undo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            cmap = {r['tour_id']: r['cid'] for r in rows}
            allm = [(m['tour_id'], m['from_associate'], m['to_associate'], cmap.get(m['tour_id'])) for m in led] + picks
            json.dump({'moves': [{'tour_id': t, 'from': fr, 'to': to, 'community_id': c} for t, fr, to, c in allm if c],
                       'ledger_reapplied': reapplied, 'excluded': excl, 'rollups': rollups, 'written_at': datetime.datetime.utcnow().isoformat()}, f, default=str)
    for t, fr, to, c in picks:
        move(cur, t, fr, to, counts)
        cur.execute('insert into demo_seed.associate_move_ledger (tour_id, from_associate, to_associate) values (%s, %s, %s) on conflict do nothing', (t, fr, to))
    if excl:
        cur.execute('select distinct community_id::text c from tours where tour_id=any(%s::uuid[])', ([e['tour_id'] for e in excl],))
        comms = sorted(set(comms) | {r['c'] for r in cur.fetchall()})
    if picks or reapplied or excl:
        for r in ROLLUPS:
            cur.execute(f'delete from public.{r} where community_id=any(%s::uuid[])', (comms,)); counts['deleted:' + r] = cur.rowcount
    print(json.dumps(counts))
    if a.commit and (picks or reapplied or excl):
        conn.commit(); print('committed')
    else:
        conn.rollback(); print('dry run: rolled back' if not a.commit else 'nothing to do')


if __name__ == '__main__':
    main()
