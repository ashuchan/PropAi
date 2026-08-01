from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._probe import probe_get


OUT = Path("/private/tmp/propai-fnd-vBkmT9/hb_scully_residual3_detail_probe")
OUTPUT = OUT / "current_parent_boundaries.json"
PROPERTIES = (
    {
        "property_id": 43995,
        "property_name": "Hamilton Hall",
        "configured_url": "http://www.scullycompany.com/hamilton-hall-18.html",
        "address": "449 Hamilton St",
        "city": "Norristown",
        "zip": "19401",
        "provider_host": "hamiltonhall.scullycompany.com",
        "provider_property_id": "100003046",
        "final_path_token": "hamilton-hall",
    },
    {
        "property_id": 60141,
        "property_name": "Bridgeview",
        "configured_url": "http://www.scullycompany.com/bridgeview-14.html",
        "address": "701 Harrison St",
        "city": "Allentown",
        "zip": "18103",
        "provider_host": "bridgeview.scullycompany.com",
        "provider_property_id": "100002842",
        "final_path_token": "bridgeview",
    },
    {
        "property_id": 63191,
        "property_name": "Avenir on Fifteenth",
        "configured_url": "http://avenirphilly.com/",
        "address": "42 S 15th St",
        "city": "Philadelphia",
        "zip": "19102",
        "provider_host": "avenir.scullycompany.com",
        "provider_property_id": "100002834",
        "final_path_token": "avenir",
    },
)


def tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def main() -> None:
    results = []
    for prop in PROPERTIES:
        response = probe_get(
            prop["configured_url"],
            timeout=30,
            unlocker=False,
            retries=1,
            proxies={},
        )
        body = response.content or b""
        html = body.decode("utf-8", "replace")
        final_url = str(response.url or prop["configured_url"])
        assert response.status_code == 200 and html
        assert (urlsplit(final_url).hostname or "").casefold() == "www.scullycompany.com"
        assert prop["final_path_token"] in urlsplit(final_url).path.casefold()

        soup = BeautifulSoup(html, "lxml")
        visible = soup.get_text(" ", strip=True)
        visible_tokens = tokens(visible)
        address_tokens = re.findall(r"[a-z0-9]+", prop["address"].casefold())
        street_number = address_tokens[0]
        street_token = next(
            token
            for token in address_tokens[1:]
            if len(token) >= 3
            and token not in {"street", "st", "road", "rd", "avenue", "ave"}
        )
        assert street_number in visible_tokens
        assert street_token in visible_tokens
        assert prop["city"].casefold() in visible.casefold()
        assert prop["zip"] in visible

        iframe_id = f"website_{prop['provider_property_id']}"
        matching = []
        for iframe in soup.select("iframe[src]"):
            if str(iframe.get("id") or "").strip() != iframe_id:
                continue
            source = urljoin(final_url, str(iframe.get("src") or "").strip())
            parsed = urlsplit(source)
            query = parse_qs(parsed.query)
            if (
                (parsed.hostname or "").casefold() == prop["provider_host"]
                and query.get("snippet_type", [""])[0].casefold() == "website"
                and query.get("is_responsive_snippet", [""])[0] == "1"
                and query.get("occupancy_type", [""])[0].casefold()
                in {"1", "conventional"}
            ):
                matching.append(source)
        assert len(matching) == 1

        raw_path = OUT / f"{prop['property_id']}_current_parent.html.gz"
        with gzip.open(raw_path, "wb") as handle:
            handle.write(body)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        results.append(
            {
                **prop,
                "status": response.status_code,
                "final_url": final_url,
                "title": title,
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "raw_artifact": str(raw_path),
                "raw_artifact_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "parent_name_token_match": any(
                    token in visible_tokens
                    for token in re.findall(r"[a-z0-9]+", prop["property_name"].casefold())
                    if len(token) >= 4
                ),
                "parent_street_number_match": True,
                "parent_street_token_match": True,
                "parent_city_match": True,
                "parent_zip_match": True,
                "published_inventory_iframe_id": iframe_id,
                "published_inventory_iframe_url": matching[0],
            }
        )

    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "lane": "scully_current_configured_parent_boundary_capture",
        "guardrails": {
            "direct_http_only": True,
            "web_unlocker": False,
            "hyperbrowser": False,
            "llm": False,
            "captcha_solving": False,
            "paid_canary": False,
        },
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
                "properties": len(results),
                "provider_property_ids": [
                    row["provider_property_id"] for row in results
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
