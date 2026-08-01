from __future__ import annotations

import asyncio
import hashlib
import json
import re

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import _HbSession, _session_options
from ma_poc.pms.adapters.entrata import parse_entrata_pp_unit_cards


CONFIGURED_URL = "https://www.reviveapartments.com/"
INDEX_URL = "https://www.lakesatfife.com/fife/the-lakes/conventional/"
PLAN_URLS = [
    "https://www.lakesatfife.com/floorplans/fife-WA/the-lakes/1-bed-1-bath-a-upgraded-385726-1/",
    "https://www.lakesatfife.com/floorplans/fife-WA/the-lakes/1-bed-1-bath-b-385728-1/",
    "https://www.lakesatfife.com/floorplans/fife-WA/the-lakes/2-bed-1-bath-a-385732-1/",
]

FETCH_JS = """async (url) => {
  try {
    const response = await fetch(url, {
      headers: {Accept: 'text/html,application/xhtml+xml'},
      credentials: 'include'
    });
    return {status: response.status, body: await response.text(), url: response.url};
  } catch (error) {
    return {status: -1, body: '', url: ''};
  }
}"""


def summarize(url: str, response: dict[str, object]) -> dict[str, object]:
    body = str(response.get("body") or "")
    soup = BeautifulSoup(body, "html.parser")
    rows = parse_entrata_pp_unit_cards(body, str(response.get("url") or url))
    strict_rows = [
        row
        for row in rows
        if str(row.get("unit_number") or "").strip()
        and float(row.get("market_rent_low") or 0) > 0
    ]
    return {
        "requested_url": url,
        "final_url": str(response.get("url") or ""),
        "status": int(response.get("status") or 0),
        "body_bytes": len(body.encode()),
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "identity": {
            "revive": bool(re.search(r"\bRevive\b", body, re.I)),
            "the_lakes": bool(re.search(r"The\s+Lakes", body, re.I)),
            "street_2341_58th": bool(re.search(r"2341\s+58th", body, re.I)),
            "fife": bool(re.search(r"\bFife\b", body, re.I)),
            "zip_98424": bool(re.search(r"98424", body)),
        },
        "unit_card_count": len(soup.select(".unit-card")),
        "option_row_count": len(soup.select(".option-row")),
        "parsed_rows": len(rows),
        "strict_native_positive_rows": len(strict_rows),
        "strict_rows": strict_rows,
    }


async def main() -> None:
    session = _HbSession(mode="render")
    page = None
    try:
        page = await session.open()
        await page.goto(CONFIGURED_URL, wait_until="domcontentloaded", timeout=40_000)
        landing = str(page.url or "")
        responses: list[dict[str, object]] = []
        for url in [INDEX_URL, *PLAN_URLS]:
            response = await page.evaluate(FETCH_JS, url)
            responses.append(summarize(url, response or {}))
        print(
            json.dumps(
                {
                    "guardrails": {
                        "hyperbrowser_sessions": 1,
                        "session_options": _session_options("render"),
                        "captcha_solving": False,
                        "fingerprint_rotation": False,
                        "web_unlocker": False,
                        "flaresolverr": False,
                        "llm": False,
                        "paid_canary": False,
                    },
                    "configured_url": CONFIGURED_URL,
                    "configured_landing": landing,
                    "responses": responses,
                },
                indent=2,
            )
        )
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
