import json,re,html as H,concurrent.futures as cf
from curl_cffi import requests as r
res=json.load(open('/tmp/gc_vendor_res.json'))
sites=[u for u,v in res]
DET_PATHS=['','/floorplans/','/floor-plans/','/floorplan/','/floor-plan-cards/','/availability/','/vacancy/','/apartments/','/floorplans-and-pricing/','/living/floor-plans/?sort=unitrent&order=ASC']
LINK_RE=re.compile(r'href=["\']([^"\']*(?:floor-?plan|floorplan|availab|vacancy|/apartment|floor-plan-cards|online_app)[^"\']*)["\']',re.I)
def has_units(html):
    if not html: return None
    d=H.unescape(html).replace('\\/','/')
    # strong server-rendered signals from proven adapters
    if re.search(r'"available_units"\s*:\s*\[\s*{',d): return 'available_units_json'
    if 'AvailUnitRow' in d: return 'securecafe_availunitrow'
    if re.search(r'data-type=["\']uid["\']',d) and 'floorplan-detail__units' in d: return 'spherexx_zrs'
    if re.search(r'apts247|/api/v1/floorplans',d,re.I): return 'apts247'
    if re.search(r'data-base-unit-price=',d): return 'unit_price_attr'
    # generic: a unit/apt/suite token near a $rent near an availability cue
    for m in re.finditer(r'(?:apt|unit|suite|apartment)\s*#?\s*[A-Z]?\d{1,4}\b',d,re.I):
        w=d[m.start():m.start()+260]
        if re.search(r'\$\s?[1-9]\d{2,3}',w) and re.search(r'avail|now|\d{1,2}/\d{1,2}/\d{2,4}|move',w,re.I):
            return 'generic_unit_row'
    # >=4 distinct $rents AND >=4 distinct unit-ish numbers on one page = unit table
    rents=set(re.findall(r'\$\s?[1-9]\d{2,3}(?:\.\d+)?',d))
    if len(rents)>=4 and len(set(re.findall(r'\b[A-Z]?\d{2,4}\b(?=[^$]{0,40}\$)',d)))>=4:
        return 'multi_unit_table'
    return None
def fetch(u):
    uu=u if u.startswith('http') else 'https://'+u
    try:
        base=r.get(uu,impersonate='chrome120',timeout=12,allow_redirects=True)
        home=base.text or ''; origin=str(base.url).split('/')[0]+'//'+str(base.url).split('/')[2]
    except Exception as e:
        return (u,'DEAD',f'home-err:{type(e).__name__}')
    sig=has_units(home)
    if sig: return (u,'HAS_UNITS',f'home:{sig}')
    # collect candidate detail links from home + try known path templates
    cands=[]
    for m in LINK_RE.finditer(home):
        p=m.group(1)
        if p.startswith('http') or p.startswith('/'): cands.append(p if p.startswith('http') else origin+p)
    cands=list(dict.fromkeys(cands))[:6]
    for dp in DET_PATHS[1:]:
        cands.append(origin+dp)
    seen=set()
    for c in cands[:14]:
        if c in seen: continue
        seen.add(c)
        try:
            h=r.get(c,impersonate='chrome120',timeout=10,allow_redirects=True).text or ''
        except Exception: continue
        s=has_units(h)
        if s: return (u,'HAS_UNITS',f'detail:{s}')
        # 2nd level: from a floorplan index, follow a plan link
        if re.search(r'floor-?plan',c,re.I):
            sub=[]
            for m in LINK_RE.finditer(h):
                pp=m.group(1)
                if re.search(r'/(floor-?plan|floorplan|floor-plan-cards)s?/[a-z0-9-]+/?[a-z0-9-]*/?$',pp,re.I):
                    sub.append(pp if pp.startswith('http') else origin+pp)
            for sc in list(dict.fromkeys(sub))[:4]:
                try: hh=r.get(sc,impersonate='chrome120',timeout=10,allow_redirects=True).text or ''
                except Exception: continue
                if has_units(hh): return (u,'HAS_UNITS',f'plan-detail:{has_units(hh)}')
    return (u,'NEEDS_EYEBALL','no static unit signal (may be JS-rendered)')
out=[]
with cf.ThreadPoolExecutor(max_workers=40) as ex:
    for v in ex.map(fetch,sites): out.append(v)
from collections import Counter
c=Counter(x[1] for x in out)
json.dump(out,open('/tmp/probe456_res.json','w'))
with open('/tmp/probe456_out.txt','w') as f:
    f.write(f"ALL {len(sites)} genuine-custom — per-site automated unit check\n\n")
    for k,v in c.most_common(): f.write(f"  {k:<14}{v:5d}\n")
    bysig=Counter(x[2].split(':')[1] if x[1]=='HAS_UNITS' and ':' in x[2] else '' for x in out if x[1]=='HAS_UNITS')
    f.write("\nHAS_UNITS by signal:\n")
    for k,v in bysig.most_common(): f.write(f"  {k:<24}{v:4d}\n")
print("done",len(sites))
