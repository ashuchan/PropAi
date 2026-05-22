"""Custom WordPress ``/ajax/api/plansandpricing/`` floorplan-feed parser.

This is a same-origin WP/AJAX endpoint that several custom marketing
sites use to back their /floor-plans page. The feed shape was first
observed on Hazel at National Landing
(``www.livehazelnationallanding.com/ajax/api/plansandpricing/`` —
PID 264589 on 2026-05-22) but the response is a verbatim
RentDynamics-style payload that is used across other operators
(``DisplayPrice`` / ``UnitsDatesAvailable`` / ``FloorPlanID`` /
``PriceFeedName`` are RentDynamics PMS hallmarks).

The matcher therefore gates on PATH (``/ajax/api/plansandpricing``) not
host, so any property hitting this WP route gets parsed deterministically
without re-running the LLM. The body envelope is a flat JSON array of
floor-plan dicts.

Live-verified item keys (PID 264589, 37 floor plans):

    FloorPlanName                  human-readable name ("S1", "A1", …)
    Bedrooms / Bathrooms           numeric (0.0, 1.0, 1.5, …)
    MinSqFt / MaxSqFt              integer sqft bounds
    MinPrice / MaxPrice            ASKING (list) rent range — pre-special
    UnitMinPrice / UnitMaxPrice    EFFECTIVE (per-unit) rent range — post-special
    MinNetEffective / MaxNetEffective  net-effective rent (after lease
                                      incentives amortised across the term)
    UnitsAvailable / TotalUnitsAvailable  per-plan availability counts
    EarliestUnitAvailable          "MM-DD-YYYY" (NON-ISO — convert!)
    UnitsDatesAvailable            pipe/semi-colon encoded ``count|MM/DD/YYYY``
                                      pairs; expands to one row per available
                                      cohort when more than one date present
    LeaseTerm                      months
    SpecialsDescription            free-text concession copy (null when none)
    FloorPlanType                  "Studio" / "1 Bedroom" / "2 Bedroom" / …
    PriceFeedName                  upstream PMS name (RentDynamics, RealPage …)
    Available                      bool — plan is leasable

We emit ``MinPrice``/``MaxPrice`` as the market range and ``UnitMinPrice``/
``UnitMaxPrice`` as the effective range — both surfaced so downstream
analytics can compute the concession spread without parsing
``SpecialsDescription``. When the two ranges agree the row is "no special
active" regardless of what ``SpecialsDescription`` says.

When ``UnitsDatesAvailable`` carries multiple ``count|date`` pairs (e.g.
``"1|07/01/2026;2|08/15/2026"``) we expand to one plan_summary row per
date cohort with the per-cohort count, so day-on-market analytics see the
date distribution. Single-pair payloads collapse back to one row.
"""

from __future__ import annotations

import re
from typing import Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)

TIER = "TIER_1_API_PLANSANDPRICING"

# Path marker. Host is INTENTIONALLY ignored — many properties use this
# same WP/AJAX route on their own marketing host.
_PATH_MARKER = "/ajax/api/plansandpricing"

# Date arrives as "MM-DD-YYYY" (sloppy) or "MM/DD/YYYY". Convert to ISO.
_DATE_MMDDYYYY_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")


def is_plansandpricing_url(url: str) -> bool:
    """True if *url* matches the WP plansandpricing AJAX route."""
    return bool(url) and _PATH_MARKER in url.lower()


def _is_plansandpricing_body(body: Any) -> bool:
    """Return True when *body* looks like a plansandpricing payload.

    The body is a flat JSON list of dicts whose first item carries
    ``FloorPlanName`` AND at least one of the rent or sqft markers. The
    dual-key check guards against unrelated list-at-root payloads that
    happen to share a single key.
    """
    if not isinstance(body, list) or not body:
        return False
    first = body[0]
    if not isinstance(first, dict):
        return False
    if "FloorPlanName" not in first:
        return False
    return any(
        k in first
        for k in (
            "MinPrice", "MaxPrice",
            "UnitMinPrice", "UnitMaxPrice",
            "MinSqFt", "MaxSqFt",
            "UnitsAvailable", "EarliestUnitAvailable",
        )
    )


def _to_int(v: Any) -> int | None:
    """Coerce to int. Treat empty / "0" / 0.0 / null as None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("0", "0.0", "null"):
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _norm_date(s: str) -> str:
    """Convert ``MM-DD-YYYY`` / ``MM/DD/YYYY`` to ``YYYY-MM-DD``.

    Returns the original string when it doesn't match the US shape — the
    feed sometimes ships already-ISO dates which need no rewrite, and the
    odd unparseable string should reach the validator un-mangled.
    """
    if not s:
        return ""
    s = s.strip()
    if not s:
        return ""
    # Already ISO?
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = _DATE_MMDDYYYY_RE.match(s)
    if not m:
        return s
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def _parse_units_dates_available(s: Any) -> list[tuple[int, str]]:
    """Decode ``UnitsDatesAvailable`` into ``[(count, iso_date), …]``.

    Format observed: ``"1|07/01/2026"`` for a single-date plan, or
    ``"1|07/01/2026;2|08/15/2026"`` for multiple date cohorts. Returns an
    empty list when the string is malformed or empty — callers fall back
    to ``EarliestUnitAvailable``.
    """
    if not s:
        return []
    out: list[tuple[int, str]] = []
    for part in str(s).split(";"):
        part = part.strip()
        if not part or "|" not in part:
            continue
        count_str, date_str = part.split("|", 1)
        c = _to_int(count_str)
        d = _norm_date(date_str)
        if c is None or not d:
            continue
        out.append((c, d))
    return out


def parse_plansandpricing_units(
    items: list[dict[str, Any]],
    url: str,
) -> list[dict[str, str]]:
    """Project plansandpricing rows into the canonical unit-dict shape.

    Each input row is a floor-plan summary. We expand multi-cohort
    ``UnitsDatesAvailable`` strings into one row per date so downstream
    aggregation sees each availability cohort separately. Rows where the
    plan is ``Available=False`` are emitted with status UNAVAILABLE so the
    extractor records pricing for not-currently-leasable plans rather
    than silently dropping them.
    """
    units: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        fp_name = str(item.get("FloorPlanName") or "").strip()

        # Bedrooms/Bathrooms arrive as floats (Bedrooms=0.0 for studio).
        beds_raw = item.get("Bedrooms")
        baths_raw = item.get("Bathrooms")
        beds: int | None
        if beds_raw in (None, ""):
            beds = None
        else:
            try:
                beds = int(float(beds_raw))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                beds = None
        baths_val: float | None
        if baths_raw in (None, ""):
            baths_val = None
        else:
            try:
                baths_val = float(baths_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                baths_val = None

        sqft_min = _to_int(item.get("MinSqFt"))
        sqft_max = _to_int(item.get("MaxSqFt"))
        sqft_str = ""
        if sqft_min and sqft_max and sqft_min != sqft_max:
            sqft_str = f"{sqft_min}-{sqft_max}"
        elif sqft_min:
            sqft_str = str(sqft_min)
        elif sqft_max:
            sqft_str = str(sqft_max)

        # Effective (per-unit) price is the asking rent we surface — that's
        # what tenants see on the site. The market range (MinPrice/MaxPrice)
        # is preserved in the rent_range string when it differs.
        eff_lo = money_to_int(str(item.get("UnitMinPrice") or ""))
        eff_hi = money_to_int(str(item.get("UnitMaxPrice") or ""))
        mkt_lo = money_to_int(str(item.get("MinPrice") or ""))
        mkt_hi = money_to_int(str(item.get("MaxPrice") or ""))

        rent_lo = eff_lo or mkt_lo
        rent_hi = eff_hi or mkt_hi
        if rent_hi is None:
            rent_hi = rent_lo

        # Concession: prefer free-text SpecialsDescription, else synthesise a
        # "$X off" placeholder when market > effective (i.e. a discount is
        # actually being applied).
        special_text = str(item.get("SpecialsDescription") or "").strip()
        concession = special_text
        if not concession and mkt_lo and eff_lo and mkt_lo > eff_lo:
            concession = f"${mkt_lo - eff_lo} off baseline"

        avail = item.get("Available")
        avail_total = _to_int(item.get("UnitsAvailable")) or _to_int(
            item.get("TotalUnitsAvailable")
        )

        date_cohorts = _parse_units_dates_available(item.get("UnitsDatesAvailable"))
        earliest = _norm_date(str(item.get("EarliestUnitAvailable") or ""))

        # Lease term + plan type are useful when present but never gate
        # extraction.
        lease_term = ""
        lt = _to_int(item.get("LeaseTerm"))
        if lt is not None and lt > 0:
            lease_term = str(lt)

        building = ""
        # ``FloorplanBuildingName`` is documented in the schema; ship it as
        # ``building`` when populated so multi-building properties keep
        # plan-to-building correspondence.
        bld_raw = item.get("FloorplanBuildingName")
        if isinstance(bld_raw, str) and bld_raw.strip():
            building = bld_raw.strip()

        bed_lbl = bed_label_from(beds, fp_name)
        beds_str = str(beds) if beds is not None else ""
        baths_str = (
            str(int(baths_val))
            if baths_val is not None and baths_val == int(baths_val)
            else (str(baths_val) if baths_val is not None else "")
        )

        # No date cohorts: emit a single row using EarliestUnitAvailable.
        if not date_cohorts:
            status = "AVAILABLE" if (avail is True and avail_total) else (
                "UNAVAILABLE" if avail is False else "UNKNOWN"
            )
            units.append(
                make_unit_dict(
                    floor_plan_name=fp_name,
                    bed_label=bed_lbl,
                    bedrooms=beds_str,
                    bathrooms=baths_str,
                    sqft=sqft_str,
                    unit_number="",  # plan-level
                    rent_range=format_rent_range(rent_lo, rent_hi),
                    rent_low=rent_lo,
                    rent_high=rent_hi,
                    availability_status=status,
                    available_units=str(avail_total) if avail_total else "",
                    availability_date=earliest,
                    lease_term=lease_term,
                    building=building,
                    concession=concession,
                    source_api_url=url,
                    extraction_tier=TIER,
                )
            )
            continue

        # Multi-cohort expansion: one row per (count, date) pair.
        for cohort_count, cohort_date in date_cohorts:
            units.append(
                make_unit_dict(
                    floor_plan_name=fp_name,
                    bed_label=bed_lbl,
                    bedrooms=beds_str,
                    bathrooms=baths_str,
                    sqft=sqft_str,
                    unit_number="",
                    rent_range=format_rent_range(rent_lo, rent_hi),
                    rent_low=rent_lo,
                    rent_high=rent_hi,
                    availability_status="AVAILABLE",
                    available_units=str(cohort_count),
                    availability_date=cohort_date,
                    lease_term=lease_term,
                    building=building,
                    concession=concession,
                    source_api_url=url,
                    extraction_tier=TIER,
                )
            )
    return units


def try_parse_plansandpricing(
    resp: dict[str, Any],
) -> tuple[list[dict[str, str]], bool]:
    """Best-effort plansandpricing parse for a single intercepted response.

    Returns ``(units, matched)``. ``matched`` is True when the URL+body
    pair is recognised even if the projection emitted zero rows.
    """
    url = str(resp.get("url") or "")
    body = resp.get("body")
    if not is_plansandpricing_url(url):
        return [], False
    if not _is_plansandpricing_body(body):
        return [], False
    assert isinstance(body, list)
    return parse_plansandpricing_units(body, url), True
