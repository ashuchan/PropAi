"""Deep probe runner — 10-step contract per property, curl_cffi-only steps.

Steps performed here (synchronous, fast, all via curl_cffi chrome120):
  1. Landing fetch — status, size, HTML snapshot
  2. /floorplans
  3. /floor-plans (hyphen variant)
  4. /availability
  5. Landing HTML fingerprint scan (PMS markers, powered-by credits)
  7. JS bundle grep for fetch/axios URL constants
  9. Conventional host patterns (/api, /wp-json, /admin-ajax.php, /graphql)

Steps reserved for Chrome MCP (NOT run here, residue only):
  6. Network panel XHR capture during page load
  8. Click drill anchors ("N Available", "View Details")

Output: artifacts/probe/{cohort}_results.jsonl — append-only, resumable.
       Re-run skips pids already in the result file (by pid + timestamp).

USAGE: ./ma_poc/.venv/bin/python investigations/2026-05-25-unit-debug/probe_runner.py [cohort]
       cohort = "n_full_zero" (default) | "low_strict" | "both"
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

# curl_cffi for Chrome120 TLS fingerprint
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    print("ERROR: curl_cffi not installed in this venv. Falling back to httpx.")
    curl_requests = None
    import httpx

PROBE_DIR = Path(__file__).parent / "artifacts" / "probe"
TIMEOUT = 15.0

# Known PMS / platform markers (signature → tag)
PMS_MARKERS: dict[str, list[str]] = {
    "rentcafe":          ["rentcafe.com", "_rcweb_", "rcWebsite"],
    "securecafe":        ["securecafe.com"],
    "entrata":           ["entrata.com", "prospectportal", "entrata-cdn"],
    "appfolio":          ["appfolio.com", "Appfolio.Listing"],
    "knock":             ["knock-cdn", "knockrentals", "knock.app", "knck.io", "doorway.knck"],
    "onesite":           ["onlineleasing.realpage.com", "onesite"],
    "realpage_cws":      ["cws.realpage.com", "realpage-cws", "cs-cdn.realpage.com"],
    "g5":                ["g5dxm.com", "g5marketingcloud", "g5-c-"],
    "rentmanager":       ["rentmanager.com", "myresman.com"],
    "rentvision":        ["rentvision", "powered by rentvision"],
    "sightmap":          ["sightmap", "engrain.com"],
    "engrain":           ["engrain.com"],
    "repli360":          ["repli360"],
    "resman":            ["myresman.com", "aptdyn"],
    "rentaladdress":     ["rentaladdress.com", "floor_plan_list"],
    "marketapts":        ["marketapts"],
    "365res":            ["365residentservices"],
    "spherexx":          ["spherexx"],
    "avalonbay":         ["avalonbay"],
    "essex":             ["essex"],
    "amli":              ["amli.com"],
    "wix":                ["wix.com", "_wix_", "static.wixstatic"],
    "squarespace":       ["squarespace.com", "static1.squarespace"],
    "wordpress":         ["wp-content", "wp-includes"],
    "jetengine":         ["jetengine"],
    "sucuri":            ["sucuri.net", "sgcaptcha", "Access denied"],
    "datadome":          ["datadome", "_dd_"],
    "cloudflare":        ["cf-ray:", "cloudflare", "__cf_chl"],
    "godaddy_builder":   ["img1.wsimg.com", "Go Daddy Website Builder", "Starfield Technologies"],
}

# JS bundle URL-constant patterns
JS_URL_PATTERNS = [
    re.compile(r"https?://api\.[\w.-]+/[\w/-]*", re.IGNORECASE),
    re.compile(r"https?://[\w.-]+/api/[\w/-]+", re.IGNORECASE),
    re.compile(r"['\"](/api/[\w/-]+)['\"]"),
    re.compile(r"['\"](/wp-json/[\w/-]+)['\"]"),
    re.compile(r"['\"](/graphql)['\"]", re.IGNORECASE),
    re.compile(r"axios\.[a-z]+\(['\"]([^'\"]+)['\"]"),
    re.compile(r"fetch\(['\"]([^'\"]+)['\"]"),
]


def _fetch(url: str, *, timeout: float = TIMEOUT) -> tuple[int, str, dict]:
    """Return (status, body_text, headers_dict). Empty on failure."""
    try:
        if curl_requests is not None:
            r = curl_requests.get(url, impersonate="chrome120", timeout=timeout, allow_redirects=True)
            return r.status_code, r.text or "", dict(r.headers)
        else:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 Chrome/124"}) as c:
                r = c.get(url)
                return r.status_code, r.text or "", dict(r.headers)
    except Exception as exc:
        return 0, f"__FETCH_ERROR__: {exc!s}", {}


def _signatures(html: str) -> list[str]:
    """Return list of matched platform tags."""
    hits = []
    low = html.lower() if html else ""
    for tag, markers in PMS_MARKERS.items():
        if any(m.lower() in low for m in markers):
            hits.append(tag)
    return hits


def _js_url_constants(html: str) -> list[str]:
    """Extract fetch/axios URL strings from inline + linked JS."""
    found: set[str] = set()
    if not html:
        return []
    for pat in JS_URL_PATTERNS:
        for m in pat.finditer(html):
            u = m.group(1) if pat.groups else m.group(0)
            if u and len(u) < 200:
                found.add(u)
    return sorted(found)[:30]


def _floorplan_link_candidates(html: str) -> list[str]:
    """Extract floor-plan-ish hrefs from landing HTML."""
    if not html:
        return []
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    keys = ("floorplan", "floor-plan", "availab", "apartments", "residences", "/units", "/floor")
    return [h for h in hrefs if any(k in h.lower() for k in keys)][:20]


def probe_property(prop: dict) -> dict:
    """Run the 10-step contract (sans Chrome MCP steps 6+8) on one prop."""
    pid = prop.get("apartment_id")
    base = prop.get("website") or ""
    if not base:
        return {"pid": pid, "error": "no_website", "ts": datetime.now(timezone.utc).isoformat()}
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    out: dict = {
        "pid": pid,
        "name": prop.get("name"),
        "url": base,
        "tier_observed": prop.get("extraction_tier"),
        "cohort": prop.get("_probe_cohort"),
        "tier_cohort": prop.get("_probe_tier"),
        "pool_size": prop.get("_pool_size"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    # Step 1: landing fetch
    s1_status, s1_body, s1_hdr = _fetch(base)
    out["steps"]["1_landing"] = {
        "url": base,
        "status": s1_status,
        "size": len(s1_body),
        "ctype": s1_hdr.get("content-type", ""),
        "redirect": s1_hdr.get("location", ""),
    }
    landing_html = s1_body if s1_status == 200 and not s1_body.startswith("__FETCH_ERROR__") else ""

    # Step 5: fingerprint scan (do early so we know which paths to try)
    fps = _signatures(landing_html)
    # SiteGround sgcaptcha fast-path: 202 + ≤500-byte body + meta-refresh = anti-bot wall.
    # Run against the raw s1_body (not landing_html) because landing_html is blanked on non-200.
    if (
        s1_status == 202
        and len(s1_body) < 500
        and 'meta http-equiv="refresh"' in s1_body.lower()
    ):
        fps.append("sgcaptcha_202")
    out["steps"]["5_fingerprints"] = fps

    # Steps 2/3/4: alt URL probes. Each "step" sweeps the bare slug plus extension and
    # underscore variants; the FIRST 200 response wins so downstream verdict logic is unchanged.
    # Variants caught real-world misses on 2026-05-25: /floor-plans.aspx (66homer.com — Knock+RealPage),
    # /availability.html (1515parkplace.com — EXR), /floor_plans (cedarridgeapts — RentalAddress).
    SLUG_VARIANTS: dict[str, list[str]] = {
        "floorplans":   ["floorplans", "floorplans.aspx", "floorplans.html", "floorplans.php"],
        "floor-plans":  ["floor-plans", "floor-plans.aspx", "floor-plans.html", "floor-plans.php", "floor_plans"],
        "availability": ["availability", "availability.aspx", "availability.html", "availability.php"],
    }
    for base_slug, label in [("floorplans", "2_floorplans"), ("floor-plans", "3_floor-plans"), ("availability", "4_availability")]:
        chosen_url = ""
        chosen_status = 0
        chosen_body = ""
        tried: list[dict] = []
        for variant in SLUG_VARIANTS[base_slug]:
            u = urljoin(origin + "/", variant)
            st, body, _ = _fetch(u)
            tried.append({"url": u, "status": st, "size": len(body)})
            # First 200 wins; otherwise keep the best-status response so we still log something
            if st == 200 and not body.startswith("__FETCH_ERROR__"):
                chosen_url, chosen_status, chosen_body = u, st, body
                break
            if chosen_status == 0:
                chosen_url, chosen_status, chosen_body = u, st, body
        # Mini-signal: how many floorplan/unit links does it carry?
        links = _floorplan_link_candidates(chosen_body) if chosen_status == 200 else []
        # Mini-signal: presence of any unit-row indicators
        has_units = bool(chosen_body and any(k in chosen_body.lower() for k in ("unit_number", "unit-number", "data-unit", "apt-price", "apt-value", "rr-unit", "term-pricing-popup", "available_now", "monthly_rate", '"rent"', '"price"'))) if chosen_body else False
        out["steps"][label] = {
            "url": chosen_url,
            "status": chosen_status,
            "size": len(chosen_body),
            "links_floorplans": len(links),
            "has_unit_markers": has_units,
            "variants_tried": tried,
        }

    # Step 7: JS URL constants from landing
    out["steps"]["7_js_urls"] = _js_url_constants(landing_html)

    # Step 9: conventional host patterns
    s9 = {}
    for probe_path in ["/api", "/api/", "/wp-json/wp/v2/pages", "/admin-ajax.php", "/graphql"]:
        u = urljoin(origin + "/", probe_path)
        st, body, _ = _fetch(u)
        # We care: does this NOT 404? Does it return JSON?
        ct = ""
        for _, body_, hdr_ in [(None, body, _fetch.__wrapped__ if hasattr(_fetch, "__wrapped__") else None)]:
            pass
        # Simpler: re-fetch to grab headers
        st, body, hdr = _fetch(u)
        is_json = "application/json" in hdr.get("content-type", "").lower()
        s9[probe_path] = {"status": st, "is_json": is_json, "size": len(body)}
    out["steps"]["9_conventional"] = s9

    # Also try api.{host} subdomain
    api_subdomain = f"{parsed.scheme}://api.{parsed.netloc}"
    st, _, hdr = _fetch(api_subdomain + "/", timeout=5.0)
    out["steps"]["9_conventional"]["api_subdomain"] = {"status": st, "ctype": hdr.get("content-type", "")}

    # Verdict heuristic (cheap, will refine in clustering pass)
    verdict = _verdict(out)
    out["verdict"] = verdict
    return out


def _verdict(out: dict) -> str:
    """Rough cluster hypothesis from step results."""
    s = out["steps"]
    if s["1_landing"]["status"] == 0:
        return "FETCH_ERROR"
    if s["1_landing"]["status"] in (403, 401, 429, 503):
        return f"BLOCKED_HTTP_{s['1_landing']['status']}"
    fps = s.get("5_fingerprints", [])
    if "sucuri" in fps or "datadome" in fps or "sgcaptcha_202" in fps:
        return "ANTIBOT_WALL"
    # Look for unit-level markers across the 4 probed paths
    unit_paths = [k for k in ("2_floorplans", "3_floor-plans", "4_availability") if s.get(k, {}).get("has_unit_markers")]
    if unit_paths:
        return f"HAS_UNIT_MARKERS_AT_{','.join(unit_paths)}"
    if any(s.get(k, {}).get("status") == 200 and s.get(k, {}).get("links_floorplans", 0) > 0 for k in ("2_floorplans", "3_floor-plans")):
        return "FLOORPLAN_INDEX_NO_UNITS"
    # API hints?
    if s.get("9_conventional", {}).get("/wp-json/wp/v2/pages", {}).get("status") == 200:
        return "WORDPRESS_BACKED"
    if s.get("9_conventional", {}).get("/api", {}).get("is_json") or s.get("9_conventional", {}).get("/api/", {}).get("is_json"):
        return "HAS_GENERIC_API"
    if any("api." in u for u in s.get("7_js_urls", [])):
        return "JS_REFERENCES_API"
    if not fps:
        return "NO_FINGERPRINT_NO_API"
    return f"FINGERPRINT_{fps[0]}_NO_UNITS"


def _load_worklist(name: str) -> list[dict]:
    p = PROBE_DIR / f"{name}_worklist.jsonl"
    if not p.exists():
        raise SystemExit(f"missing worklist: {p}")
    with p.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _existing_pids(out_path: Path) -> set[int]:
    if not out_path.exists():
        return set()
    pids: set[int] = set()
    with out_path.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("pid") is not None:
                    pids.add(r["pid"])
            except Exception:
                continue
    return pids


def run_cohort(name: str, *, max_workers: int = 8) -> None:
    worklist = _load_worklist(name)
    out_path = PROBE_DIR / f"{name}_results.jsonl"
    done = _existing_pids(out_path)
    pending = [p for p in worklist if p.get("apartment_id") not in done]
    print(f"[{name}] worklist={len(worklist)}  done={len(done)}  pending={len(pending)}")
    if not pending:
        return
    t0 = time.time()
    with out_path.open("a") as fout, ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(probe_property, p): p for p in pending}
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            fout.write(json.dumps(res, default=str) + "\n")
            fout.flush()
            print(f"  [{i:>2}/{len(pending)}] pid={res.get('pid')} verdict={res.get('verdict')}")
    print(f"[{name}] done in {time.time() - t0:.1f}s → {out_path}")


def main() -> None:
    cohort = sys.argv[1] if len(sys.argv) > 1 else "n_full_zero"
    if cohort == "both":
        run_cohort("n_full_zero")
        run_cohort("low_strict")
    else:
        run_cohort(cohort)


if __name__ == "__main__":
    main()
