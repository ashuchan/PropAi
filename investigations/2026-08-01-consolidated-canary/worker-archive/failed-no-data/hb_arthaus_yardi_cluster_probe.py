from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

from ma_poc.fetch.hyperbrowser_backend import _HbSession


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_arthaus_yardi_cluster_probe")
PAGE_URL = "https://arthaus.mov/building-community.php?slug=arthaus-jack-london"


async def _fetch(page, path: str) -> tuple[int, str]:
    response = await page.evaluate(
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
    return int(response.get("status") or 0), str(response.get("body") or "")


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    session = _HbSession(mode="render")
    results = []
    try:
        page = await session.open()
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=40_000)
        status, body = await _fetch(page, "/api-proxy.php?endpoint=property")
        properties = json.loads(body) if status == 200 and body else []
        candidates = []
        for row in properties if isinstance(properties, list) else []:
            if not isinstance(row, dict):
                continue
            acf = row.get("acf") or {}
            yardi_id = str(acf.get("yardi_id") or "").strip() if isinstance(acf, dict) else ""
            if not yardi_id:
                continue
            candidates.append((row, acf, yardi_id))
        # Jack London first, then bounded published portfolio members. Stop
        # once three non-empty native rosters prove the response shape.
        candidates.sort(
            key=lambda item: (
                0 if item[0].get("slug") == "arthaus-jack-london" else 1,
                str(item[0].get("slug") or ""),
            )
        )
        for row, acf, yardi_id in candidates[:15]:
            encoded = quote(yardi_id, safe="")
            availability_url = (
                f"/api-proxy.php?yardi_endpoint=/data/PJDUP/availability/{encoded}"
            )
            avail_status, avail_body = await _fetch(page, availability_url)
            try:
                payload = json.loads(avail_body) if avail_body else {}
            except json.JSONDecodeError:
                payload = {}
            units = payload.get("apartmentAvailabilities") or [] if isinstance(payload, dict) else []
            native_priced = [
                unit
                for unit in units
                if isinstance(unit, dict)
                and unit.get("apartmentId")
                and str(unit.get("apartmentName") or "").strip()
                and float(unit.get("minimumRent") or 0) > 0
            ]
            property_ids = sorted(
                {str(unit.get("propertyId")) for unit in native_priced if unit.get("propertyId")}
            )
            results.append(
                {
                    "slug": row.get("slug"),
                    "title": (row.get("title") or {}).get("rendered")
                    if isinstance(row.get("title"), dict)
                    else row.get("title"),
                    "address": acf.get("address"),
                    "yardi_id": yardi_id,
                    "availability_status": avail_status,
                    "native_priced_units": len(native_priced),
                    "distinct_apartment_ids": len(
                        {str(unit.get("apartmentId")) for unit in native_priced}
                    ),
                    "distinct_unit_numbers": len(
                        {str(unit.get("apartmentName")) for unit in native_priced}
                    ),
                    "source_property_ids": property_ids,
                    "sample": native_priced[:2],
                }
            )
            if sum(result["native_priced_units"] > 0 for result in results) >= 3:
                break
            await asyncio.sleep(0.2)
    finally:
        await session.close()
    payload = {
        "solve_captchas": False,
        "properties_probed": len(results),
        "nonempty_native_priced_properties": sum(
            result["native_priced_units"] > 0 for result in results
        ),
        "results": results,
    }
    (ROOT / "cluster.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload))


if __name__ == "__main__":
    asyncio.run(main())
