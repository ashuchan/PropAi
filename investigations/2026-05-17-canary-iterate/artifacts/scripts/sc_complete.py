import json,subprocess,re,random
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.rentcafe import _find_securecafe_base  # discovery helper
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
random.seed(11)
rows=[]
for p in random.sample(sc,min(15,len(sc))):
    got=len([u for u in p['units'] if u.get('rent_low') or u.get('rent_high')])
    # find the securecafe availableunits.aspx via the stored api_responses url
    aurl=None
    for ar in (p.get('_extract_result') or {}).get('api_responses') or []:
        u=str(ar.get('url') or '')
        if 'availableunits' in u.lower(): aurl=u; break
    if not aurl: aurl=str((p.get('_extract_result') or {}).get('winning_url') or '')
    truth='?'
    try:
        if 'availableunits' in aurl.lower():
            h=probe_get(aurl,timeout=25).text or ''
            truth=len(re.findall(r"AvailUnitRow",h,re.I))
        else:
            truth=f'no-aspx-url'
    except Exception as e: truth=f'ERR:{type(e).__name__}'
    rows.append((p.get('website'),got,truth,aurl[:60]))
with open('/tmp/sc_complete_out.txt','w') as f:
    okc=tot=0
    for w,g,t,u in rows:
        if isinstance(t,int):
            tot+=1; okc+= 1 if g>=t else 0
            flag='' if g>=t else f'  UNDER by {t-g}'
        else: flag=''
        f.write(f"  {str(w)[:42]:<43} extracted={g:>3} aspx_AvailUnitRows={t}{flag}\n")
    f.write(f"\n  COMPLETE (extracted>=aspx rows): {okc}/{tot}\n")
print("done")
