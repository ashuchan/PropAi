#!/usr/bin/env python3
"""Fetch the property-scoped public Union chat bootstrap for Banyan."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


out = Path(
    "/private/tmp/propai-fnd-vBkmT9/appfolio_generic_lane/probes/"
    "258661_union_chat_settings.json"
)
headers = out.with_suffix(".headers")
url = "https://my.gounion.com/api/v1/chat_settings/?uuid=null"
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
    "-H",
    "Accept: application/json, text/plain, */*",
    "-H",
    "Content-Type: application/json",
    "-H",
    "X-Name: banyan-on-washington",
    "-H",
    "Client-ID: 3cec5220c2f7d0cc059e5cfb13808cf4",
    "-D",
    str(headers),
    "-o",
    str(out),
    "-w",
    "%{http_code}\n%{url_effective}\n%{content_type}\n%{size_download}\n",
    url,
]
completed = subprocess.run(command, capture_output=True, text=True, check=False)
result = {
    "url": url,
    "returncode": completed.returncode,
    "curl_metadata": completed.stdout.splitlines(),
    "stderr": completed.stderr.strip(),
    "body": str(out),
    "headers": str(headers),
}
print(json.dumps(result, indent=2))
