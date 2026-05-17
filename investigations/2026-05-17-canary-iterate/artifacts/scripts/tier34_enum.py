import json,subprocess,concurrent.futures as cf
paths=[l.strip() for l in open('/tmp/prod0517_paths.txt') if l.strip()]
led=json.load(open('/tmp/domllm_ledger.json'))
u2p={(v.get('url') or '').strip().lower().rstrip('/'):v.get('platform') for v in led.values() if v.get('url')}
def fetch(p):
    try:
        d=json.loads(subprocess.run(['gsutil','cat',p],capture_output=True,timeout=90).stdout)
        out=[]
        for r in (d if isinstance(d,list) else []):
            er=r.get('_extract_result') or {}; t=er.get('tier_used') or ''
            if t.startswith('TIER_3_DOM') or t.startswith('TIER_4_LLM'):
                u=r.get('units') or []
                rr=[z for z in u if str(z.get('unit_id') or '') and not str(z.get('unit_id')).startswith('inferred_') and (z.get('rent_low') or z.get('rent_high'))]
                w=(r.get('website') or '').strip()
                out.append((w, t, u2p.get(w.lower().rstrip('/'),'?'), len(u), len(rr)))
        return out
    except Exception: return []
rows=[]
with cf.ThreadPoolExecutor(max_workers=20) as ex:
    for s in ex.map(fetch,paths): rows.extend(s)
from collections import Counter
byplat=Counter(r[2] for r in rows)
bytier=Counter(r[1] for r in rows)
nounit=[r for r in rows if r[4]==0]
with open('/tmp/tier34_out.txt','w') as f:
    f.write(f"TIER_3_DOM + TIER_4_LLM* population: {len(rows)} props ({len(nounit)} with 0 true unit-level)\n\n")
    f.write("by static-fingerprint platform (the cluster map for Chrome-probe):\n")
    for k,v in byplat.most_common(20): f.write(f"  {k:<22}{v:5d}\n")
    f.write("\nby winning tier:\n")
    for k,v in bytier.most_common(8): f.write(f"  {k:<26}{v:5d}\n")
    # sample URLs per top platform cluster for Chrome-MCP probing
    f.write("\nsample no-unit URLs per top cluster:\n")
    import collections
    ex2=collections.defaultdict(list)
    for w,t,pl,nu,rr in rows:
        if rr==0 and w: ex2[pl].append(w)
    for pl,_ in byplat.most_common(10):
        f.write(f"  [{pl}] "+" | ".join(ex2[pl][:4])+"\n")
json.dump([{'url':r[0],'tier':r[1],'plat':r[2]} for r in rows], open('/tmp/tier34_pop.json','w'))
print("done", len(rows))
