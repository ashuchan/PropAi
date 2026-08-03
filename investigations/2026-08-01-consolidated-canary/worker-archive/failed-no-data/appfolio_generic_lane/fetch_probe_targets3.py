#!/usr/bin/env python3
"""Fetch public, exact-property API targets discovered in round two."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


OUT = Path("/private/tmp/propai-fnd-vBkmT9/appfolio_generic_lane/probes3")
OUT.mkdir(parents=True, exist_ok=True)

TARGETS: list[tuple[str, str, str]] = [
    (
        "60145",
        "applicant_theme",
        "https://woodmontmewsapartments.securecafeapplicant.com/onlineleasing/api/themeloader/getcustomcolorsfilename?siteName=woodmont-mews",
    ),
    (
        "60145",
        "applicant_units",
        "https://woodmontmewsapartments.securecafeapplicant.com/onlineleasing/api/floorplan/getfloorplanandavailableunits?propertyId=144461&RequestBeforeLogin=true&isPropertyList=false",
    ),
    (
        "55709",
        "betternoi_all",
        "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_uuid=54523ff9-0329-43dd-83b4-3066820f136e&is_available=true",
    ),
    (
        "55709",
        "betternoi_1br",
        "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_uuid=54523ff9-0329-43dd-83b4-3066820f136e&floorplan_uuid=ca472563-60b0-4cfd-929b-d412135fbdd9&is_available=true",
    ),
    (
        "55709",
        "betternoi_2br",
        "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_uuid=54523ff9-0329-43dd-83b4-3066820f136e&floorplan_uuid=c64e3195-a349-41d7-8c5f-fc94fac81abf&is_available=true",
    ),
    (
        "55709",
        "betternoi_2br_b",
        "https://ares.betternoi.com/api/pub/v1/client/building/unit?client_uuid=54523ff9-0329-43dd-83b4-3066820f136e&floorplan_uuid=e707a040-eed0-4358-a2b5-b0e78e32345f&is_available=true",
    ),
]


def fetch(pid: str, label: str, url: str) -> dict[str, object]:
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    body = OUT / f"{pid}_{label}_{digest}.body"
    headers = OUT / f"{pid}_{label}_{digest}.headers"
    completed = subprocess.run(
        [
            "curl",
            "--globoff",
            "-L",
            "-sS",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            "60",
            "-A",
            "Mozilla/5.0 (compatible; PropAiEvidenceAudit/1.0)",
            "-D",
            str(headers),
            "-o",
            str(body),
            "-w",
            "%{http_code}\n%{url_effective}\n%{content_type}\n%{size_download}\n",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
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
