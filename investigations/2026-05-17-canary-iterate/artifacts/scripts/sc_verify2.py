import json,subprocess,re,random
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.rentcafe import _SECURECAFE_URL_RE, parse_securecafe_availableunits
def pull(rd):
    o=subprocess.run(['bash','-c',f"gsutil ls gs://jugnu-canary/runs/{rd}/ 2>/dev/null"],capture_output=True,text=True).stdout
    r=[]
    for sh in [l for l in o.split() if 'shard_' in l]:
        try:
            d=json.loads(subprocess.run(['gsutil','cat',sh+'properties.json'],capture_output=True,timeout=90).stdout)
            r+= d if isinstance(d,list) else [d]
        except Exception: pass
    return r
sc=[p for p in pull('2026-05-17-pl-securecafe') if (p.get('_extract_result') or {}).get('tier_used','').startswith('TIER_1_API_RENTCAFE_SECURECAFE') and p.get('units')]
random.seed(3)
out=[]
for p in random.sample(sc,min(12,len(sc))):
    w=(p.get('website') or '').rstrip('/'); 
    got=len([u for u in p['units'] if u.get('rent_low') or u.get('rent_high')])
    if not w: continue
    try:
        hp=probe_get(w+'/',timeout=20).text or ''
        m=_SECURECAFE_URL_RE.search(hp)
        if not m: out.append((w,got,'no-sc-link-on-home')); continue
        base=f"https://{m.group('sub')}.securecafe.com/onlineleasing/{m.group('slug')}"
        au=base+"/availableunits.aspx"
        h=probe_get(au,timeout=25).text or ''
        truth=len(re.findall(r"AvailUnitRow",h,re.I))
        parsed=len(parse_securecafe_availableunits(h,au))
        out.append((w,got,truth,parsed))
    except Exception as e:
        out.append((w,got,f'ERR:{type(e).__name__}'))
with open('/tmp/sc_verify2_out.txt','w') as f:
    okc=tot=0
    for row in out:
        if len(row)==4:
            w,g,t,pp=row; tot+=1
            verdict='OK' if g>=t*0.7 else f'UNDER (run {g} vs live {t})'
            if g>=t*0.7: okc+=1
            f.write(f"  {w[:42]:<43} run_extracted={g:>3} live_aspx_rows={t:>3} live_parsed={pp:>3}  {verdict}\n")
        else:
            f.write(f"  {row[0][:42]:<43} run_extracted={row[1]} -> {row[2]}\n")
    f.write(f"\n  comparable={tot}  not-systematically-undercounting (run>=70% of live): {okc}/{tot}\n")
    f.write("  (time-drift expected: availability changes day-to-day; flag only large systematic gaps)\n")
print("done")
