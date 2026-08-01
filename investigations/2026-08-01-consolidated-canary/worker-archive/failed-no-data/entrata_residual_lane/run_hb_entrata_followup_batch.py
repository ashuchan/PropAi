#!/usr/bin/env python3
"""Run the strict one-session Entrata follow-up against an explicit batch."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


BASE = Path(
    "/private/tmp/propai-fnd-vBkmT9/entrata_residual_lane/"
    "hb_entrata_residual_followup_audit.py"
)


def safe_native_card_parser(html: str, url: str) -> list[dict[str, object]]:
    """Extract only explicit PP apartment cards with stable native IDs.

    This deliberately avoids document-wide legacy regular expressions. It is
    fail-closed: visible unit number, native ``data-unit``/UID, floor-plan ID,
    and a positive rent inside the same card are all mandatory.
    """
    soup = BeautifulSoup(html or "", "lxml")
    path = urlsplit(url).path.rstrip("/")
    fpid_match = re.search(r"-(\d+)(?:-\d+)?(?:/fp_name/.*)?$", path)
    derived_fpid = fpid_match.group(1) if fpid_match else ""
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    cards = list(soup.select(".unit-card"))
    cards.extend(
        card
        for card in soup.select(".option-row")
        if card not in cards
    )
    cards.extend(
        card
        for card in soup.select('a[data-jd-fp-selector="unit-card"]')
        if "preload" not in " ".join(card.get("class") or [])
        and card not in cards
    )
    for card in cards:
        unit = ""
        for selector in (".unit-number", ".detail.first"):
            node = card.select_one(selector)
            if node:
                unit = node.get_text(" ", strip=True)
                break
        if not unit:
            unit = str(card.get("title") or "")
        unit = re.sub(r"^\s*(?:unit\s*)?#?\s*", "", unit, flags=re.I).strip()

        uid = str(
            card.get("data-unit-id")
            or card.get("data-uid")
            or card.get("data-unit")
            or ""
        ).strip()
        native = card.select_one("[data-unit], [data-unit-id], [data-uid]")
        if native and not uid:
            uid = str(
                native.get("data-unit")
                or native.get("data-unit-id")
                or native.get("data-uid")
                or ""
            ).strip()
        fpid = str(card.get("data-floorplan") or derived_fpid).strip()
        if native and not fpid:
            fpid = str(native.get("data-floorplan") or "").strip()

        rent = 0
        for selector in (
            ".unit-pricing .price-value",
            ".unit-price",
            ".unit-rent",
            ".stat-value.unit-rent",
            "[data-jd-fp-selector='unit-rent']",
        ):
            node = card.select_one(selector)
            if not node:
                continue
            match = re.search(r"\$\s*([1-9]\d{0,2}(?:,\d{3})*)", node.get_text(" ", strip=True))
            if match:
                rent = int(match.group(1).replace(",", ""))
                break
        if not (unit and uid and fpid and rent > 0):
            continue
        key = (unit.casefold(), uid)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "floor_plan_name": "",
                "unit_number": unit,
                "market_rent_low": rent,
                "market_rent_high": rent,
                "availability_status": "AVAILABLE",
                "availability_date": "",
                "source_api_url": url,
                "extraction_tier": "TIER_1_DOM_ENTRATA_PP_SAFE_NATIVE_CARD",
                "source_ids": {
                    "entrata_uid": uid,
                    "entrata_fpid": fpid,
                },
                "data_gaps": [],
                "data_quality_flag": "",
            }
        )
    return rows


def main() -> None:
    spec = importlib.util.spec_from_file_location("entrata_followup_base", BASE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The broad JSON-LD/legacy regex parsers can exhibit pathological runtime
    # on rendered WAF/template bodies. The current residual targets publish
    # native PP unit-card/JD-card/UnitsData or VUS routes, so keep the drill to
    # those bounded native parsers and fail closed on everything else.
    module.PARSERS = (safe_native_card_parser,)
    targets = json.loads(os.environ["ENTRATA_TARGETS_JSON"])
    assert isinstance(targets, dict) and targets
    assert all(str(key).isdigit() for key in targets)
    module.TARGETS = {str(key): str(value) for key, value in targets.items()}
    module.OUTPUT = Path(os.environ["ENTRATA_OUTPUT_PATH"])
    asyncio.run(module.main())


if __name__ == "__main__":
    main()
