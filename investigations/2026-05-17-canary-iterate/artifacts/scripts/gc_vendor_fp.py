import json,re,concurrent.futures as cf
from curl_cffi import requests as r
pop=json.load(open('/tmp/tier34_pop.json'))
GC={'none','?','wordpress','duda','wix','squarespace','elfsight'}
seen=set(); sites=[]
for p in pop:
    if p.get('plat') in GC:
        u=(p.get('url') or '').strip()
        if u and u.rstrip('/') not in seen: seen.add(u.rstrip('/')); sites.append(u)
VEND=[('jonahdigital',r'jonahdigital\.com'),('repli360',r'repli360\.com'),
 ('marketapts',r'marketapts\.com|marketapartments'),('365rs_digilease',r'365residentservices|digi\.lease'),
 ('dynasty',r'rent\.dynasty\.com|dynasty\.com/'),('caf_v2',r'/assets/(?:css|javascript)/community/version2|visitdata-\d'),
 ('duda',r'static\.cdn-website\.com|dudamobile'),('milestone',r'milestoneinternet\.com'),
 ('repli',r'repli\b'),('squarespace',r'squarespace\.com'),('wix',r'parastorage|wix\.com'),
 ('knock_widget',r'knockrentals'),('rentcafe_w',r'rentcafe\.com'),('entrata_w',r'entrata\.com|prospectportal'),
 ('resman_w',r'myresman\.com|/Portal/Applicants/Availability'),('sightmap_w',r'sightmap\.com'),
 ('realpage_w',r'realpage\.com|onlineleasing'),('appfolio_w',r'appfolio'),('rentmanager_w',r'rentmanager\.com'),
 ('g5_w',r'getg5|g5-cl-'),('funnel_w',r'nestiolistings|funnelleasing'),('spherexx_w',r'spherexx|apts247')]
def chk(u):
    uu=u if u.startswith('http') else 'https://'+u
    try:
        h=r.get(uu,impersonate='chrome120',timeout=12,allow_redirects=True).text or ''
    except Exception: return (u,'ERR')
    for n,pat in VEND:
        if re.search(pat,h,re.I): return (u,n)
    return (u,'truly_bespoke')
res=[]
with cf.ThreadPoolExecutor(max_workers=40) as ex:
    for v in ex.map(chk,sites): res.append(v)
from collections import Counter
c=Counter(x[1] for x in res)
with open('/tmp/gc_vendor_sized.txt','w') as f:
    f.write(f"genuine-custom population sized: {len(sites)} sites\n\n")
    for k,v in c.most_common(): f.write(f"  {k:<22}{v:5d}\n")
    KNOWN={'resman_w','sightmap_w','realpage_w','appfolio_w','rentcafe_w','entrata_w','g5_w','funnel_w','spherexx_w','knock_widget','rentmanager_w'}
    TEMPL={'jonahdigital','repli360','marketapts','365rs_digilease','dynasty','caf_v2','duda','milestone','repli','squarespace','wix'}
    f.write(f"\n  => MISFP known-PMS (existing adapters): {sum(v for k,v in c.items() if k in KNOWN)}\n")
    f.write(f"  => vendor-TEMPLATE clusters (generic detail-crawl target): {sum(v for k,v in c.items() if k in TEMPL)}\n")
    f.write(f"  => truly bespoke: {c.get('truly_bespoke',0)}  | ERR/dead: {c.get('ERR',0)}\n")
json.dump(res,open('/tmp/gc_vendor_res.json','w'))
print("done",len(sites))
