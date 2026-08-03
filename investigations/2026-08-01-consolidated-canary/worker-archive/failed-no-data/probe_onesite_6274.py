#!/usr/bin/env python3
"""Read-only exact RealPage OLL workflow probe for property 6274."""

from __future__ import annotations

import json
from pathlib import Path

from curl_cffi import requests

from ma_poc.pms.adapters.onesite import (
    _XYZ_IMPERSONATE_CHAIN,
    _XYZ_USER_AGENT,
    _generate_xyz_token,
    _onesite_workflowstartup_url,
    parse_onesite_workflowstartup,
)


SITE_ID = "1043860"
PAGE = "https://www.fmgnj.com/apartments/nj/voorhees/the-village-apartments/floor-plans-apply"
RAW = Path("/private/tmp/vendor_tail_6274_workflow.json")
EVIDENCE = Path("/private/tmp/propai-fnd-vBkmT9/evidence_onesite_6274_direct.json")


def main() -> None:
    url = _onesite_workflowstartup_url(SITE_ID)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.fmgnj.com",
        "Referer": PAGE,
        "User-Agent": _XYZ_USER_AGENT,
        "xyz": _generate_xyz_token(SITE_ID, _XYZ_USER_AGENT),
        "X-AuthToken": "",
        "X-Phased": "",
    }
    attempts = []
    body_text = ""
    winning_imp = ""
    for imp in _XYZ_IMPERSONATE_CHAIN:
        try:
            response = requests.get(url, headers=headers, timeout=20, impersonate=imp)
            attempts.append({"impersonation": imp, "status": response.status_code, "body_bytes": len(response.content)})
            if response.status_code == 200 and response.text and '"Workflow":null' not in response.text[:160]:
                body_text = response.text
                winning_imp = imp
                break
        except Exception as exc:
            attempts.append({"impersonation": imp, "error": f"{type(exc).__name__}: {exc}"})
    body = json.loads(body_text) if body_text else {}
    units = parse_onesite_workflowstartup(body, url) if body else []
    native = [u for u in units if str(u.get("unit_number") or "").strip()]
    if body_text:
        RAW.write_text(body_text)
    result = {
        "property_id": 6274,
        "property_name": "The Village at Voorhees",
        "marketing_page": PAGE,
        "site_id": SITE_ID,
        "request_url": url,
        "attempts": attempts,
        "winning_impersonation": winning_imp,
        "property_boundary": {
            "page_config_realpageId": SITE_ID,
            "workflow_site_id": (body.get("Workflow") or {}).get("SiteId"),
            "workflow_pmc_id": (body.get("Workflow") or {}).get("PmcId"),
        },
        "parsed_rows": units,
        "native_priced_rows": native,
        "native_priced_count": len(native),
        "raw_body_path": str(RAW) if body_text else "",
    }
    EVIDENCE.write_text(json.dumps(result, indent=2))
    print(EVIDENCE)
    print(json.dumps({"attempts": attempts, "workflow_site_id": result["property_boundary"]["workflow_site_id"], "parsed": len(units), "native": len(native)}, indent=2))
    for unit in native:
        print(unit.get("unit_number"), unit.get("floor_plan_name"), unit.get("market_rent_low"), unit.get("market_rent_high"))


if __name__ == "__main__":
    main()
