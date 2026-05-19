import json, re, concurrent.futures as cf
from curl_cffi import requests as r
sites=json.load(open('/tmp/defic295.json'))
PAT={
 'rentcafe-securecafe': [r'securecafe\.com/onlineleasing', r'cdngeneral\.rentcafe\.com', r'RENTCafe', r'yardipcv'],
 'resman':              [r'myresman\.com', r'var\s+unitTypes'],
 'sightmap-engrain':    [r'sightmap\.com', r'engrain', r'\.sightmap\.'],
 'entrata':             [r'entrata\.com', r'prospectportal\.com', r'/conventional/'],
 'onesite-realpage':    [r'onesite', r'realpage', r'rpginleasing', r'\.rpg'],
 'appfolio':            [r'appfolio', r'appfoliowebsites'],
 'knock':               [r'knockrentals', r'knock-react', r'knock\.'],
 'g5':                  [r'g5-cl-', r'getg5', r'g5search'],
 'yardi':               [r'yardi', r'rentcafe'],
 'spherexx':            [r'spherexx', r'g5-'],
 'funnel':              [r'funnelleasing', r'nestiolistings'],
 'rently':              [r'rently\.com', r'use\.rently'],
}
EMB={'json-ld':r'application/ld\+json','xhr-api':r'/api/(availability|floorplans|units|pricing)','availunitrow':r"AvailUnitRow",'unittypes':r'var\s+unitTypes','sightmap-embed':r'sightmap\.com/embed'}
SUB=['','/floorplans/','/floor-plans/','/availability/','/apartments/','/floorplans']
def probe(s):
    pid,url=s['pid'],s['url']
    if not url.startswith('http'): url='https://'+url
    base=url.rstrip('/')
    found=set(); emb=set(); st=None; blocked=False; bytes_=0
    for sp in SUB[:3]:
        u=base+sp if sp else url
        try:
            resp=r.get(u,impersonate='chrome120',timeout=18,allow_redirects=True)
            st=resp.status_code; body=resp.text or ''; bytes_=max(bytes_,len(body))
            low=body.lower()
            if st in (403,429,503) or 'just a moment' in low or 'challenge-platform' in low: blocked=True
            for k,ps in PAT.items():
                if any(re.search(p,body,re.I) for p in ps): found.add(k)
            for k,p in EMB.items():
                if re.search(p,body,re.I): emb.add(k)
            if found or emb: break
        except Exception as e:
            st=f'ERR:{type(e).__name__}'
    return {'pid':pid,'url':url,'platforms':sorted(found),'embed':sorted(emb),'status':st,'blocked':blocked,'bytes':bytes_}
res=[]
with cf.ThreadPoolExecutor(max_workers=24) as ex:
    for x in ex.map(probe,sites): res.append(x)
json.dump(res,open('/tmp/probe295_results.json','w'))
from collections import Counter
pc=Counter()
for x in res:
    if x['platforms']:
        for p in x['platforms']: pc[p]+=1
    elif x['embed']: pc['(embed-only:'+','.join(x['embed'])+')']+=1
    elif x['blocked']: pc['CF-BLOCKED']+=1
    else: pc['no-signature']+=1
print("=== 295 platform probe ===")
for p,n in pc.most_common(): print(f"  {p:<40}{n:4d}")
adapter_cov=sum(1 for x in res if x['platforms'])
print(f"\non a KNOWN adapter platform: {adapter_cov}/295 ({100*adapter_cov/295:.0f}%)")
print(f"CF-blocked: {sum(1 for x in res if x['blocked'])}  no-sig: {sum(1 for x in res if not x['platforms'] and not x['embed'] and not x['blocked'])}")
