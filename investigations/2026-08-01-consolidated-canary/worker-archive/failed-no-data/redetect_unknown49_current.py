#!/usr/bin/env python3
import gzip,json,re
from pathlib import Path
from ma_poc.pms.detector import detect_pms
root=Path('/private/tmp/propai-fnd-vBkmT9')
rows=json.loads((root/'evidence_unknown49_current_fetch.json').read_text())
markers={
 'rentcafe':[r'rentcafe',r'yardi',r'securecafe'], 'entrata':[r'entrata',r'prospectportal',r'property_floorplans'],
 'onesite':[r'leasing\.realpage',r'onlineleasing',r'realpage'], 'knock':[r'knockdoorway',r'knockrentals',r'knock\.com'],
 'funnel_nestio':[r'funnelleasing',r'nestiolistings',r'nestio'], 'sightmap':[r'sightmap'],
 'leaseleads':[r'leaseleads'], 'rentmanager':[r'rentmanager',r'resman',r'myresman'], 'appfolio':[r'appfolio'],
 'marketapts':[r'marketapts'], 'engrain':[r'engrain'], 'encoreskyline':[r'encoreskyline'], 'realpage_cws':[r'CrossFire',r'GetUnits',r'WebServices'],
 'realpage_oll_config':[r'realpageId',r'RealPageOnlineLeasing'], 'respage':[r'respage'], 'apts247':[r'apts247'],
 'resident360':[r'resident360'], 'hyly':[r'hyly'], 'perq':[r'perq'], 'rentpress':[r'rentpress-app',r'data-floorplans'],
 'engrain_asset':[r'engrain\.com',r'unitmap'], 'propertyboss':[r'propertyboss'], 'rentvision':[r'rentvision'],
 'showmojo':[r'showmojo'], 'buildium':[r'buildium'], 'tenantturner':[r'tenantturner'],
}
out=[]
for r in rows:
 html=''
 if r.get('body_path'):
  try:html=gzip.open(r['body_path'],'rt',encoding='utf8',errors='replace').read()
  except:pass
 d=detect_pms(r.get('final_url') or r['requested_url'],None,html)
 found={k:[pat for pat in pats if re.search(pat,html,re.I)] for k,pats in markers.items()}
 found={k:v for k,v in found.items() if v}
 scripts=re.findall(r'<script[^>]+src=["\']([^"\']+)',html,re.I)
 iframes=re.findall(r'<iframe[^>]+src=["\']([^"\']+)',html,re.I)
 links=re.findall(r'(?:href|action)=["\']([^"\']+)',html,re.I)
 candidates=[u for u in scripts+iframes+links if any(k in u.lower() for k in ['floor','avail','unit','lease','apply','rent','entrata','realpage','sightmap','engrain','knock','nestio','funnel','yardi','securecafe'])]
 out.append({**r,'redetected_pms':d.pms,'redetected_confidence':d.confidence,'redetected_evidence':d.evidence,'marker_clusters':found,'candidate_urls':candidates[:100]})
(root/'evidence_unknown49_current_redetection.json').write_text(json.dumps(out,indent=2)+'\n')
for x in sorted(out,key=lambda x:-int(x.get('rp_oracle_native_unit_rows') or 0)):
 print(x['property_id'],x['rp_oracle_native_unit_rows'],x['redetected_pms'],x['redetected_confidence'],','.join(x['marker_clusters']), 'urls',len(x['candidate_urls']))
