import json,subprocess,re,random,asyncio
from ma_poc.pms.adapters.apts247 import find_apts247_api_key,_fetch as af
from ma_poc.pms.adapters._probe import probe_get
def pull(rd):
    o=subprocess.run(['bash','-c',f"gsutil ls gs://jugnu-canary/runs/{rd}/ 2>/dev/null"],capture_output=True,text=True).stdout
    r=[]
    for sh in [l for l in o.split() if 'shard_' in l]:
        try:
            d=json.loads(subprocess.run(['gsutil','cat',sh+'properties.json'],capture_output=True,timeout=90).stdout)
            r+= d if isinstance(d,list) else [d]
        except Exception: pass
    return r
random.seed(7)
# APTS247: extracted vs API nested-units truth
ap=[p for p in pull('2026-05-17-apts247-i16') if (p.get('_extract_result') or {}).get('tier_used','').startswith('TIER_1_API_APTS247') and p.get('units')]
async def a247():
    res=[]
    for p in random.sample(ap,min(15,len(ap))):
        w=(p.get('website') or '').rstrip('/')
        if not w: continue
        try:
            k=find_apts247_api_key(await af(w+'/'))
            j=json.loads(await af(f"{w}/api/v1/floorplans/?api_key={k}"))
            truth=sum(len(o.get('units') or []) for o in j.get('objects',[]))
            got=len([u for u in p['units'] if u.get('rent_low') or u.get('rent_high')])
            res.append((w,got,truth))
        except Exception: res.append((w,len(p.get('units') or []),'ERR'))
    return res
# SECURECAFE: extracted vs AvailUnitRow count on availableunits.aspx
sc=[p for p in pull('2026-05-17-pl-securecafe') if (p.get('_extract_result') or {}).get('tier_used','').startswith('TIER_1_API_RENTCAFE_SECURECAFE') and p.get('units')]
def scchk():
    res=[]
    for p in random.sample(sc,min(15,len(sc))):
        wu=((p.get('_extract_result') or {}).get('winning_url') or '')
        got=len([u for u in p['units'] if u.get('rent_low') or u.get('rent_high')])
        try:
            if 'availableunits' in wu:
                h=probe_get(wu,timeout=20).text or ''
                truth=len(re.findall(r"class=['\"]AvailUnitRow",h,re.I))
            else: truth='no-url'
        except Exception: truth='ERR'
        res.append((p.get('website'),got,truth))
    return res
r1=asyncio.run(a247()); r2=scchk()
with open('/tmp/completeness_out.txt','w') as f:
    def emit(tag,rows):
        ok=tot=0
        f.write(f"=== {tag} (extracted vs source-truth) ===\n")
        for w,g,t in rows:
            comp = isinstance(t,int) and g>=max(t-0,0) and t>0
            if isinstance(t,int) and t>0: tot+=1; ok+= 1 if g>=t else 0
            f.write(f"  {str(w)[:46]:<47} got={g} truth={t}{'' if (isinstance(t,int) and g>=t) else ' UNDER' if isinstance(t,int) else ''}\n")
        f.write(f"  COMPLETE {ok}/{tot} (got>=source truth)\n\n")
    emit("APTS247",r1); emit("SECURECAFE",r2)
print("done")
