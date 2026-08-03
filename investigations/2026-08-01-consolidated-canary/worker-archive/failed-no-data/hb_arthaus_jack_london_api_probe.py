from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

from ma_poc.fetch.hyperbrowser_backend import _HbSession


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_arthaus_jack_london_api_probe")
PAGE_URL = "https://arthaus.mov/building-community.php?slug=arthaus-jack-london"


async def _fetch(page, path: str) -> dict[str, object]:
    return await page.evaluate(
        """async (path) => {
          try {
            const response = await fetch(path, {
              headers: {'Accept': 'application/json'},
              credentials: 'include'
            });
            return {status: response.status, body: await response.text()};
          } catch (error) {
            return {status: -1, body: '', error: String(error)};
          }
        }""",
        path,
    )


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    session = _HbSession(mode="render")
    payload: dict[str, object] = {
        "property_id": 268888,
        "page_url": PAGE_URL,
        "solve_captchas": False,
    }
    try:
        page = await session.open()
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=40_000)
        property_response = await _fetch(
            page, "/api-proxy.php?endpoint=property&slug=arthaus-jack-london"
        )
        payload["property_response"] = property_response
        property_body = json.loads(str(property_response.get("body") or "null"))
        property_row = property_body[0] if isinstance(property_body, list) else property_body
        if not isinstance(property_row, dict):
            raise RuntimeError("property endpoint returned no exact property object")
        acf = property_row.get("acf") or {}
        yardi_id = str(acf.get("yardi_id") or "").strip() if isinstance(acf, dict) else ""
        payload["property_identity"] = {
            "native_property_id": property_row.get("id"),
            "slug": property_row.get("slug"),
            "title": (property_row.get("title") or {}).get("rendered")
            if isinstance(property_row.get("title"), dict)
            else property_row.get("title"),
            "address": acf.get("address") if isinstance(acf, dict) else None,
            "yardi_id": yardi_id,
        }
        if not yardi_id:
            raise RuntimeError("exact property object has no Yardi id")
        encoded = quote(yardi_id, safe="")
        floorplans_response = await _fetch(
            page,
            f"/api-proxy.php?yardi_endpoint=/data/PJDUP/floorplans/{encoded}",
        )
        availability_response = await _fetch(
            page,
            f"/api-proxy.php?yardi_endpoint=/data/PJDUP/availability/{encoded}",
        )
        payload["floorplans_response"] = floorplans_response
        payload["availability_response"] = availability_response
    finally:
        await session.close()
    (ROOT / "probe.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(
        json.dumps(
            {
                "property_identity": payload.get("property_identity"),
                "property_status": (payload.get("property_response") or {}).get("status"),
                "floorplans_status": (payload.get("floorplans_response") or {}).get("status"),
                "availability_status": (payload.get("availability_response") or {}).get("status"),
                "availability_bytes": len(
                    str((payload.get("availability_response") or {}).get("body") or "")
                ),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
