# CLAUDE.md — Adapter Misrouting Fixes & New PMS Adapters

**Source of truth for this work**: the 2026-04-20 run (`data/runs/2026-04-20/`) produced 72 failures out of 200 properties. Live web fetches against representative failures established that:

- **12 Windsor Communities failures** routed to `TIER_1_API_RENTCAFE` but actually run on **Funnel / Nestio** (`nestiolistings.com/api/v2/listings/residential/rentals/`)
- **2 Mark-Taylor failures** routed to `TIER_1_API_RENTCAFE` but actually run on **Union / Liftlytics** (proprietary, JS-rendered)
- **3 Vegas properties** (Summer Winds, Madera, Positano) routed to `TIER_1_API_SIGHTMAP` but actually run on **TouchTour / Ovation** (`lasvegasliving.mytouchtour.com`)
- The 2026-04-19 RentCafe PascalCase fix had **zero effect** on 04-20 because its documented targets (Windsor, Weidner, Bexley, Pacifica) are not on RentCafe at all

This document specifies six changes, in execution order. Each change has explicit file paths, test case names, and a validation gate. Do not skip the gates. Do not implement step N+1 until step N's gate is green.

---

## Principles (read once, apply throughout)

1. **Stop stamping `tier_used` pre-extraction.** Every adapter in this repo currently sets `tier_used = "TIER_1_API_<PMS>"` in the `AdapterResult` constructor before any body-shape check runs. A property where zero responses matched is indistinguishable in the report from a property where shape matched but parsing emitted zero units. Fix this first — everything else depends on being able to tell those apart.

2. **Structured error codes, not prose.** The SightMap adapter's `SIGHTMAP_NO_RESPONSE` / `SIGHTMAP_AMENITIES_ONLY` / `SIGHTMAP_PARSE_FAILED` split is the pattern to generalize. Every adapter failure mode must be expressible as a machine-readable code, not a sentence.

3. **New adapters follow the existing `PmsAdapter` protocol.** Same `extract(page, ctx) -> AdapterResult` signature, same `static_fingerprints()`, same top-of-file research log block (Web sources / Real payloads / Key findings). Do not invent a new adapter shape.

4. **Research-first on every adapter.** Before implementing Funnel or TouchTour, produce the research log block with at least 2 real API payloads inspected. If `data/runs/*/raw_api/` has no capture for a target PMS, run a live fetch against 3 representative properties from the failure list and save the captures under `tests/pms/adapters/fixtures/<pms>/<canonical_id>.json` before writing parser code.

5. **Reuse over replace.** `ma_poc/pms/adapters/_parsing.py` already has `money_to_int`, `bed_label_from`, `make_unit_dict`, `format_rent_range`. Use these. Do not reimplement.

6. **Conventions** (same as `claude-scrapper-arch.md`):
   - Python 3.11+, Pydantic v2 (`model_dump(mode='json')`, never `.dict()`)
   - `pytest-asyncio` with `asyncio_mode = "auto"`
   - Type hints on all signatures
   - Never `browser.close()` — always `context.close()`
   - `asyncio.Semaphore` for browser concurrency, `asyncio.Lock` for state files
   - `hashlib.sha256` for deterministic hashing
   - Relative imports within `ma_poc`, lazy imports for heavy modules

---

## Change 1 — Fix pre-extraction tier stamping (both existing adapters)

**Problem.** `ma_poc/pms/adapters/rentcafe.py` and `ma_poc/pms/adapters/sightmap.py` both do:

```python
async def extract(self, page, ctx):
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE")  # stamped too early
    ...
    if not all_units:
        result.confidence = 0.0  # tier_used unchanged — looks like "RentCafe ran and parsed zero"
```

Downstream reporting sees `tier_used: TIER_1_API_RENTCAFE` on 38 failures and cannot distinguish "no RentCafe response captured" from "RentCafe response captured and parsed to zero units."

**Fix.** Re-stamp `tier_used` on every failure path with a specific code. Pattern:

```python
_TIER_BASE = "TIER_1_API_RENTCAFE"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_LIST_EMPTY = f"{_TIER_BASE}_LIST_EMPTY"
_TIER_PARSE_ZERO = f"{_TIER_BASE}_PARSE_ZERO"

async def extract(self, page, ctx):
    result = AdapterResult(tier_used=_TIER_BASE)
    # ... run adapter logic ...
    if all_units:
        result.tier_used = _TIER_BASE  # success
        result.units = all_units
        result.confidence = min(0.95, 0.7 + 0.05 * len(all_units))
        return result
    # Failure — classify precisely
    result.tier_used = _classify_failure(api_responses)
    result.confidence = 0.0
    return result
```

### Files to modify

- `ma_poc/pms/adapters/rentcafe.py`
- `ma_poc/pms/adapters/sightmap.py`
- `ma_poc/pms/adapters/base.py` — no change expected; verify `tier_used: str` is not frozen in `AdapterResult`

### Failure classifier for RentCafe

```python
def _classify_rentcafe_failure(api_responses: list[dict]) -> tuple[str, str]:
    """Return (tier_code, error_message)."""
    if not api_responses:
        return (_TIER_NO_RESPONSE,
                "RENTCAFE_NO_RESPONSE: no network responses captured during page load")
    shape_matches = [r for r in api_responses
                     if _is_rentcafe_response(r.get("body"))]
    if not shape_matches:
        return (_TIER_SHAPE_REJECTED,
                f"RENTCAFE_SHAPE_REJECTED: {len(api_responses)} responses captured, "
                f"none matched RentCafe envelope/key signature")
    # Shape matched but nothing came out
    total_items = 0
    for r in shape_matches:
        items = _unwrap_rentcafe_list(r.get("body"))
        total_items += len(items or [])
    if total_items == 0:
        return (_TIER_LIST_EMPTY,
                f"RENTCAFE_LIST_EMPTY: {len(shape_matches)} shape-matched responses, "
                f"floorplan list was empty in all")
    return (_TIER_PARSE_ZERO,
            f"RENTCAFE_PARSE_ZERO: {total_items} floorplan items present across "
            f"{len(shape_matches)} responses, but parser emitted zero units "
            f"(field-name mismatch likely)")
```

Mirror the same pattern in `sightmap.py`. SightMap already has three codes (`SIGHTMAP_NO_RESPONSE` / `SIGHTMAP_AMENITIES_ONLY` / `SIGHTMAP_PARSE_FAILED`) — keep those names but extend so they also re-stamp `tier_used`.

### Also fix: latent value-case bug in `_is_rentcafe_response`

In `ma_poc/pms/adapters/rentcafe.py`:

```python
# BEFORE
if first_lc.get("api") == "rentcafe":

# AFTER
if str(first_lc.get("api") or "").lower() == "rentcafe":
```

Windsor payloads (if they ever get through the router) may ship `"Api": "RentCafe"`. After `_normalise_item`, the key is lowercased but the value is not. Low-risk, cheap fix.

### Also fix: tighten SightMap's `_is_sightmap_response`

Current check is too loose — any CMS with `data.amenities` matches. In `ma_poc/pms/adapters/sightmap.py`:

```python
# BEFORE
sightmap_keys = {"units", "floor_plans", "amenities", "sightmap_id"}
return bool(sightmap_keys & set(data.keys()))

# AFTER
# Strong signal: explicit sightmap_id OR the units+floor_plans pair that defines the envelope
if "sightmap_id" in data:
    return True
if "floor_plans" in data and isinstance(data.get("floor_plans"), list):
    fps = data["floor_plans"]
    if fps and isinstance(fps[0], dict):
        # SightMap floor_plan shape: id, name, filter_label, bedroom_count, bathroom_count
        sightmap_fp_keys = {"bedroom_count", "bathroom_count", "filter_label"}
        if sightmap_fp_keys & set(fps[0].keys()):
            return True
if "units" in data and "floor_plans" in data:
    return True
return False
```

### Named tests — add to `tests/pms/adapters/test_rentcafe.py`

| Test | Input | Expected |
|---|---|---|
| `test_rentcafe_tier_re_stamped_on_no_response` | `ctx._api_responses = []` | `result.tier_used == "TIER_1_API_RENTCAFE_NO_RESPONSE"`, `confidence == 0.0` |
| `test_rentcafe_tier_re_stamped_on_shape_reject` | `ctx._api_responses = [{"url": "x", "body": {"random": "payload"}}]` | `result.tier_used == "TIER_1_API_RENTCAFE_SHAPE_REJECTED"` |
| `test_rentcafe_tier_re_stamped_on_empty_list` | `ctx._api_responses = [{"url": "x", "body": {"data": []}}]` with Yardi wrapper | `result.tier_used == "TIER_1_API_RENTCAFE_LIST_EMPTY"` |
| `test_rentcafe_tier_re_stamped_on_parse_zero` | Real RentCafe envelope but items have no recognizable field names | `result.tier_used == "TIER_1_API_RENTCAFE_PARSE_ZERO"` |
| `test_rentcafe_success_tier_unchanged` | Valid Brookfield capture from `tests/pms/adapters/fixtures/rentcafe/35593.json` | `result.tier_used == "TIER_1_API_RENTCAFE"`, `len(result.units) > 0` |
| `test_rentcafe_api_value_case_insensitive` | `{"Api": "RentCafe", "FloorplanName": "A1", "FloorplanId": "1", "MinimumRent": "1500", "MaximumRent": "1600"}` | `_is_rentcafe_response(body_wrapping_item)` is `True` |
| `test_rentcafe_errors_list_has_machine_readable_prefix` | any failure case | `result.errors[0].split(":")[0]` matches `/^RENTCAFE_[A-Z_]+$/` |

### Named tests — add to `tests/pms/adapters/test_sightmap.py`

| Test | Input | Expected |
|---|---|---|
| `test_sightmap_tier_re_stamped_on_no_response` | `ctx._api_responses = []` | `result.tier_used == "TIER_1_API_SIGHTMAP_NO_RESPONSE"` |
| `test_sightmap_amenities_only_no_false_positive` | `{"data": {"amenities": [...]}}` only | `_is_sightmap_response` is `False` (was True before) |
| `test_sightmap_shape_check_requires_fp_keys` | `{"data": {"floor_plans": [{"id": 1, "name": "X"}]}}` (missing bedroom_count/filter_label) | `_is_sightmap_response` is `False` |
| `test_sightmap_shape_check_accepts_real_fp_envelope` | `{"data": {"floor_plans": [{"id": 1, "name": "X", "bedroom_count": 1}]}}` | `_is_sightmap_response` is `True` |
| `test_sightmap_tier_re_stamped_on_partial_parse` | Real SightMap payload where 80%+ of units fail the `floor_plan_id` join | `result.tier_used == "TIER_1_API_SIGHTMAP_PARSE_FAILED"` and warning in `errors` |

### Partial-parse warning (SightMap only, scope-creep-worth-it)

In `parse_sightmap_payload`, track dropped units:

```python
dropped = 0
for u in raw_units:
    # ... existing join logic ...
    if fp_id not in fp_by_id:
        dropped += 1
        continue
    # ...
# caller receives both the units_out and the dropped count; if dropped > 0.2 * len(raw_units),
# the extract() method appends a warning to result.errors even on success.
```

Return `(units_out, dropped_count)` from `parse_sightmap_payload`. Update the call site in `extract()` to emit a `SIGHTMAP_PARTIAL_JOIN` warning if `dropped_count > 0.2 * len(raw_units)`. This surfaces silent data loss inside "successful" scrapes — relevant because the 04-20 data shows TIER_1_API scrapes have 64.9% missing rent.

### Change 1 gate

- All named tests pass
- `mypy --strict ma_poc/pms/adapters/rentcafe.py ma_poc/pms/adapters/sightmap.py` clean
- `ruff check ma_poc/pms/adapters/rentcafe.py ma_poc/pms/adapters/sightmap.py` clean
- Re-run the 04-20 dataset through a dry-run replay (if replay exists per Jugnu §1.1) — the 38 RentCafe failures must now split across ≥2 distinct tier codes, not all collapse to `TIER_1_API_RENTCAFE`
- `grep -c "TIER_1_API_RENTCAFE_" data/runs/2026-04-20-replay/report.json` returns ≥ 2

---

## Change 2 — Enforce router → adapter invariant (no PMS-specific routing in adapters)

**Problem.** Adapters don't know they're being misrouted. The router must either route correctly or route to "unknown". An adapter's job is "parse this payload shape"; the router's job is "decide which adapter." Right now the detector's URL-based heuristics are matching `.aspx` or `securecafe.com` links on Windsor sites and sending them to RentCafe. That's a router bug, not an adapter bug — and Change 1 was a prerequisite so we can actually measure it.

### Files to modify

- `ma_poc/pms/detector.py` — the source of misrouting

### What to do

Add **body-shape secondary-check** to every URL-based detection. Detection stays cheap (no network), but once the Playwright page has actually loaded and captured responses, the orchestrator calls `detector.confirm_detection(initial_detection, api_responses)` which can **demote** a detection to `unknown` if no captured body matches the adapter's expected shape.

```python
# ma_poc/pms/detector.py

def confirm_detection(
    initial: DetectedPMS,
    api_responses: list[dict],
) -> DetectedPMS:
    """After page load, verify the URL-based detection against captured bodies.

    If no captured response matches the detected PMS's body shape, demote
    to 'unknown' so the generic cascade runs with LLM/Vision allowed,
    rather than stamping a misleading TIER_1_API_<PMS> label on a
    mismatched property.

    This is the counter to Windsor/Mark-Taylor/Vegas misrouting: URL
    says 'RentCafe', but body shape says 'Funnel' — the router must
    notice and step aside.
    """
    if initial.pms == "unknown":
        return initial
    # Import locally to avoid circulars
    from ma_poc.pms.adapters.registry import get_adapter
    adapter = get_adapter(initial.pms)
    # Each adapter exposes a response-body check
    checker = getattr(adapter, "matches_response_body", None)
    if checker is None:
        return initial  # adapter hasn't implemented the check; stay with URL detection
    for resp in api_responses:
        if checker(resp.get("body")):
            return initial  # at least one captured body matches — router was right
    # No body matched — demote
    return DetectedPMS(
        pms="unknown",
        confidence=0.0,
        evidence=initial.evidence + [
            f"demoted_from_{initial.pms}:no_matching_body_in_{len(api_responses)}_captures"
        ],
        pms_client_account_id=None,
        recommended_strategy="cascade",
    )
```

### Adapter protocol extension

Add a new **optional** method to the `PmsAdapter` protocol in `ma_poc/pms/adapters/base.py`:

```python
@runtime_checkable
class PmsAdapter(Protocol):
    pms_name: str

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult: ...

    def static_fingerprints(self) -> list[str]: ...

    def matches_response_body(self, body: Any) -> bool:
        """Cheap body-shape check. True if *body* plausibly belongs to this PMS.
        Used by detector.confirm_detection to demote misrouted detections.
        Default implementation returns False for backward-compat, forcing
        adapters to opt in to the invariant enforcement."""
        return False
```

For RentCafe: `return _is_rentcafe_response(body)` (reuses existing function).
For SightMap: `return _is_sightmap_response(body)` (reuses existing function).

### Orchestrator integration

In `ma_poc/scripts/daily_runner.py` and/or `jugnu_runner.py` — wherever PMS detection happens — insert `confirm_detection` between "run scrape, capture responses" and "select adapter":

```python
# existing
initial_detection = detect_pms(url, csv_row, page_html=None)
# ... page.goto, capture api_responses ...

# NEW
confirmed_detection = confirm_detection(initial_detection, api_responses)

# existing
adapter = get_adapter(confirmed_detection.pms)
result = await adapter.extract(page, ctx)
```

### Named tests — `tests/pms/test_detector.py`

| Test | Input | Expected |
|---|---|---|
| `test_confirm_detection_keeps_when_body_matches` | RentCafe URL detection + fixture with real RentCafe body | returns unchanged detection |
| `test_confirm_detection_demotes_when_no_body_matches` | RentCafe URL detection + fixtures containing only Funnel bodies | returns `pms="unknown"`, evidence contains `demoted_from_rentcafe:...` |
| `test_confirm_detection_demotes_when_no_responses` | any detection + `api_responses=[]` | returns `pms="unknown"` |
| `test_confirm_detection_leaves_unknown_alone` | `DetectedPMS(pms="unknown")` + any responses | returns unchanged |
| `test_confirm_detection_handles_adapter_without_body_check` | adapter that doesn't implement `matches_response_body` | returns initial detection unchanged (backward-compat) |

### Change 2 gate

- Named tests pass
- On a dry-run replay of 04-20, the 12 Windsor properties now route to `pms=unknown` (demoted), not `rentcafe` — confirm via the `evidence` field
- The 3 Vegas lasvegasliving.com properties similarly demote from `sightmap` to `unknown`
- **Important**: at this stage, demoted properties will STILL fail (no adapter yet), but they'll fail via the generic cascade with `tier_used` reflecting that fact (probably `TIER_4_LLM_DOM` or similar), which is honest. Change 3 provides the actual recovery.

---

## Change 3 — Funnel / Nestio adapter (NEW)

**Highest-leverage addition.** 12 confirmed Windsor properties in 04-20, likely more across the full 5K. Funnel publishes a documented public API, so this is a parser exercise, not a reverse-engineering project.

### Research (do this first, before writing code)

1. Fetch `https://developers.funnelleasing.com/api/v2/auth.html` and `https://developers.funnelleasing.com/api/v2/` (follow the docs sitemap). Save useful pages as notes under `docs/research/funnel/`.
2. For three Windsor properties from the 04-20 failure list — **65069 Olympic by Windsor, 77589 Windsor Sugarloaf, 5715 Windsor Westminster** — open the site in Playwright (`scripts/capture_raw_api.py <canonical_id>` if such a helper exists; else add one) and save full network captures.
3. Identify the Funnel API endpoint pattern. Evidence from live fetch of `windsorcommunities.com/properties/windsor-sugarloaf/floorplans/` shows Apply links target `nestiolistings.com/api/v2/onlineleasing-link?myOlePropertyId=32164&companyID=19139&UnitID=...`. The **listings** endpoint is separate and documented as `nestiolistings.com/api/v2/listings/residential/rentals/?key=<public_key>`. Find the public key in the Windsor page source (usually embedded in a JS bundle or a `data-` attribute) and capture the response.
4. Save 3 real payloads to `tests/pms/adapters/fixtures/funnel/65069.json`, `77589.json`, `5715.json`. **Do not proceed if you cannot get at least 2.** Mark the adapter `research-blocked` and surface the blocker to the user.

### Files to create

1. `ma_poc/pms/adapters/funnel.py`
2. `tests/pms/adapters/test_funnel.py`
3. `tests/pms/adapters/fixtures/funnel/` — at least 2 real captures

### Files to modify

1. `ma_poc/pms/adapters/__init__.py` — register `FunnelAdapter`
2. `ma_poc/pms/adapters/registry.py` — add `"funnel"` to accepted `pms` literals
3. `ma_poc/pms/detector.py` — add Funnel URL and HTML fingerprints
4. `ma_poc/pms/detector.py` — extend `DetectedPMS.pms` Literal to include `"funnel"`

### `funnel.py` top-of-file research log (template — fill in from real research)

```python
"""
Funnel / Nestio adapter.

Research log
------------
Web sources consulted:
  - https://developers.funnelleasing.com/api/v2/auth.html (accessed YYYY-MM-DD)
  - https://funnelleasing.com/products/ (accessed YYYY-MM-DD)
  - https://nestiolistings.com/api/v2/listings/residential/rentals/ (documented endpoint)
Real payloads inspected (from data/runs/*/raw_api/ or live captures):
  - 65069 (Olympic by Windsor, Los Angeles CA) — <endpoint> — <envelope shape>
  - 77589 (Windsor Sugarloaf, Suwanee GA) — <endpoint> — <envelope shape>
  - 5715 (Windsor Westminster) — <endpoint> — <envelope shape>
Key findings:
  - API endpoint: nestiolistings.com/api/v2/listings/residential/rentals/?key=<public_key>
  - Public key: embedded in page HTML as <FILL IN — JS var name or data- attr>
  - Response envelope: <FILL IN — e.g. "root is a list of listings; each listing has rentals[]">
  - Unit-level object: keys <FILL IN — e.g. unit, listingId, marketRent, availabilityDate, bedrooms, bathrooms, squareFeet>
  - Unit ID field: <FILL IN — listingId or unit>
  - Rent field: <FILL IN — marketRent (number) or rent (string)>
  - Availability date field: <FILL IN>
  - Known gotchas: <FILL IN — e.g. "Funnel is PMS-agnostic; the back-office may be Yardi
    but the listing API is Funnel's. Do not confuse securecafe.com resident portal links
    with the actual inventory endpoint.">
"""
```

### Adapter skeleton (fill in parser based on real payload shapes)

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_TIER_BASE = "TIER_1_API_FUNNEL"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_LIST_EMPTY = f"{_TIER_BASE}_LIST_EMPTY"
_TIER_PARSE_ZERO = f"{_TIER_BASE}_PARSE_ZERO"


# URL fingerprints — Funnel always serves through nestiolistings.com regardless of customer domain
_FUNNEL_URL_MARKERS = ("nestiolistings.com/api/v2/", "nestiostaging.com/api/")


def _is_funnel_response_url(url: str) -> bool:
    """True if *url* is a Funnel/Nestio listings API call."""
    return any(m in url for m in _FUNNEL_URL_MARKERS)


def _is_funnel_response_body(body: Any) -> bool:
    """Body-shape check for Funnel listings response.

    Expected shape (based on research log above):
      <FILL IN — confirm envelope from real captures before shipping>
    """
    # EXAMPLE — REPLACE with real envelope check
    if not isinstance(body, (list, dict)):
        return False
    if isinstance(body, list) and body and isinstance(body[0], dict):
        # List-at-root: check for listing-level keys
        keys = set(body[0].keys())
        funnel_listing_keys = {"listingId", "marketRent", "availabilityDate", "rentals"}
        return len(funnel_listing_keys & keys) >= 2
    if isinstance(body, dict):
        for list_key in ("listings", "results", "data"):
            v = body.get(list_key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                keys = set(v[0].keys())
                if {"listingId", "marketRent"} & keys:
                    return True
    return False


def parse_funnel_listings(body: Any, url: str) -> list[dict[str, str]]:
    """Parse Funnel/Nestio listings response into standard unit dicts.

    Source envelope reference: see research log at top of file.
    """
    # FILL IN based on real captures. Sketch:
    # 1. Unwrap to get the rentals/listings list
    # 2. For each rental item:
    #    - Map marketRent → rent_low/rent_high (may be flat, not a range)
    #    - Map availabilityDate → availability_date
    #    - Map bedrooms/bathrooms/squareFeet → bed_label/bathrooms/sqft
    #    - Unit ID: listingId preferred, fall back to unit
    # 3. Use make_unit_dict with extraction_tier=_TIER_BASE
    units: list[dict[str, str]] = []
    # ... implementation ...
    return units


def _classify_funnel_failure(api_responses: list[dict]) -> tuple[str, str]:
    """Same pattern as Change 1. Returns (tier_code, error_message)."""
    if not api_responses:
        return (_TIER_NO_RESPONSE,
                "FUNNEL_NO_RESPONSE: no network responses captured during page load; "
                "check if page is making calls to nestiolistings.com at all")
    url_matches = [r for r in api_responses
                   if _is_funnel_response_url(r.get("url", ""))]
    shape_matches = [r for r in api_responses
                     if _is_funnel_response_body(r.get("body"))]
    if not url_matches and not shape_matches:
        return (_TIER_SHAPE_REJECTED,
                f"FUNNEL_SHAPE_REJECTED: {len(api_responses)} responses captured, "
                f"none to nestiolistings.com and none matched Funnel envelope")
    relevant = shape_matches or url_matches
    # Peek — did the list-unwrap yield zero items?
    total_items = 0
    for r in relevant:
        items = _unwrap_funnel_list(r.get("body")) or []
        total_items += len(items)
    if total_items == 0:
        return (_TIER_LIST_EMPTY,
                f"FUNNEL_LIST_EMPTY: {len(relevant)} relevant responses, "
                f"listings list empty in all (property may have 0 availability)")
    return (_TIER_PARSE_ZERO,
            f"FUNNEL_PARSE_ZERO: {total_items} listing items present but parser "
            f"emitted zero units (field-name mismatch)")


def _unwrap_funnel_list(body: Any) -> list[Any] | None:
    """Return the rentals/listings list, handling envelope variants."""
    # FILL IN based on real captures
    ...


class FunnelAdapter:
    """Funnel / Nestio PMS adapter.

    Funnel markets itself as 'PMS-agnostic' — the listing API is always Funnel's
    even when the back-office PMS is Yardi, RealPage, or Entrata. The adapter
    must therefore rely on response shape / URL, not on clues from the customer's
    marketing domain (windsorcommunities.com, etc.).
    """

    pms_name: str = "funnel"
    _fingerprints: list[str] = [
        "nestiolistings.com",
        # Add more as encountered in captures; these are the ones confirmed from Windsor
    ]

    async def extract(self, page: "Page", ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER_BASE)
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            if not (_is_funnel_response_url(url) or _is_funnel_response_body(body)):
                continue
            units = parse_funnel_listings(body, url)
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)

        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.95, 0.7 + 0.05 * len(all_units))
            return result

        tier_code, err_msg = _classify_funnel_failure(api_responses)
        result.tier_used = tier_code
        result.confidence = 0.0
        result.errors.append(err_msg)
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        return _is_funnel_response_body(body)
```

### Detector integration (add to `ma_poc/pms/detector.py`)

```python
# In the Literal
class DetectedPMS:
    pms: Literal[..., "funnel", ...]  # add "funnel"

# In URL host fingerprints rules, add:
_FUNNEL_HOSTS = ("nestiolistings.com", "nestiostaging.com")
# These only match if page URL or captured requests touch these — but captured
# requests aren't available at URL-detection time, so:

# In HTML markers rules (runs when page_html is passed), add a regex:
# - <script src="...nestiolistings.com...">
# - window.NESTIO_* or FUNNEL_* JavaScript globals
# - data-nestio-listing-id, data-funnel-listing attributes

# In MGMT_TO_PMS_PRIOR, add:
#   "Windsor Communities": "funnel"    # confirmed via 2026-04-20 analysis
#   "Windsor Property Management": "funnel"
# Source in comment: 04-20 live-fetch of windsorcommunities.com/properties/windsor-sugarloaf/
# showed Apply buttons pointing at nestiolistings.com/api/v2/onlineleasing-link
```

### Recommended-strategy mapping (add row to detector table)

| PMS | strategy |
|---|---|
| funnel | `api_first` |

### Named tests — `tests/pms/adapters/test_funnel.py`

Fixtures: at least 2 real captures under `tests/pms/adapters/fixtures/funnel/`. If count < 2, the adapter test file itself must contain a `pytest.mark.skip(reason="research-blocked: need ≥2 real Funnel captures")`.

| Test | What it checks |
|---|---|
| `test_funnel_extract_happy_path_from_fixture` | Uses `fixtures/funnel/65069.json`; returns ≥ 1 unit with non-null `floor_plan_name` and either rent or unit_id |
| `test_funnel_extract_from_second_fixture` | Uses a different fixture to catch over-fitting (per claude_refactor.md requirement) |
| `test_funnel_returns_empty_on_no_data` | `ctx._api_responses = []` → `result.units == []`, `tier_used == TIER_1_API_FUNNEL_NO_RESPONSE` |
| `test_funnel_tier_re_stamped_on_shape_reject` | responses containing only RentCafe bodies → `tier_used == TIER_1_API_FUNNEL_SHAPE_REJECTED` |
| `test_funnel_tier_re_stamped_on_empty_list` | URL matches Funnel but listings list is `[]` → `tier_used == TIER_1_API_FUNNEL_LIST_EMPTY` |
| `test_funnel_static_fingerprints_contains_nestio` | `"nestiolistings.com" in FunnelAdapter().static_fingerprints()` |
| `test_funnel_matches_response_body_accepts_real_capture` | real fixture body → `True` |
| `test_funnel_matches_response_body_rejects_rentcafe` | RentCafe fixture body → `False` |
| `test_funnel_matches_response_body_rejects_sightmap` | SightMap fixture body → `False` |
| `test_funnel_tier_used_is_pms_specific` | success case → `tier_used` contains `"FUNNEL"` |
| `test_funnel_unit_id_format_valid` | all emitted unit IDs match the documented regex (document regex in research log) |
| `test_funnel_rent_within_sanity_range` | all emitted rents in `[200, 50000]` or null |

### Detector test additions — `tests/pms/test_detector.py`

| Test | What it checks |
|---|---|
| `test_detect_funnel_from_mgmt_windsor_communities` | `csv={"Management Company": "Windsor Communities"}` → `pms="funnel"`, conf≥0.70 |
| `test_detect_funnel_from_html_nestio_script` | `page_html` containing `<script src="https://nestiolistings.com/...">` → `pms="funnel"`, conf≥0.90 |
| `test_detect_rentcafe_no_longer_matches_windsor` | Windsor URL + CSV with `Management Company=Windsor Communities` → `pms != "rentcafe"` (Funnel wins over any RentCafe priors) |

### Change 3 gate

- All named tests pass
- At least 2 real Funnel captures exist under `tests/pms/adapters/fixtures/funnel/`
- `mypy --strict ma_poc/pms/adapters/funnel.py` clean
- `ruff check ma_poc/pms/adapters/funnel.py` clean
- The top-of-file research log has all four sections filled in with real URLs and real canonical_ids (empty placeholders fail the gate — script `scripts/gate_refactor.py` already checks this per `claude_refactor.md`)
- Dry-run replay of 04-20: 12 Windsor properties now produce `tier_used: TIER_1_API_FUNNEL` with ≥ 1 unit each, OR a machine-readable Funnel-family failure code if availability is genuinely zero

---

## Change 4 — TouchTour / Ovation adapter (NEW)

3 Vegas properties in 04-20 failure list (Summer Winds, Madera, Positano) — all at `*.mytouchtour.com` or `lasvegasliving.com`. Managed by **Ovation Property Management**.

TouchTour is proprietary; no public developer docs exist. This means the adapter ships only after live capture and reverse-engineering. Do it after Funnel because the payoff per hour is lower.

### Research

1. Fetch `https://lasvegasliving.mytouchtour.com/community/floorplans/summer_winds` and `.../madera` and `.../positano` via Playwright with full network capture.
2. Look at the XHR/fetch calls made when the floorplans list renders. Likely patterns based on common multifamily platforms: `/api/availability`, `/api/floorplans`, `/api/units`, `/assets/<property>/inventory.json`. The exact path must be confirmed from capture, not assumed.
3. Save at least 2 real captures to `tests/pms/adapters/fixtures/touchtour/`. Property IDs: 24928 (Summer Winds), 26151 (Madera), 27595 (Positano).
4. Also check `www.liveovation.com` — the Ovation parent site — to see if there's a portfolio-level endpoint that could serve as an alternate source.
5. Verify that ApartmentHomeLiving.com's listing for Madera (with units like `"Apartment Unit: 1128, Apartment Model: 1D F - Clove"`) is a syndication, not the source. The source must be TouchTour itself.

### If research yields no API

TouchTour may be server-rendered with inventory embedded in HTML rather than fetched via XHR. If so:

- This adapter is a **TIER_3_DOM + TIER_1_5_EMBEDDED hybrid**, not a clean TIER_1.
- Name the winning tier `TIER_3_DOM_TOUCHTOUR` to distinguish from generic DOM parsing.
- The parser becomes a CSS-selector-based scraper against the floorplans page DOM. Use the patterns at `ma_poc/scripts/entrata.py` DOM parser section as reference.
- The adapter still implements `matches_response_body` but returns `False` always (body-shape doesn't apply) — which means Change 2's `confirm_detection` won't demote, but that's correct for DOM-only adapters (the detection must use URL/HTML fingerprints, which are strong for `mytouchtour.com`).

### Files to create

1. `ma_poc/pms/adapters/touchtour.py`
2. `tests/pms/adapters/test_touchtour.py`
3. `tests/pms/adapters/fixtures/touchtour/` — ≥ 2 captures

### Files to modify

Same as Change 3: `adapters/__init__.py`, `registry.py`, `detector.py` — register new PMS literal `"touchtour"`, add URL fingerprint `mytouchtour.com`, add MGMT prior `"Ovation Property Management" → "touchtour"`.

### Adapter contract

Same skeleton as Funnel (`_TIER_BASE = "TIER_1_API_TOUCHTOUR"` or `"TIER_3_DOM_TOUCHTOUR"` depending on research outcome), same structured-error pattern, same `matches_response_body` / `static_fingerprints` / research-log header.

### Named tests — at minimum

| Test | What it checks |
|---|---|
| `test_touchtour_extract_happy_path_from_fixture` | `fixtures/touchtour/24928.json` or equivalent → ≥ 1 unit with name + rent/id |
| `test_touchtour_extract_from_second_fixture` | second property, no overfitting |
| `test_touchtour_returns_empty_on_no_data` | empty input → empty `units`, correct tier code |
| `test_touchtour_static_fingerprints_contains_mytouchtour` | `"mytouchtour.com" in static_fingerprints()` |
| `test_touchtour_detector_routes_from_mytouchtour_url` | `detect_pms("https://lasvegasliving.mytouchtour.com/community/floorplans/summer_winds")` → `pms="touchtour"`, conf ≥ 0.95 |
| `test_touchtour_detector_routes_from_mgmt_ovation` | `csv={"Management Company": "Ovation Property Management"}` → `pms="touchtour"`, conf ≥ 0.70 |
| `test_touchtour_sightmap_no_longer_matches_vegas` | Vegas property URL → `pms != "sightmap"` |

### Change 4 gate

Same pattern as Change 3. Dry-run replay: 3 Vegas properties now produce `TIER_1_API_TOUCHTOUR` (or `TIER_3_DOM_TOUCHTOUR`) with ≥ 1 unit each, or a machine-readable TouchTour failure code.

---

## Change 5 — Tier 4 LLM escalation gate

**Problem.** The 04-20 run spent **$0.94** on 13 TIER_1_API properties that failed. LLM fallback was triggered on empty/garbage payloads. Per the analysis above, 5 of those 13 are on `gscapts.com` (one cluster). The escalation policy should be: don't run Tier 4 LLM if Tier 1 captured nothing meaningful.

### Files to modify

- `ma_poc/services/llm_extractor.py` (or wherever Tier 4 invocation lives — check `claude-scrapper-arch.md`)
- The orchestration site that chains tiers (likely `ma_poc/scripts/daily_runner.py` or `jugnu_runner.py`)

### What to do

Before invoking Tier 4 LLM DOM extraction, check an escalation gate:

```python
def should_escalate_to_llm(
    tier1_result: AdapterResult | None,
    tier2_result: list[dict] | None,  # JSON-LD units
    tier3_result: list[dict] | None,  # DOM units
    page_has_meaningful_body: bool,  # True if HTML > 1KB and contains ≥ 1 "rent"/"apartment"/"floor" token
) -> tuple[bool, str]:
    """Return (should_escalate, reason).

    Policy:
    - If Tier 1 adapter captured at least one shape-matched response (even if it
      parsed to zero units), DO escalate — the LLM can sometimes salvage a
      partial payload.
    - If Tier 1 ran and got TIER_1_API_*_NO_RESPONSE or _SHAPE_REJECTED, AND
      Tier 2/3 also returned empty, AND the page body isn't meaningful, SKIP.
      This is the 'we have nothing to send to the LLM' gate.
    - If the page body is meaningful but all 3 deterministic tiers failed,
      DO escalate — this is the LLM's actual job.
    """
    if not page_has_meaningful_body:
        return (False, "LLM_GATE_NO_BODY: page body too short or no rent tokens")
    if tier1_result and tier1_result.api_responses:
        # Shape matched somewhere — worth trying the LLM
        return (True, "LLM_GATE_TIER1_SHAPE_MATCHED")
    if tier2_result or tier3_result:
        # Deterministic tiers got something — LLM shouldn't be needed
        return (False, "LLM_GATE_DETERMINISTIC_TIERS_SUFFICED")
    # Page has content but all structured extraction failed
    return (True, "LLM_GATE_FALLBACK_JUSTIFIED")
```

### Named tests — `tests/services/test_llm_gate.py`

| Test | Input | Expected |
|---|---|---|
| `test_llm_gate_skips_on_no_body` | `page_has_meaningful_body=False`, everything else None | `(False, "LLM_GATE_NO_BODY")` |
| `test_llm_gate_escalates_on_shape_matched_but_empty` | tier1 with `api_responses=[...]` but `units=[]` | `(True, "LLM_GATE_TIER1_SHAPE_MATCHED")` |
| `test_llm_gate_skips_when_tier3_got_units` | tier3 returned 5 units | `(False, "LLM_GATE_DETERMINISTIC_TIERS_SUFFICED")` |
| `test_llm_gate_escalates_on_fallback` | meaningful body, no tier1/2/3 units, no tier1 responses | `(True, "LLM_GATE_FALLBACK_JUSTIFIED")` |

### Change 5 gate

- Named tests pass
- Dry-run replay of 04-20: total LLM spend drops by ≥ $0.50 (targeting the 13 TIER_1_API wasted calls). Specifically, the 13 properties listed in the failure analysis — [45791, 77558, 6477, 58699, 21589, 4953, 242945, 71773, 1783, 61377, 78179, 5859, 221319] — should see their `llm_cost_usd == 0` if the escalation gate fires correctly.

---

## Change 6 — Post-change verification & replay

### Files to create (if not already present)

- `scripts/replay_run.py` — takes a `data/runs/<date>/` directory and re-runs extraction against its `raw_api/` captures without re-hitting the network. Already specified in Jugnu §1.1 ("replay against 30-day raw HTML store").

If `replay_run.py` doesn't exist, skip this change's automated verification and instead run Change 1–5 gates manually on live traffic against a 20-property canary subset. Document the canary list in `docs/2026-04-20-adapter-fix-canary.md`.

### Canary subset (if replay unavailable)

One property per failure class, from the 04-20 list:

| Class | Property | Expected 2026-04-21 outcome |
|---|---|---|
| RentCafe misroute → Funnel | 65069 Olympic by Windsor | `TIER_1_API_FUNNEL`, ≥ 1 unit |
| SightMap misroute → TouchTour | 24928 Summer Winds | `TIER_1_API_TOUCHTOUR` (or DOM variant), ≥ 1 unit |
| Genuine RentCafe (control) | 35593 The Continental | unchanged: `TIER_1_API_RENTCAFE`, ≥ 1 unit |
| Genuine SightMap (control) | 268836 Hawthorne at Traditions | unchanged: `TIER_1_API_SIGHTMAP`, ≥ 1 unit |
| TIER_1_API LLM waste | 6477 Harbour Pointe | `llm_cost_usd == 0` (gate fires), tier code reflects skip |
| Genuine fallback (control) | any property currently succeeding on TIER_4_LLM_DOM | unchanged |

### Full validation gate (Change 6)

Run against a 200-property canary (same distribution as 04-20 if possible):

- **Success rate ≥ 75%** (currently 64%) — 11-point lift from Funnel + TouchTour recoveries alone
- **LLM spend per run ≤ $1.50** (currently $2.42) — escalation gate removes ~$0.94
- **Zero `TIER_1_API_RENTCAFE` failures that are actually Funnel sites** — verify by cross-referencing with the 12 Windsor properties from 04-20
- **Zero `TIER_1_API_SIGHTMAP` failures that are actually TouchTour sites** — verify against the 3 Vegas properties
- **All RentCafe + SightMap failures now have a specific sub-tier code** — no bare `TIER_1_API_RENTCAFE` on a failed property
- `pytest ma_poc/ tests/ --ignore=data --ignore=config` — all green
- `scripts/gate_refactor.py phase 3` passes (per `claude_refactor.md`)

---

## File creation order

Strict order. Each step follows the mandatory workflow: generate → implement → write tests immediately → run tests → static analysis → gate → next step.

1. `ma_poc/pms/adapters/rentcafe.py` (modify) — Change 1 tier re-stamping + value-case fix
2. `tests/pms/adapters/test_rentcafe.py` (modify) — add 7 new tests
3. `ma_poc/pms/adapters/sightmap.py` (modify) — Change 1 tier re-stamping + tightened shape check + partial-parse warning
4. `tests/pms/adapters/test_sightmap.py` (modify) — add 5 new tests
5. **→ Change 1 gate**
6. `ma_poc/pms/adapters/base.py` (modify) — add `matches_response_body` to `PmsAdapter` Protocol with default `False`
7. `ma_poc/pms/adapters/rentcafe.py` (modify) — implement `matches_response_body`
8. `ma_poc/pms/adapters/sightmap.py` (modify) — implement `matches_response_body`
9. `ma_poc/pms/detector.py` (modify) — add `confirm_detection` function
10. `tests/pms/test_detector.py` (modify) — add 5 `confirm_detection` tests
11. Orchestrator integration (`daily_runner.py` and/or `jugnu_runner.py`) — insert `confirm_detection` call
12. **→ Change 2 gate**
13. **Funnel research** — live captures, save to `tests/pms/adapters/fixtures/funnel/` (≥ 2 files)
14. `ma_poc/pms/adapters/funnel.py` (new)
15. `tests/pms/adapters/test_funnel.py` (new) — 12 tests
16. `ma_poc/pms/adapters/__init__.py` (modify) — register `FunnelAdapter`
17. `ma_poc/pms/adapters/registry.py` (modify) — add `"funnel"` to accepted literals
18. `ma_poc/pms/detector.py` (modify) — add Funnel fingerprints + MGMT prior + Literal extension
19. `tests/pms/test_detector.py` (modify) — add 3 Funnel routing tests
20. **→ Change 3 gate**
21. **TouchTour research** — live captures, save fixtures (≥ 2 files)
22. `ma_poc/pms/adapters/touchtour.py` (new)
23. `tests/pms/adapters/test_touchtour.py` (new) — 7 tests
24. Register TouchTour in `__init__.py`, `registry.py`, `detector.py`
25. Add 3 TouchTour routing tests to `tests/pms/test_detector.py`
26. **→ Change 4 gate**
27. `ma_poc/services/llm_gate.py` (new) — `should_escalate_to_llm`
28. `tests/services/test_llm_gate.py` (new) — 4 tests
29. Wire into orchestrator
30. **→ Change 5 gate**
31. `scripts/replay_run.py` (new, if missing) — per Jugnu §1.1
32. `docs/2026-04-20-adapter-fix-canary.md` — canary list + expected outcomes
33. Run Change 6 full validation gate

Total: 6 new files, 9 modified files, 41 new test cases, 6 gates.

---

## What explicitly NOT to do

- Do not add more keys to `_RENTCAFE_WRAPPER_KEYS` or `_unwrap_rentcafe_list` in `rentcafe.py`. The Windsor/Mark-Taylor properties are not RentCafe. Expanding the RentCafe parser to "handle" them would be doubling down on the misrouting bug.
- Do not ship Funnel before the research log has ≥ 2 real captures. Shipping an adapter grounded in assumed field names is how the 04-19 SightMap "fix" ended up missing all its named targets.
- Do not skip Change 1 to jump to Funnel. Without structured error codes, you cannot tell whether Funnel is recovering the Windsor properties or just replacing one opaque failure with another.
- Do not add Union / Liftlytics in this pass. Only 2 properties affected; the reverse-engineering cost is not justified until a broader sample (post-5K-list) confirms the scope.
- Do not modify the `UNKNOWN` tier bucket (7 props in 04-20). That's a separate bug in verdict classification and has its own fix ticket.

---

## Links & references

- `Jugnu_Robust_Crawler_Architecture.docx` v1.0 — overall architecture for router + adapter separation
- `claude_refactor.md` — adapter protocol, registry pattern, test conventions, gate discipline
- `claude-scrapper-arch.md` — ScrapeProfile lifecycle (for profile updates after adapter changes)
- 2026-04-20 run data: `data/runs/2026-04-20/report.json`, `properties.json`
- Live research captures: `docs/research/funnel/`, `docs/research/touchtour/`
- Funnel developer docs: https://developers.funnelleasing.com/api/v2/auth.html