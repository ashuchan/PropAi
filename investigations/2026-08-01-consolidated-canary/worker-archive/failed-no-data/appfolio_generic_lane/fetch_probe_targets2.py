#!/usr/bin/env python3
"""Fetch second-round exact-property unit roster targets."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


OUT = Path("/private/tmp/propai-fnd-vBkmT9/appfolio_generic_lane/probes2")
OUT.mkdir(parents=True, exist_ok=True)

TARGETS: list[tuple[str, str, str]] = [
    (
        "60145",
        "securecafe_969192",
        "https://woodmontmewsapartments.securecafe.com/onlineleasing/woodmont-mews/availableunits.aspx?myOlePropertyId=144461&floorPlans=969192",
    ),
    (
        "60145",
        "securecafe_969190",
        "https://woodmontmewsapartments.securecafe.com/onlineleasing/woodmont-mews/availableunits.aspx?myOlePropertyId=144461&floorPlans=969190",
    ),
    (
        "60145",
        "securecafe_969191",
        "https://woodmontmewsapartments.securecafe.com/onlineleasing/woodmont-mews/availableunits.aspx?myOlePropertyId=144461&floorPlans=969191",
    ),
    (
        "60145",
        "securecafe_969194",
        "https://woodmontmewsapartments.securecafe.com/onlineleasing/woodmont-mews/availableunits.aspx?myOlePropertyId=144461&floorPlans=969194",
    ),
    ("55709", "exact_floorplans", "https://livevistapointeapts.com/en/floor-plans/"),
    (
        "55709",
        "legacy_floorplans",
        "https://ridgepointeblueridge.bettercmspro.com/en/floor-plans/",
    ),
    (
        "299847",
        "alert_11649",
        "https://www.mirabellamcallen.com/availability-alert?id=11649",
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
