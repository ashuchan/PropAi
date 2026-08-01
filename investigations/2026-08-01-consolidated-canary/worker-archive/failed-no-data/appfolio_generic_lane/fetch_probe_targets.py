#!/usr/bin/env python3
"""Fetch a small, explicit set of exact-property discovery targets.

This is evidence collection only: no CAPTCHA solving, no canary, and no
portfolio-wide endpoint enumeration.  Responses are written to the lane's
temporary evidence directory for incremental inspection.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


OUT = Path("/private/tmp/propai-fnd-vBkmT9/appfolio_generic_lane/probes")
OUT.mkdir(parents=True, exist_ok=True)

TARGETS: list[tuple[str, str, str]] = [
    (
        "15014",
        "wildwood_prospectportal",
        "https://rentwwp.prospectportal.com/Apartments/module/application_authentication/property[id]/211184/show_in_popup/false/kill_session/1/",
    ),
    (
        "258661",
        "union_dwell_js",
        "https://my.gounion.com/api/v1/load_dwelljs/dwell.js?client_id=3cec5220c2f7d0cc059e5cfb13808cf4",
    ),
    (
        "258661",
        "site_app_client_js",
        "https://banyanonwashington.com/static/js/app.client.js?v1.0.0",
    ),
    ("60145", "apply", "https://www.woodmontmewsapartments.com/pages/apply.asp"),
    ("60145", "one_bed", "https://www.woodmontmewsapartments.com/pages/1-bed.asp"),
    ("60145", "one_bed_den", "https://www.woodmontmewsapartments.com/pages/1-bed-den.asp"),
    ("60145", "two_bed", "https://www.woodmontmewsapartments.com/pages/2-bed.asp"),
    (
        "299847",
        "fp_11649",
        "https://www.mirabellamcallen.com/floorplans-and-pricing/1-bed/11649",
    ),
    (
        "299847",
        "fp_11650",
        "https://www.mirabellamcallen.com/floorplans-and-pricing/1-bed/11650",
    ),
    (
        "299847",
        "fp_11651",
        "https://www.mirabellamcallen.com/floorplans-and-pricing/1-bed/11651",
    ),
    (
        "299847",
        "fp_11930",
        "https://www.mirabellamcallen.com/floorplans-and-pricing/2-beds/11930",
    ),
    (
        "299847",
        "fp_11652",
        "https://www.mirabellamcallen.com/floorplans-and-pricing/2-beds/11652",
    ),
    ("52182", "home", "https://woodsideapartments.net/"),
    ("52182", "schedule", "https://woodsideapartments.net/schedule-tour/"),
    ("251514", "old_lc_sobro", "https://lifestylecommunities.com/community/lc-sobro"),
    ("251514", "nashville", "https://lifestylecommunities.com/communities/nashville/"),
    ("251514", "wp_search", "https://lifestylecommunities.com/wp-json/wp/v2/search?search=LC%20SoBro&per_page=100"),
    ("55709", "configured", "https://ridgepointeblueridgeapts.com/en/"),
    ("55709", "configured_http", "http://ridgepointeblueridgeapts.com/en/"),
]


def fetch(pid: str, label: str, url: str) -> dict[str, object]:
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    body = OUT / f"{pid}_{label}_{digest}.body"
    headers = OUT / f"{pid}_{label}_{digest}.headers"
    command = [
        "curl",
        "--globoff",
        "-L",
        "-sS",
        "--compressed",
        "--connect-timeout",
        "15",
        "--max-time",
        "45",
        "-A",
        "Mozilla/5.0 (compatible; PropAiEvidenceAudit/1.0)",
        "-D",
        str(headers),
        "-o",
        str(body),
        "-w",
        "%{http_code}\n%{url_effective}\n%{content_type}\n%{size_download}\n",
        url,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    lines = completed.stdout.splitlines()
    return {
        "property_id": pid,
        "label": label,
        "url": url,
        "returncode": completed.returncode,
        "status": lines[0] if lines else "",
        "final_url": lines[1] if len(lines) > 1 else "",
        "content_type": lines[2] if len(lines) > 2 else "",
        "size": int(lines[3]) if len(lines) > 3 and lines[3].isdigit() else 0,
        "body": str(body),
        "headers": str(headers),
        "stderr": completed.stderr.strip(),
    }


results = [fetch(*target) for target in TARGETS]
manifest = OUT / "probe_manifest.json"
manifest.write_text(json.dumps({"results": results}, indent=2) + "\n")
print(json.dumps({"manifest": str(manifest), "results": results}, indent=2))
