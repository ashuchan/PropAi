from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx


OUT = Path("/private/tmp/propai-fnd-vBkmT9/rentcafe_residual_parallel")

TARGETS = {
    594: [
        "https://www.harbinwoodbyelon.com/floorplans.aspx",
        "https://harbinwoodbyelon.securecafe.com/onlineleasing/harbinwood/availableunits.aspx",
    ],
    5782: [
        "https://www.springfieldrenton.com/floorplans",
        "https://springfieldrenton.securecafe.com/onlineleasing/springfield-apartments-5/availableunits.aspx",
    ],
    17186: [
        "https://www.theparkattivoli.com/floorplans",
    ],
    17674: [
        "https://hpgmanagement.com/property/6550yucca/",
    ],
    24337: [
        "https://residential.eprodesse.com/colonial-house-apartments/index.aspx",
    ],
    27080: [
        "https://www.oxfordrealtygroup.com/properties/new-jersey/easton-north/",
        "https://oxfordrealtygroup.securecafe.com/onlineleasing/easton-north/availableunits.aspx",
    ],
    39710: [
        "https://www.greystar.com/ashton-at-judiciary-square-luxury-apartments-washington-dc/p_13572",
    ],
    58390: [
        "https://live30lancaster.com/floorplans",
        "https://live30lancaster.securecafe.com/onlineleasing/new-30-lancaster/availableunits.aspx",
    ],
    58546: [
        "https://wright-weber.com/property/LakePark",
    ],
    71534: [
        "https://www.wimmercommunities.com/apartments/menomonee-falls/riverwalk-on-the-falls/",
        "https://wimmercommunities.securecafe.com/onlineleasing/apartmentsforrent/availableunits.aspx",
    ],
    72743: [
        "https://brandywinecommunities.com/properties/della-plaza/",
        "https://listings-brandywinecommunities.securecafeapplicant.com/onlineleasing/content3/access/della-plaza/floorplans/997141",
    ],
    74519: [
        "https://www.thejamestownapartments.com/floorplans",
        "https://thejamestownapartments.securecafe.com/onlineleasing/jamestown-apartments/availableunits.aspx",
    ],
    223248: [
        "https://www.metroplexapts.com/",
    ],
    225886: [
        "https://www.casabaywoodapts.com/floorplans",
        "https://casabaywoodapts.securecafe.com/onlineleasing/casa-baywood/availableunits.aspx",
        "https://casabaywoodapts.securecafeapplicant.com/onlineleasing/content3/access/casa-baywood/floorplans",
    ],
    231543: [
        "https://bestrentnj.com/Communities/Autumn-Hills/",
        "https://autumnhills-bestrentnj.securecafe.com/onlineleasing/village-at-autumn-hills/availableunits.aspx?myolepropertyid=1026013&floorPlans=3429401",
    ],
    241538: [
        "https://www.block88apts.com/floorplans",
        "https://block88apts.securecafe.com/onlineleasing/block-88/availableunits.aspx",
    ],
    244756: [
        "https://harvest-properties.com/project/town-commons-apartments/",
    ],
    262799: [
        "https://www.dermotcompany.com/building/220-east-72nd-street#availability",
    ],
    266766: [
        "https://www.101oxford.com/floorplans",
        "https://101oxford.securecafe.com/onlineleasing/101-w-oxford/availableunits.aspx",
    ],
    289338: [
        "https://www.201walnut.com/floorplans",
        "https://201walnut.securecafe.com/onlineleasing/201-walnut-avenue/availableunits.aspx",
    ],
}

URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)


async def fetch_one(client: httpx.AsyncClient, pid: int, index: int, url: str) -> dict:
    row = {"property_id": pid, "input_url": url}
    try:
        response = await client.get(url)
        body = response.content
        text = response.text.replace("\\/", "/")
        host = (urlparse(str(response.url)).hostname or "").replace(".", "_")
        raw_path = OUT / f"{pid}_{index}_{host}.html.gz"
        with gzip.open(raw_path, "wb") as fh:
            fh.write(body)
        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        urls = []
        for match in URL_RE.finditer(text):
            candidate = match.group(0).replace("&amp;", "&")
            if any(token in candidate.casefold() for token in ("securecafe", "rentcafe", "floorplan", "availableunit")):
                if candidate not in urls:
                    urls.append(candidate)
        row.update(
            {
                "status": response.status_code,
                "final_url": str(response.url),
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "title": re.sub(r"\s+", " ", title_match.group(1)).strip()[:240] if title_match else "",
                "markers": {
                    "avail_unit_row": "availunitrow" in text.casefold(),
                    "unit_availability": "unitavailability" in text.casefold(),
                    "applicant_portal": "applicant portal" in text.casefold(),
                    "captcha": any(x in text.casefold() for x in ("captcha", "turnstile", "cf-chl")),
                    "contact_for_availability": "contact for availability" in text.casefold(),
                    "property_id": bool(re.search(r"(?:myolepropertyid|propertyid)\D{0,12}\d{4,}", text, re.I)),
                },
                "candidate_urls": urls[:30],
                "raw_path": str(raw_path),
            }
        )
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


async def main() -> None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    limits = httpx.Limits(max_connections=6, max_keepalive_connections=3)
    timeout = httpx.Timeout(30.0, connect=15.0)
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout, limits=limits) as client:
        tasks = [
            fetch_one(client, pid, index, url)
            for pid, urls in TARGETS.items()
            for index, url in enumerate(urls)
        ]
        rows = await asyncio.gather(*tasks)
    output = {
        "guardrails": {
            "direct_only": True,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "hyperbrowser": False,
            "llm": False,
            "paid_canary": False,
        },
        "results": rows,
    }
    path = OUT / "direct_probe_manifest.json"
    path.write_text(json.dumps(output, indent=2, sort_keys=True))
    print(path)
    for row in rows:
        print(json.dumps({k: row.get(k) for k in ("property_id", "status", "final_url", "bytes", "title", "markers", "error")}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
