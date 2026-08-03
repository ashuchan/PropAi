from __future__ import annotations

import csv
import gzip
import hashlib
import html
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "appfolio_generic_lane" / "live"
OUT.mkdir(parents=True, exist_ok=True)

KEYWORDS = re.compile(
    r"(?:availab|vacanc|floor[-_ ]?plans?|units?|pricing|listings)", re.I
)
ATTR_URL = re.compile(
    r'''(?:href|src|data-src|data-url|action)\s*=\s*["']([^"']+)["']''', re.I
)


def eligible_same_property_link(base: str, value: str) -> bool:
    if not value or value.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    absolute = urljoin(base, html.unescape(value))
    parsed = urlparse(absolute)
    base_parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.hostname == base_parsed.hostname:
        return bool(KEYWORDS.search(parsed.path + "?" + parsed.query))
    if parsed.hostname and parsed.hostname.endswith(".appfolio.com"):
        return "/listings" in parsed.path and (
            "property_list" in parsed.query or "listable_uid" in parsed.query
        )
    if parsed.hostname and parsed.hostname.endswith(".spherexx.com"):
        return "availability" in parsed.path.lower()
    if parsed.hostname and parsed.hostname.endswith(".betternoi.com"):
        return "/api/" in parsed.path.lower()
    return False


def candidates() -> list[dict[str, str]]:
    with (ROOT / "strict_recovery_remaining_current.csv").open(
        newline="", encoding="utf-8-sig"
    ) as handle:
        remaining = list(csv.DictReader(handle))
    targets = [
        row
        for row in remaining
        if row.get("current_detected_adapter") in {"appfolio", "generic_plan_text"}
    ]
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in targets:
        pid = row["property_id"]
        base = row["website"]
        urls = [base]
        profile_path = ROOT / "profiles" / f"{pid}.json"
        if profile_path.exists():
            profile = json.loads(profile_path.read_text())
            navigation = profile.get("navigation") or {}
            for key in ("entry_url", "winning_page_url"):
                if navigation.get(key):
                    urls.append(str(navigation[key]))
            if navigation.get("availability_page_path"):
                urls.append(urljoin(base, str(navigation["availability_page_path"])))
            urls.extend(str(value) for value in navigation.get("explored_links") or [])
        raw_path = ROOT / "raw_all" / f"{pid}.html.gz"
        if raw_path.exists():
            archived = gzip.open(raw_path, "rt", errors="replace").read()
            urls.extend(
                urljoin(base, html.unescape(value))
                for value in ATTR_URL.findall(archived)
                if eligible_same_property_link(base, value)
            )
        for url in urls:
            url = html.unescape(url).replace(" ", "%20")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            marker = (pid, url)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(
                {
                    "property_id": pid,
                    "adapter": row["current_detected_adapter"],
                    "property_name": row["property_name"],
                    "url": url,
                }
            )
    return result


def fetch(item: dict[str, str]) -> dict[str, object]:
    digest = hashlib.sha256(item["url"].encode()).hexdigest()[:12]
    target = OUT / f"{item['property_id']}_{digest}.html"
    proc = subprocess.run(
        [
            "curl",
            "-L",
            "--compressed",
            "--max-time",
            "35",
            "--connect-timeout",
            "12",
            "--retry",
            "1",
            "-A",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "-sS",
            "-o",
            str(target),
            "-w",
            "%{http_code}\t%{url_effective}\t%{content_type}\t%{size_download}",
            item["url"],
        ],
        text=True,
        capture_output=True,
        timeout=80,
    )
    fields = proc.stdout.strip().split("\t")
    return {
        **item,
        "returncode": proc.returncode,
        "status": fields[0] if len(fields) > 0 else "",
        "final_url": fields[1] if len(fields) > 1 else "",
        "content_type": fields[2] if len(fields) > 2 else "",
        "size": int(float(fields[3])) if len(fields) > 3 and fields[3] else 0,
        "file": str(target),
        "stderr": proc.stderr[-500:],
    }


items = candidates()
results: list[dict[str, object]] = []
with ThreadPoolExecutor(max_workers=6) as pool:
    futures = {pool.submit(fetch, item): item for item in items}
    for future in as_completed(futures):
        try:
            results.append(future.result())
        except Exception as exc:  # keep a complete per-URL audit trail
            results.append({**futures[future], "error": repr(exc)})

results.sort(key=lambda row: (int(row["property_id"]), str(row["url"])))
(OUT.parent / "live_fetch_manifest.json").write_text(
    json.dumps(
        {
            "batch": "appfolio_generic_remaining_30_live_direct",
            "request_count": len(results),
            "results": results,
        },
        indent=2,
    )
)
print(json.dumps({"requests": len(results), "outcomes": results}, indent=2))
