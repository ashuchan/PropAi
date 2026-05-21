"""Deep-probe a HAR file: find the response that actually contains unit data
by body-content scoring, not URL pattern matching.

For each HAR:
  • Parse all response entries.
  • Score each non-trivial body (>1KB) by unit-data signals: $-rent
    in the rent band, bed/bath/sqft tokens, JSON unit keys, JSON-LD
    Schema.org Apartment types, PMS-vendor markers.
  • Pick the smoking-gun response (highest score).
  • Emit a single result line per HAR + a markdown per-HAR note.

Usage:
  python3 scripts/deep_probe.py <har_path> --out per-har/<stem>.md \\
    --jsonl worklist.jsonl --stem <stem>

Designed to be resumable: re-running on an already-probed HAR is a no-op
unless --force is set.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any

# ── Scoring patterns ────────────────────────────────────────────────────────

RENT_RE = re.compile(r"\$\s*([1-9]\d{2,4})(?:[\.,]\d{2})?")
BED_RE = re.compile(r"\b(?:studio|\d{1,2}\s*(?:bed|br|bedroom)s?)\b", re.IGNORECASE)
BATH_RE = re.compile(r"\b\d(?:\.\d)?\s*(?:bath|ba|bathroom)s?\b", re.IGNORECASE)
SQFT_RE = re.compile(r"\b\d{2,4}\s*sq\.?\s*ft\.?\b|\b\d{2,4}\s*square\s*feet\b", re.IGNORECASE)

# Strong JSON-key signals — appearance in JSON body is near-certain unit data
JSON_UNIT_KEYS_RE = re.compile(
    r'"(?:floorPlans?|floor_plans?|units?|availableUnits?|availability|'
    r'pricing|rentRange|marketRent|monthlyRent|unitNumber|unit_number|'
    r'unitType|UnitType|FloorPlans|FloorPlanList|floorplansList|'
    r'numberOfBedrooms|numberOfBathrooms|squareFootage|sqFt|sqft|'
    r'Beds|Baths|MinRent|MaxRent|MinSqFt|MaxSqFt)"\s*:',
    re.IGNORECASE,
)

JSONLD_APT_RE = re.compile(
    r'"@type"\s*:\s*"(?:Apartment|ApartmentUnit|ApartmentComplex|Offer|'
    r'FloorPlan|Residence|SingleFamilyResidence|Accommodation|House|Suite)"',
    re.IGNORECASE,
)

# PMS-vendor markers — substring presence in body or URL
PMS_MARKERS: dict[str, tuple[str, ...]] = {
    "rentcafe": ("rentcafe", "rcDataPropertyId", "rc-floorplans"),
    "yardi": ("ysi.floorplansList", "Yardi", "yardione", "securecafe"),
    "realpage_oll": ("/oll/", "realpage", "RPFP_config", "leasing.realpage.com"),
    "realpage_cws": ("CmsSiteManager", "Proxy/GetUnits"),
    "entrata": ("entrata", "/conventional/", "siteid"),
    "knock": ("knock", "knockportal", "doorway"),
    "appfolio": ("appfolio", "/listings/"),
    "sightmap": ("sightmap", "/sightmap/"),
    "funnel": ("funnelleasing", "funnel-portal"),
    "spherexx": ("spherexx", "spherexxoptin"),
    "engrain": ("engrain", "stack-svg"),
    "wix": ("wixstatic", "parastorage", "_api/wix"),
    "wordpress": ("wp-content", "wp-json"),
    "g5": ("g5plus", "g5-cl-"),
    "razz": ("razz", "razz-cms"),
    "mri": ("prospectconnect", "mriapartmentsearch"),
    "goprisma": ("goprisma", "prismaonline"),
    "fortresstech": ("fortresstech", "fortressrentals"),
}

CF_BLOCK_RE = re.compile(r"just a moment|cloudflare|attention required", re.IGNORECASE)
DATADOME_BLOCK_RE = re.compile(r"datadome|geo\.captcha", re.IGNORECASE)


def _decompress(body: str, encoding: str | None) -> str:
    if not body:
        return ""
    if encoding == "base64":
        import base64
        try:
            raw = base64.b64decode(body)
            try:
                return raw.decode("utf-8", errors="ignore")
            except Exception:
                # Try gzip
                try:
                    return gzip.decompress(raw).decode("utf-8", errors="ignore")
                except Exception:
                    # Full-body fallback decode — no cap.
                    return raw.decode("latin-1", errors="ignore")
        except Exception:
            return ""
    return body


def _score_body(body: str, url: str) -> dict[str, Any]:
    if not body:
        return {"score": 0}
    body_lc = body.lower()
    url_lc = url.lower()
    rents = RENT_RE.findall(body)
    # Filter to rent band 200-50000
    valid_rents = [int(r) for r in rents if 200 <= int(r) <= 50_000]
    beds = len(BED_RE.findall(body))
    baths = len(BATH_RE.findall(body))
    sqfts = len(SQFT_RE.findall(body))
    json_keys = len(JSON_UNIT_KEYS_RE.findall(body))
    jsonld = len(JSONLD_APT_RE.findall(body))
    pms_hits: list[str] = []
    for vendor, markers in PMS_MARKERS.items():
        for m in markers:
            if m.lower() in body_lc or m.lower() in url_lc:
                pms_hits.append(vendor)
                break
    blocked_cf = bool(CF_BLOCK_RE.search(body))
    blocked_dd = bool(DATADOME_BLOCK_RE.search(body))

    # Composite score: need rent+bed+sqft co-occurrence OR strong JSON-LD/JSON keys
    co_signal = min(len(valid_rents), beds, sqfts)
    score = co_signal * 3 + json_keys * 2 + jsonld * 5
    # Penalty for blocked responses
    if blocked_cf or blocked_dd:
        score = max(0, score // 4)

    return {
        "score": score,
        "n_rent": len(valid_rents),
        "n_bed": beds,
        "n_bath": baths,
        "n_sqft": sqfts,
        "n_json_keys": json_keys,
        "n_jsonld": jsonld,
        "co_signal": co_signal,
        "pms": sorted(set(pms_hits)),
        "blocked_cf": blocked_cf,
        "blocked_dd": blocked_dd,
    }


def probe_har(har_path: Path) -> dict[str, Any]:
    """Open HAR, score every response, pick the smoking-gun."""
    try:
        with har_path.open(encoding="utf-8") as fh:
            har = json.load(fh)
    except Exception as exc:
        return {"error": f"parse-error: {type(exc).__name__}: {exc}"}

    entries = har.get("log", {}).get("entries", [])
    candidates: list[dict[str, Any]] = []
    pms_overall: set[str] = set()
    blocked_count = 0
    total_responses = 0

    for ent in entries:
        req = ent.get("request") or {}
        resp = ent.get("response") or {}
        url = req.get("url") or ""
        method = req.get("method") or ""
        status = resp.get("status") or 0
        content = resp.get("content") or {}
        mime = (content.get("mimeType") or "").lower()
        size = content.get("size") or 0
        body = content.get("text") or ""
        encoding = content.get("encoding")
        total_responses += 1

        # Skip trivial / image / font responses
        if size < 500:
            continue
        if any(skip in mime for skip in ("image/", "font/", "video/", "audio/", "css")):
            continue
        # Skip 3xx / 0
        if status >= 300 and status < 400:
            continue

        decoded = _decompress(body, encoding)
        if not decoded:
            continue
        # No body-size cap — user explicitly requested full-body scoring
        # (2026-05-21). Regex over 10+ MB bodies is slow but every byte
        # gets a chance to surface signal.
        score_info = _score_body(decoded, url)
        if score_info.get("blocked_cf") or score_info.get("blocked_dd"):
            blocked_count += 1
        pms_overall.update(score_info.get("pms", []))
        if score_info["score"] <= 0:
            continue
        candidates.append({
            "url": url,
            "method": method,
            "status": status,
            "mime": mime,
            "body_len": len(decoded),
            **score_info,
        })

    candidates.sort(key=lambda c: -c["score"])
    return {
        "total_responses": total_responses,
        "candidates_with_signal": len(candidates),
        "blocked_responses": blocked_count,
        "pms_overall": sorted(pms_overall),
        "top": candidates[:5],
    }


def classify(top: list[dict[str, Any]], pms_overall: list[str]) -> dict[str, str]:
    """Translate a probe result into a verdict bucket + adapter routing
    suggestion. Buckets:
      - tier1_api_exists: clear API response with unit JSON
      - jsonld_only: JSON-LD has Apartment/Offer
      - embedded_json_ssr: large HTML with JSON unit keys inline
      - html_only_dom: HTML page with rent+bed+sqft visible text
      - blocked_only: every response is CF/DataDome-blocked
      - no_unit_signal: no candidate has unit signals — needs deeper render
    """
    if not top:
        return {"verdict": "no_unit_signal", "adapter_hint": ""}
    top1 = top[0]
    mime = top1.get("mime", "")
    url = top1.get("url", "")

    # API tier-1
    if "json" in mime and top1["n_json_keys"] >= 3:
        # Vendor-specific adapter hints
        for v in top1.get("pms", []):
            if v in ("rentcafe", "yardi", "realpage_oll", "realpage_cws",
                     "entrata", "knock", "appfolio", "sightmap", "funnel",
                     "spherexx", "mri", "goprisma", "fortresstech"):
                return {"verdict": "tier1_api_exists", "adapter_hint": v}
        return {"verdict": "tier1_api_exists", "adapter_hint": "generic_api"}

    if top1["n_jsonld"] >= 1:
        return {"verdict": "jsonld_only", "adapter_hint": "jsonld_extractor"}

    if "html" in mime and top1["n_json_keys"] >= 2:
        return {"verdict": "embedded_json_ssr", "adapter_hint": "extract_embedded_blobs"}

    if "html" in mime and top1["co_signal"] >= 3:
        return {"verdict": "html_only_dom", "adapter_hint": "container_discovery"}

    if all(c.get("blocked_cf") or c.get("blocked_dd") for c in top):
        return {"verdict": "blocked_only", "adapter_hint": "needs_proxy_or_browser"}

    return {"verdict": "weak_signal", "adapter_hint": "needs_chrome_probe"}


def render_md(stem: str, probe: dict[str, Any], verdict: dict[str, str]) -> str:
    lines = [
        f"# {stem}",
        "",
        f"**Verdict:** `{verdict['verdict']}`  ",
        f"**Adapter hint:** `{verdict['adapter_hint']}`  ",
        f"**Total responses:** {probe.get('total_responses', 0)}  ",
        f"**Candidates with unit-signal:** {probe.get('candidates_with_signal', 0)}  ",
        f"**Blocked responses:** {probe.get('blocked_responses', 0)}  ",
        f"**PMS markers detected:** {', '.join(probe.get('pms_overall', [])) or '(none)'}  ",
        "",
        "## Top scoring responses",
        "",
    ]
    if probe.get("error"):
        lines.append(f"**Error:** {probe['error']}")
        return "\n".join(lines)
    for i, c in enumerate(probe.get("top", []), 1):
        lines.append(f"### {i}. score={c['score']} status={c['status']} mime=`{c['mime']}` len={c['body_len']:,}")
        lines.append(f"  URL: `{c['url'][:140]}`")
        lines.append(
            f"  Signals: rent×{c['n_rent']} bed×{c['n_bed']} bath×{c['n_bath']} "
            f"sqft×{c['n_sqft']} json_keys×{c['n_json_keys']} jsonld×{c['n_jsonld']} "
            f"co_signal={c['co_signal']}"
        )
        if c.get("pms"):
            lines.append(f"  PMS: {', '.join(c['pms'])}")
        if c.get("blocked_cf") or c.get("blocked_dd"):
            lines.append(f"  ⚠ BLOCKED (cf={c.get('blocked_cf')} dd={c.get('blocked_dd')})")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("har_path", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--jsonl", type=Path, required=True)
    ap.add_argument("--stem", required=True)
    ap.add_argument("--bucket", default="", help="Failure-bucket label")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.out.exists() and not args.force:
        # Resumable: skip already-probed
        print(f"SKIP {args.stem} (already probed)")
        return 0

    probe = probe_har(args.har_path)
    verdict = classify(probe.get("top", []), probe.get("pms_overall", []))
    md = render_md(args.stem, probe, verdict)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md)

    # Append durable jsonl record
    args.jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.jsonl.open("a") as jf:
        rec = {
            "stem": args.stem,
            "bucket": args.bucket,
            "verdict": verdict["verdict"],
            "adapter_hint": verdict["adapter_hint"],
            "n_candidates": probe.get("candidates_with_signal", 0),
            "n_responses": probe.get("total_responses", 0),
            "blocked": probe.get("blocked_responses", 0),
            "pms_overall": probe.get("pms_overall", []),
            "top1_url": (probe.get("top") or [{}])[0].get("url", "")[:200],
            "top1_score": (probe.get("top") or [{}])[0].get("score", 0),
        }
        jf.write(json.dumps(rec) + "\n")

    print(f"DONE {args.stem} verdict={verdict['verdict']} hint={verdict['adapter_hint']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
