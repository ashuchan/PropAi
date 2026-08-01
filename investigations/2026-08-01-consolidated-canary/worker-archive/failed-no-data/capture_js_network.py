#!/usr/bin/env python3
import argparse, asyncio, hashlib, json, re
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

async def main():
 ap=argparse.ArgumentParser();ap.add_argument('property_id');ap.add_argument('url');ap.add_argument('--settle',type=int,default=12);args=ap.parse_args()
 root=Path('/private/tmp/propai-fnd-vBkmT9/unknown49_current'); root.mkdir(exist_ok=True)
 records=[]
 async with async_playwright() as p:
  b=await p.chromium.launch(headless=True)
  page=await b.new_page(viewport={'width':1440,'height':1000})
  async def route(r):
   if r.request.resource_type in {'image','font','media'}: await r.abort()
   else: await r.continue_()
  await page.route('**/*',route)
  async def on_response(resp):
   ct=(resp.headers.get('content-type') or '').lower(); rec={'url':resp.url,'status':resp.status,'content_type':ct,'resource_type':resp.request.resource_type}
   if any(x in ct for x in ['json','html','text','javascript','xml']) and resp.request.resource_type in {'xhr','fetch','document'}:
    try:
     body=await resp.body()
     if len(body)<=2_000_000:
      digest=hashlib.sha256(resp.url.encode()).hexdigest()[:16]; ext='json' if 'json' in ct else 'txt'; path=root/f"{args.property_id}_net_{digest}.{ext}";path.write_bytes(body);rec['body_path']=str(path);rec['bytes']=len(body)
    except Exception as e: rec['capture_error']=f'{type(e).__name__}:{e}'
   records.append(rec)
  page.on('response',on_response)
  err=''
  try:
   await page.goto(args.url,wait_until='domcontentloaded',timeout=45000)
   await page.wait_for_timeout(args.settle*1000)
  except Exception as e:err=f'{type(e).__name__}:{e}'
  rendered=root/f'{args.property_id}_rendered.html';rendered.write_text(await page.content(),encoding='utf8')
  result={'property_id':int(args.property_id),'requested_url':args.url,'final_url':page.url,'title':await page.title(),'error':err,'rendered_html_path':str(rendered),'network':records}
  out=root/f'{args.property_id}_network.json';out.write_text(json.dumps(result,indent=2)+'\n')
  print(json.dumps({'output':str(out),'final_url':page.url,'title':result['title'],'responses':len(records),'captured':[r for r in records if r.get('body_path')]},indent=2))
  await b.close()
asyncio.run(main())
