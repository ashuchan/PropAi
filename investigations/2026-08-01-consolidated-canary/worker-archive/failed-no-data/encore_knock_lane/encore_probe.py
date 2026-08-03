from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._probe import probe_get


OUT = Path("/private/tmp/propai-fnd-vBkmT9/encore_knock_lane/encore_probe.json")
BODY_DIR = OUT.parent / "encore_bodies"
TARGETS = [
    (42571, "Westwood Village", "2203 Beck Ave", "Panama City", "FL", "32405", "https://westwoodvillageapthomes.com/en/"),
    (59649, "801 Polaris", "801 Polaris Pkwy", "Columbus", "OH", "43240", "https://801polaris.com/"),
    (228341, "The Commons at Cowan Boulevard", "2352 Cowan Blvd", "Fredericksburg", "VA", "22401", "http://commonsatcowanboulevard.com/"),
    (252116, "Emerson Park", "6735 Brookline Gardens Rd", "Westerville", "OH", "43081", "https://www.liveemersonpark.com/"),
    (258789, "Luxe 88", "1025 Luxe Ave", "Columbus", "OH", "43220", "https://luxe88apts.com/"),
    (275898, "Bradley Pointe", "1355 Bradley Blvd", "Savannah", "GA", "31419", "https://www.liveatbradleypointe.com/"),
]


def fetch(url: str) -> tuple[int, str, str, str]:
    try:
        response = probe_get(url, timeout=30, unlocker=False, retries=1)
        return (
            int(getattr(response, "status_code", 0) or 0),
            str(getattr(response, "url", "") or url),
            str(getattr(response, "text", "") or ""),
            "",
        )
    except Exception as exc:
        return 0, url, "", f"{type(exc).__name__}: {exc}"


def route_summary(url: str, status: int, final_url: str, body: str, error: str) -> dict:
    soup = BeautifulSoup(body, "html.parser") if body else None
    title = soup.title.get_text(" ", strip=True) if soup and soup.title else ""
    plan_links: list[str] = []
    iframe_urls: list[str] = []
    if soup:
        for anchor in soup.select("a[href]"):
            href = urljoin(final_url, str(anchor.get("href") or ""))
            if re.search(r"/(?:floor-?plans?)(?:/|$)", urlparse(href).path, re.I):
                if href not in plan_links:
                    plan_links.append(href)
        for frame in soup.select("iframe[src]"):
            src = urljoin(final_url, str(frame.get("src") or ""))
            if src not in iframe_urls:
                iframe_urls.append(src)
    low = body.lower()
    return {
        "requested_url": url,
        "status": status,
        "final_url": final_url,
        "body_bytes": len(body.encode()),
        "title": title,
        "error": error,
        "signals": {
            "meetelise": "meetelise" in low,
            "jonah": "jonahwidget" in low or "jonahdigital" in low,
            "jonah_resource": "jd-fp-data-script-resource" in low,
            "rentpress": "data-floorplans" in low,
            "entrata": "entrata" in low,
            "entrata_snippet": "entratasnipit" in low,
            "custom_floorplan": "custom_floorplan" in low,
            "jsonld_floorplan_count": low.count('"@type": "floorplan"') + low.count('"@type":"floorplan"'),
            "jsonld_apartment_count": low.count('"@type": "apartment"') + low.count('"@type":"apartment"'),
            "native_unit_token_count": len(re.findall(r'(?i)(?:unit[_ -]?(?:id|number|code)|apartment[_ -]?number)', body)),
        },
        "plan_links": plan_links[:40],
        "iframe_urls": iframe_urls[:20],
    }


def current_routes(home_url: str, home_final: str, body: str) -> list[str]:
    soup = BeautifulSoup(body, "html.parser")
    exact: list[str] = []
    for anchor in soup.select("a[href]"):
        href = urljoin(home_final, str(anchor.get("href") or ""))
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        text = anchor.get_text(" ", strip=True)
        if re.search(r"floor\s*-?\s*plans?|pricing|availability|lease now|apply", f"{parsed.path} {text}", re.I):
            href = href.split("#", 1)[0]
            if href and href != home_url and href not in exact:
                exact.append(href)
    for frame in soup.select("iframe[src]"):
        src = urljoin(home_final, str(frame.get("src") or ""))
        if re.search(r"entrata|rentcafe|securecafe|availability|floorplan|leasing", src, re.I) and src not in exact:
            exact.append(src)
    # Keep this bounded and deterministic: exact public links only.
    return exact[:6]


def main() -> None:
    BODY_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for pid, name, address, city, state, zip_code, website in TARGETS:
        status, final_url, body, error = fetch(website)
        home = route_summary(website, status, final_url, body, error)
        (BODY_DIR / f"{pid}_home.html").write_text(body, encoding="utf-8")
        routes = []
        for index, route in enumerate(current_routes(website, final_url, body), start=1):
            route_status, route_final, route_body, route_error = fetch(route)
            summary = route_summary(route, route_status, route_final, route_body, route_error)
            routes.append(summary)
            (BODY_DIR / f"{pid}_route{index}.html").write_text(route_body, encoding="utf-8")
        results.append({
            "property_id": pid,
            "canonical_name": name,
            "canonical_address": address,
            "canonical_city": city,
            "canonical_state": state,
            "canonical_zip": zip_code,
            "website": website,
            "home": home,
            "exact_linked_routes": routes,
        })
    payload = {
        "scope": "current exact public routes; direct requests only; no unlocker; no LLM; no CAPTCHA solving",
        "target_count": len(results),
        "results": results,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "artifact": str(OUT),
        "results": [
            {
                "property_id": row["property_id"],
                "home_status": row["home"]["status"],
                "home_final": row["home"]["final_url"],
                "route_count": len(row["exact_linked_routes"]),
                "route_statuses": [route["status"] for route in row["exact_linked_routes"]],
            }
            for row in results
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
