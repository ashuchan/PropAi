# CLAUDE_ADAPTER_FIXES.md

> Read this entire document before writing any code. There are two independent
> fixes — one per file. Each fix has hard non-negotiable constraints and a gate
> that must pass before you commit.

---

## Context

The 2026-04-19 Jugnu run produced a 50% success rate against a 95% SLO target.
Root-cause analysis identified two adapters as responsible for 19 of the 50
failures:

- `ma_poc/pms/adapters/rentcafe.py` — 15 FAILED_NO_DATA (100% failure rate)
- `ma_poc/pms/adapters/sightmap.py` — 4 FAILED_NO_DATA (100% failure rate)

Both adapters correctly **detect** their PMS (the tier label is set) but extract
zero units. The bugs are entirely in post-detection response parsing — not in
fingerprinting, fetch, or profile logic.

---

## Non-negotiables (apply to both fixes)

1. **No imports added.** Both files use only stdlib + existing internal imports.
   Do not add any new import lines.
2. **Function signatures are frozen.** `parse_rentcafe_floorplans`,
   `_unwrap_rentcafe_list`, `_is_rentcafe_response`, `parse_sightmap_payload`
   — argument lists and return types must not change.
3. **Backward compatibility.** Every payload that passed before must still pass
   after. The existing research-log payload (property 35593, lowercase keys,
   root-level list) must continue to work.
4. **Write tests immediately after each file edit** — before moving to the next
   fix. Tests live in `tests/pms/adapters/`.
5. **Run tests after each file.** Gate: all new tests green, no regressions in
   the existing adapter test suite.
6. **Research before coding SightMap.** Before writing SM-Fix-2, you MUST search
   `data/runs/*/raw_api/` for any file whose name starts with `24928`, `24929`,
   `liveotis`, `ovationco`, or `sightmap`. If you find a real unit-bearing
   payload, document its field names in the research log comment at the top of
   `sightmap.py` before writing the fix. If you find nothing, note that
   explicitly and proceed with the fix as specified.

---

## Fix 1 — `rentcafe.py` (RC-1 + RC-2 + RC-3)

### What is broken

**RC-1 — `_is_rentcafe_response` uses case-sensitive key intersection.**

The fingerprint set is `{"floorplanName", "floorplanId", "minimumRent", ...}`.
Windsor Communities, Weidner, Bexley, and Pacifica Residential all serve Yardi
APIs that return PascalCase keys: `FloorplanName`, `FloorplanId`, `MinimumRent`,
`MaximumRent`, `AvailableUnitsCount`. The intersection of those two sets is
always empty → `_is_rentcafe_response` returns `False` → response silently
skipped → `FAILED_NO_DATA`.

This is the root cause for 8–12 of the 15 failures. The `_unwrap_rentcafe_list`
docstring says it fixed the Windsor wrapper problem, but that fix only handles
the *outer dict key* (`"data"`, `"Result"`, etc.). The inner item-field check
still case-matches → the fix is half-complete.

**RC-2 — `parse_rentcafe_floorplans` has the same case-sensitivity bug.**

Even if RC-1 is fixed, the parser produces empty records because every field
access uses the lowercase form:

```python
item.get("floorplanName")       # misses "FloorplanName"
item.get("beds")                # misses "Beds"
item.get("minimumSQFT")        # misses "MinimumSQFT"
item.get("minimumRent")        # misses "MinimumRent"
item.get("floorplanId")        # misses "FloorplanId"
```

**RC-3 — `_RENTCAFE_WRAPPER_KEYS` is incomplete.**

Missing wrapper keys observed in the wild:
- `"Floorplans"` (capital F, no camel — distinct from `"floorPlans"` already in
  the tuple)
- `"FloorplanList"` (Yardi SOAP-derived JSON)
- `"GetFloorplansResult"` (some Yardi-hosted endpoints)

Also: `_unwrap_rentcafe_list` is strictly 1-level deep. Some Yardi endpoints nest
2 levels (`{"response": {"result": [...]}}`, `{"Property": {"Floorplans": [...]}}`).

### Exact changes required

**Step 1 — Add a normalisation helper above `_is_rentcafe_response`.**

Insert this function between `_unwrap_rentcafe_list` and `_is_rentcafe_response`:

```python
def _normalise_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *item* with all keys lowercased.

    RentCafe/Yardi APIs are inconsistent in casing across management companies
    (e.g. Windsor Communities uses PascalCase while Brookfield uses camelCase).
    Normalising to lowercase lets all downstream field lookups use a single key.
    Called by both _is_rentcafe_response and parse_rentcafe_floorplans.
    """
    return {k.lower(): v for k, v in item.items()}
```

**Step 2 — Update `_is_rentcafe_response`.**

Replace the body of `_is_rentcafe_response` (lines 116–128 in the original) with:

```python
def _is_rentcafe_response(body: Any) -> bool:
    """Check if a response body looks like RentCafe floorplan data."""
    items = _unwrap_rentcafe_list(body)
    if not items:
        return False
    first = items[0]
    if not isinstance(first, dict):
        return False
    first_lc = _normalise_item(first)
    if first_lc.get("api") == "rentcafe":
        return True
    rentcafe_keys = {"floorplanname", "floorplanid", "minimumrent", "maximumrent",
                     "availableunitscount", "availabilityurl"}
    return len(rentcafe_keys & set(first_lc.keys())) >= 3
```

Key changes:
- Calls `_normalise_item(first)` to lowercase all keys before the intersection.
- Fingerprint set is now all-lowercase (note: `"floorplanname"` not
  `"floorplanName"`).
- The `api == "rentcafe"` check also goes through `first_lc`.

**Step 3 — Update `parse_rentcafe_floorplans`.**

Replace the first line inside the `for item in items:` loop body with a
normalisation call, then update every `item.get(...)` to use `item_lc`:

```python
def parse_rentcafe_floorplans(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse a RentCafe/Yardi floorplan list into standard unit dicts."""
    units: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_lc = _normalise_item(item)

        name = str(item_lc.get("floorplanname") or "")
        beds_raw = item_lc.get("beds")
        baths_raw = item_lc.get("baths")
        beds = int(beds_raw) if beds_raw is not None else None
        baths_str = str(baths_raw) if baths_raw is not None else None
        baths = int(float(baths_str)) if baths_str is not None else None

        sqft_lo = str(item_lc.get("minimumsqft") or item_lc.get("minsqft") or "")
        sqft_hi = str(item_lc.get("maximumsqft") or item_lc.get("maxsqft") or "")
        sqft = sqft_lo if sqft_lo == sqft_hi or not sqft_hi else f"{sqft_lo}-{sqft_hi}"

        # Prefer numeric min_price/max_price; fall back to string minimumRent/maximumRent
        rent_lo_raw = item_lc.get("min_price")
        if rent_lo_raw is not None and rent_lo_raw != "":
            rent_lo = int(rent_lo_raw) if rent_lo_raw else None
        else:
            rent_lo = money_to_int(str(item_lc.get("minimumrent") or ""))

        rent_hi_raw = item_lc.get("max_price")
        if rent_hi_raw is not None and rent_hi_raw != "":
            rent_hi = int(rent_hi_raw) if rent_hi_raw else None
        else:
            rent_hi = money_to_int(str(item_lc.get("maximumrent") or ""))

        avail_count = str(item_lc.get("availableunitscount") or item_lc.get("unitscount") or "")
        avail_date = str(item_lc.get("availabledate") or "")

        units.append(make_unit_dict(
            floor_plan_name=name,
            bed_label=bed_label_from(beds, name),
            bedrooms=str(beds) if beds is not None else "",
            bathrooms=str(baths) if baths is not None else "",
            sqft=sqft,
            unit_number=str(item_lc.get("floorplanid") or ""),
            rent_range=format_rent_range(rent_lo, rent_hi),
            availability_status="AVAILABLE" if avail_count and avail_count != "0" else "UNAVAILABLE",
            available_units=avail_count,
            availability_date=avail_date,
            source_api_url=url,
            extraction_tier="TIER_1_API_RENTCAFE",
        ))
    return units
```

Note: `item.get("floorplanId") or item.get("floorPlanId")` collapses to a single
`item_lc.get("floorplanid")` because normalisation handles both input variants.

**Step 4 — Extend `_RENTCAFE_WRAPPER_KEYS` and add 2-level unwrap.**

Replace the tuple and the `_unwrap_rentcafe_list` function:

```python
_RENTCAFE_WRAPPER_KEYS = (
    "data", "results", "floorplans", "floorPlans", "Floorplans",
    "FloorplanList", "GetFloorplansResult", "items", "Result",
)

# Keys used when the list is nested two levels deep, e.g.
# {"response": {"result": [...]}} or {"Property": {"Floorplans": [...]}}
_RENTCAFE_WRAPPER_KEYS_L2: tuple[tuple[str, str], ...] = (
    ("response", "result"),
    ("Property", "Floorplans"),
    ("property", "floorplans"),
)


def _unwrap_rentcafe_list(body: Any) -> list[Any] | None:
    """Return the floorplan list inside common wrapper shapes, or None.

    Handles:
    - Root-level list: [...]
    - Single-level dict wrapper: {"data": [...]} / {"Result": [...]} / etc.
    - Two-level dict wrapper: {"response": {"result": [...]}} / {"Property": {"Floorplans": [...]}}

    Why: the original matcher only accepted root-level lists. Sites like
    windsorcommunities.com wrap the same RentCafe payload as
    ``{"data": [...]}`` or ``{"Result": [...]}`` (Yardi-style), so 12 of
    13 RentCafe NO_DATA properties in the 2026-04-19 run were silently
    rejected even when the API was successfully captured.
    """
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in _RENTCAFE_WRAPPER_KEYS:
            v = body.get(k)
            if isinstance(v, list) and v:
                return v
        for outer, inner in _RENTCAFE_WRAPPER_KEYS_L2:
            outer_val = body.get(outer)
            if isinstance(outer_val, dict):
                v = outer_val.get(inner)
                if isinstance(v, list) and v:
                    return v
    return None
```

**Step 5 — Update the research log comment at the top of `rentcafe.py`.**

Append to the "Key findings" section:

```
  - 2026-04-19 fix: Windsor Communities, Weidner, Bexley, Pacifica Residential
    all use PascalCase keys (FloorplanName, FloorplanId, MinimumRent, etc.).
    _normalise_item() lowercases all item keys before fingerprinting and parsing.
    _unwrap_rentcafe_list extended with Floorplans, FloorplanList,
    GetFloorplansResult, and two-level nesting support.
```

---

### Tests to write — `tests/pms/adapters/test_rentcafe.py`

Add the following test cases to the existing test file. If the file does not
exist, create it with the standard boilerplate (imports, fixtures).

Write each test as an independent function. All must pass.

**RC_T01 — existing lowercase payload still works (regression guard)**
```
Input: root-level list, lowercase keys, api=="rentcafe" marker
Expected: _is_rentcafe_response returns True, parse produces 1+ units with
          non-empty floor_plan_name, non-None rent_low
```

**RC_T02 — PascalCase root-level list is fingerprinted correctly**
```
Input: [{"FloorplanName": "1BR", "FloorplanId": "FP1", "MinimumRent": "2100.00",
          "MaximumRent": "2400.00", "AvailableUnitsCount": 2, "Beds": 1, "Baths": 1,
          "MinimumSQFT": "700", "MaximumSQFT": "750"}]
Expected: _is_rentcafe_response returns True
```

**RC_T03 — PascalCase items parse to correct field values**
```
Input: same payload as RC_T02
Expected: parse_rentcafe_floorplans returns 1 unit dict where:
          floor_plan_name == "1BR"
          bedrooms == "1"
          rent_low (extracted from rent_range) is non-None and > 0
          unit_number == "FP1"
```

**RC_T04 — {"data": [PascalCase items]} wrapper is unwrapped and parsed**
```
Input: {"data": [{"FloorplanName": "Aspen", "FloorplanId": "A1", "Beds": 1,
                  "MinimumRent": "2195.00", "MaximumRent": "2395.00",
                  "AvailableUnitsCount": 3, "Baths": 1,
                  "MinimumSQFT": "685", "MaximumSQFT": "695"}]}
Expected: _is_rentcafe_response returns True, parse_rentcafe_floorplans
          extracts 1 unit with floor_plan_name == "Aspen"
```

**RC_T05 — {"Result": [lowercase items]} wrapper is unwrapped (existing wrapper)**
```
Input: {"Result": [{"floorplanName": "Studio", "floorplanId": "S1",
                    "minimumRent": "1500.00", "availableUnitsCount": 1,
                    "availabilityURL": "https://securecafe.com/..."}]}
Expected: _is_rentcafe_response returns True, 1 unit extracted
```

**RC_T06 — {"Floorplans": [...]} new wrapper key is handled**
```
Input: {"Floorplans": [{"floorplanName": "2BR", "api": "rentcafe",
                         "minimumRent": "2500.00", "availableUnitsCount": 1}]}
Expected: _is_rentcafe_response returns True
```

**RC_T07 — two-level {"response": {"result": [...]}} is unwrapped**
```
Input: {"response": {"result": [{"floorplanName": "1BR", "api": "rentcafe",
                                  "minimumRent": "1800.00", "availableUnitsCount": 2}]}}
Expected: _unwrap_rentcafe_list returns the inner list, _is_rentcafe_response True
```

**RC_T08 — all-UNAVAILABLE floorplans are extracted (availableUnitsCount==0)**
```
Input: [{"floorplanName": "3BR", "floorplanId": "FP3", "minimumRent": "3000.00",
          "maximumRent": "3200.00", "availableUnitsCount": 0, "beds": 3, "baths": 2}]
Expected: 1 unit extracted, availability_status == "UNAVAILABLE",
          rent_low is non-None (0-available units are still valid data points)
```

**RC_T09 — non-RentCafe JSON body is rejected**
```
Input: {"amenities": [{"id": 1, "name": "Pool"}], "events": []}
Expected: _is_rentcafe_response returns False
```

**RC_T10 — empty list body is rejected**
```
Input: []
Expected: _is_rentcafe_response returns False, _unwrap_rentcafe_list returns None
```

---

### Gate — RentCafe fix

Run: `pytest tests/pms/adapters/test_rentcafe.py -v`

Pass criteria:
- All 10 new tests (RC_T01–RC_T10) green.
- No pre-existing rentcafe test failures.

Do not proceed to Fix 2 until this gate is green.

---

## Fix 2 — `sightmap.py` (SM-1 + SM-2)

### What is broken

**SM-1 — URL filter rejects proxied responses.**

```python
if "sightmap.com" not in url:
    continue
```

`lasvegasliving.com` is a multi-property portal that proxies SightMap data
through its own CDN domain. None of the captured API responses from that domain
contain `"sightmap.com"` in the URL, so every body is silently skipped despite
potentially containing valid SightMap-shaped JSON. This accounts for at least 2
of the 4 SightMap failures (Summer Winds and Madera, both on lasvegasliving.com).

The fix: replace the URL filter with a **body-shape check**. A response should be
processed if its body looks like SightMap data, regardless of which domain served
it. The existing `parse_sightmap_payload` already checks `body.get("data")` —
we need an equivalent `_is_sightmap_response` guard.

**SM-2 — Parser written without a single real unit-bearing payload.**

The research log explicitly states all 3 inspected payloads were amenities-only
(no `units[]`). The parser's join logic (`data.units[]` ↔ `data.floor_plans[]`
via `floor_plan_id`) is inferred from documentation, not from observed data.
Field names may be incorrect. Additionally, the adapter cannot distinguish three
distinct "empty" scenarios:

1. Amenities-only endpoint (map configured without unit data — data is at a
   *different* sightmap endpoint)
2. Property genuinely has zero available units (all leased)
3. Parse failure (units[] exists but join produces nothing due to wrong field names)

All three currently emit the same generic error string.

### Exact changes required

**Step 0 — Research gate (mandatory before writing any code).**

Run: `find data/runs -name "*.json" | xargs grep -l "sightmap" 2>/dev/null | head -20`

Also run: `ls data/runs/*/raw_api/ | grep -i "24928\|otis\|ovation\|sightmap" 2>/dev/null`

If you find any file whose body contains `"units"` with non-empty array alongside
`"floor_plans"`, treat it as the reference payload. Document the real field names
in the research log at the top of `sightmap.py` before proceeding.

If no unit-bearing payload is found (which is likely given the research log),
note "No unit-bearing SightMap payload found in raw_api store as of 2026-04-19"
in the research log and proceed with the fix as written below — the field names
in the current parser are consistent with the official SightMap API docs and are
left unchanged.

**Step 1 — Add `_is_sightmap_response` above `SightMapAdapter`.**

Insert this function between `parse_sightmap_payload` and `class SightMapAdapter`:

```python
def _is_sightmap_response(body: Any) -> bool:
    """Return True if *body* looks like a SightMap API response.

    Matches on body shape rather than source URL so that portal sites
    (e.g. lasvegasliving.com) that proxy SightMap data through their own
    CDN domain are handled correctly.

    Positive match criteria (any one sufficient):
    - body is a dict with a "data" key whose value has a "units" or
      "floor_plans" or "amenities" subkey (SightMap data envelope)
    - body["data"]["sightmap_id"] exists (direct SightMap identifier)
    """
    if not isinstance(body, dict):
        return False
    data = body.get("data")
    if not isinstance(data, dict):
        return False
    sightmap_keys = {"units", "floor_plans", "amenities", "sightmap_id"}
    return bool(sightmap_keys & set(data.keys()))
```

**Step 2 — Replace the URL filter in `SightMapAdapter.extract` with the body check.**

In `SightMapAdapter.extract`, replace:

```python
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            if "sightmap.com" not in url:
                continue
            if not isinstance(body, dict):
                continue
            units = parse_sightmap_payload(body, url)
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)
```

With:

```python
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            if not isinstance(body, dict):
                continue
            if not _is_sightmap_response(body):
                continue
            units = parse_sightmap_payload(body, url)
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)
```

**Step 3 — Add diagnostic error differentiation in the zero-units path.**

Replace the current else-branch at the end of `SightMapAdapter.extract`:

```python
        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.95, 0.7 + 0.05 * len(all_units))
        else:
            result.confidence = 0.0
            result.errors.append("No SightMap unit data found in captured API responses")
```

With:

```python
        if all_units:
            result.units = all_units
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            result.confidence = min(0.95, 0.7 + 0.05 * len(all_units))
        else:
            result.confidence = 0.0
            sightmap_responses = [
                r for r in api_responses
                if isinstance(r.get("body"), dict) and _is_sightmap_response(r.get("body"))
            ]
            if not sightmap_responses:
                result.errors.append(
                    "SIGHTMAP_NO_RESPONSE: no SightMap-shaped response captured — "
                    "check if the page loads sightmap.com assets at all"
                )
            else:
                for r in sightmap_responses:
                    data = r.get("body", {}).get("data", {})
                    raw_units = data.get("units") or []
                    if not raw_units:
                        result.errors.append(
                            f"SIGHTMAP_AMENITIES_ONLY: sightmap response at {r.get('url','?')[:80]} "
                            f"has no units[] — map may be configured as amenities-only; "
                            f"check for a separate /available or /assets endpoint"
                        )
                    else:
                        result.errors.append(
                            f"SIGHTMAP_PARSE_FAILED: units[] present ({len(raw_units)} entries) "
                            f"but join produced 0 records — field name mismatch likely; "
                            f"inspect raw_api payload for {r.get('url','?')[:80]}"
                        )
```

**Step 4 — Update the research log comment at the top of `sightmap.py`.**

Append to the "Known gotchas" section:

```
    - 2026-04-19 fix: removed "sightmap.com" URL filter from extract().
      lasvegasliving.com (Summer Winds, Madera) proxies SightMap data through
      its own CDN — no sightmap.com in the response URL. Replaced with
      _is_sightmap_response() body-shape check so any domain serving
      SightMap-shaped JSON is matched.
    - 2026-04-19: added three-way error differentiation: SIGHTMAP_NO_RESPONSE
      vs SIGHTMAP_AMENITIES_ONLY vs SIGHTMAP_PARSE_FAILED.
```

---

### Tests to write — `tests/pms/adapters/test_sightmap.py`

**SM_T01 — sightmap.com URL still works (regression guard)**
```
Input: resp with url="https://sightmap.com/app/api/v1/abc/sightmaps/123",
       body={"data": {"units": [{"floor_plan_id": 1, "price": 1800, "unit_number": "101",
                                  "area": 720, "available_on": "2026-05-01"}],
                      "floor_plans": [{"id": 1, "name": "1BR", "bedroom_count": 1,
                                       "bathroom_count": 1}]}}
Expected: _is_sightmap_response returns True, extract() returns 1 unit
          with bedrooms=="1", rent non-empty, unit_number=="101"
```

**SM_T02 — proxied URL (no sightmap.com) with valid body is now matched**
```
Input: resp with url="https://lasvegasliving.com/api/properties/123/availability",
       body same as SM_T01
Expected: _is_sightmap_response returns True, extract() returns 1 unit
          (previously this was silently skipped by the URL filter)
```

**SM_T03 — amenities-only response produces SIGHTMAP_AMENITIES_ONLY error**
```
Input: resp with url="https://sightmap.com/app/api/v1/abc/sightmaps/456",
       body={"data": {"amenities": [{"id": 1, "name": "Pool"}],
                      "floor_plans": [], "units": []}}
Expected: extract() returns 0 units,
          result.errors contains a string starting with "SIGHTMAP_AMENITIES_ONLY"
```

**SM_T04 — no sightmap response at all produces SIGHTMAP_NO_RESPONSE error**
```
Input: api_responses = [{"url": "https://example.com/api/other", "body": {"foo": "bar"}}]
Expected: extract() returns 0 units,
          result.errors contains a string starting with "SIGHTMAP_NO_RESPONSE"
```

**SM_T05 — units[] present but floor_plans[] empty produces SIGHTMAP_PARSE_FAILED**
```
Input: body={"data": {"units": [{"floor_plan_id": 99, "price": 1500}],
                      "floor_plans": []}}
Expected: extract() returns 0 units,
          result.errors contains a string starting with "SIGHTMAP_PARSE_FAILED"
```

**SM_T06 — _is_sightmap_response rejects non-SightMap body**
```
Input: {"floorplanName": "1BR", "minimumRent": "1800"}
Expected: _is_sightmap_response returns False
```

**SM_T07 — _is_sightmap_response matches on sightmap_id field alone**
```
Input: {"data": {"sightmap_id": 80671, "other_stuff": []}}
Expected: _is_sightmap_response returns True
```

**SM_T08 — unit without matching floor_plan produces record with empty beds/baths**
```
Input: body with units=[{floor_plan_id: 999 (no matching fp), price: 2000,
                          unit_number: "A1", area: 800}],
       floor_plans=[]
Expected: 0 units extracted (the join fails), SIGHTMAP_PARSE_FAILED in errors
```

---

### Gate — SightMap fix

Run: `pytest tests/pms/adapters/test_sightmap.py -v`

Pass criteria:
- All 8 new tests (SM_T01–SM_T08) green.
- No pre-existing sightmap test failures.

---

## Final gate (run after both fixes)

```bash
pytest tests/pms/adapters/ -v
```

Pass criteria:
- All 18 new tests green (RC_T01–RC_T10 + SM_T01–SM_T08).
- Zero regressions across the full `tests/pms/adapters/` suite.
- `mypy ma_poc/pms/adapters/rentcafe.py ma_poc/pms/adapters/sightmap.py --ignore-missing-imports` exits 0.

Do not mark this task complete until the final gate passes.

---

## What is explicitly out of scope

- Do not touch `detector.py`, `base.py`, `_parsing.py`, `registry.py`, or any
  other adapter file.
- Do not add logging imports or change the logging strategy — the new error
  strings go into `result.errors` as plain strings, matching the existing pattern.
- Do not attempt to fix the `$1.22 LLM cost not surfaced per-property` issue —
  that is a separate task tracked in the Jugnu observability layer.
- Do not add retries, proxy handling, or any fetch-layer logic. These files are
  pure parsers.