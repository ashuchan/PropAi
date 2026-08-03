#!/usr/bin/env python3
import csv, gzip, json, re
from pathlib import Path

from ma_poc.pms.detector import detect_pms

root=Path('/private/tmp/propai-fnd-vBkmT9')
rows=[]
with (root/'strict_recovery_remaining_current.csv').open(newline='',encoding='utf-8-sig') as f:
    rows=[r for r in csv.DictReader(f) if r.get('current_detected_adapter')=='unknown']

markers={
 'rentcafe': [r'rentcafe',r'yardi',r'securecafe'],
 'entrata': [r'entrata',r'prospectportal',r'property_floorplans'],
 'onesite': [r'realpage',r'leasing\.realpage',r'oll\.'],
 'knock': [r'knockdoorway',r'knockrentals',r'knock\.com'],
 'funnel_nestio': [r'funnelleasing',r'nestiolistings',r'nestio'],
 'sightmap': [r'sightmap'],
 'leaseleads': [r'leaseleads'],
 'rentmanager': [r'rentmanager',r'resman',r'myresman'],
 'appfolio': [r'appfolio'],
 'marketapts': [r'marketapts'],
 'engrain': [r'engrain'],
 'perq': [r'perq'],
 'encoreskyline': [r'encoreskyline',r'skyline'],
 'realpage_cws': [r'realpage\.com/WebServices',r'CrossFire',r'propertyid'],
 'respage': [r'respage'],
 'apts247': [r'apts247'],
 'resident360': [r'resident360'],
 'hyly': [r'hyly'],
 'bozzuto': [r'bozzuto'],
}
out=[]
for r in rows:
    pid=int(r['property_id']); p=root/'raw_all'/f'{pid}.html.gz'
    html=''; err=''
    try: html=gzip.open(p,'rt',encoding='utf-8',errors='replace').read()
    except Exception as e: err=f'{type(e).__name__}:{e}'
    d=detect_pms(r['website'],None,html)
    found={k:[pat for pat in pats if re.search(pat,html,re.I)] for k,pats in markers.items()}
    found={k:v for k,v in found.items() if v}
    out.append({**r,'archived_path':str(p),'archived_bytes':len(html.encode('utf8')),'read_error':err,
                'redetected_pms':d.pms,'redetected_confidence':d.confidence,'redetected_evidence':d.evidence,
                'marker_clusters':found})
(root/'evidence_unknown49_redetection.json').write_text(json.dumps(out,indent=2)+'\n')
for x in sorted(out,key=lambda x:-int(x['rp_oracle_native_unit_rows'] or 0)):
 print(x['property_id'],x['rp_oracle_native_unit_rows'],x['property_name'],x['redetected_pms'],x['redetected_confidence'],','.join(x['marker_clusters']))
