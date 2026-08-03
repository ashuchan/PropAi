#!/usr/bin/env python3
import csv, gzip, json, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests

root=Path('/private/tmp/propai-fnd-vBkmT9')
outdir=root/'unknown49_current'
outdir.mkdir(exist_ok=True)
with (root/'strict_recovery_remaining_current.csv').open(newline='',encoding='utf-8-sig') as f:
 rows=[r for r in csv.DictReader(f) if r.get('current_detected_adapter')=='unknown']

def norm(url):
 url=(url or '').strip()
 if url and not urlparse(url).scheme: url='https://'+url
 return url

def one(row):
 pid=int(row['property_id']); url=norm(row['website'])
 rec={**row,'requested_url':url}
 try:
  resp=requests.get(url,impersonate='chrome116',timeout=35,allow_redirects=True,
                    headers={'Accept-Language':'en-US,en;q=0.9'})
  body=resp.content or b''
  p=outdir/f'{pid}.html.gz'
  with gzip.open(p,'wb') as g:g.write(body)
  text=body.decode('utf-8','replace')
  m=re.search(r'<title[^>]*>(.*?)</title>',text,re.I|re.S)
  rec.update({'status':resp.status_code,'final_url':str(resp.url),'bytes':len(body),
              'content_type':resp.headers.get('content-type',''),'title':re.sub(r'\s+',' ',m.group(1)).strip() if m else '',
              'body_path':str(p),'error':''})
 except Exception as exc:
  rec.update({'status':None,'final_url':'','bytes':0,'content_type':'','title':'','body_path':'','error':f'{type(exc).__name__}: {exc}'})
 return rec

results=[]
with ThreadPoolExecutor(max_workers=10) as pool:
 futs={pool.submit(one,r):r for r in rows}
 for fut in as_completed(futs): results.append(fut.result())
results.sort(key=lambda r:int(r['property_id']))
(root/'evidence_unknown49_current_fetch.json').write_text(json.dumps(results,indent=2)+'\n')
for r in sorted(results,key=lambda x:-int(x.get('rp_oracle_native_unit_rows') or 0)):
 print(r['property_id'],r['rp_oracle_native_unit_rows'],r['status'],r['bytes'],r['final_url'],r['title'][:90],r['error'][:100])
